# Broadcast and the local API

## Sending commands to several devices

Open **Broadcast** from the sidebar, or press `Ctrl+Shift+B`.

Tick the devices, write the commands, and send. Each device reports back
individually, so a session that was disconnected shows as a failure rather
than being quietly skipped.

Disconnected sessions start unticked and cannot be selected — they would fail
anyway, and a checkbox that does nothing is worse than one that is absent. The
tab you were looking at starts ticked.

**All** is a toggle: it selects every connected device, and pressing it again
clears them. The label follows what pressing it will do, so it is never
ambiguous which half you are about to get.

### The library

The commands worth broadcasting are usually the ones you send often, and a
broadcast is the worst possible place to be improvising. So the library holds
them, written once and checked: saving the configuration on each platform,
interface and error summaries, neighbours, logs, a health check.

Click one to load it. **Save to library** puts whatever is in the box back,
under a name.

Anything that changes the device is marked **writes**. Nothing destructive
ships in the library — `write erase` one mis-click away is not a default.

The library lives in `snippets.json` in your data folder, so you can edit it
in bulk or carry it between machines. Deleting a built-in keeps it deleted.

### Sequences

One command per line, sent in order, with a wait between them. That is what
makes save-and-verify a single action:

```
write memory
show startup-config | include ^Building
```

A device that has just written its configuration will not answer for a second
or two, so the wait is part of the task rather than a preference — snippets
carry their own.

Devices run **at the same time** and commands run **in order on each device**.
A two-second gap means two seconds, not two seconds multiplied by the number
of switches.

A block of configuration is just several lines. If one fails, that device
stops there rather than pressing on: the rest of a sequence rarely makes sense
once a step has failed, and continuing is how half-applied changes happen.

There is no progress bar. There does not need to be one — the commands appear
in the tabs as they land, which is a better view of what is happening than any
progress bar would be.

### Why it is not keystroke mirroring

Most terminals implement this by mirroring what you type into every open tab.
That is the wrong shape for a tool used on production kit:

- A stray keypress reaches the entire fleet.
- You never see the finished command before it lands.
- Devices with different prompts, or that autocomplete differently, diverge
  part-way through.

Here the commands are written once, and before anything is sent you confirm a
summary listing every command against every device by name. Slower by a
second, and much harder to regret.

Every command still passes through that session's normal outbound path, so
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
| `POST` | `/api/sessions/{id}/platform` | Say what the device is, when ShellMate could not be sure |
| `POST` | `/api/broadcast` | Send commands to several sessions |

Naming a platform yourself applies it immediately — aliases, reload patterns
and the paging command, if that setting is on. The response says what was
actually sent:

```
curl -X POST http://127.0.0.1:8765/api/sessions/{id}/platform \
  -H "Content-Type: application/json" \
  -d '{"platform":"nxos"}'
```

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

A sequence, with a wait between each command:

```
curl -X POST http://127.0.0.1:8765/api/broadcast \
  -H "Content-Type: application/json" \
  -d '{"session_ids":["...","..."],
       "commands":["write memory","show startup-config | include ^Building"],
       "wait_ms":2500}'
```

`command` also accepts a block with newlines in it, which is split into one
command per line. A whole sequence runs inside the one request and is
abandoned after 180 seconds, so a mistyped wait cannot hold the connection
open indefinitely.

### The command library

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/snippets` | The saved command library |
| `PUT` | `/api/snippets/{id}` | Create or update one (`new` to create) |
| `DELETE` | `/api/snippets/{id}` | Remove one |
| `POST` | `/api/snippets/reset` | Restore the shipped library |

### History and configuration

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/history/search?q=&hostname=&since=` | Search every recorded command |
| `GET` | `/api/history/sessions` | List recorded sessions |
| `GET` | `/api/history/sessions/{id}` | One session with all its commands |
| `GET` | `/api/configs/{hostname}` | Configuration snapshots for a device |
| `GET` | `/api/configs/snapshot/{id}` | One snapshot in full |
| `GET` | `/api/configs/diff/{old}/{new}` | Diff two snapshots |
| `GET` | `/api/configs/archive` | Where captures are filed, and what is there |
| `POST` | `/api/sessions/{id}/snapshot` | Capture the running config now |
| `GET` | `/api/sessions/{id}/drift` | What changed since the last visit |

Finding every time you touched a device:

```
curl "http://127.0.0.1:8765/api/history/search?hostname=core-sw-01&q=interface"
```

### Files

| Method | Path | Purpose |
|---|---|---|
On the device, over SFTP:

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/sftp/{id}/list?path=` | List a remote directory |
| `GET` | `/api/sftp/{id}/download?path=` | Download a file |
| `POST` | `/api/sftp/{id}/upload?path=` | Upload one |
| `DELETE` | `/api/sftp/{id}/file?path=` | Delete one |

On this machine, for the fields that need a local path — a private key, or
where captured configurations are filed:

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/pick-file` | Raise the platform's own file or folder dialog |
| `GET` | `/api/local/browse?path=` | List a local directory, for the in-app browser |

`/api/pick-file` answers `{"available": false}` when there is no desktop
window to raise a dialog from, which is not a failure — it is how the
interface knows to open its own browser instead. Neither endpoint reads file
*contents*; a browser deliberately withholds the path of a file you choose,
and these exist only to get one back.

### Configuration

| Method | Path | Purpose |
|---|---|---|
| `GET` `POST` | `/api/settings` | Read and write settings |
| `GET` | `/api/platforms` | Platform definitions |
| `PUT` | `/api/platforms/{id}` | Update one |
| `GET` | `/api/prompts` | The assistant's prompts, with their defaults |
| `PUT` | `/api/prompts/{mode}` | Replace one (`tshoot` or `learn`) |
| `POST` | `/api/prompts/reset` | Restore one, or both with no `mode` |
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
