"""
connections/base.py — The transport contract every connection type implements.

ShellMate Portable speaks to devices over SSH, a serial console cable, or
telnet.  Above the transport, nothing cares which: the WebSocket bridge, the
session buffer and the AI context all work in terms of "bytes in, bytes out".
This module defines that boundary so a new transport is a new subclass and
nothing else changes.

The awkward part of the contract is :meth:`ConnectionHandler.recv`, which has
to distinguish two things that both look like "no bytes":

* **Nothing arrived yet** — the device is connected and idle.  Normal; the
  read loop should keep waiting.
* **The far end closed** — the session is over and the tab should be marked
  disconnected.

Returning ``None`` for the first and ``b""`` for the second keeps that
distinction explicit at every call site.  Signalling idleness with an
exception, as the raw paramiko API does, invites the two being conflated —
and a handler that reports "closed" whenever a device simply goes quiet drops
sessions the moment someone stops typing.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Connection parameters
# ---------------------------------------------------------------------------


@dataclass
class ConnectionParams:
    """
    Everything needed to open a session, for any transport.

    One flat structure rather than a union per transport: it maps directly to
    the JSON the connection dialog posts, and keeps profile storage simple.
    Each handler reads only the fields that apply to it.
    """

    connection_type: str = "ssh"          # "ssh" | "serial" | "telnet"
    display_label: str = ""

    # --- Network transports (ssh, telnet) ---------------------------------
    hostname: str = ""
    port: int = 22
    username: str = ""
    password: str = ""

    # --- SSH key authentication -------------------------------------------
    private_key_path: str = ""
    private_key_passphrase: str = ""
    # The account the key belongs to. SSH authenticates one username per
    # connection, so this simply takes precedence over `username` when set —
    # it exists so the name can be given beside the key it goes with, rather
    # than in a field further up the dialog.
    private_key_username: str = ""

    # --- SSH jump host / bastion ------------------------------------------
    jump_host: str = ""
    jump_port: int = 22
    jump_username: str = ""
    jump_password: str = ""
    jump_private_key_path: str = ""
    jump_private_key_passphrase: str = ""

    # --- Serial console ----------------------------------------------------
    serial_port: str = ""                 # e.g. "COM3" or "/dev/ttyUSB0"
    baud_rate: int = 9600
    data_bits: int = 8
    parity: str = "N"                     # N, E, O, M, S
    stop_bits: float = 1                  # 1, 1.5, 2
    flow_control: str = "none"            # none | xonxoff | rtscts | dsrdtr

    # --- Where the credential came from ------------------------------------
    # Not a credential and not sent anywhere: it exists so a failure can say
    # "the saved password was refused" rather than "check your password",
    # which are two different next actions. "typed" | "saved" | "".
    # Set by the API layer, which is the only part that knows.
    credential_source: str = ""

    # Answers to a keyboard-interactive challenge the device raised on a
    # previous attempt (#406): a one-time code, a second factor, a
    # "press Enter to continue". Consumed in prompt order; a prompt that
    # asks for a password is answered with the password instead. Never
    # saved anywhere — scrubbed with the other secrets.
    interactive_answers: list = field(default_factory=list)

    def effective_username(self) -> str:
        """
        The account to authenticate as.

        A username given beside the key wins, because someone who filled that
        in was answering the question "who does this key belong to" and meant
        it for this connection.
        """
        return (self.private_key_username or self.username or "").strip()

    def label(self) -> str:
        """Human-readable name for the tab, falling back to the target."""
        if self.display_label.strip():
            return self.display_label.strip()
        if self.connection_type == "serial":
            return self.serial_port or "serial"
        return self.hostname or self.connection_type

    def target(self) -> str:
        """Short description of what we are connected to, for logs and the UI."""
        if self.connection_type == "serial":
            return f"{self.serial_port}@{self.baud_rate}"
        return f"{self.hostname}:{self.port}"

    def scrub_secrets(self) -> None:
        """
        Drop credentials once the connection is established.

        Every transport calls this after authenticating.  The live session
        does not need them again, and not holding them means they cannot be
        read out of a memory dump or leak into a crash report.  Reconnecting
        a dropped tab deliberately re-prompts rather than caching them.
        """
        self.password = ""
        self.private_key_passphrase = ""
        self.jump_password = ""
        self.jump_private_key_passphrase = ""
        self.interactive_answers = []


# ---------------------------------------------------------------------------
# Handler contract
# ---------------------------------------------------------------------------


class InteractiveRequired(Exception):
    """
    The device asked something only the user can answer (#406).

    Raised out of an SSH connect when the server's keyboard-interactive
    challenge carried a prompt that is not a password prompt — a one-time
    code, a Duo push confirmation, a "verification code:". The connection
    attempt is abandoned; the API returns the prompts; the interface asks;
    the next attempt carries the answers in ``interactive_answers``.

    ``prompts`` is ``[{"text": str, "echo": bool}]`` in the order the
    server asked. ``echo`` False means what is typed should be masked.
    """

    def __init__(self, title: str, instructions: str, prompts: list[dict]) -> None:
        self.title = title or ""
        self.instructions = instructions or ""
        self.prompts = prompts
        super().__init__(f"The device is asking: {', '.join(p['text'] for p in prompts) or 'a question'}")

    def as_dict(self) -> dict:
        return {"title": self.title, "instructions": self.instructions,
                "prompts": [dict(p) for p in self.prompts]}


class ConnectionError_(Exception):
    """
    Raised when a connection cannot be established.

    Carries a message already suitable for showing to the user — handlers are
    responsible for translating library-specific errors ("Authentication
    failed.", "[Errno 13] could not open port COM3") into something a network
    engineer can act on.
    """


@dataclass
class ConnectionHandler(ABC):
    """
    Base class for a single live connection to one device.

    Subclasses own exactly one connection and are not thread-safe; the session
    manager guarantees one reader and one writer per handler.
    """

    params: ConnectionParams = field(default_factory=ConnectionParams)

    @abstractmethod
    def connect(self) -> None:
        """
        Open the connection and leave it ready for interactive use.

        Blocking — callers run this in a worker thread so the event loop is
        not stalled during a TCP or SSH handshake.

        Raises:
            ConnectionError_: With a message suitable for display.
        """

    @abstractmethod
    def send(self, data: bytes) -> None:
        """Write raw bytes to the device. Silently ignored when disconnected."""

    @abstractmethod
    def recv(self, size: int = 4096) -> bytes | None:
        """
        Read up to *size* bytes.

        Returns:
            ``bytes``: Data that arrived.
            ``None``:  Nothing arrived within the read window; still connected.
            ``b""``:   The far end closed the connection.
        """

    @abstractmethod
    def disconnect(self) -> None:
        """Close the connection. Safe to call more than once."""

    def resize(self, cols: int, rows: int) -> None:
        """
        Tell the device the terminal size changed.

        Default is a no-op: a serial console has no concept of window size,
        and plenty of telnet servers never negotiate NAWS.  Only transports
        that genuinely support it override this.
        """

    @property
    @abstractmethod
    def is_connected(self) -> bool:
        """True while the connection is usable."""
