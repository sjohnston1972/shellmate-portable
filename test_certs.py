"""
test_certs.py — Reading an OpenSSH certificate.

The inspector exists because the failures it catches are invisible: a
certificate that expired an hour ago, or is valid for principals that do not
include the account being used, refuses a login in exactly the same way as a
wrong password. `ssh-keygen -L` answers that and is absent from a locked-down
Windows machine, so the wire format is parsed here.

The fixtures below are real certificates signed by ssh-keygen and pasted in,
not hand-built byte strings — a parser tested only against its own idea of
the format is a parser that agrees with itself. The expectations were checked
against `ssh-keygen -L` on the same files.

Two things matter as much as reading a good certificate: reading a *bad* one
without raising, since the malformed input is exactly what somebody is
inspecting, and never mistaking an expired certificate for a valid one.

    python test_certs.py
"""

import sys
import time

from backend import certs

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


# A user certificate: principals steven,admin · serial 42 · key id
# "steven-laptop" · force-command · permit-pty · valid 2026-08-07 08:08→10:13.
USER_CERT = (
    "ssh-ed25519-cert-v01@openssh.com "
    "AAAAIHNzaC1lZDI1NTE5LWNlcnQtdjAxQG9wZW5zc2guY29tAAAAIOEcDpc4BNzUyln3"
    "H7ozy32LaVqtyof/MlWUZLa2PiiWAAAAIBiE1QA2iJfuLY4quVOadorsWzIHTq1haL5n"
    "V9p4pd0IAAAAAAAAACoAAAABAAAADXN0ZXZlbi1sYXB0b3AAAAATAAAABnN0ZXZlbgAA"
    "AAVhZG1pbgAAAABqdYRjAAAAAGp1oa8AAAAlAAAADWZvcmNlLWNvbW1hbmQAAAAQAAAA"
    "DHNob3cgdmVyc2lvbgAAABIAAAAKcGVybWl0LXB0eQAAAAAAAAAAAAAAMwAAAAtzc2gt"
    "ZWQyNTUxOQAAACAon5wzQvqzEe5sjpbQznzZBbflE97WfcYKJHn3r00P6QAAAFMAAAAL"
    "c3NoLWVkMjU1MTkAAABAuGopGFva+Y+RIS4WdLzcon/tvZnQvjx8UnOk/fVgMQnhVtxv"
    "r/FTjlH3GBc6BAZvalIimTNANgNXMrjW1qhXAQ=="
    " steven@laptop"
)


def test_reads_a_real_certificate() -> None:
    print("\n-- A certificate signed by ssh-keygen --")
    info = certs.parse(USER_CERT)

    check("it reads", info.ok, info.reason)
    if not info.ok:
        return

    check("the type is the certificate type",
          info.type == "ssh-ed25519-cert-v01@openssh.com", info.type)
    check("and it names the key it certifies",
          info.certifies == "ssh-ed25519", info.certifies)
    check("a user certificate, not a host one", info.kind == "user", info.kind)
    check("the key id", info.key_id == "steven-laptop", info.key_id)
    check("the serial", info.serial == 42, str(info.serial))
    # The principals are the field that decides whether a login is allowed,
    # so a wrong reading here is the whole feature failing quietly.
    check("every principal, in order",
          info.principals == ["steven", "admin"], str(info.principals))
    check("critical options are read as a mapping",
          info.critical_options == {"force-command": "show version"},
          str(info.critical_options))
    check("extensions are named", info.extensions == ["permit-pty"],
          str(info.extensions))
    check("the signing CA is fingerprinted the way ssh-keygen prints it",
          info.ca_fingerprint
          == "SHA256:U/yoVi/+4cXCBd7gHWlfI98pvC2Onfro/6XiyjinSsc",
          info.ca_fingerprint)
    check("and the CA's own key type is given",
          info.ca_type == "ssh-ed25519", info.ca_type)
    check("the validity window is an epoch pair",
          info.valid_after > 0 and info.valid_before > info.valid_after,
          f"{info.valid_after} → {info.valid_before}")


def test_the_verdict_follows_the_clock() -> None:
    """The dates are the point: the same certificate is fine, then it is not."""
    print("\n-- Valid, then expired --")
    info = certs.parse(USER_CERT)
    if not info.ok:
        check("certificate available for the clock test", False, info.reason)
        return

    midway = (info.valid_after + info.valid_before) / 2
    check("valid in the middle of its window",
          certs.verdict(info, midway)["state"] in ("valid", "expiring"),
          str(certs.verdict(info, midway)))

    check("not yet valid before it starts",
          certs.verdict(info, info.valid_after - 60)["state"] == "not-yet-valid",
          str(certs.verdict(info, info.valid_after - 60)))

    # An hour past the end. This is the failure that looks exactly like a
    # wrong password from the device's side.
    expired = certs.verdict(info, info.valid_before + 3600)
    check("expired after it ends", expired["state"] == "expired", str(expired))

    soon = certs.verdict(info, info.valid_before - 1800)
    check("and it warns while it is still working",
          soon["state"] == "expiring", str(soon))


def test_bad_input_is_reported_not_raised() -> None:
    """The malformed thing is exactly what somebody is inspecting."""
    print("\n-- Anything else --")

    cases = {
        "nothing at all": "",
        "whitespace": "   \n  ",
        "prose": "this is not a certificate",
        "a plain public key": (
            "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIP+PJ/QpTf6h9dMRnR7Q0MnIsWSj"
            "yxvIRJc0P54tCvJm steven@laptop"),
        "bad base64": "ssh-ed25519-cert-v01@openssh.com !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!",
        "truncated": USER_CERT[:120],
    }
    for name, text in cases.items():
        try:
            info = certs.parse(text)
            check(f"{name} is refused with a reason",
                  info.ok is False and bool(info.reason), repr(info))
        except Exception as exc:
            check(f"{name} is refused with a reason", False, f"raised {exc!r}")

    # And the verdict copes with a result it could not read.
    unreadable = certs.parse("nonsense")
    check("an unreadable certificate has an unreadable verdict",
          certs.verdict(unreadable, time.time())["state"] == "unreadable")


def test_the_endpoint_answers() -> None:
    print("\n-- Over the API --")
    from fastapi.testclient import TestClient

    from backend.app import app

    client = TestClient(app)
    response = client.post("/api/keys/certificate", json={"text": USER_CERT})
    check("the endpoint answers", response.status_code == 200, response.text)
    if response.status_code != 200:
        return
    body = response.json()
    check("with the parsed certificate", body.get("key_id") == "steven-laptop",
          str(body)[:200])
    check("and a verdict beside it", "state" in (body.get("verdict") or {}),
          str(body.get("verdict")))

    # No private material, ever — the response is built field by field for
    # exactly this reason.
    check("and nothing that looks like a private key",
          "PRIVATE" not in response.text.upper(), "private material in response")


def main() -> int:
    print("\n" + "=" * 52)
    print("  Certificates")
    print("=" * 52)

    for test in (
        test_reads_a_real_certificate,
        test_the_verdict_follows_the_clock,
        test_bad_input_is_reported_not_raised,
        test_the_endpoint_answers,
    ):
        try:
            test()
        except Exception as exc:
            failed.append(f"{test.__name__} raised {exc!r}")
            print(f"  FAIL {test.__name__} raised\n       {exc!r}")

    print("\n" + "-" * 52)
    if failed:
        print(f"  {passed} passed, {len(failed)} FAILED")
        for line in failed:
            print(f"    - {line}")
    else:
        print(f"  all {passed} checks passed")
    print("-" * 52)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
