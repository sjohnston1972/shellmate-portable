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
import json
import uuid

from backend import paths
from backend.vault import VaultError, vault


def _load() -> list[dict]:
    profiles_file = paths.profiles_file()
    if not profiles_file.exists():
        return []
    try:
        return json.loads(profiles_file.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save(profiles: list[dict]) -> None:
    profiles_file = paths.profiles_file()
    profiles_file.parent.mkdir(parents=True, exist_ok=True)
    profiles_file.write_text(json.dumps(profiles, indent=2), encoding="utf-8")


# Credential fields that may be remembered for a profile. Mirrors
# SECRET_FIELDS — these are exactly the values stripped from the profile
# itself, redirected into the vault instead.
CREDENTIAL_FIELDS = (
    "password",
    "private_key_passphrase",
    "jump_password",
    "jump_private_key_passphrase",
)


def _credential_key(profile_id: str, field: str) -> str:
    """Vault key for one credential belonging to one profile."""
    return f"profile:{profile_id}:{field}"


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
    """Return a profile's remembered credentials, or an empty dict."""
    out = {}
    for field in CREDENTIAL_FIELDS:
        value = vault.get(_credential_key(profile_id, field))
        if value:
            out[field] = value
    return out or _load_plaintext().get(profile_id, {})


def has_credentials(profile_id: str) -> bool:
    """True when any credential is remembered for this profile, either way."""
    if any(vault.has(_credential_key(profile_id, f)) for f in CREDENTIAL_FIELDS):
        return True
    return bool(_load_plaintext().get(profile_id))


def credential_storage(profile_id: str) -> str:
    """Where this profile's credentials are kept: "vault", "plaintext" or ""."""
    if any(vault.has(_credential_key(profile_id, f)) for f in CREDENTIAL_FIELDS):
        return "vault"
    if _load_plaintext().get(profile_id):
        return "plaintext"
    return ""


def forget_credentials(profile_id: str) -> None:
    """Remove every remembered credential for a profile, from both stores."""
    try:
        vault.set_many({_credential_key(profile_id, f): "" for f in CREDENTIAL_FIELDS})
    except VaultError:
        pass
    _forget_plaintext(profile_id)


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
    path = _plaintext_file()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_plaintext(data: dict) -> None:
    path = _plaintext_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    try:
        # Best effort: no-op on Windows, where the ACL inherited from the
        # user's own data directory is what actually protects it.
        path.chmod(0o600)
    except OSError:
        pass


def save_plaintext_credentials(profile_id: str, values: dict) -> bool:
    """
    Write a profile's credentials to disk unencrypted, at the user's request.

    Returns True if anything was written. Storing nothing clears the entry, so
    unticking the option and reconnecting forgets what was there.
    """
    kept = {f: values.get(f, "") for f in CREDENTIAL_FIELDS if values.get(f)}
    data = _load_plaintext()

    if not kept:
        data.pop(profile_id, None)
        _write_plaintext(data)
        return False

    data[profile_id] = kept
    _write_plaintext(data)
    return True


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
}


def credential_fields(profile_id: str) -> dict[str, str]:
    """
    Which credentials are saved for a profile, and where each one lives.

    Returns field -> "vault" or "plaintext". A locked vault reports only the
    plaintext ones — see ``vault.is_locked()``; the caller has to say so on
    screen rather than presenting a short list as a complete one.
    """
    found: dict[str, str] = {}
    plaintext = _load_plaintext().get(profile_id, {})

    for field in CREDENTIAL_FIELDS:
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
    if field not in CREDENTIAL_FIELDS:
        raise ValueError(f"'{field}' is not a credential ShellMate stores.")
    return _load_plaintext().get(profile_id, {}).get(field, "")


def set_credential(profile_id: str, field: str, value: str, storage: str) -> str:
    """
    Save or change one credential, in the store named.

    Writes to one store and clears the other, so changing where a credential
    lives cannot leave a stale copy behind in the place it moved out of —
    which for the plaintext file would mean a password the user believes they
    have encrypted still sitting readable on disk.

    Returns where it ended up, or "" if it was cleared.
    """
    if field not in CREDENTIAL_FIELDS:
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


def forget_credential(profile_id: str, field: str) -> bool:
    """Remove one credential from wherever it is. True if anything went."""
    if field not in CREDENTIAL_FIELDS:
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
               if f in CREDENTIAL_FIELDS and v}
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
    return [f for f in stored if f in CREDENTIAL_FIELDS and stored[f]]


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


def dedupe_existing() -> int:
    """
    Merge duplicates already sitting in profiles.json.

    Refusing new ones does not remove the ones anybody using ShellMate already
    has. This runs when the list is read and writes only when it actually
    removes something.

    Which entry survives matters more than it looks: credentials are keyed by
    profile id, so keeping the wrong one loses the saved password and leaves
    the real one orphaned in the vault where no interface can ever reach it.
    The entry holding credentials therefore wins, and any credentials attached
    to the entries removed are forgotten rather than left behind.

    Returns the number of profiles removed.
    """
    profiles = _load()
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

        with_credentials = [p for p in group if has_credentials(p.get("id", ""))]
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


def get_profiles() -> list[dict]:
    """
    Return saved profiles, each flagged with whether credentials are stored.

    The flag is a boolean and never the credential itself — the UI only needs
    to know whether to ask for a password.
    """
    dedupe_existing()

    profiles = _load()
    for profile in profiles:
        profile_id = profile.get("id", "")
        profile["has_saved_credentials"] = has_credentials(profile_id)
        # Which store, so the dialog can show the right option already ticked
        # and not quietly move a password from one to the other.
        profile["credential_storage"] = credential_storage(profile_id)
    return profiles


# Fields that must never be written to a profile, whatever the caller passes.
# Profiles are plain JSON on disk, so this is the last line of defence against
# a credential ending up there.
SECRET_FIELDS = {
    "password",
    "private_key_passphrase",
    "jump_password",
    "jump_private_key_passphrase",
}


def save_profile(fields: dict) -> dict:
    """
    Save a connection profile.

    Accepts the whole field set so serial and jump-host details persist
    alongside the SSH basics. Secrets are stripped rather than trusted to be
    absent — a path to a key file is fine to store, the passphrase for it is
    not.
    """
    cleaned = {k: v for k, v in fields.items() if k not in SECRET_FIELDS}

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

        profile["detected_hostname"] = detected

        # Only replace a name that is still just the address.
        current = (profile.get("name") or "").strip()
        if current in ("", target) and current != detected:
            profile["name"] = detected
            changed = True
        elif profile.get("detected_hostname") != detected:
            changed = True

    if changed:
        _save(profiles)
    return changed


def delete_profile(profile_id: str) -> bool:
    """
    Delete a profile and any credentials remembered for it.

    Forgetting the credentials matters: without it, deleting a profile would
    leave orphaned secrets in the vault that no UI can ever reach or remove.
    """
    profiles = _load()
    new_profiles = [p for p in profiles if p.get("id") != profile_id]
    if len(new_profiles) == len(profiles):
        return False
    _save(new_profiles)
    forget_credentials(profile_id)
    return True
