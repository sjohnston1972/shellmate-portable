"""
test_licence.py — Licence keys verify offline and mean what they say (#446).

A key the private half did not sign must never pass; a tampered payload must
never pass; expiry, grace and revocation must each read the way the manual
says. The private key here is a throwaway generated for the test — the real
one lives only in the licence service.

    python test_licence.py
"""

import base64
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from backend import paths

_TEMP = Path(tempfile.mkdtemp(prefix="shellmate-licence-"))
paths._data_dir_cache = _TEMP

from backend import licence                                              # noqa: E402

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


PRIV = ed25519.Ed25519PrivateKey.generate()
PRIV_B64 = base64.b64encode(PRIV.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                                 serialization.NoEncryption())).decode()
PUB_B64 = base64.b64encode(PRIV.public_key().public_bytes(serialization.Encoding.Raw,
                                                           serialization.PublicFormat.Raw)).decode()
OTHER = ed25519.Ed25519PrivateKey.generate()
OTHER_B64 = base64.b64encode(OTHER.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                                  serialization.NoEncryption())).decode()


def day(offset: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=offset)).date().isoformat()


def make(**over) -> str:
    payload = {"id": "lic-test01", "kind": "person", "licensee": "Test Engineer", "email": "t@example.com",
               "seats": 1, "issued": day(-1), "expires": day(30), "grace_days": 14, "features": ["updates"]}
    payload.update(over)
    return licence.sign(payload, PRIV_B64)


def test_verification() -> None:
    print("\n-- Verification --")
    token = make()
    lic = licence.parse(token, PUB_B64)
    check("a signed key parses", lic.licensee == "Test Engineer" and lic.kind == "person")
    check("  and keeps its token for refresh", lic.token == token)
    check("the shipped public key is a real Ed25519 key",
          licence.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).__len__() == 32)
    forged = licence.sign({"id": "x", "kind": "person", "licensee": "Forger"}, OTHER_B64)
    check("a key signed by someone else is refused",
          _fails(lambda: licence.parse(forged, PUB_B64), "signature"))
    head, payload, sig = token.split(".")
    tampered = ".".join([head, payload[:-2] + ("AA" if payload[-2:] != "AA" else "BB"), sig])
    check("a tampered payload is refused", _fails(lambda: licence.parse(tampered, PUB_B64), ""))
    check("a truncated key is refused", _fails(lambda: licence.parse(token[:40], PUB_B64), "three parts"))
    check("nonsense is refused", _fails(lambda: licence.parse("hello", PUB_B64), "three parts"))
    check("a key with a bad kind is refused",
          _fails(lambda: licence.parse(make(kind="sideways"), PUB_B64), "kind"))
    check("whitespace and line breaks in a pasted key are tolerated",
          licence.parse(token[:30] + "\n " + token[30:], PUB_B64).id == "lic-test01")


def test_status() -> None:
    print("\n-- What a key means today --")
    active = licence.parse(make(), PUB_B64)
    s = licence.status(active)
    check("a current key is active and valid", s["valid"] and s["state"] == "active", str(s))
    check("  and says who and until when", "Test Engineer" in s["detail"])
    forever = licence.parse(make(expires=""), PUB_B64)
    check("no expiry is perpetual", licence.status(forever)["state"] == "active" and "no expiry" in licence.status(forever)["detail"])
    graced = licence.parse(make(expires=day(-3), grace_days=14), PUB_B64)
    s = licence.status(graced)
    check("three days past expiry is grace, still valid", s["valid"] and s["state"] == "grace" and s["days_left"] == 11, str(s))
    dead = licence.parse(make(expires=day(-20), grace_days=14), PUB_B64)
    s = licence.status(dead)
    check("past the grace period is expired and not valid", not s["valid"] and s["state"] == "expired", str(s))
    check("no key at all is 'none' and says the app still works",
          licence.status(None)["state"] == "none" and "works without one" in licence.status(None)["detail"])


def test_storage_and_features() -> None:
    print("\n-- Installing and removing --")
    licence.PUBLIC_KEY_B64, saved = PUB_B64, licence.PUBLIC_KEY_B64
    try:
        check("nothing installed to start", licence.load() is None and not licence.has_feature("updates"))
        installed = licence.install(make())
        check("a key installs to licence.key", licence.key_file().exists() and installed.id == "lic-test01")
        check("  and loads back verified", licence.load().licensee == "Test Engineer")
        check("  and the updates feature is on", licence.has_feature("updates"))
        check("  but a feature it does not carry is not", not licence.has_feature("teleport"))
        check("a bad key is refused and the good one kept",
              _fails(lambda: licence.install("SM1.bad.bad"), "") and licence.load() is not None)
        licence._write_state({"revoked": "test"})
        s = licence.status()
        check("a locally-recorded revocation wins", not s["valid"] and s["state"] == "revoked", str(s))
        check("installing a key again clears the revocation", licence.install(make()) and licence.status()["state"] == "active")
        check("removal empties the slot", licence.remove() and licence.load() is None)
        check("removing twice is not an error", licence.remove() is False)
        m = licence.machine_info()
        check("the machine record has a stable 16-char id and the version",
              len(m["id"]) == 16 and m["id"] == licence.machine_info()["id"] and m["version"] and m["hostname"], str(m))
        check("  and says nothing about any device", set(m) == {"id", "hostname", "user", "platform", "version"})
        licence.install(make())
        url, licence.SERVICE_URL = licence.SERVICE_URL, "http://127.0.0.1:9"
        try:
            a = licence.announce("activate", timeout=1.0)
            check("announcing to an unreachable service is not an error", a == {"announced": False, "reason": "unreachable"}, str(a))
        finally:
            licence.SERVICE_URL = url
        licence.remove()
    finally:
        licence.PUBLIC_KEY_B64 = saved


def _fails(fn, needle: str) -> bool:
    try:
        fn()
    except licence.LicenceError as exc:
        return needle.lower() in str(exc).lower()
    except Exception:
        return needle == ""
    return False


def main() -> int:
    print("=" * 52)
    print("  Licence keys")
    print("=" * 52)
    for test in (test_verification, test_status, test_storage_and_features):
        try:
            test()
        except Exception as exc:
            failed.append(f"{test.__name__}: raised {type(exc).__name__}: {exc}")
            print(f"  FAIL {test.__name__} raised {type(exc).__name__}: {exc}")
    shutil.rmtree(_TEMP, ignore_errors=True)
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
