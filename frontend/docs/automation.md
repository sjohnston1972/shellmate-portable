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
them, written once and checked.

It is **grouped by vendor**. Cross-vendor entries come first, then whatever
the devices you have selected are running, then everything else — visible but
collapsed, because wanting a Junos command while looking at a switch is a
perfectly reasonable thing to do.

Click one to load it. **Ctrl+click**, or the **+** button, adds it to what is
already in the box, so a sequence can be assembled from pieces that have
already been checked rather than typed fresh into something that sends to a
fleet. Appending takes the *longest* wait of the snippets involved: waiting
longer than necessary costs a second, waiting less than a device needs means
the next command arrives while it is still busy.

Searching ignores the grouping and shows everything that matches, labelled
with its vendor — the point of a search is finding something without knowing
which group it was filed under.

**Save to library** puts whatever is in the box back, under a name.

Anything that changes the device is marked **writes**. Nothing destructive
ships in the library — `write erase` one mis-click away is not a default.

#### Where the entries come from

Most of them are **generated from the alias table** in Platform Definitions.
`ints` is `show ip interface brief` on IOS, `show interfaces terse` on Junos
and `show interface all` on PAN-OS, and ShellMate already knew that — keeping
a second copy in the library would mean two sets of commands that eventually
disagree.

So correcting a command under **Settings → Platform Definitions** corrects it
in the library too, and a platform you add gets a library of its own with
nothing else to do.

The library lives in `snippets.json` in your data folder, so you can edit it
in bulk or carry it between machines. Deleting an entry keeps it deleted —
including when a later version ships new ones, which are added alongside
without bringing back anything you removed.

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

The rule is **loopback, or a token**. By default it listens on `127.0.0.1`
only and there is no authentication, because anything that can reach the port
is already running as you. Set the `SHELLMATE_AUTH_TOKEN` environment variable
(or put it in `.env`) and every request must prove it holds the token: a
browser gets a login page at `/login`, and `POST /api/login` exchanges the
token for a cookie that covers the API and the WebSockets alike — a script
does the same and sends the cookie back.

Bound wider than loopback with **no** token set, ShellMate refuses to start at
all. A warning in a log nobody reads is how an installation ends up exposed; a
hard failure at startup is how it does not.

The port is normally 8765, but ShellMate picks the next free one if that is
taken. `GET /api/system/info` confirms which.

### Sessions

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/sessions` | List open sessions |
| `POST` | `/api/sessions` | Open a connection |
| `POST` | `/api/sessions/{id}/disconnect` | End the connection, keep the session and its buffer |
| `DELETE` | `/api/sessions/{id}` | Close one, buffer and all |
| `POST` | `/api/sessions/{id}/platform` | Say what the device is, when ShellMate could not be sure |
| `POST` | `/api/tags/{tag}/connect` | Open a session to every connection carrying a tag |
| `POST` | `/api/broadcast` | Send commands to several sessions |

Disconnect and delete are different on purpose: disconnecting keeps the
transcript readable and Reconnect one click away — the same state a session
that dropped on its own leaves behind.

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
abandoned after three minutes (settable), so a mistyped wait cannot hold the
connection open indefinitely.

### The command library

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/snippets` | The saved command library |
| `GET` | `/api/snippets/quick` | The subset offered on a tab's right-click menu |
| `PUT` | `/api/snippets/{id}` | Create or update one (`new` to create) |
| `DELETE` | `/api/snippets/{id}` | Remove one |
| `POST` | `/api/snippets/reset` | Restore the shipped library |

### History and configuration

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/history/search?q=&hostname=&since=` | Search every recorded command |
| `GET` | `/api/history/sessions` | List recorded sessions |
| `GET` | `/api/history/sessions/{id}` | One session with all its commands |
| `DELETE` | `/api/history` | Clear history — all of it, one device's, or older than N days |
| `GET` | `/api/configs/{hostname}` | Configuration snapshots for a device |
| `GET` | `/api/configs/snapshot/{id}` | One snapshot in full |
| `GET` | `/api/configs/diff/{old}/{new}` | Diff two snapshots |
| `GET` | `/api/configs/baseline/{hostname}` | The pinned baseline, if one is set |
| `POST` | `/api/configs/baseline` | Pin a snapshot as what the device should match |
| `DELETE` | `/api/configs/baseline/{hostname}` | Unpin, back to comparing against the last visit |
| `GET` | `/api/configs/archive` | Where captures are filed, and what is there |
| `POST` | `/api/sessions/{id}/snapshot` | Capture the running config now |
| `GET` | `/api/sessions/{id}/drift` | What changed since the last visit |

Finding every time you touched a device:

```
curl "http://127.0.0.1:8765/api/history/search?hostname=core-sw-01&q=interface"
```

### Saved connections and groups

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/profiles` | Saved connections |
| `POST` | `/api/profiles` | Save one — secrets are stripped, whatever you pass |
| `DELETE` | `/api/profiles/{id}` | Delete one, and forget its password |
| `GET` | `/api/profiles/tags` | Every tag in use, with a count |
| `PUT` | `/api/profiles/{id}/tags` | Replace a connection's tags |
| `GET` | `/api/groups` | Every group on the dashboard |
| `POST` | `/api/groups` | Create a group, or adopt an existing tag as one |
| `PUT` | `/api/groups/{key}` | Rename, recolour, favourite or reposition one |
| `PUT` | `/api/groups/order` | Store the dashboard arrangement |
| `POST` | `/api/groups/{key}/members` | Add a connection to a group, or remove it |
| `DELETE` | `/api/groups/{key}` | Remove a group — the connections in it survive |
| `GET` | `/api/credential-sets` | Named shared credentials, and how many connections use each. No values |
| `POST` | `/api/credential-sets` | Create or update one |
| `DELETE` | `/api/credential-sets/{id}` | Forget one, detaching every connection using it |

### Finding devices

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/discovery/subnets` | The subnets this machine is on |
| `POST` | `/api/discovery/preview` | How many addresses a target comes to, without scanning |
| `POST` | `/api/discovery/scans` | Start a sweep — returns at once |
| `GET` | `/api/discovery/scans/{id}` | Progress and results so far |
| `POST` | `/api/discovery/scans/{id}/cancel` | Stop a sweep |
| `DELETE` | `/api/discovery/scans/{id}` | Discard a finished one |
| `POST` | `/api/discovery/save` | Save discovered devices as connections |

### Files and logs

On the device, over SFTP:

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/sftp/{id}/list?path=` | List a remote directory |
| `GET` | `/api/sftp/{id}/download?path=` | Download a file |
| `POST` | `/api/sftp/{id}/upload?path=` | Upload one |
| `DELETE` | `/api/sftp/{id}/file?path=` | Delete one |
| `POST` | `/api/sftp/{id}/rename` | `{path, new_path}` — rename or move |
| `POST` | `/api/sftp/{id}/mkdir` | `{path}` — create a directory |
| `POST` | `/api/sftp/{id}/chmod` | `{path, mode}` — octal mode such as `644` |
| `DELETE` | `/api/sftp/{id}/directory?path=` | Delete a directory and everything beneath it |
| `GET` | `/api/sftp/{id}/download-directory?path=` | A directory as a zip |

On this machine, for the fields that need a local path — a private key, or
where captured configurations are filed:

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/pick-file` | Raise the platform's own file or folder dialog |
| `GET` | `/api/local/browse?path=` | List a local directory, for the in-app browser |
| `GET` | `/api/logs` | Session log files ShellMate has written |
| `GET` | `/api/logs/{filename}` | Download one |

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
| `GET` | `/api/advanced` | Every advanced setting, its default and its bounds |
| `POST` | `/api/advanced` | Change some. Values are clamped server-side |
| `POST` | `/api/advanced/reset` | Reset one, one category, or all |
| `GET` | `/api/schemes` | Terminal colour schemes, built-in and custom |
| `POST` | `/api/schemes` | Save a custom scheme |
| `DELETE` | `/api/schemes/{name}` | Remove one — built-ins can only be shadowed |
| `GET` | `/api/keys` | SSH keys ShellMate is looking after |
| `POST` | `/api/keys` | Generate one |
| `POST` | `/api/keys/import` | Copy an existing key in |
| `POST` | `/api/keys/passphrase` | Add, change or remove one |
| `POST` | `/api/keys/delete` | Remove a key |
| `GET` | `/api/support/sections` | What can go in a diagnostic bundle |
| `POST` | `/api/support/preview` | Gather sections without writing anything |
| `POST` | `/api/support/bundle` | Write the chosen sections as one zip |
| `GET` | `/api/prompts` | The assistant's prompts, with their defaults |
| `PUT` | `/api/prompts/{mode}` | Replace one (`tshoot`, `learn` or `investigate`) |
| `POST` | `/api/prompts/reset` | Restore one, or all three with no `mode` |
| `GET` | `/api/serial/ports` | Serial ports on this machine |
| `GET` | `/api/restart` | Whether ShellMate can relaunch itself, and what is still connected |
| `POST` | `/api/restart` | Start a fresh copy and stop this one |

### Tunnels, pushes and backups

Added in 1.0, all against a live session or a group:

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/sessions/{id}/forwards` | The port forwards a session holds, and the limit |
| `POST` | `/api/sessions/{id}/forwards` | `{kind, listen_port, host, port, remember}` — start one |
| `DELETE` | `/api/sessions/{id}/forwards/{fid}?forget=` | Stop one; `forget=true` also drops it from the profile |
| `PUT` | `/api/profiles/{id}/forwards` | Replace the forwards a saved connection starts with |
| `POST` | `/api/configs/{id}/preview` | `{text, fresh}` — what applying the lines would change; sends nothing |
| `POST` | `/api/configs/{id}/apply` | `{text, save, force}` — send them, capture before and after, return the diff |
| `GET` | `/api/configs/{id}/restore/{snapshot}` | A proposed change back to an earlier capture, as text |
| `PUT` | `/api/groups/{key}` with `backup` | `{enabled, every, at, day}` — the group's backup schedule |
| `POST` | `/api/groups/{key}/backup/run` | Back the group up now; returns what happened per device |
| `GET` | `/api/system/update` | Compare this build with the latest GitHub release |

`POST /api/sessions` answers **409** with `{"detail": {"interactive": …}}`
when the device asks a keyboard-interactive question the password cannot
answer; post again with `interactive_answers` in prompt order. The chat
WebSocket accepts `history` (earlier turns) and `investigate_step`, and
sends a `usage` message with the provider's token counts after each reply.

### AI providers and Jira

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/providers/models` | Ask every configured provider what models it offers |
| `GET` | `/api/providers/{provider}/test` | Test one provider |
| `GET` | `/api/providers/cached` | The model list as discovered last time, no network |
| `POST` | `/api/ai/session-summary` | An AI-written summary of the open sessions and chat |
| `GET` | `/api/jira/config` | Whether Jira is configured, and the project key |
| `GET` | `/api/jira/search` | Search issues in the configured project |
| `GET` | `/api/jira/issue-types` | Issue types available there |
| `POST` | `/api/jira/session` | Write the session up as a Jira issue or comment |

A complete, always-current reference is served at
`http://127.0.0.1:8765/docs` while ShellMate is running — it is generated
from the code, so it cannot drift from what the server actually does.

### What the API will not give you

No endpoint returns a stored password, an API key, or the private half of an
SSH key. Connection listings carry a
flag saying whether a password is saved, never the password itself. This
holds for scripts exactly as it does for the interface.
