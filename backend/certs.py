"""
certs.py — Read an OpenSSH certificate and say what it actually permits.

A certificate is the one credential whose problems are invisible until they
bite: it expires at a moment nobody wrote down, it is valid for principals
that may not include the account being logged into, and it is signed by a CA
the device may not trust. `ssh-keygen -L` answers all of that, and is not on
a locked-down Windows machine — so the format is parsed here instead.

The format is the SSH wire encoding: length-prefixed fields, big-endian,
described in PROTOCOL.certkeys. Nothing here needs a private key and nothing
here verifies a signature; this reports what a certificate *claims*, which is
what somebody debugging "permission denied" needs to see.

Parsing is total: a file that is not a certificate, or is truncated, or was
written by something else entirely, comes back as a result that says so. An
inspector that raises on the malformed input you are inspecting is no use at
the moment you need it.
"""

import base64
import binascii
import hashlib
import struct
from dataclasses import dataclass, field

#: Certificate key types, and the plain key type each certifies.
CERT_TYPES = {
    "ssh-rsa-cert-v01@openssh.com": "ssh-rsa",
    "rsa-sha2-256-cert-v01@openssh.com": "ssh-rsa",
    "rsa-sha2-512-cert-v01@openssh.com": "ssh-rsa",
    "ssh-ed25519-cert-v01@openssh.com": "ssh-ed25519",
    "ecdsa-sha2-nistp256-cert-v01@openssh.com": "ecdsa-sha2-nistp256",
    "ecdsa-sha2-nistp384-cert-v01@openssh.com": "ecdsa-sha2-nistp384",
    "ecdsa-sha2-nistp521-cert-v01@openssh.com": "ecdsa-sha2-nistp521",
    "ssh-dss-cert-v01@openssh.com": "ssh-dss",
}

#: cert_type field: 1 is a user certificate, 2 is a host certificate.
CERT_KINDS = {1: "user", 2: "host"}

#: Never-expires, spelled as the maximum unsigned 64-bit value.
FOREVER = 0xFFFFFFFFFFFFFFFF


@dataclass
class CertInfo:
    """What a certificate says about itself."""

    ok: bool
    reason: str = ""
    type: str = ""                    # the certificate key type
    certifies: str = ""               # the plain key type inside it
    kind: str = ""                    # user | host
    key_id: str = ""
    serial: int = 0
    principals: list[str] = field(default_factory=list)
    valid_after: int = 0
    valid_before: int = 0
    critical_options: dict = field(default_factory=dict)
    extensions: list[str] = field(default_factory=list)
    ca_fingerprint: str = ""
    ca_type: str = ""
    fingerprint: str = ""

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "type": self.type,
            "certifies": self.certifies,
            "kind": self.kind,
            "key_id": self.key_id,
            "serial": self.serial,
            "principals": self.principals,
            "valid_after": self.valid_after,
            "valid_before": self.valid_before,
            "critical_options": self.critical_options,
            "extensions": self.extensions,
            "ca_fingerprint": self.ca_fingerprint,
            "ca_type": self.ca_type,
            "fingerprint": self.fingerprint,
        }


class _Reader:
    """The SSH wire format: length-prefixed fields, big-endian."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._at = 0

    def _take(self, count: int) -> bytes:
        if count < 0 or self._at + count > len(self._data):
            raise ValueError("the certificate ends in the middle of a field")
        chunk = self._data[self._at:self._at + count]
        self._at += count
        return chunk

    def string(self) -> bytes:
        (length,) = struct.unpack(">I", self._take(4))
        return self._take(length)

    def text(self) -> str:
        return self.string().decode("utf-8", errors="replace")

    def uint64(self) -> int:
        (value,) = struct.unpack(">Q", self._take(8))
        return value

    def uint32(self) -> int:
        (value,) = struct.unpack(">I", self._take(4))
        return value

    def name_list(self) -> list[str]:
        """A string holding a sequence of strings — principals, extensions."""
        inner = _Reader(self.string())
        out: list[str] = []
        while inner.remaining:
            out.append(inner.text())
        return out

    def option_map(self) -> dict:
        """Critical options and extensions: name, then a wrapped value."""
        inner = _Reader(self.string())
        out: dict = {}
        while inner.remaining:
            name = inner.text()
            raw = inner.string()
            # Each value is itself a string when it carries one at all.
            try:
                value = _Reader(raw).text() if raw else ""
            except ValueError:
                value = ""
            out[name] = value
        return out

    @property
    def remaining(self) -> bool:
        return self._at < len(self._data)


def _fingerprint(blob: bytes) -> str:
    digest = hashlib.sha256(blob).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


def parse(text: str) -> CertInfo:
    """
    Read a certificate from the one-line OpenSSH form.

    Accepts the whole line — "type base64 comment" — or the base64 on its
    own, because both are things people paste.
    """
    if not (text or "").strip():
        return CertInfo(ok=False, reason="Nothing to read.")

    # The interesting part is the base64 field; a line may or may not carry
    # the type in front of it and a comment behind.
    blob = ""
    for word in text.split():
        if word in CERT_TYPES:
            continue
        if len(word) > 60:
            blob = word
            break
    if not blob:
        return CertInfo(
            ok=False,
            reason="That does not look like a certificate. Paste the whole "
                   "line from an *-cert.pub file.",
        )

    try:
        raw = base64.b64decode(blob, validate=True)
    except (binascii.Error, ValueError):
        return CertInfo(ok=False, reason="The certificate is not valid base64.")

    reader = _Reader(raw)
    try:
        cert_type = reader.text()
        if cert_type not in CERT_TYPES:
            return CertInfo(
                ok=False,
                reason=f"This is a '{cert_type}' key, not a certificate. A "
                       f"certificate's type ends in -cert-v01@openssh.com.",
            )

        reader.string()                       # nonce
        _skip_public_key(reader, CERT_TYPES[cert_type])
        serial = reader.uint64()
        kind = CERT_KINDS.get(reader.uint32(), "unknown")
        key_id = reader.text()
        principals = reader.name_list()
        valid_after = reader.uint64()
        valid_before = reader.uint64()
        critical = reader.option_map()
        extensions = list(reader.option_map().keys())
        reader.string()                       # reserved
        signature_key = reader.string()
    except (ValueError, struct.error, UnicodeDecodeError) as exc:
        return CertInfo(ok=False, reason=f"The certificate could not be read: {exc}")

    ca_type = ""
    try:
        ca_type = _Reader(signature_key).text()
    except (ValueError, struct.error):
        pass

    return CertInfo(
        ok=True,
        type=cert_type,
        certifies=CERT_TYPES[cert_type],
        kind=kind,
        key_id=key_id,
        serial=serial,
        principals=principals,
        valid_after=valid_after,
        valid_before=valid_before,
        critical_options=critical,
        extensions=extensions,
        ca_fingerprint=_fingerprint(signature_key) if signature_key else "",
        ca_type=ca_type,
        fingerprint=_fingerprint(raw),
    )


def _skip_public_key(reader: _Reader, key_type: str) -> None:
    """
    Step over the certified key's own fields.

    Their number depends on the algorithm, and getting it wrong would read
    the rest of the certificate out of alignment rather than fail — which is
    why each is spelled out instead of guessed.
    """
    if key_type == "ssh-rsa":
        reader.string()                       # e
        reader.string()                       # n
    elif key_type == "ssh-ed25519":
        reader.string()                       # public key
    elif key_type.startswith("ecdsa-sha2-"):
        reader.string()                       # curve name
        reader.string()                       # public point
    elif key_type == "ssh-dss":
        for _ in range(4):                    # p, q, g, y
            reader.string()
    else:
        raise ValueError(f"unsupported key type '{key_type}'")


def verdict(info: CertInfo, now: float) -> dict:
    """
    Say whether it is usable *at this moment*, and for how much longer.

    The dates are the whole point of an inspector — "valid from" in the
    future and "valid until" in the past are the two failures that look
    identical from the device's side: permission denied.
    """
    if not info.ok:
        return {"state": "unreadable", "detail": info.reason}

    if info.valid_after and now < info.valid_after:
        return {"state": "not-yet-valid",
                "detail": "This certificate does not start until later."}

    if info.valid_before and info.valid_before != FOREVER and now > info.valid_before:
        return {"state": "expired",
                "detail": "This certificate has expired and will be refused."}

    if info.valid_before == FOREVER or not info.valid_before:
        return {"state": "valid", "detail": "Valid, with no expiry."}

    left = int(info.valid_before - now)
    days = left // 86400
    if days < 1:
        hours = max(1, left // 3600)
        return {"state": "expiring",
                "detail": f"Valid, but expires in about {hours} hour"
                          f"{'' if hours == 1 else 's'}."}
    if days <= 7:
        return {"state": "expiring",
                "detail": f"Valid, but expires in {days} day"
                          f"{'' if days == 1 else 's'}."}
    return {"state": "valid", "detail": f"Valid for another {days} days."}
