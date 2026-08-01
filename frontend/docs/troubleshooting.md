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

## Paging is still on, or aliases are not working

Both depend on the device having been identified **confidently**, and there
are two distinct states that look similar in passing:

**The status bar says `unidentified`.** Neither the banner nor the prompt
matched anything.

**The status bar names a platform, followed by `(unconfirmed)`.** It was
recognised, but only from its prompt — which narrows the field without picking
the platform, so nothing is sent. The note in the corner of the terminal says
this when you connect, and names the command it declined to send.

Either way the causes are the same:

- A legal banner replacing the version string. Very common.
- A terminal server sitting in front of the device.
- A platform ShellMate does not know.

**For this session:** click the device name in the status bar and say what it
is. Aliases apply at once and the paging command is sent.

**For good:** **Settings → Platform Definitions** — add something from the
device's banner to that platform's *Identify by* list. See
[Device awareness](#device-awareness).

If the status bar shows the platform with no qualifier and paging is still on,
check the setting itself is on under **Settings → Device Awareness** — the
note in the corner says so explicitly when that is the reason.

## The configuration snapshot is skipped

The drift check needs a second SSH channel. Three reasons it may not be
available:

- The device caps concurrent sessions at one. Common on older switches.
- The session is serial or telnet, which cannot multiplex.
- The retrieval command for that platform is wrong or the account lacks
  privilege.

It is skipped rather than falling back to typing into your live session,
which would scroll a page of configuration under your cursor.

A fourth possibility: capture is switched off under **Settings →
Configuration Capture**, which also stops the drift check.

If captures are being taken but no files appear, **Also save each capture as a
file** is the separate switch for that — and only a configuration that has
actually *changed* is written, so a device nobody has touched produces one
file and then nothing.

## Aliases are not expanding

Three things must all hold:

1. The device was identified confidently.
2. Alias expansion is on under **Settings → Device Awareness**.
3. You typed the alias **on its own**. `ints` expands; `show ints` does not,
   deliberately.

## The AI panel is not there

It is off until you ask for it — the terminal takes the full width on a fresh
install. The robot icon in the sidebar turns it on.

## The AI panel is not working

Press **Test connections & refresh models** under **Settings → AI
Providers**. It reports exactly what went wrong per provider.

"Could not reach" on a corporate build usually means outbound HTTPS is
proxied or blocked, not that the key is wrong. Ollama running locally avoids
the problem entirely.

## Reporting a problem

Click the **?** in the sidebar. It opens a panel that gathers what the first
reply would otherwise have to ask for, writes it as a single zip in your data
folder, and opens an email naming the file so you can attach it.

Everything is a choice, and everything can be read first. **Preview** beside
each row shows exactly what would be written.

| Included by default | What it is |
|---|---|
| About this installation | Version, frozen or not, where the data lives |
| Library versions | What paramiko, pywebview and the rest actually are |
| Application log | What ShellMate did this run — not your session contents |
| Settings | Your preferences, with API keys masked |

| Off unless you ask | What it is |
|---|---|
| Platform definitions | The commands and aliases per device type |
| Assistant prompts | The system prompts, and whether you changed them |
| Command library | Your saved broadcast commands |
| AI providers | Which are configured — never the keys |
| Open sessions | Device names, transports, what each was identified as |
| Recent terminal output | The last few hundred lines, credentials masked |

The last two are marked **about your devices**, because that is the only
judgement worth making here: everything else describes ShellMate, not the
estate you have pointed it at.

**What is never gathered, whatever you tick:** API keys, device passwords,
vault contents, and the plaintext credentials file. Terminal output goes
through the same masking that covers session logs.

The bundles are kept in `ShellMate-Data/support/`, so you can look at one
again — or delete it — after sending.
