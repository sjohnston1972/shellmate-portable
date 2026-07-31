"""
platforms.py — What ShellMate knows about each kind of device.

A network estate is rarely one vendor.  The same intent — "turn paging off",
"show me the interfaces", "dump the running config" — is a different command
on IOS, NX-OS, Junos and PAN-OS, and the differences are exactly the kind of
detail that wastes an engineer's attention while they are trying to fix
something.

Each profile collects everything platform-specific in one place:

``paging_off``          Sent on connect, so nobody types ``terminal length 0``
                       for the hundredth time this week.
``show_run``           How to retrieve the running configuration.
``version_command``    How to ask what the device is.
``aliases``            Short names mapped to the real command per platform.
``dangerous_commands``  Feeds the Phase 6 guardrails.
``config_mode_markers`` Prompt fragments meaning "you are editing live config".

Profiles are **data**, not code.  The built-in set below is written to
``platforms.json`` in the data directory on first run, and the file is read
back in preference to the defaults.  A new platform, or a correction to an
existing one, is then a text edit rather than a rebuild — which matters when
the person who needs it is on a customer site with the executable on a stick
and no Python toolchain anywhere.
"""

import json
import logging
from dataclasses import dataclass, field, asdict

from backend import paths

logger = logging.getLogger(__name__)

GENERIC = "generic"


@dataclass
class PlatformProfile:
    """Everything ShellMate knows about one family of device."""

    id: str
    name: str
    paging_off: str = ""
    show_run: str = ""
    version_command: str = ""
    # Substrings that identify this platform in a banner or version output.
    # Matched case-insensitively; the longest match wins so that "nx-os" beats
    # the "cisco" that also appears in the same banner.
    signatures: list[str] = field(default_factory=list)
    aliases: dict[str, str] = field(default_factory=dict)
    dangerous_commands: list[str] = field(default_factory=list)
    config_mode_markers: list[str] = field(default_factory=list)
    comment_prefix: str = "!"

    def as_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Built-in profiles
# ---------------------------------------------------------------------------

BUILTIN: dict[str, PlatformProfile] = {
    "ios": PlatformProfile(
        id="ios",
        name="Cisco IOS / IOS-XE",
        paging_off="terminal length 0",
        show_run="show running-config",
        version_command="show version",
        signatures=["cisco ios software", "ios-xe", "ios software", "cisco ios"],
        aliases={
            "ints":     "show ip interface brief",
            "int":      "show ip interface brief",
            "ver":      "show version",
            "run":      "show running-config",
            "start":    "show startup-config",
            "arp":      "show ip arp",
            "mac":      "show mac address-table",
            "cdp":      "show cdp neighbors detail",
            "lldp":     "show lldp neighbors detail",
            "routes":   "show ip route",
            "bgp":      "show ip bgp summary",
            "ospf":     "show ip ospf neighbor",
            "vlans":    "show vlan brief",
            "trunks":   "show interfaces trunk",
            "log":      "show logging",
            "errors":   "show interfaces | include error|CRC|drop",
            "poe":      "show power inline",
            "stp":      "show spanning-tree summary",
            "uptime":   "show version | include uptime",
        },
        dangerous_commands=[
            "reload", "write erase", "erase startup-config", "erase nvram:",
            "delete flash:", "format flash:", "shutdown", "no shutdown",
            "clear counters", "clear ip bgp", "clear line", "no router",
            "no interface", "no vlan", "boot system",
        ],
        config_mode_markers=["(config", "(conf-"],
        comment_prefix="!",
    ),
    "nxos": PlatformProfile(
        id="nxos",
        name="Cisco NX-OS",
        paging_off="terminal length 0",
        show_run="show running-config",
        version_command="show version",
        signatures=["nx-os", "nxos", "nexus"],
        aliases={
            "ints":   "show interface brief",
            "int":    "show interface brief",
            "ver":    "show version",
            "run":    "show running-config",
            "arp":    "show ip arp",
            "mac":    "show mac address-table",
            "cdp":    "show cdp neighbors detail",
            "routes": "show ip route",
            "bgp":    "show ip bgp summary",
            "vlans":  "show vlan brief",
            "vpc":    "show vpc",
            "log":    "show logging last 100",
            "uptime": "show version | include uptime",
        },
        dangerous_commands=[
            "reload", "write erase", "shutdown", "no shutdown",
            "clear counters", "delete bootflash:", "no feature",
        ],
        config_mode_markers=["(config"],
        comment_prefix="!",
    ),
    "asa": PlatformProfile(
        id="asa",
        name="Cisco ASA",
        paging_off="terminal pager 0",
        show_run="show running-config",
        version_command="show version",
        signatures=["adaptive security appliance", "cisco asa"],
        aliases={
            "ints":   "show interface ip brief",
            "ver":    "show version",
            "run":    "show running-config",
            "routes": "show route",
            "xlate":  "show xlate",
            "conns":  "show conn count",
            "failover": "show failover",
            "log":    "show logging",
        },
        dangerous_commands=[
            "reload", "write erase", "clear configure", "shutdown",
            "no shutdown", "failover active", "no failover",
        ],
        config_mode_markers=["(config"],
        comment_prefix="!",
    ),
    "junos": PlatformProfile(
        id="junos",
        name="Juniper Junos",
        paging_off="set cli screen-length 0",
        show_run="show configuration | display set",
        version_command="show version",
        signatures=["junos", "juniper"],
        aliases={
            "ints":   "show interfaces terse",
            "int":    "show interfaces terse",
            "ver":    "show version",
            "run":    "show configuration | display set",
            "arp":    "show arp",
            "routes": "show route",
            "bgp":    "show bgp summary",
            "ospf":   "show ospf neighbor",
            "lldp":   "show lldp neighbors",
            "log":    "show log messages | last 100",
            "uptime": "show system uptime",
            "alarms": "show chassis alarms",
        },
        dangerous_commands=[
            "request system reboot", "request system halt", "delete",
            "rollback 0", "load override", "commit", "deactivate",
            "request system zeroize",
        ],
        config_mode_markers=["[edit"],
        comment_prefix="#",
    ),
    "panos": PlatformProfile(
        id="panos",
        name="Palo Alto PAN-OS",
        paging_off="set cli pager off",
        show_run="show config running",
        version_command="show system info",
        signatures=["pan-os", "palo alto", "panorama"],
        aliases={
            "ints":     "show interface all",
            "ver":      "show system info",
            "run":      "show config running",
            "routes":   "show routing route",
            "arp":      "show arp all",
            "sessions": "show session all",
            "log":      "show log system direction equal backward",
            "ha":       "show high-availability state",
        },
        dangerous_commands=[
            "request restart system", "request shutdown system", "delete",
            "commit", "request system private-data-reset", "set operational-mode",
        ],
        config_mode_markers=["# "],
        comment_prefix="#",
    ),
    "arista": PlatformProfile(
        id="arista",
        name="Arista EOS",
        paging_off="terminal length 0",
        show_run="show running-config",
        version_command="show version",
        signatures=["arista", "eos"],
        aliases={
            "ints":   "show interfaces status",
            "ver":    "show version",
            "run":    "show running-config",
            "arp":    "show ip arp",
            "mac":    "show mac address-table",
            "routes": "show ip route",
            "bgp":    "show ip bgp summary",
            "lldp":   "show lldp neighbors",
            "mlag":   "show mlag",
            "log":    "show logging last 100",
        },
        dangerous_commands=[
            "reload", "write erase", "shutdown", "no shutdown", "delete flash:",
        ],
        config_mode_markers=["(config"],
        comment_prefix="!",
    ),
    "linux": PlatformProfile(
        id="linux",
        name="Linux / Unix shell",
        paging_off="",              # a shell has no pager to disable globally
        show_run="",
        version_command="uname -a",
        signatures=["gnu/linux", "ubuntu", "debian", "centos", "red hat"],
        aliases={
            "ints":   "ip -br addr",
            "routes": "ip route",
            "arp":    "ip neigh",
            "listen": "ss -tulpn",
            "log":    "journalctl -n 100 --no-pager",
        },
        dangerous_commands=["rm -rf", "mkfs", "dd if=", "shutdown", "reboot", "init 0"],
        config_mode_markers=[],
        comment_prefix="#",
    ),
    GENERIC: PlatformProfile(
        id=GENERIC,
        name="Unknown device",
        # Nothing is sent to a device we cannot identify. Guessing a
        # paging command and having it land as a syntax error — or worse,
        # as something meaningful on an unrecognised platform — is a poor
        # trade for saving one line of typing.
        paging_off="",
        show_run="",
        version_command="",
        signatures=[],
        aliases={},
        dangerous_commands=["reload", "reboot", "erase", "format", "shutdown"],
        config_mode_markers=["(config", "[edit"],
        comment_prefix="#",
    ),
}


# ---------------------------------------------------------------------------
# Loading and overrides
# ---------------------------------------------------------------------------

_cache: dict[str, PlatformProfile] | None = None


def profiles_path():
    """Where the editable copy of the platform definitions lives."""
    return paths.data_dir() / "platforms.json"


def load_profiles(refresh: bool = False) -> dict[str, PlatformProfile]:
    """
    Return every known platform profile.

    Writes the built-in set to disk on first run so there is something to edit,
    then prefers whatever is on disk. A malformed or partial file falls back to
    the built-ins for the affected platform rather than leaving ShellMate with
    no idea what any device is.
    """
    global _cache
    if _cache is not None and not refresh:
        return _cache

    merged = {key: PlatformProfile(**profile.as_dict()) for key, profile in BUILTIN.items()}

    path = profiles_path()
    if not path.exists():
        _write_defaults(path)
    else:
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
            for key, values in (stored.get("platforms") or {}).items():
                if not isinstance(values, dict):
                    continue
                base = merged.get(key)
                if base is None:
                    # A platform the user added themselves.
                    values.setdefault("id", key)
                    values.setdefault("name", key)
                    merged[key] = PlatformProfile(**_only_known_fields(values))
                else:
                    merged[key] = PlatformProfile(**{**base.as_dict(), **_only_known_fields(values)})
        except (OSError, json.JSONDecodeError, TypeError) as exc:
            logger.warning("Ignoring unreadable platforms.json (%s); using built-ins", exc)

    _cache = merged
    return merged


def _only_known_fields(values: dict) -> dict:
    """Drop unrecognised keys so a stray field cannot break construction."""
    allowed = set(PlatformProfile.__dataclass_fields__)
    return {k: v for k, v in values.items() if k in allowed}


def _write_defaults(path) -> None:
    """Write the built-in profiles out so the user has something to edit."""
    document = {
        "_comment": (
            "Platform definitions for ShellMate Portable. Edit freely — this "
            "file is read in preference to the built-in defaults. Delete it to "
            "restore them."
        ),
        "platforms": {key: profile.as_dict() for key, profile in BUILTIN.items()},
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document, indent=2), encoding="utf-8")
        logger.info("Wrote default platform definitions to %s", path)
    except OSError as exc:
        logger.warning("Could not write platform definitions: %s", exc)


def get_profile(platform_id: str) -> PlatformProfile:
    """Return one profile, falling back to the generic one."""
    profiles = load_profiles()
    return profiles.get(platform_id) or profiles[GENERIC]


def resolve_alias(platform_id: str, command: str) -> str | None:
    """
    Expand a short alias for this platform.

    Only a bare alias on its own is expanded. "ints" becomes the platform's
    interface command; "show ints" is left alone, because the user has clearly
    typed a real command and silently rewriting the middle of it would be
    worse than not helping at all.
    """
    stripped = command.strip()
    if not stripped or " " in stripped:
        return None
    expanded = get_profile(platform_id).aliases.get(stripped.lower())
    return expanded if expanded and expanded != stripped else None
