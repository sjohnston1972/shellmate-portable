"""
connections/ssh_handler.py — SSH connection handler.

Wraps paramiko to provide a raw interactive shell channel.  Paramiko's
``invoke_shell()`` is used rather than a higher-level automation library
because the user is typing live: tab completion, ``--More--`` paging and
coloured output all depend on having a real PTY rather than a
send-command-parse-response loop.

Supports three things the original password-only handler did not:

* **Key authentication**, including encrypted keys, with the key never leaving
  the local machine.
* **A jump host**, so devices on a management network reachable only through
  a bastion can be opened in a tab like any other.
* **Multiplexed channels** via :meth:`open_secondary_channel`, which later
  phases use to run background commands without disturbing what the user is
  typing.
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path

import paramiko

from backend.connections.base import ConnectionError_, ConnectionHandler, ConnectionParams

from backend.advanced import get as advanced

logger = logging.getLogger(__name__)

# How long to wait on the TCP + SSH handshake before giving up.
CONNECT_TIMEOUT = 15

# recv() blocks for this long before reporting "nothing yet". Short enough that
# a closed channel is noticed promptly, long enough not to spin the CPU.
READ_TIMEOUT = 0.5

# Key types to try when the user supplies a key file. Paramiko needs to be told
# the algorithm, and the file itself does not reliably say which it is.
KEY_CLASSES = (
    paramiko.Ed25519Key,
    paramiko.ECDSAKey,
    paramiko.RSAKey,
    paramiko.DSSKey,
)


def load_private_key(path: str, passphrase: str = "") -> paramiko.PKey:
    """
    Load a private key, trying each supported algorithm in turn.

    Paramiko has no reliable "detect the type" entry point, and the file
    extension tells us nothing, so the only robust approach is to try each
    class and keep the one that parses.

    Args:
        path:       Path to the private key file.
        passphrase: Passphrase, or "" for an unencrypted key.

    Raises:
        ConnectionError_: The file is missing, unreadable, or the passphrase
            is wrong — distinguished so the user gets a useful message rather
            than a generic parse failure.
    """
    key_path = Path(path).expanduser()
    if not key_path.is_file():
        raise ConnectionError_(f"Private key not found: {key_path}")

    needs_passphrase = False

    for key_class in KEY_CLASSES:
        try:
            return key_class.from_private_key_file(str(key_path), password=passphrase or None)
        except paramiko.PasswordRequiredException:
            # Right algorithm, wrong or missing passphrase. Keep trying the
            # others in case of an ambiguous parse, but remember why.
            needs_passphrase = True
        except (paramiko.SSHException, ValueError):
            continue
        except OSError as exc:
            raise ConnectionError_(f"Could not read private key {key_path}: {exc}") from exc

    if needs_passphrase:
        raise ConnectionError_(
            f"Private key {key_path.name} is encrypted and the passphrase was "
            f"missing or incorrect."
        )
    raise ConnectionError_(
        f"Unsupported or malformed private key: {key_path.name}. "
        f"Supported types are Ed25519, ECDSA, RSA and DSA."
    )


def _host_key_policy():
    """
    What to do about a host key ShellMate has not seen before.

    Auto-add by default: network devices legitimately change keys after an RMA
    or an image upgrade, and blocking on an unknown-host prompt would make the
    tool unusable on the estate it is built for. A site where every key is
    already known can choose to reject instead.
    """
    import paramiko

    choice = str(advanced("ssh.host_key_policy"))
    if choice == "reject":
        return paramiko.RejectPolicy()
    if choice == "warn":
        return paramiko.WarningPolicy()
    return paramiko.AutoAddPolicy()


def _algorithm_overrides() -> dict:
    """
    Turn the user's chosen algorithm lists into paramiko's disabled_algorithms.

    paramiko takes a list of what *not* to offer rather than what to prefer, so
    naming what you want means disabling everything else. Nothing chosen
    disables nothing, which is the default behaviour.

    All four negotiated lists are covered. A device old enough to need a legacy
    key exchange usually needs a legacy MAC and host key algorithm too, and
    fixing one of the four leaves the same device unreachable for a different
    reason.
    """
    from backend.advanced import available_algorithms

    disabled: dict[str, list[str]] = {}
    chosen: dict[str, set[str]] = {}

    for setting, group in (("ssh.kex_algorithms", "kex"),
                           ("ssh.ciphers", "ciphers"),
                           ("ssh.macs", "macs"),
                           ("ssh.host_key_algorithms", "keys")):
        wanted = _split(advanced(setting))
        if not wanted:
            continue
        chosen[group] = wanted
        disabled[group] = [
            name for name in available_algorithms(group) if name not in wanted
        ]

    if not disabled:
        return {}

    logger.info("Restricting SSH algorithms: %s",
                "; ".join(f"{g}={sorted(v)}" for g, v in chosen.items()))
    return {"disabled_algorithms": disabled}


def _split(value: str) -> set[str]:
    return {part.strip() for part in str(value or "").split(",") if part.strip()}


@dataclass
class SSHHandler(ConnectionHandler):
    """Manages a single SSH connection and its interactive shell."""

    _client: paramiko.SSHClient | None = field(default=None, init=False, repr=False)
    _jump_client: paramiko.SSHClient | None = field(default=None, init=False, repr=False)
    _channel: paramiko.Channel | None = field(default=None, init=False, repr=False)

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """Establish the SSH connection and open an interactive shell."""
        params = self.params

        username = params.effective_username()

        try:
            sock = self._open_jump_channel() if params.jump_host else None

            self._client = paramiko.SSHClient()
            # AutoAddPolicy is deliberate: this is an interactive terminal
            # where the user is choosing what to connect to, and network gear
            # legitimately changes host keys after an RMA or an image upgrade.
            # Blocking on an unknown-host prompt would make the tool unusable.
            # A managed estate can tighten this under Stockton.
            self._client.set_missing_host_key_policy(_host_key_policy())

            logger.info("Connecting to %s as %s", params.target(), username)

            # Algorithm preferences, for the devices modern paramiko will not
            # otherwise talk to. Very old kit offers only key exchanges and
            # ciphers that have since been dropped from the defaults, which
            # makes it unreachable with no way to fix it from the interface.
            disabled = _algorithm_overrides()

            # Only go looking for keys when there is nothing better to try.
            #
            # paramiko offers public keys before it offers a password, and a
            # good deal of network kit cannot survive the rejection: a Cisco
            # SSH stack answers a failed publickey attempt by tearing the
            # connection down —
            #
            #     Authentication (publickey) failed.
            #     Disconnect (code 2): Protocol error: expected packet type 50, got 5
            #
            # so the password is never tried at all. The user sees
            # "authentication failed", types the password again more carefully,
            # and gets the same thing, because the password was never the
            # problem. Merely *having* an id_ed25519 in ~/.ssh made the device
            # unreachable.
            #
            # A password the user supplied is a statement of how they intend to
            # authenticate. An explicit key path is honoured either way — this
            # governs only the keys paramiko finds by itself.
            discover_keys = bool(advanced("ssh.look_for_keys"))
            if params.password and not params.private_key_path:
                discover_keys = False

            self._client.connect(
                hostname=params.hostname,
                port=params.port,
                username=username,
                sock=sock,
                timeout=advanced("ssh.connect_timeout"),
                allow_agent=False,
                look_for_keys=discover_keys,
                **disabled,
                **self._auth_kwargs(params.private_key_path, params.private_key_passphrase, params.password),
            )

            # Firewalls and jump hosts idle a session out mid-change, and the
            # first anyone knows is a dead prompt. Off by default because it is
            # traffic on a link that may be metered.
            keepalive = advanced("ssh.keepalive_seconds")
            transport = self._client.get_transport()
            if keepalive and transport is not None:
                transport.set_keepalive(int(keepalive))

            self._channel = self._client.invoke_shell(
                term="xterm-256color", width=80, height=24,
            )
            # A timeout (rather than non-blocking) lets recv() distinguish
            # "idle" from "closed". With setblocking(False), recv() returns
            # b"" immediately when idle, which is indistinguishable from the
            # channel having been closed.
            self._channel.settimeout(advanced("ssh.read_timeout"))

        except paramiko.AuthenticationException as exc:
            self.disconnect()
            raise ConnectionError_(
                f"Authentication failed for {username}@{params.hostname}. "
                f"Check the username, password or key."
            ) from exc
        except paramiko.SSHException as exc:
            self.disconnect()
            raise ConnectionError_(f"SSH error connecting to {params.target()}: {exc}") from exc
        except OSError as exc:
            self.disconnect()
            raise ConnectionError_(f"Could not reach {params.target()}: {exc}") from exc

        params.scrub_secrets()
        logger.info("SSH channel open to %s", params.target())

    @staticmethod
    def _auth_kwargs(key_path: str, passphrase: str, password: str) -> dict:
        """
        Build the credential arguments for paramiko's connect().

        A key takes precedence when supplied. The password is still passed
        alongside it, because network devices commonly accept a key for the
        SSH layer and then prompt for an enable/AAA password.
        """
        if key_path:
            return {"pkey": load_private_key(key_path, passphrase), "password": password or None}
        return {"password": password}

    def _open_jump_channel(self) -> paramiko.Channel:
        """
        Connect to the bastion and open a direct-tcpip channel to the target.

        This is what OpenSSH's ProxyJump does. The channel behaves like a
        socket, so paramiko runs a complete second SSH session through it —
        the target's traffic is encrypted end to end and the bastion cannot
        read it.
        """
        params = self.params
        logger.info("Opening jump host %s:%s", params.jump_host, params.jump_port)

        self._jump_client = paramiko.SSHClient()
        self._jump_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        try:
            self._jump_client.connect(
                hostname=params.jump_host,
                port=params.jump_port,
                username=params.jump_username or params.username,
                timeout=CONNECT_TIMEOUT,
                allow_agent=False,
                look_for_keys=False,
                **self._auth_kwargs(
                    params.jump_private_key_path,
                    params.jump_private_key_passphrase,
                    params.jump_password,
                ),
            )
        except paramiko.AuthenticationException as exc:
            raise ConnectionError_(
                f"Authentication failed on jump host {params.jump_host}."
            ) from exc
        except (paramiko.SSHException, OSError) as exc:
            raise ConnectionError_(f"Could not reach jump host {params.jump_host}: {exc}") from exc

        transport = self._jump_client.get_transport()
        if transport is None:
            raise ConnectionError_(f"Jump host {params.jump_host} closed the connection.")

        try:
            return transport.open_channel(
                "direct-tcpip",
                dest_addr=(params.hostname, params.port),
                src_addr=("127.0.0.1", 0),
            )
        except paramiko.SSHException as exc:
            raise ConnectionError_(
                f"Jump host {params.jump_host} refused to open a channel to "
                f"{params.target()}: {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # I/O
    # ------------------------------------------------------------------

    def send(self, data: bytes) -> None:
        """Write bytes to the remote shell."""
        if self._channel and not self._channel.closed:
            self._channel.send(data)

    def recv(self, size: int = 4096) -> bytes | None:
        """Read from the remote shell. See ConnectionHandler.recv for semantics."""
        if self._channel is None:
            return b""
        try:
            return self._channel.recv(size)
        except TimeoutError:
            # socket.timeout is an alias of TimeoutError from Python 3.10.
            # No data this window — closed only if the channel says so.
            return b"" if self._channel.closed else None
        except Exception:
            return b""

    def resize(self, cols: int, rows: int) -> None:
        """Send a window-size change so pagers and `terminal width` adapt."""
        if self._channel and not self._channel.closed:
            try:
                self._channel.resize_pty(width=cols, height=rows)
            except paramiko.SSHException:
                # A resize failing is never worth dropping the session over.
                pass

    # ------------------------------------------------------------------
    # Secondary channels
    # ------------------------------------------------------------------

    def open_secondary_channel(self) -> "paramiko.Channel | None":
        """
        Open an additional shell channel on the existing SSH transport.

        Used by background features (config snapshots, watch expressions) to
        run commands without touching what the user is typing. No second
        login is involved — SSH multiplexes channels over one connection.

        Returns None when the device refuses, which is common: plenty of
        switches cap concurrent sessions at one. Callers must have a fallback
        rather than treating a channel as guaranteed.
        """
        if self._client is None:
            return None
        transport = self._client.get_transport()
        if transport is None or not transport.is_active():
            return None

        try:
            channel = transport.open_session(timeout=CONNECT_TIMEOUT)
            channel.get_pty(term="vt100", width=200, height=1000)
            channel.invoke_shell()
            channel.settimeout(READ_TIMEOUT)
            return channel
        except paramiko.SSHException as exc:
            logger.info("Device refused a secondary channel: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Teardown
    # ------------------------------------------------------------------

    def disconnect(self) -> None:
        """Close the channel, the SSH transport, and the jump host if used."""
        for resource in (self._channel, self._client, self._jump_client):
            if resource is not None:
                try:
                    resource.close()
                except Exception:
                    pass

        self._channel = None
        self._client = None
        self._jump_client = None

    @property
    def is_connected(self) -> bool:
        """True if the channel is open and the transport is still active."""
        if self._channel is None or self._channel.closed or self._client is None:
            return False
        transport = self._client.get_transport()
        return transport is not None and transport.is_active()
