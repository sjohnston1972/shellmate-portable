# Troubleshooting

## Start here

`ShellMate-Data/shellmate.log` records what the application did on this run.
It is rewritten each launch, so what is in it is current.

`GET http://127.0.0.1:8765/api/system/info` reports where data is being
stored and which port is in use.

## It will not start

**A message box appears about the server.** Something else holds the port, or
a security product blocked the listen. ShellMate tries port 8765 and walks
upward, so this usually means all twenty were unavailable — check the log.

**Nothing happens at all.** Corporate antivirus quarantines unsigned
single-file executables more often than any other kind. Check its logs. A
folder build is far less likely to be flagged; see the README for the one-line
change that produces one.

**It opens the copy already running.** That is intended. ShellMate refuses to
run twice over the same data folder, because two instances would fight over
the same database. Quit from the tray icon first.

## Settings are not being kept

Look at the first line of the log. If it reports a folder inside your user
profile rather than one next to the executable, the location it was run from
was not writable — Program Files, or a write-protected stick — and it fell
back so it could still start.

Move the executable somewhere writable if you want the data to travel with it.

## Connection problems

### SSH authentication fails

Confirm the username and password by other means first. If you are using a
key, check the path is right and the passphrase is correct — ShellMate says
which of those failed rather than reporting a generic error.

A device that accepts a key for SSH and then prompts for an enable password
needs both fields filled in.

### The serial port will not open

**Already in use** — PuTTY, another terminal, or a session that did not close
cleanly. Windows allows one owner per port.

**Not found** — cable unplugged, or the USB-to-serial driver is not
installed. ShellMate lists the ports it *can* see, which usually identifies
the problem.

### Telnet connects then hangs

Some devices wait for option negotiation that never completes. ShellMate
answers everything it understands and refuses the rest, but very old
equipment occasionally expects otherwise. Try a serial console if one is to
hand.

## The device was not identified

The status bar says *unidentified* when the banner and prompt were not enough
to be confident. This is deliberate: paging-off and aliases stay disabled
rather than risk sending the wrong command.

Common causes:

- A legal banner replacing the version string. Very common.
- A terminal server sitting in front of the device.
- A platform ShellMate does not know.

The fix for the last is **Settings → Platform Definitions** — add the text
that identifies it. See [Device awareness](#device-awareness).

## The configuration snapshot is skipped

The drift check needs a second SSH channel. Three reasons it may not be
available:

- The device caps concurrent sessions at one. Common on older switches.
- The session is serial or telnet, which cannot multiplex.
- The retrieval command for that platform is wrong or the account lacks
  privilege.

It is skipped rather than falling back to typing into your live session,
which would scroll a page of configuration under your cursor.

## Aliases are not expanding

Three things must all hold:

1. The device was identified confidently.
2. Alias expansion is on under **Settings → Device Awareness**.
3. You typed the alias **on its own**. `ints` expands; `show ints` does not,
   deliberately.

## The AI panel is not working

Press **Test connections & refresh models** under **Settings → AI
Providers**. It reports exactly what went wrong per provider.

"Could not reach" on a corporate build usually means outbound HTTPS is
proxied or blocked, not that the key is wrong. Ollama running locally avoids
the problem entirely.

## Reporting a problem

Email **support@foundry-ns.com** with:

- What you were doing and what happened instead
- The relevant part of `ShellMate-Data/shellmate.log`
- The device platform and version, if it is connection-related

**Check the log before sending it.** It records what the application did, not
your session contents, but read it through — it is the sort of file worth
knowing the contents of before it leaves the building.
