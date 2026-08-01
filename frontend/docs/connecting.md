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
