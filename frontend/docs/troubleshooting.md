# Troubleshooting

## Start here

`ShellMate-Data/shellmate.log` records what the application did on this run.
It is rewritten each launch, so what is in it is current.

**Settings → Diagnostics** shows the essentials without leaving the
interface: which build this is, where the data folder and log are, and what
the history database holds. `GET http://127.0.0.1:8765/api/system/info`
reports the same from outside it.

## It will not start

**A message box appears about the server.** Something else holds the port, or
a security product blocked the listen. ShellMate tries port 8765 and walks
upward — twenty ports by default; the count is **Ports to try if 8765 is
busy**, under Settings → Diagnostics — so this usually means all of them were
unavailable. Check the log.

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
back so it could still start. Settings → Diagnostics says so too, on the
data-folder row.

Move the executable somewhere writable if you want the data to travel with it.

## Connection problems

### SSH authentication fails

Confirm the username and password by other means first. If you are using a
key, check the path is right and the passphrase is correct — ShellMate says
which of those failed rather than reporting a generic error.

A device that accepts a key for SSH and then prompts for an enable password
needs both fields filled in.

Each message a refused key can produce, and what to check on the device for
it, is listed under *Troubleshooting* on the [SSH keys](#ssh-keys) page.

### The device asked for a code and the login failed

A device behind two-factor asks for the code through the connection itself.
The first attempt cannot know the answer, so it is refused, the form
appears, and the *second* attempt carries the code. Two things go wrong:

- **The code was used up.** A one-time code is consumed by the attempt that
  carries it. A wrong or stale code is a plain refusal; wait for the next
  code and connect again.
- **The account locked after the first attempt.** Some TACACS policies lock
  after one failure. The first, exploratory attempt is that failure; the
  policy needs to allow two, or the device needs to ask for the code in the
  password prompt so that no exploratory attempt is needed.

### A port forward will not start

*Could not listen on localhost:PORT* means something on this machine already
has the port — another forward, another tool, or a previous session that has
not finished closing. Pick another port. *The device refused* on a remote
forward means the device's SSH policy does not allow it; most network kit
does not. A local forward that starts but carries nothing means the device
could not reach the destination you gave: the host and port are as the
*device* sees them, not as this machine does.

### Apply configuration was refused

*No configuration commands for platform* — the device is unidentified, or
identified as something ShellMate does not push to (Linux, a platform you
added without `config_enter`). Identify it from the Device chip, or add the
commands under Platform Definitions. *The change contains a command the
guardrail holds* — a line is on the platform's dangerous list; the preview
names it, and Apply asks for a confirmation that sends it anyway. *The
session is no longer connected* — the push needs the live SSH session; a
serial or telnet tab cannot be pushed to.

### A scheduled backup skipped a device

The result on the group says why, per device: *no saved credentials* (a
scheduled job cannot ask — save a password or point the connection at a
shared credential), *not an SSH connection* (serial and telnet cannot be
captured), or a failure message from the login or the capture itself. A
device that was skipped because its open session refused a second channel
is captured next time the session is closed.

### The update check says GitHub could not be reached

That is the whole message on a machine with no route to the internet, and it
is not a fault. The check sends the version number and nothing else, and a
copy that cannot ask simply does not know whether a newer release exists.
The release page is at the repository on GitHub if another machine can look.

### "Nothing was copied" or "Nothing was pasted"

The browser refused clipboard access. In the desktop window this is usually
a click in the terminal being needed first; in a browser it is the site's
clipboard permission. The warning appears at most once every thirty seconds
so that copy-on-select cannot flood the screen while the clipboard is
blocked.

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

## The configuration snapshot did not happen

The drift check prefers a **second SSH channel** — it cannot disturb what you
are typing, and it gets its own wide terminal so paging never engages. Not
every device offers one: many switches cap concurrent sessions at one, and
serial and telnet cannot multiplex at all.

Those devices are not skipped. The capture runs **through the session you are
already in** — hidden from your screen while it happens, never started
mid-command, and stated plainly once it is done, because nothing ShellMate
sends is sent silently. That behaviour is a setting, on by default: **Capture
over your session if a second channel is refused**, among the advanced rows
under **Settings → Configuration Capture**.

If no capture is happening at all, the likely reasons:

- Capture is switched off under **Settings → Configuration Capture**, which
  also stops the drift check.
- The live fallback above is switched off, and the device refuses a second
  channel.
- The retrieval command for that platform is wrong, or the account lacks
  privilege — the device returns nothing, and ShellMate says so rather than
  storing an empty snapshot.

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
install. The robot icon in the tab bar turns it on; **ShellMate Interface →
Show the AI panel** is the same switch.

## The AI panel is not working

Press **Test connections & refresh models** under **Settings → AI
Providers**. It reports exactly what went wrong per provider.

"Could not reach" on a corporate build usually means outbound HTTPS is
proxied or blocked, not that the key is wrong. Ollama running locally avoids
the problem entirely.

## The model list is stale

Cloud providers retire models. If a chat fails because the selected model no
longer exists, the picker refreshes itself from the providers so you can pick
a current one — the failure is self-healing rather than permanent. **Test
connections & refresh models** under Settings → AI Providers, and the refresh
button beside the model picker itself, do the same on demand.

## An advanced setting broke it

Run ShellMate once with `--reset-advanced` and every advanced setting goes back
to its default. Deleting the `advanced` section from `settings.json` does the
same thing.

Nothing among the advanced rows can stop ShellMate starting — every value is
held to a range on the server — but a connect timeout of three seconds or a
restrictive cipher list will certainly stop it reaching a device.

## Reporting a problem

Click the **?** in the sidebar. It opens a panel that gathers what the first
reply would otherwise have to ask for, writes it as a single zip in your data
folder, and opens an email naming the file so you can attach it. The same
panel opens from **Build a support bundle** under Settings → Diagnostics.

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

### Anything that is not a fault

The same panel has **Email the developer**, which opens a plain email with
nothing attached and nothing gathered. An idea, something that reads oddly, a
device that behaved unexpectedly, or that it is useful — none of that needs a
diagnostic bundle, and dressing it up as a fault report to get it sent is how
good feedback goes unsent.
