"""End-to-end tests: start keydrop on localhost, submit the form over HTTP, check what the sink got.

Run: python3 -m unittest discover -s tests
The Keychain test writes and then deletes one item in the login keychain; it runs only when
KEYDROP_TEST_KEYCHAIN=1.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "plugins" / "keydrop" / "skills" / "keydrop" / "keydrop.py"
TOKEN = "sk-test-" + "a" * 24


class Drop:
    def __init__(self, *args: str, timeout: int = 20):
        self.proc = subprocess.Popen(
            [sys.executable, "-I", str(SCRIPT), "--bind", "localhost", "--timeout", str(timeout), "--lang", "en", *args],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        line = self.proc.stdout.readline()
        if not line:
            raise AssertionError(f"keydrop did not start: {self.proc.stderr.read()}")
        self.listening = json.loads(line)
        self.url = self.listening["url"]

    def post(self, url: str | None = None, **fields: str) -> tuple[int, str]:
        data = urllib.parse.urlencode(fields).encode()
        try:
            with urllib.request.urlopen(url or self.url, data=data, timeout=10) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def get(self, url: str) -> int:
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    def finish(self) -> tuple[int, list[dict], str]:
        out, err = self.proc.communicate(timeout=30)
        return self.proc.returncode, [json.loads(x) for x in out.splitlines() if x.strip()], err


class EnvSinkTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.env = Path(self.dir.name) / ".env"

    def tearDown(self):
        self.dir.cleanup()

    def test_saves_token_and_keeps_other_lines(self):
        self.env.write_text("KEEP=1\nOPENAI_API_KEY=old\n")
        d = Drop("--env", str(self.env), "--var", "OPENAI_API_KEY:sk-", "--var", "OTHER_KEY")
        self.assertEqual(d.get(d.url.rsplit("/", 1)[0] + "/wrong"), 404)
        self.assertEqual(d.get(d.url), 200)
        status, _ = d.post(OPENAI_API_KEY="nope-" + "a" * 20)
        self.assertEqual(status, 400)
        status, _ = d.post()
        self.assertEqual(status, 400)
        status, body = d.post(OPENAI_API_KEY=TOKEN)
        self.assertEqual(status, 200)
        self.assertNotIn(TOKEN, body)
        code, events, err = d.finish()
        self.assertEqual(code, 0)
        self.assertEqual(events[-1], {"event": "saved", "vars": ["OPENAI_API_KEY"]})
        self.assertNotIn(TOKEN, json.dumps(events) + err)
        self.assertEqual(self.env.read_text(), f"KEEP=1\nOPENAI_API_KEY={TOKEN}\n")
        self.assertEqual(stat.S_IMODE(self.env.stat().st_mode), 0o600)
        self.assertEqual([p.name for p in Path(self.dir.name).iterdir()], [".env"])

    def test_text_values_are_quoted_or_refused(self):
        d = Drop("--env", str(self.env), "--var", "DB_PASSWORD", "--format", "text")
        status, _ = d.post(DB_PASSWORD="""a'b"c$d""")
        self.assertEqual(status, 400)
        status, _ = d.post(DB_PASSWORD="p@ss w#rd $x")
        self.assertEqual(status, 200)
        code, _, _ = d.finish()
        self.assertEqual(code, 0)
        self.assertEqual(self.env.read_text(), "DB_PASSWORD='p@ss w#rd $x'\n")

    def test_page_is_single_use(self):
        d = Drop("--env", str(self.env), "--var", "A_TOKEN")
        self.assertEqual(d.post(A_TOKEN=TOKEN)[0], 200)
        code, _, _ = d.finish()
        self.assertEqual(code, 0)
        with self.assertRaises(OSError):
            d.post(A_TOKEN=TOKEN)

    def test_timeout_saves_nothing(self):
        d = Drop("--env", str(self.env), "--var", "A_TOKEN", timeout=1)
        code, events, _ = d.finish()
        self.assertEqual(code, 2)
        self.assertEqual(events[-1], {"event": "timeout"})
        self.assertFalse(self.env.exists())

    def test_warns_when_env_is_not_gitignored(self):
        subprocess.run(["git", "init", "-q", self.dir.name], check=True)
        d = Drop("--env", str(self.env), "--var", "A_TOKEN", timeout=1)
        self.assertTrue(d.listening["warnings"])
        d.finish()


class CommandSinkTest(unittest.TestCase):
    def test_value_arrives_on_stdin_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Drop("--sink-cmd", f"sh -c 'cat > \"{tmp}/$KEYDROP_NAME.{{name}}\"'",
                     "--var", "A_TOKEN", "--var", "B_TOKEN")
            self.assertEqual(d.post(A_TOKEN=TOKEN, B_TOKEN=TOKEN + "b")[0], 200)
            code, events, _ = d.finish()
            self.assertEqual(code, 0)
            self.assertEqual(events[-1]["vars"], ["A_TOKEN", "B_TOKEN"])
            self.assertEqual((Path(tmp) / "A_TOKEN.A_TOKEN").read_text(), TOKEN)
            self.assertEqual((Path(tmp) / "B_TOKEN.B_TOKEN").read_text(), TOKEN + "b")

    def test_failing_command_reports_error(self):
        d = Drop("--sink-cmd", "false", "--var", "A_TOKEN")
        status, body = d.post(A_TOKEN=TOKEN)
        self.assertEqual(status, 500)
        code, events, _ = d.finish()
        self.assertEqual(code, 3)
        self.assertEqual(events[-1], {"event": "error", "failed": ["A_TOKEN"], "vars": []})


class StartupTest(unittest.TestCase):
    def run_keydrop(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-I", str(SCRIPT), *args], capture_output=True, text=True, timeout=30)

    def test_refuses_all_interfaces(self):
        r = self.run_keydrop("--env", "/tmp/x.env", "--var", "A", "--bind", "0.0.0.0")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(r.stdout, "")

    def test_rejects_missing_sink_program(self):
        r = self.run_keydrop("--sink-cmd", "no-such-program-keydrop", "--var", "A", "--bind", "localhost")
        self.assertEqual(r.returncode, 1)
        self.assertIn("not found", r.stderr)


@unittest.skipUnless(os.environ.get("KEYDROP_TEST_KEYCHAIN") == "1" and sys.platform == "darwin",
                     "set KEYDROP_TEST_KEYCHAIN=1 on macOS")
class KeychainSinkTest(unittest.TestCase):
    SERVICE = "keydrop-selftest"

    def tearDown(self):
        subprocess.run(["security", "delete-generic-password", "-s", self.SERVICE, "-a", "A_PASSWORD"],
                       capture_output=True)

    def test_roundtrip(self):
        # Non-ASCII matters: `security -w` would hand it back as hex.
        for value in ("p@ss w'rd \"x\" $y", "비밀번호 pw"):
            with self.subTest(value=value):
                d = Drop("--keychain", self.SERVICE, "--var", "A_PASSWORD", "--format", "text")
                self.assertEqual(d.post(A_PASSWORD=value)[0], 200)
                code, _, _ = d.finish()
                self.assertEqual(code, 0)
                r = run_exec("--keychain", self.SERVICE, "A_PASSWORD", "--", "sh", "-c", 'printf %s "$A_PASSWORD"')
                self.assertEqual((r.returncode, r.stdout), (0, value))


def run_exec(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-I", str(SCRIPT), "exec", *args],
                          capture_output=True, text=True, timeout=30)


class ExecTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.env = Path(self.dir.name) / ".env"

    def tearDown(self):
        self.dir.cleanup()

    def test_reads_common_env_forms(self):
        self.env.write_text(
            "A=old\n"
            "export B='p@ss w#rd $x'\n"
            'C="x y" # note\n'
            "D=plain # note\n"
            "A=new\n"
        )
        r = run_exec("--env", str(self.env), "A", "B", "C", "D", "--",
                     "sh", "-c", 'printf "%s|%s|%s|%s" "$A" "$B" "$C" "$D"')
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "new|p@ss w#rd $x|x y|plain", ""))

    def test_reads_back_what_the_page_wrote(self):
        values = {"A_PASSWORD": "p@ss w#rd $x", "B_PASSWORD": "it's fine", "C_PASSWORD": "비밀 번호"}
        d = Drop("--env", str(self.env), "--format", "text", *[f"--var={n}" for n in values])
        self.assertEqual(d.post(**values)[0], 200)
        self.assertEqual(d.finish()[0], 0)
        r = run_exec("--env", str(self.env), *values, "--",
                     "sh", "-c", 'printf "%s|%s|%s" "$A_PASSWORD" "$B_PASSWORD" "$C_PASSWORD"')
        self.assertEqual((r.returncode, r.stdout), (0, "|".join(values.values())))

    def test_passes_child_exit_code_through(self):
        self.env.write_text(f"A_TOKEN={TOKEN}\n")
        r = run_exec("--env", str(self.env), "A_TOKEN", "--", "sh", "-c", "exit 7")
        self.assertEqual(r.returncode, 7)

    def test_missing_name_runs_nothing(self):
        self.env.write_text(f"A_TOKEN={TOKEN}\nEMPTY=\n")
        marker = Path(self.dir.name) / "ran"
        r = run_exec("--env", str(self.env), "A_TOKEN", "B_TOKEN", "EMPTY", "--", "touch", str(marker))
        self.assertEqual(r.returncode, 125)
        self.assertIn("B_TOKEN, EMPTY", r.stderr)
        self.assertNotIn(TOKEN, r.stdout + r.stderr)
        self.assertFalse(marker.exists())

    def test_usage_errors(self):
        self.env.write_text(f"A_TOKEN={TOKEN}\n")
        cases = [
            (("--env", str(self.env), "A_TOKEN", "true"), 125),            # no `--`
            (("--env", str(self.env), "A_TOKEN", "--"), 125),              # no command
            (("A_TOKEN", "--", "true"), 125),                              # no source
            (("--env", str(self.env) + ".nope", "A_TOKEN", "--", "true"), 125),
            (("--env", str(self.env), "A_TOKEN", "--", "no-such-program-keydrop"), 127),
        ]
        for args, code in cases:
            with self.subTest(args=args):
                r = run_exec(*args)
                self.assertEqual(r.returncode, code, r.stderr)
                self.assertNotIn(TOKEN, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
