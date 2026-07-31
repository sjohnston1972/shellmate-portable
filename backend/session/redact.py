"""
session/redact.py — Keep credentials out of written session logs.

Session logs capture everything a device sends, and devices echo.  Type a
password at a login prompt on a box that echoes, or run ``show run`` on one
with unencrypted secrets in its configuration, and the credential lands in a
file — a file whose whole purpose is to be handed to someone else as evidence
of what you did.

This masks the credential and leaves everything around it intact, because a
log with the shape of the configuration preserved is still useful and one with
whole lines removed is not.  ``username neteng password 7 09461A1D`` becomes
``username neteng password 7 ********``: you can still see the account exists
and how it is configured.

Two limits worth being honest about.  This is pattern matching, so a
credential in a form not listed here goes through untouched — it reduces
exposure rather than guaranteeing its absence.  And it only applies to what is
*written to disk*; the live terminal always shows the truth, because hiding
things from the person at the keyboard would be worse than useless.
"""

import re

MASK = "********"

# Configuration statements that carry a secret. Each pattern keeps the
# statement and its non-secret arguments, and replaces only the value.
#
# Ordered most specific first, so "password 7 <hash>" is matched before the
# looser bare-password form.
_PATTERNS: list[tuple[re.Pattern, str]] = [
    # Cisco / Arista: username x password [0|7] <secret>, enable secret 5 <hash>
    (re.compile(r"(?i)\b(password|secret)(\s+\d)?\s+\S+"), r"\1\2 " + MASK),

    # SNMP community strings — a read-write community is a credential.
    (re.compile(r"(?i)\b(snmp-server\s+community)\s+\S+"), r"\1 " + MASK),
    (re.compile(r"(?i)\b(community)\s+(?!ro\b|rw\b)\S+"), r"\1 " + MASK),

    # Shared keys: IPsec, BGP, OSPF, TACACS, RADIUS.
    (re.compile(r"(?i)\b(pre-shared-key(?:\s+(?:local|remote))?)\s+\S+"), r"\1 " + MASK),
    # "key-string 0 <secret>" — the digit is the encryption type, not the
    # secret, so it has to be kept or the wrong token gets masked.
    (re.compile(r"(?i)\b(key-string)(\s+\d)?\s+\S+"), r"\1\2 " + MASK),
    (re.compile(r"(?i)\b(key)\s+\d+\s+\S+"), r"\1 " + MASK),
    (re.compile(r"(?i)\b((?:tacacs|radius)-server\s+(?:host\s+\S+\s+)?key)\s+\S+"), r"\1 " + MASK),
    (re.compile(r"(?i)\b(authentication-key|message-digest-key\s+\d+\s+md5)\s+\S+"), r"\1 " + MASK),

    # Junos encrypted values.
    (re.compile(r"(?i)\b(encrypted-password)\s+\S+"), r"\1 " + MASK),
    (re.compile(r'(?i)\b(secret)\s+"[^"]*"'), r"\1 " + MASK),

    # Wireless.
    (re.compile(r"(?i)\b(wpa-psk\s+ascii\s+\d?)\s*\S+"), r"\1 " + MASK),
]

# A line that is *only* a credential prompt. What follows on the same line is
# whatever the device echoed back, which on some platforms is the password.
_PROMPT_LINE = re.compile(
    r"(?i)^(\s*(?:password|passphrase|enable\s+password|secret)\s*:\s*)(.+)$"
)


def redact_line(line: str) -> str:
    """Mask any credential in a single line."""
    prompt = _PROMPT_LINE.match(line)
    if prompt and prompt.group(2).strip():
        return prompt.group(1) + MASK

    for pattern, replacement in _PATTERNS:
        line = pattern.sub(replacement, line)
    return line


def redact(text: str) -> str:
    """
    Mask credentials throughout *text*.

    Applied line by line so a match cannot run past a line ending and swallow
    the rest of a configuration.
    """
    if not text:
        return text
    return "\n".join(redact_line(line) for line in text.split("\n"))
