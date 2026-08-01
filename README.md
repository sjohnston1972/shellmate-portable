# ShellMate Portable

A split-screen, multi-tab network terminal with a built-in agentic AI copilot. Built for network engineers working with Cisco switches, routers, firewalls and similar devices.

Ships as a **single portable executable** — no Python, no installer, no administrator rights, and no internet access required. Runs from a USB stick on a locked-down corporate machine and carries its settings with it.

> A fork of [ShellMate](https://github.com/sjohnston1972/shellmate), reshaped around portability and a much wider feature set.

![ShellMate welcome screen](docs/screenshot-welcome.png)

## What it does

- **SSH, serial console and telnet** — one tabbed terminal for all three. SSH supports password or key authentication (Ed25519/ECDSA/RSA/DSA, encrypted keys included) and jump-host chaining through a bastion. Serial enumerates the COM ports actually present on the machine and sends break for ROMMON access. Telnet negotiates options properly and can answer login prompts for you
- **SFTP file browser** — pull a config off a device or push an image to it over the SSH connection the tab already has open. No second login, no separate tool
- **Encrypted credentials vault** — API keys and remembered device passwords are encrypted on disk, never in plain text. By default they are tied to your Windows account, so a lost USB stick is useless to anyone else; switch to a master password if you need the vault to work on any machine
- **Knows what it is talking to** — identifies IOS/IOS-XE, NX-OS, ASA, Junos, PAN-OS, Arista EOS and Linux from the login banner, then adapts: turns paging off with the right command so you stop typing `terminal length 0`, picks the right config command, and shows the platform and version in the status bar. Nothing is sent to a device it cannot confidently identify — and when that happens it says so, names the command it declined to send, and lets you name the platform yourself
- **Cross-vendor aliases** — type `ints` and get `show ip interface brief` on IOS, `show interfaces terse` on Junos, `show interface all` on PAN-OS. The terminal shows what was actually sent
- **Configurable output colours** — `down` in red, `up` in green, `error` in orange, driven by your own regex rules with a live preview in Settings
- **Searchable session history** — every session is recorded automatically as structured commands and output. *"What did I change on the Glasgow core last Tuesday"* is a search with a device filter and a date range, not a grep across a folder of log files
- **Session replay** — open any past session and read it back command by command, with the output and how long each took
- **Diff on connect** — every SSH login captures the running config on a second channel, invisibly, and compares it against your last visit. If it changed you are *asked* whether you want to see the difference, never interrupted with it; the diff opens as readable blocks with a copy button on each. Captures can also be kept as redacted `.cfg` files wherever you choose, with retention limits
- **Pending-action alerts** — `reload in 10` and Junos `commit confirmed` are tracked per session with a live countdown on the tab and in the status bar, re-synchronised against the device's own announcements. How loudly it interrupts you — flash, tone, pop-up — is yours to set
- **Split views** — seven tile layouts (`Ctrl+Alt+1`–`7`), with tabs assigned to panes from their context menu. Sessions in hidden panes keep running and keep their scrollback
- **Multi-tab terminal** — connect to multiple network devices simultaneously, each in its own tab with an independent session, buffer and WebSocket
- **AI chat copilot** — Claude, OpenAI, xAI Grok, DeepSeek or local Ollama models see your live terminal output and answer questions about what's on screen. Off until you ask for it, so a fresh install opens with the terminal at full width and no provider configured
- **Tshoot / Learn mode toggle** — a pill in the chat header flips the AI persona between *Troubleshoot* (terse, fix-it-now) and *Learn* (patient mentor that explains the why)
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
git clone https://github.com/sjohnston1972/shellmate-portable.git
cd shellmate-portable
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

The tree below is a summary. `CLAUDE.md` carries the authoritative one with a
line explaining each module — this is 24 backend modules, 35 frontend scripts
and 24 test suites, so a full listing here would be a second copy to keep in
step and it would lose.

```
shellmate-portable/
├── run.py                     # Entry point — server thread, window on the main thread
├── build.spec                 # PyInstaller definition for the one-file executable
├── tools/
│   ├── vendor_assets.py       # Downloads and subsets frontend assets (build-time)
│   └── collect_licences.py    # Gathers third-party licence texts (build-time)
├── backend/
│   ├── paths.py               # Every filesystem location resolves here
│   ├── app.py                 # FastAPI routes, WebSockets, auth gate
│   ├── auth.py                # Optional token authentication
│   ├── advanced.py            # Stockton — the settings registry
│   ├── vault.py               # Encrypted credential storage
│   ├── profiles.py            # Saved connections and shared credentials
│   ├── platforms.py           # Per-platform commands and aliases (data, not code)
│   ├── fingerprint.py         # Identify vendor/OS/version on connect
│   ├── discovery.py           # Network scanning
│   ├── store.py               # SQLite session history with FTS5
│   ├── configs.py             # Config capture, diff, drift-on-connect
│   ├── desktop.py             # Native window, tray, self-restart
│   ├── session/               # ANSI handling, transcript parsing, buffers, redaction
│   ├── connections/           # ssh / serial / telnet handlers, SFTP, session manager
│   └── ai/                    # Provider clients, router, prompts
├── frontend/
│   ├── index.html
│   ├── css/style.css
│   ├── js/                    # 35 modules — one per panel or concern
│   ├── docs/                  # The bundled manual, including licences/
│   └── vendor/                # xterm.js and fonts — no CDN at runtime
└── test_*.py                  # 24 suites, run individually: python test_vault.py
```

## Design

ShellMate uses the *Deep Space* design system — dark background, Space Grotesk headlines, Inter UI text, and JetBrains Mono for the terminal. Built to feel like a high-performance instrument, not a SaaS dashboard. A light theme is also available.

## Security

- **No authentication by default, and that is a bargain rather than an oversight.** ShellMate is an interactive SSH client: anyone who reaches the web UI can open sessions to any host the server can route to, read every open session, and browse the filesystem. That is acceptable only while nothing but the local machine can reach the port.
  - `python run.py` binds `127.0.0.1` — the case ShellMate is built for, and nothing changes.
  - **Binding anywhere else requires `SHELLMATE_AUTH_TOKEN`, and ShellMate refuses to start without it.** Not a warning: a hard failure, because a warning in a log nobody reads is how an installation ends up exposed. Set it to a long random string and ShellMate asks for it before opening anything, over both HTTP and WebSockets.
  - The Docker / `docker-compose.yml` path binds `0.0.0.0:8765`, so the token is mandatory there — the compose file will not start without one. A reverse proxy that authenticates in front of it (Cloudflare Access, Tailscale, an SSO-aware proxy) is still worth having; the token is the floor, not the ceiling.
- **Passwords are dropped from memory once the SSH session is open.** They complete the authentication handshake and are then cleared — the long-lived session object holds an empty string in their place.
- **A password is only persisted if you ask for it, and you are told where it went.** Tick *Remember these credentials* and it goes to the encrypted vault. Tick the plaintext option instead and it is written as readable text to `ShellMate-Data/credentials-plaintext.json` — named for exactly what it is, kept out of `profiles.json` so that file stays safe to share, and logged at warning level each time it is used. That option exists because a vault you are locked out of is worse than no vault; think twice on anything shared.
- **The vault** is encrypted with your Windows account by default, so there is nothing to type and a stolen USB stick is inert. A master password is the alternative and travels between machines, at the cost of typing it at startup — with no recovery, deliberately. The whole entry set is one AEAD blob rather than per-entry ciphertext, so entries cannot be swapped or replayed undetected, and which keys exist is not disclosed.
- **Settings → Credentials Vault** lists everything saved, marks which store each one is in, will move a plaintext one into the vault, and lets you change or delete any of them. A plaintext credential can be displayed — it is already readable in a text file — and an encrypted one cannot, at any price.
- **A saved password never reaches the browser.** The interface sends a connection id and the server fills the credential in on its own side, so a remembered password exists only on disk and in memory during the handshake. `SECRET_FIELDS` stops one reaching `profiles.json` whatever a caller passes.
- **API keys** resolve vault → `settings.json` → `.env`. The Settings panel is the preferred place; `update_settings()` diverts them into the vault and blanks them before writing, so they do not land in `settings.json` either.
- **Session buffers** are in memory and cleared on disconnect. Command transcripts are a separate thing and *are* recorded to `ShellMate-Data/shellmate.db` unconditionally — that is the History and drift feature, and it is documented in the manual.
- **Terminal content sent to a cloud AI provider is masked** on the way out by the same redaction that covers session logs. It is pattern matching, so it reduces exposure rather than guaranteeing absence. Ollama sends nothing anywhere.

## Licence

ShellMate is owned by **Foundry Networks and Services**, and is proprietary
software rather than open source. Copyright © 2025–2026 Foundry Networks and
Services. Provided **as is**, without warranty of any kind — and it is a tool
that types commands into live network devices, so that is worth reading rather
than skimming.

Contact **support@foundry-ns.com**.

The bundled manual's **Legal and licences** page carries the full statement,
the warranty disclaimer, a note on what is sent to AI providers, and
attribution for every third-party component in the executable — with each
licence text reproduced in `frontend/docs/licences/`. Regenerate both after
changing a dependency:

```bash
python tools/collect_licences.py
```
