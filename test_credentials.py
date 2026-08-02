"""
test_credentials.py — The path from the credential picker to paramiko.

`test_vault.py` covers encryption and `test_profiles.py` covers storage.
Neither covers what happens *between* them — resolving a chosen credential
into the parameters a connection is made with — and every fault in the
credential cluster (#154 through #161) lived in exactly that gap.

The one that mattered most: a credential set keeps its secrets in the vault
and its **username on the set entry**, so a caller reading one store gets half
a credential. Session creation applied the password, never looked at the
username, and asked the device to log in as ``""``. The failure blamed the
password.

Run:  python test_credentials.py

Against a real device as well, when you have one to hand:

    LAB_HOST=... LAB_USER=... LAB_PW=... python test_credentials.py

Without those the live section is skipped and says so. It is skipped rather
than faked because a mock SSH server accepts whatever it is given, which is
precisely the thing that would not have caught any of this.
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backend import paths  # noqa: E402

_TEMP = Path(tempfile.mkdtemp(prefix="shellmate-creds-"))
paths._data_dir_cache = _TEMP
assert paths.data_dir() == _TEMP, (
    f"refusing to run: this would use {paths.data_dir()}")

from backend import profiles as pm  # noqa: E402

passed = 0
failed = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global passed, failed
    if condition:
        passed += 1
        print(f"  [ok]   {name}")
    else:
        failed += 1
        print(f"  [FAIL] {name}" + (f"  --  {detail}" if detail else ""))


def reset() -> None:
    """A clean store between sections."""
    for name in ("profiles.json", "credential-sets.json",
                 "credentials-plaintext.json", "vault.json"):
        path = _TEMP / name
        if path.exists():
            path.unlink()
    pm._load_sets.cache_clear() if hasattr(pm._load_sets, "cache_clear") else None


# ---------------------------------------------------------------------------
print("\nA credential set resolves to both halves")
# ---------------------------------------------------------------------------
for storage in ("vault", "plaintext"):
    reset()
    made = pm.save_credential_set(f"Lab {storage}", "steven", "s3cret",
                                  storage=storage)
    resolved = pm.resolve_set(made["id"])

    check(f"{storage}: the password resolves",
          resolved.get("password") == "s3cret", str(sorted(resolved)))
    # The one that was missing. A set's username lives on the entry, not in
    # the vault, so anything reading only CREDENTIAL_FIELDS gets half of it.
    check(f"{storage}: the username resolves too",
          resolved.get("username") == "steven",
          f"got {resolved.get('username')!r} — a blank here is the bug that "
          f"made a device refuse the login")


# ---------------------------------------------------------------------------
print("\nA connection referencing a set gets both halves")
# ---------------------------------------------------------------------------
reset()
shared = pm.save_credential_set("Shared", "steven", "s3cret", storage="vault")
profile = pm.save_profile({"name": "sw1", "hostname": "10.0.0.1", "port": 22,
                           "connection_type": "ssh"})
pm.attach_credential_set(profile["id"], shared["id"])

loaded = pm.load_credentials(profile["id"])
check("the password comes through", loaded.get("password") == "s3cret")
check("the username comes through", loaded.get("username") == "steven",
      f"got {loaded.get('username')!r}")

stored = next(p for p in pm.get_profiles() if p["id"] == profile["id"])
check("the reference is persisted on the profile",
      stored.get("credential_ref") == shared["id"], str(stored.get("credential_ref")))
check("and the profile reports having credentials",
      bool(stored.get("has_saved_credentials")))


# ---------------------------------------------------------------------------
print("\nA connection's own credential beats the shared one")
# ---------------------------------------------------------------------------
reset()
shared = pm.save_credential_set("Shared", "steven", "shared-pw", storage="vault")
profile = pm.save_profile({"name": "sw2", "hostname": "10.0.0.2", "port": 22,
                           "connection_type": "ssh"})
pm.attach_credential_set(profile["id"], shared["id"])
pm.save_credentials(profile["id"], {"password": "its-own"})

loaded = pm.load_credentials(profile["id"])
# Somebody who set a password on this specific device meant it for this
# device — most obviously the one switch whose password was changed.
check("its own password wins", loaded.get("password") == "its-own",
      str(loaded.get("password")))


# ---------------------------------------------------------------------------
print("\nChanging a set updates everything pointing at it")
# ---------------------------------------------------------------------------
reset()
shared = pm.save_credential_set("Shared", "steven", "before", storage="vault")
ids = []
for n in range(3):
    p = pm.save_profile({"name": f"sw{n}", "hostname": f"10.0.1.{n}", "port": 22,
                         "connection_type": "ssh"})
    pm.attach_credential_set(p["id"], shared["id"])
    ids.append(p["id"])

pm.save_credential_set("Shared", "steven", "after", storage="vault",
                       set_id=shared["id"])
check("all three follow the change",
      all(pm.load_credentials(i).get("password") == "after" for i in ids),
      str([pm.load_credentials(i).get("password") for i in ids]))
check("and all three follow the username",
      all(pm.load_credentials(i).get("username") == "steven" for i in ids))


# ---------------------------------------------------------------------------
print("\nForgetting plain-text credentials actually removes them")
# ---------------------------------------------------------------------------
reset()
profile = pm.save_profile({"name": "sw3", "hostname": "10.0.2.1", "port": 22,
                           "connection_type": "ssh"})
pm.save_plaintext_credentials(profile["id"], {"password": "readable"})
plain_file = _TEMP / "credentials-plaintext.json"
check("it was written in the clear", plain_file.exists() and "readable" in
      plain_file.read_text(encoding="utf-8"))

pm.forget_all_plaintext() if hasattr(pm, "forget_all_plaintext") else \
    pm._forget_plaintext(profile["id"])
remaining = plain_file.read_text(encoding="utf-8") if plain_file.exists() else "{}"
# The control whose whole purpose is removing readable secrets from disk must
# not report success and leave them there.
check("and it is gone afterwards", "readable" not in remaining, remaining[:80])


# ---------------------------------------------------------------------------
print("\nSecrets never leave the backend")
# ---------------------------------------------------------------------------
reset()
shared = pm.save_credential_set("Shared", "steven", "s3cret", storage="vault")
listed = pm.credential_sets()
blob = repr(listed)
check("the set list carries no password", "s3cret" not in blob, blob[:120])
check("but does say it has one",
      any(s.get("has_credentials") for s in listed), blob[:120])

profile = pm.save_profile({"name": "sw4", "hostname": "10.0.3.1", "port": 22,
                           "connection_type": "ssh", "password": "typed-pw"})
saved_blob = (_TEMP / "profiles.json").read_text(encoding="utf-8")
check("a password is never written to profiles.json",
      "typed-pw" not in saved_blob)


# ---------------------------------------------------------------------------
# The live section. Skipped without a device rather than faked.
# ---------------------------------------------------------------------------
HOST = os.environ.get("LAB_HOST")
USER = os.environ.get("LAB_USER")
PW = os.environ.get("LAB_PW")

if not (HOST and USER and PW):
    print("\nLive device pass: skipped (set LAB_HOST, LAB_USER, LAB_PW)")
else:
    print(f"\nLive device pass against {HOST}")
    from backend.connections.base import ConnectionParams, ConnectionError_
    from backend.connections.ssh_handler import SSHHandler

    def connect(credential_ref="", profile_id="", **kw):
        """What app.py does when it builds a session."""
        params = ConnectionParams(connection_type="ssh", hostname=HOST,
                                  port=22, **kw)
        if credential_ref and not kw.get("password"):
            for field, value in pm.resolve_set(credential_ref).items():
                if not getattr(params, field, ""):
                    setattr(params, field, value)
        if profile_id:
            for field, value in pm.load_credentials(profile_id).items():
                if not getattr(params, field, ""):
                    setattr(params, field, value)
        handler = SSHHandler(params=params)
        try:
            handler.connect()
            return handler.is_connected
        finally:
            try:
                handler.disconnect()
            except Exception:
                pass

    reset()
    try:
        check("typed credentials connect", connect(username=USER, password=PW))
    except ConnectionError_ as exc:
        check("typed credentials connect", False, str(exc)[:90])

    for storage in ("vault", "plaintext"):
        reset()
        made = pm.save_credential_set(f"Lab {storage}", USER, PW, storage=storage)
        try:
            check(f"{storage}: a new connection by reference",
                  connect(credential_ref=made["id"]))
        except ConnectionError_ as exc:
            check(f"{storage}: a new connection by reference", False, str(exc)[:90])

        p = pm.save_profile({"name": f"lab-{storage}", "hostname": HOST,
                             "port": 22, "connection_type": "ssh"})
        pm.attach_credential_set(p["id"], made["id"])
        try:
            check(f"{storage}: a saved connection by reference",
                  connect(profile_id=p["id"]))
        except ConnectionError_ as exc:
            check(f"{storage}: a saved connection by reference", False, str(exc)[:90])


print("\n" + "=" * 52)
print(f"  {passed} passed  |  {failed} failed")
print("=" * 52)

sys.exit(1 if failed else 0)
