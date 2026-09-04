"""
ansible_health.py — Is the link to the runner healthy, and is it actually secure?

The status pill in the Ansible view answers "can I use this". This answers
the narrower and more awkward question behind it: **what exactly is on the
other end, and is anything checking?**

Those are two separate questions and the code keeps them separate, because
conflating them is how a tool ends up reassuring somebody about a
connection nothing verifies:

- **Does the certificate verify** under the trust ShellMate is configured
  with? A yes/no, with the reason when it is no.
- **What is the certificate** — subject, issuer, expiry, fingerprint? This
  is read with verification *off*, on purpose. A certificate that has
  expired or that nothing trusts is precisely the one somebody needs to see
  the details of, and a verifying read would refuse and tell them nothing.

Reading it unverified is safe here because nothing is sent: the socket is
opened, the certificate is taken from the handshake, and the connection is
closed without a byte of ShellMate's data crossing it. What comes back is
evidence to look at, never a reason to trust anything.

The fingerprint is the point of the whole exercise. A self-signed runner
certificate can only be trusted by comparing it to a value obtained some
other way — the container prints one at startup — so ShellMate shows the
fingerprint it is actually talking to and leaves the comparison to a person.
Nothing here decides that a certificate is genuine.

Expiry gets its own state. A certificate with three weeks left is working
perfectly and is about to stop, and the only useful time to say so is
before it does.
"""

import logging
import socket
import ssl
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

#: Below this many days remaining, a working certificate is still worth a
#: warning. Long enough to renew a certificate without a rush, short enough
#: that it is not permanently amber on a one-year certificate.
EXPIRY_WARNING_DAYS = 30

#: How long to wait on the socket. Short: this runs on a timer, and a probe
#: that hangs for the full request timeout would stack up behind itself.
PROBE_TIMEOUT = 6.0


def _split(url: str) -> tuple[str, int, bool]:
    """Host, port and whether it is TLS, from the configured URL."""
    from urllib.parse import urlparse

    parsed = urlparse(url or "")
    secure = parsed.scheme == "https"
    port = parsed.port or (443 if secure else 80)
    return parsed.hostname or "", port, secure


def _describe(der: bytes) -> dict:
    """
    Read the certificate's own fields out of the DER.

    Not `getpeercert()`: that returns the parsed dictionary only when the
    handshake verified, and the certificates most worth describing are
    exactly the ones that did not — so it comes back empty for every case
    this exists to explain. Reading the DER gives the same facts whether or
    not anything trusts it, which is the point.

    `cryptography` is already declared and already bundled — paramiko brings
    it — so this costs nothing to reach for.
    """
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.x509.oid import NameOID

    cert = x509.load_der_x509_certificate(der)

    def common(name) -> str:
        found = name.get_attributes_for_oid(NameOID.COMMON_NAME)
        return found[0].value if found else name.rfc4514_string()

    try:
        expires, starts = cert.not_valid_after_utc, cert.not_valid_before_utc
    except AttributeError:                                # cryptography < 42
        expires = cert.not_valid_after.replace(tzinfo=timezone.utc)
        starts = cert.not_valid_before.replace(tzinfo=timezone.utc)

    names: list[str] = []
    try:
        alt = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        names = [str(v) for v in alt.value.get_values_for_type(x509.DNSName)]
        names += [str(v) for v in alt.value.get_values_for_type(x509.IPAddress)]
    except x509.ExtensionNotFound:
        names = []

    days_left = int((expires - datetime.now(timezone.utc)).total_seconds() // 86400)
    subject, issuer = common(cert.subject), common(cert.issuer)

    return {
        "subject": subject,
        "issuer": issuer,
        # A self-signed certificate is its own trust anchor, which is why
        # the file to point ShellMate at is the certificate itself.
        "self_signed": subject == issuer,
        "not_before": starts.isoformat(),
        "not_after": expires.isoformat(),
        "days_left": days_left,
        "expired": days_left < 0,
        "expiring": 0 <= days_left <= EXPIRY_WARNING_DAYS,
        "names": names,
        "serial": format(cert.serial_number, "x"),
        # SHA-256 of the DER: what every other tool prints, so it can be
        # compared with the container's own startup line without anybody
        # converting between formats.
        "fingerprint": cert.fingerprint(hashes.SHA256()).hex(),
    }


def certificate(url: str) -> dict:
    """
    What certificate the runner presents, read without verifying it.

    Deliberately unverified. The certificates worth inspecting are the ones
    that fail — expired, self-signed, issued for another name — and a
    verifying read refuses those and reports nothing about them, which
    leaves somebody guessing at exactly the moment they need a fact.

    Nothing is sent over this socket. It exists to look, and what it returns
    is evidence for a person, never grounds for the code to trust anything.
    """
    import hashlib

    host, port, secure = _split(url)
    if not host:
        return {"available": False, "why": "No runner address is set."}
    if not secure:
        return {"available": False,
                "why": "The runner address is plain HTTP, so there is no "
                       "certificate to inspect."}

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((host, port), timeout=PROBE_TIMEOUT) as raw:
            with context.wrap_socket(raw, server_hostname=host) as tls:
                der = tls.getpeercert(binary_form=True)
                protocol = tls.version()
                cipher = (tls.cipher() or ("", "", 0))[0]
    except Exception as exc:
        return {"available": False,
                "why": f"Could not read a certificate from {host}:{port} "
                       f"({exc.__class__.__name__})."}

    if not der:
        return {"available": False,
                "why": "The runner presented no certificate."}
    try:
        described = _describe(der)
    except Exception as exc:                              # pragma: no cover
        logger.warning("Could not parse the runner certificate: %s", exc)
        return {"available": False,
                "why": f"The certificate could not be read "
                       f"({exc.__class__.__name__})."}

    described.update(available=True, protocol=protocol or "", cipher=cipher or "")
    return described


def _verifies(url: str, ca_cert: str, verify_tls: bool) -> tuple[bool, str]:
    """
    Whether a verifying handshake succeeds under the configured trust.

    Separate from reading the certificate, and asked separately, because
    "here is the certificate" and "and it is trusted" are two claims and
    only one of them is ever in doubt.
    """
    host, port, secure = _split(url)
    if not secure:
        return False, "The connection is plain HTTP, so nothing is verified."
    if not ca_cert and not verify_tls:
        return False, "Verification is turned off in Settings."

    try:
        if ca_cert:
            context = ssl.create_default_context(cafile=ca_cert)
        else:
            context = ssl.create_default_context()
    except Exception as exc:
        return False, f"The CA file could not be loaded: {exc}"

    try:
        with socket.create_connection((host, port), timeout=PROBE_TIMEOUT) as raw:
            with context.wrap_socket(raw, server_hostname=host):
                return True, ""
    except ssl.SSLCertVerificationError as exc:
        return False, str(getattr(exc, "verify_message", "") or exc)
    except Exception as exc:
        return False, f"{exc.__class__.__name__}: {exc}"


#: What the indicator shows. Ordered worst to best; the first that applies
#: wins, so a certificate problem is never hidden behind a token problem.
#: `kind` drives the colour and nothing else reads it.
STATES = {
    "off":        ("grey",  "Not set up"),
    "unreachable": ("bad",  "Unreachable"),
    "expired":    ("bad",   "Certificate expired"),
    "untrusted":  ("bad",   "Certificate not trusted"),
    "insecure":   ("warn",  "Not encrypted"),
    "unverified": ("warn",  "Not verified"),
    "expiring":   ("warn",  "Certificate expiring"),
    "refused":    ("warn",  "Token refused"),
    "ok":         ("ok",    "Secure"),
}


def probe() -> dict:
    """
    One reading for the indicator: is it up, is it encrypted, is it checked.

    Cheap enough to run on a timer. `/health` needs no token by design, so
    liveness can be established without one — which is what lets the
    indicator distinguish "nothing there" from "there and refusing us" while
    somebody is still hunting for the token.

    Never raises. An indicator that can throw is an indicator that stops
    updating, and a frozen light reads as a healthy one.
    """
    from backend import ansible

    started = time.monotonic()
    cfg = ansible.config()
    out: dict = {
        "url": cfg.url,
        "checked": time.time(),
        "encrypted": bool(cfg.url.startswith("https://")),
        "verify_setting": bool(cfg.verify_tls),
        "ca_cert": cfg.ca_cert,
        "has_token": bool(cfg.token),
    }

    if not cfg.ready:
        out.update(state="off", detail=", ".join(cfg.missing()) or
                   "No runner address yet. Settings → Ansible.",
                   reachable=False, verified=False, certificate={})
        out.update(kind=STATES["off"][0], label=STATES["off"][1])
        return out

    try:
        state = ansible.ping()
    except Exception as exc:                              # pragma: no cover
        logger.warning("Ansible health probe failed: %s", exc)
        state = {"reachable": False, "detail": str(exc)}

    cert = certificate(cfg.url) if out["encrypted"] else {}
    verified, why = (_verifies(cfg.url, cfg.ca_cert, cfg.verify_tls)
                     if out["encrypted"] else (False, ""))

    out["reachable"] = bool(state.get("reachable"))
    out["authenticated"] = state.get("authenticated")
    out["verified"] = verified
    out["certificate"] = cert
    out["ansible_core"] = state.get("ansible_core", "")
    out["playbooks"] = state.get("playbooks")
    out["latency_ms"] = int((time.monotonic() - started) * 1000)

    # Worst first. A certificate that expired yesterday matters more than a
    # token that was refused because of it, and reporting the token would
    # send somebody to the wrong file.
    if cert.get("expired"):
        name, detail = "expired", (
            f"The runner's certificate expired on "
            f"{cert.get('not_after', '')[:10]}.")
    elif out["encrypted"] and not verified:
        if not cfg.verify_tls and not cfg.ca_cert:
            name, detail = "unverified", (
                "Encrypted, but verification is turned off — nothing is "
                "checking who is on the other end.")
        else:
            name, detail = "untrusted", (
                why or "The certificate was not accepted.")
    elif not out["reachable"]:
        name, detail = "unreachable", (
            state.get("detail") or f"Nothing answered at {cfg.url}.")
    elif not out["encrypted"]:
        name, detail = "insecure", (
            "Plain HTTP: the token and anything a run carries cross the "
            "network in the clear.")
    elif state.get("authenticated") is False:
        name, detail = "refused", (
            "The connection is secure, but the runner will not accept "
            "ShellMate. Check the token under Settings → Ansible.")
    elif cert.get("expiring"):
        name, detail = "expiring", (
            f"Secure, but the certificate expires in "
            f"{cert.get('days_left')} day"
            f"{'' if cert.get('days_left') == 1 else 's'}.")
    else:
        name, detail = "ok", (
            f"Encrypted and verified"
            + (f", {cert['protocol']}" if cert.get("protocol") else "")
            + ".")

    out["state"] = name
    out["kind"], out["label"] = STATES[name]
    out["detail"] = detail
    return out
