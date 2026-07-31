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

Every result carries a **confidence**, and that gate matters: a bare
`switch01#` prompt could be almost anything, so it scores low and ShellMate
deliberately does nothing with it. Acting on a weak guess is how a tool ends
up sending `terminal length 0` to a firewall.

The status bar shows what was identified and the version. Hover it to see
where the identification came from and how confident it is.

## What it does with that

### Turns paging off

`terminal length 0` on IOS and NX-OS, `terminal pager 0` on ASA,
`set cli screen-length 0` on Junos, `set cli pager off` on PAN-OS.

The command is echoed into the terminal exactly as if you had typed it, and
the interface says what was sent. Nothing is typed into your session
silently — you may have to account for what happened in that session later.

A device that cannot be identified confidently gets **nothing**. A wrong
command is worse than a command not sent.

Turn it off under **Settings → Device Awareness**.

### Picks the right commands

Retrieving a running configuration is `show running-config` on IOS,
`show configuration | display set` on Junos, and `show config running` on
PAN-OS. Configuration snapshots and drift detection use whichever applies.

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

Nothing is expanded until the device has been identified, and nothing at all
is expanded on an unidentified one.

Turn expansion off under **Settings → Device Awareness**.

## Editing what ShellMate knows

**Settings → Platform Definitions** edits everything above: the paging
command, the config command, the text used to identify the platform, the
commands that will need confirming, and the alias table.

The same information lives in `platforms.json` in your data folder, so you
can edit it in a text editor, keep it in version control, or hand a copy to a
colleague.

### Adding a platform

Add an entry to `platforms.json` with an `id`, a `name`, and at minimum a
`signatures` list so it can be recognised. Everything else is optional.

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
