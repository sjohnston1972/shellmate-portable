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
**Settings → Serial Defaults**.

Opening a COM port needs no administrator rights, as long as the adapter's
driver is installed.

### Break signal

Sending a break is how you interrupt a Cisco device during boot to reach
ROMMON. It is available from the tab context menu on a serial session.

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

## Saved connections

Every successful connection is saved so it appears on the welcome screen.
Saved connections never contain a password.

If you connect by IP, the saved connection is renamed after the device once
it identifies itself, so the welcome screen shows `core-sw-01` rather than
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

## Reconnecting

A dropped session keeps its tab so you can still read the buffer. Right-click
the tab and choose **Reconnect** to bring it back — using the saved password
if there is one, and asking only if there is not.

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
key-based ones carry a key icon on the welcome screen — worth knowing before
you click, because when a key connection fails the cause is usually the key
rather than a password.

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
welcome screen knowing what platform they are. Scanning the same subnet again
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
duration are all in **Stockton → Network discovery**. The defaults are
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
Definitions**, and the switch is **Stockton → Terminal → Confirm destructive
commands you type**.

**It catches common mistakes rather than everything, and it is worth knowing
where the edges are.** ShellMate matches on the line it can see you assemble,
so an abbreviation like `relo` matches nothing, and a command recalled with
the up arrow arrives with nothing to match against — the cursor moved
invisibly and the assembled line is empty. A guardrail believed to be total is
worse than one whose limits are known.

On a device ShellMate could not identify confidently there is no platform
list, so the generic one is used — the commands that are destructive on
anything. **Stockton → Terminal → On devices ShellMate could not identify**
turns that off if you would rather.

## Coming back after a drop

**Stockton → SSH → Reconnect a dropped session by itself** is off by default,
because silently re-authenticating to a device is not something to do without
being asked.

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
