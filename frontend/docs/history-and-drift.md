# History, drift and evidence

Every session is recorded, automatically, with nothing to switch on.

## What gets recorded

Not a flat log file. ShellMate reconstructs the structure of the session:
each command, its output, which device it ran on, when, and how long it took.

That distinction is the whole point. *"What did I change on the Glasgow core
last Tuesday"* is a question you can answer against structured records. Against
a folder of text files it is a grep exercise.

Getting there requires undoing what a terminal does to text — colour codes
buried inside words, `--More--` prompts erased with backspaces, lines redrawn
in place by tab completion. All of that is resolved before anything is stored,
so a search for `GigabitEthernet0/1` finds the line even when the device
coloured the interface name.

## Searching

Open **Session history** from the sidebar.

- **Free text** searches commands *and* their output.
- **Device** narrows to one piece of kit.
- **Date** narrows to the last day, week, month or quarter.

Punctuation works: search for `10.20.30.40` or `GigabitEthernet0/2` and you
get what you expect.

Click any result to replay that whole session — every command in order, with
its output and timing.

## Clearing history

**Clear history**, in the History panel's header, deletes records — scoped
by whatever the
panel is already filtered to, because choosing a device and then being
offered only all-or-nothing is how the wrong thing gets deleted. Filter to
one device and it clears that device; set a date range and everything *older*
goes, so what is on screen survives. No filters means all of it.

It offers to take the device's configuration snapshots too, ticked by
default — a snapshot is a full running config, secrets included, and clearing
a device's history while quietly keeping its configs would be misleading.
What was removed is counted rather than reported as "done".

For an automatic version, **Discard history after** (under **Settings →
Session Logging**) prunes anything older than a number of days. The default
is zero — keep everything — because history you did not know was being
discarded is worse than a large database.

## Configuration capture and drift

Every connection captures the device's running configuration and compares
it with the last time you were there. If anything changed, a prompt appears
over the terminal:

> **core-sw-01 has changed since you last logged in.** 4 lines (3 added, 1
> removed), 12 days ago. Would you like to see the difference?
> **[Show me]**

This turns every login into a drift check for free. A change someone else made
last week is visible the moment you arrive, rather than when it breaks
something.

It is a prompt, not an interruption. Nothing opens over your terminal by
itself — somebody arriving at a device mid-incident is not there to read a
diff, and a window you have to close first is a tax on every login. It waits
until dismissed. Nothing changed since last time gets a quieter note that
fades on its own.

### How it captures without disturbing you

The capture runs on a **second SSH channel**, multiplexed onto the connection
your tab already has. No second login, nothing typed into the session you are
working in, and its own wide terminal so paging never engages. That is always
tried first.

Not every device cooperates. Some switches allow only one session at a time
and refuse the second channel; serial and telnet cannot multiplex at all. On
those, the capture runs **through your own session** instead — and because
that is a different thing to do, it follows strict rules:

- It waits until the device is idle at a prompt, and gives up the moment you
  type.
- The command and its output are withheld from your screen, the session
  buffer, the transcript and the log — so it is neither scrolled under your
  cursor nor recorded as something you did.
- A pager is answered rather than reconfigured: it will not send
  `terminal length 0` to change your session's state.
- **It is announced afterwards.** Hidden while it happens, stated when it is
  done — the capture notice says the command ran in this session. Genuinely
  invisible would break the rule that nothing is sent silently.

The fallback is governed by **Capture over your session if a second channel
is refused**, on by default. Turn it off and nothing is ever typed into a
session on your behalf — at the cost of no capture at all on serial, telnet
and single-session switches.

Identical configurations are not stored twice, so the snapshot history shows
actual changes rather than one entry per login.

### The diff window

**Show me** opens the comparison, split into blocks — one per changed region,
each labelled with roughly where in the configuration it is. A configuration
diff is rarely one change, and rendered as a single wall they run together.

Each block carries two copy buttons:

- **Copy added** — the added lines with the `+` markers stripped. This is what
  you would paste into a device, which is why anyone copies a config block.
- **Copy hunk** — the block exactly as shown, markers and context included, for
  a change record or an email.

**Copy all** in the header takes the whole diff.

### Settings

Under **Settings → Configuration Capture**:

| Setting | Default | Effect |
|---|---|---|
| Capture the configuration on connect | On | Off, nothing is fetched and the drift check stops |
| Offer a diff when it has changed | On | Off, captures still happen silently |
| Capture over your session if a second channel is refused | On | Off, devices that refuse one get no capture at all |
| Also save each capture as a file | Off | Writes each one out as a `.cfg` |

The timings behind it — capture timeout, quiet period, diff context — render
in the same section as advanced rows.

### Keeping the captures as files

Captures always go into `shellmate.db`, which is what the diff reads. Turning
on **Also save each capture as a file** additionally writes each one out as
plain text — for mailing to a vendor, attaching to a change record, grepping a
hundred at once, or simply so your backup system sees them.

They go to `configs/` in your data folder by default, one folder per device,
named for the device and the moment. **Browse** points that anywhere,
including a network share; a relative name stays inside the data folder.

Only a configuration that has *actually changed* is written, so the folder is
a record of changes rather than one identical copy per login. The exception is
the first capture of a device, which is always kept — otherwise switching the
setting on would produce an empty folder until something happened to change.

Three limits keep it bounded, and all of them apply: how many captures are
kept per device, how old they may get, and how large the whole archive may be.
When the size limit is reached the oldest go first, since the newest capture
of each device is the one the diff compares against.

**Captures are redacted** by the same **Obscure passwords and secrets**
setting that covers session logs. A running configuration carries password
hashes, pre-shared keys and community strings, and this writes it somewhere
you chose, which may well be backed up. One switch, one promise.

## Session logs

Separate from the history database, and off by default: plain text files, one
per session, under **Settings → Session Logging**.

### Credentials in anything that leaves the machine

Devices echo. A password typed at a login prompt can end up in a file whose
whole purpose is to be handed to someone else.

**Obscure passwords and secrets** is on by default and masks credentials in
everything ShellMate writes or sends — session logs, captured configurations,
the terminal output given to the AI assistant, and anything exported to Jira:

```
username neteng password 7 ********
 enable secret 5 ********
 snmp-server community ******** RW
```

The statement is kept and only the value replaced, so the log still shows the
account exists and how it is configured.

One switch covers all of it, deliberately. It reads as a property of
ShellMate — *obscure passwords and secrets* — rather than of one output
format, and two switches for the same promise is a promise nobody can rely on.

Two honest limits. It is pattern matching, so a credential in a form not
recognised will go through untouched — this reduces exposure rather than
guaranteeing its absence. And it applies only to what is written to disk; the
terminal always shows the truth, because hiding things from the person at the
keyboard would be worse than useless.

## Pinning a baseline

"Has anything changed since my last visit?" is the wrong question more often
than it looks, because *your last visit* is an accident of when you happened to
log in. Worse, simply looking consumes it: connect, see four lines changed,
reconnect an hour later to investigate, and you are told nothing has changed —
the evidence is one snapshot further back.

**Pin as baseline**, in the configuration-changes window, fixes a moment you
chose. Drift then reports both numbers:

> You were last here 12 days ago and 4 lines have changed.
> *(2 lines differ from the baseline set on 3 March.)*

They answer different questions, so you get both rather than one replacing the
other. **Settings → Configuration Capture → Compare a config against**
switches to one or the other if you would rather.

A pinned baseline is exempt from retention. Pruning is oldest-first, so
without that the baseline would be the first thing discarded — which is
backwards, since being old is what makes it a baseline.

## Comparing any two snapshots

The same window lists every snapshot held for the device. Tick two and
**Compare selected** to diff them directly — useful when the interesting
change is between two visits rather than between now and the last one.

Comparing *across* devices — a switch against a known-good sibling — works at
the data layer but is not offered, because a raw diff of two devices is
dominated by hostnames, addresses and interface counts. That needs a
normalisation step to be worth anything, and it is a real piece of work rather
than a checkbox.
