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

from backend.connections.base import (ConnectionError_, ConnectionHandler,
                                      ConnectionParams, HostKeyChanged,
                                      InteractiveRequired)

from backend import keys as keys_module

from backend.advanced import get as advanced

logger = logging.getLogger(__name__)

# How long to wait on the TCP + SSH handshake before giving up.

# recv() blocks for this long before reporting "nothing yet". Short enough that
# a closed channel is noticed promptly, long enough not to spin the CPU.

# Key types to try when the user supplies a key file. Paramiko needs to be told
# the algorithm, and the file itself does not reliably say which it is.
#
# Built by asking rather than by naming, because the set shrinks: paramiko
# 5.0 removed DSSKey, and a bare `paramiko.DSSKey` here stopped the whole
# application importing — not SSH, everything, because app.py reaches this
# module on the way up. A dead algorithm should cost a key type nobody can
# load, not the process.
KEY_CLASSES = tuple(
    cls for cls in (
        getattr(paramiko, name, None)
        for name in ("Ed25519Key", "ECDSAKey", "RSAKey", "DSSKey")
    ) if cls is not None
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


def _verify_host_key(hostname: str, port: int, key, trust_new: bool = False) -> str:
    """
    The one host-key decision, shared by every path that opens a transport
    (#528).

    Trust on first use, warn on change. An unknown host is remembered without
    asking — prompting for every one is unusable on a lab estate, and the
    prompt nobody can answer is the prompt everybody clicks through. A host
    whose key has *changed* stops the connection and becomes a question, with
    both fingerprints, because only the user can tell an RMA from a machine
    in the middle.

    ``ssh.host_key_policy`` still applies to a first sighting: a managed
    estate where every key is already known can refuse one that is not.

    Returns the fingerprint of the key that was accepted, for the interface
    to show.

    Raises:
        HostKeyChanged: The stored key for this host and algorithm is not the
            one presented, and the user has not said to trust the new one.
        ConnectionError_: The host is unknown and the policy is to reject.
    """
    if key is None or not hasattr(key, "get_name"):
        return ""                                # a transport with no key to check

    fingerprint = keys_module.host_fingerprint(key)
    name = keys_module.host_entry_name(hostname, port)

    # Compared per algorithm. A device that offered Ed25519 last week and RSA
    # today has not changed its key — it has offered a different one of the
    # keys it holds — and raising the alarm for that would teach people to
    # click through the warning that matters.
    stored = keys_module.known_host_key(hostname, port, key.get_name())
    if stored is None:
        stored = keys_module.system_host_key(hostname, port, key.get_name())

    if stored is not None:
        if stored == key:
            return fingerprint
        if not trust_new:
            raise HostKeyChanged(hostname, port, key.get_name(),
                                 keys_module.host_fingerprint(stored), fingerprint)
        # Logged at warning level, always. Trusting a changed key is the one
        # decision here that somebody may have to account for afterwards, and
        # the log is where they will look.
        logger.warning("The %s host key for %s changed from %s to %s, and was "
                       "trusted at the user's explicit request",
                       key.get_name(), name,
                       keys_module.host_fingerprint(stored), fingerprint)
        keys_module.remember_host(hostname, port, key)
        return fingerprint

    choice = str(advanced("ssh.host_key_policy"))
    if choice == "reject" and not trust_new:
        raise ConnectionError_(
            f"{hostname} presented a host key that is not known ({fingerprint}), "
            "and the host-key policy is set to reject unknown keys.")
    if choice == "warn":
        logger.warning("%s presented an unknown host key: %s", name, fingerprint)

    keys_module.remember_host(hostname, port, key)
    return fingerprint


class _TrustOnFirstUse:
    """
    ShellMate's answer to paramiko's "I have never seen this host".

    paramiko only calls a missing-host-key policy from
    ``SSHClient.connect()``, and only with the name it built itself — which
    is why this carries the address and port it was created for rather than
    re-deriving them from that name. Everything it does is
    :func:`_verify_host_key`, so the password path and the two hand-driven
    ones cannot drift apart.
    """

    def __init__(self, hostname: str, port: int, trust_new: bool = False) -> None:
        self.hostname = hostname
        self.port = port
        self.trust_new = trust_new
        self.fingerprint = ""

    def missing_host_key(self, client, hostname, key) -> None:
        self.fingerprint = _verify_host_key(
            self.hostname, self.port, key, self.trust_new)


def _check_host_key(transport, hostname: str, port: int,
                    trust_new: bool = False) -> str:
    """
    Apply the host-key check to a transport we drove ourselves (#472, #528).

    `SSHClient.connect()` is the only place paramiko compares the server's
    key with known_hosts and consults the policy. The no-credential and
    keyboard-interactive paths build a Transport by hand and never call
    connect(), so `ssh.host_key_policy = reject` protected the password
    path and silently not these two. Same comparison, same store, same
    warning on a changed key, before any authentication.
    """
    try:
        key = transport.get_remote_server_key()
    except Exception:
        key = None
    return _verify_host_key(hostname, port, key, trust_new)


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


class _JumpView:
    """
    The jump host's fields, shaped like a connection's own.

    So `_explain_auth_failure()` can say the same useful things about a
    bastion that it says about a device — which method was refused, whether
    the far end hung up, whether there was anything to fall back to. A
    failure on the way *through* is otherwise the least diagnosable one in
    the application: the address in the message is not the address the user
    typed.
    """

    def __init__(self, params):
        self.hostname = params.jump_host
        self.password = params.jump_password
        self.private_key_path = params.jump_private_key_path
        self.credential_source = getattr(params, "credential_source", "")


def _explain_auth_failure(exc: Exception, params, username: str,
                          offered_keys: bool, still_connected: bool) -> str:
    """
    Say what actually happened, rather than listing what might have.

    "Authentication failed. Check the username, password or key" is three
    suggestions and no diagnosis, and it is actively wrong in the case that
    turned out to be common: paramiko offers public keys before passwords, and
    a good deal of network kit answers a rejected key by closing the
    connection — so the password is never tried, and the message sends someone
    to retype a password that was correct.

    Everything below is read from the exception paramiko raised and the state
    of the transport at the moment it did. Nothing is guessed.
    """
    where = f"{username}@{params.hostname}"

    # The server saying what it *would* have accepted. This is the most useful
    # thing available anywhere in the failure and it was being discarded.
    allowed = [str(a) for a in getattr(exc, "allowed_types", []) or []]
    if allowed:
        readable = ", ".join(_METHOD_NAMES.get(a, a) for a in allowed)
        return (f"{params.hostname} refused the way ShellMate tried to "
                f"authenticate as {username}. It will only accept: {readable}.")

    used_key = bool(params.private_key_path)
    used_password = bool(params.password)

    # A device that hung up rather than rejecting a credential. The tell is
    # that the transport is gone by the time the exception surfaces.
    if not still_connected and (used_key or offered_keys) and used_password:
        return (
            f"{params.hostname} closed the connection after refusing a key, "
            f"so the password for {username} was never tried. Some platforms "
            f"do this instead of falling back. Turn off “Try keys in "
            f"~/.ssh” in Stockton, or name the key this device expects."
        )

    if used_key and not used_password:
        which = Path(params.private_key_path).name
        return (f"{params.hostname} refused the key {which} for {username}, "
                f"and no password was given to fall back to.")

    if used_password:
        source = {"saved": "the saved password",
                  "typed": "the password you typed"}.get(
                      getattr(params, "credential_source", ""), "the password")
        return (f"{params.hostname} refused {source} for {username}."
                + (" The username may be wrong rather than the password."
                   if not used_key else ""))

    if offered_keys:
        return (f"{params.hostname} refused every key in ~/.ssh for "
                f"{username}, and no password was given.")

    return (f"{params.hostname} would not authenticate {username}, and "
            f"ShellMate had no password or key to offer it.")


#: SSH method names as a person would say them.
_METHOD_NAMES = {
    "password":            "a password",
    "publickey":           "a key",
    "keyboard-interactive": "keyboard-interactive (a prompted challenge)",
    "gssapi-with-mic":     "Kerberos",
    "none":                "no authentication at all",
}


@dataclass
class SSHHandler(ConnectionHandler):
    """Manages a single SSH connection and its interactive shell."""

    _client: paramiko.SSHClient | None = field(default=None, init=False, repr=False)
    _jump_client: paramiko.SSHClient | None = field(default=None, init=False, repr=False)
    _channel: paramiko.Channel | None = field(default=None, init=False, repr=False)

    #: The SHA-256 fingerprint of the key this device proved itself with
    #: (#528), for the tab's hover card. Empty when the system known_hosts
    #: already held it, since paramiko then matches it without telling us
    #: which key it matched. Not a secret — a host key's public half is
    #: what every client on the network sees.
    host_key_fingerprint: str = field(default="", init=False)

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    @staticmethod
    def _wants_interactive_login(params) -> bool:
        """
        Whether the user asked to log in at the device rather than here.

        Expressed by leaving everything blank: no password, no key, no
        credential reference. Anything supplied is a statement of how they
        intend to authenticate, and is honoured.
        """
        return not any((params.password, params.private_key_path,
                        getattr(params, "credential_ref", "")))

    def _connect_interactive(self, params, username, sock, disabled) -> bool:
        """
        Open a shell with no authentication, if the device permits it.

        Returns True when a shell is open. False means the device wants
        credentials after all, and the caller falls through to the ordinary
        path so the failure is reported the way every other failure is.

        Serial and telnet have always worked like this — no credential in the
        dialog, login happening in the terminal — so this is SSH being allowed
        to do what the other two transports already do.
        """
        import socket as _socket

        try:
            raw = sock or _socket.create_connection(
                (params.hostname, params.port), advanced("ssh.connect_timeout"))
            transport = paramiko.Transport(raw, disabled_algorithms=(
                disabled or {}).get("disabled_algorithms"))
            transport.start_client(timeout=advanced("ssh.banner_timeout"))
            self.host_key_fingerprint = _check_host_key(
                transport, params.hostname, params.port, params.trust_new_host_key)

            # auth_none returns the methods still required. An empty list is
            # the device saying "come in".
            remaining = transport.auth_none(username)
            if remaining:
                logger.info(
                    "%s will not accept an unauthenticated login (wants %s)",
                    params.target(), ", ".join(remaining))
                transport.close()
                return False

            # Handed to the SSHClient rather than held separately, so every
            # existing path works unchanged: is_connected reads the transport
            # off the client, disconnect() closes the client, and
            # open_secondary_channel() asks the client for it. A detached
            # transport would have reported the session dead the instant it
            # opened.
            self._client._transport = transport

            keepalive = advanced("ssh.keepalive_seconds")
            if keepalive:
                transport.set_keepalive(int(keepalive))

            self._channel = transport.open_session()
            self._channel.get_pty(term="xterm-256color", width=80, height=24)
            self._channel.invoke_shell()
            self._channel.settimeout(advanced("ssh.read_timeout"))
            params.scrub_secrets()
            logger.info("Connected to %s with no authentication; the device "
                        "will do its own asking", params.target())
            return True
        except HostKeyChanged:
            # Never a reason to fall back and try again. The device is not
            # the one we trusted, and the next path would open a second
            # connection to it before anybody had been asked (#528).
            try:
                transport.close()
            except Exception:
                pass
            raise
        except Exception as exc:
            logger.info("Unauthenticated login to %s did not work (%s); "
                        "falling back", params.target(), exc)
            try:
                transport.close()
            except Exception:
                pass
            return False

    def _interactive_login(self, params, username, sock, disabled) -> None:
        """
        Authenticate by keyboard-interactive, asking the user what the
        device asks (#406).

        Runs the challenge with a handler that answers a password prompt
        from the password and any other prompt from ``interactive_answers``
        in order. A prompt with no answer available is answered blank —
        the device refuses, the attempt ends — and the prompts seen are
        raised as :class:`InteractiveRequired` so the interface can ask.
        The next attempt arrives with the answers and goes straight here.

        A one-time code is consumed by the attempt that carries it, which
        is why the ordinary password-first connect is bypassed when answers
        are present: paramiko's own fallback would spend the code on a
        prompt it answers with the password.
        """
        import socket as _socket

        answers = [str(a) for a in (params.interactive_answers or [])]
        password = params.password or ""
        seen: list[dict] = []
        unanswered: list[str] = []
        state = {"title": "", "instructions": ""}

        def handler(title, instructions, prompts):
            state["title"] = title or ""
            state["instructions"] = instructions or ""
            responses = []
            for text, echo in prompts:
                seen.append({"text": str(text), "echo": bool(echo)})
                lowered = str(text).lower()
                if "password" in lowered and password:
                    responses.append(password)
                elif answers:
                    responses.append(answers.pop(0))
                else:
                    unanswered.append(str(text))
                    responses.append("")
            return responses

        transport = None
        try:
            raw = sock or _socket.create_connection(
                (params.hostname, params.port), advanced("ssh.connect_timeout"))
            transport = paramiko.Transport(raw, disabled_algorithms=(
                disabled or {}).get("disabled_algorithms"))
            transport.start_client(timeout=advanced("ssh.banner_timeout"))
            self.host_key_fingerprint = _check_host_key(
                transport, params.hostname, params.port, params.trust_new_host_key)
            transport.auth_interactive(username, handler)
        except paramiko.AuthenticationException as exc:
            if transport is not None:
                try:
                    transport.close()
                except Exception:
                    pass
            if unanswered:
                raise InteractiveRequired(state["title"], state["instructions"], seen) from exc
            raise ConnectionError_(
                f"{params.hostname} refused the answers given for {username}."
                + (" Check the code and try again." if params.interactive_answers else "")
            ) from exc
        except HostKeyChanged:
            # Already phrased as a question for the user; close the transport
            # and let it travel. Without this clause the `SSHException` one
            # below would not catch it either, and the socket would leak.
            if transport is not None:
                try:
                    transport.close()
                except Exception:
                    pass
            raise
        except (paramiko.SSHException, OSError) as exc:
            if transport is not None:
                try:
                    transport.close()
                except Exception:
                    pass
            raise ConnectionError_(f"SSH error connecting to {params.target()}: {exc}") from exc

        # Authenticated. Hand the transport to an SSHClient so every existing
        # path — is_connected, disconnect, the second channel — works as it
        # does for a password login.
        self._client = paramiko.SSHClient()
        self._client.set_missing_host_key_policy(
            _TrustOnFirstUse(params.hostname, params.port, params.trust_new_host_key))
        self._client._transport = transport
        keepalive = advanced("ssh.keepalive_seconds")
        if keepalive:
            transport.set_keepalive(int(keepalive))
        self._channel = transport.open_session()
        self._channel.get_pty(term="xterm-256color", width=80, height=24)
        self._channel.invoke_shell()
        self._channel.settimeout(advanced("ssh.read_timeout"))
        params.scrub_secrets()
        logger.info("Connected to %s by keyboard-interactive", params.target())

    def connect(self) -> None:
        """Establish the SSH connection and open an interactive shell."""
        params = self.params

        username = params.effective_username()

        try:
            sock = self._open_jump_channel() if params.jump_host else None

            self._client = paramiko.SSHClient()
            if not params.trust_new_host_key:
                try:
                    # Without this, nothing is ever "known" and a reject policy
                    # refuses every device (#472). Skipped only when the user
                    # has just said to trust a new key: paramiko compares
                    # against this store itself and raises before our own
                    # policy is ever consulted, so leaving it loaded would make
                    # their answer unanswerable (#528).
                    self._client.load_system_host_keys()
                except Exception:
                    pass
            # Trust on first use, warn on change (#528). This is an
            # interactive terminal where the user chooses what to connect to,
            # and network gear legitimately changes host keys after an RMA or
            # an image upgrade — so an unknown key is remembered rather than
            # blocked on, and it is the *change* that becomes a question. A
            # managed estate can refuse unknown keys outright under Stockton.
            policy = _TrustOnFirstUse(params.hostname, params.port,
                                      params.trust_new_host_key)
            self._client.set_missing_host_key_policy(policy)

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

            # No credentials at all: let the device do the asking (#132).
            #
            # Console and terminal servers commonly accept SSH with no
            # authentication and then present their own `Username:` prompt in
            # the shell — which is the login that matters. paramiko's
            # SSHClient.connect() cannot express that, since it always
            # authenticates, so this path drives the transport directly.
            #
            # **Nothing is invented.** No agent key, no discovered key, no
            # remembered password. Somebody who asked for an interactive login
            # and got three failed attempts against a TACACS policy without
            # typing anything would have every right to be furious.
            if self._wants_interactive_login(params):
                if self._connect_interactive(params, username, sock, disabled):
                    return
                # The probe wrapped the bastion's channel in its own transport,
                # and closing that transport closed the channel with it (#342)
                # — so the fallback below dialled through a dead sock and every
                # interactive-login connection via a jump host failed with
                # "Socket is closed" instead of falling back. Open a fresh
                # channel; the bastion session itself is still up.
                if sock is not None:
                    sock = self._open_jump_channel()

            # Answers to an earlier challenge (#406): the device asked for
            # something beyond a password last time, and the user has typed
            # it. Go straight to the keyboard-interactive exchange rather
            # than through paramiko's password-first sequence, which would
            # burn the one-time code on a prompt it answers with the password.
            if params.interactive_answers:
                try:
                    self._interactive_login(params, username, sock, disabled)
                except BaseException:
                    # InteractiveRequired is not a ConnectionError_, so the
                    # cleanup below never saw it and the bastion session
                    # opened for this attempt stayed open (#473).
                    self.disconnect()
                    raise
                return

            self._client.connect(
                hostname=params.hostname,
                port=params.port,
                username=username,
                sock=sock,
                timeout=advanced("ssh.connect_timeout"),
                # Distinct phases of the handshake, and each has its own way
                # of failing: a terminal server slow to print its banner, a
                # device waiting on a distant RADIUS server.
                banner_timeout=advanced("ssh.banner_timeout"),
                auth_timeout=advanced("ssh.auth_timeout"),
                allow_agent=bool(advanced("ssh.allow_agent")),
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
            # Empty when the system known_hosts already knew this device, in
            # which case paramiko matched it and never called the policy.
            self.host_key_fingerprint = policy.fingerprint or self.host_key_fingerprint

        except HostKeyChanged:
            # A question for the user, not a failure — but the bastion session
            # opened for this attempt still has to be closed, or every refused
            # answer leaves one behind (#341, #473).
            self.disconnect()
            raise
        except paramiko.BadHostKeyException as exc:
            # paramiko's own comparison, against the *system* known_hosts,
            # which it consults before the policy. Same event, same question:
            # translated rather than reported as a generic SSH error, which is
            # what the `except paramiko.SSHException` below would have made of
            # it (BadHostKeyException is one).
            self.disconnect()
            raise HostKeyChanged(
                params.hostname, params.port,
                exc.key.get_name() if exc.key is not None else "",
                keys_module.host_fingerprint(exc.expected_key),
                keys_module.host_fingerprint(exc.key)) from exc
        except ConnectionError_:
            # Raised with the failure already phrased — jump auth refused, the
            # bastion declining a channel, a bad key path — but possibly
            # *after* the bastion login succeeded. Without this cleanup each
            # failed attempt left an authenticated session open on the jump
            # host for the life of the process (#341): five retries against a
            # bastion that caps per-user sessions locked the user out of the
            # door itself.
            self.disconnect()
            raise
        except paramiko.AuthenticationException as exc:
            transport = self._client.get_transport()
            still_up = bool(transport and transport.is_active())
            allowed = [str(a) for a in getattr(exc, "allowed_types", []) or []]
            self.disconnect()
            # The device offers keyboard-interactive: find out what it wants
            # to ask (#406). A prompt the password cannot answer comes back
            # as InteractiveRequired for the interface to put to the user.
            if "keyboard-interactive" in allowed:
                sock2 = self._open_jump_channel() if params.jump_host else None
                try:
                    self._interactive_login(params, username, sock2, disabled)
                except BaseException:
                    # Raised inside an except clause, so the sibling
                    # `except ConnectionError_` does not run; close the
                    # bastion session ourselves (#473, the #341 lockout).
                    self.disconnect()
                    raise
                return
            raise ConnectionError_(
                _explain_auth_failure(exc, params, username,
                                      offered_keys=discover_keys,
                                      still_connected=still_up)
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
        if not params.trust_new_host_key:
            try:
                self._jump_client.load_system_host_keys()  # see #472, #528
            except Exception:
                pass
        # The same settings as the target connection, rather than three
        # hardcoded values.
        #
        # `ssh.host_key_policy` is the one that matters. Its whole purpose is
        # letting a managed estate refuse an unknown host key — and the
        # bastion is the machine most worth refusing, because it is the one
        # every session goes through and the one an attacker would rather be.
        # Hardcoding AutoAddPolicy here meant the setting protected the switch
        # and not the door to it.
        #
        # The timeout came from a module constant the target stopped using,
        # and the algorithm overrides exist for kit modern paramiko will not
        # otherwise negotiate with — a bastion is as likely to be old as
        # anything behind it.
        self._jump_client.set_missing_host_key_policy(
            _TrustOnFirstUse(params.jump_host, params.jump_port,
                             params.trust_new_host_key))

        discover_keys = bool(advanced("ssh.look_for_keys"))
        if params.jump_password and not params.jump_private_key_path:
            discover_keys = False

        try:
            self._jump_client.connect(
                hostname=params.jump_host,
                port=params.jump_port,
                username=params.jump_username or params.username,
                timeout=advanced("ssh.connect_timeout"),
                banner_timeout=advanced("ssh.banner_timeout"),
                auth_timeout=advanced("ssh.auth_timeout"),
                allow_agent=bool(advanced("ssh.allow_agent")),
                look_for_keys=discover_keys,
                **_algorithm_overrides(),
                **self._auth_kwargs(
                    params.jump_private_key_path,
                    params.jump_private_key_passphrase,
                    params.jump_password,
                ),
            )
        except paramiko.AuthenticationException as exc:
            transport = self._jump_client.get_transport()
            raise ConnectionError_(
                "Jump host: " + _explain_auth_failure(
                    exc, _JumpView(params), params.jump_username or params.username,
                    offered_keys=discover_keys,
                    still_connected=bool(transport and transport.is_active()))
            ) from exc
        except paramiko.BadHostKeyException as exc:
            # The bastion is the machine most worth checking: every session
            # goes through it, and it is the one an attacker would rather be.
            raise HostKeyChanged(
                params.jump_host, params.jump_port,
                exc.key.get_name() if exc.key is not None else "",
                keys_module.host_fingerprint(exc.expected_key),
                keys_module.host_fingerprint(exc.key)) from exc
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
        """
        Write bytes to the remote shell — all of them.

        `Channel.send` is a partial write: it stops at the remote window or
        the packet size and returns how much went, and the rest was silently
        lost (#470) — a pasted block, a config push, a broadcast, each
        losing its tail with no error on a tool whose rule is never to
        send silently. `sendall` loops until it is all through. A device
        that has stopped reading — sitting at --More--, mid-reload — makes
        it time out instead; that is raised with a plain message, and the
        caller decides what to do with one keystroke that did not go.
        """
        if not self._channel or self._channel.closed:
            return
        try:
            self._channel.sendall(data)
        except _socket.timeout as exc:
            logger.warning("%s is not accepting input; %d byte(s) were not sent",
                           self.params.target() if self.params else "the device", len(data))
            raise ConnectionError_(
                "The device is not accepting input right now (its window is full — "
                "is it waiting at --More-- or mid-reload?)") from exc

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
            channel = transport.open_session(timeout=advanced("ssh.connect_timeout"))
            # Wide enough that the device does not wrap the configuration
            # itself — a wrapped capture diffs against an unwrapped one and
            # reports the whole file as changed.
            channel.get_pty(term="vt100",
                            width=int(advanced("capture.channel_width")),
                            height=int(advanced("capture.channel_height")))
            channel.invoke_shell()
            channel.settimeout(advanced("ssh.secondary_channel_timeout"))
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
