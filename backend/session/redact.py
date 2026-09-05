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

# The secret itself: one token, or a Junos quoted value — so that a trailing
# ``;`` survives and the line still reads as configuration.
_VALUE = r'(?:"[^"]*"|\S+)'

# Configuration statements that carry a secret. Each pattern keeps the
# statement and its non-secret arguments, and replaces only the value.
#
# The patterns run in order on the same line, so a later, looser one sees
# the earlier one's result. That is why the specific forms come first and
# the bare ``key`` forms last, and why the loose ones refuse to treat a
# keyword such as ``md5`` as the value: ``ntp authentication-key 1 md5
# <hash>`` used to have its key *number* masked and the hash left alone
# (#495). Every pattern here has a case in test_outbound.py, including the
# ordinary lines it must leave alone.
_PATTERNS: list[tuple[re.Pattern, str]] = [
    # Cisco / Arista: username x password [0|7] <secret>, enable secret 5
    # <hash>; Junos: secret "$9$...", authentication-password "...".
    #
    # The optional linking word is for prose, not configuration (#558).
    # A chat message is the first thing to go through here that is not
    # a device line, and "the password is hunter2" used to mask the
    # word "is" and store the password — the pattern masks whatever
    # follows the keyword, and in prose that is a preposition. No
    # configuration on any platform writes "password is", so allowing
    # the hop cannot change what a device line redacts to.
    (re.compile(r"(?i)\b(password|secret)(\s+\d)?(\s+(?:is|was|will be|should be|to|for|=|:))?\s+" + _VALUE),
     r"\1\2\3 " + MASK),

    # SNMP community strings — a read-write community is a credential.
    (re.compile(r"(?i)\b(snmp-server\s+community)\s+\S+"), r"\1 " + MASK),
    # `show snmp community` prints it as "Community name: <string>".
    (re.compile(r"(?i)\b(community\s+(?:name|securityname)\s*:)\s*\S+"), r"\1 " + MASK),
    # The trap receiver's community: snmp-server host <ip> [traps|informs]
    # [vrf X] [version 1|2c] <community> (for version 3 the token is the
    # user, which costs nothing to mask).
    (re.compile(r"(?i)\b(snmp-server\s+host\s+\S+"
                r"(?:\s+(?:traps|informs|vrf\s+\S+|version\s+(?:1|2c|3\s+(?:auth|noauth|priv))))*)"
                r"\s+(?!traps\b|informs\b|vrf\b|version\b)\S+"), r"\1 " + MASK),
    # Anything else that says "community <value>" — but not BGP communities,
    # which are route policy, not secrets: "set community 65000:100",
    # "match community CL", "send-community both", or the well-known names.
    (re.compile(r"(?i)(?<!set\s)(?<!match\s)(?<!send-)\b(community)\s+"
                r"(?!ro\b|rw\b|name\b|securityname\b|index\b|list\b|none\b"
                r"|internet\b|local-as\b|no-export\b|no-advertise\b|\d)\S+"), r"\1 " + MASK),

    # SNMPv3 users: auth (md5|sha) <pass> priv (des|3des|aes [bits]) <pass>;
    # NX-OS prints the localised form "auth md5 0x… priv [aes-128] 0x…
    # localizedkey". "priv" alone is not enough — a v3 group line reads
    # "v3 priv read <view>" — so a bare one has to be followed by a hex value.
    (re.compile(r"(?i)\b(auth\s+(?:md5|sha(?:-?\d+)?))\s+" + _VALUE), r"\1 " + MASK),
    (re.compile(r"(?i)\b(priv\s+(?:des|3des|aes(?:[\s-]+\d+)?))\s+" + _VALUE), r"\1 " + MASK),
    (re.compile(r"(?i)\b(priv)\s+0x\S+"), r"\1 " + MASK),

    # IPsec: crypto isakmp key [6] <secret> address <peer>; the keyring form
    # pre-shared-key address <peer> [mask] key [6] <secret>; IKEv2 and Junos
    # pre-shared-key [local|remote|ascii-text|hexadecimal] <secret>.
    (re.compile(r"(?i)\b(isakmp\s+key(?:\s+\d)?)\s+\S+"), r"\1 " + MASK),
    (re.compile(r"(?i)\b(pre-shared-key\s+(?:address|hostname)\s+.*?\bkey(?:\s+\d)?)\s+\S+"), r"\1 " + MASK),
    (re.compile(r"(?i)\b(pre-shared-key(?:\s+(?:local|remote|ascii-text|hexadecimal))?)\s+"
                r"(?!address\b|hostname\b)" + _VALUE), r"\1 " + MASK),

    # Keyed authentication. "key-string 0 <secret>" — the digit is the
    # encryption type, not the secret, so it has to be kept or the wrong
    # token gets masked. The same goes for the key number in "ntp
    # authentication-key 1 md5 <hash> [7]" and "ip ospf message-digest-key
    # 1 md5 [7] <hash>", and for the type digit in "ip ospf
    # authentication-key 7 <hash>"; Junos writes "authentication-key 1 type
    # md5 value <secret>" and "authentication-key <secret>".
    (re.compile(r"(?i)\b(key-string)(\s+\d)?\s+\S+"), r"\1\2 " + MASK),
    (re.compile(r"(?i)\b(authentication-key(?:\s+\d+\s+(?:type\s+md5\s+value|md5)|\s+[0-7])?)\s+" + _VALUE),
     r"\1 " + MASK),
    (re.compile(r"(?i)\b(message-digest-key\s+\d+\s+md5(?:\s+[0-7])?)\s+\S+"), r"\1 " + MASK),
    (re.compile(r"(?i)\b((?:tacacs|radius)-server\s+(?:host\s+\S+\s+)?key(?:\s+\d)?)\s+\S+"), r"\1 " + MASK),

    # Junos encrypted values.
    (re.compile(r"(?i)\b(encrypted-password)\s+" + _VALUE), r"\1 " + MASK),

    # Wireless.
    (re.compile(r"(?i)\b(wpa-psk\s+ascii\s+\d?)\s*\S+"), r"\1 " + MASK),

    # The bare forms, last. "key <type> <secret>" anywhere on a line
    # ("radius-server host … auth-port 1812 acct-port 1813 key 7 <hash>"),
    # keeping the type digit. Not the "key" of a hyphenated statement —
    # those have their own patterns above, and "authentication-key 1 type
    # md5 value …" is a key number followed by a keyword, not a secret.
    (re.compile(r"(?i)(?<!-)\b(key)(\s+[0-7])\s+(?!md5\b|sha\b)\S+"), r"\1\2 " + MASK),
    # "key <secret>" on a line of its own, which is the new-style "tacacs
    # server" / "radius server" sub-mode. Anchored, so "key" inside an
    # interface description is not a secret; and a bare number is a key
    # chain's "key 1", which is a label, not a credential.
    (re.compile(r"(?i)^(\s*key)\s+(?!\d+\s*$)" + _VALUE + r"\s*$"), r"\1 " + MASK),
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
