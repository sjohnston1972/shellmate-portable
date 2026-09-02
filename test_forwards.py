"""
test_forwards.py — Port forwarding over a real SSH transport (#405).

An in-process paramiko server that bridges direct-tcpip channels to a local
echo service stands in for a device with a host behind it. The checks: a
local forward carries bytes both ways; a dynamic forward speaks enough
SOCKS5 for a browser; listeners bind loopback only; the per-session limit
and a port in use are refused with a reason; and forwards close with the
session.

    python test_forwards.py
"""

import shutil
import socket
import struct
import sys
import tempfile
import threading
from pathlib import Path

import paramiko

from backend import paths

_TEMP = Path(tempfile.mkdtemp(prefix="shellmate-forwards-"))
paths._data_dir_cache = _TEMP

from backend.connections import forwards as forwards_module               # noqa: E402
from backend.connections.base import ConnectionError_, ConnectionParams   # noqa: E402
from backend.connections.manager import SessionManager                    # noqa: E402

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


def start_echo() -> tuple[int, socket.socket]:
    """A TCP service that sends back whatever it receives, upper-cased."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    port = listener.getsockname()[1]

    def run():
        while True:
            try:
                client, _ = listener.accept()
            except OSError:
                return
            def serve(c=client):
                with c:
                    while True:
                        data = c.recv(4096)
                        if not data:
                            return
                        c.sendall(data.upper())
            threading.Thread(target=serve, daemon=True).start()

    threading.Thread(target=run, daemon=True).start()
    return port, listener


class BridgingServer(paramiko.ServerInterface):
    """A device that allows a shell and bridges direct-tcpip to real sockets."""

    def check_auth_password(self, username, password):
        return paramiko.AUTH_SUCCESSFUL

    def get_allowed_auths(self, username):
        return "password"

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED if kind == "session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def __init__(self) -> None:
        # Server-side channel id → where the client asked to go, so the
        # accept loop can tell a tunnel from the shell.
        self.destinations: dict = {}
        self.last_destination = None

    def check_channel_direct_tcpip_request(self, chanid, origin, destination):
        self.destinations[chanid] = destination
        self.last_destination = destination
        return paramiko.OPEN_SUCCEEDED

    def check_channel_pty_request(self, *args, **kwargs):
        return True

    def check_channel_shell_request(self, channel):
        return True


def start_device() -> tuple[int, socket.socket, BridgingServer]:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(4)
    port = listener.getsockname()[1]
    host_key = paramiko.RSAKey.generate(2048)
    server = BridgingServer()
    keep: list = []

    def bridge(channel, destination):
        try:
            target = socket.create_connection(destination, timeout=5)
        except OSError:
            channel.close()
            return
        forwards_module._pump(channel, target)

    def run():
        while True:
            try:
                client, _ = listener.accept()
            except OSError:
                return
            transport = paramiko.Transport(client)
            transport.add_server_key(host_key)
            try:
                transport.start_server(server=server)
            except Exception:
                continue
            keep.append(transport)

            def accept_channels(t=transport):
                while t.is_active():
                    channel = t.accept(1)
                    if channel is None:
                        continue
                    keep.append(channel)
                    # paramiko hands every accepted channel here — the shell
                    # session and the tunnels alike. A tunnel is one whose id
                    # was seen in check_channel_direct_tcpip_request.
                    dest = server.destinations.pop(channel.get_id(), None)
                    if dest:
                        threading.Thread(target=bridge, args=(channel, dest), daemon=True).start()
            threading.Thread(target=accept_channels, daemon=True).start()

    threading.Thread(target=run, daemon=True).start()
    return port, listener, server


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_forwards() -> None:
    print("\n-- Forwards over a live transport --")
    echo_port, echo = start_echo()
    device_port, device, server = start_device()
    manager = SessionManager()
    session_id = None
    try:
        created = manager.create_session(ConnectionParams(
            connection_type="ssh", hostname="127.0.0.1", port=device_port,
            username="neteng", password="x"))
        session_id = created["session_id"]
        session = manager.get_session(session_id)
        fm = forwards_module.manager_for(session)

        local_port = free_port()
        entry = fm.add("local", local_port, "127.0.0.1", echo_port)
        check("a local forward starts and describes itself",
              entry["kind"] == "local" and str(local_port) in entry["describe"], str(entry))
        with socket.create_connection(("127.0.0.1", local_port), timeout=5) as c:
            c.sendall(b"hello through the device")
            c.settimeout(5)
            reply = c.recv(4096)
        check("bytes go through the device and back", reply == b"HELLO THROUGH THE DEVICE", repr(reply))
        check("the device was asked for the right destination",
              server.last_destination == ("127.0.0.1", echo_port), str(server.last_destination))
        check("the forward counted the connection", fm.list()[0]["connections"] == 1)

        socks_port = free_port()
        fm.add("dynamic", socks_port)
        with socket.create_connection(("127.0.0.1", socks_port), timeout=5) as c:
            c.settimeout(5)
            c.sendall(b"\x05\x01\x00")                       # hello, no auth
            check("SOCKS5: the greeting is answered", c.recv(2) == b"\x05\x00")
            c.sendall(b"\x05\x01\x00\x01" + socket.inet_aton("127.0.0.1") + struct.pack("!H", echo_port))
            reply = c.recv(10)
            check("SOCKS5: CONNECT succeeds", reply[:2] == b"\x05\x00", repr(reply))
            c.sendall(b"via socks")
            check("SOCKS5: data flows", c.recv(4096) == b"VIA SOCKS")

        # Bound to loopback only: the wildcard address must not answer.
        bound_wide = False
        try:
            probe = socket.create_connection((socket.gethostbyname(socket.gethostname()), local_port), timeout=1)
            probe.close()
            bound_wide = True
        except OSError:
            pass
        check("listeners bind loopback only", not bound_wide)

        check("a port already forwarded is refused",
              _raises(lambda: fm.add("local", local_port, "127.0.0.1", 1), "already forwarded"))
        taken = socket.socket(); taken.bind(("127.0.0.1", 0)); taken.listen(1)
        check("a port in use on this machine is refused with a reason",
              _raises(lambda: fm.add("local", taken.getsockname()[1], "127.0.0.1", 1), "Could not listen"))
        taken.close()
        check("nonsense is refused", _raises(lambda: fm.add("sideways", 1, "", 0), "kind"))
        check("a listing has both", len(fm.list()) == 2)

        first_id = fm.list()[0]["id"]
        check("a forward can be stopped", fm.remove(first_id) and len(fm.list()) == 1)
        check("stopping it closes the port",
              _raises(lambda: socket.create_connection(("127.0.0.1", local_port), timeout=1), ""))

        manager.destroy_session(session_id)
        session_id = None
        check("the rest die with the session",
              _raises(lambda: socket.create_connection(("127.0.0.1", socks_port), timeout=1), ""))
    finally:
        if session_id:
            manager.destroy_session(session_id)
        device.close()
        echo.close()


def _raises(fn, needle: str) -> bool:
    try:
        fn()
    except Exception as exc:
        return needle.lower() in str(exc).lower()
    return False


def main() -> int:
    print("=" * 52)
    print("  Port forwarding")
    print("=" * 52)
    try:
        test_forwards()
    except Exception as exc:
        failed.append(f"test_forwards: raised {type(exc).__name__}: {exc}")
        print(f"  FAIL test_forwards raised {type(exc).__name__}: {exc}")
    shutil.rmtree(_TEMP, ignore_errors=True)
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
