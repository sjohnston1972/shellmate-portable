"""
neighbours.py — What the device you reached can see (#542).

On a site you did not build, the first switch you get into knows about the
other twelve. The subnet scanner cannot help: it sweeps addresses on a
network, and everything interesting is usually across a routed boundary.
CDP and LLDP are already on those switches, already answering, and already
hold the management address, the platform string and both ends of every
link.

Four rules, and each of them is a way this could quietly mislead:

**The second channel or nothing.** Collecting neighbours means running two
commands, and the live-session fallback types them into the terminal the
user is working in — two more lines they did not run, in the transcript
that is their record of what they did. Serial and telnet cannot multiplex,
so on those this refuses and says why rather than borrowing the session.

**A platform read out of a CDP string is a guess.** "cisco WS-C3850-48P" is
strong evidence and "Juniper Networks ex4300" is too, but neither is the
device telling ShellMate what it is — it is a neighbour telling ShellMate
what it thinks its neighbour is. It is stored below the threshold that
lets anything be sent to a device, exactly as the scanner's guesses are.

**A neighbour with no address is still a neighbour.** LLDP frequently
carries a system name and no management address at all. Dropping those
would silently hide half a site; they are kept, flagged, and cannot be
saved as a connection because there is nothing to dial.

**Both protocols, deduplicated.** A device running CDP and LLDP appears
twice, under two spellings of the same name, and a list that shows both is
a list somebody has to reconcile by hand. Matched on management address
where there is one, on the bare name where there is not.
"""

import logging
import re

logger = logging.getLogger(__name__)

#: Which aliases hold a neighbour command, in the order they are tried.
#: `nei` is deliberately not among them: it is the short summary table on
#: most platforms, and the detail forms carry the management address.
PROTOCOLS = ("cdp", "lldp")

#: How long to wait for each. A full CDP table on a distribution switch is
#: not fast, and cutting it off produces a shorter list rather than an
#: error — which is the failure mode nobody notices.
READ_TIMEOUT = 20.0

#: Enough of a vendor string to recognise, mapped to a ShellMate platform.
#: Deliberately coarse: this decides which platform a *guess* names, and a
#: guess is never acted on.
_VENDOR_HINTS = (
    ("nexus", "nxos"),
    ("n9k", "nxos"),
    ("n3k", "nxos"),
    ("juniper", "junos"),
    ("ex4", "junos"),
    ("srx", "junos"),
    ("arista", "arista"),
    ("aruba", "aoscx"),
    ("procurve", "aoscx"),
    ("mikrotik", "routeros"),
    ("fortinet", "fortios"),
    ("palo alto", "panos"),
    ("asa", "asa"),
    ("cisco", "ios"),
)

#: An address, so a management field holding a name is not mistaken for one.
_IPV4 = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")

#: The template field names, which differ between the CDP and LLDP
#: templates and between ntc-templates releases. Tried in order; the first
#: that holds anything wins.
_FIELDS = {
    "name": ("neighbor_name", "neighbor", "destination_host", "chassis_id"),
    "address": ("mgmt_address", "management_ip", "neighbor_ip", "mgmt_ip"),
    "platform": ("platform", "neighbor_description", "system_description"),
    "local_port": ("local_interface", "local_port", "local_port_id"),
    "remote_port": ("neighbor_interface", "neighbor_port_id", "remote_port"),
}


class NeighbourError(RuntimeError):
    """Neighbours cannot be collected on this session, with the reason."""


def _field(row: dict, which: str) -> str:
    for name in _FIELDS[which]:
        value = row.get(name)
        if isinstance(value, list):
            value = value[0] if value else ""
        text = str(value or "").strip()
        if text:
            return text
    return ""


def guess_platform(description: str) -> str:
    """
    A ShellMate platform id from a neighbour's own description of itself.

    A guess, and treated as one everywhere it goes. The device saying this
    is not the device being described — it is repeating what a neighbour
    advertised — so it never reaches the threshold that lets ShellMate
    send anything anywhere.
    """
    text = (description or "").lower()
    for hint, platform in _VENDOR_HINTS:
        if hint in text:
            return platform
    return ""


def _short(name: str) -> str:
    """`sw1.example.net` as `sw1`; an address left exactly as it is."""
    text = (name or "").strip()
    if not text or _IPV4.match(text):
        return text
    return text.split(".")[0]


def normalise(rows: list[dict], protocol: str, local_host: str) -> list[dict]:
    """Template rows as neighbours, dropping the ones that say nothing."""
    out = []
    for row in rows or []:
        name = _field(row, "name")
        address = _field(row, "address")
        if not _IPV4.match(address):
            # An LLDP template will happily put a chassis MAC or a system
            # name in a management field. Anything that is not an address
            # is not one, and saving it as a hostname to dial would create
            # a profile that cannot connect and does not say why.
            address = ""
        if not name and not address:
            continue
        out.append({
            "name": _short(name) or address,
            "full_name": name,
            "address": address,
            "platform_description": _field(row, "platform"),
            "platform": guess_platform(_field(row, "platform")),
            "local_port": _field(row, "local_port"),
            "remote_port": _field(row, "remote_port"),
            "protocol": protocol,
            "seen_from": local_host,
            # Said on the row rather than inferred from an empty address,
            # because the interface has to explain why a device it found
            # cannot be saved.
            "reachable": bool(address),
        })
    return out


def merge(found: list[dict]) -> list[dict]:
    """
    One entry per neighbour, however many protocols announced it.

    Keyed on the management address where there is one and on the bare
    name otherwise. A device running both CDP and LLDP appears twice, under
    two spellings, and a list showing both is one somebody reconciles by
    hand.
    """
    merged: dict[str, dict] = {}
    for entry in found:
        key = entry["address"] or f"name:{entry['name'].lower()}"
        existing = merged.get(key)
        if existing is None:
            entry = dict(entry)
            entry["protocols"] = [entry.pop("protocol")]
            merged[key] = entry
            continue
        protocol = entry["protocol"]
        if protocol not in existing["protocols"]:
            existing["protocols"].append(protocol)
        # Fill the gaps rather than overwrite: CDP usually carries the
        # platform and LLDP usually carries the better port names, and
        # whichever ran second should not blank what the first found.
        for field in ("address", "platform", "platform_description",
                      "local_port", "remote_port", "full_name"):
            if not existing.get(field) and entry.get(field):
                existing[field] = entry[field]
        existing["reachable"] = bool(existing["address"])
    return sorted(merged.values(), key=lambda e: (not e["reachable"], e["name"]))


def collect(session: dict) -> dict:
    """
    Ask a live session what it can see.

    Uses a second SSH channel and refuses without one. The live-session
    fallback that `capture_config` falls back to types into the terminal
    the user is working in, and two commands they did not run would land
    in the transcript that is their record of what they did.
    """
    from backend.configs import _read_until_idle
    from backend.connections.base import ConnectionError_
    from backend.connections.ssh_handler import SSHHandler
    from backend.platforms import get_profile
    from backend.session import parsed as parsed_module

    handler = session.get("handler")
    if not handler or not getattr(handler, "is_connected", False):
        raise NeighbourError("The session is no longer connected.")
    if not isinstance(handler, SSHHandler):
        raise NeighbourError(
            "Neighbours are collected over a second SSH channel, and "
            f"{session.get('connection_type', 'this')} connections cannot "
            "open one. Running the commands in your own session instead "
            "would put two lines you did not type into your transcript.")

    platform = session.get("platform") or session.get("last_seen_platform") or ""
    profile = get_profile(platform)
    commands = {name: profile.aliases.get(name, "")
                for name in PROTOCOLS if profile.aliases.get(name)}
    if not commands:
        raise NeighbourError(
            f"ShellMate does not know a neighbour command for "
            f"{platform or 'this device'}, so it will not guess at one.")

    channel = handler.open_secondary_channel()
    if channel is None:
        raise NeighbourError(
            "A second channel could not be opened. Some devices allow only "
            "one session at a time; nothing was run in yours.")

    local_host = session.get("hostname") or session.get("display_label") or ""
    found: list[dict] = []
    ran: list[str] = []
    quiet: list[dict] = []

    try:
        _read_until_idle(channel, timeout=3.0)
        if profile.paging_off:
            channel.send((profile.paging_off + "\n").encode())
            _read_until_idle(channel, timeout=5.0)

        for name, command in commands.items():
            try:
                channel.send((command + "\n").encode())
                raw = _read_until_idle(channel, timeout=READ_TIMEOUT)
            except Exception as exc:                      # pragma: no cover
                quiet.append({"protocol": name, "why": str(exc)[:120]})
                continue
            ran.append(command)

            rows = parsed_module.parse(platform, command, raw)
            if rows is None:
                # No template, or one that did not match this release's
                # output. Said rather than swallowed: "CDP found nothing"
                # and "ShellMate could not read the CDP output" are
                # different facts, and the second is not the device's
                # fault.
                quiet.append({
                    "protocol": name,
                    "why": "ShellMate has no parser for this device's "
                           f"output of '{command}'.",
                })
                continue
            found.extend(normalise(rows, name, local_host))
    finally:
        try:
            channel.close()
        except Exception:
            pass

    neighbours = merge(found)
    logger.info("Neighbours of %s: %d found via %s",
                local_host or "?", len(neighbours), ", ".join(ran) or "nothing")
    return {
        "host": local_host,
        "platform": platform,
        "commands": ran,
        "neighbours": neighbours,
        # Why a protocol produced nothing, when it did. An empty list with
        # no explanation reads as "this device has no neighbours", which
        # is a different and much stronger claim.
        "quiet": quiet,
        "via": "second channel",
    }
