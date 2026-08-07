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


# Sectigo's DV certificate for github.com. A real one, so the expectations
# below were checked against `openssl x509` on the same bytes.
X509_CERT = """-----BEGIN CERTIFICATE-----
MIID7jCCA5SgAwIBAgIQcgEOA/SgZ/5OeWJmQwcY9jAKBggqhkjOPQQDAjBgMQsw
CQYDVQQGEwJHQjEYMBYGA1UEChMPU2VjdGlnbyBMaW1pdGVkMTcwNQYDVQQDEy5T
ZWN0aWdvIFB1YmxpYyBTZXJ2ZXIgQXV0aGVudGljYXRpb24gQ0EgRFYgRTM2MB4X
DTI2MDcwMzAwMDAwMFoXDTI2MDkzMDIzNTk1OVowFTETMBEGA1UEAxMKZ2l0aHVi
LmNvbTBZMBMGByqGSM49AgEGCCqGSM49AwEHA0IABIWWMDSOi/1sMgquP4I/obBM
735wpzcIZi4fLeiBsToXVVSwjj4OPH+W6azHzxETM0gUP7raehddpJ8uwjqYsTij
ggJ5MIICdTAfBgNVHSMEGDAWgBQXmagEwW/kLXCoChA9A9PpGrgmYzAdBgNVHQ4E
FgQUEKU6Ytbv1gZWnty4gvzCe2hdPWkwDgYDVR0PAQH/BAQDAgeAMAwGA1UdEwEB
/wQCMAAwEwYDVR0lBAwwCgYIKwYBBQUHAwEwSQYDVR0gBEIwQDA0BgsrBgEEAbIx
AQICBzAlMCMGCCsGAQUFBwIBFhdodHRwczovL3NlY3RpZ28uY29tL0NQUzAIBgZn
gQwBAgEwgYQGCCsGAQUFBwEBBHgwdjBPBggrBgEFBQcwAoZDaHR0cDovL2NydC5z
ZWN0aWdvLmNvbS9TZWN0aWdvUHVibGljU2VydmVyQXV0aGVudGljYXRpb25DQURW
RTM2LmNydDAjBggrBgEFBQcwAYYXaHR0cDovL29jc3Auc2VjdGlnby5jb20wggEF
BgorBgEEAdZ5AgQCBIH2BIHzAPEAdgDXbX0Q0af1d8LH6V/XAL/5gskzWmXh0LMB
cxfAyMVpdwAAAZ8lTHVtAAAEAwBHMEUCIQCkpa0ZYNwsPiMRLHz+kk1QS/W9bg/8
4yNBVGkT289dNQIgMWLgxYp6vGJXJxyD3c1NI1aZsPA7GqyLSXaZLZHgKh0AdwDI
o8R/x7OtuTVrAT9qehJt4zpOQ6XGRvmXrTl1mR3PmgAAAZ8lTHVhAAAEAwBIMEYC
IQDsO+TR8EVfCiObBPoDLRKzKLQ/uorsebJ2aZDIejA9RgIhAJ6dp7FqCD93tQXX
AF24pDIms1fX4dZ+VPzXGuD8u8t1MCUGA1UdEQQeMByCCmdpdGh1Yi5jb22CDnd3
dy5naXRodWIuY29tMAoGCCqGSM49BAMCA0gAMEUCIB0PC2GRSurxu8gCkSNsYxmw
kAtCNfCvpXRiif8PhGkmAiEAzBH4AVYAtv1FsMrJabD9FYcAql0EteKafckH2exj
Uag=
-----END CERTIFICATE-----"""


def test_reads_an_x509_certificate() -> None:
    """
    The TLS kind (#304).

    A device's management page, a RADIUS server and a captive portal all
    serve one of these, and pasting one used to be reported as a corrupt SSH
    certificate — an error describing the wrong format entirely.
    """
    print("\n-- An X.509 certificate --")
    info = certs.parse(X509_CERT)

    check("it reads", info.ok, info.reason)
    if not info.ok:
        return

    check("and is recognised as X.509, not SSH", info.family == "x509", info.family)
    check("the subject", info.subject == "CN=github.com", info.subject)
    check("the issuer names the CA",
          "Sectigo Public Server Authentication CA DV E36" in info.issuer,
          info.issuer)
    # The SANs are what a client actually checks the hostname against, so
    # these are the field that explains a name-mismatch warning.
    check("the names it is valid for",
          info.sans == ["github.com", "www.github.com"], str(info.sans))
    check("the serial, as openssl prints it",
          f"{info.serial:X}" == "72010E03F4A067FE4E796266430718F6",
          f"{info.serial:X}")
    check("the public key is described",
          info.public_key == "EC secp256r1", info.public_key)
    check("it is not a CA certificate", info.is_ca is False)
    check("nor self-signed", info.self_signed is False)
    check("its use is stated",
          "digital signature" in info.key_usage and "serverAuth" in info.key_usage,
          str(info.key_usage))
    check("the fingerprint matches openssl's",
          info.fingerprint.lower().endswith(
              "17:f8:fd:2e:3f:d2:c1:13:fc:b9:77:2d:8a:4b:ab:b8:"
              "52:2d:d0:6d:d0:79:49:15:a4:ff:98:b1:b6:86:3a:00"),
          info.fingerprint)
    check("the validity window is an epoch pair",
          info.valid_after > 0 and info.valid_before > info.valid_after,
          f"{info.valid_after} → {info.valid_before}")

    # And the same verdict machinery serves both kinds.
    expired = certs.verdict(info, info.valid_before + 86400)
    check("an expired one is called expired", expired["state"] == "expired",
          str(expired))


def test_each_format_reaches_its_own_reader() -> None:
    """Neither reader is handed the other's input (#304)."""
    print("\n-- Telling the two apart --")

    check("an SSH certificate goes to the SSH reader",
          certs.parse(USER_CERT).family == "openssh")
    check("a PEM certificate goes to the X.509 reader",
          certs.parse(X509_CERT).family == "x509")

    # A signing request is neither, and saying so beats a parse error.
    csr = "-----BEGIN CERTIFICATE REQUEST-----\nMIIB\n-----END CERTIFICATE REQUEST-----"
    result = certs.parse(csr)
    check("a signing request is named as one",
          result.ok is False and "signing request" in result.reason.lower(),
          result.reason)

    # A private key must never be inspected — and must not be mistaken for
    # something to parse either.
    key = "-----BEGIN OPENSSH PRIVATE KEY-----\nb3Blb\n-----END OPENSSH PRIVATE KEY-----"
    result = certs.parse(key)
    check("a private key is refused",
          result.ok is False and "private key" in result.reason.lower(),
          result.reason)


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
        test_reads_an_x509_certificate,
        test_each_format_reaches_its_own_reader,
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
