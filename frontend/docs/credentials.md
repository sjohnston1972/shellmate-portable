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

Tick **Remember these credentials** when connecting. The password goes into
the vault, filed against that saved connection.

Next time, leave the password field blank — the dialog shows a **saved in
vault** badge so you know one is stored, and offers to forget it.

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

## What is never stored

- Passwords in saved connections — the file is plain JSON by design, so it can
  be read, diffed and shared.
- Key passphrases, in either place.
- Anything sensitive in a response from ShellMate's own API. Connection
  listings carry a "has a saved password" flag, never the password.
