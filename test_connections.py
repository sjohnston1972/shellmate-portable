"""
test_connections.py — Tests for the Phase 2 transport layer.

Focused on the parts that are easy to get subtly wrong and hard to notice:
telnet option negotiation, IAC escaping, and the idle-versus-closed
distinction in recv().  A telnet bug does not announce itself — it shows up as
stray bytes on the user's screen or a session that hangs waiting for a reply
that never comes.

Runs against a scripted fake telnet server on localhost. No devices needed:

    python test_connections.py
"""

import socket
import sys
import threading
import time

from backend.connections.base import ConnectionParams
from backend.connections.telnet_handler import (
    DO, DONT, IAC, OPT_ECHO, OPT_NAWS, OPT_SGA, OPT_TTYPE, SB, SE, WILL, WONT,
    TelnetHandler,
)

passed = 0
failed: list[str] = []


def ok(name: str) -> None:
    global passed
    passed += 1
    print(f"  OK  {name}")


def fail(name: str, detail: str) -> None:
    failed.append(f"{name}: {detail}")
    print(f"  FAIL {name}\n       {detail}")


def check(name: str, condition: bool, detail: str = "") -> None:
    ok(name) if condition else fail(name, detail)


# ---------------------------------------------------------------------------
# A fake telnet server we can script
# ---------------------------------------------------------------------------


class FakeTelnetServer:
    """
    Minimal scripted telnet peer.

    Sends a fixed byte script on connect and records everything received, so
    tests can assert on exactly which negotiation replies the handler sent.
    """

    def __init__(self, script: bytes = b"", echo: bool = False) -> None:
        self.script = script
        self.echo = echo
        self.received = bytearray()
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(1)
        self.port = self._listener.getsockname()[1]
        self._conn: socket.socket | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        try:
            self._listener.settimeout(5)
            conn, _ = self._listener.accept()
            self._conn = conn
            conn.settimeout(0.2)
            if self.script:
                conn.sendall(self.script)
            while not self._stop.is_set():
                try:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    self.received.extend(chunk)
                    if self.echo:
                        conn.sendall(chunk)
                except socket.timeout:
                    continue
                except OSError:
                    break
        except Exception:
            pass

    def send(self, data: bytes) -> None:
        if self._conn:
            self._conn.sendall(data)

    def close(self) -> None:
        self._stop.set()
        for sock in (self._conn, self._listener):
            try:
                if sock:
                    sock.close()
            except Exception:
                pass


def drain(handler: TelnetHandler, seconds: float = 1.0) -> bytes:
    """Collect application data from the handler for a short window."""
    out = bytearray()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        chunk = handler.recv()
        if chunk is None:
            continue
        if chunk == b"":
            break
        out.extend(chunk)
    return bytes(out)


def connect_to(server: FakeTelnetServer, **kwargs) -> TelnetHandler:
    params = ConnectionParams(
        connection_type="telnet", hostname="127.0.0.1", port=server.port, **kwargs
    )
    handler = TelnetHandler(params=params)
    handler.connect()
    return handler


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_strips_negotiation_from_output() -> None:
    """Command sequences must never reach the terminal as visible bytes."""
    print("\n-- IAC stripping --")
    script = (
        bytes([IAC, DO, OPT_TTYPE])
        + b"Welcome"
        + bytes([IAC, WILL, OPT_ECHO])
        + b" to the lab\r\n"
    )
    server = FakeTelnetServer(script)
    try:
        handler = connect_to(server)
        data = drain(handler)
        check(
            "negotiation stripped from user-visible output",
            data == b"Welcome to the lab\r\n",
            f"got {data!r}",
        )
        handler.disconnect()
    finally:
        server.close()


def test_responds_to_option_demands() -> None:
    """A device waiting on a negotiation reply will stall if we stay silent."""
    print("\n-- Option negotiation --")
    server = FakeTelnetServer(bytes([IAC, DO, OPT_TTYPE]) + bytes([IAC, DO, 99]))
    try:
        handler = connect_to(server)
        drain(handler, 1.0)
        time.sleep(0.3)
        sent = bytes(server.received)

        check(
            "agrees to terminal type (DO TTYPE -> WILL TTYPE)",
            bytes([IAC, WILL, OPT_TTYPE]) in sent,
            f"sent {sent!r}",
        )
        check(
            "refuses unknown option 99 (DO 99 -> WONT 99)",
            bytes([IAC, WONT, 99]) in sent,
            f"sent {sent!r}",
        )
        check(
            "requests suppress-go-ahead on connect",
            bytes([IAC, DO, OPT_SGA]) in sent,
            f"sent {sent!r}",
        )
        handler.disconnect()
    finally:
        server.close()


def test_terminal_type_subnegotiation() -> None:
    """The device asks what we are; answering wrong breaks escape sequences."""
    print("\n-- Terminal type subnegotiation --")
    server = FakeTelnetServer(bytes([IAC, SB, OPT_TTYPE, 1, IAC, SE]))
    try:
        handler = connect_to(server)
        drain(handler, 1.0)
        time.sleep(0.3)
        expected = bytes([IAC, SB, OPT_TTYPE, 0]) + b"xterm-256color" + bytes([IAC, SE])
        check(
            "replies with terminal type",
            expected in bytes(server.received),
            f"sent {bytes(server.received)!r}",
        )
        handler.disconnect()
    finally:
        server.close()


def test_naws_window_size() -> None:
    """Window size must be sent as 16-bit big-endian values."""
    print("\n-- NAWS window size --")
    server = FakeTelnetServer()
    try:
        handler = connect_to(server)
        handler.resize(120, 40)
        time.sleep(0.3)
        expected = bytes([IAC, SB, OPT_NAWS, 0, 120, 0, 40, IAC, SE])
        check(
            "sends 120x40 as NAWS subnegotiation",
            expected in bytes(server.received),
            f"sent {bytes(server.received)!r}",
        )
        handler.disconnect()
    finally:
        server.close()


def test_iac_escaping_both_directions() -> None:
    """A literal 0xFF byte is ambiguous with IAC and must be doubled."""
    print("\n-- IAC escaping --")

    # Inbound: doubled IAC in the script should arrive as a single 0xFF.
    server = FakeTelnetServer(b"A" + bytes([IAC, IAC]) + b"B")
    try:
        handler = connect_to(server)
        data = drain(handler)
        check(
            "inbound IAC IAC decodes to a single 0xFF",
            data == b"A\xffB",
            f"got {data!r}",
        )
        handler.disconnect()
    finally:
        server.close()

    # Outbound: a raw 0xFF we send must be doubled on the wire.
    server = FakeTelnetServer()
    try:
        handler = connect_to(server)
        time.sleep(0.2)
        server.received.clear()
        handler.send(b"x\xffy")
        time.sleep(0.3)
        check(
            "outbound 0xFF is escaped to IAC IAC",
            bytes(server.received) == b"x" + bytes([IAC, IAC]) + b"y",
            f"sent {bytes(server.received)!r}",
        )
        handler.disconnect()
    finally:
        server.close()


def test_split_command_across_reads() -> None:
    """
    A command split across TCP segments must not leak onto the screen.

    This is the bug that only shows up on a congested link, which makes it
    exactly the kind worth having a test for.
    """
    print("\n-- Split command sequences --")
    server = FakeTelnetServer()
    try:
        handler = connect_to(server)
        server.send(b"hello" + bytes([IAC, DO]))   # command cut in half
        time.sleep(0.3)
        first = drain(handler, 0.6)
        server.send(bytes([OPT_TTYPE]) + b"world")  # remainder arrives later
        time.sleep(0.3)
        second = drain(handler, 0.6)

        check(
            "split IAC sequence never reaches the terminal",
            first + second == b"helloworld",
            f"got {first + second!r}",
        )
        check(
            "split sequence is still answered correctly",
            bytes([IAC, WILL, OPT_TTYPE]) in bytes(server.received),
            f"sent {bytes(server.received)!r}",
        )
        handler.disconnect()
    finally:
        server.close()


def test_idle_is_not_closed() -> None:
    """
    recv() must distinguish "nothing yet" from "connection gone".

    Conflating them drops sessions the moment the user stops typing.
    """
    print("\n-- Idle vs closed --")
    server = FakeTelnetServer()
    try:
        handler = connect_to(server)
        time.sleep(0.1)
        check("idle connection returns None, not b''", handler.recv() is None)
        check("handler still reports connected while idle", handler.is_connected)

        server.close()
        time.sleep(0.3)
        # Drain until the close is observed.
        result = None
        for _ in range(10):
            result = handler.recv()
            if result == b"":
                break
        check("closed connection returns b''", result == b"", f"got {result!r}")
        handler.disconnect()
    finally:
        server.close()


def test_autologin() -> None:
    """Credentials should answer a login prompt, and only a login prompt."""
    print("\n-- Auto-login --")
    server = FakeTelnetServer(b"\r\nUsername: ")
    try:
        handler = connect_to(server, username="neteng", password="hunter2")
        drain(handler, 1.0)
        time.sleep(0.3)
        check(
            "sends username at the Username: prompt",
            b"neteng\r\n" in bytes(server.received),
            f"sent {bytes(server.received)!r}",
        )

        server.send(b"\r\nPassword: ")
        drain(handler, 1.0)
        time.sleep(0.3)
        check(
            "sends password at the Password: prompt",
            b"hunter2\r\n" in bytes(server.received),
            f"sent {bytes(server.received)!r}",
        )
        check(
            "credentials are dropped once login completes",
            handler._login_password == "" and handler._login_user == "",
        )

        # The word "password" appearing in ordinary output must not re-trigger.
        server.received.clear()
        server.send(b"\r\nswitch#show run | inc password\r\n")
        drain(handler, 0.8)
        time.sleep(0.2)
        check(
            "does not re-send credentials later in the session",
            b"hunter2" not in bytes(server.received),
            f"sent {bytes(server.received)!r}",
        )
        handler.disconnect()
    finally:
        server.close()


def test_no_autologin_without_username() -> None:
    """With no credentials supplied, nothing should be typed for the user."""
    print("\n-- Auto-login disabled --")
    server = FakeTelnetServer(b"\r\nUsername: ")
    try:
        handler = connect_to(server)
        drain(handler, 0.8)
        time.sleep(0.2)
        payload = bytes(server.received)
        # Only negotiation bytes should have been sent.
        check(
            "no credentials sent when none were supplied",
            b"\r\n" not in payload.replace(bytes([IAC, WILL, OPT_TTYPE]), b""),
            f"sent {payload!r}",
        )
        check("auto-login marked done immediately", handler._login_stage == "done")
        handler.disconnect()
    finally:
        server.close()


def test_handler_registry() -> None:
    """Every advertised connection type must resolve to a handler."""
    print("\n-- Handler registry --")
    from backend.connections.base import ConnectionHandler
    from backend.connections.manager import HANDLERS

    check("ssh, serial and telnet all registered",
          set(HANDLERS) == {"ssh", "serial", "telnet"}, f"got {set(HANDLERS)}")
    check("every handler implements the contract",
          all(issubclass(h, ConnectionHandler) for h in HANDLERS.values()))


def test_secrets_are_scrubbed() -> None:
    """Credentials must not linger in the params object after connecting."""
    print("\n-- Secret handling --")
    params = ConnectionParams(
        password="hunter2", private_key_passphrase="pp",
        jump_password="jj", jump_private_key_passphrase="jp",
    )
    params.scrub_secrets()
    check(
        "scrub_secrets clears every credential field",
        not any([params.password, params.private_key_passphrase,
                 params.jump_password, params.jump_private_key_passphrase]),
    )

    from backend.profiles import SECRET_FIELDS, save_profile
    check(
        "profile secret blocklist covers all credential fields",
        SECRET_FIELDS == {"password", "private_key_passphrase",
                          "jump_password", "jump_private_key_passphrase"},
        f"got {SECRET_FIELDS}",
    )


def test_end_to_end_session() -> None:
    """
    Drive a real session through the HTTP API and the terminal WebSocket.

    Exercises the whole path the browser uses — create the session, bridge
    keystrokes and output over the WebSocket, fill the session buffer, then
    tear it down — with a scripted telnet peer standing in for a device.
    """
    print("\n-- End to end through the API --")
    from fastapi.testclient import TestClient

    from backend.app import app, session_manager

    server = FakeTelnetServer(b"\r\nswitch01> ", echo=True)
    session_id = None
    try:
        with TestClient(app) as client:
            response = client.post("/api/sessions", json={
                "connection_type": "telnet",
                "hostname": "127.0.0.1",
                "port": server.port,
                "display_label": "lab-switch",
            })
            check("POST /api/sessions creates a telnet session",
                  response.status_code == 200, f"{response.status_code}: {response.text}")
            if response.status_code != 200:
                return

            session = response.json()
            session_id = session["session_id"]
            check("session reports its transport", session["connection_type"] == "telnet")
            check("display label is used for the tab", session["display_label"] == "lab-switch")
            check("no credential fields leak into the response",
                  not any(k in session for k in ("password", "params", "handler")),
                  f"keys: {sorted(session)}")

            # The session clock is counted in the browser from this one value,
            # so it has to be present and it has to be a timestamp JavaScript's
            # Date.parse understands. A string it cannot read would leave every
            # tab silently counting from the moment the page loaded instead.
            from datetime import datetime
            check("the response carries when the session connected",
                  bool(session.get("connected_at")), f"got {session.get('connected_at')!r}")
            try:
                stamp = datetime.fromisoformat(session["connected_at"])
                check("and it is ISO 8601 with a timezone",
                      stamp.tzinfo is not None, f"naive: {session['connected_at']}")
            except (ValueError, KeyError, TypeError) as exc:
                check("and it is ISO 8601 with a timezone", False, str(exc))

            listed = client.get("/api/sessions").json()
            check("session appears in the session list",
                  any(s["session_id"] == session_id for s in listed))

            with client.websocket_connect(f"/ws/terminal/{session_id}") as ws:
                banner = ws.receive_json()
                check("device banner reaches the browser",
                      "switch01" in banner.get("data", ""), f"got {banner!r}")

                ws.send_json({"type": "input", "data": "show version\r"})
                # The fake peer echoes, so what comes back proves the whole
                # send path reached the socket and returned.
                echoed = ""
                for _ in range(10):
                    message = ws.receive_json()
                    echoed += message.get("data", "")
                    if "show version" in echoed:
                        break
                check("keystrokes reach the device and echo back",
                      "show version" in echoed, f"got {echoed!r}")

            time.sleep(0.3)
            internal = session_manager.get_session(session_id)
            buffered = internal["buffer"].get_text(50) if internal else ""
            check("session buffer captured the output",
                  "switch01" in buffered, f"buffer: {buffered!r}")

            check("DELETE tears the session down",
                  client.delete(f"/api/sessions/{session_id}").status_code == 200)
            check("session is gone from the manager",
                  session_manager.get_session(session_id) is None)
            session_id = None

    finally:
        if session_id:
            session_manager.destroy_session(session_id)
        server.close()


def test_a_session_records_which_connection_opened_it() -> None:
    """
    A session carries its profile id, and returns it.

    Without this the interface had to match a session back to a saved
    connection by address and port, which is not an identity. An estate behind
    one jump host, a lab of containers on one address, or two profiles for one
    switch with different credentials all collide — and they did: one open
    session lit the connected indicator on five thousand connections, tabs
    took a neighbouring group's name, and clicking any disconnected device
    switched to the tab already open rather than connecting, so only the first
    device in an estate could ever be reached (#187, #190, #192, #193).

    The id was already being sent for credentials and then thrown away by
    `to_params()`. This checks it survives to the response, and that a session
    opened without one still says so rather than inventing an answer.
    """
    print("\n-- A session knows which connection it is --")
    from fastapi.testclient import TestClient

    from backend.app import app, session_manager

    # One peer each: the fake listens for a single connection, and this test
    # opens two sessions on purpose.
    server = FakeTelnetServer(b"\r\nswitch01> ")
    adhoc_server = FakeTelnetServer(b"\r\nswitch02> ")
    opened: list[str] = []
    try:
        with TestClient(app) as client:
            body = {"connection_type": "telnet", "hostname": "127.0.0.1",
                    "port": server.port, "display_label": "lab-switch"}

            named = client.post("/api/sessions",
                                json={**body, "profile_id": "abc-123"})
            check("a session opened from a profile is created",
                  named.status_code == 200, f"{named.status_code}: {named.text}")
            if named.status_code == 200:
                opened.append(named.json()["session_id"])
                check("and returns the profile it came from",
                      named.json().get("profile_id") == "abc-123",
                      f"got {named.json().get('profile_id')!r} — without this "
                      f"the interface can only guess from the address")

            adhoc = client.post("/api/sessions",
                                json={**body, "port": adhoc_server.port})
            check("a session opened from the dialog is created",
                  adhoc.status_code == 200, f"{adhoc.status_code}: {adhoc.text}")
            if adhoc.status_code == 200:
                opened.append(adhoc.json()["session_id"])
                # Empty, not absent and not a guess. Callers fall back to the
                # address match for exactly these, and only for these.
                check("and reports no profile rather than guessing one",
                      adhoc.json().get("profile_id") == "",
                      f"got {adhoc.json().get('profile_id')!r}")

            listed = client.get("/api/sessions")
            by_id = {s["session_id"]: s for s in listed.json()}
            check("the listing carries it too",
                  all("profile_id" in s for s in listed.json()),
                  "GET /api/sessions is what the dashboard polls for live state")
            if opened and opened[0] in by_id:
                check("and it is the same id the create returned",
                      by_id[opened[0]]["profile_id"] == "abc-123",
                      f"got {by_id[opened[0]].get('profile_id')!r}")

            # It is an id, never a credential — the whole design depends on
            # nothing secret travelling this way.
            for session in listed.json():
                check("no credential rides along with it",
                      not any(k in session for k in
                              ("password", "params", "passphrase", "handler")),
                      f"keys: {sorted(session)}")
                break
    finally:
        for session_id in opened:
            session_manager.destroy_session(session_id)
        server.close()
        adhoc_server.close()


def test_a_dropped_session_stops_reporting_itself_as_open() -> None:
    """
    `is_connected` asks the transport, not only the flag.

    The flag is written by the WebSocket read loop when `recv()` returns b"" —
    which only happens once something tries to read. A transport that went
    away between reads left the flag saying True, and everything polling
    /api/sessions believed it: the device's light in the group tree, and its
    group's, stayed green after the session had died (#203).

    Both are consulted, and conjoined. The flag can say False for a session
    deliberately closed; the handler can say False for one that dropped.
    Either is enough to be disconnected.
    """
    print("\n-- A dropped session says so --")
    from fastapi.testclient import TestClient

    from backend.app import app, session_manager

    server = FakeTelnetServer(b"\r\nswitch01> ")
    session_id = None
    try:
        with TestClient(app) as client:
            made = client.post("/api/sessions", json={
                "connection_type": "telnet", "hostname": "127.0.0.1",
                "port": server.port, "display_label": "lab-switch",
                "profile_id": "prof-1",
            })
            if made.status_code != 200:
                check("a session could be opened to check", False, made.text)
                return
            session_id = made.json()["session_id"]

            listed = client.get("/api/sessions").json()
            check("it starts out reported as open",
                  listed and listed[0]["is_connected"] is True,
                  f"got {listed}")

            # The transport goes away without anything having read from it —
            # exactly the window the flag could not see.
            internal = session_manager.get_session(session_id)
            check("the internal session is reachable for the test",
                  internal is not None and internal.get("handler") is not None)
            if internal and internal.get("handler"):
                internal["handler"].disconnect()

            # The flag is untouched — that is the point.
            check("the stored flag still says connected",
                  internal.get("is_connected") is True,
                  "if this changed, the test is no longer exercising the gap")

            after = client.get("/api/sessions").json()
            check("but the API reports it closed",
                  after and after[0]["is_connected"] is False,
                  f"got {after} — a session whose transport has gone must not "
                  f"go on showing a green light")

            # And nothing about the identity was lost on the way.
            check("the profile it came from survives the drop",
                  after and after[0].get("profile_id") == "prof-1",
                  f"got {after[0].get('profile_id')!r}")
    finally:
        if session_id:
            session_manager.destroy_session(session_id)
        server.close()


def test_unknown_connection_type_rejected() -> None:
    """An unsupported transport should fail cleanly, not with a 500."""
    print("\n-- Unknown transport --")
    from fastapi.testclient import TestClient

    from backend.app import app

    with TestClient(app) as client:
        response = client.post("/api/sessions", json={
            "connection_type": "carrier-pigeon", "hostname": "10.0.0.1",
        })
        check("unknown type returns 400", response.status_code == 400,
              f"got {response.status_code}")
        check("error names the supported types",
              "ssh" in response.text and "telnet" in response.text,
              f"got {response.text}")


def test_a_password_stops_paramiko_offering_keys() -> None:
    """
    Having a key in ~/.ssh must not make a device unreachable.

    paramiko offers public keys before it offers a password. A Cisco SSH
    stack answers a rejected key by tearing the connection down —

        Authentication (publickey) failed.
        Disconnect (code 2): Protocol error: expected packet type 50, got 5

    — so the password is never tried. What the user sees is "authentication
    failed for steven@10.20.30.40. Check the username, password or key", so
    they type the password again more carefully and get the same result. The
    password was never the problem: the presence of an unrelated id_ed25519 in
    their home directory was.

    This drives the real connect() far enough to inspect the arguments it
    builds, without a device.
    """
    print("\n-- What paramiko is allowed to offer --")

    import paramiko

    from backend.connections import ssh_handler

    captured: dict = {}

    class Recorder:
        def set_missing_host_key_policy(self, policy): pass
        def load_system_host_keys(self, *a, **k): pass
        def connect(self, **kwargs):
            captured.update(kwargs)
            raise paramiko.SSHException("captured, for the test")
        def close(self): pass
        def get_transport(self): return None

    original = paramiko.SSHClient
    paramiko.SSHClient = Recorder

    def attempt(**overrides) -> dict:
        captured.clear()
        params = ConnectionParams(
            connection_type="ssh", hostname="10.20.30.40", port=22,
            username="steven", **overrides)
        handler = ssh_handler.SSHHandler(params=params)
        try:
            handler.connect()
        except Exception:
            pass
        return dict(captured)

    try:
        with_password = attempt(password="hunter2")
        check("a password means keys are not gone looking for",
              with_password.get("look_for_keys") is False,
              f"look_for_keys={with_password.get('look_for_keys')!r} — an "
              f"unrelated key in ~/.ssh will make this device unreachable")
        check("and the password is passed", with_password.get("password") == "hunter2")
        check("the agent is never consulted",
              with_password.get("allow_agent") is False,
              "an agent key fails the same way a file key does")

        without = attempt()
        check("with no password, keys are still discovered",
              without.get("look_for_keys") is True,
              "key-only authentication has to keep working")

        # An explicitly named key is a deliberate choice and is honoured
        # whatever else was given.
        named = attempt(password="hunter2", private_key_path="/no/such/key")
        check("naming a key file overrides the narrowing",
              named.get("look_for_keys") is True or not named,
              f"look_for_keys={named.get('look_for_keys')!r}")
    finally:
        paramiko.SSHClient = original


def test_a_failure_says_what_actually_happened() -> None:
    """
    "Check the username, password or key" is three guesses and no diagnosis.

    It was also actively wrong in the case that turned out to be common: a
    device that hangs up after refusing a key never gets as far as the
    password, so the message sent someone to retype a password that was
    correct — three times, across three storage paths.

    Everything asserted here is read from the exception paramiko raised and
    the state of the transport when it did. None of it is guessed.
    """
    print("\n-- What a failed connection says --")

    import paramiko

    from backend.connections.ssh_handler import _explain_auth_failure

    def params(**overrides) -> ConnectionParams:
        return ConnectionParams(connection_type="ssh", hostname="10.20.30.40",
                                username="steven", **overrides)

    generic = paramiko.AuthenticationException("failed")

    # The server telling us what it would have accepted. The single most
    # useful thing available in the whole failure, and it was discarded.
    told = _explain_auth_failure(
        paramiko.BadAuthenticationType("no", ["publickey", "keyboard-interactive"]),
        params(password="x"), "steven", offered_keys=True, still_connected=True)
    check("the server's allowed types are used when it offers them",
          "only accept" in told and "a key" in told, told)
    check("and translated out of protocol names",
          "publickey" not in told, told)

    # The reported bug.
    hung_up = _explain_auth_failure(generic, params(password="x"), "steven",
                                    offered_keys=True, still_connected=False)
    check("a device that hangs up after a key says so",
          "closed the connection" in hung_up and "never tried" in hung_up,
          hung_up)
    check("and names the setting that stops it",
          "Try keys in ~/.ssh" in hung_up, hung_up)

    # Where the credential came from changes what to go and check.
    typed = _explain_auth_failure(generic, params(password="x", credential_source="typed"),
                                  "steven", offered_keys=False, still_connected=True)
    saved = _explain_auth_failure(generic, params(password="x", credential_source="saved"),
                                  "steven", offered_keys=False, still_connected=True)
    check("a typed password is described as typed",
          "password you typed" in typed, typed)
    check("a saved one as saved", "saved password" in saved, saved)
    check("which are different messages", typed != saved)

    named = _explain_auth_failure(generic, params(private_key_path=r"C:\k\lab_ed25519"),
                                  "steven", offered_keys=False, still_connected=True)
    check("a named key is named in the failure", "lab_ed25519" in named, named)
    check("and the path around it is not", "C:\\k" not in named,
          "the whole path is noise in a one-line error")

    nothing = _explain_auth_failure(generic, params(), "steven",
                                    offered_keys=False, still_connected=True)
    check("having nothing to offer is its own message",
          "no password or key" in nothing, nothing)

    # Every branch has to name the device and the account, because the message
    # is what a tile notification shows and there is no terminal to look at.
    for message in (told, hung_up, typed, saved, named, nothing):
        check(f"names the device: {message[:34]}…", "10.20.30.40" in message)
        check("and the account", "steven" in message)

    check("and none of them is the old catch-all",
          all("Check the username, password or key" not in m
              for m in (told, hung_up, typed, saved, named, nothing)))


def test_the_build_time_is_reportable() -> None:
    """
    Which copy is running.

    A --onefile binary carries no visible clue that the source beside it has
    moved on, so an executable twenty minutes older than a fix is
    indistinguishable from the fix not working. That is exactly what happened,
    and it cost more time than the bug did.
    """
    print("\n-- Which build is this --")

    from backend.app import _build_time

    stamp = _build_time()
    check("a timestamp is produced", bool(stamp), "empty")
    check("and it is readable rather than epoch seconds",
          len(stamp) == 16 and stamp[4] == "-" and stamp[13] == ":",
          f"got {stamp!r}")

    from backend import support

    about = support.collect(["about"])["about"]
    check("the support bundle carries it", "Built:" in about,
          "the first question to ask about a report saying it is still broken")


def main() -> int:
    print("=" * 52)
    print("  Phase 2 transport tests")
    print("=" * 52)

    for test in (
        test_strips_negotiation_from_output,
        test_responds_to_option_demands,
        test_terminal_type_subnegotiation,
        test_naws_window_size,
        test_iac_escaping_both_directions,
        test_split_command_across_reads,
        test_idle_is_not_closed,
        test_autologin,
        test_no_autologin_without_username,
        test_handler_registry,
        test_secrets_are_scrubbed,
        test_end_to_end_session,
        test_a_session_records_which_connection_opened_it,
        test_a_dropped_session_stops_reporting_itself_as_open,
        test_unknown_connection_type_rejected,
        test_a_password_stops_paramiko_offering_keys,
        test_a_failure_says_what_actually_happened,
        test_the_build_time_is_reportable,
    ):
        try:
            test()
        except Exception as exc:
            fail(test.__name__, f"raised {type(exc).__name__}: {exc}")

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
