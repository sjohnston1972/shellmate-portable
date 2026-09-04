# Device awareness

ShellMate works out what it is connected to and adapts, rather than assuming
Cisco and hoping.

## How a device is identified

Three sources, cheapest first.

**The login banner.** Free — the device has already sent it. Cisco IOS
announces itself, Junos prints its version, PAN-OS names itself. Enough on its
own most of the time.

**The prompt.** Weaker but always present. `user@host>` is Junos or PAN-OS,
`host(config-if)#` is Cisco-like, `host:~$` is a shell. Used when the banner
says nothing, which is common on devices configured with a legal warning
instead of a version string.

**A version command.** Definitive, but costs a round trip. Used to confirm,
never as the first resort.

## The confidence gate

Every result carries a **confidence**, and it gates the one thing ShellMate
sends to a device unasked: the paging-off command. Acting on a weak guess is
how a tool ends up sending `terminal length 0` to a firewall, so below a
threshold ShellMate identifies the device and then deliberately withholds the
command.

The gate covers exactly that — what gets *sent*. Aliases switch on with the
identification itself, confident or not, because an expansion is shown to you
as it happens rather than sent behind your back.

This matters more than it sounds, because the gate is met less often than you
would expect. A banner naming the software is conclusive. A prompt on its own
usually is not:

| Prompt | Read as | Enough to act on? |
|---|---|---|
| `hostname(config)#` | Cisco IOS | No |
| `user@host>` | Junos | No |
| `host(active)#` | PAN-OS | No |
| `host:~$` | a shell | Yes |
| `host/pri/act#` | Cisco ASA | Yes |

`hostname(config)#` looks decisive, and in a sense it is — nothing else prints
it. But it says the device is *Cisco-shaped*, and IOS, NX-OS, ASA and Arista
EOS all print it identically. The command that would be sent belongs to the
platform, not the family: `terminal length 0` is right on three of those and
wrong on the ASA. A prompt that narrows the field to four vendors is not the
same as one that picks the platform.

So a device with a legal banner instead of a version string — extremely
common — or anything reached through a terminal server is identified from its
prompt, scores below the bar, and is sent nothing at all.

### When that happens

The note in the corner of the terminal says so, and names the command it did
not send:

> identified Cisco IOS / IOS-XE from its prompt alone — not confident enough
> to send "terminal length 0", so paging is still on

It carries a **Set platform** button, and then fades — the offer's permanent
home is the **Device** chip in the status bar, which reads `(unconfirmed)`
rather than showing the device as known. Hovering the chip gives the
confidence, where the identification came from, and what that means.

### Telling it yourself

Click the **Device** chip in the status bar and choose the platform — it
works even from the dashboard, where the chip still describes the last
session shown. That is the one source that is not a guess, so it carries full
confidence: aliases switch over immediately and the paging command is sent,
if that setting is on. What was sent is announced exactly as it would have
been automatically.

The choice applies to that session — and is remembered against the saved
connection, see below.

## What it does with that

### Turns paging off

`terminal length 0` on IOS and NX-OS, `terminal pager 0` on ASA,
`set cli screen-length 0` on Junos, `set cli pager off` on PAN-OS.

The command is echoed into the terminal exactly as if you had typed it, and
the interface says what was sent. Nothing is typed into your session
silently — you may have to account for what happened in that session later.

A device that cannot be identified confidently gets **nothing**. A wrong
command is worse than a command not sent.

Turn it off under **Settings → Device Awareness**. Turned off, the note in the
corner says the setting is off rather than implying the device refused —
"nothing was sent" has several possible causes and they are not
interchangeable.

### Sends the connection's own lines

A saved connection can carry an **on-connect script**: the four or five
commands you type on every device before you start work. `enable`,
`terminal monitor`, a screen width, entering a VDOM or a context. See
*On-connect commands* on the [Connecting](#connecting) page.

It runs after paging-off, under the same rules — one line per prompt, each
echoed into the session, the whole list announced before anything goes out,
and anything destructive still held for confirmation.

It is also the honest answer for the platforms where paging-off is per user
or per context and cannot be a platform default: FortiOS and MikroTik have
nothing correct for ShellMate to send, and a connection does.

### Picks the right commands

Retrieving a running configuration is `show running-config` on IOS,
`show configuration | display set` on Junos, and `show config running` on
PAN-OS. Configuration snapshots and drift detection use whichever applies.
The destructive-command list is per-platform for the same reason.

### Enables aliases

See below.

## Aliases

Type `ints` and get the right command for whatever you are connected to:

| Alias | IOS | Junos | PAN-OS |
|---|---|---|---|
| `ints` | `show ip interface brief` | `show interfaces terse` | `show interface all` |
| `routes` | `show ip route` | `show route` | `show routing route` |
| `bgp` | `show ip bgp summary` | `show bgp summary` | `show routing protocol bgp summary` |
| `cpu` | `show processes cpu sorted` | `show chassis routing-engine` | `show system resources` |
| `log` | `show logging` | `show log messages \| last 100` | `show log system` |

Around forty aliases ship for IOS and NX-OS, thirty-five for Junos and
Arista, and proportionate sets for ASA, PAN-OS and Linux. The same short name
means the same *intent* on every platform, which is the whole point in a
mixed estate.

Two rules keep this safe:

- **Only a bare alias on its own is expanded.** `ints` becomes the interface
  command; `show ints` is left exactly as typed, because you have clearly
  written a real command and silently rewriting the middle of it would be
  worse than not helping.
- **The terminal shows what was actually sent.** You see your `ints`, then
  you see the real command replace it, and a note says so.

Nothing is expanded until the device has been identified — an unidentified
device has no alias table, so nothing at all is expanded on one. An
identification below the confidence bar is still an identification: aliases
work there, with every expansion shown as it happens.

Turn expansion off under **Settings → Device Awareness**. The same section
also holds **Keep sessions alive** — a nudge typed at an idle prompt so
`exec-timeout` never closes the tab you left over lunch — which sits there
because, like everything on this page, it types into your session and is
never done silently.

## Editing what ShellMate knows

**Settings → Platform Definitions** edits everything above: the paging
command, the config command, the text used to identify the platform, the
commands that will need confirming, and the alias table. It is also where a
device ShellMate has never seen gets taught.

The editor writes `platforms.json` in your data folder — the same file, so you
can also edit it in a text editor for bulk changes, keep it in version
control, or carry your definitions to another machine.

If you keep having to identify a device by hand, this is the fix: add
something from its banner to that platform's **Identify by** list. Even a
hostname convention will do, as long as it is distinctive — the longest match
wins, so be specific.

### Adding a platform

Add an entry to `platforms.json` with an `id`, a `name`, and at minimum a
`signatures` list so it can be recognised. Everything else is optional.

### The configuration commands

Three fields say how *Apply configuration* talks to a platform:
`config_enter` (what opens configuration mode), `config_exit` (what leaves
it, committing where the platform commits on exit) and `save_command` (what
makes the change survive a reload). The built-ins carry them for IOS, NX-OS,
ASA, Junos, PAN-OS and EOS. A `platforms.json` written by an earlier version
lacks the fields; a blank one falls back to the built-in value, so an
existing installation pushes to the platforms it already knew without an
edit. A platform with no enter command is never pushed to.

The assistant also uses the platform id to parse show output into rows: the
built-in ids map to ntc-templates' names, and a platform you add yourself is
sent as raw text until its id is one ntc-templates knows.

### Aliases and upgrades

Aliases **merge** with the built-in set rather than replacing it. If your file
simply won, you would be frozen on the alias set that existed the day you
first opened it, and would never receive a new one — the wrong outcome for a
file you are encouraged to edit.

The consequence is that omitting an alias does not delete it. To suppress a
built-in alias, set it to an empty string. That is a deliberate act and
survives upgrades, whereas absence cannot be told apart from "this predates
the alias existing".

The editor handles this for you: removing a row and saving does the right
thing.

### Starting again

**Reset all to defaults** discards every edit and restores the built-in
definitions. Deleting `platforms.json` does the same.


## It remembers what you told it

Identifying a device by hand used to last as long as the tab. Close it,
reconnect, and you were back to a device identified from its prompt alone —
below the confidence threshold, so no paging command, and the guardrail
falling back to the generic list.

That landed hardest on exactly the devices the override exists for. A switch
whose banner is a legal warning, and anything behind a terminal server, are
the two cases automatic identification will never settle on its own — so they
were the ones you had to re-identify every single time.

Tell ShellMate what a device is and it now remembers, against the saved
connection. The next time you connect, aliases work immediately, paging is
turned off, and destructive commands are matched against the right platform's
list.

**A confident banner still wins.** A device that used to answer as an ASA and
now announces itself as IOS has most likely been replaced, and the banner is
evidence about the device as it is today. When that happens ShellMate says so
rather than quietly changing its mind — and offers to let you re-identify it
if the new answer is wrong.

**It only works for saved connections**, because there is nowhere else to put
it. An ad-hoc connection to an address you typed once is forgotten with the
tab, as before.

Devices found by a network scan arrive with their platform already recorded,
from the SSH banner the scan read — so this works on them from the first
connection without anybody setting anything.

## It remembers what the device said

Alongside what you told it, a saved connection keeps what the device said
about itself: its **release**, its **model**, its **serial number**, and
when you **last opened** it. The release and model are known the moment the
device is identified. The serial arrives with the configuration capture, on
the second channel that capture already opens, from the platform's own
version and inventory commands — read-only, and never on the fallback that
types into your session.

That turns the connection list into an inventory. The tree search matches
them, so typing a model number lists every device carrying it and a release
lists everything still on it. The hover cards show them. The CSV export
carries them, which is how "what version is everywhere" becomes one file.

They are facts, not settings: whatever the device says next replaces them,
an empty answer never erases what was known, and the import deliberately
has no column for them — only a device may state what it is.

They are kept against the connection the session was opened from, not
against the address. That matters wherever a hundred devices sit behind one
address — a terminal server, or a lab behind one jump host — because
otherwise every one of them would claim the serial number of whichever was
opened. A session started straight from the dialog has no saved connection
to name, so there the address is all there is to go on and every connection
sharing it learns the same thing.
