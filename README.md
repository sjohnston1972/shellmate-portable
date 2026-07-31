# ShellMate Portable

A split-screen, multi-tab network terminal with a built-in agentic AI copilot. Built for network engineers working with Cisco switches, routers, firewalls and similar devices.

Ships as a **single portable executable** — no Python, no installer, no administrator rights, and no internet access required. Runs from a USB stick on a locked-down corporate machine and carries its settings with it.

> A fork of [ShellMate](https://github.com/sjohnston1972/shellmate), reshaped around portability and a much wider feature set.

![ShellMate welcome screen](docs/screenshot-welcome.png)

## What it does

- **SSH, serial console and telnet** — one tabbed terminal for all three. SSH supports password or key authentication (Ed25519/ECDSA/RSA/DSA, encrypted keys included) and jump-host chaining through a bastion. Serial enumerates the COM ports actually present on the machine and sends break for ROMMON access. Telnet negotiates options properly and can answer login prompts for you
- **SFTP file browser** — pull a config off a device or push an image to it over the SSH connection the tab already has open. No second login, no separate tool
- **Encrypted credentials vault** — API keys and remembered device passwords are encrypted on disk, never in plain text. By default they are tied to your Windows account, so a lost USB stick is useless to anyone else; switch to a master password if you need the vault to work on any machine
- **Knows what it is talking to** — identifies IOS/IOS-XE, NX-OS, ASA, Junos, PAN-OS, Arista EOS and Linux from the login banner, then adapts: turns paging off with the right command so you stop typing `terminal length 0`, picks the right config command, and shows the platform and version in the status bar. Nothing is sent to a device it cannot confidently identify
- **Cross-vendor aliases** — type `ints` and get `show ip interface brief` on IOS, `show interfaces terse` on Junos, `show interface all` on PAN-OS. The terminal shows what was actually sent
- **Configurable output colours** — `down` in red, `up` in green, `error` in orange, driven by your own regex rules with a live preview in Settings
- **Searchable session history** — every session is recorded automatically as structured commands and output. *"What did I change on the Glasgow core last Tuesday"* is a search with a device filter and a date range, not a grep across a folder of log files
- **Session replay** — open any past session and read it back command by command, with the output and how long each took
- **Diff on connect** — every SSH login snapshots the running config and compares it against your last visit: *"you were last here 12 days ago, and 4 lines have changed since."* Runs on a second channel, so it never disturbs what you are typing
- **Multi-tab terminal** — connect to multiple network devices simultaneously, each in its own tab with an independent session, buffer and WebSocket
- **AI chat copilot** — Claude, OpenAI, xAI Grok, DeepSeek or local Ollama models see your live terminal output and answer questions about what's on screen
- **Tshoot / Learn mode toggle** — single pill in the tab bar flips the AI persona between *Troubleshoot* (terse, fix-it-now) and *Learn* (patient mentor that explains the why)
- **Knowledge-base augmentation (Chroma DB)** — point ShellMate at a Chroma vector store of your design guidelines and matching snippets are auto-retrieved and injected into every AI prompt; silently disabled when not configured
- **Configurable provider keys** — set Anthropic / OpenAI / xAI / DeepSeek / Ollama / Chroma credentials in the Settings panel as well as `.env`; the UI shows *"Already preconfigured by env variable"* when an env var is the active source
- **Command suggestions** — the AI suggests CLI commands you can approve with one click; dangerous commands get a confirmation prompt
- **Saved connection profiles** — save device details (no passwords stored) for one-click reconnect from the welcome screen
- **Session-aware context** — use `/context all` or `/context 2` to pull in other tabs; the AI always knows which tab is active
- **Tab management** — drag to reorder, right-click context menu, `Ctrl+1–9` shortcuts, `Ctrl+T`/`Ctrl+W`
- **Settings panel** — font, size, colour scheme (Deep Space, Solarized Dark, Nord, One Dark, Gruvbox, Dracula, Monokai), cursor, scrollback, UI text size, AI provider keys, Chroma DB endpoint
- **Conclude to Jira** — bundle session transcripts + chat into a Jira ticket (or comment on an existing one) with one click
- **Light / dark theme** — toggle from the sidebar
- **Smart copy/paste** — `Ctrl+C` (smart — copies selection or passes SIGINT), `Ctrl+Shift+C/V`, right-click paste dialog
- **Session logging** — optional per-session file logging to a configurable directory

## Tech stack

| Layer | Technology |
|---|---|
| Backend | Python · FastAPI · uvicorn · paramiko · pyserial |
| Frontend | Vanilla JS · xterm.js · HTML/CSS |
| AI | Claude · OpenAI · xAI Grok · DeepSeek · Ollama (local) |
| Knowledge base | Chroma vector DB (optional) |

## Getting started

There are two ways to run ShellMate. Use the **portable executable** if you just
want to use it; use the **source install** if you want to develop it.

### Portable executable (no install, no admin rights)

`ShellMate-Portable.exe` is a single self-contained file. It needs no Python, no installer
and no administrator rights, and it runs happily from a USB stick on a locked-down
corporate machine. All third-party assets are bundled, so it works with no
internet access at all.

Download it, put it wherever you like, and double-click it. It opens as a
**desktop application** — its own window, its own taskbar entry, no browser
chrome — using the WebView2 runtime that ships with Windows 11. There is no
console window; every run writes `ShellMate-Data/shellmate.log` instead.

**Closing the window does not end your sessions.** ShellMate keeps running in
the system tray with every connection alive, which matters when you have shut
the window on a device that is mid-reload. Reopen it from the tray icon, or
choose **Quit** there to stop properly.

If the WebView2 runtime is missing (some older Windows 10 builds), it falls
back to a chromeless Edge window, and then to your default browser. It always
starts. And because the UI is just a local web page, you can always point a
browser at `http://localhost:8765` as well — useful for devtools or a second
view of the same sessions.

Command-line flags, mostly for testing: `--browser` opens the default browser
instead of a window, and `--no-window` serves without opening anything.

On first run it creates a `ShellMate-Data/` folder **next to the executable**
holding your settings, profiles and logs. Everything travels with the exe — move
the folder to another machine and your setup comes with it. If the location it is
run from happens to be read-only (Program Files, a write-protected stick), it
falls back to `%LOCALAPPDATA%\ShellMatePortable` and says so clearly in the console.

To build it yourself:

```bash
pip install -r requirements-dev.txt
pyinstaller build.spec --noconfirm
# -> dist/ShellMate-Portable.exe
```

If corporate antivirus quarantines the single-file build, set `ONEFILE = False`
in `build.spec` to produce a folder build instead. It starts faster and is far
less likely to be flagged.

### Source install

#### Requirements

- Python 3.11+
- Network access to an SSH device (or use localhost for testing)
- An API key for at least one of Anthropic, OpenAI, xAI, DeepSeek, **or** [Ollama](https://ollama.ai) running locally
- *(Optional)* a [Chroma](https://www.trychroma.com/) server hosting a `design_guidelines` collection if you want vector-RAG augmentation

#### Install

```bash
git clone https://github.com/sjohnston1972/shellmate.git
cd shellmate
pip install -r requirements.txt
```

Third-party frontend assets (xterm.js, fonts) are committed under
`frontend/vendor/`, so nothing is fetched from a CDN at runtime. To refresh or
upgrade them:

```bash
pip install -r requirements-dev.txt
python tools/vendor_assets.py
```

#### Configure

```bash
cp .env.example .env
# Add your ANTHROPIC_API_KEY (or any of OPENAI_API_KEY, XAI_API_KEY, DEEPSEEK_API_KEY)
# or leave them all blank and use Ollama
```

Anything in `.env` can be overridden at runtime in **Settings → AI Providers** and
**Settings → Knowledge Base (Chroma DB)**. The hierarchy is:

1. Value saved in the Settings panel (stored in `settings.json`) wins
2. Falls back to the matching `.env` variable
3. If neither is set, the provider is simply unavailable

For a Chroma-backed knowledge base, set `CHROMA_URL` (e.g. `http://localhost:8000`)
and `CHROMA_COLLECTION` (defaults to `design_guidelines`). When unset, ShellMate
silently skips the lookup — there's no penalty for leaving it disabled.

#### Run

```bash
python run.py
```

ShellMate starts a local web server and opens your browser to it automatically.
It prefers port 8765 and walks upward if that is already taken, so it will not
fail to start just because something else has the port. Launching it a second
time opens the copy that is already running rather than starting a rival server.

### Where your data lives

| What | Where |
|---|---|
| Settings | `ShellMate-Data/settings.json` |
| Connection profiles | `ShellMate-Data/profiles.json` (never any secret) |
| API keys and saved passwords | `ShellMate-Data/vault.json` (encrypted) |
| Session history and configs | `ShellMate-Data/shellmate.db` (SQLite) |
| Platform definitions | `ShellMate-Data/platforms.json` (yours to edit) |
| Session logs | `ShellMate-Data/logs/` |
| Pre-seeded `.env` | Next to the executable |

### Credentials

Nothing sensitive is written in plain text. API keys and any device passwords
you choose to remember go into an encrypted vault, and connection profiles hold
only the non-secret details needed to reconnect.

Two ways to protect the vault, switchable in **Settings → Credentials Vault**:

| Mode | What it means |
|---|---|
| **Windows account** (default) | Encrypted with your Windows login. Nothing to type, and the file cannot be decrypted by another user or on another machine — a lost stick is inert. Does not travel between accounts. |
| **Master password** | Encrypted with a passphrase (scrypt + AES-GCM). Works on any machine, at the cost of typing it each launch. |

Keys left in plain text by an earlier version are moved into the vault
automatically on first run and blanked from `settings.json`.

Two honest limits. There is **no recovery** for a forgotten master password —
the key is derived from the passphrase and nothing else. And this protects
against a lost USB stick or someone reading files off the disk, not against
malware already running as you, which can ask Windows to decrypt exactly as
ShellMate does. Full-disk encryption remains worth having.

`ShellMate-Data/` sits beside the executable when frozen, and in the repository
root when running from source. Data written by older versions to the project root
is migrated automatically on first run. The current location is always shown at
`GET /api/system/info`.

### Run with Docker

A `Dockerfile` and `docker-compose.yml` are included. The compose file attaches the
container to an external Docker network called `net_core`, so it pairs naturally
with a Cloudflare tunnel or any other reverse-proxy container on the same network.

```bash
cp .env.example .env       # add your keys
docker compose up -d --build
```

The container binds uvicorn to `0.0.0.0:8765`, mounts `./ShellMate-Data`
for persistence, and exposes `8765:8765` for direct local access. To reach an
Ollama instance on the host, set `OLLAMA_HOST=http://host.docker.internal:11434`
(or the LAN IP of the box running Ollama) in `.env`.

If you front the container with a TLS reverse proxy, the WebSocket clients pick
`wss://` automatically — no extra configuration needed.

## Usage

| Action | How |
|---|---|
| New connection | Click **+ New** in the tab bar, or `Ctrl+T` |
| Quick connect | Click a saved device tile on the welcome screen |
| Switch AI mode | Click the **MODE** pill in the tab bar to flip between *Tshoot* and *Learn* |
| Pick AI model | Use the model dropdown in the chat header (cloud + local groups) |
| Switch tab | Click the tab, or `Ctrl+1` – `Ctrl+9` |
| Close tab | Click **×** on the tab, or `Ctrl+W` |
| Reorder tabs | Drag and drop |
| Ask the AI | Type in the chat panel on the right |
| Include all tabs in AI context | Start message with `/context all` |
| Include a specific tab | Start message with `/context 2` |
| Run AI-suggested command | Click **Send** on the command block |
| Send the session to Jira | Click **Conclude** in the chat header |
| Copy terminal text | `Ctrl+C` (with selection), or `Ctrl+Shift+C` |
| Paste into terminal | `Ctrl+V` or right-click |
| Open settings | Gear icon in the left sidebar |
| Configure provider keys / Chroma | Settings → *AI Providers* and *Knowledge Base (Chroma DB)* |
| Toggle light/dark theme | Moon icon in the left sidebar |

## Project structure

```
shellmate/
├── run.py                     # Entry point — starts server, opens browser
├── requirements.txt
├── .env.example               # Configuration template
├── backend/
│   ├── app.py                 # FastAPI app, REST endpoints, WebSocket handlers
│   ├── config.py              # Loads .env config
│   ├── profiles.py            # Connection profile persistence
│   ├── settings_store.py      # Application settings persistence
│   ├── connections/
│   │   ├── manager.py         # Session lifecycle (create/track/destroy by UUID)
│   │   ├── ssh_handler.py     # paramiko SSH interactive shell
│   │   └── serial_handler.py  # pyserial console
│   ├── session/
│   │   └── buffer.py          # Per-session terminal I/O buffer
│   └── ai/
│       ├── router.py          # Routes to selected backend, builds session context, queries Chroma
│       ├── prompts.py         # Tshoot + Learn personas, context builder
│       ├── claude_client.py   # Claude API streaming client
│       ├── openai_client.py   # OpenAI streaming client
│       ├── xai_client.py      # xAI Grok streaming client
│       ├── deepseek_client.py # DeepSeek streaming client
│       ├── ollama_client.py   # Ollama streaming client
│       ├── chroma_client.py   # Optional Chroma vector-DB lookup (silently disabled if not configured)
│       └── summarize.py       # One-shot session summary used by Conclude → Jira
└── frontend/
    ├── index.html
    ├── css/style.css
    └── js/
        ├── connections.js     # Connection dialog + saved profiles
        ├── tabs.js            # Tab bar management + drag reorder
        ├── terminal.js        # xterm.js init, copy/paste, settings apply
        ├── mode.js            # Tshoot / Learn pill toggle, persists to localStorage
        ├── chat.js            # AI chat panel, command blocks, streaming
        ├── settings.js        # Settings panel (incl. provider keys + Chroma)
        ├── jira.js            # Conclude-session → Jira modal
        └── logs.js            # Logs panel
```

## Design

ShellMate uses the *Deep Space* design system — dark background, Space Grotesk headlines, Inter UI text, and JetBrains Mono for the terminal. Built to feel like a high-performance instrument, not a SaaS dashboard. A light theme is also available.

## Security

- **No built-in authentication.** ShellMate is an interactive SSH client, so anyone who reaches the web UI can launch sessions to any host the server can route to. Treat it like an open shell:
  - Local development (`python run.py`) binds to `127.0.0.1` only — fine for a single user on the same machine.
  - The Docker / `docker-compose.yml` path binds to `0.0.0.0:8765` so the container is reachable on its network. **Do not expose it directly to the public internet.** Put it behind something that authenticates users — Cloudflare Access, Tailscale, an SSO-aware reverse proxy, etc.
- **Passwords are never persisted and dropped from memory once the SSH session is open.** They're prompted on each new connection, used to complete the authentication handshake, then cleared — the long-lived session object holds an empty string in their place.
- **API keys** live in `.env` only — never in code, never in saved profiles.
- **Session buffers** are in-memory and cleared on disconnect, unless file logging is explicitly enabled.
- **Saved profiles** record host, port, username, and connection type so you can one-click reconnect. They never contain the password — that is always re-prompted.

## License

MIT
