# Credentials

Nothing sensitive is written in plain text. API keys and any device passwords
you choose to remember are encrypted; saved connections hold only the
non-secret details needed to reconnect.

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

Next time, leave the password field blank — the dialog shows a **saved in
vault** badge so you know one is stored, and offers to forget it.

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
