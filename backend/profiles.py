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


def get_profiles() -> list[dict]:
    """
    Return saved profiles, each flagged with whether credentials are stored.

    The flag is a boolean and never the credential itself — the UI only needs
    to know whether to ask for a password.
    """
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
