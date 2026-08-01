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

from backend.connections.base import ConnectionError_, ConnectionHandler

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
            waiting = self._serial.in_waiting
            data = self._serial.read(max(1, min(size, waiting or 1)))
            return data if data else None
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
