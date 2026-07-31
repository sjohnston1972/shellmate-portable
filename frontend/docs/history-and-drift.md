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

## Configuration snapshots and drift

Every SSH connection captures the device's running configuration and compares
it with the last time you were there:

> You were last here 12 days ago, and 4 lines have changed since.

Click **View diff** for a line-by-line comparison. Nothing changed since last
time gets a quieter note that fades on its own.

This turns every login into a drift check for free. A change someone else made
last week is visible the moment you arrive, rather than when it breaks
something.

### How it avoids disturbing you

The capture runs on a **second SSH channel**, multiplexed onto the connection
your tab already has. No second login, and nothing is typed into the session
you are working in.

Not every device cooperates. Some switches allow only one session at a time;
serial and telnet cannot multiplex at all. When that happens the check is
skipped silently rather than interfering with your session.

Identical configurations are not stored twice, so the snapshot history shows
actual changes rather than one entry per login.

## Session logs

Separate from the history database, and off by default: plain text files, one
per session, under **Settings → Session Logging**.

### Credentials in logs

Devices echo. A password typed at a login prompt can end up in a file whose
whole purpose is to be handed to someone else.

**Obscure passwords and secrets** is on by default and masks credentials as
they are written:

```
username neteng password 7 ********
 enable secret 5 ********
 snmp-server community ******** RW
```

The statement is kept and only the value replaced, so the log still shows the
account exists and how it is configured.

Two honest limits. It is pattern matching, so a credential in a form not
recognised will go through untouched — this reduces exposure rather than
guaranteeing its absence. And it applies only to what is written to disk; the
terminal always shows the truth, because hiding things from the person at the
keyboard would be worse than useless.
