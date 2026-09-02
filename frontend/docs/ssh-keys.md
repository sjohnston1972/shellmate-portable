# SSH keys

Key authentication replaces the password you type at every connect with a
key pair: a private half that stays on your machine, and a public half you
give to each device. The device challenges you, your key signs the challenge,
and the device checks the signature against the public key it holds. The
private key is never sent — only what it signs.

ShellMate can make keys, keep them, put them into a connection, and tell you
what went wrong when a device refuses one. This page is the whole story, from
choosing a key type to getting the public half onto a Cisco switch.

## Why keys rather than passwords

**Nothing secret is typed per connection.** A password is typed every time,
in front of whoever is looking, and copied into whatever session log is being
kept. With a key, the passphrase — if there is one — unlocks a file on your
machine; the device sees only a signature.

**Revocable per device.** A leaked password has to be changed on every device
that shares it, and every colleague using it has to be told. A key is removed
from the one device it was installed on, or from all of them, without anyone
else's login changing.

**It survives password rotation.** The account password can be rotated on
whatever schedule the policy demands; the key keeps working, because it is
not the password.

**One identity, many devices.** The same public key goes onto every device
you look after. Losing it means replacing one thing, not remembering forty.

**It cannot be phished by a fake prompt.** A key answers a challenge from the
device it is talking to. Nothing you paste into the wrong window gives it
away.

The trade is that the private key file *is* the credential. Anyone who can
read it, and knows its passphrase or finds it has none, is you. The
*Passphrases* section below is about that.

## Which kind of key

ShellMate makes three kinds and uses four.

| Type | Use it when | Notes |
|---|---|---|
| **Ed25519** | Anything modern accepts it — Linux, Junos, recent NX-OS and EOS | Small, fast, and the default. Choose it unless something refuses it |
| **RSA** | The device is Cisco IOS/IOS-XE or ASA, or old enough to know nothing else | 3072 bits is the default; 2048 is still fine; 4096 is slower for the device to verify on every connect and rarely required |
| **ECDSA** | A standard you have to meet names it | P-256 is the widely supported curve. The larger curves are slower and no device in normal use needs them |
| **DSA** | Never, for anything new | ShellMate will load one you already have, but OpenSSH stopped accepting them years ago and most network kit never did |

In practice a network engineer ends up with two keys: an Ed25519 key for
everything that takes it, and an RSA key for the Cisco estate. Both can be
attached to different saved connections, and both can live in ShellMate's
key store.

A device refuses a key type it does not understand *when you paste the public
key in*, not later. The error is immediate and the login you were using still
works, so trying Ed25519 first costs nothing.

**Encrypted keys** — ones with a passphrase — are supported for all four
types. You will be asked for the passphrase, or can save it, exactly as with
a password.

## Making a key

### In ShellMate

The **key** icon in the sidebar opens the key store. Give the key a name —
`id_ed25519` is the convention, but *lab-core* or *cust-acme* tells you a
year from now what it was for — and press **Create**.

**Type**, **RSA size** and **Curve** sit below the name and passphrase. Leave
them alone for Ed25519; the size and curve only matter to the type they belong
to.

The **comment** ends up in the public key, and therefore in every device's
configuration. `you@laptop` is the convention. Make it something a colleague
would recognise on a switch you did not set up.

The key is written to `ShellMate-Data/keys/` and appears in the list with
both fingerprints and a **Copy public key** button. That is the line to paste
into the device.

### With ssh-keygen

A key made anywhere else works just as well. On a machine with OpenSSH:

```
ssh-keygen -t ed25519 -C "you@laptop" -f id_ed25519
ssh-keygen -t rsa -b 3072 -C "you@laptop" -f id_rsa
```

Then either **Import** it into the key store — it is copied in, and the file
permissions tightened — or point the connection dialog at it where it is.

A key made with PuTTYgen is in PuTTY's own `.ppk` format, which is not an
OpenSSH key and will not load. In PuTTYgen, **Conversions → Export OpenSSH
key** writes one that will.

## Getting the public key onto a device

Every platform wants the same thing: the single line from the `.pub` file,
filed against the username you will log in as. What differs is where it goes.
**Copy public key** in the key store gives you the line; the worked examples
below show where to put it.

Two habits worth keeping for all of them:

- **Keep the session you are in.** Add the key, then open a *second* tab with
  key authentication and prove it works before you touch password login.
  Nothing on this page will lock you out if the tab you configured from stays
  open.
- **Paste, do not type.** A public key is a few hundred characters of base64,
  and one wrong character is a key that never matches. The failure looks
  identical to a wrong username.

### Cisco IOS and IOS-XE

IOS accepts **RSA** keys for user authentication. Ed25519 is refused on most
releases, so make an RSA key for this estate. SSH version 2 has to be on,
which it is by default on anything recent.

Public keys go under `ip ssh pubkey-chain`, filed by username:

```
configure terminal
 ip ssh pubkey-chain
  username admin
   key-string
    AAAAB3NzaC1yc2EAAAADAQABAAABgQC7...
    ...
   exit
  exit
 exit
```

IOS takes the key as **base64 only** — leave out the leading `ssh-rsa` and the
trailing comment — and it takes it in **pieces**: each line pasted under
`key-string` must be shorter than about 250 characters. Split the base64 at
any point; IOS joins the pieces. Paste the whole thing at once and IOS
truncates the line and stores a key that will never match.

On finishing, the device shows what it stored as a hash rather than the key:

```
show running-config | section pubkey-chain
   username admin
    key-hash ssh-rsa 1A2B3C4D...
```

That hash is the **MD5** fingerprint, which is why ShellMate shows the MD5
fingerprint beside the SHA256 one. If the hash on the device does not match
the MD5 fingerprint in the key store, the paste went wrong.

Some IOS-XE releases let you enter the hash directly with `key-hash ssh-rsa`
instead of the key. That is the same thing: the device stores only the hash
either way.

Local user and privilege come from the ordinary `username admin privilege 15`
line; the pubkey-chain entry adds a way to prove you are that user. If AAA is
in use, `aaa authentication login` still decides what happens *after* the
key — an enable password, say — and that is the case for filling in both a
key and a password in the connection dialog.

### Cisco NX-OS

One line, on the user account, with the key in quotes:

```
configure terminal
 username admin sshkey "ssh-rsa AAAAB3NzaC1yc2E... you@laptop"
```

This is the whole public key line, prefix and comment included. NX-OS accepts
RSA everywhere and Ed25519 on recent releases; if it refuses the line at once,
use the RSA key. `show user-account admin` lists the stored key.

NX-OS also takes the key from a file with `username admin sshkey file
bootflash:id_rsa.pub`, which avoids a long paste but means copying the file
to the switch first.

### Cisco ASA

RSA keys, on the user's attributes:

```
configure terminal
 username admin attributes
  ssh authentication publickey AAAAB3NzaC1yc2E...
```

Base64 only, as with IOS. The ASA hashes it on entry and `show running-config
username` prints the hash. The `ssh authentication pkf` form takes a
PEM-wrapped key instead, pasted over several lines and ended with `quit`,
which is easier on a console with a line-length limit.

### Junos

Keys go on the user's authentication statement, with the type named:

```
configure
 set system login user admin authentication ssh-ed25519 "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5... you@laptop"
 commit
```

`ssh-rsa` and `ssh-ecdsa` are the other two. Junos takes the entire public key
line in quotes and validates it at `set`; a garbled line is refused there,
before the commit. Nothing changes on the device until `commit`, so this is
the platform where the *keep your session* rule is least at risk — but it
still applies.

### Arista EOS

The same shape as NX-OS, without the quotes:

```
configure
 username admin sshkey ssh-ed25519 AAAAC3NzaC1lZDI1NTE5... you@laptop
```

EOS accepts all three modern types. `show users accounts` shows the key.

### PAN-OS

The firewall takes public keys through the web interface rather than the CLI:
**Device → Administrators**, open the administrator, and import the `.pub`
file under **Public Key Authentication**. From the CLI the equivalent is `set
mgt-config users admin public-key` with the base64 of the *file*, which is
awkward enough that the web interface is the sensible route.

### Linux and anything OpenSSH-based

Append the public key line to `~/.ssh/authorized_keys` on the target, for the
user you log in as. From a machine that already has password access:

```
ssh-copy-id -i id_ed25519.pub admin@server
```

Or by hand, in a ShellMate session on the target:

```
mkdir -p ~/.ssh && chmod 700 ~/.ssh
echo 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5... you@laptop' >> ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys
```

The permissions are not optional. OpenSSH ignores an `authorized_keys` that
is group- or world-writable, or one in a `.ssh` directory that is, and says
nothing about it to the client. If a key that is definitely installed is
definitely refused, `chmod` is the first thing to check.

## Using a key for a connection

In the connection dialog, choose **SSH — key or jump host**. The password
form does not have the key fields; this one does.

**Private key file** — the path to the private half. **Browse** opens a file
dialog, or use **Use for a connection** in the key store, which fills the
path in for you.

**Username for this key** — the account the key belongs to, if it is not the
one in the main form. They are frequently different: a jump host that takes
your personal key under your own account, fronting devices that take a shared
service account. Given here, it wins.

**Key passphrase** — if the key has one. Left blank on an encrypted key,
ShellMate says the key is encrypted and the passphrase was missing rather
than failing generically.

**Password** — only if the device asks for one *after* accepting the key.
Cisco devices commonly take a key for the SSH layer and then prompt for an
enable or AAA password; fill in both and ShellMate answers both.

Save the connection and the key path is kept with it. A saved key-based
connection carries a key icon on the dashboard.

### Passphrases

A passphrase encrypts the private key file. Without one, anyone who can read
the file can use it — including whoever picks up the USB stick ShellMate is
running from. With one, the file is useless without the passphrase.

The passphrase is a credential and is treated exactly like a password. Type
it on each connect, or tick **Remember these credentials** and it goes into
the encrypted vault, filed against the saved connection. It is never written
to `profiles.json`, whatever you tick, and it never reaches the browser
afterwards: the interface sends a connection id, and the server fills the
passphrase in on its own side.

**Passphrase…** in the key store adds, changes or removes one on a key you
already have. Removing it is offered plainly, for the lab key nobody wants to
type for; the store says what that means at the point of choosing.

### Saving a key with a connection

What is saved is the **path**, not the key. Move the key file and the
connection stops working, with a message that says the file was not found.
Keys in `ShellMate-Data/keys/` travel with the data folder, which is the
reason for keeping them there rather than in `~/.ssh` on one particular
machine.

## Jump hosts with keys

A bastion is the most common place a key is *required*: the jump host takes
your personal key under your account, and the devices behind it take whatever
they take.

Fill in **Jump host / bastion**, and the jump host's own **Jump private key
file** and **Jump username** if they differ from the target's. ShellMate
connects to the bastion with those, opens a channel through it, and runs a
complete second SSH session to the target inside that channel — what OpenSSH
calls `ProxyJump`. The target's key, password or both go in the main fields
exactly as if there were no bastion.

The two authentications are independent. A key can be used for the bastion
and a password for the device, or the other way round, or the same key for
both. The traffic to the target is encrypted end to end; the bastion carries
it but cannot read it.

Left blank, the jump username reuses the target's. The jump password and key
do **not** fall back to the target's: a bastion given neither is tried with
the keys ShellMate finds in `~/.ssh`, and with the SSH agent if one is
running — which is exactly right for a bastion that already knows you, and a
puzzling failure otherwise. If the bastion wants a password, give it one.

### Keys ShellMate finds on its own

Like any SSH client, ShellMate will offer the keys it finds in `~/.ssh` —
`id_ed25519`, `id_rsa` and so on — when you have not named one. This is
useful on a workstation where the right key is already there, and a nuisance
on a device that answers a rejected key by closing the connection, so that the
password you also gave is never tried. ShellMate detects that case and says
so. The behaviour is **Try keys in ~/.ssh** in Stockton, the advanced
settings, and is off for any connection that has a password but no key.

## Troubleshooting

ShellMate reports what actually happened rather than listing what might have.
The messages below are the ones you will see, and what each one means.

**"Private key not found"** — the path is wrong, or the file has moved. Saved
connections hold a path, not the key. Point the connection at the file's new
home, or import the key into the key store so it travels with the data folder.

**"is encrypted and the passphrase was missing or incorrect"** — the key
loaded far enough to know it is encrypted, and the passphrase given did not
open it, or none was. A saved passphrase that has become wrong is a key whose
passphrase was changed since it was saved: forget the credential on the
connection and enter the new one. The key store's **Passphrase…** dialog
tells the two cases apart if you are unsure which you have — it says "needs a
passphrase" for a missing one and "does not open this key" for a wrong one.

**"Unsupported or malformed private key"** — the file is not an OpenSSH or PEM
private key. The usual causes:

- It is a PuTTY `.ppk`. Export it as an OpenSSH key from PuTTYgen.
- It is the **public** key. The private key has no `.pub` extension and begins
  `-----BEGIN OPENSSH PRIVATE KEY-----` or `-----BEGIN RSA PRIVATE KEY-----`.
- It was copied through something that changed line endings or added a byte
  order mark. Re-export it.

**"refused the key … and no password was given to fall back to"** — the device
saw the key and did not accept it. The key loaded fine on this side; the
problem is on the device:

- The public key is filed under a **different username** from the one you are
  logging in as. The *Username for this key* field is the usual fix.
- The paste was truncated or broken. Compare the device's stored hash with the
  **MD5** fingerprint in the key store; IOS shows it under `show running-config
  | section pubkey-chain`, ASA under `show running-config username`.
- The key type is one the device does not accept. IOS and ASA want RSA.
- On Linux, `~/.ssh` or `authorized_keys` has permissions OpenSSH refuses.
  `chmod 700 ~/.ssh; chmod 600 ~/.ssh/authorized_keys`.
- The device's SSH server does not have public-key authentication enabled at
  all. The message that starts "It will only accept:" lists what the device
  offered, and if a key is not in the list, no key will work until it is.

**"closed the connection after refusing a key, so the password … was never
tried"** — a discovered key from `~/.ssh` was offered first, the device
dropped the session rather than moving on to the password, and the password
was never tried. Either name the key the device expects, or turn off **Try
keys in ~/.ssh** in Stockton.

**"It will only accept: …"** — the device said what it would take. If
*a key* is not listed, public-key authentication is off on the device. If
*a password* is not listed and you have no key, the device is key-only and a
password will never work.

**The key works from a terminal but not from ShellMate** — almost always the
username. An OpenSSH `config` file may be supplying a different one, or a
different key, for that host. ShellMate reads neither, and uses exactly what
is in the dialog.

**OpenSSH on this machine refuses a key ShellMate made** — "permissions are
too open". ShellMate tightens the file's permissions as it is written, and
again on import, but a copy made afterwards inherits the permissions of
wherever it was copied to. Import the copy, or fix the permissions by hand.

The general rule for all of these: the key loaded, or it did not. If ShellMate
got as far as talking to the device, the key is fine and the device's
configuration is what to look at. See [Troubleshooting](#troubleshooting)
for problems that are not about keys.
