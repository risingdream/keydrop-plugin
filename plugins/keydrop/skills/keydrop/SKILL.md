---
name: keydrop
description: Receive an API key, token or password from the user without it ever appearing in the conversation. Opens a one-time input page on the machine's Tailscale address (or localhost) that the user fills in from a phone or another device; the value goes straight to a .env file, the macOS Keychain, or a command that reads stdin (gh secret set, secret-tool, …). Use whenever the task needs a secret — "add my API key", "set the token", "put the key in .env", "register a secret", 키 등록, API 키 넣어줘, 시크릿 입력, 토큰 전달 — and especially when the user cannot type into a terminal (remote or mobile session). Never ask the user to paste a secret into the chat.
license: MIT
compatibility: Requires python3 (3.9+, standard library only). The default network mode needs an active Tailscale connection; --bind localhost works without it when the browser runs on the same machine. --keychain needs macOS. Works with Claude Code, Codex, Cursor and other agents that run shell commands.
metadata:
  version: 0.1.0
---

# keydrop

Never ask for a secret in the chat. Start keydrop, give the user its one-time URL, and wait for it to report which variable names were saved. You never see the value.

The script is `keydrop.py` in this skill's folder. In Claude Code it is `${CLAUDE_PLUGIN_ROOT}/skills/keydrop/keydrop.py`; other agents use the path of the folder this SKILL.md was loaded from.

## Pick a destination

| The app reads its secret from | Flag | Notes |
|---|---|---|
| a `.env` file | `--env <project>/.env` | Mode 600, other lines kept. keydrop warns if the file is not git-ignored |
| the macOS Keychain | `--keychain <service>` | One generic password per variable: service `<service>`, account `NAME` |
| anything else | `--sink-cmd "<command>"` | Runs once per variable with the value on stdin. `{name}` in the command becomes the variable name; `KEYDROP_NAME` is also set |

`--sink-cmd` examples (each reads the value from stdin):

- GitHub Actions secret: `--sink-cmd "gh secret set {name} --repo owner/repo"`
- Linux Secret Service: `--sink-cmd "secret-tool store --label={name} service myapp account {name}"`

Only use a command that reads the value from stdin. Never build a command that puts the value in an argument.

## Procedure

1. Choose variable names and, when you know them, the expected prefixes. A prefix rejects a mistyped value early; separate alternatives with `|`.
   - `OPENAI_API_KEY:sk-`, `ANTHROPIC_API_KEY:sk-ant-`, `OPENROUTER_API_KEY:sk-or-`, `GITHUB_TOKEN:ghp_|github_pat_`
   - Default `--format token` accepts 16–400 characters of `A-Z a-z 0-9 _ - .`. For passwords and other free text, use `--format text` (1–2048 characters, no control characters).
2. Run it in the background (Bash `run_in_background: true`) with `python3 -I`:

   ```bash
   python3 -I "${CLAUDE_PLUGIN_ROOT}/skills/keydrop/keydrop.py" --env ./.env \
     --title "<project name>" --var OPENAI_API_KEY:sk-
   ```

   Several `--var` flags put several fields on one page; the user fills only the ones they need.
   Use `--bind localhost` when the user's browser is on this same machine and Tailscale is not available.
3. The first stdout line is `{"event":"listening","url":...}`. Give the user the URL and tell them to open it on a device connected to the same tailnet. If `warnings` is not empty, tell the user (for example, fix `.gitignore` before saving). The URL is single-use; do not store it anywhere.
4. Wait for the process to finish:
   - `{"event":"saved","vars":[...]}`, exit 0 — saved.
   - `{"event":"timeout"}`, exit 2 — nothing saved (default 10 minutes, `--timeout` to change). Start a new one if still needed.
   - `{"event":"error","failed":[...],"vars":[...]}`, exit 3 — the destination refused the listed names; the ones in `vars` were saved. Offer the fallback below.
5. Confirm by name only, never by value:
   - `.env`: `grep -cE '^NAME=.+' .env`
   - Keychain: `security find-generic-password -s <service> -a NAME >/dev/null && echo present`
6. To use a Keychain secret in a command without printing it, expand it inside the child shell:

   ```bash
   sh -c 'OPENAI_API_KEY="$(security find-generic-password -s myapp -a OPENAI_API_KEY -w)" python3 app.py'
   ```

## Startup failures

keydrop checks everything before the page opens, so a value the user types is never lost. On failure it prints a reason to stderr and exits 1.

| Message | Cause | What to do |
|---|---|---|
| `No Tailscale IPv4 address found` | This machine is not on Tailscale | Ask the user to connect Tailscale, or use `--bind localhost` if they are at this machine |
| `--sink-cmd program not found` | The command is not installed | Install it or pick another destination |
| `--keychain needs macOS` | Not on macOS | Use `--env` or `--sink-cmd` (for example `secret-tool`) |
| `Cannot write` / `Directory does not exist` | Bad `--env` path | Fix the path |

Fallback when keydrop cannot be used: ask the user to run the destination's own hidden-input command themselves (for example `gh secret set NAME`, or `read -rs V && printf 'NAME=%s\n' "$V" >> .env` in their own terminal). Do not suggest pasting the value into the chat.

## Never

- Print, log, commit or pass a secret as a command argument. Do not `cat` the `.env`, `echo "$NAME"`, run `env`, or use `security ... -w` outside a child-shell expansion.
- Open the page to every interface (`--bind 0.0.0.0` is refused on purpose).
- Reuse a URL. Start a new keydrop instead.
- Kill other processes by pattern (`pkill -f`). If one is left running, stop only the PID you started.
