#!/usr/bin/env python3
"""keydrop — a one-time page for handing a secret to an agent without pasting it into the chat.

The page is served only on this machine's Tailscale address (or localhost), behind a random
one-time path, and shuts down after one successful save or when the timeout expires. Values
never reach stdout, logs or command-line arguments; output carries variable names only.

Sinks (pick one):
  --env PATH            write NAME=value into a .env file (mode 600, atomic replace)
  --keychain SERVICE    macOS Keychain generic password, service SERVICE, account NAME
  --sink-cmd "CMD"      run CMD once per variable with the value on stdin; {name} is replaced
                        with the variable name and KEYDROP_NAME is set in its environment

Usage:
  python3 -I keydrop.py --env ./.env --var OPENAI_API_KEY:sk-
  python3 -I keydrop.py --keychain myapp --var "OPENCODE_API_KEY:sk-|oc_sk_" --format token
  python3 -I keydrop.py --sink-cmd "gh secret set {name}" --var DEPLOY_TOKEN --bind localhost

stdout (one JSON object per line):
  {"event":"listening","url":...,"expires_in":600,...}
  {"event":"saved","vars":[...]}               exit 0
  {"event":"timeout"}                          exit 2
  {"event":"error","failed":[...],"vars":[...]} exit 3 (the sink rejected one or more values)
Startup problems print a message to stderr and exit 1 before the page opens, so a value the
user typed is never lost to a misconfigured sink.

Using a saved secret (one command only, nothing printed):
  python3 -I keydrop.py exec (--env PATH | --keychain SERVICE) NAME [NAME ...] -- CMD [ARG ...]
The named values are added to CMD's environment and keydrop is replaced by CMD, so the exit
code is CMD's own. keydrop's own failures exit 125 (missing name, unreadable source),
126 (CMD not executable) or 127 (CMD not found) without running anything.
"""

from __future__ import annotations

import argparse
import html
import ipaddress
import json
import os
import re
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

__version__ = "0.2.0"

TOKEN_RE = re.compile(r"[A-Za-z0-9_\-.]{16,400}")
# No control characters: a newline would break .env files and shell injection of the value.
TEXT_RE = re.compile(r"[^\x00-\x1f\x7f]{1,2048}")
TAILNET = ipaddress.ip_network("100.64.0.0/10")
MAX_BODY = 16384

MESSAGES = {
    "en": {
        "suffix": "Secret drop",
        "intro": "Values are saved to {dest}. Fill only the fields you need. This page closes after saving.",
        "bad": "Invalid format: {names}",
        "empty": "Fill at least one field.",
        "failed": "Could not save: {names}. Ask the agent for another way to deliver it.",
        "saved": "Saved: {names}. You can close this window.",
        "gone": "This page has already been used.",
        "button": "Save",
        "or": " or ",
        "value": "value",
    },
    "ko": {
        "suffix": "키 입력",
        "intro": "{dest} 에 저장됩니다. 필요한 칸만 채우세요. 저장하면 이 페이지는 닫힙니다.",
        "bad": "형식이 맞지 않습니다: {names}",
        "empty": "최소 한 칸은 채워야 합니다.",
        "failed": "저장하지 못했습니다: {names}. 에이전트에게 다른 전달 방법을 물어보세요.",
        "saved": "저장했습니다: {names}. 이 창을 닫으셔도 됩니다.",
        "gone": "이미 사용된 페이지입니다.",
        "button": "저장",
        "or": " 또는 ",
        "value": "값",
    },
}


def emit(**data) -> None:
    print(json.dumps(data, ensure_ascii=False), flush=True)


def run(argv: list[str], timeout: int, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, **kw)


def tailscale_ip() -> str:
    # Interface listings are fast; the tailscale CLI can take seconds, so it is the last resort.
    for argv in (["ifconfig"], ["ip", "-4", "-o", "addr", "show"]):
        try:
            out = run(argv, 5).stdout
        except (OSError, subprocess.TimeoutExpired):
            continue
        for ip in re.findall(r"\binet (\d+\.\d+\.\d+\.\d+)", out):
            if ipaddress.ip_address(ip) in TAILNET:
                return ip
    cli = shutil.which("tailscale") or "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
    try:
        out = run([cli, "ip", "-4"], 30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        sys.exit(f"No Tailscale IPv4 address found ({exc}). Connect Tailscale, or use --bind localhost "
                 "when the browser runs on this machine.")
    ip = out.stdout.strip().splitlines()[0] if out.stdout.strip() else ""
    if not ip or ipaddress.ip_address(ip) not in TAILNET:
        detail = out.stderr.strip() or out.stdout.strip() or "empty output"
        sys.exit(f"No Tailscale IPv4 address found ({detail}). Connect Tailscale, or use --bind localhost "
                 "when the browser runs on this machine.")
    return ip


def resolve_bind(value: str) -> str:
    if value == "auto":
        return tailscale_ip()
    if value == "localhost":
        return "127.0.0.1"
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        sys.exit(f"--bind takes auto, localhost or an IP address, not {value!r}")
    if ip.is_unspecified:
        sys.exit("Refusing to listen on every interface. Use auto (Tailscale) or localhost.")
    return str(ip)


def env_literal(value: str) -> str | None:
    """Quote a value so common .env parsers read it back unchanged, or None if no safe form exists."""
    if re.fullmatch(r"[A-Za-z0-9_\-.:/+=@,%]+", value):
        return value
    if "'" not in value:
        return f"'{value}'"
    if not re.search(r'["\\$`]', value):
        return f'"{value}"'
    return None


class EnvSink:
    def __init__(self, path: str):
        self.path = Path(path).expanduser().resolve()
        self.label = self.path.name
        if not self.path.parent.is_dir():
            sys.exit(f"Directory does not exist: {self.path.parent}")
        if not os.access(self.path.parent, os.W_OK) or (self.path.exists() and not os.access(self.path, os.W_OK)):
            sys.exit(f"Cannot write {self.path}")

    def warnings(self) -> list[str]:
        git = shutil.which("git")
        if not git:
            return []
        try:
            out = run([git, "-C", str(self.path.parent), "check-ignore", "-q", str(self.path)], 10)
        except (OSError, subprocess.TimeoutExpired):
            return []
        # 0: ignored, 1: inside a repository and not ignored, 128: not a repository.
        return [f"{self.path} is not git-ignored; add it to .gitignore"] if out.returncode == 1 else []

    def rejects(self, value: str) -> bool:
        return env_literal(value) is None

    def store(self, values: dict[str, str]) -> list[str]:
        lines = self.path.read_text().splitlines() if self.path.exists() else []
        out, done = [], set()
        for line in lines:
            m = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=", line)
            if m and m.group(1) in values:
                name = m.group(1)
                if name not in done:
                    out.append(f"{name}={env_literal(values[name])}")
                    done.add(name)
                continue
            out.append(line)
        out += [f"{k}={env_literal(v)}" for k, v in values.items() if k not in done]
        tmp = self.path.with_name(f".{self.path.name}.keydrop-{secrets.token_hex(4)}")
        try:
            # Create with mode 600 from the start so the value is never readable by others, then swap in.
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write("\n".join(out) + "\n")
            os.replace(tmp, self.path)
            os.chmod(self.path, 0o600)
        except OSError:
            tmp.unlink(missing_ok=True)
            return list(values)
        return []


def keychain_cli(service: str) -> str:
    if sys.platform != "darwin":
        sys.exit("--keychain needs macOS. On Linux use --sink-cmd with secret-tool (see README).")
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", service):
        sys.exit(f"Invalid keychain service name: {service!r} (letters, digits, . _ - only)")
    cli = shutil.which("security") or "/usr/bin/security"
    if not os.access(cli, os.X_OK):
        sys.exit("macOS `security` command not found")
    return cli


class KeychainSink:
    def __init__(self, service: str):
        self.cli = keychain_cli(service)
        self.service = service
        self.label = f"macOS Keychain ({service})"

    def warnings(self) -> list[str]:
        return []

    def rejects(self, value: str) -> bool:
        return False

    def store(self, values: dict[str, str]) -> list[str]:
        # Commands go to `security -i` on stdin and the value is hex-encoded (-X), so it never appears
        # in a process argument list and needs no quoting.
        failed = []
        for name, value in values.items():
            script = f"add-generic-password -U -s {self.service} -a {name} -X {value.encode().hex()}\n"
            try:
                run([self.cli, "-i"], 30, input=script)
                check = run([self.cli, "find-generic-password", "-s", self.service, "-a", name], 30)
                if check.returncode != 0:
                    failed.append(name)
            except (OSError, subprocess.TimeoutExpired):
                failed.append(name)
        return failed


class CommandSink:
    def __init__(self, template: str):
        self.argv = shlex.split(template)
        if not self.argv:
            sys.exit("--sink-cmd is empty")
        exe = shutil.which(self.argv[0])
        if not exe:
            sys.exit(f"--sink-cmd program not found: {self.argv[0]}")
        self.argv[0] = exe
        self.label = f"`{Path(exe).name}`"

    def warnings(self) -> list[str]:
        return []

    def rejects(self, value: str) -> bool:
        return False

    def store(self, values: dict[str, str]) -> list[str]:
        # The value goes in on stdin only. The command's own output is discarded: it may echo the value.
        failed = []
        for name, value in values.items():
            argv = [a.replace("{name}", name) for a in self.argv]
            try:
                out = run(argv, 60, input=value, env={**os.environ, "KEYDROP_NAME": name})
                if out.returncode != 0:
                    failed.append(name)
            except (OSError, subprocess.TimeoutExpired):
                failed.append(name)
        return failed


def parse_var(spec: str) -> tuple[str, list[str]]:
    name, _, prefixes = spec.partition(":")
    if not re.fullmatch(r"[A-Z][A-Z0-9_]*", name):
        sys.exit(f"Invalid variable name: {name!r} (UPPER_SNAKE_CASE)")
    return name, [p for p in prefixes.split("|") if p]


def read_env_file(path: Path) -> dict[str, str]:
    # The inverse of env_literal, plus the unquoted `KEY=value # comment` form other tools write.
    # Later definitions win, as when a shell sources the file.
    values = {}
    for line in path.read_text().splitlines():
        m = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line)
        if not m:
            continue
        raw = m.group(2).strip()
        quoted = re.fullmatch(r"""(['"])(.*)\1(?:\s+#.*)?""", raw)
        values[m.group(1)] = quoted.group(2) if quoted else re.split(r"\s+#", raw, maxsplit=1)[0]
    return values


def read_keychain(cli: str, service: str, name: str) -> str | None:
    # `-w` prints non-ASCII passwords as bare hex, indistinguishable from a hex token, so read the
    # `-g` line instead: `password: "value"` or `password: 0xHEX  "escaped"`. It goes to stderr.
    try:
        out = run([cli, "find-generic-password", "-s", service, "-a", name, "-g"], 30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    for line in out.stderr.splitlines():
        if line.startswith("password: 0x"):
            return bytes.fromhex(line[len("password: 0x"):].split()[0]).decode(errors="replace")
        if line.startswith('password: "') and line.endswith('"'):
            return line[len('password: "'):-1]
    return None


class ExecParser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        exec_fail(message)


def exec_fail(message: str, code: int = 125):
    print(f"keydrop exec: {message}", file=sys.stderr)
    sys.exit(code)


def exec_main(argv: list[str]) -> None:
    split = argv.index("--") if "--" in argv else len(argv)
    command = argv[split + 1:]
    ap = ExecParser(prog="keydrop.py exec",
                    description="Run one command with saved secrets added to its environment.")
    source = ap.add_mutually_exclusive_group(required=True)
    source.add_argument("--env", metavar="PATH", help=".env file to read")
    source.add_argument("--keychain", metavar="SERVICE", help="macOS Keychain service name")
    ap.add_argument("names", nargs="+", metavar="NAME")
    args = ap.parse_args(argv[:split])
    if split == len(argv):
        exec_fail("missing `--` before the command")
    if not command:
        exec_fail("no command after `--`")
    for name in args.names:
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", name):
            exec_fail(f"invalid variable name: {name!r}")

    try:
        if args.env:
            path = Path(args.env).expanduser()
            try:
                found = read_env_file(path)
            except OSError as exc:
                exec_fail(f"cannot read {path}: {exc.strerror}")
            values = {n: found[n] for n in args.names if found.get(n)}
            label = str(path)
        else:
            cli = keychain_cli(args.keychain)
            values = {}
            for n in args.names:
                v = read_keychain(cli, args.keychain, n)
                if v:
                    values[n] = v
            label = f"Keychain service {args.keychain}"
    except SystemExit as exc:
        if isinstance(exc.code, str):
            exec_fail(exc.code)
        raise
    missing = [n for n in args.names if n not in values]
    if missing:
        exec_fail(f"not found in {label}: {', '.join(missing)}")
    try:
        os.execvpe(command[0], command, {**os.environ, **values})
    except FileNotFoundError:
        exec_fail(f"command not found: {command[0]}", 127)
    except PermissionError:
        exec_fail(f"command not executable: {command[0]}", 126)


PAGE ="""<!doctype html><html lang="{lang}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} · {suffix}</title>
<style>
:root{{color-scheme:light dark;--fg:#111;--muted:#666;--line:#bbb;--bg:#fff;--accent:#2563eb}}
@media (prefers-color-scheme:dark){{:root{{--fg:#eee;--muted:#999;--line:#555;--bg:#111}}}}
body{{font:16px -apple-system,system-ui,sans-serif;color:var(--fg);background:var(--bg);margin:0 auto;padding:24px 16px;max-width:520px}}
label{{display:block;margin:18px 0 6px;font:600 14px ui-monospace,monospace}}
input{{width:100%;box-sizing:border-box;font:16px ui-monospace,monospace;padding:12px;border:1px solid var(--line);border-radius:8px;background:transparent;color:inherit}}
button{{margin-top:20px;width:100%;padding:13px;font-size:16px;border:0;border-radius:8px;background:var(--accent);color:#fff}}
p{{color:var(--muted)}}.err{{color:#dc2626}}</style></head><body>
<h2>{title}</h2><p class="{cls}">{msg}</p>{form}</body></html>"""


def main() -> None:
    if sys.argv[1:2] == ["exec"]:
        return exec_main(sys.argv[2:])
    ap = argparse.ArgumentParser(description="One-time page for handing a secret to an agent.",
                                 epilog="To run a command with saved secrets: keydrop.py exec --help")
    sink_group = ap.add_mutually_exclusive_group(required=True)
    sink_group.add_argument("--env", metavar="PATH", help=".env file to write")
    sink_group.add_argument("--keychain", metavar="SERVICE", help="macOS Keychain service name")
    sink_group.add_argument("--sink-cmd", metavar="CMD", help="command that reads the value on stdin; {name} is replaced")
    ap.add_argument("--var", action="append", required=True, metavar="NAME[:prefix1|prefix2]")
    ap.add_argument("--format", choices=("token", "text"), default="token",
                    help="token: 16-400 chars of [A-Za-z0-9_-.]; text: 1-2048 chars without control characters")
    ap.add_argument("--title", default="keydrop")
    ap.add_argument("--timeout", type=int, default=600, help="seconds before the page closes")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--bind", default="auto", help="auto (Tailscale IPv4), localhost, or an IP address")
    ap.add_argument("--lang", choices=("auto", "en", "ko"), default="auto")
    ap.add_argument("--version", action="version", version=f"keydrop {__version__}")
    args = ap.parse_args()

    specs = dict(parse_var(v) for v in args.var)
    value_re = TOKEN_RE if args.format == "token" else TEXT_RE
    lang = args.lang
    if lang == "auto":
        lang = "ko" if os.environ.get("LANG", "").lower().startswith("ko") else "en"
    t = MESSAGES[lang]
    if args.env:
        sink = EnvSink(args.env)
    elif args.keychain:
        sink = KeychainSink(args.keychain)
    else:
        sink = CommandSink(args.sink_cmd)
    bind = resolve_bind(args.bind)
    route = f"/{secrets.token_urlsafe(18)}"
    state = {"code": None}
    lock = threading.Lock()

    def form_html() -> str:
        fields = "".join(
            f'<label for="{n}">{n}</label>'
            f'<input id="{n}" name="{n}" type="password" autocomplete="off" spellcheck="false" '
            f'placeholder="{html.escape(t["or"].join(p) + "…" if p else t["value"])}">'
            for n, p in specs.items()
        )
        return (f'<form method="post" action="{route}" autocomplete="off">{fields}'
                f'<button type="submit">{t["button"]}</button></form>')

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _page(self, code: int, msg: str, form: bool, err: bool = False) -> None:
            body = PAGE.format(
                lang=lang, suffix=t["suffix"], title=html.escape(args.title), msg=html.escape(msg),
                cls="err" if err else "", form=form_html() if form else "",
            ).encode()
            self.send_response(code)
            for k, v in (("Content-Type", "text/html; charset=utf-8"), ("Cache-Control", "no-store"),
                         ("Referrer-Policy", "no-referrer"), ("X-Frame-Options", "DENY"),
                         ("X-Content-Type-Options", "nosniff"),
                         ("Content-Security-Policy",
                          "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'"),
                         ("Content-Length", str(len(body)))):
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path != route:
                return self.send_error(404)
            if state["code"] is not None:
                return self._page(410, t["gone"], False, err=True)
            self._page(200, t["intro"].format(dest=sink.label), True)

        def do_POST(self):
            if self.path != route:
                return self.send_error(404)
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                return self.send_error(413)
            form = parse_qs(self.rfile.read(length).decode(errors="replace"))
            values, errors = {}, []
            for name, prefixes in specs.items():
                v = (form.get(name) or [""])[0].strip()
                if not v:
                    continue
                if ((prefixes and not any(v.startswith(p) for p in prefixes))
                        or not value_re.fullmatch(v) or sink.rejects(v)):
                    errors.append(name)
                else:
                    values[name] = v
            if errors:
                return self._page(400, t["bad"].format(names=", ".join(errors)), True, err=True)
            if not values:
                return self._page(400, t["empty"], True, err=True)
            # One save per page: a second submission racing the shutdown must not write again.
            with lock:
                if state["code"] is not None:
                    return self._page(410, t["gone"], False, err=True)
                failed = sink.store(values)
                saved = [n for n in values if n not in failed]
                state["code"] = 3 if failed else 0
            if failed:
                self._page(500, t["failed"].format(names=", ".join(failed)), False, err=True)
                emit(event="error", failed=failed, vars=saved)
            else:
                self._page(200, t["saved"].format(names=", ".join(saved)), False)
                emit(event="saved", vars=saved)
            threading.Thread(target=server.shutdown, daemon=True).start()

    class Server(ThreadingHTTPServer):
        address_family = socket.AF_INET6 if ":" in bind else socket.AF_INET

        def handle_error(self, request, client_address):
            if not isinstance(sys.exc_info()[1], (ConnectionError, socket.timeout)):
                super().handle_error(request, client_address)

    server = Server((bind, args.port), Handler)
    timer = threading.Timer(args.timeout, server.shutdown)
    timer.daemon = True
    timer.start()
    host = f"[{bind}]" if ":" in bind else bind
    emit(event="listening", url=f"http://{host}:{server.server_address[1]}{route}",
         expires_in=args.timeout, destination=sink.label, vars=list(specs), warnings=sink.warnings())
    server.serve_forever()
    if state["code"] is None:
        emit(event="timeout")
        sys.exit(2)
    sys.exit(state["code"])


if __name__ == "__main__":
    main()
