"""
test_vault.py — Tests for the encrypted credentials vault.

The properties worth proving here are the ones whose failure is silent: a
secret still readable on disk, a tampered file decrypting anyway, or a mode
switch that leaves the old ciphertext in place.  None of those raise on their
own, so they are asserted directly.

Runs against a temporary data directory, so the real vault is never touched.

    python test_vault.py
"""

import json
import shutil
import sys
import tempfile
from pathlib import Path

from backend import paths

# Point every path helper at a scratch directory before anything reads them.
_TEMP_ROOT = Path(tempfile.mkdtemp(prefix="shellmate-vault-test-"))
paths._data_dir_cache = _TEMP_ROOT

from backend import profiles as profiles_module          # noqa: E402
from backend import settings_store                        # noqa: E402
from backend.vault import (                               # noqa: E402
    MODE_DPAPI, MODE_PASSWORD, Vault, VaultError, dpapi_available,
    password_decrypt, password_encrypt, vault,
)

passed = 0
failed: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    global passed
    if condition:
        passed += 1
        print(f"  OK  {name}")
    else:
        failed.append(f"{name}: {detail}")
        print(f"  FAIL {name}\n       {detail}")


def fresh_vault() -> tuple[Vault, Path]:
    """Return a Vault backed by its own empty directory."""
    directory = Path(tempfile.mkdtemp(prefix="shellmate-vault-"))
    paths._data_dir_cache = directory
    instance = Vault()
    return instance, directory


SECRET = "sk-ant-api03-DO-NOT-LEAK-THIS-VALUE"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_password_round_trip() -> None:
    """The portable backend must survive a full encrypt/decrypt cycle."""
    print("\n-- Master password encryption --")
    payload = password_encrypt(b"hello world", "correct horse battery staple")
    check("ciphertext is not the plaintext",
          b"hello world" not in json.dumps(payload).encode())

    recovered = password_decrypt(payload, "correct horse battery staple")
    check("decrypts with the right password", recovered == b"hello world",
          f"got {recovered!r}")

    check("rejects the wrong password",
          _raises(lambda: password_decrypt(payload, "wrong"), "Incorrect master password"))

    check("each encryption uses a fresh salt and nonce",
          password_encrypt(b"x", "p")["salt"] != password_encrypt(b"x", "p")["salt"])


def test_tamper_detection() -> None:
    """AES-GCM must reject a modified ciphertext rather than return garbage."""
    print("\n-- Tamper detection --")
    import base64

    payload = password_encrypt(b"hostname switch01", "pw")
    raw = bytearray(base64.b64decode(payload["ciphertext"]))
    raw[0] ^= 0xFF                                    # flip a bit
    payload["ciphertext"] = base64.b64encode(bytes(raw)).decode()

    check("a flipped bit is detected",
          _raises(lambda: password_decrypt(payload, "pw"), "altered"))


def test_dpapi_backend() -> None:
    """The default Windows backend stores and retrieves without a password."""
    print("\n-- DPAPI backend --")
    if not dpapi_available():
        print("  .. skipped (not Windows)")
        return

    instance, directory = fresh_vault()
    try:
        instance.set("anthropic_api_key", SECRET)
        check("mode is dpapi", instance.mode() == MODE_DPAPI, f"got {instance.mode()}")
        check("never locked", not instance.is_locked())
        check("reads back the secret", instance.get("anthropic_api_key") == SECRET)

        on_disk = (directory / "vault.json").read_text(encoding="utf-8")
        check("secret is not readable on disk", SECRET not in on_disk)
        check("key names are not readable on disk either",
              "anthropic_api_key" not in on_disk)

        # A separate instance proves it survives a restart, not just the cache.
        reopened = Vault()
        check("survives a restart", reopened.get("anthropic_api_key") == SECRET)

        instance.delete("anthropic_api_key")
        check("delete removes the entry", not Vault().has("anthropic_api_key"))
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_password_vault_locking() -> None:
    """A master-password vault must stay shut until the password is given."""
    print("\n-- Password vault locking --")
    instance, directory = fresh_vault()
    try:
        instance.set("openai_api_key", SECRET)          # starts as dpapi
        instance.set_mode(MODE_PASSWORD, "s3cret-passphrase")
        check("mode switched to password", instance.mode() == MODE_PASSWORD)
        check("secret survives the re-encryption",
              instance.get("openai_api_key") == SECRET)

        on_disk = (directory / "vault.json").read_text(encoding="utf-8")
        check("re-encrypted file holds no plaintext", SECRET not in on_disk)
        check("no DPAPI blob left behind", '"mode": "password"' in on_disk)

        reopened = Vault()
        check("a fresh instance reports locked", reopened.is_locked())
        check("locked vault yields nothing", reopened.get("openai_api_key") == "")
        check("wrong password is rejected",
              _raises(lambda: reopened.unlock("nope"), "Incorrect master password"))

        reopened.unlock("s3cret-passphrase")
        check("unlocks with the right password",
              reopened.get("openai_api_key") == SECRET)
        check("no longer reports locked", not reopened.is_locked())

        reopened.lock()
        check("lock() re-locks it", reopened.is_locked())
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_mode_switch_back() -> None:
    """Switching back to DPAPI must re-encrypt, not leave the old file."""
    print("\n-- Switching back to DPAPI --")
    if not dpapi_available():
        print("  .. skipped (not Windows)")
        return

    instance, directory = fresh_vault()
    try:
        instance.set("xai_api_key", SECRET)
        instance.set_mode(MODE_PASSWORD, "temporary")
        instance.set_mode(MODE_DPAPI)

        check("mode is dpapi again", instance.mode() == MODE_DPAPI)
        check("secret intact", instance.get("xai_api_key") == SECRET)
        check("a new instance needs no password", not Vault().is_locked())
        check("still no plaintext on disk",
              SECRET not in (directory / "vault.json").read_text(encoding="utf-8"))
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_settings_migration() -> None:
    """Plaintext keys from a pre-vault settings.json must move and be blanked."""
    print("\n-- Migration from plaintext settings.json --")
    directory = Path(tempfile.mkdtemp(prefix="shellmate-migrate-"))
    paths._data_dir_cache = directory
    vault.lock()
    try:
        (directory / "settings.json").write_text(json.dumps({
            "providers": {
                "anthropic_api_key": SECRET,
                "openai_api_key": "sk-openai-ALSO-SECRET",
                "ollama_host": "http://localhost:11434",
            }
        }), encoding="utf-8")

        moved = settings_store.migrate_plaintext_secrets()
        check("both keys migrated",
              set(moved) == {"anthropic_api_key", "openai_api_key"}, f"got {moved}")

        on_disk = (directory / "settings.json").read_text(encoding="utf-8")
        check("settings.json no longer holds the secret", SECRET not in on_disk)
        check("non-secret settings are untouched", "localhost:11434" in on_disk)

        check("key still resolves through get_effective",
              settings_store.get_effective("anthropic_api_key") == SECRET)
        check("non-secret still resolves",
              settings_store.get_effective("ollama_host") == "http://localhost:11434")

        check("migration is idempotent", settings_store.migrate_plaintext_secrets() == [])
    finally:
        vault.lock()
        shutil.rmtree(directory, ignore_errors=True)


def test_update_settings_diverts_secrets() -> None:
    """Saving settings must put secrets in the vault, never in settings.json."""
    print("\n-- update_settings diverts secrets --")
    directory = Path(tempfile.mkdtemp(prefix="shellmate-update-"))
    paths._data_dir_cache = directory
    vault.lock()
    try:
        settings_store.update_settings({"providers": {
            "deepseek_api_key": SECRET,
            "ollama_host": "http://box:11434",
        }})

        on_disk = (directory / "settings.json").read_text(encoding="utf-8")
        check("secret never written to settings.json", SECRET not in on_disk)
        check("non-secret written normally", "http://box:11434" in on_disk)
        check("secret readable through get_effective",
              settings_store.get_effective("deepseek_api_key") == SECRET)

        ui = settings_store.get_settings_for_ui()
        check("UI receives a mask, not the secret",
              ui["providers"]["deepseek_api_key"] == "•" * 8,
              f"got {ui['providers']['deepseek_api_key']!r}")
        check("UI is told a value is set",
              ui["providers_has_value"]["deepseek_api_key"] is True)
        check("secret appears nowhere in the UI payload",
              SECRET not in json.dumps(ui))

        # Re-saving with the mask must not overwrite the real key.
        settings_store.update_settings({"providers": {"deepseek_api_key": "•" * 8}})
        check("saving the mask leaves the key intact",
              settings_store.get_effective("deepseek_api_key") == SECRET)

        # An explicit empty value should clear it.
        settings_store.update_settings({"providers": {"deepseek_api_key": ""}})
        check("an empty value clears the key",
              settings_store.get_effective("deepseek_api_key") == "")
    finally:
        vault.lock()
        shutil.rmtree(directory, ignore_errors=True)


def test_profile_credentials() -> None:
    """Remembered device credentials live in the vault, not profiles.json."""
    print("\n-- Profile credentials --")
    directory = Path(tempfile.mkdtemp(prefix="shellmate-creds-"))
    paths._data_dir_cache = directory
    vault.lock()
    try:
        profile = profiles_module.save_profile({
            "name": "glasgow-core", "hostname": "10.20.30.40",
            "username": "neteng", "connection_type": "ssh",
            # Deliberately smuggled in: must be stripped.
            "password": "should-never-persist",
        })
        profile_id = profile["id"]

        on_disk = (directory / "profiles.json").read_text(encoding="utf-8")
        check("password never reaches profiles.json",
              "should-never-persist" not in on_disk, f"file: {on_disk}")

        check("no credentials remembered yet",
              not profiles_module.has_credentials(profile_id))

        profiles_module.save_credentials(profile_id, {
            "password": "hunter2", "jump_password": "bastion-pw",
        })
        check("credentials now flagged", profiles_module.has_credentials(profile_id))

        on_disk = (directory / "profiles.json").read_text(encoding="utf-8")
        check("credentials are not in profiles.json", "hunter2" not in on_disk)
        check("credentials are not readable in the vault file",
              "hunter2" not in (directory / "vault.json").read_text(encoding="utf-8"))

        loaded = profiles_module.load_credentials(profile_id)
        check("password round-trips", loaded.get("password") == "hunter2")
        check("jump password round-trips", loaded.get("jump_password") == "bastion-pw")

        listed = profiles_module.get_profiles()
        check("profile list flags saved credentials",
              listed[0]["has_saved_credentials"] is True)
        check("profile list contains no secret", "hunter2" not in json.dumps(listed))

        # Deleting the profile must not orphan its secrets in the vault.
        profiles_module.delete_profile(profile_id)
        check("deleting the profile forgets its credentials",
              not profiles_module.has_credentials(profile_id))
    finally:
        vault.lock()
        shutil.rmtree(directory, ignore_errors=True)


def test_api_never_leaks_secrets() -> None:
    """No endpoint may return a stored secret."""
    print("\n-- API surface --")
    from fastapi.testclient import TestClient

    directory = Path(tempfile.mkdtemp(prefix="shellmate-api-"))
    paths._data_dir_cache = directory
    vault.lock()
    try:
        from backend.app import app

        with TestClient(app) as client:
            client.post("/api/settings", json={"settings": {
                "providers": {"anthropic_api_key": SECRET}
            }})

            status = client.get("/api/vault/status").json()
            check("vault status reports it exists", status["exists"] is True)
            check("vault status leaks nothing", SECRET not in json.dumps(status))
            check("vault status lists no key names",
                  "anthropic_api_key" not in json.dumps(status))

            settings = client.get("/api/settings").json()
            check("GET /api/settings leaks nothing", SECRET not in json.dumps(settings))

            profile = client.post("/api/profiles", json={
                "name": "sw1", "hostname": "10.0.0.1", "username": "admin",
            }).json()
            client.post("/api/sessions", json={})   # ignored, just exercising the path

            listing = client.get("/api/profiles").json()
            check("GET /api/profiles leaks nothing", SECRET not in json.dumps(listing))

            check("forgetting credentials returns ok",
                  client.delete(f"/api/profiles/{profile['id']}/credentials").status_code == 200)
    finally:
        vault.lock()
        shutil.rmtree(directory, ignore_errors=True)


def test_locked_vault_degrades_gracefully() -> None:
    """A locked vault must not break ordinary settings reads."""
    print("\n-- Locked vault behaviour --")
    instance, directory = fresh_vault()
    try:
        instance.set("anthropic_api_key", SECRET)
        instance.set_mode(MODE_PASSWORD, "pw")

        locked = Vault()
        check("get() returns empty rather than raising", locked.get("anthropic_api_key") == "")
        check("has() returns False rather than raising", locked.has("anthropic_api_key") is False)
        check("keys() returns empty rather than raising", locked.keys() == [])
        check("writing while locked is refused",
              _raises(lambda: locked.set("x", "y"), "locked"))
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def _raises(fn, fragment: str) -> bool:
    """True if fn raises VaultError whose message contains *fragment*."""
    try:
        fn()
    except VaultError as exc:
        return fragment.lower() in str(exc).lower()
    except Exception:
        return False
    return False


def main() -> int:
    print("=" * 52)
    print("  Credentials vault tests")
    print("=" * 52)

    for test in (
        test_password_round_trip,
        test_tamper_detection,
        test_dpapi_backend,
        test_password_vault_locking,
        test_mode_switch_back,
        test_settings_migration,
        test_update_settings_diverts_secrets,
        test_profile_credentials,
        test_api_never_leaks_secrets,
        test_locked_vault_degrades_gracefully,
    ):
        try:
            test()
        except Exception as exc:
            failed.append(f"{test.__name__}: raised {type(exc).__name__}: {exc}")
            print(f"  FAIL {test.__name__} raised {type(exc).__name__}: {exc}")

    shutil.rmtree(_TEMP_ROOT, ignore_errors=True)

    print("\n" + "=" * 52)
    print(f"  {passed} passed  |  {len(failed)} failed")
    print("=" * 52)
    if failed:
        print("\nFAILURES:")
        for item in failed:
            print(f"  {item}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
