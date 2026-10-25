# keydrop

[한국어](README.ko.md)

Hand an API key to your coding agent without pasting it into the chat.

When an agent needs a secret, keydrop opens a one-time input page on your machine's
[Tailscale](https://tailscale.com) address. You open the link on your phone or laptop, paste the
key, and it goes straight to a `.env` file, the macOS Keychain, or any command that reads stdin.
The agent only learns which variable names were saved, so the value never appears in the
transcript, the logs or a command line.

It is an [Agent Skill](https://agentskills.io) packaged as a Claude Code and Codex plugin: one
`SKILL.md` and one Python script that uses only the standard library. `keydrop exec` then runs a
single command with the saved values in its environment, so the agent can use a key in the same
session without ever printing it.

## Install

| Client | How |
|---|---|
| Claude Code | `claude plugin marketplace add risingdream/keydrop-plugin`, then `claude plugin install keydrop@keydrop-plugin` |
| Claude Desktop (Code) | Settings → Plugins → Add → From repository → `risingdream/keydrop-plugin` |
| Codex | `codex plugin marketplace add risingdream/keydrop-plugin`, then `codex plugin add keydrop@keydrop-plugin` |
| OpenCode | Copy the skill folder (below) |
| Other agents | Copy `plugins/keydrop/skills/keydrop/` into the agent's skills folder |

OpenCode reads skills from `~/.config/opencode/skills/` (every project) or `.opencode/skills/`
(one project):

```bash
git clone https://github.com/risingdream/keydrop-plugin /tmp/keydrop-plugin
mkdir -p ~/.config/opencode/skills
cp -R /tmp/keydrop-plugin/plugins/keydrop/skills/keydrop ~/.config/opencode/skills/
opencode debug skill | grep -A2 '"name": "keydrop"'
```

Pull the repository and copy again to update. If another skill named `keydrop` is already
installed, keep only one: agents pick one by name and may not pick this one.

Requirements: `python3` 3.9 or newer. Tailscale for the default network mode. macOS for `--keychain`.

## Use

Ask the agent for something that needs a key ("set up the OpenAI key for this project"). It runs:

```bash
python3 -I keydrop.py --env-auto --title myapp --var OPENAI_API_KEY:sk-
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
| `--env-auto` | The project's `.env` files, chosen per variable (project directory: `--dir`, default current). A name already defined in a git-ignored `.env*` file is updated there, in every such file, so no stale copy shadows the new value. A new name goes to an existing ignored `.env.local` or `.env`, otherwise a new ignored `.env` (or `.env.local`). Tracked files and templates (`.env.example`, `.sample`, `.template`, `.tmpl`, `.dist`) are never written. If the only candidate is not git-ignored, keydrop stops before the page opens. The first output line lists the chosen files under `files` |
| `--env PATH` | `NAME=value` in that file. Refuses git-tracked files and templates. Other lines are kept, the file is replaced atomically with mode 600, and keydrop warns when the file is not git-ignored. Free-text values are quoted so dotenv parsers read them back unchanged; a value that cannot be quoted safely is refused |
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
| `--dir DIR` | `.` | Project directory for `--env-auto` |
| `--bind auto\|localhost\|IP` | `auto` | `auto` uses this machine's Tailscale IPv4. `0.0.0.0` and `::` are refused |
| `--timeout SECONDS` | `600` | The page closes after this long with nothing saved |
| `--port N` | random | Listening port |
| `--title TEXT` | `keydrop` | Heading on the page |
| `--lang auto\|en\|ko` | `auto` | Page language (`auto` follows `LANG`) |

Exit codes: `0` saved, `1` startup error (nothing opened), `2` timeout, `3` the destination refused a value.

### Using a saved secret: `keydrop exec`

Saving a secret cannot change the environment of an agent that is already running, and most agents
start a fresh shell for every command. `exec` reads the named values and runs one command with them
added to its environment:

```bash
python3 -I keydrop.py exec --env-auto A_TOKEN B_TOKEN -- \
  sh -c 'curl -sf -H "Authorization: Bearer $A_TOKEN" https://api.example.com/health'
python3 -I keydrop.py exec --keychain myapp OPENAI_API_KEY -- python3 app.py
```

- Nothing is printed. keydrop replaces itself with the command, so the exit code is the command's own.
- If any name is missing or empty it exits `125` and runs nothing (`127`: command not found,
  `126`: not executable). The error lists names only.
- `--env-auto` reads `.env` then `.env.local` in `--dir` (default current directory); with
  `--mode M` it reads `.env`, `.env.M`, `.env.local`, `.env.M.local`. Later files win, including an
  empty definition, the same order Next.js and Vite use. `--env PATH` can be repeated; later wins.
- `.env` parsing understands `KEY=value`, `export KEY=value`, single- and double-quoted values and
  trailing `# comments`; the last definition of a name wins. Escapes inside double quotes are not
  interpreted.
- `--keychain` reads the item `keydrop` wrote (service `SERVICE`, account `NAME`), including
  non-ASCII values that `security find-generic-password -w` would print as hex.
- Single-quote `$NAME` as above so the child shell expands it, not yours.

### Session start note (Claude Code)

The plugin ships a SessionStart hook that runs `keydrop.py context` in the project. When the
project's git-ignored `.env*` files define secrets, it adds a short note to the session listing
their **names** and the exact `exec --env-auto --dir <project>` command to use. Values never
enter the prompt. Nothing is added when there are none. Keychain items are not listed.

Codex plugins cannot ship hooks (`plugin_hooks` is marked removed in Codex 0.159), and OpenCode
has no equivalent here, so on those agents the skill alone guides the agent.

## Security model

What keydrop protects against: a secret ending up in the agent's context, chat history, provider
logs, shell history or process list.

How:

- Listens only on the Tailscale address (or loopback). Tailscale encrypts the traffic between your
  devices with WireGuard, so plain HTTP inside the tailnet is not readable on the wire.
- The path contains 144 bits of randomness; every other path is a 404. The page closes after one
  successful save or on timeout, and a second submission is refused.
- `.env` destinations refuse files that git tracks and committed templates, so a secret is not
  committed by accident.
- The value goes to the destination on stdin or straight into a mode-600 file. stdout carries only
  variable names.
- Responses send `Cache-Control: no-store`, `Referrer-Policy: no-referrer`, a strict
  `Content-Security-Policy` and `X-Frame-Options: DENY`; fields are `type=password` with
  autocomplete off.

What it does not protect against:

- Anyone on your tailnet who gets the URL before you use it. Do not paste the URL anywhere public.
- An agent that reads the destination afterwards (`cat .env`). The skill tells the agent not to;
  keydrop cannot enforce it.
- Another project's agent reading this project's secrets. Every agent runs as your OS user and can
  read any `.env` file or Keychain item that user can. "Per project" in keydrop means where values
  are stored and which names are suggested, not an access boundary; use an agent sandbox or a
  secrets manager with per-project access (1Password, Doppler, Infisical) when you need one.
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

`.claude-plugin/` and `.agents/plugins/` hold the Claude Code and Codex marketplaces; both point at
`plugins/keydrop/`, whose `.claude-plugin/plugin.json` and `.codex-plugin/plugin.json` share one
`skills/` folder. `plugins/keydrop/hooks/hooks.json` is the Claude Code SessionStart hook. Keep the version the same in all four manifests, `SKILL.md` and `__version__` in `keydrop.py`.

## License

[MIT](LICENSE)
