"""
test_neighbours.py — What the device you reached can see (#542).

On a site you did not build, the first switch you get into knows about the
other twelve. The subnet scanner cannot help — everything interesting is
usually across a routed boundary — but CDP and LLDP are already on those
switches, already answering.

Four things are worth holding, and each is a way this could quietly
mislead:

- **The second channel or nothing.** Running these commands in the user's
  own session would put two lines they did not type into the transcript
  that is their record of what they did.
- **A platform read out of a CDP string is a guess**, because the device
  saying it is not the device being described. It stays below the
  threshold that lets anything be sent anywhere.
- **A neighbour with no management address is still a neighbour.** LLDP
  often carries a name and nothing else, and dropping those silently hides
  half a site.
- **Both protocols, deduplicated.** A device running CDP and LLDP appears
  twice under two spellings of its name.

And one that is about honesty rather than correctness: a protocol that
produced nothing says why. An empty list on its own reads as "this device
has no neighbours", which is a much stronger claim than "ShellMate could
not read the output".

Run: python test_neighbours.py
"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backend import paths  # noqa: E402

_TEMP = Path(tempfile.mkdtemp(prefix="shellmate-nei-"))
paths._data_dir_cache = _TEMP
assert paths.data_dir() == _TEMP, "test isolation failed — refusing to run"

from backend import neighbours  # noqa: E402
from backend.connections.base import ConnectionParams  # noqa: E402
from backend.session import parsed as parsed_module  # noqa: E402
from backend.store import store  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                        # pragma: no cover
    pass

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


def refuses(fn, *args, **kwargs) -> str:
    try:
        fn(*args, **kwargs)
    except neighbours.NeighbourError as exc:
        return str(exc)
    return ""


#: Real `show cdp neighbors detail` output, blocks and all.
#:
#: Trimmed the first time this was written, and the template then matched
#: only the second entry — a shortened sample tests the shortening rather
#: than the parser, and would have hidden a real change in the templates.
CDP_DETAIL = """-------------------------
Device ID: sw-access-1.example.net
Entry address(es): 
  IP address: 10.20.0.11
Platform: cisco WS-C3850-48P,  Capabilities: Switch IGMP 
Interface: GigabitEthernet1/0/24,  Port ID (outgoing port): GigabitEthernet0/1
Holdtime : 143 sec

Version :
Cisco IOS Software, IOS-XE Software, Catalyst L3 Switch

advertisement version: 2

-------------------------
Device ID: rtr-edge-1.example.net
Entry address(es): 
  IP address: 10.20.0.1
Platform: cisco ISR4331/K9,  Capabilities: Router 
Interface: GigabitEthernet1/0/1,  Port ID (outgoing port): GigabitEthernet0/0/1
Holdtime : 121 sec

Version :
Cisco IOS XE Software

advertisement version: 2
"""


def reading_what_a_device_reports() -> None:
    print("\n-- Reading what the device reports --")

    if not parsed_module.available():                     # pragma: no cover
        check("the template library is installed", False,
              "ntc-templates is a dependency of this feature")
        return

    rows = parsed_module.parse("ios", "show cdp neighbors detail", CDP_DETAIL)
    check("the CDP output parses at all", bool(rows), str(rows))

    found = neighbours.normalise(rows, "cdp", "core-1")
    check("both neighbours come through", len(found) == 2, str(len(found)))

    access = next(n for n in found if n["name"] == "sw-access-1")
    check("the domain is trimmed off the name",
          access["name"] == "sw-access-1", access["name"])
    check("but kept, because it is what the device actually said",
          access["full_name"] == "sw-access-1.example.net", access["full_name"])
    check("the management address comes across",
          access["address"] == "10.20.0.11", access["address"])
    check("and both ends of the link",
          access["local_port"] == "GigabitEthernet1/0/24"
          and access["remote_port"] == "GigabitEthernet0/1", str(access))
    check("with the device it was seen from",
          access["seen_from"] == "core-1", access["seen_from"])


def a_platform_is_a_guess() -> None:
    print("\n-- A platform read out of a CDP string is a guess --")

    check("a Catalyst is guessed as IOS",
          neighbours.guess_platform("cisco WS-C3850-48P") == "ios")
    check("a Nexus is not guessed as IOS",
          neighbours.guess_platform("cisco Nexus9000 C93180YC") == "nxos",
          "IOS and NX-OS do not share a paging command, and this is exactly "
          "the mistake that sends the wrong command to a device")
    check("a Juniper is guessed as Junos",
          neighbours.guess_platform("Juniper Networks ex4300-48t") == "junos")
    check("an Arista is guessed as EOS",
          neighbours.guess_platform("Arista Networks DCS-7050SX") == "arista")
    check("something unrecognised is guessed as nothing",
          neighbours.guess_platform("SuperSwitch 9000") == "",
          "an unrecognised vendor string is not evidence of anything, and "
          "a wrong platform is worse than none")
    check("and an empty description likewise",
          neighbours.guess_platform("") == "")

    # The description travels beside the guess, so somebody can see what
    # the guess was made from.
    rows = parsed_module.parse("ios", "show cdp neighbors detail", CDP_DETAIL)
    found = neighbours.normalise(rows, "cdp", "core-1")
    check("the string the guess came from is kept",
          "3850" in found[0]["platform_description"]
          or "ISR" in found[0]["platform_description"],
          str(found[0]["platform_description"]))


def a_neighbour_with_no_address() -> None:
    print("\n-- A neighbour with no address is still a neighbour --")

    rows = [{"neighbor_name": "ap-lobby-2", "local_interface": "Gi1/0/8",
             "neighbor_interface": "eth0", "mgmt_address": ""}]
    found = neighbours.normalise(rows, "lldp", "core-1")
    check("it is kept rather than dropped", len(found) == 1,
          "dropping them silently hides half a site")
    check("and flagged as not reachable",
          found[0]["reachable"] is False, str(found[0]))
    check("with the name it did give", found[0]["name"] == "ap-lobby-2")

    # An LLDP template will happily put a chassis MAC in a management
    # field. Saving that as a hostname makes a profile that cannot connect
    # and does not say why.
    rows = [{"neighbor_name": "sw-x", "mgmt_address": "00:11:22:33:44:55",
             "local_interface": "Gi1/0/9"}]
    found = neighbours.normalise(rows, "lldp", "core-1")
    check("something that is not an address is not treated as one",
          found[0]["address"] == "" and found[0]["reachable"] is False,
          str(found[0]))

    check("a row with neither a name nor an address is dropped",
          neighbours.normalise([{"local_interface": "Gi1/0/1"}], "lldp", "c") == [],
          "an edge to nothing is not an edge")


def one_entry_per_neighbour() -> None:
    print("\n-- Both protocols, one list --")

    cdp = neighbours.normalise(
        [{"neighbor_name": "sw-access-1.example.net", "mgmt_address": "10.20.0.11",
          "platform": "cisco WS-C3850-48P", "local_interface": "Gi1/0/24"}],
        "cdp", "core-1")
    lldp = neighbours.normalise(
        [{"neighbor_name": "sw-access-1", "mgmt_address": "10.20.0.11",
          "local_interface": "Gi1/0/24", "neighbor_interface": "Gi0/1"}],
        "lldp", "core-1")

    merged = neighbours.merge(cdp + lldp)
    check("a device announced by both appears once",
          len(merged) == 1, str(merged))
    check("and says which protocols saw it",
          sorted(merged[0]["protocols"]) == ["cdp", "lldp"],
          str(merged[0]["protocols"]))
    check("the platform CDP knew survives",
          merged[0]["platform"] == "ios", str(merged[0]))
    check("and the port LLDP knew is filled in",
          merged[0]["remote_port"] == "Gi0/1", str(merged[0]),)

    # Two different devices with no address must not collapse into one.
    nameless = neighbours.normalise(
        [{"neighbor_name": "ap-1", "local_interface": "Gi1/0/8"},
         {"neighbor_name": "ap-2", "local_interface": "Gi1/0/9"}], "lldp", "c")
    check("two unaddressed neighbours stay two",
          len(neighbours.merge(nameless)) == 2, str(neighbours.merge(nameless)))

    # Reachable first: the ones that can be saved are the ones somebody is
    # about to act on.
    mixed = neighbours.merge(cdp + nameless)
    check("the ones that can be dialled come first",
          mixed[0]["reachable"] is True, str([m["name"] for m in mixed]))


def the_edges_are_kept() -> None:
    print("\n-- The edges are kept --")

    found = neighbours.merge(neighbours.normalise(
        parsed_module.parse("ios", "show cdp neighbors detail", CDP_DETAIL),
        "cdp", "core-1"))
    check("both edges are stored",
          store.record_neighbours("core-1", found) == 2)

    ports = store.neighbours_of("core-1")
    check("and read back by port",
          {p["local_port"] for p in ports}
          == {"GigabitEthernet1/0/24", "GigabitEthernet1/0/1"}, str(ports))
    check("the other end is named",
          any(p["remote_host"] == "sw-access-1" for p in ports), str(ports))

    # A switch swapped for another answers on the same port with a
    # different name, and keeping both would make the table unreadable.
    store.record_neighbours("core-1", [{
        "name": "sw-access-9", "address": "10.20.0.19",
        "local_port": "GigabitEthernet1/0/24", "remote_port": "Gi0/1",
        "protocols": ["cdp"]}])
    ports = store.neighbours_of("core-1")
    check("a port has one answer, not a history of answers",
          len([p for p in ports if p["local_port"] == "GigabitEthernet1/0/24"]) == 1,
          str(ports))
    check("and it is the newer one",
          next(p["remote_host"] for p in ports
               if p["local_port"] == "GigabitEthernet1/0/24") == "sw-access-9")

    check("the other direction is answerable too",
          [r["hostname"] for r in store.neighbours_naming("sw-access-9")] == ["core-1"],
          "a device you cannot reach is often still visible in the fact "
          "that its neighbours can see it")

    check("a neighbour with no local port is not an edge",
          store.record_neighbours("core-1", [{"name": "floating"}]) == 0,
          "an edge with no near end would collide every such neighbour "
          "onto one row")


def it_will_not_borrow_the_session() -> None:
    print("\n-- The second channel or nothing --")

    from backend.connections.telnet_handler import TelnetHandler

    telnet = TelnetHandler(params=ConnectionParams(
        connection_type="telnet", hostname="10.0.0.1"))
    telnet._socket = object()                             # look connected

    why = refuses(neighbours.collect,
                  {"handler": telnet, "connection_type": "telnet",
                   "hostname": "sw-1", "platform": "ios"})
    check("telnet is refused rather than borrowed", bool(why), "it went ahead")
    check("and the reason says what it would have cost",
          "transcript" in why or "did not type" in why, why)

    why = refuses(neighbours.collect, {"handler": None, "hostname": "sw-1"})
    check("a dead session is refused", bool(why), "it went ahead")


if __name__ == "__main__":
    reading_what_a_device_reports()
    a_platform_is_a_guess()
    a_neighbour_with_no_address()
    one_entry_per_neighbour()
    the_edges_are_kept()
    it_will_not_borrow_the_session()

    print("\n" + "=" * 52)
    print(f"  {passed} passed  |  {len(failed)} failed")
    print("=" * 52)
    for line in failed:
        print("  -", line)
    sys.exit(1 if failed else 0)
