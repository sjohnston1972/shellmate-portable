"""
discovery.py — Finding out what is on the wire.

Getting a device into ShellMate meant knowing its address already. On a site
you did not build — a customer, an acquisition, a rack somebody documented
badly — the first job is finding out what is there, and that happened in a
different tool before ShellMate was opened at all.

What makes this ShellMate's rather than a port scanner: the SSH banner from
port 22 goes straight into ``fingerprint.identify()``, the same function that
turns a banner into "Cisco IOS / IOS-XE, confidence 0.9" when a session
connects. So the results say *what each device is* before anyone connects to
it, and a profile saved from a scan already knows its platform. Everything
else here is available from a dozen other tools; that is not.

**No ICMP.** A ping sweep needs raw sockets, which need administrator rights
on Windows, and ShellMate deliberately requires none. It would also find less:
ICMP is filtered on most management networks, which are exactly the networks
worth scanning. A TCP connect sweep needs no privileges and is a better probe
here. The panel says so, rather than leaving people wondering where ping went.

**Passive only.** It reads a banner and fetches ``/``. It does not try
credentials. This finds devices; it does not test them.

Four constraints shape the implementation rather than decorating it, because
someone will start a /24 sweep while mid-change on a switch:

**Nothing blocking on the event loop.** Every probe is an ``asyncio``
connection. One blocking socket connect in the async path stalls every
WebSocket in the application, including the terminal the user is typing into.

**Bounded concurrency, defaulting low.** A few dozen in flight, not 254. An
unbounded sweep exhausts a firewall's session table and is indistinguishable
from an attack.

**Cancellable, and bounded overall.** A scan that cannot be stopped is one
people will not start.

**Honest progress.** Scanned, total, found, and which address is being probed
now — so a slow scan is visibly working rather than apparently hung.
"""

import asyncio
import ipaddress
import logging
import re
import socket
import ssl
import time
import uuid

from backend import fingerprint

logger = logging.getLogger(__name__)

#: Live and recently finished scans, by id. In memory only.
#:
#: A scan is a snapshot of one moment, and persisting it would mean retention
#: rules, a place in the evidence pack, and a second thing to explain. The
#: durable output of a scan is the connection profiles saved from it, which do
#: persist. Closing ShellMate discards the rest.
_scans: dict[str, "Scan"] = {}

#: How many finished scans to keep so results survive closing the panel.
_KEEP_FINISHED = 5


#: Ports where the far end speaks first, so connecting reads a banner. That
#: banner from 22 is the whole point of this module — it is what
#: fingerprint.identify() turns into a platform.
SSH_PORTS = (22, 2222)
TELNET_PORTS = (23, 992)
SPEAKS_FIRST = SSH_PORTS + TELNET_PORTS

#: Ports tried over TLS rather than plain HTTP. Everything else that is open
#: and does not speak first is tried as plain HTTP.
TLS_PORTS = (443, 8443, 4443, 9443)


class TargetError(ValueError):
    """A target specification that could not be understood, said plainly."""


def parse_targets(text: str, limit: int) -> list[str]:
    """
    Turn what somebody typed into a list of addresses.

    Accepts CIDR (``10.20.30.0/24``), a range (``10.20.30.1-10.20.30.60`` or
    ``10.20.30.1-60``), a comma-separated list of any of those, and single
    addresses or names.

    Network and broadcast addresses are dropped from a CIDR block: they are
    not hosts, and probing them wastes a slot and looks careless in a firewall
    log. A /31 and /32 are left alone, where that convention does not apply.

    Raises:
        TargetError: Nothing usable, or more addresses than ``limit``. The
            size check is here rather than at the caller because every route
            in has to be subject to it — /16 is 65,534 addresses and nobody
            means it the first time.
    """
    found: list[str] = []
    seen: set[str] = set()

    for piece in re.split(r"[,\s]+", (text or "").strip()):
        if not piece:
            continue
        for address in _expand(piece):
            if address not in seen:
                seen.add(address)
                found.append(address)
            if len(found) > limit:
                raise TargetError(
                    f"That is more than {limit:,} addresses. Narrow the range, "
                    f"or raise the limit in Stockton if you mean it.")

    if not found:
        raise TargetError(
            "Nothing to scan. Give a subnet (10.20.30.0/24), a range "
            "(10.20.30.1-60), a list, or a single address.")
    return found


def _expand(piece: str) -> list[str]:
    """Expand one comma-separated element into addresses."""
    if "/" in piece:
        try:
            network = ipaddress.ip_network(piece, strict=False)
        except ValueError as exc:
            raise TargetError(f"'{piece}' is not a subnet ShellMate understands.") from exc
        if network.num_addresses <= 2:
            return [str(a) for a in network]
        return [str(a) for a in network.hosts()]

    if "-" in piece and not piece.startswith("-"):
        start_text, _, end_text = piece.partition("-")
        try:
            start = ipaddress.ip_address(start_text.strip())
            end_text = end_text.strip()
            # "10.20.30.1-60" — the short form people actually type. For
            # IPv6 the short form is the last group: "2001:db8::1-1f" (#419).
            if start.version == 6:
                if ":" not in end_text:
                    prefix = start_text.strip().rsplit(":", 1)[0]
                    end = ipaddress.ip_address(f"{prefix}:{end_text}")
                else:
                    end = ipaddress.ip_address(end_text)
            elif "." not in end_text:
                prefix = start_text.strip().rsplit(".", 1)[0]
                end = ipaddress.ip_address(f"{prefix}.{end_text}")
            else:
                end = ipaddress.ip_address(end_text)
        except ValueError as exc:
            raise TargetError(f"'{piece}' is not a range ShellMate understands.") from exc

        if start.version != end.version:
            raise TargetError(f"'{piece}' mixes IPv4 and IPv6.")
        if int(end) < int(start):
            start, end = end, start
        return [str(ipaddress.ip_address(n)) for n in range(int(start), int(end) + 1)]

    return [piece]


def local_subnets() -> list[dict]:
    """
    The subnets this machine is on, as scan targets.

    Offered as the default because it is the answer almost every time, and
    because a default of "the local subnet" is the one that cannot accidentally
    reach somebody else's network.

    Read by opening a UDP socket towards a public address and asking which
    local address the routing table chose — no packet is sent. Enumerating
    interfaces properly needs a platform-specific library, and this needs no
    privileges and no dependency.
    """
    subnets: list[dict] = []

    for probe_target in ("8.8.8.8", "1.1.1.1"):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                probe.settimeout(0.5)
                probe.connect((probe_target, 53))
                address = probe.getsockname()[0]
        except OSError:
            continue

        # /24 is assumed rather than read. The real mask needs an interface
        # enumeration this deliberately avoids, and on a management network a
        # /24 is right far more often than not — while being small enough that
        # guessing wrong scans too little rather than too much.
        network = ipaddress.ip_network(f"{address}/24", strict=False)
        entry = {"address": address, "cidr": str(network),
                 "hosts": network.num_addresses - 2}
        if entry not in subnets:
            subnets.append(entry)
        break

    return subnets


class Scan:
    """One sweep: its targets, its progress, and its results."""

    def __init__(self, targets: list[str], ports: list[int], settings: dict):
        self.id = str(uuid.uuid4())
        self.targets = targets
        self.ports = ports
        self.settings = settings

        self.started = time.time()
        self.finished: float | None = None
        self.scanned = 0
        self.current = ""
        self.results: list[dict] = []
        self.cancelled = False
        self.error = ""

    @property
    def running(self) -> bool:
        return self.finished is None and not self.cancelled

    def state(self) -> dict:
        """Everything the panel needs to draw itself."""
        return {
            "id":        self.id,
            "running":   self.running,
            "cancelled": self.cancelled,
            "error":     self.error,
            "total":     len(self.targets),
            "scanned":   self.scanned,
            "found":     len(self.results),
            "current":   self.current,
            "elapsed":   round((self.finished or time.time()) - self.started, 1),
            "ports":     self.ports,
            "results":   sorted(self.results, key=_sort_key),
        }


def _sort_key(result: dict):
    """Numeric order for addresses, so .2 comes before .10."""
    try:
        return (0, int(ipaddress.ip_address(result["address"])))
    except ValueError:
        return (1, result["address"])


def start(targets: list[str], ports: list[int], settings: dict) -> Scan:
    """Register a scan and start it. Returns immediately."""
    scan = Scan(targets, ports, settings)
    _scans[scan.id] = scan
    _forget_old()

    asyncio.create_task(_run(scan))
    logger.info("Discovery scan %s started: %d address(es), ports %s",
                scan.id[:8], len(targets), ports)
    return scan


def get(scan_id: str) -> Scan | None:
    return _scans.get(scan_id)


def cancel(scan_id: str) -> bool:
    """Stop a running scan. In-flight probes finish; nothing new is started."""
    scan = _scans.get(scan_id)
    if scan is None or not scan.running:
        return False
    scan.cancelled = True
    scan.finished = time.time()
    logger.info("Discovery scan %s cancelled after %d/%d",
                scan_id[:8], scan.scanned, len(scan.targets))
    return True


def forget(scan_id: str) -> bool:
    """
    Drop a scan entirely, cancelling it first if it is still going.

    Results are an inventory of somebody's network — addresses, banners, page
    titles, certificate names — and until now they stayed in memory until the
    application closed, with no way to say "I am finished with this". On a
    customer site that is a small disclosure with no undo.
    """
    scan = _scans.get(scan_id)
    if scan is None:
        return False
    if scan.running:
        cancel(scan_id)
    _scans.pop(scan_id, None)
    logger.info("Discarded scan %s", scan_id[:8])
    return True


def _forget_old() -> None:
    """Keep the last few finished scans and drop the rest."""
    done = sorted((s for s in _scans.values() if not s.running),
                  key=lambda s: s.finished or 0)
    for scan in done[:-_KEEP_FINISHED] if len(done) > _KEEP_FINISHED else []:
        _scans.pop(scan.id, None)


async def _run(scan: Scan) -> None:
    """Probe every target, bounded by concurrency and by an overall deadline."""
    limit = asyncio.Semaphore(max(1, int(scan.settings["concurrency"])))
    deadline = scan.started + float(scan.settings["max_seconds"])

    async def one(address: str) -> None:
        async with limit:
            if scan.cancelled or time.time() > deadline:
                return
            scan.current = address
            try:
                result = await _probe(address, scan.ports, scan.settings)
            except Exception as exc:
                # A single unreachable or malformed target must not end the
                # sweep — on a range typed by hand, some of them will be.
                logger.debug("Probe of %s failed: %s", address, exc)
                result = None
            scan.scanned += 1
            if result:
                scan.results.append(result)

    try:
        await asyncio.gather(*(one(a) for a in scan.targets))
    except Exception as exc:
        scan.error = str(exc)
        logger.warning("Discovery scan %s failed: %s", scan.id[:8], exc)

    if not scan.cancelled:
        scan.finished = time.time()
        if time.time() > deadline and scan.scanned < len(scan.targets):
            scan.error = (f"Stopped at the {int(scan.settings['max_seconds'])}s "
                          f"limit with {scan.scanned} of {len(scan.targets)} "
                          f"scanned. Raise it in Stockton, or scan less.")

    scan.current = ""
    logger.info("Discovery scan %s finished: %d found in %d scanned",
                scan.id[:8], len(scan.results), scan.scanned)


async def _probe(address: str, ports: list[int], settings: dict) -> dict | None:
    """
    Probe one address. Returns None when nothing answered.

    An address with no open port is not reported at all. A list padded with
    254 "nothing here" rows buries the four devices that did answer.
    """
    timeout = float(settings["timeout"])
    open_ports: list[int] = []
    banner = ""
    http: dict = {}
    certificate = ""

    for port in ports:
        speaks_first = port in SPEAKS_FIRST
        opened, first_bytes = await _connect(address, port, timeout,
                                             read=speaks_first)
        if not opened:
            continue
        open_ports.append(port)

        if port in SSH_PORTS and first_bytes:
            banner = first_bytes
        elif not speaks_first and settings["fetch_http"]:
            # Anything that is not SSH or telnet is tried as a web interface.
            # Keyed on the port rather than a fixed 80/443 pair because a
            # custom port list is the case this is for — somebody probing 8443
            # wants the page title as much as somebody probing 443.
            secure = port in TLS_PORTS
            http = await _http(address, port, timeout, secure) or http
            if secure:
                certificate = await _certificate(address, port, timeout)

    if not open_ports:
        return None

    # The part that makes this ShellMate's. The same function that identifies
    # a device when a session connects, given the banner from a port scan.
    identity = fingerprint.identify(banner=banner)

    return {
        "address":     address,
        "hostname":    await _reverse_dns(address, timeout),
        "ports":       open_ports,
        "ssh_banner":  banner.strip(),
        "platform":    identity.platform,
        "platform_name": identity.name,
        "confidence":  round(identity.confidence, 2),
        "version":     identity.version,
        "model":       identity.model,
        "http":        http,
        "certificate": certificate,
        # What the connection dialog should be pre-filled with. SSH wins where
        # both are open: telnet sends credentials in clear text, and offering
        # it by default on a device that also speaks SSH would be poor advice.
        "suggested_type": (
            "ssh" if any(p in SSH_PORTS for p in open_ports)
            else "telnet" if any(p in TELNET_PORTS for p in open_ports)
            else ""),
    }


async def _connect(address: str, port: int, timeout: float,
                   read: bool = False) -> tuple[bool, str]:
    """
    Open a TCP connection, optionally reading whatever the far end says first.

    SSH and telnet both speak first, which is where the banner comes from. The
    read has its own short timeout: a port that accepts and stays silent must
    not hold a concurrency slot for the whole per-probe budget.
    """
    writer = None
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(address, port), timeout=timeout)
    except (OSError, asyncio.TimeoutError, ValueError):
        return False, ""

    text = ""
    if read:
        try:
            data = await asyncio.wait_for(reader.read(512), timeout=min(timeout, 2.0))
            text = data.decode("utf-8", errors="replace")
        except (OSError, asyncio.TimeoutError):
            pass

    try:
        writer.close()
        await asyncio.wait_for(writer.wait_closed(), timeout=1.0)
    except (OSError, asyncio.TimeoutError):
        pass
    return True, text


def _url_host(address: str) -> str:
    """A literal IPv6 address needs brackets in a URL and in http.client (#419)."""
    return f"[{address}]" if ":" in address and not address.startswith("[") else address


async def _http(address: str, port: int, timeout: float, secure: bool) -> dict:
    """
    Fetch ``/`` and keep what identifies the device.

    The status, the redirect, the Server header and the page title. Between
    them those name most web-managed equipment, and a title of "Cisco
    Integrated Management Controller" is worth considerably more than
    "port 443 open".
    """
    def _fetch() -> dict:
        import http.client

        try:
            if secure:
                context = ssl.create_default_context()
                # Management interfaces almost universally present a
                # self-signed certificate. Refusing to look would mean finding
                # nothing on exactly the devices this is for; nothing is sent,
                # so there is no secret to protect.
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
                connection = http.client.HTTPSConnection(
                    _url_host(address), port, timeout=timeout, context=context)
            else:
                connection = http.client.HTTPConnection(_url_host(address), port, timeout=timeout)

            connection.request("GET", "/", headers={"User-Agent": "ShellMate-discovery"})
            response = connection.getresponse()
            body = response.read(8192).decode("utf-8", errors="replace")
            found = {
                "status":   response.status,
                "server":   response.getheader("Server", "") or "",
                "location": response.getheader("Location", "") or "",
                "title":    _title(body),
                "scheme":   "https" if secure else "http",
            }
            connection.close()
            return found
        except Exception:
            return {}

    return await asyncio.to_thread(_fetch)


_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


def _title(body: str) -> str:
    match = _TITLE_RE.search(body)
    return re.sub(r"\s+", " ", match.group(1)).strip()[:120] if match else ""


async def _certificate(address: str, port: int, timeout: float) -> str:
    """
    The certificate's subject common name, which often carries the real name.

    A management interface's certificate is usually self-signed and generated
    from the device's own hostname, which makes it one of the more reliable
    names available without logging in.
    """
    def _read() -> str:
        try:
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            with socket.create_connection((address, port), timeout=timeout) as raw:
                with context.wrap_socket(raw, server_hostname=address) as tls:
                    # binary_form is needed because an unverified certificate
                    # is not returned in dict form at all.
                    der = tls.getpeercert(binary_form=True)
            if not der:
                return ""
            names = re.findall(rb"[\x20-\x7e]{4,}", der)
            for candidate in names:
                text = candidate.decode("ascii", errors="ignore")
                if "." in text and " " not in text and len(text) < 80:
                    return text
            return ""
        except Exception:
            return ""

    return await asyncio.to_thread(_read)


async def _reverse_dns(address: str, timeout: float) -> str:
    """A name for the address, or "". Never blocks the loop."""
    def _lookup() -> str:
        try:
            return socket.gethostbyaddr(address)[0]
        except (OSError, socket.herror):
            return ""

    # Bounded by giving up on the thread, not by socket.setdefaulttimeout
    # (#325): that mutated a process-wide global from worker threads and
    # never restored it, leaving every future default-timeout socket in the
    # application with the probe's timeout — and it never bounded this call
    # anyway, because the resolver does not go through socket timeouts.
    try:
        return await asyncio.wait_for(asyncio.to_thread(_lookup), timeout)
    except asyncio.TimeoutError:
        return ""
