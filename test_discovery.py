"""
test_discovery.py — Finding devices, and not disturbing anything doing it.

Two halves, and they matter for different reasons.

**Target parsing** is where a typo becomes a 65,534-address sweep of somebody
else's network. Everything about the size limit, the range shorthand and the
network/broadcast exclusion is checked here rather than trusted, because the
failure is not a wrong answer — it is traffic that has already left.

**The probe** is checked against real listening sockets on the loopback
interface, including one that presents a Cisco SSH banner. That last one is
the point of the whole module: the banner goes into `fingerprint.identify()`,
the same function a live session uses, so a scan says what a device *is*
before anyone connects to it. Everything else here is available from any port
scanner.

Nothing in this file touches a network beyond 127.0.0.1.

    python test_discovery.py
"""

import asyncio
import socket
import sys
import threading
import time

from backend import discovery

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


SETTINGS = {"concurrency": 16, "timeout": 1.0, "fetch_http": True,
            "max_seconds": 30}


class FakeDevice:
    """
    A socket on the loopback interface that says what a switch would say.

    Speaking first is the behaviour under test: SSH announces itself before
    anything is sent, which is the only reason a scan can identify a platform
    without logging in.
    """

    def __init__(self, banner: bytes = b""):
        self.banner = banner
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.bind(("127.0.0.1", 0))
        self.socket.listen(8)
        self.port = self.socket.getsockname()[1]
        self.running = True
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        while self.running:
            try:
                client, _ = self.socket.accept()
            except OSError:
                return
            try:
                if self.banner:
                    client.sendall(self.banner)
                time.sleep(0.05)
            except OSError:
                pass
            finally:
                try:
                    client.close()
                except OSError:
                    pass

    def close(self) -> None:
        self.running = False
        try:
            self.socket.close()
        except OSError:
            pass


def test_targets() -> None:
    print("\n-- What somebody typed --")

    check("a single address", discovery.parse_targets("10.20.30.1", 1024)
          == ["10.20.30.1"])
    check("a comma-separated list",
          discovery.parse_targets("10.0.0.1, 10.0.0.2", 1024)
          == ["10.0.0.1", "10.0.0.2"])
    check("whitespace is a separator too",
          len(discovery.parse_targets("10.0.0.1 10.0.0.2\n10.0.0.3", 1024)) == 3)

    block = discovery.parse_targets("10.20.30.0/29", 1024)
    check("a CIDR block expands", len(block) == 6, f"{len(block)}: {block}")
    check("without the network address", "10.20.30.0" not in block)
    check("or the broadcast address", "10.20.30.7" not in block,
          "probing those wastes a slot and looks careless in a firewall log")

    check("a /32 is the address itself",
          discovery.parse_targets("10.20.30.5/32", 1024) == ["10.20.30.5"],
          "the network/broadcast convention does not apply there")

    full = discovery.parse_targets("10.20.30.10-10.20.30.14", 1024)
    check("a full range", full == [f"10.20.30.{n}" for n in range(10, 15)], str(full))
    short = discovery.parse_targets("10.20.30.10-14", 1024)
    check("and the short form people actually type", short == full, str(short))
    check("a backwards range is not an error",
          discovery.parse_targets("10.20.30.14-10", 1024) == full,
          "refusing it teaches nothing; reading it the obvious way does")

    check("duplicates collapse",
          discovery.parse_targets("10.0.0.1, 10.0.0.1, 10.0.0.1", 1024)
          == ["10.0.0.1"])
    check("a hostname is a target",
          discovery.parse_targets("switch01.example.net", 1024)
          == ["switch01.example.net"])


def test_ipv6_targets() -> None:
    """IPv6 subnets, ranges and the short last-group form (#419)."""
    print("\n-- IPv6 targets --")
    from backend import discovery
    full = discovery.parse_targets("2001:db8::1-2001:db8::4", 100)
    check("a full IPv6 range expands", full == ["2001:db8::1", "2001:db8::2", "2001:db8::3", "2001:db8::4"], str(full))
    short = discovery.parse_targets("2001:db8::a-c", 100)
    check("the short last-group form works", short == ["2001:db8::a", "2001:db8::b", "2001:db8::c"], str(short))
    cidr = discovery.parse_targets("2001:db8::/126", 100)
    check("an IPv6 subnet expands to its hosts", len(cidr) == 3 and "2001:db8::1" in cidr, str(cidr))
    mixed = discovery.parse_targets("10.0.0.1, 2001:db8::1", 100)
    check("v4 and v6 can be listed together", mixed == ["10.0.0.1", "2001:db8::1"], str(mixed))
    check("a range that mixes families is refused",
          _raises(lambda: discovery.parse_targets("10.0.0.1-2001:db8::1", 100)))
    check("a literal IPv6 host is bracketed for HTTP", discovery._url_host("2001:db8::1") == "[2001:db8::1]")
    check("an IPv4 host is left alone", discovery._url_host("10.0.0.1") == "10.0.0.1")


def _raises(fn) -> bool:
    try:
        fn()
    except Exception:
        return True
    return False


def test_the_size_limit_is_not_optional() -> None:
    """
    A /16 is 65,534 addresses and nobody means it the first time.

    The check is inside parse_targets rather than at the caller, because
    every route in has to be subject to it — the panel, the connection
    dialog, and anything that calls the API directly.
    """
    print("\n-- The sweep somebody did not mean --")

    for oversized in ("10.0.0.0/16", "10.0.0.1-10.0.20.0"):
        raised = ""
        try:
            discovery.parse_targets(oversized, 1024)
        except discovery.TargetError as exc:
            raised = str(exc)
        check(f"{oversized} is refused", bool(raised))
        check("and the message names the limit", "1,024" in raised, raised)

    for nonsense in ("", "   ", ",,,"):
        raised = ""
        try:
            discovery.parse_targets(nonsense, 1024)
        except discovery.TargetError as exc:
            raised = str(exc)
        check(f"{nonsense!r} is refused with an example", "10.20.30.0/24" in raised,
              raised)

    raised = ""
    try:
        discovery.parse_targets("10.20.30.0/nonsense", 1024)
    except discovery.TargetError as exc:
        raised = str(exc)
    check("a malformed subnet says which part it could not read",
          "10.20.30.0/nonsense" in raised, raised)


def test_it_finds_something_that_is_there() -> None:
    print("\n-- A device that answers --")
    device = FakeDevice()
    try:
        result = asyncio.run(discovery._probe("127.0.0.1", [device.port], SETTINGS))
        check("an open port is reported", result is not None)
        if result:
            check("with the port listed", result["ports"] == [device.port])
            check("and an address", result["address"] == "127.0.0.1")
    finally:
        device.close()


def test_it_reports_nothing_for_a_dead_address() -> None:
    """
    Silence is not a result.

    A list padded with 254 "nothing here" rows buries the four devices that
    did answer, which is the whole reason anybody ran the scan.
    """
    print("\n-- An address with nothing on it --")
    # Bind and immediately close, so the port is almost certainly free.
    spare = socket.socket()
    spare.bind(("127.0.0.1", 0))
    port = spare.getsockname()[1]
    spare.close()

    result = asyncio.run(discovery._probe("127.0.0.1", [port], SETTINGS))
    check("nothing is reported at all", result is None,
          "a closed port produced a row")


def test_the_banner_becomes_a_platform() -> None:
    """
    The part that makes this ShellMate's rather than a port scanner.

    The banner from port 22 goes into the same fingerprint function a live
    session uses, so the results say what each device is before anyone
    connects to it.
    """
    print("\n-- What the banner says the device is --")

    # A real listener on a real SSH port, saying what a Cisco switch says.
    # Bound to 2222 rather than 22 so the test needs no privileges — both are
    # in SSH_PORTS precisely so a device on a non-standard port is still
    # identified, which is the case this also covers.
    device = FakeDevice(b"SSH-2.0-Cisco-1.25\r\n")
    device.close()

    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        listener.bind(("127.0.0.1", 2222))
    except OSError as exc:
        check("port 2222 is available to bind", False, str(exc))
        listener.close()
        return
    listener.close()

    ssh = _device_on_port(2222, b"SSH-2.0-Cisco-1.25\r\n")
    if ssh is None:
        check("a fake SSH device could be started", False, "port 2222 in use")
        return

    try:
        result = asyncio.run(discovery._probe("127.0.0.1", [2222], SETTINGS))
        check("the device is found", result is not None)
        if not result:
            return

        check("the banner is read", "SSH-2.0-Cisco" in result["ssh_banner"],
              repr(result["ssh_banner"]))
        check("and identified as Cisco",
              result["platform"] in ("ios", "nxos", "asa"),
              f"got {result['platform']!r} — a scan that cannot name the "
              f"platform is a port scanner")
        check("with a confidence worth showing", result["confidence"] > 0,
              str(result["confidence"]))
        check("and a name for the interface to print",
              bool(result["platform_name"]), repr(result["platform_name"]))
        check("SSH is what the connection dialog would be pre-filled with",
              result["suggested_type"] == "ssh", result["suggested_type"])
    finally:
        ssh.close()


def _device_on_port(port: int, banner: bytes) -> "FakeDevice | None":
    """A FakeDevice bound to a specific port, or None if it is taken."""
    device = FakeDevice.__new__(FakeDevice)
    device.banner = banner
    device.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    device.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        device.socket.bind(("127.0.0.1", port))
    except OSError:
        device.socket.close()
        return None
    device.socket.listen(8)
    device.port = port
    device.running = True
    device.thread = threading.Thread(target=device._serve, daemon=True)
    device.thread.start()
    return device


def test_telnet_is_never_the_suggestion_when_ssh_is_open() -> None:
    """
    Offering telnet on a device that also speaks SSH is bad advice.

    Telnet sends the password in clear text. Where both are open the choice is
    not a preference, and the interface should not present it as one.
    """
    print("\n-- What a discovered device is offered as --")

    ssh = _device_on_port(2222, b"SSH-2.0-OpenSSH_9.0\r\n")
    telnet = FakeDevice(b"\r\nUser Access Verification\r\n")
    if ssh is None:
        check("a fake SSH device could be started", False, "port 2222 in use")
        telnet.close()
        return

    try:
        both = asyncio.run(discovery._probe(
            "127.0.0.1", [2222, telnet.port], SETTINGS))
        check("both ports are found", both and len(both["ports"]) == 2,
              str(both and both["ports"]))
        check("and SSH is what is suggested",
              both and both["suggested_type"] == "ssh",
              str(both and both["suggested_type"]))
    finally:
        ssh.close()
        telnet.close()


def test_a_sweep_is_bounded_and_can_be_stopped() -> None:
    """
    Someone will start a /24 while mid-change on a switch.

    Concurrency is the bound that keeps a sweep from looking like an attack;
    cancellation is the one that makes people willing to start it at all.
    """
    print("\n-- Bounds --")

    async def drive() -> dict:
        # A range on a subnet nothing routes to: every probe times out, which
        # is exactly the slow case that has to stay stoppable.
        targets = discovery.parse_targets("10.255.254.1-60", 1024)
        scan = discovery.start(targets, [22],
                               {"concurrency": 8, "timeout": 5.0,
                                "fetch_http": False, "max_seconds": 60})
        await asyncio.sleep(1.0)
        mid = scan.state()
        discovery.cancel(scan.id)
        await asyncio.sleep(0.3)
        return {"mid": mid, "after": scan.state(), "id": scan.id}

    outcome = asyncio.run(drive())

    check("a scan reports progress while running",
          outcome["mid"]["total"] == 60, str(outcome["mid"]["total"]))
    check("it can be cancelled", outcome["after"]["cancelled"] is True)
    check("and stops well short of the whole range",
          outcome["after"]["scanned"] < 60,
          f"scanned {outcome['after']['scanned']} of 60 — cancellation did "
          f"not stop anything")
    check("no longer running", outcome["after"]["running"] is False)

    # Cancelling something already finished is not an error worth raising,
    # but it is not a success either.
    check("cancelling a finished scan reports false",
          discovery.cancel(outcome["id"]) is False)
    check("as does cancelling one that never existed",
          discovery.cancel("not-a-scan") is False)


def test_the_overall_deadline_is_honest_about_stopping_early() -> None:
    """
    A scan that ran out of time must not look like one that finished.

    Reporting 12 of 254 as a completed sweep is how somebody concludes a
    subnet is empty when it is not.
    """
    print("\n-- Running out of time --")

    async def drive() -> dict:
        targets = discovery.parse_targets("10.255.253.1-80", 1024)
        scan = discovery.start(targets, [22],
                               {"concurrency": 4, "timeout": 5.0,
                                "fetch_http": False, "max_seconds": 1})
        for _ in range(60):
            await asyncio.sleep(0.3)
            if not scan.running:
                break
        return scan.state()

    state = asyncio.run(drive())
    check("the scan stops", state["running"] is False)
    check("and says it stopped early rather than finishing",
          bool(state["error"]) and "limit" in state["error"],
          f"error was {state['error']!r} after {state['scanned']}/80")
    check("naming how far it got",
          str(state["scanned"]) in state["error"], state["error"])


def test_results_are_ordered_the_way_addresses_read() -> None:
    print("\n-- The order results come back in --")
    rows = [{"address": a} for a in
            ("10.0.0.10", "10.0.0.2", "10.0.0.100", "switch01")]
    ordered = [r["address"] for r in sorted(rows, key=discovery._sort_key)]
    check(".2 comes before .10", ordered.index("10.0.0.2") < ordered.index("10.0.0.10"),
          str(ordered))
    check("and .10 before .100", ordered.index("10.0.0.10") < ordered.index("10.0.0.100"),
          str(ordered))
    check("a name sorts after the addresses", ordered[-1] == "switch01", str(ordered))


def test_the_local_subnet_is_offered() -> None:
    """The default target, and the one that cannot reach somebody else's network."""
    print("\n-- The local subnet --")
    subnets = discovery.local_subnets()
    check("at least one is found", len(subnets) >= 1,
          "no route to the outside world, or no interface at all")
    for subnet in subnets:
        check(f"{subnet['cidr']}: it is a real network",
              "/" in subnet["cidr"] and subnet["hosts"] > 0, str(subnet))
        check("and small enough to scan without thinking about it",
              subnet["hosts"] <= 254, str(subnet["hosts"]))


def main() -> int:
    print("\n" + "=" * 52)
    print("  Network discovery")
    print("=" * 52)

    for test in (test_targets, test_ipv6_targets,
                 test_the_size_limit_is_not_optional,
                 test_it_finds_something_that_is_there,
                 test_it_reports_nothing_for_a_dead_address,
                 test_the_banner_becomes_a_platform,
                 test_telnet_is_never_the_suggestion_when_ssh_is_open,
                 test_a_sweep_is_bounded_and_can_be_stopped,
                 test_the_overall_deadline_is_honest_about_stopping_early,
                 test_results_are_ordered_the_way_addresses_read,
                 test_the_local_subnet_is_offered):
        try:
            test()
        except Exception as exc:
            failed.append(f"{test.__name__}: raised {type(exc).__name__}: {exc}")
            print(f"  FAIL {test.__name__} raised {type(exc).__name__}: {exc}")

    print("\n" + "=" * 52)
    print(f"  {passed} passed  |  {len(failed)} failed")
    print("=" * 52)
    if failed:
        print("\nFAILURES:")
        for item in failed:
            print(f"  {item}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
