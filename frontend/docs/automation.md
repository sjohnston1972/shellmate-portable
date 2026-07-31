# Broadcast and the local API

## Sending one command to several devices

Open **Broadcast** from the sidebar, or press `Ctrl+Shift+B`.

Tick the devices, type the command, and send. Each device reports back
individually, so a session that was disconnected shows as a failure rather
than being quietly skipped.

Disconnected sessions start unticked and cannot be selected — they would fail
anyway, and a checkbox that does nothing is worse than one that is absent.

### Why it is not keystroke mirroring

Most terminals implement this by mirroring what you type into every open tab.
That is the wrong shape for a tool used on production kit:

- A stray keypress reaches the entire fleet.
- You never see the finished command before it lands.
- Devices with different prompts, or that autocomplete differently, diverge
  part-way through.

Here the command is written once, the targets are listed by name, and you
confirm a summary before anything is sent. Slower by a second, and much
harder to regret.

Each command still passes through that session's normal outbound path, so
alias expansion applies exactly as it would if you had typed it.

## The local API

ShellMate is a local web server, and everything the interface does goes
through an HTTP API you can drive yourself — for scripting, for integration,
or just to check something quickly with `curl`.

It listens on `127.0.0.1` only. It is not reachable from the network, and
there is no authentication, because anything that can reach the port is
already running as you.

The port is normally 8765, but ShellMate picks the next free one if that is
taken. `GET /api/system/info` confirms which.

### Sessions

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/sessions` | List open sessions |
| `POST` | `/api/sessions` | Open a connection |
| `DELETE` | `/api/sessions/{id}` | Close one |
| `POST` | `/api/broadcast` | Send a command to several sessions |

Opening an SSH session:

```
curl -X POST http://127.0.0.1:8765/api/sessions \
  -H "Content-Type: application/json" \
  -d '{"connection_type":"ssh","hostname":"10.20.30.40",
       "username":"neteng","password":"...","display_label":"core-a"}'
```

Sending a command to several at once:

```
curl -X POST http://127.0.0.1:8765/api/broadcast \
  -H "Content-Type: application/json" \
  -d '{"session_ids":["...","..."],"command":"show version"}'
```

### History and configuration

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/history/search?q=&hostname=&since=` | Search every recorded command |
| `GET` | `/api/history/sessions` | List recorded sessions |
| `GET` | `/api/history/sessions/{id}` | One session with all its commands |
| `GET` | `/api/configs/{hostname}` | Configuration snapshots for a device |
| `GET` | `/api/configs/diff/{old}/{new}` | Diff two snapshots |
| `POST` | `/api/sessions/{id}/snapshot` | Capture the running config now |
| `GET` | `/api/sessions/{id}/drift` | What changed since the last visit |

Finding every time you touched a device:

```
curl "http://127.0.0.1:8765/api/history/search?hostname=core-sw-01&q=interface"
```

### Files

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/sftp/{id}/list?path=` | List a remote directory |
| `GET` | `/api/sftp/{id}/download?path=` | Download a file |
| `POST` | `/api/sftp/{id}/upload?path=` | Upload one |
| `DELETE` | `/api/sftp/{id}/file?path=` | Delete one |

### Configuration

| Method | Path | Purpose |
|---|---|---|
| `GET` `POST` | `/api/settings` | Read and write settings |
| `GET` | `/api/platforms` | Platform definitions |
| `PUT` | `/api/platforms/{id}` | Update one |
| `GET` | `/api/profiles` | Saved connections |
| `GET` | `/api/serial/ports` | Serial ports on this machine |
| `GET` | `/api/providers/models` | Test AI providers and list models |

A complete, always-current reference is served at
`http://127.0.0.1:8765/docs` while ShellMate is running — it is generated
from the code, so it cannot drift from what the server actually does.

### What the API will not give you

No endpoint returns a stored password or API key. Connection listings carry a
flag saying whether a password is saved, never the password itself. This
holds for scripts exactly as it does for the interface.
