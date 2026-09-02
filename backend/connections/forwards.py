"""
forwards.py — Port forwarding over an existing SSH session (#405).

The gap a PuTTY user hits first: reaching a device's web page, or a host on
the management network, through the SSH session that is already open.
Three kinds, the same three OpenSSH offers:

**local** (``-L``): a listener on this machine; each connection to it becomes
a ``direct-tcpip`` channel to *host:port* as seen from the device.

**dynamic** (``-D``): a SOCKS5 listener on this machine; a browser pointed at
it reaches anything the device can reach, each connection its own channel.

**remote** (``-R``): the device listens; connections arriving there come back
as ``forwarded-tcpip`` channels and are joined to *host:port* here.

Rules that keep this safe:

- **Listeners bind loopback only.** Nothing off this machine can use a
  forward, for the same reason nothing off this machine can use the API.
- **Bounded.** ``ssh.max_forwards`` per session, and a forward that fails to
  bind says so rather than silently taking another port.
- **They die with the session.** Every listener and every pump thread is
  owned by the session's manager and closed when the session ends.
- **Nothing is silent.** A forward is listed on the tab, and every
  connection through it is logged at debug level.
"""

import logging
import select
import socket
import struct
import threading
import uuid
from dataclasses import dataclass, field

import paramiko

from backend.advanced import get as advanced
from backend.connections.base import ConnectionError_

logger = logging.getLogger(__name__)

KINDS = ("local", "dynamic", "remote")


@dataclass
class Forward:
    id: str
    kind: str
    listen_port: int
    host: str = ""
    port: int = 0
    listener: socket.socket | None = field(default=None, repr=False)
    threads: list = field(default_factory=list, repr=False)
    connections: int = 0
    error: str = ""

    def as_dict(self) -> dict:
        return {
            "id": self.id, "kind": self.kind, "listen_port": self.listen_port,
            "host": self.host, "port": self.port, "connections": self.connections,
            "error": self.error,
            "describe": describe(self.kind, self.listen_port, self.host, self.port),
        }


def describe(kind: str, listen_port: int, host: str, port: int) -> str:
    if kind == "local":
        return f"localhost:{listen_port} → {host}:{port} via the device"
    if kind == "dynamic":
        return f"SOCKS5 proxy on localhost:{listen_port} via the device"
    return f"device:{listen_port} → {host or 'localhost'}:{port} here"


def _pump(a, b, on_close=None) -> None:
    """Copy bytes both ways until either side closes."""
    try:
        while True:
            readable, _, _ = select.select([a, b], [], [], 30)
            if not readable:
                # Idle: keep waiting unless a side is gone.
                if _closed(a) or _closed(b):
                    break
                continue
            for src in readable:
                dst = b if src is a else a
                try:
                    data = src.recv(65536)
                except OSError:
                    data = b""
                if not data:
                    return
                dst.sendall(data)
    except OSError:
        pass
    finally:
        for s in (a, b):
            try:
                s.close()
            except Exception:
                pass
        if on_close:
            on_close()


def _closed(sock) -> bool:
    try:
        return bool(getattr(sock, "closed", False)) or sock.fileno() < 0
    except Exception:
        return True


class ForwardManager:
    """The forwards of one session. Created on demand, closed with it."""

    def __init__(self, transport_getter) -> None:
        self._transport_getter = transport_getter
        self._forwards: dict[str, Forward] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------ queries
    def list(self) -> list[dict]:
        with self._lock:
            return [f.as_dict() for f in self._forwards.values()]

    def _transport(self) -> paramiko.Transport:
        transport = self._transport_getter()
        if transport is None or not transport.is_active():
            raise ConnectionError_("The session is not connected.")
        return transport

    # ------------------------------------------------------------ add
    def add(self, kind: str, listen_port: int, host: str = "", port: int = 0) -> dict:
        kind = (kind or "").strip().lower()
        if kind not in KINDS:
            raise ConnectionError_(f"'{kind}' is not a kind of forward. Use local, dynamic or remote.")
        listen_port = int(listen_port)
        if not 1 <= listen_port <= 65535:
            raise ConnectionError_("The listening port must be between 1 and 65535.")
        if kind != "dynamic":
            port = int(port or 0)
            if not host or not 1 <= port <= 65535:
                raise ConnectionError_("A host and a port between 1 and 65535 are needed.")
        limit = int(advanced("ssh.max_forwards"))
        with self._lock:
            if len(self._forwards) >= limit:
                raise ConnectionError_(
                    f"This session already has {limit} forwards, the limit in Stockton.")
            if any(f.kind != "remote" and f.listen_port == listen_port for f in self._forwards.values()):
                raise ConnectionError_(f"Port {listen_port} is already forwarded on this session.")

        transport = self._transport()
        forward = Forward(id=uuid.uuid4().hex[:8], kind=kind, listen_port=listen_port,
                          host=host, port=port)
        if kind == "remote":
            self._start_remote(transport, forward)
        else:
            self._start_listener(transport, forward)
        with self._lock:
            self._forwards[forward.id] = forward
        logger.info("Forward started: %s", describe(kind, listen_port, host, port))
        return forward.as_dict()

    def _start_listener(self, transport: paramiko.Transport, forward: Forward) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind(("127.0.0.1", forward.listen_port))
            listener.listen(16)
        except OSError as exc:
            listener.close()
            raise ConnectionError_(
                f"Could not listen on localhost:{forward.listen_port}: {exc.strerror or exc}. "
                f"Something else may be using that port.") from exc
        listener.settimeout(1.0)
        forward.listener = listener

        def accept_loop():
            while forward.listener is not None:
                try:
                    client, peer = listener.accept()
                except socket.timeout:
                    continue
                except OSError:
                    return
                worker = threading.Thread(
                    target=self._serve_client, args=(transport, forward, client, peer),
                    daemon=True, name=f"forward-{forward.id}")
                worker.start()

        thread = threading.Thread(target=accept_loop, daemon=True, name=f"listen-{forward.id}")
        thread.start()
        forward.threads.append(thread)

    def _serve_client(self, transport, forward: Forward, client: socket.socket, peer) -> None:
        try:
            if forward.kind == "dynamic":
                target = _socks5_handshake(client)
                if target is None:
                    client.close()
                    return
                host, port = target
            else:
                host, port = forward.host, forward.port
            try:
                channel = transport.open_channel("direct-tcpip", (host, port), peer,
                                                 timeout=advanced("ssh.connect_timeout"))
            except Exception as exc:
                logger.debug("Forward %s: device refused %s:%s (%s)", forward.id, host, port, exc)
                if forward.kind == "dynamic":
                    _socks5_reply(client, 5)      # connection refused
                client.close()
                return
            if forward.kind == "dynamic":
                _socks5_reply(client, 0)
            forward.connections += 1
            logger.debug("Forward %s: %s → %s:%s", forward.id, peer, host, port)
            _pump(client, channel)
        except Exception as exc:
            logger.debug("Forward %s: connection ended: %s", forward.id, exc)
            try:
                client.close()
            except Exception:
                pass

    def _start_remote(self, transport: paramiko.Transport, forward: Forward) -> None:
        def handler(channel, origin, server):
            def run():
                try:
                    local = socket.create_connection((forward.host or "127.0.0.1", forward.port),
                                                     timeout=advanced("ssh.connect_timeout"))
                except OSError as exc:
                    logger.debug("Remote forward %s: %s:%s unreachable here (%s)",
                                 forward.id, forward.host, forward.port, exc)
                    channel.close()
                    return
                forward.connections += 1
                _pump(channel, local)
            threading.Thread(target=run, daemon=True, name=f"remote-{forward.id}").start()

        try:
            transport.request_port_forward("", forward.listen_port, handler)
        except paramiko.SSHException as exc:
            raise ConnectionError_(
                f"The device refused to listen on port {forward.listen_port}: {exc}") from exc

    # ------------------------------------------------------------ remove
    def remove(self, forward_id: str) -> bool:
        with self._lock:
            forward = self._forwards.pop(forward_id, None)
        if forward is None:
            return False
        self._stop(forward)
        logger.info("Forward stopped: %s", describe(forward.kind, forward.listen_port,
                                                    forward.host, forward.port))
        return True

    def _stop(self, forward: Forward) -> None:
        if forward.kind == "remote":
            try:
                self._transport().cancel_port_forward("", forward.listen_port)
            except Exception:
                pass
        listener, forward.listener = forward.listener, None
        if listener is not None:
            try:
                listener.close()
            except Exception:
                pass

    def close_all(self) -> None:
        with self._lock:
            forwards = list(self._forwards.values())
            self._forwards.clear()
        for forward in forwards:
            self._stop(forward)


# ---------------------------------------------------------------- SOCKS5
def _socks5_handshake(client: socket.socket) -> tuple[str, int] | None:
    """
    The client half of RFC 1928, no authentication, CONNECT only.

    Returns the requested (host, port), or None after answering the client
    with a refusal. Kept to the subset a browser or curl actually sends.
    """
    client.settimeout(10)
    try:
        head = _recv_exact(client, 2)
        if not head or head[0] != 5:
            return None
        methods = _recv_exact(client, head[1])
        if methods is None or 0 not in methods:
            client.sendall(b"\x05\xff")
            return None
        client.sendall(b"\x05\x00")
        request = _recv_exact(client, 4)
        if request is None or request[0] != 5:
            return None
        if request[1] != 1:                          # CONNECT only
            _socks5_reply(client, 7)
            return None
        atyp = request[3]
        if atyp == 1:
            raw = _recv_exact(client, 4)
            host = socket.inet_ntoa(raw) if raw else None
        elif atyp == 3:
            length = _recv_exact(client, 1)
            raw = _recv_exact(client, length[0]) if length else None
            host = raw.decode("utf-8", errors="replace") if raw else None
        elif atyp == 4:
            raw = _recv_exact(client, 16)
            host = socket.inet_ntop(socket.AF_INET6, raw) if raw else None
        else:
            _socks5_reply(client, 8)
            return None
        port_raw = _recv_exact(client, 2)
        if host is None or port_raw is None:
            return None
        port = struct.unpack("!H", port_raw)[0]
        client.settimeout(None)
        return host, port
    except (OSError, struct.error):
        return None


def _socks5_reply(client: socket.socket, code: int) -> None:
    try:
        client.sendall(b"\x05" + bytes([code]) + b"\x00\x01" + b"\x00\x00\x00\x00" + b"\x00\x00")
    except OSError:
        pass


def _recv_exact(sock: socket.socket, n: int) -> bytes | None:
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            return None
        data += chunk
    return data


# ---------------------------------------------------------------- session glue
def manager_for(session: dict) -> ForwardManager:
    """The session's ForwardManager, created on first use."""
    manager = session.get("forwards")
    if manager is None:
        handler = session.get("handler")

        def transport():
            client = getattr(handler, "_client", None)
            return client.get_transport() if client is not None else None

        if not hasattr(handler, "_client"):
            raise ConnectionError_("Port forwarding needs an SSH session.")
        manager = session["forwards"] = ForwardManager(transport)
    return manager


def close_for(session: dict) -> None:
    """Stop every forward a session holds. Safe on a session that has none."""
    manager = session.get("forwards")
    if manager is not None:
        try:
            manager.close_all()
        except Exception as exc:
            logger.debug("Closing forwards: %s", exc)
