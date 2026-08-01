# Settings

Open **Settings** from the sidebar. It is a category list with a search box:
if you know roughly what a setting is called, type it and every matching row
across every category appears, whichever one it lives under.

Several rows carry a **?** beside the label. That is where the trade-off is
written — what the setting does is usually clear from its name, what you give
up by turning it on is not. `Copy on Select` explains itself; that it will
replace your clipboard every time you drag across the terminal does not.

Everything is stored in `settings.json` in your data folder, and nothing
sensitive goes in it — API keys are diverted into the vault before the file is
written.

## The categories

| Category | What lives there |
|---|---|
| Terminal Appearance | Font, size, colour scheme, cursor, and a live preview |
| ShellMate Interface | Theme, text size, density, tab width, confirmations, window behaviour, whether the AI panel is shown |
| Alerts | How loudly a pending reload or commit-confirm interrupts you |
| Behavior | Scrollback, right-click paste, copy on select |
| Device Awareness | Paging-off and alias expansion, both gated on confident identification |
| Platform Definitions | What ShellMate knows about each kind of device |
| Serial Defaults | Baud rate and framing for new serial connections |
| Output Colours | Regular expressions that colour terminal text |
| Credentials Vault | How saved passwords and API keys are encrypted |
| AI Providers | API keys, endpoints, and the connection test |
| Knowledge Base | An optional Chroma collection of your own guidelines |
| Session Logging | Plain-text logs, and whether secrets are masked in them |
| Configuration Capture | Snapshots on connect, the diff prompt, and the file archive |

## The ones worth knowing about

### The assistant is off until you ask for it

A fresh install opens with the terminal at full width. The assistant is
optional, on a locked-down network there may be no provider to reach at all,
and a third of the window given to a pane that cannot answer anything is a
poor first impression.

The robot icon in the sidebar turns it on and off. It is the same setting as
**ShellMate Interface → Show the AI panel**, not a second one that can disagree with it.
It sits under ShellMate Interface rather than with the AI settings because it decides
the shape of the window, not how the assistant behaves.

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

## Stockton — settings for the tinkerer

The **tune** icon in the sidebar opens fifty-odd values that govern how
ShellMate behaves and were, until now, constants in the source. Timeouts,
thresholds, buffer sizes, retry counts.

Every one has a sensible default and a range it is held to, checked on the
server rather than by the input box — so nothing there can stop ShellMate
starting or send a device something wrong. The worst you can do is make it work
less well.

Anything you change is marked, with its default and range printed beneath it,
and **Reset** is available on every row, every section, and the whole panel.
Searching ordinary Settings finds advanced settings too, badged **Stockton**,
so looking for something does not depend on knowing which panel it is in.

The ones people reach for most:

| Setting | Why |
|---|---|
| SSH keepalive | Firewalls and jump hosts idle a session out mid-change. Off by default; 30–60 seconds suits most |
| Key exchange, ciphers, MACs, host keys | Tick-lists of what paramiko will negotiate. Very old kit offers only algorithms that have been dropped from the defaults, which makes it unreachable otherwise. Legacy entries are marked |
| Confidence needed to act | A single-vendor estate can safely act on weaker identification. Floored at 0.4 — never zero |
| Terminal lines sent as context | More context means better answers from the assistant and more tokens |
| The system prompts | What the assistant is told before it sees anything of yours |
| Log level | `DEBUG` is what support will ask for |

Some things are deliberately **not** there, and the panel lists them with the
reason: the vault's key-derivation parameters, the broadcast confirmation, the
loopback-only binding. Each of them could break something that matters rather
than merely degrade it.

### When a change needs a restart

Almost none of them do. Fifty-four of the fifty-seven take effect immediately,
because they are read at the point they are used rather than when ShellMate
starts. One — the terminal renderer — applies to the next tab you open, and
says so on its row.

Only two are fixed when the server starts: HTTP access logging and the port
scan range. Change either and a **Restart now** button appears in the footer.
It names every device still connected before it does anything, because a
restart really does drop them — closing the window deliberately does not, and
the two should not be confused.

Where ShellMate cannot relaunch itself, the footer says to quit from the tray
and start it again rather than showing a button that would do nothing.

### If you get stuck

Run ShellMate once with `--reset-advanced`, or delete the `advanced` section
from `settings.json`. The way back does not depend on the interface, because
the interface is what might be in the way.

## Resetting

There is no global reset. Each area has its own, because they fail
independently:

- **Platform Definitions → Reset all to defaults**, or delete `platforms.json`.
- **Output Colours → Reset to defaults**.
- The command library: `POST /api/snippets/reset`, or delete `snippets.json`.
- **Stockton → AI assistant → Reset**, or delete `prompts.json`.
- **Stockton → Reset everything**, or run once with `--reset-advanced`.
- Everything else: delete `settings.json`. ShellMate starts as if new.

Deleting the whole `ShellMate-Data` folder resets everything, including your
saved connections and session history.
