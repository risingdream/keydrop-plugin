# keydrop

[한국어](README.ko.md)

Hand an API key to your coding agent without pasting it into the chat.

When an agent needs a secret, keydrop opens a one-time input page on your machine's
[Tailscale](https://tailscale.com) address. You open the link on your phone or laptop, paste the
key, and it goes straight to a `.env` file, the macOS Keychain, or any command that reads stdin.
The agent only learns which variable names were saved, so the value never appears in the
transcript, the logs or a command line.

It is an [Agent Skill](https://agentskills.io) packaged as a Claude Code plugin: one `SKILL.md`
and one Python script that uses only the standard library.

## Install

| Client | How |
|---|---|
| Claude Code | `claude plugin marketplace add risingdream/keydrop-plugin`, then `claude plugin install keydrop@keydrop-plugin` |
| Claude Desktop (Code) | Settings → Plugins → Add → From repository → `risingdream/keydrop-plugin` |
| Codex, Cursor, other agents | Copy `plugins/keydrop/skills/keydrop/` into the agent's skills folder (for example `~/.codex/skills/keydrop/`) |

Requirements: `python3` 3.9 or newer. Tailscale for the default network mode. macOS for `--keychain`.

## Use

Ask the agent for something that needs a key ("set up the OpenAI key for this project"). It runs:

```bash
python3 -I keydrop.py --env ./.env --title myapp --var OPENAI_API_KEY:sk-
```

and gives you a link like `http://100.x.y.z:54321/<random>`. Open it on a device on your tailnet,
paste the key, press Save. The page closes and the agent sees:

```json
{"event": "saved", "vars": ["OPENAI_API_KEY"]}
```

You can also run the script yourself; `python3 keydrop.py --help` lists every option.

### Destinations

| Flag | Where the value goes |
|---|---|
| `--env PATH` | `NAME=value` in a `.env` file. Other lines are kept, the file is replaced atomically with mode 600, and keydrop warns when the file is not git-ignored. Free-text values are quoted so dotenv parsers read them back unchanged; a value that cannot be quoted safely is refused |
| `--keychain SERVICE` | macOS Keychain generic password (service `SERVICE`, account `NAME`). The value is passed to `security` on stdin, hex-encoded, never as an argument |
| `--sink-cmd "CMD"` | Runs `CMD` once per variable with the value on stdin. `{name}` is replaced with the variable name and `KEYDROP_NAME` is set. Output is discarded |

Commands that work with `--sink-cmd`:

```bash
--sink-cmd "gh secret set {name} --repo owner/repo"                          # GitHub Actions secret
--sink-cmd "secret-tool store --label={name} service myapp account {name}"   # Linux Secret Service
```

### Options

| Option | Default | Meaning |
|---|---|---|
| `--var NAME[:p1\|p2]` | required, repeatable | A field on the page, with optional accepted prefixes |
| `--format token\|text` | `token` | `token`: 16–400 chars of `A-Z a-z 0-9 _ - .`; `text`: 1–2048 chars, no control characters |
| `--bind auto\|localhost\|IP` | `auto` | `auto` uses this machine's Tailscale IPv4. `0.0.0.0` and `::` are refused |
| `--timeout SECONDS` | `600` | The page closes after this long with nothing saved |
| `--port N` | random | Listening port |
| `--title TEXT` | `keydrop` | Heading on the page |
| `--lang auto\|en\|ko` | `auto` | Page language (`auto` follows `LANG`) |

Exit codes: `0` saved, `1` startup error (nothing opened), `2` timeout, `3` the destination refused a value.

## Security model

What keydrop protects against: a secret ending up in the agent's context, chat history, provider
logs, shell history or process list.

How:

- Listens only on the Tailscale address (or loopback). Tailscale encrypts the traffic between your
  devices with WireGuard, so plain HTTP inside the tailnet is not readable on the wire.
- The path contains 144 bits of randomness; every other path is a 404. The page closes after one
  successful save or on timeout, and a second submission is refused.
- The value goes to the destination on stdin or straight into a mode-600 file. stdout carries only
  variable names.
- Responses send `Cache-Control: no-store`, `Referrer-Policy: no-referrer`, a strict
  `Content-Security-Policy` and `X-Frame-Options: DENY`; fields are `type=password` with
  autocomplete off.

What it does not protect against:

- Anyone on your tailnet who gets the URL before you use it. Do not paste the URL anywhere public.
- An agent that reads the destination afterwards (`cat .env`). The skill tells the agent not to;
  keydrop cannot enforce it.
- A compromised machine.

The `100.64.0.0/10` range Tailscale uses is shared with carrier-grade NAT. If another interface on
the machine has an address in it, `--bind auto` may pick that one; pass the Tailscale IP explicitly
in that case.

## Development

```bash
python3 -m unittest discover -s tests
KEYDROP_TEST_KEYCHAIN=1 python3 -m unittest discover -s tests   # also writes and deletes one Keychain item
claude plugin validate .
```

## License

[MIT](LICENSE)
