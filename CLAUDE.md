# ShellMate Portable

## Project overview

ShellMate is a split-screen, multi-tab network terminal with a built-in agentic AI copilot. The left pane is a fully functional terminal (like PuTTY) for connecting to network devices via SSH or serial console, with tabs for multiple simultaneous sessions. The right pane is an AI chat interface that can see everything happening in the active terminal session — and can be made aware of all open sessions — to have a conversation about what's on screen. The AI can also suggest commands which the user can approve and inject into the live terminal session with a single click.

Multi-tab is a core architectural feature, not a bolt-on. Every session has its own connection, terminal instance, and session buffer, all identified by a unique session ID. The tab bar, session management, and per-tab state must be built from Phase 1 onwards.

This is a tool built for network engineers working with Cisco switches, routers, firewalls and similar devices. The user is not a developer — the UI should be clean, intuitive and require zero configuration to get started.

## Architecture

ShellMate is a two-layer application:

1. **Python backend** (FastAPI) — handles SSH/serial connections, session buffering, AI API routing, and the command approval pipeline. Communicates with the frontend over WebSockets.
2. **Web frontend** (HTML/JS/CSS) — served locally by the backend. Uses xterm.js for the terminal emulator and a custom chat panel for the AI. Runs in the user's browser at `http://localhost:8765`.

The backend is the brain. The frontend is the face. All connection logic, AI calls and session state live in Python.

### Data flow

Every connection lives inside a **session**, identified by a unique session ID (UUID). Tabs map 1:1 to sessions. The frontend tracks which tab is active and routes keystrokes/chat to the correct session.

```
User keystroke → browser WebSocket (with session_id) → FastAPI → correct Paramiko/pyserial session → network device
Device output  → Paramiko/pyserial → FastAPI → WebSocket (with session_id) → correct xterm.js tab
                                             → session buffer for that session_id (stored in memory)

User chats with AI → browser WebSocket → FastAPI → AI router (receives active session_id + buffer)
AI reads session buffer for active tab → generates response → WebSocket → chat pane
AI can also be given buffers from ALL open sessions if the user asks (e.g., "compare the config on tab 1 vs tab 2")
AI suggests command → displayed as clickable button in chat pane
User clicks approve → command sent via WebSocket with session_id → FastAPI → injected into correct session
```

## Tech stack

### Backend (Python)
- **FastAPI** — async web framework, serves the frontend and handles WebSocket connections
- **uvicorn** — ASGI server to run FastAPI
- **paramiko** — SSH client library (what Netmiko is built on)
- **pyserial** — serial port communication for console cables
- **httpx** — async HTTP client for calling Claude API and Ollama API
- **python-dotenv** — load configuration from .env file

### Frontend (HTML/JS/CSS)
- **xterm.js** — terminal emulator component (load from CDN)
- **xterm-addon-fit** — auto-resize terminal to container (load from CDN)
- **xterm-addon-web-links** — clickable URLs in terminal output (load from CDN)
- Vanilla JS — no frameworks needed for the chat panel and UI chrome

### AI backends (user-selectable)
- **Claude API** (api.anthropic.com) — for complex reasoning, troubleshooting, deep analysis
- **Ollama** (localhost:11434) — for fast local inference, lower cost, privacy

## Project structure

Generated from the tree and each module's own header line — regenerate
rather than hand-edit (`python tools/claude_tree.py --write`). A module
with no description has no header line yet; give it one.

```
shellmate/
├── CLAUDE.md                    # This file — project spec and instructions
├── README.md                    # User and builder documentation
├── requirements.txt             # Runtime dependency floors
├── requirements.lock            # Exact versions a release is built from
├── requirements-dev.txt         # Build, vendoring and test dependencies
├── build.spec                   # PyInstaller definition; writes build_info.json
├── run.py                       # Entry point — server on a thread, window on main
├── Dockerfile, docker-compose.yml # Server deployment (token required)
├── .github/workflows/ci.yml     # Tests, build, sign, release
├── test_*.py                    # Standalone test scripts; tools/run_tests.py runs all
├── tools/
│   ├── claude_tree.py           # The project tree in CLAUDE.md, generated rather than typed
│   ├── collect_licences.py      # Gather the licence texts ShellMate has to redistribute
│   ├── release_notes.py         # The release body and checksum for a tagged build
│   ├── run_tests.py             # Run every test_*.py and say, once, whether they all passed
│   ├── seed_estate.py           # Fill a data folder with an estate, for scale testing
│   ├── vendor_assets.py         # Download ShellMate's third-party frontend assets locally
├── relay/                       # Cloudflare Worker: in-app feedback → GitHub issues
├── backend/
│   ├── advanced.py              # Stockton. The settings that were constants in the source
│   ├── alerts.py                # Things that will happen to a device unless somebody intervenes
│   ├── ansible.py               # Driving Ansible from ShellMate, through a runner service (#585)
│   ├── ansible_builder.py       # Getting to a first playbook without writing YAML (#586)
│   ├── ansible_examples.py      # Templates worth starting from (#590)
│   ├── ansible_git.py           # Keeping a playbook's history somewhere that has one (#609)
│   ├── ansible_health.py        # Is the link to the runner healthy, and is it actually secure?
│   ├── ansible_inventories.py   # Inventories somebody built, rather than the estate (#608)
│   ├── ansible_keys.py          # The secrets an automation needs, and where they go (#586)
│   ├── ansible_library.py       # What ShellMate keeps for Ansible, beside the runner (#586)
│   ├── app.py                   # FastAPI application for ShellMate
│   ├── auth.py                  # Optional authentication, for the deployments that need it
│   ├── branding.py              # One source for the application icon
│   ├── broadcast_collect.py     # The replies a broadcast produced, and how they differ (#529)
│   ├── certs.py                 # Read an OpenSSH certificate and say what it actually permits
│   ├── change.py                # A piece of work, bracketed (#544)
│   ├── compliance.py            # Did the standard land everywhere? (#543)
│   ├── config.py                # Configuration loader for ShellMate
│   ├── config_archive.py        # Keeping the captured configurations as files
│   ├── config_push.py           # Apply configuration with a preview first, and a way back (#407)
│   ├── configs.py               # Configuration capture, storage and drift reporting
│   ├── desktop.py               # Present ShellMate as a desktop application rather than a browser…
│   ├── diagnostics.py           # Is this install healthy? (#562)
│   ├── discovery.py             # Finding out what is on the wire
│   ├── feedback.py              # The in-app bug and feature-request reporter (#370)
│   ├── fingerprint.py           # Work out what kind of device is on the other end
│   ├── groups.py                # Groups on the dashboard
│   ├── jira_client.py           # Jira Cloud REST API client for ShellMate session reporting
│   ├── jsonfile.py              # The one way a JSON data file is read and written (#457)
│   ├── keys.py                  # Making SSH keys, not just using them
│   ├── knowledge.py             # The team's own documents, retrievable without a server (#561)
│   ├── licence.py               # Licence keys, verified without a network (#446)
│   ├── logsearch.py             # Finding a line across every session log (#576)
│   ├── neighbours.py            # What the device you reached can see (#542)
│   ├── ollama_pull.py           # Fetch a local model without leaving the application (#555)
│   ├── onboard.py               # What happens in the first few seconds of a session
│   ├── paths.py                 # Single source of truth for every filesystem location ShellMate…
│   ├── pipeline.py              # The chokepoint every keystroke passes through on its way out
│   ├── platforms.py             # What ShellMate knows about each kind of device
│   ├── playback.py              # A recorded session as a page that replays itself (#574)
│   ├── profiles.py              # Connection profile persistence
│   ├── report.py                # A session, a diff or a change as a file somebody else can read
│   ├── scheduler.py             # Configuration backups on a timer, per group (#408)
│   ├── schemes.py               # Terminal colour schemes as data
│   ├── server.py                # Startup orchestration for ShellMate: port selection and
│   ├── settings_store.py        # Application settings persistence for ShellMate
│   ├── snippets.py              # The saved command library
│   ├── ssh_config.py            # What OpenSSH already knows about a host (#527)
│   ├── store.py                 # Persistent session history in SQLite
│   ├── support.py               # Building a diagnostic bundle worth reading
│   ├── updater.py               # Download a release and swap the executable (#443, #444, #448)
│   ├── vault.py                 # Encrypted storage for API keys and device credentials
│   ├── version.py               # What this copy of ShellMate is
│   ├── ai/
│   │   ├── chroma_client.py     # Optional Chroma vector-DB client for ShellMate
│   │   ├── claude_client.py     # Streaming Claude API client for ShellMate
│   │   ├── deepseek_client.py   # Streaming DeepSeek client for ShellMate
│   │   ├── explain.py           # The prompts ShellMate composes itself, from device data it holds
│   │   ├── http.py              # One httpx client per provider, reused across requests (#503)
│   │   ├── ollama_client.py     # Streaming Ollama API client for ShellMate
│   │   ├── openai_client.py     # Streaming OpenAI client for ShellMate
│   │   ├── openai_compat.py     # The one streaming loop for the OpenAI-shaped providers
│   │   ├── prompt_store.py      # The system prompts, as data rather than code
│   │   ├── prompts.py           # AI system prompts for ShellMate
│   │   ├── providers.py         # Model discovery per provider, cached to models.json
│   │   ├── router.py            # Routes AI chat requests to the correct backend (Claude / xAI /…
│   │   ├── summarize.py         # One-shot AI summary of a terminal session for the
│   │   ├── toolloop.py          # Answering what the assistant asked for (#560)
│   │   ├── tools.py             # What the assistant may ask ShellMate to do (#560)
│   │   ├── turns.py             # A conversation, shaped for each provider
│   │   ├── xai_client.py        # Streaming xAI (Grok) client for ShellMate
│   ├── connections/
│   │   ├── base.py              # ConnectionHandler contract and ConnectionParams
│   │   ├── forwards.py          # Port forwarding over an existing SSH session (#405)
│   │   ├── manager.py           # Session lifecycle and the transport registry
│   │   ├── serial_handler.py    # Serial console via pyserial
│   │   ├── sftp.py              # File transfer over an existing SSH transport
│   │   ├── ssh_handler.py       # SSH via paramiko: keys, jump host, second channel
│   │   ├── telnet_handler.py    # Telnet over a raw socket, with IAC negotiation
│   ├── session/
│   │   ├── ansi.py              # Undo escape sequences, backspace and bare CR
│   │   ├── buffer.py            # Rolling per-session screen buffer
│   │   ├── outbound.py          # The one door out: redaction before any AI call
│   │   ├── parsed.py            # Show output as rows, when a template exists for it (#404)
│   │   ├── redact.py            # Secret-pattern redaction for logs and prompts
│   │   ├── transcript.py        # Prompt detection and command segmentation
├── frontend/
│   ├── index.html               # The one page: tab bar, split panes, every panel
│   ├── css/style.css            # All styling; dark and light themes from tokens
│   ├── docs/*.md                # The bundled manual, rendered offline by docs.js
│   ├── vendor/                  # xterm.js, addons, fonts — no CDN at runtime
│   └── js/
│       ├── ai_panel.js          # Provider testing, model discovery, and hiding the AI panel
│       ├── alerts.js            # Telling someone that something is about to happen to a device
│       ├── ansible.js           # Driving an Ansible runner, and watching it work (#585)
│       ├── ansible_builder.js   # A playbook, drawn as the thing it is (#586, #600)
│       ├── ansible_dashboard.js # The Ansible view's first screen (#586)
│       ├── ansible_environments.js # Named settings a run inherits, so production is one choice not…
│       ├── ansible_estate.js    # Turning something dragged out of the tree into a target (#601)
│       ├── ansible_inventory.js # What a run is pointed at, and where a list comes from (#586, #608)
│       ├── ansible_keys.js      # Credentials a run needs, held in the vault and sent only with a…
│       ├── ansible_playbooks.js # Where the Playbooks area actually lives (#586)
│       ├── ansible_repositories.js # Where a set of playbooks came from, and how it gets to the runner…
│       ├── ansible_runs.js      # What has run, and what it did (#591)
│       ├── ansible_templates.js # Parameterised plays: the holes, the form that fills them, and…
│       ├── ansible_tls.js       # The TLS indicator, and the probe behind it (#586)
│       ├── ansible_view.js      # The Ansible view: which area is showing, and nothing else (#586)
│       ├── backup_digest.js     # What the overnight backups found (#539)
│       ├── broadcast.js         # Send commands to several devices at once
│       ├── change.js            # Bracketing a piece of work (#544)
│       ├── chat.js              # AI chat panel for ShellMate
│       ├── chat_context.js      # Choose which sessions the assistant can see
│       ├── compliance.js        # Did the standard land everywhere? (#543)
│       ├── config_push.js       # Apply configuration with a preview first (#407)
│       ├── connections.js       # Connection dialog and profile management
│       ├── credentials.js       # Managing the passwords ShellMate has been asked to remember
│       ├── device.js            # Report what ShellMate has worked out about each device
│       ├── dialog.js            # ShellMate's own confirm, prompt and alert
│       ├── discovery.js         # The panel that finds devices on the network
│       ├── docs.js              # The built-in manual, and the support link
│       ├── drift.js             # "This device has changed since you were last here. Want to see?"
│       ├── exit.js              # Exit ShellMate from the status bar (#452)
│       ├── feedback.js          # The bug / feature-request reporter (#370)
│       ├── filepicker.js        # Choosing a file on this machine
│       ├── forwards.js          # Port forwards on a session (#405)
│       ├── groups.js            # Groups on the dashboard
│       ├── highlight.js         # Colour terminal output by regex rule
│       ├── highlight_settings.js # Editor for the output colour rules
│       ├── history.js           # Search across every session ever recorded
│       ├── jira.js              # Conclude Session / Jira integration for ShellMate
│       ├── keys.js              # Making an SSH key without leaving the application
│       ├── knowledge.js         # The folder the assistant reads your own documents from (#561)
│       ├── layout.js            # Tiling. Show several sessions at once instead of one at a time
│       ├── licence.js           # The Licence section in Settings (#446, #448)
│       ├── logs.js              # Logs panel for ShellMate
│       ├── markdown.js          # A small Markdown renderer for the built-in documentation
│       ├── menu.js              # The one context menu
│       ├── mode.js              # Learn / Troubleshoot mode toggle
│       ├── neighbours.js        # "What else is on this site?" (#542)
│       ├── notes.js             # What you were doing, written down beside what you did (#530)
│       ├── ollama_pull.js       # Getting a local model without leaving the app (#555)
│       ├── palette.js           # Find a tab by name (#410), and recall a command (#522)
│       ├── panel_resize.js      # Dragging a side panel wider
│       ├── platforms_editor.js  # Edit device platform definitions in the app
│       ├── prefs.js             # Interface preferences that used to live in localStorage
│       ├── prompts_editor.js    # Reading and changing what the assistant is told
│       ├── regex_builder.js     # Build and test an output-colour pattern
│       ├── report.js            # Export a session, a diff or a change as a file (#540)
│       ├── settings.js          # Settings panel for ShellMate
│       ├── settings_nav.js      # Make Settings navigable instead of a long scroll
│       ├── sftp.js              # Remote file browser for the active SSH tab
│       ├── stockton.js          # The advanced settings, rendered from the registry
│       ├── support.js           # Assembling a support request worth answering
│       ├── tabs.js              # Tab bar management for ShellMate
│       ├── tabtip.js            # The hover card on a tab (#435)
│       ├── terminal.js          # xterm.js terminal initialisation for ShellMate
│       ├── tooltips.js          # The explanation that does not fit on the label
│       ├── update.js            # Updates, from the user's side (#420, #441, #442–#445, #448)
│       ├── uptime.js            # How long each session has been up
│       ├── usage.js             # ShellMate's own footprint, in the status bar (#266)
│       ├── vault.js             # Unlock prompt and vault settings
│       ├── visibility.js        # Timers that stop while the window is hidden (#491)
└── profiles/examples.json       # Example connection profile
```

## Portable runtime — rules that must not be broken

ShellMate ships as a single PyInstaller `--onefile` executable that runs with no
install and no administrator rights.

**Never derive a writable path from `__file__`.** Under `--onefile` the process
unpacks into a temporary directory that the bootloader deletes on exit, so
anything written relative to `__file__` is lost the moment the app closes — with
no error. All locations come from `backend/paths.py`:

| Helper | Meaning | Writable? |
|---|---|---|
| `app_dir()` | Folder containing the executable | — |
| `resource_dir()` | Bundled assets (`sys._MEIPASS` when frozen) | **No** — wiped on exit |
| `data_dir()` | `ShellMate-Data/` beside the exe, falling back to `%LOCALAPPDATA%` | Yes |

Two further constraints:

- **No CDN references in the frontend.** ShellMate must work fully air-gapped.
  Third-party assets live in `frontend/vendor/`, refreshed by
  `python tools/vendor_assets.py`. That script also subsets the Material Symbols
  icon font and verifies with HarfBuzz that every icon still shapes to a single
  glyph — a broken subset renders icons as plain text and raises no error.
- **Don't hardcode the port.** `backend/server.py` picks a free one at startup.

## The desktop shell

The UI is still a local web page; `backend/desktop.py` only changes the frame
around it. A native window via pywebview (WebView2), falling back to a
chromeless Edge window, then to the default browser — it must always start,
because a missing runtime is not a reason to be unable to reach a device.

Consequences worth knowing before editing `run.py`:

- **The server runs on a daemon thread and the window owns the main thread.**
  Native GUI toolkits require their event loop on the process main thread, so
  the ordering is forced, not preferred.
- **Closing the window hides it.** Terminal sessions live in the server
  process; closing the window while a device is mid-reload must not drop the
  connection. Quitting is explicit, from the tray.
- **`uvicorn.run(log_config=None)`.** Its default config calls `dictConfig`
  and replaces the process's log handlers, which silently truncates the log
  file — the only diagnostic a windowed build has.
- **The build is windowed (`console=False`).** Startup failures raise a
  message box pointing at `ShellMate-Data/shellmate.log`.
- **Tests must pass `--no-window`**, or `run.py` blocks on a window.

## Transports

All connection types implement `ConnectionHandler` in `backend/connections/base.py`
and are registered in `HANDLERS` in `manager.py`. Adding one means a subclass and
one line — nothing above the transport layer branches on connection type.

The subtle part of the contract is `recv()`, which returns:

| Value | Meaning |
|---|---|
| `bytes` | Data arrived |
| `None` | Nothing arrived yet — still connected |
| `b""` | The far end closed |

Conflating the last two drops sessions the moment a user stops typing, which is
why idleness is a return value here rather than an exception as in raw paramiko.

Two more rules worth stating because breaking them fails silently:

- **Credentials are scrubbed after connecting** via `ConnectionParams.scrub_secrets()`,
  and `SECRET_FIELDS` in `profiles.py` blocks them from ever being written to disk.
  `_public_view()` in `manager.py` keeps `params` out of every API response.
- **Telnet auto-login is deadline-bounded.** It answers a login prompt once and
  then disables itself. Without that, a prompt regex matching ordinary output an
  hour into a session would type a password into a live device.

## The transcript layer

A terminal stream is not text — it is instructions to a display, and the same
visible line can arrive as any number of byte sequences. Two modules turn it
back into something answerable:

- `session/ansi.py` — undoes escape sequences, backspace (how a device erases
  `--More--`) and bare carriage returns (how it redraws a line in place).
  Without this, stored output is unsearchable: a coloured interface name has
  escape codes buried inside the word.
- `session/transcript.py` — reconstructs commands by watching for the device
  prompt. Everything between one prompt and the next is one command and its
  output.

`PROMPT_RE` is the **single** prompt pattern, covering IOS, NX-OS, ASA, Junos,
PAN-OS and Linux. There were previously three Cisco-only copies (in `app.py`,
`ai/router.py`, and none at all in the store); anything needing prompt
detection uses `match_prompt()` or `detect_hostname()`.

The parser is deliberately conservative. A missed prompt merges two records; a
*false* prompt slices real output in half and files configuration lines under
the wrong command — much worse when the result is evidence of what changed.

`store.py` persists it all to SQLite with FTS5. Recording is automatic and
unconditional, and every write is wrapped so that a history failure can never
interrupt a live session.

## Device awareness

`fingerprint.py` identifies the device from its banner, falling back to the
prompt shape, and refines with a version command where a second channel is
available. Every result carries a **confidence**, and
`certain_enough_to_act` gates anything that touches the device — acting on a
weak guess is how a tool ends up sending `terminal length 0` to a firewall.

`platforms.py` holds everything platform-specific: paging-off command, config
command, aliases, dangerous commands. These are **data, not code** — written to
`platforms.json` in the data dir on first run and read back in preference to
the built-ins, so a new platform is a text edit rather than a rebuild.

`onboard.summarise()` is the single answer to "what was sent, and if not, why
not". Both gates — the confidence threshold and the user's setting — resolve
there, so the interface has one thing to believe rather than re-deriving the
decision and getting it subtly wrong. The reason codes (`unconfident`,
`unidentified`, `off`, `no-command`) are not interchangeable and the UI states
each differently: "identified Cisco IOS" while paging stayed on reads as
success, which is exactly the bug that produced this.

The threshold is met less often than it looks. `hostname(config)#` is printed
identically by IOS, NX-OS, ASA and EOS, and the command belongs to the
platform, not the family — so prompt-only identification stays below the bar
and `onboard.as_chosen()` is the way out, not a higher score.

`pipeline.py` is the chokepoint every keystroke passes through on its way out.
It assembles keystrokes into lines and can rewrite one before it reaches the
device. Alias expansion, the dangerous-command guardrail and the hold-and-
confirm it raises all live there.

Paste pacing is split between the two sides, because the two kinds answer
different questions. Chunking a paste by **bytes** is the frontend's, in
`terminal.js`: it slows a stream down, and that has to happen before the bytes
reach the socket. Pacing it by **lines** is `pipeline.PasteBatch`, driven from
the session's read loop, because what a sixty-line ACL needs is not a gap in
milliseconds but the device back at its prompt — and `idle_at_prompt` is only
visible on this side. Every line still goes through `pipeline.process`, so a
`reload` in a pasted block is held exactly as a typed one is, and the batch
waits for the answer rather than sending the next line past it.

Two rules for anything that writes into a live session:

- **Never send silently.** The paging command is echoed like any other, and the
  UI says what was sent and why. People have to account for what happened in
  their sessions afterwards.
- **Never guess.** The generic profile sends nothing at all. A wrong command is
  worse than a command not sent.

## Secrets

Nothing sensitive is ever written in plain text. `backend/vault.py` encrypts
with Windows DPAPI by default, or scrypt + AES-GCM under a master password.

Rules that keep it that way:

- **Never write a secret to settings.json or profiles.json.** `update_settings()`
  diverts `SECRET_FIELDS` into the vault and blanks them before writing;
  `save_profile()` strips `SECRET_FIELDS` from whatever it is handed. Both
  enforce it rather than trusting callers.
- **Resolve credentials through `get_effective()`.** It reads vault → settings →
  `.env`, so every AI client picks up the vault without changing.
- **Saved device passwords never reach the browser.** The frontend sends a
  `profile_id` and the backend fills credentials in server-side. Nothing
  returns a stored secret — profile listings carry a `has_saved_credentials`
  boolean instead.
- **The whole entry set is one AEAD blob**, not per-entry ciphertext. Per-entry
  encryption would leak which keys exist and let entries be swapped or replayed
  undetected.
- **A locked vault degrades to "no value"** rather than raising, so a forgotten
  master password never blocks reaching a device.
- **Redaction is one switch, not two.** `logging.redact_secrets` covers session
  logs *and* archived configurations. A running config carries hashes, keys and
  community strings, and the archive folder may be a share — two switches for
  one guarantee is a guarantee nobody can rely on.

## Themes

`style.css` carries a dark and a light theme. The rule that keeps them honest:
**a colour that changes with the theme must come from a token.** Six floating
surfaces once hardcoded `rgba(32,32,32,0.9x)` while their text colour followed
the theme, so in the light theme they rendered dark grey on near-black. Three
had light-theme overrides bolted on and three did not.

They now take `--overlay` / `--overlay-solid`. `test_contrast.py` asserts both
halves of this by measurement: that no floating surface hardcodes a background,
and that every resolved pair clears WCAG AA in both themes. Checking by eye is
unreliable here anyway — the theme transition is animated, so a reading taken
mid-transition measures a blend.

## Stockton — the advanced settings

`backend/advanced.py` holds ~53 values that used to be constants scattered
across the modules. The declaration **is** the default: `ssh_handler.py` calls
`advanced("ssh.connect_timeout")` rather than holding its own, and the panel
renders itself from the same registry. Sixty hand-written rows against sixty
constants would drift, and the drift would be silent — the label would go on
describing what the code no longer does.

Three rules for adding one:

- **It must be bounded, and the bound is enforced in `clamp()`**, not by the
  input's `min` attribute. The API is scriptable and settings.json is a text
  file people are told to edit.
- **The worst outcome must be *degraded*, never broken.** Anything that fails
  that goes in `NOT_EXPOSED` with its reason, which the panel shows — an
  absent setting with no explanation just sends someone hunting in the JSON.
- **The category prefix must have a heading** in `CATEGORIES`, or the row
  renders outside every group and is invisible. `test_advanced.py` checks
  this, along with every default surviving its own clamp.

A category may also carry **one bespoke section** where the thing being edited
genuinely is not a scalar. The system prompts are the worked example: two
kilobytes of prose in `prompts.json`, with their own reset and their own marker
warning. Widening `Setting` to express that would make it carry a storage
indirection it was designed not to have, so the editor is markup that
`stockton.js` moves into place instead.

Anything in Stockton that is *not* a registry entry has to be added to `EXTRAS`
in `settings_nav.js`, or the ordinary Settings search — which queries
`/api/advanced` — will not know it exists, and moving something there becomes a
regression for anyone who had learned where it was.

## Changing a default

`DEFAULT_SETTINGS` applies to an installation that has never been configured.
Changing one reaches into every existing setup, which is why
`LEGACY_DEFAULTS` in `settings_store.py` exists: a settings file that predates
a key keeps that key's old value, written in explicitly, and only a first run —
no file at all — sees the new one. `ai.panel_enabled` is the worked example.

## Configuration (.env)

```env
# ShellMate configuration

# Server
SHELLMATE_HOST=127.0.0.1
SHELLMATE_PORT=8765

# AI providers (all optional — leave blank to disable; the vault wins over .env)
ANTHROPIC_API_KEY=
OPENAI_API_KEY=
XAI_API_KEY=
DEEPSEEK_API_KEY=

# Optional Chroma store of design guidelines for the assistant
CHROMA_URL=
CHROMA_COLLECTION=design_guidelines

# Optional: bind wider than loopback — refuses to start without a token
# SHELLMATE_AUTH_TOKEN=

# Ollama (optional — defaults shown)
OLLAMA_HOST=http://localhost:11434
OLLAMA_MODEL=qwen2.5:14b

# Default AI backend: "claude" or "ollama"
DEFAULT_AI_BACKEND=ollama

# Ansible runner (optional — the address and certificates live in Settings).
# A fallback for a headless install; Settings puts the token in the vault.
ANSIBLE_RUNNER_TOKEN=

# Jira, for Conclude Session (optional). Settings has a Ticketing section
# that wins over these, and puts the token in the vault; these remain as the
# fallback for a setup that already had them.
JIRA_URL=
JIRA_USER_EMAIL=
JIRA_API_TOKEN=
JIRA_PROJECT_KEY=

# Serial port defaults (Windows)
DEFAULT_SERIAL_PORT=COM3
DEFAULT_BAUD_RATE=9600
```

Every one of these is read by `backend/config.py`, `backend/ansible.py` or
`backend/jira_client.py`.
A variable the code reads and this block does not mention is a variable
nobody knows exists; the drift is silent in that direction only.

## Build phases

**Where this stands:** Phases 1–5 are complete, and the application has grown
well past them — telnet, SFTP, discovery, groups, the vault, history and
drift, broadcast, keys and certificates, Jira export, the feedback relay.
The phases below are kept as the record of how it was built and what each
step was for. Open work is tracked as GitHub issues, not here.

Build ShellMate incrementally. Each phase should produce a working application that does something useful. Do not skip ahead — complete and test each phase before moving to the next.

### Phase 1 — Multi-tab SSH terminal (no AI yet)

**Goal**: A working multi-tab terminal in the browser that can SSH into devices, with each tab being an independent session.

**Backend — session architecture:**
1. Set up the project structure and install dependencies
2. Create the FastAPI app that serves `index.html` as a static file
3. Implement `connections/manager.py` — a `SessionManager` class that maintains a dictionary of active sessions keyed by session ID (UUID). Each session holds: connection handler, session buffer, metadata (hostname, connection type, connect time, display label)
4. Implement `session/buffer.py` — a `SessionBuffer` class that stores all terminal I/O for a single session. One buffer instance per session
5. Implement `ssh_handler.py` — connect to a device using paramiko's `invoke_shell()`, return the channel. Each SSH connection is independent
6. Implement WebSocket endpoint `/ws/terminal/{session_id}` — the session_id in the URL tells the backend which session this WebSocket belongs to. On each message: look up the session by ID, pipe the data to/from the correct paramiko channel
7. Implement REST endpoint `POST /api/sessions` — creates a new session (accepts hostname, port, username, password, connection type), returns the new session_id
8. Implement REST endpoint `GET /api/sessions` — returns list of active sessions with metadata (for the tab bar to display)
9. Implement REST endpoint `DELETE /api/sessions/{session_id}` — tears down a session (closes SSH, clears buffer, removes from manager)

**Frontend — tab UI:**
10. Build `index.html` with a tab bar across the top and a terminal area below. Initially shows a "welcome" state with a connect button
11. Implement `tabs.js` — manages the tab bar UI. Each tab stores: session_id, display label, the xterm.js Terminal instance, and the WebSocket connection. Clicking a tab shows/hides the correct terminal (use CSS `display:none` on inactive terminals, NOT destroying and recreating them — xterm.js needs to stay alive to receive background data)
12. Implement `connections.js` — a connection dialog (modal) that asks for: display name (optional), hostname, port (default 22), username, password. On submit: POST to `/api/sessions`, get back session_id, create a new tab
13. Implement `terminal.js` — when a new tab is created, instantiate a new xterm.js Terminal, open a WebSocket to `/ws/terminal/{session_id}`, bind them together. Handle resize events with the fit addon
14. Tab close button (×) on each tab: sends DELETE to `/api/sessions/{session_id}`, closes WebSocket, destroys terminal instance, removes tab
15. Keyboard shortcuts: `Ctrl+T` opens connection dialog (new tab), `Ctrl+W` closes active tab, `Ctrl+1` through `Ctrl+9` switches to that tab number
16. Create `run.py` that starts uvicorn and opens the browser automatically

**Important implementation details:**
- xterm.js instances for background tabs must continue to exist and receive data even when not visible — if a command is running on a background tab, the output must still be captured. Only toggle CSS visibility, never destroy inactive terminals
- Each tab's WebSocket is independent — closing one tab does not affect others
- The tab label should auto-detect the device hostname from the CLI prompt if possible (parse for patterns like `hostname#` or `hostname>`), falling back to the display name or IP address
- Handle disconnection per-tab: if a session drops, mark that tab visually (greyed out label, "(disconnected)" suffix) but don't remove it — the user might want to read the buffer

**Test**: User can open ShellMate, create three tabs connecting to three different switches, switch between them freely, type commands in each, close individual tabs, and the other sessions remain unaffected.

### Phase 2 — Split screen with AI chat

**Goal**: Add the AI chat pane alongside the terminal tabs. The AI is session-aware and can see the active tab's terminal output.

1. Redesign `index.html` for split-screen layout — terminal pane (with tab bar) on the left (60% width), chat on the right (40% width), with a draggable divider between them
2. Build the chat panel UI — message history area, text input box, send button. The chat panel is global (one chat, not per-tab) but the AI always knows which tab is currently active
3. Implement WebSocket endpoint `/ws/chat` in FastAPI — chat messages include the active `session_id` so the backend knows which session buffer to include as context
4. Wire up the session buffer (built in Phase 1) — ensure every byte of terminal I/O is being written to the correct session's buffer
5. Implement `ai/router.py` — accepts a chat message, the active session's buffer content, and the selected backend; routes to the correct AI client
6. Implement `ai/ollama_client.py` — sends the chat message with session context to Ollama, streams the response back token by token over the WebSocket
7. Implement `ai/claude_client.py` — same thing but for Claude API
8. Add AI backend selector toggle in the chat panel header (Claude / Ollama)
9. The AI system prompt (in `prompts.py`) should establish the AI as a senior network engineer who can see the terminal session and is here to help
10. When the user switches tabs, the next AI message should automatically use the new active tab's session buffer — no manual action required
11. Add a chat command `/context all` that includes ALL open session buffers in the next AI request (for cross-device questions like "compare the BGP tables on tab 1 and tab 3"). Add `/context [tab_number]` to target a specific tab regardless of which is active

**Test**: User can SSH into a device in one tab, run commands, then ask the AI "what does this output mean?" and get a contextual answer. User switches to a different tab and asks about that device — the AI seamlessly switches context.

### Phase 3 — Command suggestions (suggest and approve)

**Goal**: The AI can suggest CLI commands that the user approves with one click, sent to the correct tab's session.

1. Update the AI system prompt to instruct it to wrap suggested commands in a specific format: `[SUGGEST_CMD]show ip bgp summary[/SUGGEST_CMD]`
2. In the chat panel frontend, parse AI responses for these tags and render them as styled clickable command blocks (monospace, highlighted, with a "Send to terminal" button and an "Edit" button). Each command block is tagged with the session_id it was generated for (i.e., whichever tab was active when the AI responded)
3. When the user clicks "Send to terminal", send the command via WebSocket to the backend with the correct session_id — it gets injected into the right session even if the user has since switched tabs. Show a small label on the command block indicating which tab it will target (e.g., "→ switch01")
4. When the user clicks "Edit", make the command text editable before sending
5. Add a visual indicator in the terminal when a command was AI-suggested (e.g., a subtle flash or marker in the chat log)
6. Add a confirmation step for potentially dangerous commands — the AI should flag commands like `reload`, `write erase`, `shutdown`, `no shutdown` (on interfaces), `clear` commands — these get an "Are you sure?" prompt

**Test**: User asks "how do I check the spanning tree status?" — AI responds with explanation and a clickable `show spanning-tree summary` command block. User clicks it, command executes in the terminal, output appears.

### Phase 4 — Serial console support

**Goal**: Add serial/console cable connections alongside SSH.

1. Implement `serial_handler.py` — connect to a COM port using pyserial, pipe data over WebSocket
2. Update the connection dialog to offer connection type: SSH or Serial
3. For serial: ask for COM port (with auto-detection of available ports), baud rate (default 9600), data bits, parity, stop bits
4. Add a backend endpoint that returns available COM ports (pyserial can enumerate these)
5. Serial connections use the same terminal pane and session buffer as SSH — the rest of the app is connection-agnostic
6. Handle serial-specific quirks: send a carriage return on connect to wake the device, handle break signals

**Test**: User plugs in a console cable to a Cisco switch, selects Serial connection in ShellMate, picks the COM port, and gets a working console session.

### Phase 5 — Connection profiles and polish

**Goal**: Save and manage connection profiles, general UX polish.

1. Create a connection profile system — save/load device profiles as JSON (display name, hostname, port, username, connection type). Stored in `profiles/` directory
2. Passwords should NOT be stored in profiles — prompt on connect (or integrate with system keyring later)
3. Add a sidebar or dropdown for quick-connecting to saved profiles — clicking a profile opens a new tab with that connection pre-filled, just needs password
4. Add a "Save profile" button in the connection dialog that saves current settings
5. Add terminal customisation: font size, colour scheme (dark/light/solarized), scrollback buffer size — persisted in a `settings.json`
6. Add a "Copy output" button in the status bar that copies the last N lines of the active terminal's output to clipboard
7. Add session logging to file (optional, toggleable per-tab) — writes timestamped terminal output to `logs/` directory
8. Add tab reordering via drag and drop
9. Add a right-click context menu on tabs: duplicate connection, rename tab, copy hostname, close, close others
10. Add a "Reconnect" option for disconnected tabs that re-establishes the same connection

## AI system prompt guidelines

The AI persona in ShellMate should be:

- A senior network engineer with deep Cisco IOS/IOS-XE/NX-OS/ASA expertise
- Aware that it can see the live terminal session — it should reference specific output when answering
- Proactive but not overbearing — it can flag obvious issues it spots (e.g., interface errors incrementing, BGP neighbour down) but shouldn't spam observations
- When suggesting commands, it should briefly explain WHY it's suggesting them
- It should understand context across the session — if the user has already run `show run` earlier, the AI should reference that config when answering later questions
- It should flag dangerous commands before suggesting them
- It should know common Cisco troubleshooting workflows and guide the user through them step by step

### Session context strategy

When sending context to the AI, include:
- The **active tab's** last 200 lines of terminal output (configurable) — this gives the AI the recent working context
- The active tab's full session buffer summary if the session is long (truncated intelligently)
- The current device hostname/prompt if detectable (parse the CLI prompt)
- Which commands have been run in the active session (parse from the buffer)
- A brief summary of ALL open sessions (tab number, device name, connection type) so the AI knows what's available
- If the user used `/context all`, include the last 100 lines from EVERY open session, clearly labelled by tab

The AI prompt should be structured as:
```
[System prompt — persona and rules]
[Open sessions summary — tab list with device names]
[Active session context — last N lines of terminal output from the active tab]
[Active session command history — list of commands run this session]
[Additional session context if /context all or /context N was used]
[User message]
```

## Key design decisions

### Why multi-tab from Phase 1?
Session management (creating, tracking, destroying connections by ID) is the backbone of the backend. If you build Phase 1 with a single global connection and then try to add tabs later, you have to retrofit session IDs into every WebSocket handler, every buffer call, and every API endpoint. It's far less work to build the `SessionManager` dictionary pattern from day one — even if the user only opens one tab at first, the architecture supports N tabs with zero refactoring.

### Why a local web app and not Electron?
Electron adds ~200MB of overhead and build complexity. A Python backend serving a local web page gives us the same result with tools Steven already knows. The browser IS the renderer. If we want to package it later, we can wrap it with something like PyInstaller + a tray icon.

### Why WebSockets and not plain HTTP?
Terminal sessions are bidirectional, continuous streams. HTTP request/response doesn't work for this — you'd be polling constantly. WebSockets give us a persistent open channel in both directions, which is exactly what a terminal needs. Every keystroke goes up, every character of output comes down, in real time.

### Why paramiko directly and not netmiko?
Netmiko is built for send-command-get-response automation. We need a raw interactive shell — the user is typing live, getting real-time output, seeing prompts, using tab completion. Paramiko's `invoke_shell()` gives us that raw channel. Netmiko would actually get in the way here by trying to detect prompts and parse output.

### Why session buffer in memory?
For v1, in-memory is fine. Each tab/session has its own buffer, keyed by session ID. Buffers get cleared when the tab is closed. This avoids file I/O complexity and permission issues. Phase 5 adds optional per-tab file logging for persistence.

## Frontend layout specification

```
┌──────────────────────────────────────────────────────────┐
│  ShellMate  [+ New Tab]  [Tab 1: switch01]  [Tab 2: ...]   │
├───────────────────────────────┬───────────┬──────────────│
│                               │ ◁ ▷ drag  │              │
│                               │           │  AI Chat     │
│   Terminal (xterm.js)         │           │              │
│                               │           │  [Claude ▼]  │
│   switch01#show ip int br     │           │              │
│   Interface  IP-Address  ... │           │  You: What   │
│   Gi0/1     10.1.1.1    up  │           │  does this   │
│   Gi0/2     unassigned  down │           │  output mean?│
│   ...                         │           │              │
│                               │           │  AI: I can   │
│                               │           │  see Gi0/2   │
│                               │           │  is down...  │
│                               │           │              │
│                               │           │  ┌──────────┐│
│                               │           │  │show run  ││
│                               │           │  │int Gi0/2 ││
│                               │           │  │[Send] [✎]││
│                               │           │  └──────────┘│
│                               │           │              │
│                               │           │  [Type here] │
├───────────────────────────────┴───────────┴──────────────┤
│  SSH: switch01 (10.1.1.1:22) | Connected | Buffer: 842L | Tabs: 3  │
└──────────────────────────────────────────────────────────┘
```

### Colour scheme

Use a dark terminal theme by default (dark background, light text) as network engineers expect this. The chat panel should use a slightly different background shade to visually distinguish it from the terminal. Use a colour palette inspired by modern terminal emulators:

- Terminal background: `#1e1e2e`
- Terminal text: `#cdd6f4`
- Chat panel background: `#181825`
- Chat panel text: `#cdd6f4`
- AI messages: slightly different background to user messages
- Command suggestion blocks: highlighted with a border, monospace font
- Status bar: darker shade at the bottom

## Error handling

- SSH connection failures: show clear error in terminal pane with the paramiko error message, offer to retry
- Serial port busy/unavailable: show which ports are available, suggest checking Device Manager
- AI backend unreachable: show error in chat panel, suggest checking API key / Ollama status, allow switching backends
- WebSocket disconnect: auto-reconnect with exponential backoff, show connection status indicator
- Session timeout: detect when the device closes the connection, notify user

## Security notes

- API keys come from the vault or `.env`, never from code or profiles
- Device passwords are not stored unless the user asks; when they are, they go
  to the encrypted vault (`backend/vault.py`) or, by explicit choice, the
  plaintext file — never to `profiles.json`. See *Secrets* above.
- The web server binds to `127.0.0.1` only — not accessible from other machines
- Serial connections are inherently local
- Session buffers are in-memory only and cleared on disconnect (unless logging is explicitly enabled)

## Development workflow

This project should be built using Claude Code. Steven will provide direction and testing on real network devices. The development machine is Windows with Python installed. Claude Code should:

1. Work through phases sequentially — do not jump ahead
2. Test each component as it's built — use print statements and clear error messages
3. Keep the code clean and well-commented — Steven is learning from this codebase
4. Use type hints in Python for clarity
5. Keep functions short and single-purpose with clear docstrings
6. When creating new files, explain what the file does and why it exists in a comment at the top
