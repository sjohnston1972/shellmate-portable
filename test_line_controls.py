"""
test_line_controls.py — The things you do to the line, not down it (#525).

Break, baud and the modem control lines are all in the same awkward
category: each is something done *to the connection* rather than sent
through it, and no two transports offer the same set. A serial port has
all three. Telnet has a break and nothing else — but that break matters
more than most, because a console server turns it into a real one on the
port it is wired to, and that is how somebody reaches ROMMON on a device
they cannot walk to. SSH has none of them: there is no line.

The rule this exists to hold is the one about refusal. A control that
silently does nothing is worse than one that says it cannot: somebody
presses Break at a device stuck in boot, sees nothing happen, and
concludes the device is dead rather than that the control never applied
to this connection. So every transport answers for every control, and the
answer for "no" carries a reason worth reading.

The other thing worth stating in a test rather than a docstring: changing
the baud rate changes **ShellMate's**, never the device's. The whole
reason somebody reaches for it is that the screen is full of rubbish, and
"I changed it and it is still rubbish" has two very different causes.

Run: python test_line_controls.py
"""

import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backend import paths  # noqa: E402

_TEMP = Path(tempfile.mkdtemp(prefix="shellmate-line-"))
paths._data_dir_cache = _TEMP
assert paths.data_dir() == _TEMP, "test isolation failed — refusing to run"

from backend.connections.base import ConnectionParams, Unsupported  # noqa: E402
from backend.connections.serial_handler import SerialHandler  # noqa: E402
from backend.connections.ssh_handler import SSHHandler  # noqa: E402
from backend.connections.telnet_handler import BRK, IAC, TelnetHandler  # noqa: E402

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
    except Unsupported as exc:
        return str(exc)
    return ""


# ---------------------------------------------------------------------------
# A telnet peer that records what reached it
# ---------------------------------------------------------------------------
class Peer:
    def __init__(self) -> None:
        self.received = bytearray()
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(1)
        self.port = self._listener.getsockname()[1]
        self._accepted = threading.Event()
        self._stop = threading.Event()
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        try:
            conn, _ = self._listener.accept()
        except OSError:
            return
        self._accepted.set()
        conn.settimeout(0.3)
        while not self._stop.is_set():
            try:
                chunk = conn.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            if not chunk:
                break
            self.received.extend(chunk)
        try:
            conn.close()
        except OSError:
            pass

    def close(self) -> None:
        self._stop.set()
        try:
            self._listener.close()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# A serial port that is only as real as it needs to be
# ---------------------------------------------------------------------------
class FakePort:
    """
    Stands in for pyserial's Serial.

    Only the parts these controls touch: a live `baudrate`, the four input
    lines and the two output ones. A real port is not available on a build
    machine, and the behaviour under test is ShellMate's — that it changes
    the right property, refuses the wrong rate, and reports what the
    adapter said when it will not.
    """

    def __init__(self) -> None:
        self.is_open = True
        self.baudrate = 9600
        self.dtr = True
        self.rts = True
        self.cts = True
        self.dsr = False
        self.ri = False
        self.cd = True
        self.breaks = 0
        self.refuse_baud = False

    def send_break(self, duration: float = 0.25) -> None:
        self.breaks += 1

    def __setattr__(self, name, value):
        if name == "baudrate" and getattr(self, "refuse_baud", False):
            raise OSError("the adapter will not do that rate")
        object.__setattr__(self, name, value)


def _serial_handler() -> SerialHandler:
    handler = SerialHandler(params=ConnectionParams(
        connection_type="serial", serial_port="COM9", baud_rate=9600))
    handler._serial = FakePort()
    return handler


def what_each_transport_says_it_can_do() -> None:
    print("\n-- Every transport answers for every control --")

    serial_caps = _serial_handler().capabilities()
    check("a serial port offers all three",
          all(serial_caps[name]["ok"] for name in ("break", "baud", "signals")),
          str(serial_caps))
    check("and says what rate it is at",
          serial_caps["baud"]["value"] == 9600, str(serial_caps["baud"]))
    check("with the rates a console actually runs at",
          9600 in serial_caps["baud"]["choices"]
          and 115200 in serial_caps["baud"]["choices"],
          str(serial_caps["baud"]["choices"]))

    telnet_caps = TelnetHandler(params=ConnectionParams(
        connection_type="telnet", hostname="10.0.0.1")).capabilities()
    check("telnet offers a break",
          telnet_caps["break"]["ok"] is True, str(telnet_caps))
    check("and refuses the other two with a reason",
          not telnet_caps["baud"]["ok"] and telnet_caps["baud"]["why"]
          and not telnet_caps["signals"]["ok"] and telnet_caps["signals"]["why"],
          str(telnet_caps))
    check("which points at the console server rather than stopping dead",
          "console server" in telnet_caps["baud"]["why"],
          telnet_caps["baud"]["why"])

    ssh_caps = SSHHandler(params=ConnectionParams(
        connection_type="ssh", hostname="10.0.0.1")).capabilities()
    check("SSH offers none of them",
          not any(ssh_caps[name]["ok"] for name in ssh_caps), str(ssh_caps))
    check("and its reason says where the break *is* available",
          "console" in ssh_caps["break"]["why"], ssh_caps["break"]["why"])

    # The point of the reasons: the control is disabled and explains
    # itself, rather than being hidden and leaving somebody hunting a
    # menu for something that was never there.
    for caps, label in ((telnet_caps, "telnet"), (ssh_caps, "ssh")):
        check(f"every refusal on {label} carries a reason",
              all(entry["why"] for entry in caps.values() if not entry["ok"]),
              str(caps))


def the_serial_controls() -> None:
    print("\n-- A serial port --")

    handler = _serial_handler()
    handler.send_break()
    check("a break reaches the port", handler._serial.breaks == 1)

    check("the baud rate changes without reconnecting",
          handler.set_baud(115200) == 115200 and handler._serial.baudrate == 115200,
          "pyserial's baudrate is live, and reconnecting a console loses "
          "whatever the device printed while you were away")
    check("and the session's own record of it follows",
          handler.params.baud_rate == 115200, str(handler.params.baud_rate))
    check("so the next capabilities call reports the new rate",
          handler.capabilities()["baud"]["value"] == 115200)

    why = refuses(handler.set_baud, 96000)
    check("a rate no console uses is refused", bool(why), "96000 was accepted")
    check("and the refusal lists the ones that work",
          "115200" in why and "9600" in why, why)
    check("the port is left where it was",
          handler._serial.baudrate == 115200, str(handler._serial.baudrate))

    # An adapter that will not do it is a different failure from a rate
    # ShellMate will not offer, and the session goes on at the old rate —
    # so the message has to say which rate that is, or the next screenful
    # of rubbish gets read as the device's fault.
    handler._serial.refuse_baud = True
    why = refuses(handler.set_baud, 9600)
    check("an adapter that refuses is reported as the adapter refusing",
          "adapter" in why, why)
    check("and the message says what rate is still in use",
          "115200" in why, why)
    handler._serial.refuse_baud = False


def the_modem_control_lines() -> None:
    print("\n-- The lines --")

    handler = _serial_handler()
    lines = handler.line_signals()
    check("every line is reported",
          {"dtr", "rts", "cts", "dsr", "ri", "cd"} <= set(lines), str(lines))
    check("and which of them can be driven",
          lines["writable"] == ["dtr", "rts"], str(lines.get("writable")))

    after = handler.set_line_signal("dtr", False)
    check("DTR can be dropped",
          after["dtr"] is False and handler._serial.dtr is False, str(after))
    check("and raised again",
          handler.set_line_signal("dtr", True)["dtr"] is True)

    why = refuses(handler.set_line_signal, "cts", True)
    check("an input line cannot be driven", bool(why), "CTS was accepted")
    check("and the refusal says whose line it is",
          "outputs" in why or "device's" in why, why)

    handler._serial.is_open = False
    check("a closed port refuses rather than pretending",
          bool(refuses(handler.line_signals)),
          "returning the last known state of a port that is gone is the "
          "kind of answer somebody acts on")


def the_telnet_break() -> None:
    print("\n-- The telnet break --")

    peer = Peer()
    handler = TelnetHandler(params=ConnectionParams(
        connection_type="telnet", hostname="127.0.0.1", port=peer.port))
    try:
        handler.connect()
        peer._accepted.wait(2.0)
        time.sleep(0.2)
        before = len(peer.received)

        handler.send_break()
        deadline = time.time() + 2.0
        while time.time() < deadline and len(peer.received) == before:
            time.sleep(0.05)

        sent = bytes(peer.received[before:])
        check("IAC BRK reaches the far end",
              bytes([IAC, BRK]) in sent, repr(sent))
        check("and nothing doubled the IAC",
              bytes([IAC, IAC]) not in sent,
              "this is a command, not data — escaping it would send the two "
              "literal bytes as text and the console server would forward "
              "them to the device")

        why = refuses(handler.set_baud, 9600)
        check("baud is refused on telnet with its reason", bool(why), why)
        check("as are the modem lines",
              bool(refuses(handler.set_line_signal, "dtr", True)))
    finally:
        handler.disconnect()
        peer.close()


def ssh_refuses_all_three() -> None:
    print("\n-- SSH has no line at all --")

    handler = SSHHandler(params=ConnectionParams(
        connection_type="ssh", hostname="10.0.0.1"))
    for label, call in (("break", lambda: handler.send_break()),
                        ("baud", lambda: handler.set_baud(9600)),
                        ("a line", lambda: handler.set_line_signal("dtr", True))):
        why = refuses(call)
        check(f"{label} is refused, with a reason", bool(why), f"{label} was accepted")


if __name__ == "__main__":
    what_each_transport_says_it_can_do()
    the_serial_controls()
    the_modem_control_lines()
    the_telnet_break()
    ssh_refuses_all_three()

    print("\n" + "=" * 52)
    print(f"  {passed} passed  |  {len(failed)} failed")
    print("=" * 52)
    for line in failed:
        print("  -", line)
    sys.exit(1 if failed else 0)
