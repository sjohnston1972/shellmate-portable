# History, drift and evidence

Every session is recorded, automatically, with nothing to switch on.

## Scheduled backups

A group's right-click menu has **Schedule backups**. Switched on, every
device in the group — subgroups included — has its configuration captured on
the schedule, exactly as it is captured on connect, and the captures go to
the same history and the same archive folder.

**The schedule** is every hour, every day at a time, or every week on a day
at a time. It is armed at the next slot after you save it: a nightly backup
switched on at three in the afternoon first runs at two the next morning, not
at three. A slot that was missed because ShellMate was not running is still
owed, and runs at the next check after it starts.

**How a device is reached:**

- A device with an **open session** is captured through that session's
  second channel, the way drift detection is, and is skipped if the device
  refuses one. Nothing is typed into the session you are using.
- A device **without** one is logged into for as long as the capture takes,
  with its saved credentials or its shared credential, and disconnected. It
  is skipped, and the skip is reported, if it has no credentials saved —
  a scheduled job cannot ask.
- Serial and telnet connections are skipped; capture needs SSH.
- Devices are taken **one at a time**. Forty simultaneous logins from one
  laptop is how an authentication server rate-limits you.

**The result** is kept on the group and shown in the schedule dialog: when
it ran, how long it took, which devices were captured, which failed and why,
which were skipped and why. **Back up configurations now**, in the same
menu, runs the whole thing on demand and reports the same way.

Scheduled runs happen only while ShellMate is running. It is a portable tool
on a laptop, not a service; for a backup that must happen whether or not
your laptop is open, run it on a machine that is.

## What the backups found

A scheduled backup that runs every night is only worth having if somebody
learns what it found. ShellMate reports that once, when there is something
to report.

The morning after a run that changed something, failed, or did not happen,
a notice says so — "Scheduled backups, Glasgow: 2 changed, 1 failed" — with
a button that opens the detail. Each changed device offers its own
comparison against the configuration stored before it.

Three things are kept apart on purpose:

- **Changed** is a device whose configuration differs from the one stored
  last time. This is the thing the backups exist to catch.
- **Failed** is a device ShellMate could not reach or could not read. That
  sends you to the device.
- **Did not happen** is a scheduled run that never took place, because
  ShellMate was not running when it was due. That sends you somewhere else
  entirely — and a gap in a backup history looks exactly like a quiet week,
  which is the dangerous way round for this to be wrong.

Devices that were never going to be backed up — a serial console has no
address to reach — are listed last and quietly. They are context, not news.

**Most mornings it says nothing at all.** A clean run where nothing changed
is the normal night, and something that announces it every day is something
you learn to dismiss without reading — at which point the morning it matters
looks like all the others.

## Notes

A change window produces a running commentary. "16:02 shut Gi1/0/24, 16:05
confirmed by site, 16:40 rolled back, ticket INC-4471." It usually lives in
Notepad, where it is never searched again and never meets the transcript it
describes.

**Ctrl+Shift+N**, or **Notes for this session** on a tab's menu, opens a box
that belongs to the session in front of you. **Timestamp** puts the time at
the start of a fresh line, which is the whole point of a commentary written
while something is happening — typing "16:02" by hand while watching a
device reload is how the times end up approximate.

It saves itself a second after you stop typing, and immediately when you
close it. There is no Save button, because a note that needs saving is a
note that gets lost when the window shuts.

Notes are kept with the session, so:

- They survive a restart.
- They are searched by **History**, alongside the commands. A note hit is
  marked as a note and shown in your own words; it is never mixed in with
  the transcript, because a sentence in a transcript that nobody typed at a
  device is the one thing a record of a change window must not contain.
- They appear at the top of the session's own replay, above the commands
  they describe. "16:05 confirmed by site" means something next to what was
  running at 16:05.

**Notes are never sent to the assistant.** They carry things written for
yourself — a customer's name, why a change was really made, what somebody
said on the phone. The assistant sees terminal output; if you want it to
see a note, paste it into the chat.

If history recording is switched off there is no session on record to keep
a note against, and the box says so rather than quietly keeping nothing.

## Playing a session back

Open any recorded session from History and press **Play**. The commands run
into a terminal in the panel with their original timing — the pause between
one command and the next, and the time each one's output took to arrive —
so a change window reads back the way it happened. **Speed** is 1×, 4×,
16× or instant, and it can be changed mid-playback; **Pause** holds it
where it is and **Stop** ends it. The listing beneath is unchanged and
still there to copy from.

The bar under the controls shows where the playback has reached: the
command it is on, "12 of 41", and the elapsed time against the length of
the recording. Drag it to jump — everything up to that command is written
at once and playing carries on from there. Space pauses and resumes, and
the left and right arrows step a command at a time.

Nothing extra is recorded for this. It is the same command history, played
rather than listed, which also means it shows what the transcript captured:
commands and their replies, not every keystroke or every redraw of the
screen.

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
- **Date** narrows to the last day, week, month or quarter — or, with
  **Between two dates**, to a range you type. A date on its own means the
  whole of that day; add a time to either end to narrow it further, which
  is how you ask what happened on the Glasgow core between 02:00 and
  03:00 on the 14th. **Clear** empties both ends.

Punctuation works: search for `10.20.30.40` or `GigabitEthernet0/2` and you
get what you expect.

Click any result to replay that whole session — every command in order, with
its output and timing.

## Running something again

The show commands you ran on this switch last month are usually the ones you
want again. Two ways to reach them.

**Ctrl+R in a terminal** opens a list of every command already run on *this
device*, newest first and with duplicates folded together — the number beside
one is how often it has been run. Type to narrow it.

| Key | What it does |
|---|---|
| Enter | Puts the command at the prompt, without running it |
| Ctrl+Enter | Runs it |
| Up / Down | Move through the list |
| Escape | Close |

Enter is the gentler of the two on purpose. A command recalled from last month
is a starting point to be edited far more often than it is a thing to run
verbatim, and it arrives at the prompt where you can read it first. Either way
it goes through the same checks as anything you type: aliases expand, and a
destructive command still asks.

**In the History panel**, a result whose device is open in a live tab gains two
more buttons beside Copy: one puts the command at that tab's prompt, one runs
it. Both name the tab in their tooltip, and both target the tab matching the
*recorded* hostname — never "whichever tab is in front of you", which is how a
recalled command would find the wrong device. When the device is not open, the
buttons are not there.

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

**Explain** hands the comparison to the assistant and asks what the changes do
and whether they could cause what you are seeing. The window stays open — the
answer arrives in the chat pane beside it, so the diff is still on screen to
read the answer against. It explains whatever is currently shown: the drift
since your last visit, the before-and-after of a configuration you have just
applied, or any two captures you have picked from the history.

The diff is fetched, capped and masked on the server, exactly like terminal
output — the browser sends only which two captures to compare. How much of it
travels is **Lines of the configuration diff sent** in Stockton, under AI
Assistant; what is over the cap is announced to the assistant rather than
quietly dropped, so it never answers as though it read the whole thing.

Typing `/diff` in the chat asks the same question about the active tab without
opening the window.

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

### Logging one session

The switch in Settings covers every open tab. **Log this session**, on a
tab's right-click menu, is that decision for one tab: it says `on`, `off`, or
`on (setting)` when it is simply following the global switch, and it takes
effect immediately rather than at the next tab — "I want a record of this"
usually arrives ten minutes into a change, not at connect.

While a session is being written to a file, the status bar shows a **Logging**
chip; click it to read the file. **Open this log** on the tab menu does the
same, and the tab's hover card names the file.

A log holds what the session prints *from the moment logging starts*. What is
already on screen is not in it — that is what the next section is for.

### Saving what is already on screen

**Save scrollback as…**, on the same menu, writes everything the terminal is
still holding to a file: the change you have just finished, on the tab nobody
thought to log. It works on a disconnected tab too, because the buffer is
still there and a session that has just dropped is exactly when somebody
wants a copy of what it said.

It is masked by the same **Obscure passwords and secrets** setting below, and
the masking happens in ShellMate rather than in the browser, so the file that
lands on disk is the same either way.

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
