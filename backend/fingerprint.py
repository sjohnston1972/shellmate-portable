"""
fingerprint.py — Work out what kind of device is on the other end.

Everything platform-specific depends on this: which command turns paging off,
how to fetch the running config, what ``ints`` should expand to, which commands
deserve a confirmation prompt.  Get it wrong and the tool sends nonsense to a
device; refuse to guess and the engineer is back to typing everything.

Three sources, cheapest first:

**The login banner.**  Free — the device has already sent it.  Cisco IOS
announces itself, Junos prints its version, PAN-OS names itself.  Enough on its
own most of the time.

**The prompt shape.**  Weaker but always present.  ``user@host>`` is Junos or
PAN-OS; ``host(config-if)#`` is Cisco-like; ``host:~$`` is a shell;
``RP/0/RSP0/CPU0:host#`` is IOS-XR and nothing else.  Used to narrow things
down when the banner says nothing, which is common on devices configured with
a legal warning instead of a version string.

How much each shape is worth differs, and for one reason: whether it names
the *platform* or only the family.  ``<host>`` is Huawei VRP — and equally HP
Comware, which wants a different paging command — so it stays below the gate,
while the XR node id above it does not.

**A version command.**  Definitive, but costs a round trip and can only run
where a second channel is available.  Used to confirm and to capture the exact
version string, never as the first resort.

Every result carries a confidence.  Acting on a low-confidence guess is how a
tool ends up sending ``terminal length 0`` to a firewall, so the caller checks
it before doing anything that touches the device.
"""

import logging
import re
from dataclasses import dataclass

from backend.platforms import GENERIC, load_profiles
from backend.session.ansi import clean

logger = logging.getLogger(__name__)

# Confidence below which we identify the device but do not act on it.
ACT_THRESHOLD = 0.6


@dataclass
class Fingerprint:
    """What we believe the device to be."""

    platform: str = GENERIC
    name: str = "Unknown device"
    version: str = ""
    model: str = ""
    confidence: float = 0.0
    source: str = "none"   # banner | prompt | ssh-version | version-command | you

    @property
    def certain_enough_to_act(self) -> bool:
        """Whether to send this device platform-specific commands."""
        from backend.advanced import get as advanced

        return (self.platform != GENERIC
                and self.confidence >= advanced("identify.act_threshold"))

    def as_dict(self) -> dict:
        return {
            "platform":   self.platform,
            "name":       self.name,
            "version":    self.version,
            "model":      self.model,
            "confidence": round(self.confidence, 2),
            "source":     self.source,
        }


# ---------------------------------------------------------------------------
# Version extraction
# ---------------------------------------------------------------------------

def version_from(text: str, platform_id: str = "") -> str:
    """
    A version string out of ``text``, or "" (#536).

    The same patterns identification uses, exposed so a `show version` run
    for the inventory does not need a second copy of them. A platform id
    narrows it, which matters where two vendors' banners look alike.
    """
    if not text:
        return ""
    body = clean(text)
    for pattern_platform, pattern in VERSION_PATTERNS:
        if platform_id and pattern_platform != platform_id:
            continue
        found = pattern.search(body)
        if found:
            return found.group(1).strip()
    if platform_id:                      # nothing for that platform; try them all
        return version_from(body)
    return ""


# Ordered: the first pattern that matches wins, so the most specific come first.
VERSION_PATTERNS: list[tuple[str, re.Pattern]] = [
    # IOS-XR before IOS, and not for tidiness: an XR banner reads "Cisco IOS
    # XR Software, Version 7.3.2", which the IOS pattern below matches
    # perfectly well. First match wins *and* sets the platform, so the order
    # of these two lines is the difference between an XR router being
    # identified as XR and being identified as IOS at confidence 0.9 —
    # confident, wrong, and above the gate that sends commands.
    ("iosxr", re.compile(r"Cisco IOS ?XR Software.*?Version\s+([\w.()]+)", re.IGNORECASE)),
    ("ios",   re.compile(r"Cisco IOS.*?Version\s+([\w.()]+)", re.IGNORECASE)),
    ("nxos",  re.compile(r"NXOS:?\s*version\s+([\w.()]+)", re.IGNORECASE)),
    ("nxos",  re.compile(r"system:\s+version\s+([\w.()]+)", re.IGNORECASE)),
    ("asa",   re.compile(r"Adaptive Security Appliance.*?Version\s+([\w.()]+)", re.IGNORECASE)),
    ("junos", re.compile(r"JUNOS\s+(?:Software\s+Release\s+)?\[?([\w.\-]+)\]?", re.IGNORECASE)),
    ("panos", re.compile(r"sw-version:\s*([\w.\-]+)", re.IGNORECASE)),
    ("arista", re.compile(r"Software image version:\s*([\w.\-]+)", re.IGNORECASE)),
    # FortiOS: "Version: FortiGate-100F v7.2.5,build1517,230606 (GA.F)".
    ("fortios", re.compile(r"Version:\s*Forti[\w-]*\s+v([\w.]+)", re.IGNORECASE)),
    # Huawei VRP: "VRP (R) software, Version 5.170 (S5720 V200R019C10SPC500)".
    ("huawei", re.compile(r"VRP \(R\) software,\s*Version\s+([\w.]+)", re.IGNORECASE)),
    # RouterOS announces itself on login; `/system resource print` does not,
    # and gives "version: 7.11.2 (stable)" instead. The release channel in
    # brackets is what keeps the second pattern from matching every other
    # vendor's "version:" line.
    ("routeros", re.compile(r"RouterOS\s+v?(\d[\w.]*)", re.IGNORECASE)),
    ("routeros", re.compile(
        r"version:\s*(\d[\w.]*)\s*\((?:stable|long-term|testing|development)\)",
        re.IGNORECASE)),
    # AOS-CX: "Version      : FL.10.09.1010". The two-letter branch prefix is
    # the distinctive part — a bare "Version :" belongs to half the industry.
    ("aoscx", re.compile(r"Version\s*:\s*([A-Z]{2}\.[\w.]+)")),
]

MODEL_PATTERNS: list[re.Pattern] = [
    re.compile(r"cisco\s+(WS-[\w-]+|C\d{4}[\w-]*|ISR\d+[\w-]*|ASR[\w-]+|N\d?K-[\w-]+)", re.IGNORECASE),
    re.compile(r"Model:\s*([\w.\-]+)", re.IGNORECASE),
    re.compile(r"model:\s*([\w.\-]+)", re.IGNORECASE),
    re.compile(r"Hardware:\s*([\w.\-]+)", re.IGNORECASE),
    # After the generic ones, so nothing above changes meaning (#524).
    re.compile(r"Version:\s*(Forti[\w-]+)\s+v", re.IGNORECASE),
    re.compile(r"board-name:\s*([\w.\-/]+)", re.IGNORECASE),
    re.compile(r"\bHuawei\s+(S\d{3,4}[\w-]*|AR\d+[\w-]*|CE\d+[\w-]*"
               r"|NE\d+[\w-]*|USG\d+[\w-]*)", re.IGNORECASE),
    re.compile(r"\b(J[LG]\d{3,4}[A-Z])\b"),
]


def _match_signatures(text: str) -> tuple[str, float]:
    """
    Find the platform whose signature best matches *text*.

    The longest matching signature wins. An NX-OS banner also contains the
    word "cisco", so a shorter, vaguer match must never beat a specific one.
    """
    lowered = text.lower()
    best_platform, best_length = GENERIC, 0

    for platform_id, profile in load_profiles().items():
        for signature in profile.signatures:
            sig = (signature or "").lower()
            if not sig or len(sig) <= best_length:
                continue
            # A short signature must stand alone as a word (#323). "eos" as a
            # bare substring matched "videos" and "GeoServer" and identified
            # the device as Arista at acting confidence — and a banner match
            # can override a platform the user set by hand. Word boundaries
            # keep "EOS-4.30" and "NXOS:" while rejecting letters buried
            # inside another word. Enforced here rather than in the data,
            # because existing installs read signatures back from their own
            # platforms.json and would never see a cleaned built-in list.
            if len(sig) < 6:
                if re.search(rf"\b{re.escape(sig)}\b", lowered):
                    best_platform, best_length = platform_id, len(sig)
            elif sig in lowered:
                best_platform, best_length = platform_id, len(sig)

    if best_platform == GENERIC:
        return GENERIC, 0.0

    # A long, distinctive signature is worth more than a short generic one.
    confidence = 0.7 if best_length >= 8 else 0.6
    return best_platform, confidence


def _from_prompt(prompt: str) -> tuple[str, float]:
    """
    Guess from the shape of the prompt alone.

    Weak evidence, and mostly treated as such: enough to pick sensible
    aliases, rarely enough on its own to start sending commands.

    Nothing here requires configuration mode (#255). A fresh session lands
    at ``hostname>`` or ``hostname#``, and both are matched — at the weakest
    score, because a bare ``#`` is barely evidence. ``hostname(config)#`` is
    a *stronger* clue when it happens to be on screen, never a prerequisite:
    someone who later enters config mode sharpens the guess, and someone who
    never does still gets identified from the exec prompt or the banner.

    It is tempting to score the config shape higher — ``hostname(config)#``
    really is distinctive, and nothing else prints it.  But distinctive *of
    what?*  It says the device is Cisco-shaped, and IOS, NX-OS, ASA and EOS
    all print it identically.  The command we would act on belongs to the
    platform, not the family: ``terminal length 0`` is right on three of
    those four and wrong on an ASA, which wants ``terminal pager 0``.  A
    prompt that narrows the field to four vendors is not the same as one that
    picks the platform, so these stay below :data:`ACT_THRESHOLD` and the
    user gets an explicit override instead (see ``onboard.as_chosen``).

    ``:~`` is the exception, and only because it fails safely: a shell prompt
    is unmistakable, and the Linux profile has no paging command to get wrong.
    Raising it buys the aliases and costs nothing.
    """
    if not prompt:
        return GENERIC, 0.0

    stripped = prompt.rstrip()

    # RouterOS, before the "@" test below claims it for Junos. Nothing else
    # prints [user@host] >, and RouterOS has no paging command to get wrong,
    # so this is the same bargain the shell prompt gets: unmistakable, and it
    # fails safely.
    if stripped.startswith("[") and "@" in stripped and stripped.endswith(">"):
        return "routeros", 0.7

    # Huawei VRP: <host> is the user view, [host] the system view. Below the
    # gate on purpose — HP Comware, which came from the same lineage, prints
    # both identically and wants `screen-length disable` rather than VRP's
    # `screen-length 0 temporary`. Family, not platform.
    if stripped.startswith("<") and stripped.endswith(">"):
        return "huawei", 0.5
    if stripped.startswith("[") and stripped.endswith("]"):
        return "huawei", 0.5

    # IOS-XR names the node it landed on: RP/0/RSP0/CPU0:hostname#. Nothing
    # else prints that, and it picks the platform rather than the family, so
    # unlike hostname(config)# it is worth acting on.
    if re.match(r"^(?:RP|LC|SP|DRP|RSP)/[\w./-]*:", stripped):
        return "iosxr", 0.7

    if ":~" in prompt:
        return "linux", 0.7          # unmistakable, and nothing is sent anyway
    if prompt.rstrip().endswith("$"):
        return "linux", 0.5          # probably a shell; could be a prompt style
    if re.search(r"\((?:active|passive|suspended)\)\s*[>#]", prompt, re.IGNORECASE):
        return "panos", 0.5
    if "@" in prompt:
        return "junos", 0.4          # Junos and PAN-OS look alike here
    if "/pri/" in prompt or "/sec/" in prompt:
        return "asa", 0.6            # failover context is distinctive
    if re.search(r"\(config[^)]*\)\s*#", prompt):
        return "ios", 0.5            # Cisco-shaped — but so are NX-OS and ASA
    # FortiOS names the current VDOM or object in the prompt: "FGT (global) #".
    # Weak, and it need not be stronger: the FortiOS profile sends nothing.
    if re.search(r"\((?:global|vdom[^)]*|root)\)\s*#", prompt, re.IGNORECASE):
        return "fortios", 0.5
    if prompt.rstrip().endswith(("#", ">")):
        return "ios", 0.35           # the commonest, but barely evidence
    return GENERIC, 0.0


# ---------------------------------------------------------------------------
# The SSH version string
#
# A fourth source, and the cheapest of all: RFC 4253 requires an SSH server to
# announce itself as ``SSH-2.0-<software>`` before anything is negotiated, so
# it arrives on any connection — including one that is opened and immediately
# closed, which is what network discovery does.
#
# It is weaker evidence than a login banner. "SSH-2.0-Cisco-1.25" says Cisco
# with certainty and says nothing about whether the device is IOS, NX-OS or an
# ASA, and the confidence below reflects that: enough to show the engineer,
# deliberately *below* the threshold at which ShellMate sends the device
# anything. A guess good enough to display is not a guess good enough to act
# on.
# ---------------------------------------------------------------------------

SSH_VERSION_SIGNATURES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"SSH-2\.0-Cisco", re.IGNORECASE), "ios"),
    (re.compile(r"SSH-\d\.\d-.*\bNX-?OS\b", re.IGNORECASE), "nxos"),
    (re.compile(r"SSH-\d\.\d-.*\bArista\b", re.IGNORECASE), "arista"),
    (re.compile(r"SSH-\d\.\d-.*\bJuniper\b", re.IGNORECASE), "junos"),
    (re.compile(r"SSH-\d\.\d-.*\bPaloAlto\b", re.IGNORECASE), "panos"),
    # RouterOS answers "SSH-2.0-ROSSSH"; Huawei VRP "SSH-2.0-HUAWEI-1.5".
    (re.compile(r"SSH-\d\.\d-ROSSSH", re.IGNORECASE), "routeros"),
    (re.compile(r"SSH-\d\.\d-HUAWEI", re.IGNORECASE), "huawei"),
    # OpenSSH is on everything from a Linux host to a modern switch, so it
    # narrows nothing on its own — but on a discovery sweep, "this is a host
    # running OpenSSH" is still more than "port 22 is open".
    (re.compile(r"SSH-\d\.\d-OpenSSH", re.IGNORECASE), "linux"),
]

#: What an SSH version string is worth. Below `identify.act_threshold` on
#: purpose — see the note above.
SSH_VERSION_CONFIDENCE = 0.5


def _from_ssh_version(text: str) -> tuple[str, float]:
    """Identify from the server's SSH announcement, or (GENERIC, 0.0)."""
    for pattern, platform_id in SSH_VERSION_SIGNATURES:
        if pattern.search(text):
            return platform_id, SSH_VERSION_CONFIDENCE
    return GENERIC, 0.0


def identify(banner: str = "", prompt: str = "") -> Fingerprint:
    """
    Identify a device from what it has already told us.

    Args:
        banner: Everything received since connecting.
        prompt: The most recent prompt, if one has been seen.

    Returns:
        A Fingerprint. Never raises — an unidentifiable device is a valid
        outcome and simply yields the generic profile.
    """
    text = clean(banner or "")

    platform, confidence = _match_signatures(text)
    source = "banner"

    if platform == GENERIC:
        platform, confidence = _from_prompt(prompt)
        source = "prompt" if platform != GENERIC else "none"

    # Last, because it is the weakest: a device that printed a real banner has
    # already been identified better than its SSH version string can manage.
    if platform == GENERIC:
        platform, confidence = _from_ssh_version(text)
        source = "ssh-version" if platform != GENERIC else "none"

    version, model = "", ""
    for platform_id, pattern in VERSION_PATTERNS:
        match = pattern.search(text)
        if match:
            version = match.group(1)
            # A version string is strong evidence, and outranks a prompt guess.
            if platform_id != platform:
                platform, source = platform_id, "banner"
            confidence = max(confidence, 0.9)
            break

    for pattern in MODEL_PATTERNS:
        match = pattern.search(text)
        if match:
            model = match.group(1)
            break

    profile = load_profiles().get(platform)
    return Fingerprint(
        platform=platform,
        name=profile.name if profile else "Unknown device",
        version=version,
        model=model,
        confidence=confidence,
        source=source,
    )


def refine_with_version_output(current: Fingerprint, output: str) -> Fingerprint:
    """
    Improve a fingerprint using the output of a version command.

    This is the definitive source, so its result replaces whatever the banner
    and prompt suggested.
    """
    refined = identify(banner=output, prompt="")
    if refined.platform == GENERIC:
        return current

    refined.source = "version-command"
    refined.confidence = max(refined.confidence, 0.95)
    # Keep anything the earlier pass found that this one did not.
    refined.version = refined.version or current.version
    refined.model = refined.model or current.model
    return refined
