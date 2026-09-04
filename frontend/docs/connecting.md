# Connecting to devices

ShellMate speaks SSH, serial and telnet. Everything above the connection —
recording, search, the assistant, aliases — works the same regardless of how
you got there.

## SSH

The common case. Password or key authentication, and optionally through a
jump host.

### Key authentication

Open **Key authentication and jump host** in the connection dialog and point
it at your private key. **Browse** raises the platform's own file dialog when
ShellMate is running in its desktop window; opened in a browser instead, where
that dialog does not exist, it opens ShellMate's own file browser. Typing a
path still works.

Ed25519, ECDSA, RSA and DSA keys all work, including encrypted ones — you will
be asked for the passphrase.

**Username for this key** is separate from the one in the main form, and left
blank it reuses it. They are frequently different: a jump host that takes your
personal key under your own account, fronting devices that take a shared
service account.

The key never leaves your machine. Only the signature it produces is sent, as
with any SSH client.

You can supply a key *and* a password. Network devices commonly accept a key
for the SSH layer and then prompt for an enable or AAA password.

Making a key, choosing its type, and getting the public half onto a Cisco,
Junos or Linux device are covered on their own page: [SSH keys](#ssh-keys).

### When the device asks a question

A device or bastion behind two-factor authentication asks for something
beyond a password — a one-time code, a push confirmation, a "verification
code:" prompt. ShellMate cannot know the answer, so it stops, shows you what
the device asked, and sends your answer with the next attempt. The password
still answers the password prompt; only the extra question reaches you. A
one-time code is used by the attempt that carries it and never stored.

What you see: the connect pauses, and a small form appears with the device's
own prompts as its labels — *Verification code*, say — masked where the
device said the answer should be hidden. Type the code, press **Send**, and
the connection completes. The device's title and instructions, where it sent
any, are shown above the fields.

Three things worth knowing:

- The first attempt is counted by the device as a failed login, because the
  only way to learn what it will ask is to be asked. A TACACS policy that
  locks an account after one failure will notice; ask for the policy to
  allow two.
- A wrong code comes back as a plain refusal, not the form again — a
  one-time code cannot be retried, so the next attempt needs the next code.
- A device that wants *only* a password over keyboard-interactive is
  answered from the password field without asking you anything.

The same happens on a reconnect, and when a connection is opened straight
from a tile.

### Jump hosts

Fill in **Jump host** to reach a device through a bastion. This is the same
thing OpenSSH calls `ProxyJump`: ShellMate connects to the bastion, opens a
channel through it, and runs a complete second SSH session inside that
channel.

The traffic to the target is encrypted end to end. The bastion carries it but
cannot read it.

If the jump host uses different credentials from the target, fill in the jump
username, password or key separately. Left blank, the target's username is
reused.

## Serial console

For the device with no IP address yet, or the one whose management network
you have just broken.

Choose **Serial console** and pick a port. ShellMate lists the ports actually
present on the machine with their descriptions, because `COM3` on its own is
not enough to identify the right adapter when a laptop has a dock and two USB
converters attached.

Defaults are 9600 8-N-1 with no flow control, which is what Cisco console
ports expect. Change the defaults for every future connection under
**Settings → SSH & Serial**.

Opening a COM port needs no administrator rights, as long as the adapter's
driver is installed.

### If the port will not open

The two common causes, both reported explicitly:

- **Already in use.** PuTTY, another terminal, or a previous session that did
  not close. Windows allows one owner per port.
- **Not found.** The cable is unplugged, or the USB-to-serial driver is not
  installed.

Either way ShellMate lists the ports it *can* see, which usually identifies
the problem immediately.

## Telnet

Still everywhere: older switches, out-of-band terminal servers, lab gear, and
anything whose SSH stack was disabled or never worked.

Telnet is unencrypted. Everything, including your password, crosses the
network in plain text. ShellMate does not stop you — sometimes it is the only
way in — but it is worth being deliberate about.

If you supply a username, ShellMate answers the device's login prompts for
you. That behaviour disables itself once you are logged in, and in any case
after thirty seconds. It has to: a password prompt pattern matching ordinary
output an hour into a session would otherwise type a credential into a live
device.

Leave the username blank to log in by hand.

## Quick connect

An address in a ticket or a chat message should be a session in two
keystrokes. Press **Ctrl+P** and type it.

| What you type | What it dials |
|---|---|
| `10.1.20.5` | SSH to 10.1.20.5 on port 22 |
| `admin@10.1.20.5:2022` | SSH as `admin`, port 2022 |
| `ssh core-sw -p 2200` | SSH to `core-sw`, port 2200 |
| `telnet 10.1.1.1 2003` | Telnet to a terminal-server port |
| `COM5 115200` | A serial console at 115200 baud |

The **Connect to …** row appears at the top of the list with the full details
beside it — transport, user, address and port — because `10.1.1.1 2003` is a
terminal server port to one person and a typo to another, and the only defence
is seeing the answer before Enter rather than after.

Enter opens it. The connection dialog appears only when something is still
missing: telnet and serial connect straight away, and SSH stops at the
password field with everything else filled in.

**A saved connection wins.** If the address matches one you have already set
up, the row says **Open** and its name instead, and Enter uses the saved
profile — its credentials, its group, its key, its jump host. An open tab for
it switches to that tab rather than connecting twice.

If your clipboard already holds an address when you press Ctrl+P it is filled
in and selected, so it is one keystroke to take and one to replace. Some
webview builds refuse clipboard access; the box says so rather than appearing
to ignore what you copied.

The rest of the time this box is the tab finder it has always been. A
connection is only offered when what you typed is unambiguously a target — a
transport word, a username, a port, an IP address or a COM port — so an
ordinary search for `glasgow` does not offer to dial it.

## Saved connections

Every successful connection is saved so it appears on the dashboard.
Saved connections never contain a password.

If you connect by IP, the saved connection is renamed after the device once
it identifies itself, so the dashboard shows `core-sw-01` rather than
`10.20.30.40`. The address it dials is deliberately left alone: a saved
connection named after the device but still dialling the IP works everywhere,
whereas one dialling the name only works where that name resolves — which on
a management network is frequently nowhere.

## Remembering passwords

Tick **Remember these credentials in the encrypted vault** and the password
goes into the vault, filed against that connection. Next time, leave the
password blank.

There is a second option, **in plain text**, which writes it readable to your
data folder — no encryption and nothing to forget, at the obvious cost. The
dialog says exactly what that means when you tick it. Ticking either box
unticks the other.

The dialog shows a badge when a password is stored, saying which of the two it
is, and offers to forget it. See [Credentials](#credentials) for how the
encryption works and when plain text is a reasonable choice.

Saved passwords are never sent to the browser. The interface passes a
connection id and the server fills the credential in on its own side.

## The tab menu

Right-click any tab for everything you can do to that session:

| Entry | What it does |
|---|---|
| **Reconnect** | Bring a dropped session back — shown only when it is down |
| **Clear console** | Empty the visible buffer |
| **Copy all scrollback** / **Copy visible screen** | The session's output, onto the clipboard |
| **Copy address** | The address you dialled — for a ticket or a firewall rule, exactly when the tab has started showing the device's name instead |
| **Save this connection** | Keep an ad-hoc connection without retyping it |
| **Duplicate session** | A second tab to the same device |
| **Rename tab** | A name of your own that survives hostname detection |
| **Port forwards** | Tunnel a port through this session — see below |
| **Apply configuration** | Push a block of config with a preview first — see below |
| **Colour scheme** | Per-tab colours — production red, lab grey |
| **Keep this tab alive** / **all tabs** | Nudge the session so `exec-timeout` never fires |
| **Disconnect session** / **all sessions** | Hang up but keep the tab and its buffer |
| **Close tab** / **Close all tabs** | Tear it down |

Closing a connected tab asks first, and then hangs up properly: an explicit
disconnect goes to the device before the teardown, so the far side is logged
out even if the teardown request is lost.

**Disconnect** is not **Close**. Disconnecting keeps the buffer on screen and
puts **Reconnect** one click away — the same state a session that dropped on
its own leaves behind.

## Coming back after a drop

Not every drop is the device. The link between this window and ShellMate's
own server can go on its own — the laptop sleeps, the desktop window is hidden
long enough to be suspended, a proxy times out an idle connection — while the
session to the device stays up, because it lives in the server, not the
window. When that happens the tab says *reattaching* rather than going red,
the socket is reopened with a growing pause between tries, and whatever the
device said in the meantime is delivered when it reconnects. The tab is
marked disconnected only when the server says the session has gone, or when
the server itself cannot be reached for a couple of minutes.

A dropped session keeps its tab so you can still read the buffer. Right-click
the tab and choose **Reconnect** to bring it back — using the saved password
if there is one, and asking only if there is not.

**Settings → SSH & Serial → Reconnect a dropped session by itself** is off by
default, because silently re-authenticating to a device is not something to do
without being asked.

Turned on, it retries only a drop ShellMate did not cause — never after you
close a tab, never for serial, since the COM port did not go anywhere. It
backs off, doubling from five seconds to a minute, because a device coming
back from a reload takes minutes and hammering it for the first two is
pointless.

**It waits longer after a reload.** ShellMate saw the `reload` go in, so it
knows why the session dropped and roughly how long the device will be away.

**It only works where credentials can be resolved without being held.** The
password is cleared from memory the moment authentication succeeds, and that
does not change here — so this needs a saved connection whose password the
server can fetch for itself. Where it cannot, the tab says which is missing
rather than quietly doing nothing.

When it succeeds, the terminal says so. A session that silently reappears
leaves you unsure whether the scrollback above the line is from the same boot
of the device, which matters when you are about to reason about what changed.

## File transfer

The **Files** panel browses the device's filesystem over SFTP, using the SSH
connection the tab already has. No second login, and no separate tool for
pulling a config off a switch or pushing an image onto one.

Plenty of network devices run an SSH shell with no SFTP subsystem at all. If
yours is one of them, the panel says so rather than showing an empty
directory.

## Choosing a connection type

Four entries, and the first two are the same transport asking for different
things:

| | What it wants |
|---|---|
| **SSH — password** | An address, a username and a password |
| **SSH — key or jump host** | An address, a username, a private key file, and a bastion if you go through one |
| **Serial console** | A COM port and its line settings |
| **Telnet** | An address, and credentials only if you want ShellMate to answer the login prompts |

The two SSH entries connect identically. They are separate because the key
form has nine fields the password form does not, and putting them in one place
meant everybody paid the cost of the complicated case — it used to be a
collapsed section that dwarfed the dialog when opened.

Switching between them keeps the address and username, so picking the wrong
one costs nothing. A saved connection opens on whichever form it uses, and
key-based ones carry a key icon on the dashboard — worth knowing before
you click, because when a key connection fails the cause is usually the key
rather than a password.

## Port forwards

A tab's right-click menu has **Port forwards**: tunnels through the SSH
session that is already open, so a host that only the device can reach
becomes reachable from this machine without another login. Three kinds, the
same three OpenSSH offers.

**Local** — a port on this machine that reaches a host *as the device sees
it*. The everyday case is a web page on the management network:

1. Open the session to the device that can reach it — a core switch, a
   bastion, a jump box.
2. **Port forwards → Add**: kind *Local*, listening port `8443`,
   destination host `10.0.0.5`, port `443`.
3. Open `https://localhost:8443` in a browser. The certificate warning is the
   device's own certificate, presented for a name it was not issued for.

**Dynamic** — a SOCKS5 proxy on this machine. Point a browser (or `curl
--socks5 localhost:1080`) at it and every address resolves and connects from
the device's side of the network, each connection its own channel. No
destination is given; the client names one per connection.

**Remote** — the reverse: a port *on the device* that reaches a host here.
Useful for handing a file server or a syslog collector on your laptop to a
device that cannot otherwise reach it. The device has to allow it
(`GatewayPorts`-style policy on Linux; most network kit refuses, and says so).

Rules that keep this safe:

- **Listeners bind to this machine only.** Nothing off it can use a forward,
  for the same reason nothing off it can use ShellMate's API.
- **Bounded.** Up to eight per session, adjustable under Stockton → SSH.
  A port already in use on this machine is refused with a reason rather than
  silently taking another.
- **They die with the session.** Disconnect or close the tab and every
  listener closes.
- **Nothing is silent.** The dialog lists every forward with how many
  connections it has carried, and each one is logged.

Tick **start this forward with every session from the saved connection** and
the forward is kept on the profile; the next session from that tile starts it
automatically, and stopping one asks whether to forget it as well.

## Applying configuration

**Apply configuration** in a tab's menu takes a block of configuration and
puts it on the device — with a preview first, through your own session, and
with the change recorded as a diff. Nothing is sent until you have read the
preview and pressed Apply.

### The steps

1. **Write or paste the lines**, one command per line exactly as you would
   type them in configuration mode, indentation included. Tick *capture the
   running configuration first* if the last capture is old; otherwise the
   preview compares against the latest stored one.
2. **Read the preview.** Every line is marked:
   - `+` **new** — not in the running configuration
   - `=` **already in place** — present verbatim; sending it changes nothing
   - `−` **removal** — a `no …` (or `delete …`, `undo …`) whose target exists

   The summary names what it compared against. A device with no capture at
   all is said so plainly, and every line reads as new.
3. **Apply.** The lines go into the live session wrapped in the platform's
   enter and exit commands — `configure terminal` … `end` on IOS — paced by
   the delay under Stockton → Capture, and echoed on your screen like
   anything typed. Tick *save afterwards* to add the platform's save command
   (`write memory`, `copy running-config startup-config`, `commit`).
4. **See what changed.** The configuration is captured before and after,
   both go into the archive, and the diff opens in the configuration panel.

### Review with the assistant

The preview carries a **Review with the assistant** button. It asks for the
five things a second pair of eyes is worth: the intended effect, anything that
has to happen in a different order, an omission such as a missing `no
shutdown` or `commit`, what else on the device the change touches, and the
lines that would undo it.

The dialog stays open and the answer arrives in the chat pane beside it, so
the preview is still on screen to read the review against. Pressing it changes
nothing about what Apply will do, and **nothing is sent to the device** — the
review is built from the preview you are looking at and the stored capture,
never a fresh one.

What goes to the assistant is the platform, the classified lines, the
guardrail hits, and the stanzas of the running configuration your lines land
in — masked on the server like everything else that leaves the machine.
**Configuration sent with a push review** in Stockton, under AI Assistant,
caps how much surrounding configuration travels; zero sends the change alone.

### The guardrail

A line on the platform's dangerous list — `reload`, `shutdown`, `write
erase` — refuses the whole push. The preview names the lines; Apply then
asks once more, listing them, and sends them only on that confirmation.
The list is per platform under Settings → Platform Definitions.

### Proposing the way back

The diff panel after an apply offers **Propose the way back**. It compares
the running configuration now with the capture from before the push and
writes a change that would take the device back: what was added becomes
`no …`, what was removed is put back, with its section header for context.

It is a *proposal*, opened in the editor for you to read and edit, never
applied on its own. Platforms differ in what a bare `no` undoes — a removed
`interface` block on IOS is `no interface`, a changed default is not always
its own inverse — so read every line. The same button works for any earlier
capture from the configuration history, which makes it a general way of
rolling a device back to a known state, one reviewed step at a time.

### Which platforms

| Platform | Enter | Exit | Save |
|---|---|---|---|
| Cisco IOS / IOS-XE | `configure terminal` | `end` | `write memory` |
| Cisco NX-OS | `configure terminal` | `end` | `copy running-config startup-config` |
| Cisco ASA | `configure terminal` | `end` | `write memory` |
| Junos | `configure` | `commit and-quit` | — (the exit commits) |
| PAN-OS | `configure` | `commit` | `exit` |
| Arista EOS | `configure terminal` | `end` | `write memory` |
| Linux | — | — | not pushed to |

The commands live in each platform's definition under Settings → Platform
Definitions, as `config_enter`, `config_exit` and `save_command`. A platform
with no enter command is refused rather than guessed at, and a device that
has not been identified confidently is treated the same way — identify it
from the Device chip first.

## Files

The **Files** panel in the sidebar browses the device over the same SSH
session, without a second login. The toolbar has the path, *up*, *go*,
**upload a file**, **upload a folder** and **new folder**; each row has
**download**, **rename**, **permissions** and **delete**.

- A file downloads and uploads singly. A **folder downloads as a zip** with
  its tree inside, and **uploads file by file**, each subfolder created as it
  is first needed — one at a time on purpose, because an SFTP channel is one
  channel and forty parallel opens against a switch is a good way to lose it.
- **Rename** takes a new name, or a full path to move the entry elsewhere.
- **Permissions** takes an octal mode such as `644` or `0755`; anything else
  is refused before it reaches the device.
- **Delete** on a folder removes everything beneath it, after naming the path
  and saying there is no recycle bin on a switch.

The folder operations count what they would touch before touching anything
and refuse above the limit under Stockton → Files and panels, so a slip does
not become a wiped flash. Deleting the root is refused outright, whatever the
limit.

## Finding devices

On a site you did not build, the first job is working out what is on the wire.
The **Find Devices** panel (the radar icon in the sidebar, or **Find** beside
the hostname box in the connection dialog) sweeps a subnet and lists what
answered.

Give it a subnet (`10.20.30.0/24`), a range (`10.20.30.1-60`), a
comma-separated list, or a single address. Your own subnet is filled in for
you, because that is the answer most of the time and it is the one target that
cannot accidentally reach somebody else's network.

**What it tells you.** Not just which ports are open. ShellMate reads the SSH
banner and runs it through the same identification it uses on a live session,
so the list says *Cisco IOS / IOS-XE* rather than *port 22 open* — before you
have connected to anything. Where a device has a web interface, the page title
and certificate name are shown too; a title of "Cisco Integrated Management
Controller" identifies a box more usefully than its address does.

Tick the ones worth keeping and **Save as connections**. They arrive on the
dashboard knowing what platform they are. Scanning the same subnet again
updates those entries rather than adding a second copy of everything.

### Before you scan something

This makes real connection attempts to every address you give it, and the
network will log them. Scanning something you do not own or have permission to
test is at best poor manners, and an intrusion detection system worth its
licence will raise an alert. A scan of more than 256 addresses asks first, with
the number in the question.

ShellMate reads what each device announces and fetches its home page. **It
never tries a password.** This finds devices; it does not test them.

### Why there is no ping

A ping sweep needs raw sockets, which need administrator rights on Windows —
and ShellMate deliberately requires none. It would also find less: ICMP is
filtered on most management networks, which are exactly the ones worth
scanning. A TCP connection to port 22 needs no privileges and tells you more.

### Tuning it

How many addresses are probed at once, how long each waits, which ports are
tried, whether web pages are fetched at all, and the limits on size and
duration are all under **Settings → Network Discovery**. The defaults are
deliberately unhurried: a burst of hundreds of simultaneous connections
exhausts a firewall's session table and looks exactly like an attack to
anything watching.

## Confirming destructive commands

`reload`, `write erase` and the rest of each platform's destructive list are
held when you type them, and a prompt names **the device** before anything is
sent. Nothing has reached it at that point — the line is cleared from the
device's input and the carriage return is withheld, so cancelling leaves it
exactly as it was.

The device is named first on purpose. The mistake this exists to catch is not
"did I mean to type reload" — it is "which tab am I in".

Which commands count is per-platform, under **Settings → Platform
Definitions**, and the switch is **Settings → Terminal Behaviour → Confirm
destructive commands you type**.

**It catches common mistakes rather than everything, and it is worth knowing
where the edges are.** ShellMate matches on the line it can see you assemble,
so an abbreviation like `relo` matches nothing, and a command recalled with
the up arrow arrives with nothing to match against — the cursor moved
invisibly and the assembled line is empty. A guardrail believed to be total is
worse than one whose limits are known.

On a device ShellMate could not identify at all there is no platform list, so
the generic one is used — the commands that are destructive on anything.
**Settings → Terminal Behaviour → On devices ShellMate could not identify**
turns that off if you would rather.

## Groups

Saved connections live on the dashboard, and the **group tree** beside the
terminal area organises them: named groups with a colour and an icon of their
own, and groups inside groups — `Glasgow` holding `Glasgow/access` and
`Glasgow/core` — so a site reads as a branch with its subgroups under it.
Favourite a group and it is pinned to the top of the tree.

A connection joins a group through the **Groups** field in the connection
dialog — comma-separated, and it can be in as many as apply, because the
useful groupings overlap: a device is both in Glasgow and in production and an
access switch, and forcing a single folder means picking one. Or skip the
dialog entirely and drag a tile onto a group in the tree.

Click a group and its dashboard comes forward — even when a terminal is on
screen, because selecting a group is a statement about what you want to look
at. The tree has its own search, covering group names and the devices inside
them, and collapses to a slim rail when you want the width back. Past twenty
tiles this is the difference between finding a device and scanning for it.

Right-click a group for **Connect all** and **Disconnect all**. Connect all
opens a tab for every device in the group, subgroups included; the handshakes
are paced rather than fired at once — forty simultaneous SSH connections
through one bastion buries it — and anything that fails to connect is
reported by name rather than silently skipped. It is deliberately not offered
for the whole unfiltered dashboard: opening two hundred tabs should not be
one click away.

**Broadcast still works on open sessions only.** "Send this to everything in
`Glasgow/access`, connecting to whatever is not open" is a genuinely different
and more dangerous operation, so opening the group is the step you take first —
the devices are visibly in front of you before anything is sent to them.
