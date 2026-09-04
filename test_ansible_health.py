"""
test_ansible_health.py — The TLS indicator says the right thing (#586).

The indicator exists to answer a question the runner pill cannot: **is
anything checking who is on the other end?** Those two fail independently,
and the combination worth catching is the one where reachability is green —
a runner answering perfectly over a connection nothing verifies looks
entirely healthy, and only a light that is not measuring reachability would
ever say otherwise.

So what is tested here is the ordering of states, because that is where an
indicator quietly lies:

- an expired certificate outranks a refused token, or somebody is sent to
  look for a token when the answer is a date;
- verification being *off* and verification *failing* are different states
  with different fixes, and both are worse news than a token;
- a probe that itself fails must not leave the last colour looking current.

The certificate is parsed from real DER built for the test rather than
mocked, because the parsing is the part with edges — an expiry read a day
out is a warning that arrives after the outage.

Run: python test_ansible_health.py
"""

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backend import paths  # noqa: E402

_TEMP = Path(tempfile.mkdtemp(prefix="shellmate-anshealth-"))
paths._data_dir_cache = _TEMP
assert paths.data_dir() == _TEMP, "test isolation failed — refusing to run"

from backend import ansible_health  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                        # pragma: no cover
    pass

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


def make_cert(days_valid: int, *, common: str = "ansible-runner",
              names: tuple = ("localhost", "ansible-runner")) -> bytes:
    """A real self-signed certificate, in DER, expiring when we say."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    who = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common)])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(who)
        .issuer_name(who)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now + timedelta(days=days_valid) - timedelta(days=365))
        .not_valid_after(now + timedelta(days=days_valid))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(n) for n in names]),
            critical=False)
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.DER)


# ---------------------------------------------------------------------------
def reading_a_certificate() -> None:
    print("\n-- Reading a certificate --")

    described = ansible_health._describe(make_cert(3650))
    check("the subject comes back", described["subject"] == "ansible-runner",
          str(described["subject"]))
    check("a certificate that signed itself is recognised as such",
          described["self_signed"] is True, str(described))
    check("the names it covers are listed",
          "localhost" in described["names"], str(described["names"]))
    check("the fingerprint is 64 hex characters",
          len(described["fingerprint"]) == 64
          and all(c in "0123456789abcdef" for c in described["fingerprint"]),
          described["fingerprint"])

    # Off-by-one here is a warning that arrives after the outage.
    soon = ansible_health._describe(make_cert(10))
    check("days remaining is counted, not guessed",
          soon["days_left"] in (9, 10), str(soon["days_left"]))
    check("a certificate inside the warning window is flagged",
          soon["expiring"] is True and soon["expired"] is False, str(soon))

    far = ansible_health._describe(make_cert(3650))
    check("a certificate years away is not flagged",
          far["expiring"] is False, str(far["days_left"]))

    gone = ansible_health._describe(make_cert(-5))
    check("an expired certificate is reported expired",
          gone["expired"] is True, str(gone["days_left"]))
    check("and is not also reported as merely expiring",
          gone["expiring"] is False,
          "expired and expiring are different states with different colours")

    edge = ansible_health._describe(
        make_cert(ansible_health.EXPIRY_WARNING_DAYS + 5))
    check("just outside the window is not flagged",
          edge["expiring"] is False, str(edge["days_left"]))


# ---------------------------------------------------------------------------
def _probe_with(monkey: dict) -> dict:
    """Run probe() with its three inputs replaced."""
    import types

    from backend import ansible

    real = (ansible.config, ansible.ping,
            ansible_health.certificate, ansible_health._verifies)
    ansible.config = lambda: types.SimpleNamespace(**monkey["config"])
    ansible.ping = lambda: monkey["ping"]
    ansible_health.certificate = lambda url: monkey["cert"]
    ansible_health._verifies = lambda *a: monkey["verifies"]
    try:
        return ansible_health.probe()
    finally:
        (ansible.config, ansible.ping,
         ansible_health.certificate, ansible_health._verifies) = real


def config(**over) -> dict:
    base = {"url": "https://runner.test:8081", "token": "t", "ca_cert": "ca.pem",
            "verify_tls": True, "client_cert": "", "client_key": "",
            "timeout": 30, "ready": True, "missing": lambda: []}
    base.update(over)
    return base


def what_the_light_says() -> None:
    print("\n-- What the light says --")

    good = _probe_with({
        "config": config(),
        "ping": {"reachable": True, "authenticated": True, "ansible_core": "2.21.3"},
        "cert": {"available": True, "expired": False, "expiring": False,
                 "protocol": "TLSv1.3", "days_left": 3000},
        "verifies": (True, ""),
    })
    check("everything working is green", good["state"] == "ok"
          and good["kind"] == "ok", str(good["state"]))

    off = _probe_with({
        "config": config(ready=False, url="", missing=lambda: ["a runner address"]),
        "ping": {}, "cert": {}, "verifies": (False, ""),
    })
    check("nothing configured is grey, not red",
          off["state"] == "off" and off["kind"] == "grey",
          "an unconfigured thing has not failed")

    down = _probe_with({
        "config": config(),
        "ping": {"reachable": False, "detail": "Could not reach it."},
        "cert": {"available": True, "expired": False, "expiring": False},
        "verifies": (True, ""),
    })
    check("unreachable is red", down["state"] == "unreachable"
          and down["kind"] == "bad", str(down["state"]))

    refused = _probe_with({
        "config": config(),
        "ping": {"reachable": True, "authenticated": False},
        "cert": {"available": True, "expired": False, "expiring": False},
        "verifies": (True, ""),
    })
    check("a secure connection with a refused token is amber, not red",
          refused["state"] == "refused" and refused["kind"] == "warn",
          "the link is fine; one value is wrong")
    check("and it names the token rather than the network",
          "token" in refused["detail"].lower(), refused["detail"])

    plain = _probe_with({
        "config": config(url="http://runner.test:8081"),
        "ping": {"reachable": True, "authenticated": True},
        "cert": {}, "verifies": (False, ""),
    })
    check("plain HTTP is called out even when everything works",
          plain["state"] == "insecure" and plain["kind"] == "warn",
          "a working insecure connection is the case that needs saying")

    unverified = _probe_with({
        "config": config(ca_cert="", verify_tls=False),
        "ping": {"reachable": True, "authenticated": True},
        "cert": {"available": True, "expired": False, "expiring": False},
        "verifies": (False, ""),
    })
    check("verification turned off is its own state",
          unverified["state"] == "unverified", str(unverified["state"]))
    check("and says nothing is checking, rather than blaming the certificate",
          "turned off" in unverified["detail"], unverified["detail"])

    untrusted = _probe_with({
        "config": config(),
        "ping": {"reachable": True, "authenticated": True},
        "cert": {"available": True, "expired": False, "expiring": False},
        "verifies": (False, "self signed certificate"),
    })
    check("a certificate that will not verify is red",
          untrusted["state"] == "untrusted" and untrusted["kind"] == "bad",
          str(untrusted["state"]))
    check("and repeats the reason it was rejected",
          "self signed" in untrusted["detail"], untrusted["detail"])

    print("\n-- Which problem wins --")

    # An expired certificate causes the 401 as often as not. Reporting the
    # token would send somebody hunting through a .env for a date problem.
    both = _probe_with({
        "config": config(),
        "ping": {"reachable": True, "authenticated": False},
        "cert": {"available": True, "expired": True, "expiring": False,
                 "not_after": "2026-01-01T00:00:00+00:00"},
        "verifies": (False, "certificate has expired"),
    })
    check("an expired certificate outranks a refused token",
          both["state"] == "expired",
          "otherwise somebody hunts for a token to fix a date")
    check("and the date is in the message", "2026-01-01" in both["detail"],
          both["detail"])

    # Likewise: if nothing verified the certificate, that is the story,
    # whether or not the runner then answered.
    quiet = _probe_with({
        "config": config(ca_cert="", verify_tls=False),
        "ping": {"reachable": False, "detail": "Could not reach it."},
        "cert": {"available": True, "expired": False, "expiring": False},
        "verifies": (False, ""),
    })
    check("verification being off is reported over unreachability",
          quiet["state"] == "unverified",
          "the connection is the thing that changed; say so first")

    expiring = _probe_with({
        "config": config(),
        "ping": {"reachable": True, "authenticated": True},
        "cert": {"available": True, "expired": False, "expiring": True,
                 "days_left": 9},
        "verifies": (True, ""),
    })
    check("a working connection with a certificate about to expire is amber",
          expiring["state"] == "expiring" and expiring["kind"] == "warn",
          "it works today, which is exactly when saying so is useful")
    check("and counts the days", "9 days" in expiring["detail"],
          expiring["detail"])

    print("\n-- Every state has a colour and a name --")
    for name, (kind, label) in ansible_health.STATES.items():
        check(f"{name} has a colour and a label",
              kind in ("ok", "warn", "bad", "grey") and bool(label),
              f"{name} -> {kind}, {label!r}")


def the_probe_never_raises() -> None:
    print("\n-- The probe itself --")
    import types

    from backend import ansible

    real_config, real_ping = ansible.config, ansible.ping
    ansible.config = lambda: types.SimpleNamespace(**config())
    ansible.ping = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    try:
        # certificate() will fail to connect to runner.test, which is the
        # realistic shape of this: the host is not there.
        out = ansible_health.probe()
        check("a failing ping does not take the probe with it",
              isinstance(out, dict) and out.get("state"),
              "an indicator that raises is an indicator that stops updating, "
              "and a frozen light reads as a healthy one")
        check("and it reports a problem rather than success",
              out["kind"] in ("bad", "warn", "grey"), str(out))
    finally:
        ansible.config, ansible.ping = real_config, real_ping


if __name__ == "__main__":
    reading_a_certificate()
    what_the_light_says()
    the_probe_never_raises()

    print("\n" + "=" * 52)
    print(f"  {passed} passed  |  {len(failed)} failed")
    print("=" * 52)
    for line in failed:
        print("  -", line)
    sys.exit(1 if failed else 0)
