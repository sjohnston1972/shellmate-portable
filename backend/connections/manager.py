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

from backend.alerts import AlertTracker
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

    def create_session(self, params: ConnectionParams) -> dict[str, Any]:
        """
        Create a session, connect to the device, and store it.

        Blocking — the caller runs this in a worker thread so the event loop
        is not stalled during the handshake.

        Args:
            params: Connection details. Credentials are scrubbed by the
                    handler once the connection is established.

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
            "fingerprint":     None,
            # Chokepoint for everything the user sends. Alias expansion today,
            # guardrails and paste throttling next.
            "pipeline":        OutboundPipeline(),
            # Tracks anything scheduled on the device that has not happened
            # yet — a pending reload, a commit waiting to be confirmed.
            "alerts":          AlertTracker(),
            "params":          params,
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
        return [self._public_view(s) for s in self._sessions.values()]

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
            "is_connected":    session["is_connected"],
        }
