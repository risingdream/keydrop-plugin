"""End-to-end tests: start keydrop on localhost, submit the form over HTTP, check what the sink got.

Run: python3 -m unittest discover -s tests
The Keychain test writes and then deletes one item in the login keychain; it runs only when
KEYDROP_TEST_KEYCHAIN=1.
"""

from __future__ import annotations

import json
import os
import re
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

    def test_rejection_says_why_without_echoing_the_value(self):
        d = Drop("--env", str(self.env), "--var", "A_KEY:sk-", "--var", "B_TOKEN", "--var", "C_TOKEN", timeout=5)
        with urllib.request.urlopen(d.url, timeout=10) as r:
            self.assertIn("Starts with sk- · Letters, digits and _ - . · 16–400 characters", r.read().decode())
        bad = {"A_KEY": "pk-" + "a" * 20, "B_TOKEN": "short", "C_TOKEN": "has space " + "a" * 16}
        status, body = d.post(**bad)
        self.assertEqual(status, 400)
        self.assertIn("A_KEY: must start with sk-", body)
        self.assertIn("B_TOKEN: must be 16–400 characters (this one is 5)", body)
        self.assertIn("C_TOKEN: only letters, digits and _ - . are allowed", body)
        for value in bad.values():
            self.assertNotIn(value, body)
        self.assertEqual(d.finish()[0], 2)
        self.assertFalse(self.env.exists())

    def test_text_values_are_quoted_or_refused(self):
        d = Drop("--env", str(self.env), "--var", "DB_PASSWORD", "--format", "text")
        status, body = d.post(DB_PASSWORD="""a'b"c$d""")
        self.assertEqual(status, 400)
        self.assertIn("cannot be written to .env safely", body)
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


class VersionTest(unittest.TestCase):
    def test_every_manifest_has_the_same_version(self):
        root = SCRIPT.parents[4]
        plugin = root / "plugins" / "keydrop"
        load = lambda p: json.loads((root / p).read_text())
        found = {
            ".claude-plugin/marketplace.json metadata": load(".claude-plugin/marketplace.json")["metadata"]["version"],
            ".claude-plugin/marketplace.json plugin": load(".claude-plugin/marketplace.json")["plugins"][0]["version"],
            ".agents/plugins/marketplace.json": load(".agents/plugins/marketplace.json")["plugins"][0]["version"],
            ".claude-plugin/plugin.json": json.loads((plugin / ".claude-plugin" / "plugin.json").read_text())["version"],
            ".codex-plugin/plugin.json": json.loads((plugin / ".codex-plugin" / "plugin.json").read_text())["version"],
            "SKILL.md": re.search(r"^  version: (\S+)$", (SCRIPT.parent / "SKILL.md").read_text(), re.M).group(1),
            "keydrop.py": re.search(r'^__version__ = "([^"]+)"$', SCRIPT.read_text(), re.M).group(1),
        }
        self.assertEqual(len(set(found.values())), 1, found)


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


def run_keydrop(*args: str, cwd: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-I", str(SCRIPT), *args],
                          capture_output=True, text=True, timeout=30, cwd=cwd)


class Project:
    """A throwaway directory, optionally a git repository with an ignore file and committed files."""

    def __init__(self, git: bool = True, ignore: str = "", **files: str):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        if git:
            self.git("init", "-q")
            if ignore:
                (self.root / ".gitignore").write_text(ignore)

    def git(self, *args: str) -> None:
        subprocess.run(["git", "-C", str(self.root), "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
                       check=True, capture_output=True)

    def write(self, name: str, text: str, commit: bool = False) -> Path:
        path = self.root / name
        path.write_text(text)
        if commit:
            self.git("add", "-f", name)
            self.git("commit", "-q", "-m", name)
        return path

    def read(self, name: str) -> str:
        return (self.root / name).read_text()

    def close(self) -> None:
        self.tmp.cleanup()


class AutoEnvSinkTest(unittest.TestCase):
    def project(self, **kw) -> Project:
        p = Project(**kw)
        self.addCleanup(p.close)
        return p

    def save(self, p: Project, *names: str, **values: str) -> dict:
        d = Drop("--env-auto", "--dir", str(p.root), *[f"--var={n}" for n in names])
        self.assertEqual(d.post(**values)[0], 200)
        code, events, _ = d.finish()
        self.assertEqual(code, 0)
        return d.listening

    def test_new_name_goes_to_existing_ignored_env_local(self):
        p = self.project(ignore=".env*\n!.env.example\n")
        p.write(".env.example", "A_TOKEN=\n", commit=True)
        p.write(".env.local", "OTHER=1\n")
        listening = self.save(p, "A_TOKEN", A_TOKEN=TOKEN)
        self.assertEqual(listening["files"], {"A_TOKEN": [".env.local"]})
        self.assertEqual(p.read(".env.local"), f"OTHER=1\nA_TOKEN={TOKEN}\n")
        self.assertEqual(p.read(".env.example"), "A_TOKEN=\n")

    def test_tracked_env_is_left_alone_and_env_local_created(self):
        p = self.project(ignore=".env.local\n")
        p.write(".env", "A_TOKEN=\nPORT=3000\n", commit=True)
        listening = self.save(p, "A_TOKEN", A_TOKEN=TOKEN)
        self.assertEqual(listening["files"], {"A_TOKEN": [".env.local"]})
        self.assertEqual(p.read(".env"), "A_TOKEN=\nPORT=3000\n")
        self.assertEqual(p.read(".env.local"), f"A_TOKEN={TOKEN}\n")
        r = run_exec("--env-auto", "--dir", str(p.root), "A_TOKEN", "PORT", "--", "sh", "-c", 'printf "%s %s" "$A_TOKEN" "$PORT"')
        self.assertEqual((r.returncode, r.stdout), (0, f"{TOKEN} 3000"))

    def test_updates_every_ignored_file_that_defines_the_name(self):
        p = self.project(ignore=".env*\n")
        p.write(".env", "A_TOKEN=old\n")
        p.write(".env.development.local", "A_TOKEN=older\n")
        listening = self.save(p, "A_TOKEN", A_TOKEN=TOKEN)
        self.assertEqual(listening["files"], {"A_TOKEN": [".env", ".env.development.local"]})
        self.assertEqual(p.read(".env"), f"A_TOKEN={TOKEN}\n")
        self.assertEqual(p.read(".env.development.local"), f"A_TOKEN={TOKEN}\n")

    def test_new_ignored_env_when_nothing_exists(self):
        p = self.project(ignore=".env\n")
        self.assertEqual(self.save(p, "A_TOKEN", A_TOKEN=TOKEN)["files"], {"A_TOKEN": [".env"]})
        q = self.project(git=False)
        self.assertEqual(self.save(q, "A_TOKEN", A_TOKEN=TOKEN)["files"], {"A_TOKEN": [".env"]})

    def test_refuses_before_the_page_opens(self):
        loose = self.project()
        loose.write(".env", "A_TOKEN=old\n")
        nothing_ignored = self.project()
        for p, message in ((loose, "does not ignore"), (nothing_ignored, "No git-ignored .env file")):
            with self.subTest(message=message):
                r = run_keydrop("--env-auto", "--dir", str(p.root), "--var", "A_TOKEN", "--bind", "localhost")
                self.assertEqual((r.returncode, r.stdout), (1, ""))
                self.assertIn(message, r.stderr)

    def test_explicit_env_refuses_tracked_and_template_files(self):
        p = self.project(ignore=".env.local\n")
        p.write(".env", "PORT=3000\n", commit=True)
        for name, message in ((".env", "tracked by git"), (".env.example", "template")):
            with self.subTest(name=name):
                r = run_keydrop("--env", str(p.root / name), "--var", "A_TOKEN", "--bind", "localhost")
                self.assertEqual((r.returncode, r.stdout), (1, ""))
                self.assertIn(message, r.stderr)


class ExecSourcesTest(unittest.TestCase):
    def test_later_files_win_and_mode_files_follow_framework_order(self):
        p = Project(git=False)
        self.addCleanup(p.close)
        p.write(".env", "A=env\nB=env\nC=env\nD=env\nE=env\n")
        p.write(".env.development", "B=dev\nC=dev\nD=dev\n")
        p.write(".env.local", "C=local\nD=local\nE=\n")
        p.write(".env.development.local", "D=devlocal\n")
        show = ["--", "sh", "-c", 'printf "%s %s %s %s" "$A" "$B" "$C" "$D"']
        # The form the SessionStart note suggests: no --dir, run from the project directory.
        r = run_keydrop("exec", "--env-auto", "A", "B", *show[:3], 'printf "%s %s" "$A" "$B"', cwd=str(p.root))
        self.assertEqual((r.returncode, r.stdout), (0, "env env"), r.stderr)
        r = run_exec("--env-auto", "--dir", str(p.root), "--mode", "development", "A", "B", "C", "D", *show)
        self.assertEqual((r.returncode, r.stdout), (0, "env dev local devlocal"))
        r = run_exec("--env-auto", "--dir", str(p.root), "A", "B", "C", "D", *show)
        self.assertEqual((r.returncode, r.stdout), (0, "env env local local"))
        r = run_exec("--env", str(p.root / ".env.local"), "--env", str(p.root / ".env"), "C", "D", "--",
                     "sh", "-c", 'printf "%s %s" "$C" "$D"')
        self.assertEqual((r.returncode, r.stdout), (0, "env env"))
        # An empty later definition overrides, as frameworks do, so E counts as missing.
        r = run_exec("--env-auto", "--dir", str(p.root), "E", "--", "true")
        self.assertEqual(r.returncode, 125)
        r = run_exec("--env", str(p.root / ".env"), "--mode", "development", "A", "--", "true")
        self.assertEqual(r.returncode, 125)


class ContextTest(unittest.TestCase):
    def test_lists_names_from_ignored_files_only(self):
        p = Project(ignore=".env.local\n")
        self.addCleanup(p.close)
        p.write(".env", "PORT=3000\n", commit=True)
        r = run_keydrop("context", str(p.root))
        self.assertEqual((r.returncode, r.stdout), (0, ""))
        p.write(".env.local", f"A_TOKEN={TOKEN}\nEMPTY=\n")
        r = run_keydrop("context", str(p.root))
        self.assertEqual(r.returncode, 0)
        out = json.loads(r.stdout)["hookSpecificOutput"]
        self.assertEqual(out["hookEventName"], "SessionStart")
        self.assertIn(".env.local: A_TOKEN.", out["additionalContext"])
        self.assertIn(f'exec --env-auto --dir "{p.root.resolve()}" NAME', out["additionalContext"])
        for absent in (TOKEN, "PORT", "EMPTY"):
            self.assertNotIn(absent, out["additionalContext"])

    def test_uses_claude_project_dir_and_never_fails(self):
        p = Project(git=False)
        self.addCleanup(p.close)
        p.write(".env", f"A_TOKEN={TOKEN}\n")
        r = subprocess.run([sys.executable, "-I", str(SCRIPT), "context"], capture_output=True, text=True,
                           env={**os.environ, "CLAUDE_PROJECT_DIR": str(p.root)})
        self.assertIn("A_TOKEN", r.stdout)
        r = run_keydrop("context", str(p.root / "missing"))
        self.assertEqual((r.returncode, r.stdout), (0, ""))


if __name__ == "__main__":
    unittest.main()
