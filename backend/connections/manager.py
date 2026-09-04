"""
connections/manager.py — Session lifecycle manager.

SessionManager is the single source of truth for all active terminal
sessions.  It maintains a dictionary keyed by UUID session_id, creates new
sessions over whichever transport was requested, and tears them down cleanly.

Every other part of the backend (WebSocket handlers, AI router, REST
endpoints) goes through SessionManager — they never touch a handler or a
SessionBuffer directly.

Transports are looked up in a registry rather than branched on with if/elif,
so adding one means adding a subclass and a single line here.
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from backend.alerts import AlertTracker, WatchTracker
from backend.connections.base import ConnectionError_, ConnectionHandler, ConnectionParams
from backend.connections.serial_handler import SerialHandler
from backend.connections.ssh_handler import SSHHandler
from backend.connections.telnet_handler import TelnetHandler
from backend.onboard import Onboarder
from backend.pipeline import OutboundPipeline
from backend.session.buffer import SessionBuffer
from backend.session.transcript import TranscriptParser
from backend.store import store

from backend.advanced import get as advanced

logger = logging.getLogger(__name__)

# Transport registry. The keys are what the frontend sends as connection_type.
HANDLERS: dict[str, type[ConnectionHandler]] = {
    "ssh":    SSHHandler,
    "serial": SerialHandler,
    "telnet": TelnetHandler,
}


class SessionManager:
    """Manages the full lifecycle of all terminal sessions."""

    def __init__(self) -> None:
        # Primary store: session_id -> session dict
        self._sessions: dict[str, dict[str, Any]] = {}

        # "host:port" -> hostname the device reported.
        #
        # Exists to remove an ordering race. A device announces its name
        # within milliseconds of connecting, but the frontend saves the
        # connection profile a moment later — so the name is usually known
        # *before* there is a profile to write it to, and the write is lost.
        # Recording it here means profile creation can look it up whichever
        # way round the two happen.
        self.detected_hostnames: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Session creation
    # ------------------------------------------------------------------

    def create_session(self, params: ConnectionParams,
                       profile_id: str = "") -> dict[str, Any]:
        """
        Create a session, connect to the device, and store it.

        Blocking — the caller runs this in a worker thread so the event loop
        is not stalled during the handshake.

        Args:
            params: Connection details. Credentials are scrubbed by the
                    handler once the connection is established.
            profile_id: The saved connection this was opened from, if any.
                    **This is the session's identity as far as the interface
                    is concerned.** Without it, everything downstream had to
                    guess from the address, and address is not an identity:
                    an estate behind one jump host, a lab of containers on one
                    address, or two profiles for one switch with different
                    credentials all collide. One open session then lit the
                    indicator on five thousand connections, labelled tabs with
                    the wrong group, and made every device after the first
                    unopenable because clicking it switched to the tab already
                    there (#187, #190, #192, #193).

        Returns:
            Public session metadata (no handler, no credentials).

        Raises:
            ConnectionError_: The connection failed, with a message suitable
                for showing to the user.
        """
        handler_class = HANDLERS.get(params.connection_type)
        if handler_class is None:
            supported = ", ".join(sorted(HANDLERS))
            raise ConnectionError_(
                f"Unknown connection type '{params.connection_type}'. "
                f"Supported types are: {supported}."
            )

        session_id = str(uuid.uuid4())
        handler = handler_class(params=params)
        handler.connect()  # Raises ConnectionError_ on failure

        session: dict[str, Any] = {
            "session_id":      session_id,
            "handler":         handler,
            "buffer":          SessionBuffer(
                session_id, max_lines=advanced("history.buffer_lines")),
            # Reconstructs commands and their output from the raw stream. Lives
            # alongside the buffer rather than replacing it: the buffer paints
            # the screen, the transcript answers questions about it.
            "transcript":      TranscriptParser(),
            # Identifies the device and applies its platform settings during
            # the first seconds of the session.
            "onboarder":       Onboarder(),
            # A configuration capture running through this session's own
            # channel, when the device refused a second one. Declared here and
            # not created on demand: the read loop tests it on every pass, and
            # a key that sometimes exists is a KeyError waiting for the one
            # device that behaves differently.
            "live_capture":    None,
            # A pasted block being sent a line at a time (#523). Declared for
            # the reason live_capture is: the read loop tests it on every
            # pass, and a key that sometimes exists is a KeyError waiting for
            # the one session that behaves differently.
            "paste_batch":     None,
            # Per-tab keep-alive (#138). None means "follow the setting"; a
            # bool is this tab's own answer. Declared rather than created on
            # demand, for the same reason live_capture is.
            "keep_alive":      None,
            "fingerprint":     None,
            # Chokepoint for everything the user sends: alias expansion and
            # the dangerous-command guardrail.
            "pipeline":        OutboundPipeline(),
            # Tracks anything scheduled on the device that has not happened
            # yet — a pending reload, a commit waiting to be confirmed.
            "alerts":          AlertTracker(),
            # The user's own colour rules, the ones marked "alert", matched
            # against this session's output (#521). Created empty; the read
            # loop loads the rules on the first chunk and again whenever
            # settings are saved, the way session logging is signalled.
            "watch":           WatchTracker(),
            "params":          params,
            # Which saved connection this came from. Empty for a session
            # opened straight from the dialog, which genuinely has no profile
            # — callers fall back to the address match for those, and only
            # for those.
            "profile_id":      profile_id or "",
            # What the device calls itself. Overwritten with the detected
            # hostname once it says — deliberately, because config snapshots
            # are keyed by it and the same device should file under one name
            # however it was reached.
            "hostname":        params.hostname or params.serial_port,
            # What ShellMate dialled, and never rewritten.
            #
            # These were one field, which meant anything reading it after the
            # device announced itself got a *name* where it wanted an address:
            # a manually set platform was filed against a profile that does
            # not exist, and Duplicate prefilled the dialog with "S3-R1"
            # instead of 192.168.20.16 and could not connect.
            "address":         params.hostname or params.serial_port,
            "port":            params.port,
            "username":        params.username,
            "connection_type": params.connection_type,
            "display_label":   params.label(),
            "target":          params.target(),
            "connected_at":    datetime.now(timezone.utc).isoformat(),
            "is_connected":    True,
        }

        self._sessions[session_id] = session
        logger.info("Session created: %s (%s %s)", session_id, params.connection_type, params.target())

        # When this saved connection was last opened (#536). Best effort: a
        # profile file that will not write must never stop somebody reaching
        # a device.
        try:
            from backend.profiles import record_connected
            record_connected(session["address"], params.port, params.username or "",
                             profile_id=profile_id or "")
        except Exception as exc:                          # pragma: no cover
            logger.debug("Could not record the connection time: %s", exc)

        # Every session is recorded, with nothing to switch on. Failing to
        # record must never stop someone connecting to a device.
        try:
            store.start_session(session_id, session)
        except Exception as exc:
            logger.warning("Could not start history for session %s: %s", session_id, exc)

        return self._public_view(session)

    # ------------------------------------------------------------------
    # Session retrieval
    # ------------------------------------------------------------------

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        """Return the full internal session dict, or None if not found."""
        return self._sessions.get(session_id)

    def get_all_sessions(self) -> list[dict[str, Any]]:
        """
        Return public metadata for all sessions.

        Used by GET /api/sessions so the frontend can rebuild the tab bar
        after a page refresh.
        """
        # A snapshot first (#475): create_session and destroy_session run on
        # worker threads, and iterating the live dict while one of them
        # inserts or pops raised "dictionary changed size during iteration"
        # into the sessions list, quit and the update blockers.
        return [self._public_view(s) for s in list(self._sessions.values())]

    # ------------------------------------------------------------------
    # Session destruction
    # ------------------------------------------------------------------

    def destroy_session(self, session_id: str) -> None:
        """
        Disconnect and remove a session entirely.

        Safe to call even if the session is already disconnected.
        """
        session = self._sessions.pop(session_id, None)
        if session is None:
            logger.warning("destroy_session called for unknown id: %s", session_id)
            return

        # Port forwards are listeners on this machine; they must not outlive
        # the transport they tunnel through (#405).
        try:
            from backend.connections import forwards as forwards_module
            forwards_module.close_for(session)
        except Exception as exc:
            logger.debug("Closing forwards for %s: %s", session_id, exc)

        # A session usually ends mid-command — the tab is closed while output
        # is still arriving. Flush the parser so that last command is kept
        # rather than dropped, since it is often the interesting one.
        try:
            final = session["transcript"].flush()
            if final is not None:
                store.add_command(session_id, final)
            store.end_session(session_id)
        except Exception as exc:
            logger.warning("Could not close history for session %s: %s", session_id, exc)

        # Close the SFTP channel first if one was opened, so it is not left
        # dangling on a transport that is about to go away.
        from backend.connections.sftp import close_sftp
        close_sftp(session)

        try:
            session["handler"].disconnect()
        except Exception as exc:
            logger.warning("Error disconnecting session %s: %s", session_id, exc)

        session["buffer"].clear()
        logger.info("Session destroyed: %s", session_id)

    # ------------------------------------------------------------------
    # Buffer helpers
    # ------------------------------------------------------------------

    def write_to_buffer(self, session_id: str, data: str) -> None:
        """Append terminal output to the session's buffer."""
        session = self._sessions.get(session_id)
        if session:
            session["buffer"].write(data)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _public_view(session: dict[str, Any]) -> dict[str, Any]:
        """
        Return the serialisable subset of a session.

        Excludes the handler, the SessionBuffer and the ConnectionParams —
        the last of these because it is the structure credentials pass
        through, and it must never be reachable from an API response.
        """
        return {
            "session_id":      session["session_id"],
            # Safe to return: an id, not a credential. It is what lets the
            # interface say "this tab is that connection" without inferring it
            # from the address.
            "profile_id":      session.get("profile_id", ""),
            "hostname":        session["hostname"],
            # Both, because they answer different questions and callers have
            # been getting the wrong one.
            "address":         session.get("address", session["hostname"]),
            "port":            session["port"],
            "username":        session["username"],
            "connection_type": session["connection_type"],
            "display_label":   session["display_label"],
            "target":          session["target"],
            "connected_at":    session["connected_at"],
            # What the device proved itself with (#528). A public key's
            # fingerprint, so there is nothing here to protect — and it is
            # the thing somebody reads out to whoever is standing next to
            # the device. Empty for serial, telnet, and for an SSH host the
            # system known_hosts already matched.
            "host_key":        getattr(session.get("handler"),
                                       "host_key_fingerprint", ""),
            "is_connected":    SessionManager._still_connected(session),
        }

    @staticmethod
    def _still_connected(session: dict[str, Any]) -> bool:
        """
        Whether this session is open, asking the transport as well as the flag.

        `session["is_connected"]` is written by the WebSocket read loop when
        `recv()` returns b"" — which only happens once something tries to
        read. A transport that has gone away between reads leaves the flag
        saying True, and anything polling /api/sessions believes it: the
        device and its group went on showing a green light after the session
        had died (#203).

        Both, and conjoined: the flag can say False for a session deliberately
        closed, and the handler can say False for one that dropped. Either is
        enough to be disconnected; it takes both agreeing to be connected.
        """
        if not session.get("is_connected"):
            return False
        handler = session.get("handler")
        if handler is None:
            return False
        try:
            return bool(handler.is_connected)
        except Exception:
            # Asking cost us an exception, which is itself an answer — but not
            # one worth taking a session down over. The read loop will settle
            # it definitively the moment anything arrives.
            return True
