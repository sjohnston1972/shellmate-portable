"""
profiles.py — Connection profile persistence.

Profiles are saved to profiles.json in the portable data directory (see
backend/paths.py) and hold no secrets whatsoever.

Credentials for a profile, when the user opts in to remembering them, go to
the encrypted vault under a namespaced key instead.  Keeping the two apart
means profiles.json stays a readable, shareable, diffable file while anything
sensitive is encrypted — and the split is enforced by SECRET_FIELDS below
rather than left to callers to remember.

Saved credentials are never sent to the browser.  The frontend passes a
profile id and the backend fills the credentials in server-side, so a
remembered password exists only on disk (encrypted) and in memory during the
handshake.
"""
import csv
import functools
import io
import ipaddress
import json
import threading
import uuid
from pathlib import Path

from backend import jsonfile, paths
from backend.vault import VaultError, vault

# Every change to a data file is a load → change → save cycle, and two of
# them at once lose an edit or the whole file (#457). One re-entrant lock
# per module around each public mutator; jsonfile adds the atomic write.
_lock = threading.RLock()


def _synchronised(fn):
    @functools.wraps(fn)
    def inner(*args, **kwargs):
        with _lock:
            return fn(*args, **kwargs)
    return inner


# The parsed profile list, kept until the file changes (#465). Every drag,
# tag change and dashboard refresh parsed the whole file — 8 ms at 5,000
# connections, several times per operation — and the scheduler did it once
# a minute forever. Callers get their own shallow copy (fresh dicts, fresh
# lists) because they add keys and replace tags on what they are handed.
_cache: dict = {"key": None, "profiles": None}


def _file_key(path: Path):
    try:
        st = path.stat()
    except OSError:
        return ("absent", str(path))
    return (str(path), st.st_mtime_ns, st.st_size)


def _copy(profiles: list[dict]) -> list[dict]:
    return [{k: (list(v) if isinstance(v, list) else v) for k, v in p.items()} for p in profiles]


def _load() -> list[dict]:
    path = paths.profiles_file()
    key = _file_key(path)
    with _lock:
        if _cache["key"] == key and _cache["profiles"] is not None:
            return _copy(_cache["profiles"])
        profiles = jsonfile.read(path, [], expect=list)
        _cache["key"] = _file_key(path)
        _cache["profiles"] = _copy(profiles)
        return profiles


def _save(profiles: list[dict]) -> None:
    path = paths.profiles_file()
    with _lock:
        # Compact on disk: indent=2 was most of the 26 ms write and a third
        # of the file. Nobody edits five thousand connections by hand.
        jsonfile.write(path, profiles, indent=None)
        _cache["key"] = _file_key(path)
        _cache["profiles"] = _copy(profiles)


def find_profile(profile_id: str) -> dict | None:
    """One saved connection by id, from the cached list. None if unknown."""
    return next((p for p in _load() if p.get("id") == profile_id), None)


# Credential fields that may be remembered for a profile. Mirrors
# SECRET_FIELDS — these are exactly the values stripped from the profile
# itself, redirected into the vault instead.
CREDENTIAL_FIELDS = (
    "password",
    "private_key_passphrase",
    "jump_password",
    "jump_private_key_passphrase",
)

#: Every secret that may be stored against a profile, login or not.
#:
#: The enable password (#532) is one ShellMate holds and is *not* one it can
#: log in with, so it is deliberately outside CREDENTIAL_FIELDS: a device with
#: an enable password and no login password must not report itself as ready to
#: connect, and a reconnect that re-saves the login credentials must not blank
#: it. Everything that works on a stored secret one field at a time — listing,
#: setting, forgetting, moving into the vault — works on this wider set,
#: because those all mean "a secret kept for this connection".
STORED_SECRETS = CREDENTIAL_FIELDS + ("enable_password",)


def _credential_key(owner: str, field: str) -> str:
    """
    Vault key for one credential.

    ``owner`` is a profile id, or ``set:<id>`` for a named credential shared
    by several connections. One namespace for both, so everything that reads
    or clears a credential works the same whichever it is.
    """
    return f"profile:{owner}:{field}"


# ---------------------------------------------------------------------------
# Named credentials
#
# A credential used to belong to exactly one profile, so "use the login I
# already have" could not be expressed at all. That is fine for one device and
# wrong for forty: a scan of a lab finds forty switches that share one login,
# and attaching a copy of the password to each would leave forty entries to
# update the day it changes, with nothing recording that they were ever the
# same credential.
#
# So a credential can have a name and an identity of its own, and a profile
# references it. Change it once and every device using it is fixed.
#
# The names live in their own file because they are not secret and the values
# are not in it — a set is a name and a username, and the password for it goes
# to the vault (or the plaintext file) under the set's id, through exactly the
# same functions a per-profile credential uses.
# ---------------------------------------------------------------------------


def _sets_file():
    return paths.data_dir() / "credential-sets.json"


def _load_sets() -> list[dict]:
    return jsonfile.read(_sets_file(), [], expect=list)


def _save_sets(sets: list[dict]) -> None:
    jsonfile.write(_sets_file(), sets)


def set_owner(set_id: str) -> str:
    """The credential-store owner key for a named set."""
    return f"set:{set_id}"


def resolve_set(set_id: str) -> dict:
    """
    Everything a connection needs from a named credential: its secrets **and
    its username**.

    The username lives on the set entry and the secrets live in the vault, so
    a caller reading one store gets half a credential. That is exactly what
    happened: session creation iterated CREDENTIAL_FIELDS — passwords and
    passphrases — applied the password, never looked at the username, and
    asked the device to log in as "". The failure read "refused the saved
    password for ." and blamed the password.

    One function returning both halves, so the next caller cannot take one and
    miss the other.
    """
    if not set_id:
        return {}

    entry = next((s for s in _load_sets() if s.get("id") == set_id), None)
    if entry is None:
        return {}

    resolved = dict(_read_credentials(set_owner(set_id)))
    username = (entry.get("username") or "").strip()
    if username:
        resolved["username"] = username
    return resolved


def credential_sets() -> list[dict]:
    """
    Every named credential, with no values.

    ``in_use`` is how the interface can warn before deleting one that forty
    connections are relying on.
    """
    profiles = _load()
    plaintext = _load_plaintext()          # once, not twice per set (#469)
    out = []
    for entry in _load_sets():
        owner = set_owner(entry.get("id", ""))
        out.append({
            "id":       entry.get("id", ""),
            "name":     entry.get("name", ""),
            "username": entry.get("username", ""),
            "storage":  credential_storage(owner, profiles, plaintext),
            "has_credentials": has_credentials(owner, profiles, plaintext),
            "in_use":   sum(1 for p in profiles
                            if p.get("credential_ref") == entry.get("id")),
        })
    return out


@_synchronised
def save_credential_set(name: str, username: str, password: str,
                        storage: str = "vault", set_id: str = "") -> dict:
    """
    Create or update a named credential.

    Raises:
        ValueError: No name, or the vault refused it. A set with no name is
            unusable — the name is the whole point of it being shared.
    """
    if not (name or "").strip():
        raise ValueError("A shared credential needs a name — that is how you "
                         "pick it later.")

    sets = _load_sets()
    entry = next((s for s in sets if s.get("id") == set_id), None) if set_id else None

    if entry is None:
        entry = {"id": str(uuid.uuid4()), "name": name.strip(),
                 "username": (username or "").strip()}
        sets.append(entry)
    else:
        entry["name"] = name.strip()
        entry["username"] = (username or "").strip()

    _save_sets(sets)

    if password:
        set_credential(set_owner(entry["id"]), "password", password, storage)

    return {**entry, "storage": credential_storage(set_owner(entry["id"]))}


@_synchronised
def delete_credential_set(set_id: str) -> int:
    """
    Forget a named credential, and detach it from anything using it.

    Returns how many profiles were left without a credential. Leaving a
    dangling reference behind would make those connections look ready when
    there is nothing for them to use.
    """
    sets = [s for s in _load_sets() if s.get("id") != set_id]
    _save_sets(sets)
    forget_credentials(set_owner(set_id))

    profiles = _load()
    detached = 0
    for profile in profiles:
        if profile.get("credential_ref") == set_id:
            profile.pop("credential_ref", None)
            detached += 1
    if detached:
        _save(profiles)
    return detached


@_synchronised
def attach_credential_set(profile_id: str, set_id: str) -> bool:
    """Point a profile at a named credential, or at nothing when set_id is ""."""
    profiles = _load()
    for profile in profiles:
        if profile.get("id") != profile_id:
            continue
        if set_id:
            profile["credential_ref"] = set_id
        else:
            profile.pop("credential_ref", None)
        _save(profiles)
        return True
    return False


def _resolve_owner(profile_id: str, profiles: list[dict] | None = None,
                   plaintext: dict | None = None) -> str:
    """
    Whose credentials a profile actually uses.

    A profile's own credentials win over a shared one. Somebody who set a
    password on this specific device meant it for this specific device — most
    obviously the one switch in the lab whose password was changed.
    """
    for profile in (profiles if profiles is not None else _load()):
        if profile.get("id") != profile_id:
            continue
        own = any(vault.has(_credential_key(profile_id, f)) for f in CREDENTIAL_FIELDS)
        if own or (plaintext if plaintext is not None else _load_plaintext()).get(profile_id):
            return profile_id
        reference = profile.get("credential_ref") or ""
        return set_owner(reference) if reference else profile_id
    return profile_id


def save_credentials(profile_id: str, values: dict) -> bool:
    """
    Remember a profile's credentials in the vault.

    Args:
        profile_id: Which profile these belong to.
        values:     Any of CREDENTIAL_FIELDS. Empty values clear that entry.

    Returns:
        True if anything was written. False when the vault is locked or
        unwritable — remembering a password is a convenience, so failing at it
        must never break connecting.
    """
    entries = {
        _credential_key(profile_id, field): values.get(field, "")
        for field in CREDENTIAL_FIELDS
    }
    if not any(entries.values()):
        return False
    try:
        vault.set_many(entries)
        return True
    except VaultError:
        return False


def load_credentials(profile_id: str) -> dict:
    """
    Return a profile's remembered credentials, or an empty dict.

    Resolves a shared credential when the profile references one, so nothing
    above this has to know whether the password belongs to this device alone
    or to the forty that came off the same scan.

    When it does resolve to a set, the set's **username** comes with it. The
    secrets live in the vault and the username lives on the set entry, so
    reading only the vault returns half a credential — which is how a saved
    connection using a shared login came to authenticate as "".
    """
    owner = _resolve_owner(profile_id)
    if owner.startswith("set:"):
        return resolve_set(owner[len("set:"):])
    return _read_credentials(owner)


def enable_password(profile_id: str) -> str:
    """
    The enable password saved for a profile, or "" (#532).

    Deliberately its own function rather than a field in
    :func:`load_credentials`. That result is spread onto ``ConnectionParams``
    at connect time, and params are scrubbed the moment authentication
    succeeds — so an enable password carried there would either be gone by
    the time the on-connect script needs it, or be kept alive in the one
    structure that exists to stop holding secrets. This reads the vault at
    the moment the line is typed and holds nothing.

    Resolves a shared credential the same way everything else does, so a set
    covering forty devices can carry the enable password for all of them.
    """
    owner = _resolve_owner(profile_id)
    value = vault.get(_credential_key(owner, "enable_password"))
    if value:
        return value
    return _load_plaintext().get(owner, {}).get("enable_password", "")


def _read_credentials(owner: str) -> dict:
    """Read one owner's credentials directly, without resolving a reference."""
    out = {}
    for field in CREDENTIAL_FIELDS:
        value = vault.get(_credential_key(owner, field))
        if value:
            out[field] = value
    return out or _load_plaintext().get(owner, {})


def _can_log_in_with(entry: dict | None) -> bool:
    """
    Whether a plaintext entry holds something to authenticate *with*.

    Not "is the entry non-empty". An entry may hold only an enable password
    (#532), which is a secret kept for the connection and not a way into it —
    and a tile that reports itself ready to connect and then asks for a
    password is a tile that lied.
    """
    return any((entry or {}).get(field) for field in CREDENTIAL_FIELDS)


def _has_for_profile(profile: dict, plaintext: dict) -> bool:
    """
    Whether this profile can connect without being asked, given the profile.

    Takes the record rather than an id on purpose. `_resolve_owner()` finds it
    by scanning the whole list, which is fine for one lookup and quadratic
    when the caller is already iterating every profile — listing 5,000
    connections was 25 million comparisons, and the loop had the record in its
    hand the entire time.
    """
    profile_id = profile.get("id", "")
    if any(vault.has(_credential_key(profile_id, f)) for f in CREDENTIAL_FIELDS):
        return True
    if _can_log_in_with(plaintext.get(profile_id)):
        return True

    reference = profile.get("credential_ref") or ""
    if not reference:
        return False
    owner = set_owner(reference)
    return _has_directly(owner, plaintext)


def _storage_for_profile(profile: dict, plaintext: dict) -> str:
    """Which store holds this profile's credentials, without re-scanning."""
    profile_id = profile.get("id", "")
    if any(vault.has(_credential_key(profile_id, f)) for f in CREDENTIAL_FIELDS):
        return "vault"
    if _can_log_in_with(plaintext.get(profile_id)):
        return "plaintext"

    reference = profile.get("credential_ref") or ""
    if not reference:
        return ""
    owner = set_owner(reference)
    if any(vault.has(_credential_key(owner, f)) for f in CREDENTIAL_FIELDS):
        return "vault"
    return "plaintext" if _can_log_in_with(plaintext.get(owner)) else ""


def has_credentials(owner: str, profiles: list[dict] | None = None,
                    plaintext: dict | None = None) -> bool:
    """
    True when any credential is remembered, either way.

    Takes a profile id or a set owner key. A profile id resolves through any
    shared credential it references, which is what makes a device saved from
    a scan report itself as ready to connect.
    """
    if owner.startswith("set:"):
        return _has_directly(owner, plaintext)
    return _has_directly(_resolve_owner(owner, profiles, plaintext), plaintext)


def _has_directly(owner: str, plaintext: dict | None = None) -> bool:
    if any(vault.has(_credential_key(owner, f)) for f in CREDENTIAL_FIELDS):
        return True
    store = plaintext if plaintext is not None else _load_plaintext()
    return _can_log_in_with(store.get(owner))


def credential_storage(owner: str, profiles: list[dict] | None = None,
                       plaintext: dict | None = None) -> str:
    """Where the credentials are kept: "vault", "plaintext" or ""."""
    resolved = owner if owner.startswith("set:") else _resolve_owner(owner, profiles, plaintext)
    if any(vault.has(_credential_key(resolved, f)) for f in CREDENTIAL_FIELDS):
        return "vault"
    if _can_log_in_with(
            (plaintext if plaintext is not None else _load_plaintext()).get(resolved)):
        return "plaintext"
    return ""


def forget_credentials(profile_id: str) -> None:
    """Remove every remembered credential for a profile, from both stores."""
    forget_many([profile_id])


@_synchronised
def forget_many(profile_ids) -> None:
    """
    Forget the credentials of several connections in one pass (#460): one
    vault write covering every key, and one plaintext write, rather than a
    full re-encryption of the vault per connection.
    """
    ids = [p for p in profile_ids if p]
    if not ids:
        return
    try:
        vault.set_many({_credential_key(pid, f): "" for pid in ids for f in STORED_SECRETS})
    except VaultError:
        pass
    data = _load_plaintext()
    dropped = False
    for pid in ids:
        if data.pop(pid, None) is not None:
            dropped = True
    if dropped:
        _write_plaintext(data)


# ---------------------------------------------------------------------------
# Plaintext credentials — opt-in, and deliberately not in profiles.json
#
# Some users would rather not deal with a vault at all. That is their call to
# make, but it does not get to weaken the guarantee profiles.json carries: that
# file is meant to be readable, shareable and diffable, and SECRET_FIELDS stops
# a credential reaching it whatever a caller passes.
#
# So plaintext credentials live in their own file, named for what it is. A user
# who finds `credentials-plaintext.json` in the data folder knows immediately
# what they are looking at, and profiles.json stays safe to send to a
# colleague. The file is written 0600 where the platform honours it.
# ---------------------------------------------------------------------------


def _plaintext_file():
    return paths.data_dir() / "credentials-plaintext.json"


def _load_plaintext() -> dict:
    return jsonfile.read(_plaintext_file(), {}, expect=dict)


def _write_plaintext(data: dict) -> None:
    # 0o600 is best effort: a no-op on Windows, where the ACL inherited from
    # the user's own data directory is what actually protects it.
    jsonfile.write(_plaintext_file(), data, mode=0o600)


@_synchronised
def save_plaintext_credentials(profile_id: str, values: dict) -> bool:
    """
    Write a profile's credentials to disk unencrypted, at the user's request.

    Returns True if anything was written. Storing nothing clears the entry, so
    unticking the option and reconnecting forgets what was there.
    """
    kept = {f: values.get(f, "") for f in CREDENTIAL_FIELDS if values.get(f)}
    data = _load_plaintext()

    # Anything stored here that is not a login credential survives (#532).
    # This function rewrites a profile's entry wholesale, so an enable
    # password saved from the credentials panel would have vanished the next
    # time somebody reconnected with "remember" ticked — losing a secret as a
    # side effect of saving one.
    other = {f: v for f, v in data.get(profile_id, {}).items()
             if f not in CREDENTIAL_FIELDS and v}

    if not kept and not other:
        data.pop(profile_id, None)
        _write_plaintext(data)
        return False

    data[profile_id] = {**other, **kept}
    _write_plaintext(data)
    return bool(kept)


@_synchronised
def _forget_plaintext(profile_id: str) -> None:
    data = _load_plaintext()
    if data.pop(profile_id, None) is not None:
        _write_plaintext(data)


# ---------------------------------------------------------------------------
# Managing what is saved
#
# Until now a remembered credential was invisible from the moment it was
# saved: nothing listed them, nothing changed one, and the only way to remove
# one was to find the connection it belonged to and reconnect with the box
# unticked. These are the pieces the Credentials Vault panel is built on.
#
# The grain is one *field*, not one profile. CREDENTIAL_FIELDS is four things —
# a password, a key passphrase, and the same two for a jump host — and a
# listing that showed only "password" would be lying about what is stored.
# ---------------------------------------------------------------------------

#: What each credential field is called on screen.
FIELD_LABELS = {
    "password":                    "Password",
    "private_key_passphrase":      "Key passphrase",
    "jump_password":               "Jump host password",
    "jump_private_key_passphrase": "Jump host key passphrase",
    # Not a login credential: what the on-connect script types at the
    # `Password:` an `enable` produces (#532).
    "enable_password":             "Enable password",
}


def credential_fields(profile_id: str,
                      plaintext_store: dict | None = None) -> dict[str, str]:
    """
    Which credentials are saved for a profile, and where each one lives.

    Returns field -> "vault" or "plaintext". A locked vault reports only the
    plaintext ones — see ``vault.is_locked()``; the caller has to say so on
    screen rather than presenting a short list as a complete one.

    ``plaintext_store`` lets a caller listing many profiles read the file
    once and share it (#328) — the same fix get_profiles() already carries;
    per-call loading here re-parsed credentials-plaintext.json once per row.
    """
    found: dict[str, str] = {}
    store = _load_plaintext() if plaintext_store is None else plaintext_store
    plaintext = store.get(profile_id, {})

    for field in STORED_SECRETS:
        if vault.has(_credential_key(profile_id, field)):
            found[field] = "vault"
        elif plaintext.get(field):
            found[field] = "plaintext"
    return found


def read_plaintext_credential(profile_id: str, field: str) -> str:
    """
    Return one credential that was saved unencrypted.

    Deliberately narrow. This is the only route by which a stored secret
    leaves the backend, and it reads the plaintext file and nothing else — a
    vault-backed credential is not readable here at any price, because a vault
    that decrypts on demand for the interface is most of the way to not being
    a vault.

    Showing a plaintext one protects nothing that is not already lost: the
    value is sitting in a JSON file the user can open. Refusing would only
    send them to the text editor.
    """
    if field not in STORED_SECRETS:
        raise ValueError(f"'{field}' is not a credential ShellMate stores.")
    return _load_plaintext().get(profile_id, {}).get(field, "")


@_synchronised
def set_credential(profile_id: str, field: str, value: str, storage: str) -> str:
    """
    Save or change one credential, in the store named.

    Writes to one store and clears the other, so changing where a credential
    lives cannot leave a stale copy behind in the place it moved out of —
    which for the plaintext file would mean a password the user believes they
    have encrypted still sitting readable on disk.

    Returns where it ended up, or "" if it was cleared.
    """
    if field not in STORED_SECRETS:
        raise ValueError(f"'{field}' is not a credential ShellMate stores.")

    if not value:
        forget_credential(profile_id, field)
        return ""

    if storage == "plaintext":
        data = _load_plaintext()
        data.setdefault(profile_id, {})[field] = value
        _write_plaintext(data)
        _clear_vault_credential(profile_id, field)
        return "plaintext"

    try:
        vault.set_many({_credential_key(profile_id, field): value})
    except VaultError as exc:
        raise ValueError(
            "The vault is locked, so the credential could not be saved."
        ) from exc

    data = _load_plaintext()
    if data.get(profile_id, {}).pop(field, None) is not None:
        if not data[profile_id]:
            data.pop(profile_id)
        _write_plaintext(data)
    return "vault"


@_synchronised
def forget_credential(profile_id: str, field: str) -> bool:
    """Remove one credential from wherever it is. True if anything went."""
    if field not in STORED_SECRETS:
        raise ValueError(f"'{field}' is not a credential ShellMate stores.")

    removed = _clear_vault_credential(profile_id, field)

    data = _load_plaintext()
    if data.get(profile_id, {}).pop(field, None) is not None:
        if not data[profile_id]:
            data.pop(profile_id)
        _write_plaintext(data)
        removed = True

    return removed


def _clear_vault_credential(profile_id: str, field: str) -> bool:
    """Blank one vault entry. A locked vault silently does nothing."""
    key = _credential_key(profile_id, field)
    try:
        if not vault.has(key):
            return False
        vault.set_many({key: ""})
        return True
    except VaultError:
        return False


@_synchronised
def move_to_vault(profile_id: str) -> list[str]:
    """
    Encrypt every plaintext credential belonging to a profile.

    The one action here that improves matters rather than merely exposing it.
    The reverse is deliberately not offered: there is no case where moving a
    credential *out* of encryption is the thing somebody needed a button for.

    Returns the fields moved. Empty when the vault is locked — the plaintext
    copy is left exactly where it was, because deleting it before the
    encrypted copy exists would lose the password outright.
    """
    stored = _load_plaintext().get(profile_id, {})
    if not stored:
        return []

    entries = {_credential_key(profile_id, f): v for f, v in stored.items()
               if f in STORED_SECRETS and v}
    if not entries:
        return []

    try:
        vault.set_many(entries)
    except VaultError:
        return []

    # Only now that the encrypted copy is written.
    data = _load_plaintext()
    data.pop(profile_id, None)
    _write_plaintext(data)
    return [f for f in stored if f in STORED_SECRETS and stored[f]]


@_synchronised
def forget_all_plaintext() -> int:
    """Empty the plaintext store. Returns how many profiles it held."""
    data = _load_plaintext()
    count = len(data)
    if count:
        _write_plaintext({})
    return count


# ---------------------------------------------------------------------------
# Identity — what makes two saved connections the same connection
#
# The frontend compared hostname, port and username exactly, which meant
# `SWITCH01` and `switch01 ` were two devices, and a profile renamed to its
# detected hostname no longer matched a fresh save of the same address. The
# comparison belongs here, next to the write, for the same reason SECRET_FIELDS
# does: every other caller then gets it for free.
# ---------------------------------------------------------------------------

#: The port a transport uses when the profile does not say. Compared after
#: filling in, so a profile saved with an explicit 22 and one saved with the
#: box left empty are recognised as the same device.
DEFAULT_PORTS = {"ssh": 22, "telnet": 23}


def identity(profile: dict) -> tuple:
    """
    A comparable identity for a saved connection.

    Deliberately includes the username. Connecting to one switch as `admin`
    and as `neteng` is two sets of credentials and frequently two levels of
    access; collapsing them would silently discard one of the passwords. Two
    tiles for one device is a smaller problem than a merge that loses a
    credential.

    The display name is deliberately *excluded*: `record_detected_hostname()`
    renames a profile the moment the device identifies itself, and a name that
    changes by itself cannot be part of what identity means.
    """
    kind = (profile.get("connection_type") or "ssh").strip().lower()

    if kind == "serial":
        # One cable, one port. The baud rate is part of it — the same port at
        # 9600 and at 115200 is usually two different devices over time, and
        # merging them would silently change the setting on one of them.
        return (
            kind,
            (profile.get("serial_port") or "").strip().upper(),
            int(profile.get("baudrate") or 9600),
            "",
        )

    host = (profile.get("hostname") or "").strip().lower()
    port = int(profile.get("port") or DEFAULT_PORTS.get(kind, 22))
    user = (profile.get("username") or "").strip().lower()
    return (kind, host, port, user)


def find_matching(fields: dict, profiles: list[dict] | None = None) -> dict | None:
    """Return an existing profile for the same connection, or None."""
    wanted = identity(fields)
    for profile in (profiles if profiles is not None else _load()):
        if identity(profile) == wanted:
            return profile
    return None


def _absorb(kept: dict, duplicate: dict, overwrite: bool = False) -> None:
    """
    Fill gaps in the surviving profile from the one being discarded.

    A duplicate is rarely a straight copy — one of the two usually has the
    detected hostname, or a name somebody typed, or a jump host. Dropping it
    whole would lose that, so anything the survivor does not already have is
    taken across. Nothing is overwritten unless ``overwrite`` is set, which
    the deliberate-save path uses: pressing **Save** on a connection that is
    already saved is an edit, and an edit that quietly does nothing is worse
    than a duplicate.
    """
    for key, value in duplicate.items():
        if key in ("id", "has_saved_credentials", "credential_storage"):
            continue
        if value in (None, "", [], {}):
            continue
        if overwrite or kept.get(key) in (None, "", [], {}):
            kept[key] = value


@_synchronised
def dedupe_existing() -> int:
    """
    Merge duplicates already sitting in profiles.json.

    Refusing new ones does not remove the ones anybody using ShellMate already
    has, so this exists to clean up after the fact.

    **Deliberately not called on read any more.** It was, and it meant listing
    connections deleted them — see `get_profiles()`. It is an action somebody
    chooses, which can then say what it merged, rather than a silent
    correction applied to data nobody asked it to touch.

    Which entry survives matters more than it looks: credentials are keyed by
    profile id, so keeping the wrong one loses the saved password and leaves
    the real one orphaned in the vault where no interface can ever reach it.
    The entry holding credentials therefore wins, and any credentials attached
    to the entries removed are forgotten rather than left behind.

    Returns the number of profiles removed.
    """
    profiles = _load()
    plaintext = _load_plaintext()
    if len(profiles) < 2:
        return 0

    groups: dict[tuple, list[dict]] = {}
    for profile in profiles:
        groups.setdefault(identity(profile), []).append(profile)

    if all(len(group) == 1 for group in groups.values()):
        return 0

    kept_ids: set[str] = set()
    dropped: list[str] = []

    for group in groups.values():
        if len(group) == 1:
            kept_ids.add(group[0].get("id", ""))
            continue

        # The record is in hand: no per-profile scan of the whole list (#469).
        with_credentials = [p for p in group if _has_for_profile(p, plaintext)]
        keeper = (with_credentials or group)[0]

        for other in group:
            if other is keeper:
                continue
            _absorb(keeper, other)
            dropped.append(other.get("id", ""))

        kept_ids.add(keeper.get("id", ""))

    survivors = [p for p in profiles if p.get("id", "") in kept_ids]
    _save(survivors)

    for profile_id in dropped:
        if profile_id:
            forget_credentials(profile_id)

    return len(dropped)



# ---------------------------------------------------------------------------
# Tags
#
# A saved connection was a flat record with no way to say that a device
# belongs with other devices — fine at a dozen, wrong at the scale the rest of
# ShellMate assumes. broadcast.concurrency goes to 500 and its tip talks about
# a fleet of two hundred.
#
# Tags rather than folders, because the useful groupings overlap: a device is
# both "glasgow" and "production" and "access", and any tree forces a choice
# between them. Nothing to migrate — absent means untagged.
# ---------------------------------------------------------------------------


def normalise_tags(value) -> list[str]:
    """
    Clean a list of tags: trimmed, lower-cased, de-duplicated, order kept.

    Lower-cased because "Production" and "production" being two groups is a
    distinction nobody means to make, and the first time it happens is the
    day somebody's guardrail does not fire.
    """
    if isinstance(value, str):
        value = value.split(",")
    out: list[str] = []
    for item in value or []:
        # A JavaScript value that reached here stringified rather than being
        # dropped. "null" and "undefined" are not tags anybody types, and
        # every tag is a group now — so one of these creates a junk group on
        # the dashboard that has to be deleted by hand. "none" is deliberately
        # not on this list: somebody may well mean it.
        if item is None or item is False:
            continue
        tag = " ".join(str(item).split()).strip().lower()
        if tag in ("null", "undefined", "nan", "[object object]"):
            continue
        if tag and tag not in out:
            out.append(tag)
    return out


def all_tags() -> list[dict]:
    """Every tag in use, with how many connections carry it."""
    counts: dict[str, int] = {}
    for profile in _load():
        for tag in normalise_tags(profile.get("tags")):
            counts[tag] = counts.get(tag, 0) + 1
    return [{"tag": tag, "count": count}
            for tag, count in sorted(counts.items())]


@_synchronised
def set_tags(profile_id: str, tags) -> list[str]:
    """Replace a connection's tags. Returns what was stored."""
    cleaned = normalise_tags(tags)
    profiles = _load()
    for profile in profiles:
        if profile.get("id") != profile_id:
            continue
        if cleaned:
            profile["tags"] = cleaned
        else:
            profile.pop("tags", None)
        _save(profiles)
        return cleaned
    return []


@_synchronised
def retag_many(renames: dict[str, str]) -> int:
    """
    Rewrite several tags across every connection in one pass (#327).

    An empty replacement removes the tag. One load and one save however many
    tags move: the per-profile set_tags() loop parsed and rewrote the whole
    file once per matching connection, which at the documented estate size is
    the 62-second class of mistake get_profiles()'s comment records.

    Returns how many connections were touched — each counted once, even when
    it carried several of the renamed tags.
    """
    if not renames:
        return 0
    profiles = _load()
    touched = 0
    for profile in profiles:
        tags = normalise_tags(profile.get("tags"))
        if not any(t in renames for t in tags):
            continue
        rewritten: list[str] = []
        for tag in tags:
            replacement = renames.get(tag, tag)
            if replacement and replacement not in rewritten:
                rewritten.append(replacement)
        if rewritten:
            profile["tags"] = rewritten
        else:
            profile.pop("tags", None)
        touched += 1
    if touched:
        _save(profiles)
    return touched


@_synchronised
def record_last_seen(seen: dict[str, float]) -> int:
    """
    Note when each connection was last found reachable (#538).

    One load and one save for the whole sweep, for the reason `retag_many()`
    records: a fifty-device group would otherwise rewrite profiles.json fifty
    times, once per device that answered.

    Only what answered is written. Absence of an entry is not evidence that a
    device is gone — a laptop off the VPN would rewrite the whole estate as
    unreachable — so a failed probe leaves the last known good time alone.

    Returns how many were updated.
    """
    if not seen:
        return 0
    profiles = _load()
    touched = 0
    for profile in profiles:
        when = seen.get(profile.get("id", ""))
        if when:
            profile["last_seen"] = round(float(when))
            touched += 1
    if touched:
        _save(profiles)
    return touched


def _clean_forward(spec: dict) -> dict | None:
    """One forward as stored: kind, listen_port, host, port — nothing else."""
    if not isinstance(spec, dict):
        return None
    kind = str(spec.get("kind", "local")).lower()
    if kind not in ("local", "dynamic", "remote"):
        return None
    try:
        listen_port = int(spec.get("listen_port", 0))
        port = int(spec.get("port", 0) or 0)
    except (TypeError, ValueError):
        return None
    if not 1 <= listen_port <= 65535:
        return None
    return {"kind": kind, "listen_port": listen_port,
            "host": str(spec.get("host", "")), "port": port}


@_synchronised
def set_forwards(profile_id: str, spec: dict, present: bool) -> list[dict]:
    """Add or drop one forward on a profile (#405)."""
    cleaned = _clean_forward(spec)
    profiles = _load()
    profile = next((p for p in profiles if p.get("id") == profile_id), None)
    if profile is None or cleaned is None:
        return []
    current = [f for f in (profile.get("forwards") or []) if _clean_forward(f)]
    current = [f for f in current if _clean_forward(f) != cleaned]
    if present:
        current.append(cleaned)
    profile["forwards"] = current
    _save(profiles)
    return current


@_synchronised
def replace_forwards(profile_id: str, forwards: list) -> dict:
    """Replace a profile's forwards outright."""
    profiles = _load()
    profile = next((p for p in profiles if p.get("id") == profile_id), None)
    if profile is None:
        raise ValueError("Profile not found")
    profile["forwards"] = [f for f in (_clean_forward(x) for x in forwards or []) if f]
    _save(profiles)
    return profile


def profiles_tagged(tag: str, include_nested: bool = False) -> list[dict]:
    """
    Every connection carrying a tag.

    Args:
        tag: The tag to match.
        include_nested: Also match tags *beneath* this one — `site-1` finds
            `site-1/access` (#207). Off by default, because most callers mean
            the exact tag and silently widening what "tagged" means would
            change every one of them.

    The nested test requires the separator, so `site-1` does not swallow
    `site-10`. That is the same rule the tree and the tab strip use, and it
    is the one thing that makes a prefix match safe here.
    """
    wanted = (tag or "").strip().lower()
    if not wanted:
        return []

    prefix = f"{wanted}/"

    def matches(tags: list[str]) -> bool:
        if wanted in tags:
            return True
        return include_nested and any(t.startswith(prefix) for t in tags)

    return [p for p in get_profiles()
            if matches(normalise_tags(p.get("tags")))]


#: How many lines an on-connect script may hold, and how long each may be.
#: Bounded because this types into a live session: a script is the first
#: thirty seconds of every connection, not a configuration push, and an
#: unbounded one pasted by accident would take a device apart a line at a
#: time. The push pipeline (#407) is where a long change belongs.
MAX_ON_CONNECT_LINES = 12
MAX_ON_CONNECT_LENGTH = 200


def clean_on_connect(lines) -> list[str]:
    """
    An on-connect script as stored: plain command lines, nothing else.

    Blank lines go, `#` comments go, control characters go — a stored line
    carrying a bare carriage return would send two commands from one row and
    the interface would announce one of them.
    """
    out: list[str] = []
    for raw in (lines or []):
        line = "".join(ch for ch in str(raw) if ch.isprintable()).strip()
        if not line or line.startswith("#"):
            continue
        out.append(line[:MAX_ON_CONNECT_LENGTH])
        if len(out) >= MAX_ON_CONNECT_LINES:
            break
    return out


def on_connect_for(profile_id: str) -> list[str]:
    """The on-connect script a saved connection carries, or []."""
    if not profile_id:
        return []
    profile = find_profile(profile_id)
    return clean_on_connect((profile or {}).get("on_connect"))


@_synchronised
def replace_on_connect(profile_id: str, lines) -> dict:
    """
    Replace a profile's on-connect script (#532).

    Its own endpoint rather than riding on a profile save, because a save
    merges and never overwrites with nothing — so clearing the script by
    emptying the box would silently do nothing.
    """
    profiles = _load()
    profile = next((p for p in profiles if p.get("id") == profile_id), None)
    if profile is None:
        raise ValueError("Profile not found")
    profile["on_connect"] = clean_on_connect(lines)
    _save(profiles)
    return profile


def get_profiles() -> list[dict]:
    """
    Return saved profiles, each flagged with whether credentials are stored.

    The flag is a boolean and never the credential itself — the UI only needs
    to know whether to ask for a password.

    **This does not deduplicate.** It used to open with `dedupe_existing()`,
    which merges profiles sharing an identity and rewrites the file — so
    listing connections deleted them. Five thousand connections to one lab
    address became one, and the 62 seconds it took was the 4,999 credential
    forgets on the way out.

    Merging on save is right and still happens, via `find_matching()`. Merging
    on *read* means any two connections to the same address, as the same user,
    over the same transport quietly become one the next time anything lists
    them — and a terminal server fronting fifty devices is one address. A read
    must never mutate.
    """
    profiles = _load()
    # Read once and shared, not re-read per profile. _resolve_owner() called
    # _load() for every connection, so listing 5,000 of them parsed a 1.6 MB
    # file 5,000 times — 63 seconds, and quadratic in the size of the estate.
    plaintext = _load_plaintext()
    for profile in profiles:
        profile["has_saved_credentials"] = _has_for_profile(profile, plaintext)
        # Which store, so the dialog can show the right option already ticked
        # and not quietly move a password from one to the other.
        profile["credential_storage"] = _storage_for_profile(profile, plaintext)
    return profiles


# Fields that must never be written to a profile, whatever the caller passes.
# Profiles are plain JSON on disk, so this is the last line of defence against
# a credential ending up there.
SECRET_FIELDS = {
    "password",
    "private_key_passphrase",
    "jump_password",
    "jump_private_key_passphrase",
    # The on-connect script is stored on the profile in plain sight, because
    # a list of commands is not a secret and being able to read it is the
    # point. The password it types at an `enable` prompt is (#532), and it
    # goes to the vault like every other one.
    "enable_password",
}


@_synchronised
def save_profile(fields: dict) -> dict:
    """
    Save a connection profile.

    Accepts the whole field set so serial and jump-host details persist
    alongside the SSH basics. Secrets are stripped rather than trusted to be
    absent — a path to a key file is fine to store, the passphrase for it is
    not.
    """
    cleaned = {k: v for k, v in fields.items() if k not in SECRET_FIELDS}
    if "tags" in cleaned:
        cleaned["tags"] = normalise_tags(cleaned["tags"])
    if "on_connect" in cleaned:
        cleaned["on_connect"] = clean_on_connect(cleaned["on_connect"])

    profiles = _load()

    # Refuse to create a second entry for a device already saved. The browser
    # checked for this and the Save button did not, which is the wrong way
    # round — the rule belongs where the write happens, so a script, a second
    # interface or a future caller cannot get it wrong.
    existing = find_matching(cleaned, profiles)
    if existing is not None:
        # A deliberate save is still an edit: take across anything new, such as
        # a name the user has just typed or a jump host they have added.
        before = dict(existing)
        _absorb(existing, cleaned, overwrite=True)
        if existing != before:
            _save(profiles)
        return {**existing, "already_saved": True}

    profile = {
        "id": str(uuid.uuid4()),
        **cleaned,
    }
    profile["name"] = cleaned.get("name") or cleaned.get("hostname") or cleaned.get("serial_port") or "unnamed"

    profiles.append(profile)
    _save(profiles)
    return profile


def _looks_like_an_address(name: str) -> bool:
    """Whether a profile name is a bare IPv4/IPv6 address rather than a name."""
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        return False


@_synchronised
def record_detected_hostname(target: str, port: int, username: str, detected: str) -> bool:
    """
    Note the device's real name against the profile used to reach it.

    Connecting by IP leaves a saved connection called "10.20.30.40", which
    tells you nothing on the welcome screen. The hostname is known a second
    after connecting, so use it.

    The connect target is deliberately *not* rewritten. A profile named
    "core-sw-01" that still dials the IP works everywhere; one that dials
    "core-sw-01" only works where that name resolves, which on a management
    network is
    frequently nowhere. So the display name changes and the address does not.

    The name is only filled in when the user has not chosen one — a profile
    someone deliberately called "Glasgow core, top of rack" is left alone.

    Returns:
        True if a profile was updated.
    """
    if not detected or not target:
        return False

    profiles = _load()
    changed = False

    for profile in profiles:
        if profile.get("hostname") != target:
            continue
        if int(profile.get("port") or 0) != int(port or 0):
            continue
        if username and profile.get("username") not in ("", username):
            continue

        # Read before writing (#322): assigning first made the comparison
        # below always False, so a profile with a user-chosen name recorded
        # the detection in memory only and _save() never ran.
        previous = profile.get("detected_hostname")
        profile["detected_hostname"] = detected

        # Only replace a name the user never chose: empty, the address
        # itself, anything that reads as a bare IP (#364 — "10.20.30.40" as
        # a name says nothing the address field does not), or the first
        # octet left behind by the scanner's naming bug (#363).
        current = (profile.get("name") or "").strip()
        replaceable = (
            current in ("", target, target.split(".")[0])
            or _looks_like_an_address(current)
        )
        if replaceable and current != detected:
            profile["name"] = detected
            changed = True
        elif previous != detected:
            changed = True

    if changed:
        _save(profiles)
    return changed



#: What a connection remembers about the device behind it (#536). Facts,
#: not settings: every one is something the device said about itself, so
#: they are overwritten by whatever it says next and never merged with a
#: user's own edits. `last_connected` is ShellMate's own observation.
INVENTORY_FIELDS = ("version", "model", "serial", "last_seen_platform", "last_connected")


@_synchronised
def record_inventory(target: str, port: int, username: str, facts: dict,
                     profile_id: str = "") -> bool:
    """
    Note what the device said it is, against the connection used to reach it.

    ``profile_id`` names it outright and is used whenever the session has
    one. The target match behind it is the fallback for a session opened
    straight from the dialog — and it is only a fallback, because an estate
    where a hundred devices sit behind one address (a terminal server, a
    lab behind one jump host) would otherwise have every one of them claim
    the serial number of whichever was opened. A name cannot be used at
    all: it is rewritten the moment the device says what it is called.

    Empty values are ignored rather than written, so a `show inventory` a
    device does not support cannot erase a serial number learned last week.

    Returns:
        True if a profile was updated.
    """
    if not target and not profile_id:
        return False
    clean = {k: str(v).strip() for k, v in (facts or {}).items()
             if k in INVENTORY_FIELDS and str(v or "").strip()}
    if not clean:
        return False

    profiles = _load()
    changed = False
    for profile in profiles:
        if profile_id:
            if profile.get("id") != profile_id:
                continue
        else:
            if profile.get("hostname") != target:
                continue
            if int(profile.get("port") or 0) != int(port or 0):
                continue
            if username and profile.get("username") not in ("", username):
                continue
        for key, value in clean.items():
            if profile.get(key) != value:
                profile[key] = value
                changed = True

    if changed:
        _save(profiles)
    return changed


def record_connected(target: str, port: int, username: str, when: str = "",
                     profile_id: str = "") -> bool:
    """When this connection was last opened (#536)."""
    import datetime as _dt
    stamp = when or _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    return record_inventory(target, port, username, {"last_connected": stamp}, profile_id)


def remember_platform(target: str, port: int, username: str, platform: str) -> bool:
    """
    Note what the user said a device is, against the profile used to reach it.

    Told directly, there is nothing left to be unsure about — `as_chosen()`
    carries confidence 1.0 and source "you", and calls itself the one source
    that is not a guess. Discarding it on disconnect while keeping every
    *guess* in the database is the wrong way round, and it lands hardest on
    exactly the devices the escape hatch exists for: a legal-warning banner
    and anything behind a terminal server are the ones automatic
    identification will never get right on its own.

    Matched by target rather than by name, for the same reason
    `record_detected_hostname()` is: the name is rewritten when the device
    says what it is called, and matching on something that changes by itself
    would stop finding the profile it just renamed.

    An empty platform clears it, which is how somebody takes back a decision
    they made in March.

    Returns:
        True if a profile was updated.
    """
    if not target:
        return False

    profiles = _load()
    changed = False

    for profile in profiles:
        if profile.get("hostname") != target:
            continue
        if int(profile.get("port") or 0) != int(port or 0):
            continue
        if username and profile.get("username") not in ("", username):
            continue

        if platform:
            if profile.get("platform") != platform:
                profile["platform"] = platform
                changed = True
        elif profile.pop("platform", None) is not None:
            changed = True

    if changed:
        _save(profiles)
    return changed


@_synchronised
def remembered_platform(hostname: str, port: int, username: str) -> str:
    """
    What the user last said this device is, or "".

    Also picks up the platform `discovery_save()` writes from a scan's SSH
    banner — which was stored and read by nothing until now.
    """
    if not hostname:
        return ""

    for profile in _load():
        if profile.get("hostname") != hostname:
            continue
        if port and int(profile.get("port") or 0) != int(port):
            continue
        if username and profile.get("username") not in ("", username):
            continue
        remembered = (profile.get("platform") or "").strip()
        if remembered:
            return remembered
    return ""


def delete_profile(profile_id: str) -> bool:
    """
    Delete a profile and any credentials remembered for it.

    Forgetting the credentials matters: without it, deleting a profile would
    leave orphaned secrets in the vault that no UI can ever reach or remove.
    """
    return _delete_where(lambda p: p.get("id") == profile_id) == 1


def delete_solely_tagged(tags) -> int:
    """
    Delete every connection whose tags all fall within ``tags`` — the ones a
    group deletion would otherwise leave in Ungrouped (#360).

    A connection that also carries a tag outside the set is evidently still
    wanted in that other group and survives untouched; an untagged connection
    was never in the group and survives too. Returns how many went.
    """
    doomed = set(normalise_tags(list(tags)))
    if not doomed:
        return 0

    def orphaned(profile: dict) -> bool:
        own = normalise_tags(profile.get("tags"))
        return bool(own) and all(t in doomed for t in own)

    return _delete_where(orphaned)


def delete_untagged() -> int:
    """Delete every connection that is in no group at all (#454)."""
    return _delete_where(lambda p: not normalise_tags(p.get("tags")))


@_synchronised
def _delete_where(predicate) -> int:
    """
    Remove every profile the predicate matches, and its credentials.

    One load and one save however many go (#327): deleting a group's twenty
    connections through delete_profile() would rewrite the file twenty times.
    Credentials are forgotten per profile because the vault is keyed that way.
    Returns how many were removed.
    """
    profiles = _load()
    kept = [p for p in profiles if not predicate(p)]
    removed = [p.get("id") for p in profiles if predicate(p)]
    if not removed:
        return 0
    _save(kept)
    forget_many(removed)
    return len(removed)


# ---------------------------------------------------------------------------
# The estate as a spreadsheet (#535)
#
# The only bulk route into ShellMate was the scanner, which means a team that
# already holds its estate in a spreadsheet or a monitoring export had to
# either type two hundred sites into the dialog or sweep every subnet to
# rediscover what it already knew.
#
# Two rules shape this and neither is negotiable:
#
# **A password column is refused, not stripped.** SECRET_FIELDS would quietly
# drop it and the import would report success, leaving somebody believing the
# passwords went in — and a file full of plaintext device passwords sitting in
# their downloads folder because ShellMate implied it was the right shape.
#
# **Every exported cell is neutralised against formula injection.** A cell
# beginning `=`, `+`, `-`, `@`, tab or carriage return is executed on open by
# Excel and Sheets, and a device name is not ours to trust — a connection
# named `=cmd|...` came from a scan of somebody else's network. This is the
# same lesson as #513 on the licence portal.
# ---------------------------------------------------------------------------

#: The columns, in the order they are written and assumed when a file has no
#: header row. `credential` is the *name* of a shared credential, never a value.
CSV_COLUMNS = ("name", "hostname", "port", "type", "username", "groups",
               "platform", "credential",
               # What the devices said about themselves (#536). Exported so a
               # "what version is everywhere" spreadsheet is one click; never
               # imported, because only the device may state them.
               "version", "model", "serial", "last_connected")

#: What a header cell may be called. Generous on purpose: the file usually
#: comes out of a monitoring tool or somebody's own spreadsheet, and refusing
#: "IP Address" because it is not "hostname" would send them to a text editor.
_CSV_ALIASES = {
    "name": "name", "display name": "name", "label": "name", "device": "name",
    "hostname": "hostname", "host": "hostname", "address": "hostname",
    "ip": "hostname", "ip address": "hostname", "target": "hostname",
    "port": "port", "tcp port": "port",
    "type": "type", "connection type": "type", "transport": "type",
    "protocol": "type",
    "username": "username", "user": "username", "login": "username",
    "groups": "groups", "group": "groups", "tags": "groups", "tag": "groups",
    "site": "groups",
    "platform": "platform", "os": "platform", "device type": "platform",
    "credential": "credential", "credentials": "credential",
    "credential set": "credential", "shared credential": "credential",
}

#: Words in a header that mean the file carries secrets. Matched as substrings
#: because "Enable Password" and "SSH passphrase" are the shapes these arrive
#: in, and the point is to stop rather than to be precise.
_CSV_REFUSED_WORDS = ("password", "passphrase", "secret")

#: A leading character a spreadsheet reads as the start of a formula.
_CSV_FORMULA_LEAD = ("=", "+", "-", "@", "\t", "\r")


def _csv_cell(value) -> str:
    """
    One cell, made safe to open in a spreadsheet.

    An apostrophe rather than a quote or an escape: Excel and Sheets both read
    a leading apostrophe as "this is text", show the value without it, and
    never evaluate it. Nothing else in either application does all three.
    """
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in _CSV_FORMULA_LEAD else text


def _csv_column(cell: str) -> str:
    """The column a header cell names, or "" if it names none of them."""
    cleaned = " ".join((cell or "").replace("_", " ").split()).strip().lower()
    return _CSV_ALIASES.get(cleaned, "")


def _read_csv(text: str) -> tuple[list[str], list[tuple[int, list[str]]]]:
    """
    Split pasted or uploaded text into columns and numbered rows.

    The delimiter is sniffed rather than assumed. A spreadsheet saved in a
    locale whose list separator is `;` exports semicolons, and every row would
    otherwise arrive as one unreadable field — reported as two hundred broken
    rows rather than as the one thing that is actually wrong.

    Raises:
        ValueError: Nothing usable, or a column carrying secrets.
    """
    if not (text or "").strip():
        raise ValueError("There is nothing to import.")

    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel

    rows = [(number, [cell.strip() for cell in row])
            for number, row in enumerate(csv.reader(io.StringIO(text), dialect), start=1)
            if any((cell or "").strip() for cell in row)]
    if not rows:
        raise ValueError("There is nothing to import.")

    first = rows[0][1]

    # Checked before anything else, and whatever the row turns out to be. A
    # file with a password column is refused outright: importing it and
    # silently discarding the column would leave somebody believing the
    # passwords are in ShellMate when they are only in the file.
    for cell in first:
        lowered = (cell or "").lower()
        if any(word in lowered for word in _CSV_REFUSED_WORDS):
            raise ValueError(
                f"This file has a '{cell}' column. ShellMate will not import "
                f"passwords from a spreadsheet — take the column out and import "
                f"the rest, then save the password against the connection or "
                f"point the rows at a shared credential by name.")

    mapped = [_csv_column(cell) for cell in first]
    # A header is a row whose cells are column *names*. Two hits is the
    # threshold: one could be a device genuinely called "name".
    if sum(1 for column in mapped if column) >= 2:
        rows = rows[1:]
        columns = mapped
    else:
        columns = list(CSV_COLUMNS)

    if not rows:
        raise ValueError("That is a header row and nothing else.")
    return columns, rows


def _row_to_profile(record: dict, sets: dict[str, str]) -> dict:
    """
    One CSV row as profile fields.

    Raises:
        ValueError: Said plainly, because it is shown against the row number.
    """
    kind = (record.get("type") or "ssh").strip().lower() or "ssh"
    if kind in ("serial", "console", "com"):
        raise ValueError("a serial connection names a COM port on one "
                         "particular machine, so it cannot be imported")
    if kind not in ("ssh", "telnet"):
        raise ValueError(f"'{kind}' is not a connection type ShellMate has")

    hostname = (record.get("hostname") or "").strip()
    if not hostname:
        raise ValueError("no hostname or address")

    port_text = (record.get("port") or "").strip()
    if port_text:
        try:
            port = int(port_text)
        except ValueError:
            raise ValueError(f"'{port_text}' is not a port number") from None
        if not 1 <= port <= 65535:
            raise ValueError(f"{port} is not a port number")
    else:
        port = DEFAULT_PORTS.get(kind, 22)

    fields = {
        "name":            (record.get("name") or "").strip() or hostname,
        "hostname":        hostname,
        "port":            port,
        "connection_type": kind,
        "username":        (record.get("username") or "").strip(),
        "tags":            normalise_tags(record.get("groups")),
        "platform":        (record.get("platform") or "").strip(),
        # So an imported connection can be told from one somebody typed, the
        # way the scanner's `discovered` flag does.
        "imported":        True,
    }

    credential = (record.get("credential") or "").strip()
    if credential:
        # Rejected with a reason rather than left unattached. A row that
        # imports "successfully" and then cannot connect is worse than one
        # that says which credential it could not find.
        set_id = sets.get(credential.lower())
        if not set_id:
            raise ValueError(f"there is no shared credential called '{credential}'")
        fields["credential_ref"] = set_id

    return fields


@_synchronised
def save_many(rows: list[dict]) -> dict:
    """
    Save many connections in one load and one save (#535).

    The shape `retag_many()` established, for the same reason: `save_profile()`
    parses and rewrites the whole file per connection, so importing two hundred
    sites through it would rewrite profiles.json two hundred times.

    A row matching something already saved fills its gaps and **adds** its
    groups rather than replacing them — the same "adding, never moving" rule
    the dashboard uses, so re-importing a spreadsheet with one extra column
    cannot silently empty a group somebody arranged by hand.

    Returns ``created`` and ``existing``, the profiles as stored.
    """
    profiles = _load()
    created: list[dict] = []
    existing: list[dict] = []

    for fields in rows:
        cleaned = {k: v for k, v in fields.items() if k not in SECRET_FIELDS}
        cleaned["tags"] = normalise_tags(cleaned.get("tags"))

        match = find_matching(cleaned, profiles)
        if match is not None:
            merged = normalise_tags(normalise_tags(match.get("tags")) + cleaned["tags"])
            _absorb(match, {k: v for k, v in cleaned.items() if k != "tags"})
            if merged:
                match["tags"] = merged
            existing.append(match)
            continue

        profile = {"id": str(uuid.uuid4()), **cleaned}
        if not profile.get("tags"):
            profile.pop("tags", None)
        profile["name"] = cleaned.get("name") or cleaned.get("hostname") or "unnamed"
        profiles.append(profile)
        created.append(profile)

    if created or existing:
        _save(profiles)
    return {"created": created, "existing": existing}


def import_csv(text: str, apply: bool = False) -> dict:
    """
    Read an estate out of a CSV, previewing it or saving it.

    ``apply=False`` is the preview the dialog shows before anything is
    written — "142 new, 17 already saved, 3 unreadable rows" — parsed by
    exactly the code that will do the work, so the numbers cannot disagree
    with the outcome.

    Raises:
        ValueError: The file as a whole is unusable: empty, or carrying a
            password column. A single bad *row* is not this — it is reported
            in ``rejected`` with its line number and why, and the rest go in.
    """
    columns, rows = _read_csv(text)
    sets = {(entry.get("name") or "").strip().lower(): entry.get("id", "")
            for entry in _load_sets()}

    prepared: list[dict] = []
    rejected: list[dict] = []
    for line, values in rows:
        record = {column: value for column, value in zip(columns, values) if column}
        try:
            prepared.append(_row_to_profile(record, sets))
        except ValueError as exc:
            rejected.append({"line": line, "why": str(exc),
                             "text": ", ".join(values)[:120]})

    result = {
        "columns":       [column for column in columns if column],
        "rows":          len(rows),
        "rejected":      rejected,
        "applied":       False,
        "new":           0,
        "already_saved": 0,
    }

    if apply:
        written = save_many(prepared)
        result.update(applied=True, new=len(written["created"]),
                      already_saved=len(written["existing"]))
        return result

    # Counted against what is saved *and* against the rest of the file: the
    # same device listed twice is one connection, not two, which is what
    # save_many() will make of it.
    profiles = _load()
    seen: set[tuple] = set()
    for fields in prepared:
        key = identity(fields)
        if key in seen or find_matching(fields, profiles) is not None:
            result["already_saved"] += 1
        else:
            result["new"] += 1
        seen.add(key)
    return result


def export_csv(group: str = "") -> str:
    """
    The estate, or one group and everything nested under it, as CSV.

    Never carries a secret: the credential column holds the *name* of a shared
    credential so the file can be re-imported and re-attached, and a
    per-connection password has no column at all. SECRET_FIELDS keeps them out
    of profiles.json in the first place; this keeps them out of the export
    even if one ever got there.
    """
    names = {entry.get("id", ""): entry.get("name", "") for entry in _load_sets()}
    wanted = (group or "").strip().lower()
    prefix = f"{wanted}/"

    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(CSV_COLUMNS)

    for profile in _load():
        tags = normalise_tags(profile.get("tags"))
        if wanted and not (wanted in tags or any(t.startswith(prefix) for t in tags)):
            continue

        kind = (profile.get("connection_type") or "ssh").strip().lower()
        serial = kind == "serial"
        writer.writerow([_csv_cell(value) for value in (
            profile.get("name", ""),
            profile.get("serial_port", "") if serial else profile.get("hostname", ""),
            "" if serial else (profile.get("port") or DEFAULT_PORTS.get(kind, 22)),
            kind,
            profile.get("username", ""),
            ",".join(tags),
            profile.get("platform", ""),
            names.get(profile.get("credential_ref") or "", ""),
            # What the device said about itself (#536).
            profile.get("version", ""),
            profile.get("model", ""),
            profile.get("serial", ""),
            profile.get("last_connected", ""),
        )])

    return out.getvalue()
