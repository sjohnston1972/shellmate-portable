"""
connections/telnet_handler.py — Telnet connection handler.

Telnet is still everywhere in network estates: older switches, out-of-band
terminal servers, lab gear, and anything whose SSH stack was disabled or
never worked.  A tool for network engineers that cannot speak it is missing a
transport they need weekly.

Implemented directly on a socket rather than on ``telnetlib``, for two
reasons.  ``telnetlib`` was deprecated in Python 3.11 and removed in 3.13, so
building on it means building on something already gone.  More usefully,
network devices care about *which* options get negotiated — SGA in particular
decides whether the session is character-at-a-time or line-at-a-time — and
owning the negotiation makes that explicit rather than incidental.

The protocol itself is small: RFC 854 for the command framing, RFC 1073 for
window size, RFC 1091 for terminal type.
"""

import logging
import re
import socket
import time
from dataclasses import dataclass, field

from backend.connections.base import ConnectionError_, ConnectionHandler

from backend.advanced import get as advanced

logger = logging.getLogger(__name__)

READ_TIMEOUT = 0.5

# --- RFC 854 command bytes -------------------------------------------------
IAC  = 255   # Interpret As Command — introduces every command sequence
DONT = 254
DO   = 253
WONT = 252
WILL = 251
SB   = 250   # Subnegotiation begin
SE   = 240   # Subnegotiation end
BRK  = 243   # RFC 854 "Break" — the NVT break, not a character
IP   = 244   # RFC 854 "Interrupt Process"

# --- Options we care about -------------------------------------------------
OPT_ECHO = 1    # RFC 857  — the device echoes typed characters
OPT_SGA  = 3    # RFC 858  — suppress go-ahead, i.e. character-at-a-time
OPT_TTYPE = 24  # RFC 1091 — terminal type
OPT_NAWS = 31   # RFC 1073 — negotiate about window size

TERMINAL_TYPE = b"xterm-256color"

# Prompts that mean the device is asking who we are. Deliberately narrow:
# a false positive here would type a password into a live session.
USER_PROMPT = re.compile(rb"(?:user\s*name|username|user|login)\s*[:>]\s*$", re.IGNORECASE)
PASS_PROMPT = re.compile(rb"pass\s*word\s*[:>]\s*$", re.IGNORECASE)

# Auto-login gives up after this long and hands control to the user. Without a
# deadline, a credential prompt regex could match ordinary output an hour into
# a session and inject a password into it.
AUTOLOGIN_TIMEOUT = 30.0


@dataclass
class TelnetHandler(ConnectionHandler):
    """Manages a single telnet connection, including option negotiation."""

    _socket: socket.socket | None = field(default=None, init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    # Parser state carried between recv() calls, since a command sequence can
    # be split across TCP segments.
    _buffer: bytes = field(default=b"", init=False, repr=False)

    # Last known window size, so we can answer a NAWS request that arrives
    # before the browser has told us the terminal dimensions.
    _cols: int = field(default=80, init=False, repr=False)
    _rows: int = field(default=24, init=False, repr=False)

    # Auto-login state. Credentials are held here only until login completes.
    _login_stage: str = field(default="user", init=False, repr=False)  # user -> password -> done
    _login_user: str = field(default="", init=False, repr=False)
    _login_password: str = field(default="", init=False, repr=False)
    _login_deadline: float = field(default=0.0, init=False, repr=False)
    _recent: bytes = field(default=b"", init=False, repr=False)

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """Open the TCP connection and start option negotiation."""
        params = self.params
        port = params.port or 23

        try:
            self._socket = socket.create_connection(
                (params.hostname, port),
                timeout=advanced("ssh.telnet_connect_timeout"),
            )
        except socket.timeout as exc:
            raise ConnectionError_(
                f"Timed out connecting to {params.hostname}:{port}. "
                f"Check the address is reachable and telnet is enabled."
            ) from exc
        except OSError as exc:
            raise ConnectionError_(f"Could not reach {params.hostname}:{port}: {exc}") from exc

        self._socket.settimeout(READ_TIMEOUT)
        # Nagle batches small writes, which on an interactive session shows up
        # as keystrokes arriving in clumps.
        self._socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._closed = False

        # Announce what we support up front rather than waiting to be asked.
        # SGA is the one that matters: without it many devices stay in
        # line-at-a-time mode and tab completion never works.
        self._send_command(WILL, OPT_TTYPE)
        self._send_command(WILL, OPT_NAWS)
        self._send_command(DO, OPT_SGA)
        self._send_command(WILL, OPT_SGA)

        if params.username:
            self._login_user = params.username
            self._login_password = params.password
            # Zero disables auto-login entirely. The deadline exists so a
            # prompt-shaped line arriving an hour in cannot type a password
            # into a live device.
            window = advanced("ssh.telnet_autologin_deadline")
            self._login_deadline = (
                time.monotonic() + window if window else 0.0)
        else:
            self._login_stage = "done"

        params.scrub_secrets()
        logger.info("Telnet connected to %s:%s", params.hostname, port)

    # ------------------------------------------------------------------
    # Option negotiation
    # ------------------------------------------------------------------

    def _send_raw(self, data: bytes) -> None:
        """Write straight to the socket, bypassing IAC escaping."""
        if self._socket is None or self._closed:
            return
        try:
            self._socket.sendall(data)
        except OSError:
            self._closed = True

    def _send_command(self, verb: int, option: int) -> None:
        """Send a two-byte IAC command such as WILL SGA."""
        self._send_raw(bytes([IAC, verb, option]))

    def _respond_to_demand(self, verb: int, option: int) -> None:
        """
        Answer a WILL/WONT/DO/DONT from the far end.

        The rule is to agree only to options we actually implement and refuse
        everything else. Silence is not an option — a device waiting on a
        reply will stall, which looks to the user like a hung session.
        """
        if verb == DO:
            # "Please turn this on at your end."
            if option in (OPT_TTYPE, OPT_NAWS, OPT_SGA):
                self._send_command(WILL, option)
                if option == OPT_NAWS:
                    self._send_window_size()
            else:
                self._send_command(WONT, option)

        elif verb == DONT:
            self._send_command(WONT, option)

        elif verb == WILL:
            # "I will turn this on at my end."
            if option in (OPT_ECHO, OPT_SGA):
                self._send_command(DO, option)
            else:
                self._send_command(DONT, option)

        elif verb == WONT:
            self._send_command(DONT, option)

    def _handle_subnegotiation(self, payload: bytes) -> None:
        """
        Handle an IAC SB ... IAC SE block.

        The only one we answer is the terminal-type request, which asks us to
        name ourselves so the device knows which escape sequences it may send.
        """
        if not payload:
            return
        # TTYPE SEND (1) -> reply TTYPE IS (0) <name>
        if payload[0] == OPT_TTYPE and len(payload) > 1 and payload[1] == 1:
            self._send_raw(bytes([IAC, SB, OPT_TTYPE, 0]) + TERMINAL_TYPE + bytes([IAC, SE]))

    def _send_window_size(self) -> None:
        """Send the current terminal dimensions as a NAWS subnegotiation."""
        # Width and height are 16-bit big-endian. A literal 255 in either has
        # to be doubled, since 255 is IAC even inside a subnegotiation.
        raw = self._cols.to_bytes(2, "big") + self._rows.to_bytes(2, "big")
        escaped = raw.replace(bytes([IAC]), bytes([IAC, IAC]))
        self._send_raw(bytes([IAC, SB, OPT_NAWS]) + escaped + bytes([IAC, SE]))

    # ------------------------------------------------------------------
    # Stream parsing
    # ------------------------------------------------------------------

    def _extract(self, data: bytes) -> bytes:
        """
        Strip telnet commands from *data*, answering them as they appear.

        Returns only the application bytes destined for the terminal.

        Anything incomplete at the end of the chunk is kept in ``_buffer`` and
        reprocessed with the next read, because a command sequence can be
        split across TCP segments and a half-parsed IAC would otherwise leak
        rubbish onto the user's screen.
        """
        stream = self._buffer + data
        self._buffer = b""
        out = bytearray()
        i = 0
        length = len(stream)

        while i < length:
            byte = stream[i]

            if byte != IAC:
                out.append(byte)
                i += 1
                continue

            # Need at least the verb.
            if i + 1 >= length:
                self._buffer = stream[i:]
                break

            verb = stream[i + 1]

            if verb == IAC:
                # Escaped 255 — a literal data byte.
                out.append(IAC)
                i += 2

            elif verb in (WILL, WONT, DO, DONT):
                if i + 2 >= length:
                    self._buffer = stream[i:]
                    break
                self._respond_to_demand(verb, stream[i + 2])
                i += 3

            elif verb == SB:
                end = stream.find(bytes([IAC, SE]), i + 2)
                if end == -1:
                    self._buffer = stream[i:]
                    break
                self._handle_subnegotiation(stream[i + 2:end])
                i = end + 2

            else:
                # Two-byte command we do not implement (NOP, Are You There,
                # Break...). Skip it rather than letting it reach the screen.
                i += 2

        return bytes(out)

    # ------------------------------------------------------------------
    # Auto-login
    # ------------------------------------------------------------------

    def _try_autologin(self, text: bytes) -> None:
        """
        Answer a username or password prompt once, if credentials were given.

        Bounded deliberately. It runs only until login completes or the
        deadline passes, after which it is disabled permanently — a prompt
        regex matching ordinary output later in a session would otherwise
        type a password into a live device.
        """
        if self._login_stage == "done":
            return

        if time.monotonic() > self._login_deadline:
            logger.info("Auto-login timed out for %s; handing over to the user", self.params.hostname)
            self._finish_autologin()
            return

        # Match against a small trailing window: prompts sit at the end of the
        # stream, and this keeps a matching string from earlier output from
        # being re-triggered.
        self._recent = (self._recent + text)[-256:]

        if self._login_stage == "user" and USER_PROMPT.search(self._recent):
            self.send(self._login_user.encode() + b"\r\n")
            self._login_stage = "password" if self._login_password else "done"
            self._recent = b""
            if self._login_stage == "done":
                self._finish_autologin()

        elif self._login_stage == "password" and PASS_PROMPT.search(self._recent):
            self.send(self._login_password.encode() + b"\r\n")
            self._finish_autologin()

    def _finish_autologin(self) -> None:
        """Disable auto-login and drop the credentials it was holding."""
        self._login_stage = "done"
        self._login_user = ""
        self._login_password = ""
        self._recent = b""

    # ------------------------------------------------------------------
    # I/O
    # ------------------------------------------------------------------

    def send(self, data: bytes) -> None:
        """
        Write bytes to the device.

        A literal 255 in the data has to be doubled, or the device reads it as
        the start of a command.
        """
        if self._socket is None or self._closed:
            return
        self._send_raw(data.replace(bytes([IAC]), bytes([IAC, IAC])))

    def recv(self, size: int = 4096) -> bytes | None:
        """Read from the device, handling negotiation transparently."""
        if self._socket is None or self._closed:
            return b""

        try:
            chunk = self._socket.recv(size)
        except socket.timeout:
            return None
        except OSError:
            self._closed = True
            return b""

        if not chunk:
            self._closed = True
            return b""

        data = self._extract(chunk)

        if self._login_stage != "done":
            self._try_autologin(data)

        # The chunk may have been pure negotiation, which is not user data and
        # must not be mistaken for a closed connection.
        return data if data else None

    def resize(self, cols: int, rows: int) -> None:
        """Send the new window size if the device negotiated NAWS."""
        self._cols, self._rows = cols, rows
        self._send_window_size()

    # ------------------------------------------------------------------
    # Teardown
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # The line itself (#525)
    # ------------------------------------------------------------------

    def capabilities(self) -> dict:
        return {
            "break":   {"ok": True, "why": ""},
            "baud":    {"ok": False, "why": self._no_baud()},
            "signals": {"ok": False, "why": self._no_signals()},
        }

    def _no_baud(self) -> str:
        return ("This is a telnet session, which has no baud rate. If the "
                "far end is a console server, its own port speed is set on "
                "the console server, not here.")

    def _no_signals(self) -> str:
        return ("This is a telnet session, which has no modem control "
                "lines. A console server may expose DTR on its own line, "
                "through its own interface.")

    def send_break(self, duration: float = 0.25) -> None:
        """
        The telnet break, `IAC BRK`.

        Every console server worth having — Opengear, Avocent, a Cisco
        async line — turns this into a real break on the serial port it is
        wired to, and that is how somebody reaches ROMMON on a device they
        cannot walk to. Not sending it meant the one thing a console
        server is for was the one thing this transport could not do.

        `duration` is accepted and ignored: a break here is an event, not
        a length of time. The far end decides how long to hold the line,
        and pretending otherwise would put a number in the interface that
        changes nothing.
        """
        if self._socket is None:
            return
        try:
            # No IAC-doubling: this *is* a command, not data. Escaping it
            # would send the two literal bytes 255 and 243 as text.
            self._send_raw(bytes([IAC, BRK]))
            logger.info("Telnet break sent to %s", self.params.target())
        except OSError as exc:
            logger.warning("Could not send a telnet break: %s", exc)

    def disconnect(self) -> None:
        """Close the socket."""
        self._closed = True
        self._finish_autologin()
        if self._socket is not None:
            try:
                self._socket.close()
            except Exception:
                pass
            self._socket = None
        logger.info("Telnet disconnected from %s", self.params.hostname)

    @property
    def is_connected(self) -> bool:
        """True while the socket is open."""
        return self._socket is not None and not self._closed
