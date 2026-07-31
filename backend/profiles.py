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
    return out


def has_credentials(profile_id: str) -> bool:
    """True when any credential is remembered for this profile."""
    return any(vault.has(_credential_key(profile_id, f)) for f in CREDENTIAL_FIELDS)


def forget_credentials(profile_id: str) -> None:
    """Remove every remembered credential for a profile."""
    try:
        vault.set_many({_credential_key(profile_id, f): "" for f in CREDENTIAL_FIELDS})
    except VaultError:
        pass


def get_profiles() -> list[dict]:
    """
    Return saved profiles, each flagged with whether credentials are stored.

    The flag is a boolean and never the credential itself — the UI only needs
    to know whether to ask for a password.
    """
    profiles = _load()
    for profile in profiles:
        profile["has_saved_credentials"] = has_credentials(profile.get("id", ""))
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
