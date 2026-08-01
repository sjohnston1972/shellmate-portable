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
    # Offered on a tab's right-click menu, for the handful sent dozens of
    # times a day. A flag on the library rather than a second store: there is
    # one place commands are written down and one editor for it, whichever
    # route you arrive by.
    quick: bool = False
    # Whether to press Enter. False types the command into the session and
    # leaves it — which is what makes a shortcut usable for something you want
    # to read before running, and is the difference between a shortcut and a
    # hazard for anything destructive.
    send_return: bool = True
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


# ---------------------------------------------------------------------------
# The generated library
#
# The hand-written set above is Cisco-heavy, because writing the same fourteen
# intents out for seven platforms by hand is how a library ends up wrong: the
# alias table already knows that "interfaces" is ``show ip interface brief`` on
# IOS, ``show interfaces terse`` on Junos and ``show interface all`` on PAN-OS,
# and a second copy of that knowledge in a different file will disagree with it
# eventually.
#
# So these are derived from ``platforms.py`` at load time. A platform someone
# adds gets a library for free, and correcting a command in one place corrects
# it everywhere.
#
# Only intents worth sending to a *fleet* are here. Plenty of aliases are
# useful at a single prompt and pointless across forty switches.
# ---------------------------------------------------------------------------

#: alias -> (display name, why you would broadcast it)
BROADCASTABLE: dict[str, tuple[str, str]] = {
    "ver":     ("Version",                "What everything is running, for an upgrade plan or an advisory."),
    "uptime":  ("Uptime",                 "Which devices rebooted, and when."),
    "ints":    ("Interface summary",      "Addresses and state across the estate."),
    "port":    ("Port status",            "What is up, down, or err-disabled."),
    "errors":  ("Interface errors",       "The counters that find a bad cable before anyone reports it."),
    "desc":    ("Interface descriptions", "Auditing what the cabling records claim."),
    "nei":     ("Neighbours",             "Building a topology from what the devices themselves see."),
    "log":     ("Recent log",             "What happened last night, everywhere at once."),
    "inv":     ("Hardware inventory",     "Serial numbers and modules, for support contracts."),
    "routes":  ("Routing table",          "Where traffic is actually going."),
    "bgp":     ("BGP summary",            "Neighbours up, and how many prefixes."),
    "arp":     ("ARP table",              "Finding where a host is."),
    "mac":     ("MAC address table",      "Same question, one layer down."),
    "stp":     ("Spanning tree",          "Root bridge and topology changes."),
    "vlans":   ("VLANs",                  "Auditing what exists where."),
    "cpu":     ("CPU",                    "Load across the estate."),
    "mem":     ("Memory",                 "Free memory, before an image upgrade."),
    "ntp":     ("Clock and NTP",          "Worth checking before trusting anyone's timestamps."),
    "temp":    ("Temperature",            "After an air-conditioning failure."),
    "power":   ("Power",                  "Supplies and PoE budget."),
    "ha":      ("High availability",      "Which unit is active."),
    "run":     ("Running configuration",  "A full config off every device at once."),
}

#: A single request that answers "is this device healthy", per platform.
HEALTH_CHECK = ("ver", "ints", "errors", "log")


def generated_snippets() -> list["Snippet"]:
    """
    Build per-platform snippets from the alias table.

    Returns one snippet per (platform, intent) the platform actually defines,
    plus a health check for each platform that has enough of the pieces.
    Generic is skipped — it has no aliases by design.
    """
    from backend.platforms import GENERIC, load_profiles

    out: list[Snippet] = []

    for platform_id, profile in sorted(load_profiles().items()):
        if platform_id == GENERIC or not profile.aliases:
            continue

        for alias, (title, why) in BROADCASTABLE.items():
            command = profile.aliases.get(alias)
            if not command:
                continue
            out.append(Snippet(
                id=f"gen-{platform_id}-{alias}",
                name=title,
                commands=[command],
                description=why,
                platform=platform_id,
                # Read-only by construction: everything above is a show.
                wait_ms=500, writes=False, builtin=True,
            ))

        steps = [profile.aliases[a] for a in HEALTH_CHECK if profile.aliases.get(a)]
        if len(steps) >= 3:
            out.append(Snippet(
                id=f"gen-{platform_id}-health",
                name="Health check",
                commands=steps,
                description="Version, interfaces, errors and the recent log, in one pass.",
                platform=platform_id,
                wait_ms=800, writes=False, builtin=True,
            ))

    return out


def all_builtins() -> list["Snippet"]:
    """
    The full shipped library: hand-written plus generated.

    The hand-written ones win on an id clash, since they exist precisely
    because the generated form was not good enough.
    """
    seen = {s.id for s in BUILTIN}
    return list(BUILTIN) + [s for s in generated_snippets() if s.id not in seen]


def snippets_path():
    return paths.data_dir() / "snippets.json"


def _known_ids() -> set[str]:
    """
    Every built-in id this installation has already been offered.

    Read straight off disk rather than from the loaded library, because the
    two answer different questions: the library is what the user *has*, this
    is what they have *been shown* — and a built-in they deleted is in the
    second and not the first. Conflating them is what makes a deletion
    reappear on the next launch.
    """
    path = snippets_path()
    if not path.exists():
        return set()
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return set()
    if isinstance(document, dict):
        return set(document.get("known_builtins") or [])
    # An old bare-list file records no history; everything in it counts.
    if isinstance(document, list):
        return {i.get("id") for i in document if isinstance(i, dict) and i.get("id")}
    return set()


def _write_all(snippets: list[Snippet], known: set[str] | None = None) -> None:
    """
    Write the library.

    ``known`` records every built-in id this installation has ever been
    offered. Without it there is no way to tell a built-in the user deleted
    from one that did not exist when their file was written — and the library
    can never grow without either resurrecting deletions or never reaching
    anyone who has opened Broadcast once.
    """
    path = snippets_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    if known is None:
        known = {s.id for s in snippets if s.builtin}

    document = {
        "_comment": (
            "The command library used by Broadcast. Edit freely — this file is "
            "read in preference to the built-ins. Delete an entry and it stays "
            "deleted; delete the whole file to start again. Entries with an id "
            "beginning 'gen-' are generated from the alias table in "
            "platforms.json, so correcting a command there corrects it here."
        ),
        "known_builtins": sorted(known),
        "snippets": [s.as_dict() for s in snippets],
    }
    path.write_text(json.dumps(document, indent=2), encoding="utf-8")


def load_snippets() -> list[Snippet]:
    """
    Return the library: the built-ins, plus whatever the user has added.

    A user edit to a built-in wins, and a built-in the user has deleted stays
    deleted — the file is the record of what the library *is*, not a set of
    overrides layered onto the defaults. The alternative resurrects things
    someone deliberately removed, which is the more annoying failure.
    """
    path = snippets_path()
    builtins = all_builtins()

    if not path.exists():
        _write_all(builtins)
        return builtins

    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("snippets.json is unreadable (%s); using the built-ins", exc)
        return builtins

    # The file was a bare list before it carried "known_builtins". An older
    # file records what the user has, so treat everything in it as offered —
    # a built-in they deleted before this version comes back once, and
    # deleting it again sticks. The alternative is that a growing library
    # never reaches anybody who has opened Broadcast.
    if isinstance(document, list):
        raw, known = document, {i.get("id") for i in document if isinstance(i, dict)}
    elif isinstance(document, dict):
        raw = document.get("snippets") or []
        known = set(document.get("known_builtins") or [])
    else:
        return builtins

    out = []
    for item in raw:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        # Named "fields" rather than "known": the set of ids the file has
        # already been offered is called that, one scope out.
        fields = {f: item.get(f) for f in Snippet.__dataclass_fields__ if f in item}
        fields.setdefault("name", fields.get("id", ""))
        commands = fields.get("commands") or []
        fields["commands"] = [str(c) for c in commands if str(c).strip()]
        try:
            out.append(Snippet(**fields))
        except TypeError:
            continue

    # Anything shipped since this file was written is new to it, so append it.
    # A built-in whose id is already in known_builtins but absent from the list
    # was deleted deliberately and stays gone.
    existing = {s.id for s in out}
    added = [s for s in builtins if s.id not in known and s.id not in existing]
    if added:
        logger.info("Adding %s new built-in snippet(s) to the library", len(added))
        out.extend(added)
        _write_all(out, known | {s.id for s in builtins})

    return out or builtins



def quick_snippets() -> list[Snippet]:
    """The ones offered on a tab's right-click menu."""
    return [s for s in load_snippets() if s.quick]


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
        quick=bool(fields.get("quick")),
        # Defaults to true so an entry saved without thinking about it behaves
        # the way every other command in the library does.
        send_return=bool(fields.get("send_return", True)),
        # Editing a built-in keeps the flag, so the interface can still say
        # where it came from.
        builtin=bool(existing.builtin) if existing else False,
    )

    if existing:
        library[library.index(existing)] = updated
    else:
        library.append(updated)

    # Preserve the offered-ids record. Writing without it would forget that a
    # deleted built-in had ever been shown, and the next load would put it
    # back — so saving any snippet at all would undo an unrelated deletion.
    _write_all(library, _known_ids() | {s.id for s in all_builtins()})
    return updated


def delete_snippet(snippet_id: str) -> bool:
    """Remove a snippet. Built-ins can be removed too — see load_snippets."""
    library = load_snippets()
    remaining = [s for s in library if s.id != snippet_id]
    if len(remaining) == len(library):
        return False
    # Keep the offered-ids record intact, or deleting a built-in would remove
    # it from "known" too and the next load would put it straight back.
    _write_all(remaining, _known_ids() | {s.id for s in all_builtins()})
    return True


def reset_to_defaults() -> list[Snippet]:
    """Put the shipped library back, discarding every edit."""
    builtins = all_builtins()
    _write_all(builtins)
    return builtins
