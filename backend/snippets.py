"""
snippets.py — The saved command library.

Broadcasting is most useful for the things you do over and over: save the
configuration on twelve switches, collect a version banner from a whole
access layer, pull the log off everything that was touched last night. Typing
those out each time is where the mistakes come from — a broadcast is exactly
the wrong place to be improvising a command.

So the library is a small set of named snippets, seeded with the ones most
people want on the first day and editable from there.

A snippet holds a *list* of commands, not one command, because the useful
units are usually sequences: save and then verify, or set and then show. The
wait between them is part of the snippet, since how long a device needs before
it will answer the next command is a property of the task rather than a
preference.

Like ``platforms.json``, this is **data, not code**: the built-in set below is
written to ``snippets.json`` in the data directory on first run and read back
in preference to the defaults, so adding one is a text edit rather than a
rebuild.
"""

import json
import logging
import uuid
from dataclasses import dataclass, field, asdict

from backend import paths

logger = logging.getLogger(__name__)


@dataclass
class Snippet:
    """One named list of commands."""

    id: str
    name: str
    commands: list[str] = field(default_factory=list)
    description: str = ""
    # "" means it applies anywhere. Otherwise a platform id from platforms.py,
    # used to sort the obviously-wrong ones down rather than to forbid them —
    # ShellMate does not always know what a device is, and refusing to show a
    # command because of a weak guess would be worse than showing it.
    platform: str = ""
    # Milliseconds to wait after each command before sending the next.
    wait_ms: int = 500
    # True when this writes to the device. Drives the confirmation wording;
    # it is a label on the snippet, not a security control.
    writes: bool = False
    builtin: bool = False

    def as_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# The built-in library
#
# Chosen to be the things a network engineer actually broadcasts, rather than a
# demonstration of the feature. Anything that writes to the device is marked,
# and there is deliberately nothing destructive in here — a shipped library is
# not the place to put `write erase` one mis-click away.
# ---------------------------------------------------------------------------

BUILTIN: list[Snippet] = [
    Snippet(
        id="save-config-ios",
        name="Save configuration (IOS)",
        commands=["write memory"],
        description="Copy running-config to startup-config.",
        platform="ios", wait_ms=2000, writes=True, builtin=True,
    ),
    Snippet(
        id="save-config-nxos",
        name="Save configuration (NX-OS)",
        commands=["copy running-config startup-config"],
        description="NX-OS has no `write memory` on newer images.",
        platform="nxos", wait_ms=3000, writes=True, builtin=True,
    ),
    Snippet(
        id="save-config-junos",
        name="Commit (Junos)",
        commands=["commit"],
        description="Commit the candidate configuration.",
        platform="junos", wait_ms=3000, writes=True, builtin=True,
    ),
    Snippet(
        id="save-and-verify",
        name="Save and verify (IOS)",
        commands=["write memory", "show startup-config | include ^Building|^!"],
        description="Save, then confirm the device really wrote it.",
        platform="ios", wait_ms=2500, writes=True, builtin=True,
    ),
    Snippet(
        id="version",
        name="Version and uptime",
        commands=["show version"],
        description="What it is, what it is running, how long since it rebooted.",
        builtin=True,
    ),
    Snippet(
        id="interfaces-brief",
        name="Interface summary",
        commands=["show ip interface brief"],
        description="Addresses and up/down state.",
        platform="ios", builtin=True,
    ),
    Snippet(
        id="interface-status",
        name="Port status",
        commands=["show interfaces status"],
        description="Access-layer view: description, vlan, duplex, speed.",
        platform="ios", builtin=True,
    ),
    Snippet(
        id="interface-errors",
        name="Interface errors",
        commands=["show interfaces | include line protocol|error|CRC|drops"],
        description="The counters worth looking at when something is slow.",
        platform="ios", wait_ms=1500, builtin=True,
    ),
    Snippet(
        id="running-config",
        name="Running configuration",
        commands=["show running-config"],
        description="The whole configuration. Paging is already off.",
        platform="ios", wait_ms=4000, builtin=True,
    ),
    Snippet(
        id="routing",
        name="Routing table",
        commands=["show ip route"],
        description="",
        platform="ios", wait_ms=2000, builtin=True,
    ),
    Snippet(
        id="neighbours",
        name="Neighbours (CDP and LLDP)",
        commands=["show cdp neighbors detail", "show lldp neighbors detail"],
        description="What is plugged into what.",
        platform="ios", wait_ms=2000, builtin=True,
    ),
    Snippet(
        id="logs",
        name="Recent log",
        commands=["show logging | last 100"],
        description="The last hundred lines of the local buffer.",
        platform="ios", wait_ms=2000, builtin=True,
    ),
    Snippet(
        id="health-check",
        name="Health check",
        commands=[
            "show version | include uptime|Version",
            "show processes cpu | include CPU utilization",
            "show memory statistics | include Processor",
            "show environment all",
        ],
        description="A quick sweep before or after a change window.",
        platform="ios", wait_ms=1500, builtin=True,
    ),
    Snippet(
        id="spanning-tree",
        name="Spanning tree summary",
        commands=["show spanning-tree summary"],
        description="Root bridge, port counts and blocked ports.",
        platform="ios", wait_ms=1500, builtin=True,
    ),
    Snippet(
        id="inventory",
        name="Hardware inventory",
        commands=["show inventory"],
        description="Serial numbers and part codes, for an audit or an RMA.",
        platform="ios", wait_ms=1500, builtin=True,
    ),
    Snippet(
        id="arp-mac",
        name="ARP and MAC tables",
        commands=["show ip arp", "show mac address-table"],
        description="Where a device is, when you have an address and need a port.",
        platform="ios", wait_ms=2000, builtin=True,
    ),
    Snippet(
        id="bgp-summary",
        name="BGP summary",
        commands=["show ip bgp summary"],
        description="Neighbour states and prefix counts.",
        platform="ios", wait_ms=2000, builtin=True,
    ),
    Snippet(
        id="ntp-clock",
        name="Clock and NTP",
        commands=["show clock", "show ntp status"],
        description="Worth checking before trusting anything's timestamps.",
        platform="ios", builtin=True,
    ),
]


def snippets_path():
    return paths.data_dir() / "snippets.json"


def _write_all(snippets: list[Snippet]) -> None:
    path = snippets_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps([s.as_dict() for s in snippets], indent=2),
        encoding="utf-8",
    )


def load_snippets() -> list[Snippet]:
    """
    Return the library: the built-ins, plus whatever the user has added.

    A user edit to a built-in wins, and a built-in the user has deleted stays
    deleted — the file is the record of what the library *is*, not a set of
    overrides layered onto the defaults. The alternative resurrects things
    someone deliberately removed, which is the more annoying failure.
    """
    path = snippets_path()

    if not path.exists():
        _write_all(BUILTIN)
        return list(BUILTIN)

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("snippets.json is unreadable (%s); using the built-ins", exc)
        return list(BUILTIN)

    if not isinstance(raw, list):
        return list(BUILTIN)

    out = []
    for item in raw:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        known = {f: item.get(f) for f in Snippet.__dataclass_fields__ if f in item}
        known.setdefault("name", known.get("id", ""))
        commands = known.get("commands") or []
        known["commands"] = [str(c) for c in commands if str(c).strip()]
        try:
            out.append(Snippet(**known))
        except TypeError:
            continue

    return out or list(BUILTIN)


def save_snippet(fields: dict) -> Snippet:
    """Create or update one snippet. Returns what was stored."""
    name = (fields.get("name") or "").strip()
    if not name:
        raise ValueError("A snippet needs a name.")

    commands = [str(c).strip() for c in (fields.get("commands") or []) if str(c).strip()]
    if not commands:
        raise ValueError("A snippet needs at least one command.")

    snippet_id = (fields.get("id") or "").strip() or f"user-{uuid.uuid4().hex[:8]}"

    library = load_snippets()
    existing = next((s for s in library if s.id == snippet_id), None)

    updated = Snippet(
        id=snippet_id,
        name=name,
        commands=commands,
        description=(fields.get("description") or "").strip(),
        platform=(fields.get("platform") or "").strip(),
        wait_ms=max(0, min(60_000, int(fields.get("wait_ms", 500) or 0))),
        writes=bool(fields.get("writes")),
        # Editing a built-in keeps the flag, so the interface can still say
        # where it came from.
        builtin=bool(existing.builtin) if existing else False,
    )

    if existing:
        library[library.index(existing)] = updated
    else:
        library.append(updated)

    _write_all(library)
    return updated


def delete_snippet(snippet_id: str) -> bool:
    """Remove a snippet. Built-ins can be removed too — see load_snippets."""
    library = load_snippets()
    remaining = [s for s in library if s.id != snippet_id]
    if len(remaining) == len(library):
        return False
    _write_all(remaining)
    return True


def reset_to_defaults() -> list[Snippet]:
    """Put the shipped library back, discarding every edit."""
    _write_all(BUILTIN)
    return list(BUILTIN)
