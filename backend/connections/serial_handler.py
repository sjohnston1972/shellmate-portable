"""
connections/serial_handler.py — Serial console handler.

For console-cable access to network devices: the connection you fall back on
when the device has no IP address yet, or when you have just broken the
management network and SSH is no longer an option.

A serial line is dumber than SSH in ways that matter here.  There is no
authentication, no window-size negotiation and — importantly — no concept of
"connected".  A USB-to-serial adapter reports the port as open whether or not
anything is plugged into the other end, so silence is ambiguous in a way it
never is over TCP.  That shapes two decisions below: the wake-up carriage
return on connect, and treating a read timeout as idle rather than closed.

Needs no administrator rights: opening a COM port is a normal user operation
on Windows once the adapter's driver is installed.
"""

import logging
from dataclasses import dataclass, field

import serial
from serial.tools import list_ports

from backend.connections.base import ConnectionError_, ConnectionHandler, Unsupported

from backend.advanced import get as advanced

logger = logging.getLogger(__name__)

# Blocking read window. Matches the SSH handler so the read loop behaves
# identically regardless of transport.
READ_TIMEOUT = 0.5

PARITY_MAP = {
    "N": serial.PARITY_NONE,
    "E": serial.PARITY_EVEN,
    "O": serial.PARITY_ODD,
    "M": serial.PARITY_MARK,
    "S": serial.PARITY_SPACE,
}

STOP_BITS_MAP = {
    1: serial.STOPBITS_ONE,
    1.5: serial.STOPBITS_ONE_POINT_FIVE,
    2: serial.STOPBITS_TWO,
}

BYTE_SIZE_MAP = {
    5: serial.FIVEBITS,
    6: serial.SIXBITS,
    7: serial.SEVENBITS,
    8: serial.EIGHTBITS,
}


def available_ports() -> list[dict]:
    """
    Enumerate serial ports present on this machine.

    Returns a description and hardware ID alongside the device name, because
    "COM3" alone is not enough to pick the right one when a laptop has a
    docking station, a Bluetooth virtual port and two USB adapters attached.
    """
    ports = []
    for port in list_ports.comports():
        ports.append({
            "device":      port.device,
            "description": port.description or "",
            "hwid":        port.hwid or "",
            "manufacturer": port.manufacturer or "",
        })
    # Natural-ish ordering so COM3 sorts before COM10.
    ports.sort(key=lambda p: (len(p["device"]), p["device"]))
    return ports


@dataclass
class SerialHandler(ConnectionHandler):
    """Manages a single serial console connection."""

    _serial: serial.Serial | None = field(default=None, init=False, repr=False)

    def connect(self) -> None:
        """Open the serial port and wake the device up."""
        params = self.params

        if not params.serial_port:
            raise ConnectionError_("No serial port selected.")

        try:
            self._serial = serial.Serial(
                port=params.serial_port,
                baudrate=params.baud_rate,
                bytesize=BYTE_SIZE_MAP.get(params.data_bits, serial.EIGHTBITS),
                parity=PARITY_MAP.get(params.parity.upper(), serial.PARITY_NONE),
                stopbits=STOP_BITS_MAP.get(params.stop_bits, serial.STOPBITS_ONE),
                timeout=READ_TIMEOUT,
                write_timeout=5,
                xonxoff=params.flow_control == "xonxoff",
                rtscts=params.flow_control == "rtscts",
                dsrdtr=params.flow_control == "dsrdtr",
            )
        except serial.SerialException as exc:
            raise ConnectionError_(self._explain_open_failure(params.serial_port, exc)) from exc
        except ValueError as exc:
            raise ConnectionError_(f"Invalid serial settings: {exc}") from exc

        # A device that is already sitting at a prompt has nothing to say
        # until spoken to, so a freshly opened console looks identical to a
        # dead one. A carriage return draws out the prompt and confirms
        # something is actually on the other end of the cable.
        try:
            # Most devices need the nudge to print anything at all;
            # a few react badly, so it is switchable.
            if advanced("ssh.serial_wake_on_connect"):
                self._serial.write(b"\r\n")
        except serial.SerialException:
            pass

        logger.info("Serial port open: %s at %s baud", params.serial_port, params.baud_rate)

    @staticmethod
    def _explain_open_failure(port: str, exc: Exception) -> str:
        """
        Turn a pyserial exception into something actionable.

        "could not open port COM3: [Error 5] Access is denied" is a specific,
        common and fixable problem, and saying so beats echoing the errno.
        """
        message = str(exc)
        lowered = message.lower()

        if "access is denied" in lowered or "permission" in lowered:
            others = ", ".join(p["device"] for p in available_ports()) or "none detected"
            return (
                f"{port} is already in use — most likely by PuTTY, another "
                f"terminal, or a previous session that did not close. "
                f"Ports currently present: {others}."
            )
        if "could not open port" in lowered or "filenotfound" in lowered:
            others = ", ".join(p["device"] for p in available_ports()) or "none detected"
            return (
                f"{port} was not found. Check the console cable is plugged in "
                f"and the USB-to-serial driver is installed. "
                f"Ports currently present: {others}."
            )
        return f"Could not open {port}: {message}"

    # ------------------------------------------------------------------
    # I/O
    # ------------------------------------------------------------------

    def send(self, data: bytes) -> None:
        """Write bytes to the serial line."""
        if self._serial and self._serial.is_open:
            try:
                self._serial.write(data)
            except serial.SerialException as exc:
                logger.warning("Serial write failed on %s: %s", self.params.serial_port, exc)

    def recv(self, size: int = 4096) -> bytes | None:
        """
        Read from the serial line.

        Returns None on an empty read rather than b"": a quiet console is the
        normal resting state, not a closed one. A serial session ends only
        when the port itself goes away — the adapter being unplugged — which
        surfaces as a SerialException.
        """
        if self._serial is None or not self._serial.is_open:
            return b""

        try:
            # Read whatever has arrived; block up to the timeout for the first
            # byte so we are not busy-waiting.
            # Block for the first byte, then take whatever arrived behind
            # it (#476). Reading `max(1, waiting)` when the port was idle
            # returned one character and left the rest for the next pass,
            # so every burst became two messages, two transcript feeds and
            # two log appends, the first carrying a single character.
            first = self._serial.read(1)
            if not first:
                return None
            rest = min(size - 1, self._serial.in_waiting)
            return first + (self._serial.read(rest) if rest > 0 else b"")
        except serial.SerialException as exc:
            # Adapter unplugged mid-session.
            logger.info("Serial port %s disappeared: %s", self.params.serial_port, exc)
            return b""
        except OSError:
            return b""

    def send_break(self, duration: float = 0.25) -> None:
        """
        Send a break signal.

        This is how you interrupt a Cisco device during boot to reach ROMMON —
        one of the few reasons a console cable is still indispensable.
        """
        if self._serial and self._serial.is_open:
            try:
                self._serial.send_break(duration)
            except serial.SerialException as exc:
                logger.warning("Could not send break on %s: %s", self.params.serial_port, exc)

    # ------------------------------------------------------------------
    # The line itself (#525)
    # ------------------------------------------------------------------

    #: Offered in the menu. Everything a console is realistically set to,
    #: and nothing else — an arbitrary number box invites 96000 and a
    #: session of rubbish that reads as a broken cable.
    BAUD_RATES = (1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200)

    def capabilities(self) -> dict:
        return {
            "break":   {"ok": True, "why": ""},
            "baud":    {"ok": True, "why": "", "value": self.params.baud_rate,
                        "choices": list(self.BAUD_RATES)},
            "signals": {"ok": True, "why": ""},
        }

    def set_baud(self, baud: int) -> int:
        """
        Change the speed this end talks at, without dropping the session.

        pyserial's `baudrate` is a live property, so this is a reconfigure
        rather than a reconnect — which matters because reconnecting a
        console loses whatever the device printed while you were away, and
        the reason to change baud is usually that you cannot read it.

        It changes **ShellMate's** rate and never the device's. A console
        fixed at 9600 spoken to at 115200 answers in rubbish either way
        round; this is how you match it, not how you move it.
        """
        try:
            rate = int(baud)
        except (TypeError, ValueError):
            raise Unsupported(f"{baud!r} is not a baud rate.") from None
        if rate not in self.BAUD_RATES:
            raise Unsupported(
                f"{rate} is not one of the rates ShellMate offers. A console "
                "runs at one of " + ", ".join(str(r) for r in self.BAUD_RATES)
                + ".")
        if not (self._serial and self._serial.is_open):
            raise Unsupported("The port is not open.")

        try:
            self._serial.baudrate = rate
        except (serial.SerialException, ValueError, OSError) as exc:
            # The adapter refused it. Reported rather than swallowed: the
            # session goes on at the old rate, and somebody has to know
            # which rate that is or they will read the next screenful of
            # rubbish as the device's fault.
            raise Unsupported(
                f"The adapter would not change to {rate}: {exc}. Still at "
                f"{self.params.baud_rate}.") from exc

        self.params.baud_rate = rate
        logger.info("Serial %s now at %d baud", self.params.serial_port, rate)
        return rate

    #: The lines this can raise or drop, and the lines it can only read.
    #: DTR and RTS are outputs — an adapter wired to a device's reset holds
    #: it there while DTR is asserted, which is the reason this exists.
    OUTPUT_SIGNALS = ("dtr", "rts")
    INPUT_SIGNALS = ("cts", "dsr", "ri", "cd")

    def line_signals(self) -> dict:
        """
        Every modem control line, as it stands.

        Inputs are read-only and included anyway: "RTS is up and CTS is
        down" is the whole diagnosis for a session that accepts typing and
        sends nothing, and neither half means anything without the other.
        """
        if not (self._serial and self._serial.is_open):
            raise Unsupported("The port is not open.")
        out = {}
        for name in self.OUTPUT_SIGNALS + self.INPUT_SIGNALS:
            try:
                out[name] = bool(getattr(self._serial, name))
            except Exception:
                # An adapter that cannot report a line is not an error —
                # plenty of USB bridges expose none of the inputs. Absent
                # rather than guessed at as False, which would read as
                # "the device is not asserting it".
                out[name] = None
        out["writable"] = list(self.OUTPUT_SIGNALS)
        return out

    def set_line_signal(self, name: str, on: bool) -> dict:
        """Raise or drop DTR or RTS."""
        wanted = (name or "").strip().lower()
        if wanted not in self.OUTPUT_SIGNALS:
            raise Unsupported(
                f"{name} is not a line ShellMate can drive. DTR and RTS are "
                "outputs; CTS, DSR, RI and CD are the device's to assert.")
        if not (self._serial and self._serial.is_open):
            raise Unsupported("The port is not open.")
        try:
            setattr(self._serial, wanted, bool(on))
        except Exception as exc:
            raise Unsupported(
                f"The adapter would not change {wanted.upper()}: {exc}") from exc
        logger.info("Serial %s: %s %s", self.params.serial_port,
                    wanted.upper(), "asserted" if on else "dropped")
        return self.line_signals()

    # ------------------------------------------------------------------
    # Teardown
    # ------------------------------------------------------------------

    def disconnect(self) -> None:
        """Close the serial port, releasing it for other applications."""
        if self._serial is not None:
            try:
                self._serial.close()
            except Exception:
                pass
            self._serial = None
        logger.info("Serial port closed: %s", self.params.serial_port)

    @property
    def is_connected(self) -> bool:
        """True while the port is open. Says nothing about the far end."""
        return self._serial is not None and self._serial.is_open
