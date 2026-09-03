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
import ipaddress
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
    path = _sets_file()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _save_sets(sets: list[dict]) -> None:
    path = _sets_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sets, indent=2), encoding="utf-8")


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
    out = []
    for entry in _load_sets():
        owner = set_owner(entry.get("id", ""))
        out.append({
            "id":       entry.get("id", ""),
            "name":     entry.get("name", ""),
            "username": entry.get("username", ""),
            "storage":  credential_storage(owner),
            "has_credentials": has_credentials(owner),
            "in_use":   sum(1 for p in profiles
                            if p.get("credential_ref") == entry.get("id")),
        })
    return out


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


def _read_credentials(owner: str) -> dict:
    """Read one owner's credentials directly, without resolving a reference."""
    out = {}
    for field in CREDENTIAL_FIELDS:
        value = vault.get(_credential_key(owner, field))
        if value:
            out[field] = value
    return out or _load_plaintext().get(owner, {})


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
    if plaintext.get(profile_id):
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
    if plaintext.get(profile_id):
        return "plaintext"

    reference = profile.get("credential_ref") or ""
    if not reference:
        return ""
    owner = set_owner(reference)
    if any(vault.has(_credential_key(owner, f)) for f in CREDENTIAL_FIELDS):
        return "vault"
    return "plaintext" if plaintext.get(owner) else ""


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
    return bool((plaintext if plaintext is not None else _load_plaintext()).get(owner))


def credential_storage(owner: str) -> str:
    """Where the credentials are kept: "vault", "plaintext" or ""."""
    resolved = owner if owner.startswith("set:") else _resolve_owner(owner)
    if any(vault.has(_credential_key(resolved, f)) for f in CREDENTIAL_FIELDS):
        return "vault"
    if _load_plaintext().get(resolved):
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
    if "tags" in cleaned:
        cleaned["tags"] = normalise_tags(cleaned["tags"])

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
    for profile_id in removed:
        forget_credentials(profile_id)
    return len(removed)
