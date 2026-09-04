# Settings

Open **Settings** from the sidebar. It is one panel: a rail of sections down
the side, each with its own icon, and a search box above it — if you know
roughly what a setting is called, type it and every matching row across every
section appears, whichever one it lives under.

Several rows carry a **?** beside the label. That is where the trade-off is
written — what the setting does is usually clear from its name, what you give
up by turning it on is not. `Copy on Select` explains itself; that it will
replace your clipboard every time you drag across the terminal does not.

Everything is stored in `settings.json` in your data folder, and nothing
sensitive goes in it — API keys are diverted into the vault before the file is
written.

## The sections

In the order the rail lists them:

| Section | What lives there |
|---|---|
| Terminal Appearance | Font, size, colour scheme, cursor, and a live preview |
| ShellMate Interface | Theme, text size, density, tab width, how panels open, confirmations, window behaviour, whether the AI panel is shown — plus the file-transfer and panel-animation rows |
| Terminal Behaviour | Scrollback, right-click paste, copy on select, screen-reader mode — plus rendering, paste protection and the destructive-command confirmation |
| Output Colours | Regular expressions that colour terminal text |
| Device Awareness | Paging-off and alias expansion, both gated on confident identification — plus the identification timings and threshold |
| Platform Definitions | What ShellMate knows about each kind of device |
| SSH & Serial | Baud rate and framing for new serial connections — plus SSH timeouts, keepalive and the algorithm lists |
| Credentials Vault | How saved passwords and API keys are encrypted, and shared credentials |
| Session Logging | Plain-text logs, whether secrets are masked in them — plus what the history database records |
| Configuration Capture | Snapshots on connect, the diff prompt, and the file archive — plus capture timing and limits |
| AI Providers | API keys, endpoints, the knowledge base, and the connection test |
| AI Assistant | Temperature, context sizes, suggestion behaviour — and the system prompts themselves |
| Alerts | How loudly a pending reload or commit-confirm interrupts you — plus the timing and thresholds behind it |
| Broadcast | Timeouts, concurrency and error handling for commands sent to many devices |
| Network Discovery | How the scanner probes, and how hard |
| Diagnostics | Where this installation lives, what it has recorded, and the tools for when something goes wrong |

## Two kinds of row

Most rows are ordinary settings: change what you like, and nothing happens
until **Save Settings** at the bottom of the panel.

The rest — about eighty-three values across eleven areas, which used to be
constants in the source — behave differently, and each area says so where it
appears: **they apply as soon as you change them.** Timeouts, thresholds,
buffer sizes, retry counts. Most render as a subsection inside the section
they belong with; three — AI Assistant, Broadcast, Network Discovery — are
sections of their own.

Every one has a sensible default and a range it is held to, checked on the
server rather than by the input box — so nothing there can stop ShellMate
starting or send a device something wrong. The worst you can do is make it
work less well. Anything you move off its default is marked, with the default
and range printed beneath it, and a **Reset** button appears on that row.

Some values are deliberately not settings at all — the vault's key-derivation
parameters, the broadcast confirmation, the loopback-only binding. Each of
them could break something that matters rather than merely degrade it.

The ones people reach for most:

| Setting | Why |
|---|---|
| SSH keepalive | Firewalls and jump hosts idle a session out mid-change. Off by default; 30–60 seconds suits most. Under SSH & Serial |
| Key exchange, ciphers, MACs, host keys | Tick-lists of what paramiko will negotiate. Very old kit offers only algorithms that have been dropped from the defaults, which makes it unreachable otherwise. Legacy entries are marked |
| Confidence needed to act | A single-vendor estate can safely act on weaker identification. Floored at 0.4 — never zero. Under Device Awareness |
| Terminal lines sent as context | More context means better answers from the assistant and more tokens. Under AI Assistant |
| The system prompts | What the assistant is told before it sees anything of yours. Under AI Assistant |
| Log level | `DEBUG` is what support will ask for. Under Diagnostics |

### When a change needs a restart

Almost none of them do. Every value is read at the point it is used rather
than when ShellMate starts, so a change lands in the next connection, the next
capture, the next request.

Exactly two are fixed when the server starts: **Log every HTTP request** and
**Ports to try if 8765 is busy**, both under Diagnostics. Each carries a
*needs a restart* tag with a **Restart now** button beside it, greyed until a
setting that actually needs one has been changed — a live restart button
beside an untouched setting is an invitation to drop every session for
nothing.

Clicking it first asks whether this build can relaunch itself; where it
cannot, it says to quit from the tray and start ShellMate again rather than
pretending. Then it names every device still connected before doing anything,
because a restart really does drop them — closing the window deliberately
does not, and the two should not be confused. Once confirmed, the page waits
for the replacement to answer and reloads into it.

### If you get stuck

Run ShellMate once with `--reset-advanced`, or delete the `advanced` section
from `settings.json`. The way back does not depend on the interface, because
the interface is what might be in the way.

## The ones worth knowing about

### The assistant is off until you ask for it

A fresh install opens with the terminal at full width. The assistant is
optional, on a locked-down network there may be no provider to reach at all,
and a third of the window given to a pane that cannot answer anything is a
poor first impression.

The robot icon in the tab bar turns it on and off. It is the same setting as
**ShellMate Interface → Show the AI panel**, not a second one that can
disagree with it. It sits under ShellMate Interface rather than with the AI
settings because it decides the shape of the window, not how the assistant
behaves.

An existing installation is left exactly as it was. A default is only a
default for a setup that has never been configured — changing one under
somebody who has been using the feature for months would be a change they did
not make and cannot explain.

### Device Awareness depends on confidence

Both switches in that section — paging-off and alias expansion — do nothing
unless the device was identified *confidently*. That is the most important
thing about the section, and it is common for it not to be met: a legal banner
in place of a version string leaves ShellMate identifying the device from its
prompt alone, which is not enough to act on.

When that happens the note in the corner of the terminal says so, and names
the command it declined to send. Click the device name in the status bar to
say what it is yourself.

See [Device awareness](#device-awareness).

### Redaction covers more than session logs

**Obscure passwords and secrets**, under Session Logging, applies to captured
configurations as well. One switch, one promise — two switches for the same
guarantee is a guarantee nobody can rely on.

### Window size is remembered by having one

**Remember window size and position** is expressed as "is a size stored",
because that is what it means. Unticking it clears what was stored, so the
next launch opens at the default size rather than silently restoring a stale
one.

## Settings added in 1.0

All in Stockton unless said otherwise, each bounded and explained on its row.

| Setting | Where | What it governs |
|---|---|---|
| Earlier turns the assistant remembers | AI Assistant | How many exchanges travel with each request; zero is no memory |
| Cache the conversation prefix | AI Assistant | Claude's prompt caching on the stable part of the request |
| Lines sent for each extra session | AI Assistant | Output from the other tabs in `/context all` or the picker |
| Parse show output into tables | AI Assistant | Rows beside the raw text where a template exists |
| Investigation step budget | AI Assistant | Approved commands before Investigate mode has to conclude |
| Ollama context window / Keep the local model loaded | AI Assistant | The two knobs a local model has that a cloud one does not |
| Lines the Copy output button takes | Terminal Behaviour | The status-bar copy button |
| Port forwards per session | SSH | The ceiling on tunnels through one session |
| Delay between pushed config lines | Capture | Pacing when applying configuration |
| Most entries a folder transfer may touch | Files and panels | The ceiling on a folder download or delete |
| Check for a newer release at startup | Diagnostics | Off by default; the button under Diagnostics does the same on demand |

The tab menu's toggles under ShellMate Interface gained **Rename tab**,
**Port forwards** and **Apply configuration**, so any of them can be hidden
from the menu on a machine where it should not be offered.

## Licence

ShellMate works without a licence. What a licence buys is **updating from
inside the application**: with a key installed, a newer release can be
downloaded, verified and swapped in from the update window; without one, the
window still shows what the new version contains and where to fetch it by
hand.

A key is a line beginning `SM1.` — paste it into **Settings → Licence**, or
import the `.key` file it was sent as. Keys come by email from Foundry
Networks and Services; the Licence section links to the request page. It is verified on this machine against
a public key built into ShellMate, so an air-gapped installation is licensed
exactly like a connected one; nothing about your devices is sent anywhere,
and the key lives in `licence.key` in the data folder, never in
`settings.json`.

The section shows who the key is for, whether it is a personal or an
organisation key and with how many seats, when it expires, and what it
covers. **Refresh now** asks the licence service about the key — a renewal
arrives as a fresh key with no re-entry, and a revocation is learned the
same way. A key past its expiry is still honoured for its **grace period**
while a renewal is confirmed, and the status says so.

What the service is told is the key's id and where the key is installed:
this machine's name, your user name, the operating system and the ShellMate
version, so whoever holds an organisation licence can see which of its
seats are in use and which copies are behind. That is sent when the key is
entered, when it is removed, and at each refresh. Nothing about the devices
you connect to, your sessions or your settings ever goes with it.

## Updates

ShellMate checks for a newer release shortly after it starts — one request
carrying the version number, silent when nothing is new, and harmless with
no internet. The check can be switched off under Stockton → Diagnostics for
an air-gapped site; **Updates** in the sidebar, **Check for updates** in the
tray menu and the button under Diagnostics run it on demand.

**Update channel**, under Stockton → Diagnostics, decides which releases
the check may offer. `stable`, the default, offers only full releases.
`beta` also offers prereleases, tagged like `v1.2.0-beta.1`, for the few
people trying a build before it goes to everyone; the window marks such a
version **Beta**. The download, the checksum and the swap are the same on
both channels. **What's new in this version**, beside the check button,
reopens the notes for the copy you are running.

A newer version opens ShellMate's own window: the version, when it was
published, its size, the release notes, and **Update now**, **Later** (asks
again tomorrow) or **Skip this version**. Update now downloads the
executable into `ShellMate-Data/updates/` with a progress bar and verifies
it against the checksum published with the release; nothing is executed
until that matches. **Restart into the new version** then closes ShellMate,
swaps the file, and starts the new copy — and puts the old one back if the
new one does not come up. Live sessions are closed by the swap, and it is
refused while a device has a reload or a commit-confirm pending.

The first launch of a new version opens a **what's new** window with that
version's notes; the same text is on the What's new page of this manual.

## Diagnostics

**Version** names the release, the commit it was built from and when — the
three things to quote in a bug report, and the way to tell whether the copy
you double-clicked is the one you updated. A portable executable can be
left behind anywhere, and nothing about the file says it is stale.

**Check for updates** asks GitHub whether a newer release exists and, if so,
links to it. Nothing is downloaded and nothing about your devices is sent:
the request carries the version number. On a machine with no internet it
says GitHub could not be reached, and that is all. The same check can run
once at startup — off by default — from **Stockton → Diagnostics → Check
for a newer release at startup**.

The last section answers the questions a fault report starts with, without
leaving the interface:

- Which build this is, and whether it is the portable one.
- Where the data folder is — and whether the fallback location is in use
  because the folder beside the executable was not writable.
- Where the application log is. It is truncated on each launch, so what is in
  it is current.
- What the history database holds — sessions, commands and config snapshots —
  and which search engine it is using: FTS5, or the plain LIKE fallback where
  the bundled SQLite lacks it.

Two buttons sit alongside: one opens the per-session logs panel, the other
builds a support bundle — see [Troubleshooting](#troubleshooting) for what
goes into one. Below them, the **Logging and startup** rows: the log level and
the two restart-only settings described above.

## Resetting

There is no single global reset. Each area has its own, because they fail
independently:

- **Platform Definitions → Reset all to defaults**, or delete `platforms.json`.
- **Output Colours → Reset to defaults**.
- The command library: `POST /api/snippets/reset`, or delete `snippets.json`.
- The system prompts: the reset buttons under **AI Assistant**, or delete `prompts.json`.
- Any changed advanced row: the **Reset** button that appears on it.
- Every advanced setting at once: run once with `--reset-advanced`, or delete the `advanced` section from `settings.json`.
- Everything else: delete `settings.json`. ShellMate starts as if new.

Deleting the whole `ShellMate-Data` folder resets everything, including your
saved connections and session history.

## How panels open

**ShellMate Interface → Panels open with** decides how Settings, History and
the other side panels arrive: **slide** in from the edge they are anchored to,
**fade**, **scale**, or **nothing** — appear instantly.

Slide is the default because it says which edge the panel came from, and
therefore where Escape will send it back to.

**Nothing** is a real answer rather than a joke one. Over Remote Desktop or a
VDI session, an animation that drops frames is worse than no animation, and
the panels carry a blurred backdrop that is the expensive part of drawing
them.

Duration and pacing are in the same section, among the advanced rows —
separate values for opening and closing, because a panel generally wants to
leave faster than it arrived, and one value for both makes dismissing
something feel slow.

If your operating system is set to reduce motion, that wins over this setting.
You have already answered the question and ShellMate should not ask again.
