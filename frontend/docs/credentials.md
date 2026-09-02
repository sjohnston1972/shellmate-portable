# Credentials

API keys and any device passwords you choose to remember are encrypted, and
saved connections hold only the non-secret details needed to reconnect.

There is one exception, and it is one you have to ask for: a password can be
stored as readable text instead. **Settings → Credentials Vault** lists
everything saved, marks which of the two stores each one is in, and will move
a plaintext one into the vault.

## The vault

Two ways to protect it, switchable under **Settings → Credentials Vault**.

### Your Windows account (default)

Encrypted with your Windows login, using the same mechanism the operating
system uses for stored credentials. Nothing to remember and nothing to type.

A stolen USB stick is inert: the file cannot be decrypted by another user, or
on another machine, at all. The trade is that the vault does not travel — take
the stick to a different computer and the keys will not open there.

### A master password

Encrypted with a passphrase you type at startup. The vault works on any
machine, at the cost of entering it each launch.

**There is no recovery.** The key is derived from the passphrase and nothing
else, so a forgotten password means the stored keys are gone. There is no
reset and no backdoor. You are asked to confirm before switching to this mode
for exactly that reason.

Forgetting the password is not a lockout: ShellMate still starts, and you can
still reach every device. You simply type your keys and passwords by hand for
that session.

## Remembering a device password

Tick **Remember these credentials in the encrypted vault** when connecting.
The password goes into the vault, filed against that saved connection.

Once the connection succeeds — not before — ShellMate asks one more question:
whether to **give the credentials a name**. The same login usually covers more
than one device, and a named credential is one other connections can point at,
so naming it now is cheaper than discovering later that the same password is
saved forty times over. Leave the name blank and it stays with this connection
alone, which is the right answer for a one-off. The *Shared credentials*
section below explains what a name buys you.

Next time, leave the password field blank — the dialog shows a badge saying
where one is stored, **saved in vault** or **saved in plain text**, and offers
to forget it.

### Or in plain text

The second checkbox, **Remember these credentials in plain text**, writes the
password as readable text to `credentials-plaintext.json` in your data folder.
No encryption, no master password, nothing to forget.

Anyone who can open that folder can read the password. That includes anyone
who picks up the USB stick ShellMate is running from, and every backup the
folder is swept into. The dialog says so, in as many words, whenever the box
is ticked.

It exists because a vault you are locked out of is worse than no vault at all,
and because not every device password is worth protecting — a lab you rebuild
weekly is not the production estate. It is a deliberate choice, offered
plainly rather than hidden.

Ticking one checkbox unticks the other. A credential lives in one place; two
independent boxes would imply otherwise.

## Keys

This section is about where keys live and how they are protected. Which
kind to make, how to install the public half on each platform, jump hosts
and what the failure messages mean are on the [SSH keys](#ssh-keys) page.

### Making one

The **key** icon in the sidebar creates SSH keys, so nobody has to find
`ssh-keygen` or PuTTYgen first. Name it, press Create — Ed25519, which is the
right answer unless something refuses it.

**Type** offers the rest: RSA when the device is too old to accept anything
else, which plenty of network kit is, and ECDSA if a standard you have to meet
asks for it.

A **passphrase** encrypts the private key file itself. Without one, anyone who
can read your data folder can use the key — including anyone who picks up the
stick ShellMate is running from. ShellMate asks before making one without.

The **comment** ends up in the public key, and therefore in the device's
configuration. `you@laptop` is the convention; make it something you would
recognise a year from now on a switch you did not set up.

Keys live in `ShellMate-Data/keys/`, so they travel with the data folder. On
Windows the file's permissions are tightened as it is written — OpenSSH refuses
a private key other accounts can read, so a key made here works with `ssh.exe`
as well.

### Using one

Each key shows both fingerprints: **SHA256**, which modern OpenSSH prints, and
**MD5**, which a great deal of network kit still shows. Comparing what the
device says against what you hold is the whole point of a fingerprint, so both
are there with copy buttons.

**Copy public key** gives you the line to paste into the device.
**Use for a connection** fills it straight into the connection dialog.

You can also **change or remove a passphrase** on a key, or **import** one you
already have — copied in rather than referenced, so the list means one thing
and the permissions can be tightened. A key left where it is still works
through the connection dialog's path field.

### Pointing at one directly

Point the connection dialog at your private key file with **Browse**, which
opens the platform's own file dialog when ShellMate is running in its desktop
window, and its own file browser when you have opened it in a browser instead
— where the platform dialog does not exist at all. Typing a path still works.

A key can have its **own username**, separate from the one in the main form.
They are frequently different: a jump host that takes your personal key under
your own account, fronting devices that take a shared service account.

The key never leaves your machine. Only the signature it produces is sent, as
with any SSH client. Its passphrase is treated exactly like a password: kept
only if you ask, in whichever of the two places you chose, and never written
into `profiles.json`.

Saved passwords never reach the browser. The interface sends a connection id
and the server fills the credential in on its own side, so the secret exists
only on disk, encrypted, and in memory during the handshake.

Deleting a saved connection also forgets its password. Otherwise the secret
would be orphaned in the vault with nothing able to reach or remove it.

## API keys

The same vault holds keys for Anthropic, OpenAI, xAI and DeepSeek.

Keys can also come from a `.env` file next to the executable, which is how an
administrator hands out a preconfigured copy. The order of precedence is:

1. A key saved in Settings — stored in the vault
2. The matching variable in `.env`
3. Otherwise, that provider is unavailable

Keys left in plain text by an older version are moved into the vault
automatically on first run, and blanked from `settings.json` — but only once
the vault write has succeeded, so a failure cannot destroy the only copy.

Use **Test connections & refresh models** to check a key works. It asks the
provider what models it offers, which needs a valid key, costs nothing, and
tells you something useful when it succeeds.

## What this does and does not protect against

**Protects against:** a lost or stolen USB stick, someone reading files off
the disk, a copy of your data folder ending up somewhere it should not, and
credentials appearing in session logs you hand to a colleague.

**Does not protect against:** malware already running as you. It can ask
Windows to decrypt the vault exactly as ShellMate does. Full-disk encryption
remains worth having, and this is not a substitute for it.

## What is never stored in a saved connection

`profiles.json` is plain JSON by design, so it can be read, diffed and shared.
Four fields are stripped before anything is written, whatever the caller
passes: the password and key passphrase for the device, and the same two for
any jump host. They go to the vault, or to the plaintext file if you chose
that, or nowhere. A path to a key file is fine to store; the passphrase for it
is not.

Nothing sensitive comes back out of ShellMate's own API either. Connection
listings carry a "has a saved password" flag, never the password. That holds
for scripts exactly as it does for the interface.

## Shared credentials

A password normally belongs to one saved connection. That is right for one
device and wrong for forty: a lab, a stack, or everything a subnet scan turns
up usually shares a single login, and keeping a copy of it against each one
means forty entries to update the day it changes — with nothing recording that
they were ever the same password.

**Settings → Credentials Vault → Shared credentials** lets you name one. Give
it a name you will recognise later — *Lab admin*, *Core switches* — a username,
and a password. Connections then **point at it** rather than copying it, so
changing it here fixes every device using it at once. The naming prompt after
a successful connect creates exactly the same thing, from the credentials you
have just proved work.

Each one shows how many connections rely on it, which is what you want to know
before deleting it. Deleting a shared credential detaches everything using it,
so those connections ask for a password on next connect rather than silently
looking ready when there is nothing left to use.

**A device's own password always wins.** If one switch in the lab has had its
password changed, save that password against that connection and it takes
precedence over the shared one. Forget it again and the connection falls back
to the shared credential.

### Saving a scan with a credential attached

**Find Devices → Save as connections** asks once for a username and a
credential, and applies both to everything you ticked. Pick a shared credential
and the devices arrive ready to connect — clicking a tile opens a session
without asking for anything.

Choosing **None** is the right answer for somebody else's estate: the
connections are saved and each asks for a password the first time.
