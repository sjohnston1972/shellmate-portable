"""
app.py — FastAPI application for ShellMate.

Defines all HTTP REST endpoints and WebSocket handlers.  A single global
SessionManager instance tracks every active terminal session.  The frontend
is served as static files from the frontend/ directory.

WebSocket /ws/terminal/{session_id}:
  - Receives JSON from the browser: {type:"input", data:"..."} or
    {type:"resize", cols:N, rows:N}
  - Sends JSON to the browser: {type:"output", data:"..."} or
    {type:"hostname_detected", hostname:"..."}

REST endpoints:
  POST   /api/sessions          — create session, return session_id
  GET    /api/sessions          — list all sessions
  DELETE /api/sessions/{id}     — tear down a session
"""

import asyncio
import json
import logging
import re
from pathlib import Path

from fastapi import (
    FastAPI, File, HTTPException, Request, Response, UploadFile, WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from backend import desktop, paths
from backend.configs import capture_config, diff_snapshots, drift_report
from backend.connections.base import ConnectionError_, ConnectionParams
from backend.connections import sftp
from backend.connections.manager import SessionManager
from backend.connections.serial_handler import available_ports
from backend.profiles import (
    CREDENTIAL_FIELDS, delete_profile, forget_credentials, get_profiles,
    load_credentials, record_detected_hostname, save_credentials,
    save_plaintext_credentials, save_profile,
)
from backend import platforms as platforms_module
from backend import snippets
from backend.session.redact import redact
from backend.session.transcript import detect_hostname
from backend.settings_store import (
    get_settings, get_settings_for_ui, log_directory, migrate_plaintext_secrets,
    update_settings,
)
from backend.store import store
from backend.vault import VaultError, vault
from backend.ai.router import stream_chat
from backend.ai import chroma_client, providers
from backend.config import DEFAULT_AI_BACKEND, JIRA_URL, JIRA_USER_EMAIL, JIRA_API_TOKEN, JIRA_PROJECT_KEY

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Application and globals
# ---------------------------------------------------------------------------

app = FastAPI(title="ShellMate Portable")

# Single global session manager — all state lives here
session_manager = SessionManager()

# Absolute path to the frontend directory. Read-only and, in a frozen build,
# inside PyInstaller's temporary extraction directory — never write here.
FRONTEND_DIR = paths.frontend_dir()

# ---------------------------------------------------------------------------
# CORS — allow the browser to call the API from the loopback origin it was
# served from.
#
# The port is not known at import time because ShellMate picks a free one at
# startup (see backend/server.py), so match loopback origins by regex rather
# than listing them. The previous literal "http://localhost:*" entry was not
# valid CORS syntax — origins are compared as exact strings, so it never
# matched anything.
# ---------------------------------------------------------------------------

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^http://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Static file serving
# ---------------------------------------------------------------------------

# Serve everything under frontend/ at /static (css, js, etc.)
app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


@app.get("/")
async def serve_index() -> FileResponse:
    """Serve the main frontend page."""
    return FileResponse(str(FRONTEND_DIR / "index.html"))


# ---------------------------------------------------------------------------
# REST — Session management
# ---------------------------------------------------------------------------


class CreateSessionRequest(BaseModel):
    """Body for POST /api/sessions."""

    hostname: str
    port: int = 22
    username: str = ""
    password: str = ""
    connection_type: str = "ssh"
    display_label: str = ""

    # SSH key authentication
    private_key_path: str = ""
    private_key_passphrase: str = ""
    private_key_username: str = ""

    # SSH jump host / bastion
    jump_host: str = ""
    jump_port: int = 22
    jump_username: str = ""
    jump_password: str = ""
    jump_private_key_path: str = ""
    jump_private_key_passphrase: str = ""

    # Serial console
    serial_port: str = ""
    baud_rate: int = 9600
    data_bits: int = 8
    parity: str = "N"
    stop_bits: float = 1
    flow_control: str = "none"

    # Credential handling. The browser never receives a stored password: it
    # sends the profile id and the backend fills the credentials in from the
    # vault server-side.
    profile_id: str = ""
    remember_credentials: bool = False
    # "vault" (encrypted, the default) or "plaintext". The two are alternatives
    # rather than independent choices — a credential lives in one place.
    credential_storage: str = "vault"

    def to_params(self) -> ConnectionParams:
        """Convert to the transport-layer parameter object."""
        fields = self.model_dump()
        fields.pop("profile_id", None)
        fields.pop("remember_credentials", None)
        fields.pop("credential_storage", None)
        return ConnectionParams(**fields)


class SaveProfileRequest(BaseModel):
    """Body for POST /api/profiles."""

    name: str = ""
    hostname: str = ""
    port: int = 22
    username: str = ""
    connection_type: str = "ssh"

    # Reconnect details worth remembering. Never any secret — no password and
    # no key passphrase is ever written to a profile.
    private_key_path: str = ""
    private_key_username: str = ""
    jump_host: str = ""
    jump_port: int = 22
    jump_username: str = ""
    serial_port: str = ""
    baud_rate: int = 9600
    data_bits: int = 8
    parity: str = "N"
    stop_bits: float = 1
    flow_control: str = "none"


class UpdateSettingsRequest(BaseModel):
    """Body for POST /api/settings."""

    settings: dict


@app.post("/api/sessions")
async def create_session(request: CreateSessionRequest) -> dict:
    """
    Create a session over SSH, serial or telnet and return its metadata.

    The connection is established synchronously; on failure the handler's
    message is returned as a 400 so the frontend can show something the user
    can act on rather than a stack trace.
    """
    params = request.to_params()

    # Fill in anything the user chose to have remembered. Only fields left
    # blank are filled, so a password typed in the dialog always wins over a
    # stale stored one.
    if request.profile_id:
        for field, value in load_credentials(request.profile_id).items():
            if not getattr(params, field, ""):
                setattr(params, field, value)

    # Captured before connecting because the handler scrubs them from params
    # the moment authentication succeeds.
    to_remember = {
        field: getattr(params, field, "")
        for field in CREDENTIAL_FIELDS
    } if (request.remember_credentials and request.profile_id) else {}

    try:
        # Every transport blocks while connecting, so run it off the event loop.
        session = await asyncio.to_thread(session_manager.create_session, params)
    except ConnectionError_ as exc:
        # Already phrased for the user by the handler.
        logger.info("Connection failed: %s", exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Unexpected error creating session")
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Only remember credentials that actually worked — storing them before the
    # handshake would persist a typo the user is about to correct.
    if to_remember:
        if request.credential_storage == "plaintext":
            # The user asked for this explicitly. Logged at warning level
            # because a device password landing on disk unencrypted should be
            # visible in the record afterwards, not silent.
            logger.warning(
                "Saving credentials for profile %s in PLAIN TEXT at the user's request",
                request.profile_id,
            )
            save_plaintext_credentials(request.profile_id, to_remember)
        else:
            save_credentials(request.profile_id, to_remember)

    return session


# ---------------------------------------------------------------------------
# REST — Session history
# ---------------------------------------------------------------------------


@app.get("/api/history/search")
async def history_search(
    q: str = "", hostname: str = "", since: float | None = None,
    until: float | None = None, limit: int = 100,
) -> dict:
    """
    Search every command ever run.

    This is the point of storing transcripts rather than flat log files:
    "what did I change on the Glasgow core last Tuesday" becomes a query with
    a device filter and a date range, instead of grep across a folder.
    """
    hits = await asyncio.to_thread(
        store.search, q.strip(), hostname.strip(), since, until, limit,
    )
    return {"query": q, "count": len(hits), "results": hits}


@app.get("/api/history/sessions")
async def history_sessions(limit: int = 50, hostname: str = "") -> list[dict]:
    """List recorded sessions, newest first."""
    return await asyncio.to_thread(store.list_sessions, limit, hostname.strip())


@app.get("/api/history/sessions/{session_id}")
async def history_session_detail(session_id: str) -> dict:
    """Return one recorded session with every command it ran, for replay."""
    session = await asyncio.to_thread(store.get_session, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="No such session in history")
    return session


@app.delete("/api/history/sessions/{session_id}")
async def history_delete_session(session_id: str) -> dict:
    """Delete a recorded session and its commands."""
    if not await asyncio.to_thread(store.delete_session, session_id):
        raise HTTPException(status_code=404, detail="No such session in history")
    return {"status": "ok"}


@app.get("/api/history/devices")
async def history_devices() -> list[str]:
    """Every device seen, for the history filter."""
    return await asyncio.to_thread(store.known_hostnames)


@app.get("/api/history/stats")
async def history_stats() -> dict:
    """Summary counts for the history panel header."""
    return await asyncio.to_thread(store.stats)


# ---------------------------------------------------------------------------
# REST — Configuration snapshots
# ---------------------------------------------------------------------------


@app.get("/api/configs/{hostname}")
async def config_list(hostname: str, limit: int = 50) -> list[dict]:
    """List stored configuration snapshots for a device, newest first."""
    return await asyncio.to_thread(store.list_snapshots, hostname, limit)


@app.get("/api/configs/snapshot/{snapshot_id}")
async def config_get(snapshot_id: int) -> dict:
    """Return one configuration snapshot in full."""
    snapshot = await asyncio.to_thread(store.get_snapshot, snapshot_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="No such snapshot")
    return snapshot


@app.get("/api/configs/diff/{old_id}/{new_id}")
async def config_diff_endpoint(old_id: int, new_id: int) -> dict:
    """Return a unified diff between two stored snapshots."""
    old = await asyncio.to_thread(store.get_snapshot, old_id)
    new = await asyncio.to_thread(store.get_snapshot, new_id)
    if old is None or new is None:
        raise HTTPException(status_code=404, detail="No such snapshot")
    return diff_snapshots(old, new)


@app.post("/api/sessions/{session_id}/snapshot")
async def capture_snapshot(session_id: str) -> dict:
    """
    Capture the device's running configuration now.

    Runs on a second SSH channel so it does not disturb whatever the user is
    typing in the tab.
    """
    session = _require_session(session_id)
    try:
        return await asyncio.to_thread(capture_config, session)
    except ConnectionError_ as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/sessions/{session_id}/drift")
async def session_drift(session_id: str) -> dict:
    """
    Report what has changed on this device since it was last visited.

    Every login becomes a drift check: "you were last here 12 days ago, 4
    lines have changed."
    """
    session = _require_session(session_id)
    return await asyncio.to_thread(drift_report, session)


# ---------------------------------------------------------------------------
# REST — Credentials vault
# ---------------------------------------------------------------------------


class VaultUnlockRequest(BaseModel):
    """Body for POST /api/vault/unlock."""

    password: str = ""


class VaultModeRequest(BaseModel):
    """Body for POST /api/vault/mode."""

    mode: str                 # "dpapi" | "password"
    password: str = ""        # required when mode is "password"


@app.get("/api/vault/status")
async def vault_status() -> dict:
    """
    Report how secrets are stored and whether the vault needs unlocking.

    Returns no secrets and no key names — only what the UI needs to decide
    whether to show an unlock prompt.
    """
    return vault.status()


@app.post("/api/vault/unlock")
async def vault_unlock(request: VaultUnlockRequest) -> dict:
    """Unlock a master-password vault for the rest of this session."""
    try:
        # scrypt is deliberately slow, so keep it off the event loop.
        await asyncio.to_thread(vault.unlock, request.password)
    except VaultError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Any plaintext keys left by an older version can only be migrated once
    # the vault is readable, so this is the natural moment to try.
    for field in migrate_plaintext_secrets():
        logger.info("Migrated %s into the vault", field)

    return vault.status()


@app.post("/api/vault/lock")
async def vault_lock() -> dict:
    """Forget the decrypted vault, requiring the master password again."""
    vault.lock()
    return vault.status()


@app.post("/api/vault/mode")
async def vault_set_mode(request: VaultModeRequest) -> dict:
    """Switch between DPAPI and master-password storage, re-encrypting."""
    try:
        await asyncio.to_thread(vault.set_mode, request.mode, request.password)
    except VaultError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return vault.status()


class ProfileCredentialsRequest(BaseModel):
    """Body for PUT /api/profiles/{id}/credentials."""

    password: str = ""
    private_key_passphrase: str = ""
    jump_password: str = ""
    jump_private_key_passphrase: str = ""
    # "vault" (encrypted) or "plaintext", at the user's explicit choice.
    storage: str = "vault"


@app.put("/api/profiles/{profile_id}/credentials")
async def remember_profile_credentials(
    profile_id: str, request: ProfileCredentialsRequest,
) -> dict:
    """
    Remember credentials for a profile.

    Needed for the first connection to a new device: at connect time no
    profile existed yet, so there was nowhere to file the credentials. The
    frontend creates the profile, then calls this.

    Returns only whether anything was stored — never the values back.
    """
    values = request.model_dump()
    storage = values.pop("storage", "vault")

    if storage == "plaintext":
        logger.warning(
            "Saving credentials for profile %s in PLAIN TEXT at the user's request",
            profile_id,
        )
        return {"status": "ok", "stored": save_plaintext_credentials(profile_id, values)}

    stored = save_credentials(profile_id, values)
    if not stored and vault.is_locked():
        raise HTTPException(
            status_code=400,
            detail="The vault is locked, so credentials could not be saved.",
        )
    return {"status": "ok", "stored": stored}


@app.delete("/api/profiles/{profile_id}/credentials")
async def forget_profile_credentials(profile_id: str) -> dict:
    """Forget the credentials remembered for a profile."""
    forget_credentials(profile_id)
    return {"status": "ok", "profile_id": profile_id}


@app.get("/api/sftp/{session_id}/list")
async def sftp_list(session_id: str, path: str = ".") -> dict:
    """List a remote directory over the tab's existing SSH connection."""
    session = _require_session(session_id)
    try:
        return await asyncio.to_thread(sftp.list_directory, session, path)
    except ConnectionError_ as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/sftp/{session_id}/download")
async def sftp_download(session_id: str, path: str) -> Response:
    """Download a remote file."""
    session = _require_session(session_id)
    try:
        data = await asyncio.to_thread(sftp.read_file, session, path)
    except ConnectionError_ as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Take only the basename, and strip anything that could steer where the
    # browser writes it or break out of the Content-Disposition header.
    filename = re.sub(r'[^\w\-. ]', "_", path.rsplit("/", 1)[-1]) or "download"
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/api/sftp/{session_id}/upload")
async def sftp_upload(session_id: str, path: str, file: UploadFile = File(...)) -> dict:
    """Upload a file to the remote path."""
    session = _require_session(session_id)
    data = await file.read()
    try:
        return await asyncio.to_thread(sftp.write_file, session, path, data)
    except ConnectionError_ as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/api/sftp/{session_id}/file")
async def sftp_delete(session_id: str, path: str) -> dict:
    """Delete a remote file."""
    session = _require_session(session_id)
    try:
        return await asyncio.to_thread(sftp.delete_file, session, path)
    except ConnectionError_ as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/serial/ports")
async def serial_ports() -> list[dict]:
    """
    List serial ports present on this machine.

    Backs the port picker in the connection dialog: "COM3" on its own is not
    enough to identify the right adapter when a laptop has a dock and two USB
    converters attached.
    """
    return await asyncio.to_thread(available_ports)


# ---------------------------------------------------------------------------
# REST — Picking a file on this machine
#
# SSH key authentication needs a filesystem *path*. A browser file input hands
# over the contents and deliberately withholds the path, so the only ways to
# get one are the platform's own dialog — available when running in the native
# window — or letting the user walk the filesystem in the interface. Both are
# provided: the OS dialog is the better experience and the in-app browser is
# what makes the feature work at all when ShellMate is opened in a browser.
#
# Loopback-only, like everything else here, and nothing reads file contents.
# ---------------------------------------------------------------------------


class PickFileRequest(BaseModel):
    """Body for POST /api/pick-file."""

    title: str = "Select a file"
    directory: str = ""
    # ("Key files (*.pem;*.ppk)", "All files (*.*)") — the platform dialog's
    # own format, passed through untouched.
    file_types: list[str] = []


@app.post("/api/pick-file")
async def pick_file(request: PickFileRequest) -> dict:
    """
    Raise the platform's file dialog.

    ``available`` is false when there is no native window, which is not an
    error — it tells the interface to open its own browser instead. Reporting
    that as a failure would leave the button looking broken in exactly the
    situation where a working alternative exists.
    """
    if not desktop.has_native_window():
        return {"available": False, "path": ""}

    path = await asyncio.to_thread(
        desktop.pick_file, request.title, request.directory, tuple(request.file_types),
    )
    return {"available": True, "path": path or "", "cancelled": not path}


@app.get("/api/local/browse")
async def local_browse(path: str = "") -> dict:
    """
    List a directory on this machine, for the in-app file picker.

    Directories and file names only — never contents. Unreadable entries are
    skipped rather than failing the whole listing, because one locked folder
    should not make a directory unbrowsable.
    """
    def _listing() -> dict:
        # Default somewhere useful: this is reached for from the SSH key field.
        base = Path(path).expanduser() if path else (Path.home() / ".ssh")
        if not base.exists():
            base = Path.home()
        base = base.resolve()

        if base.is_file():
            base = base.parent

        entries = []
        try:
            for item in sorted(base.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
                try:
                    is_dir = item.is_dir()
                    entries.append({
                        "name": item.name,
                        "path": str(item),
                        "is_dir": is_dir,
                        "size": 0 if is_dir else item.stat().st_size,
                    })
                except OSError:
                    continue
        except OSError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        parent = str(base.parent) if base.parent != base else ""
        return {"path": str(base), "parent": parent, "entries": entries}

    return await asyncio.to_thread(_listing)


def _require_session(session_id: str) -> dict:
    """Look up a session or raise a 404. Shared by the SFTP endpoints."""
    session = session_manager.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


@app.get("/api/sessions")
async def list_sessions() -> list[dict]:
    """Return metadata for all active sessions."""
    return session_manager.get_all_sessions()


@app.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str) -> dict:
    """Tear down a session — closes SSH, clears buffer, removes from manager."""
    session = session_manager.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    await asyncio.to_thread(session_manager.destroy_session, session_id)
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# REST — Connection profiles
# ---------------------------------------------------------------------------

@app.get("/api/profiles")
async def list_profiles() -> list[dict]:
    """Return all saved connection profiles."""
    return get_profiles()


@app.post("/api/profiles")
async def create_profile(request: SaveProfileRequest) -> dict:
    """Save a connection profile (no password or passphrase is ever stored)."""
    profile = save_profile(request.model_dump())

    # The device usually announces its name before the frontend gets round to
    # saving the profile, so the name is known but there was nothing to write
    # it to. Look it up by target rather than relying on the two happening in
    # a particular order.
    target = f"{request.hostname}:{request.port}"
    detected = session_manager.detected_hostnames.get(target)
    if detected and detected != request.hostname:
        try:
            await asyncio.to_thread(
                record_detected_hostname,
                request.hostname, request.port, request.username, detected,
            )
            return next((p for p in get_profiles() if p["id"] == profile["id"]), profile)
        except Exception as exc:
            logger.debug("Could not name the new profile after the device: %s", exc)

    return profile


@app.delete("/api/profiles/{profile_id}")
async def remove_profile(profile_id: str) -> dict:
    """Delete a saved profile."""
    if not delete_profile(profile_id):
        raise HTTPException(status_code=404, detail="Profile not found")
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# REST — Settings
# ---------------------------------------------------------------------------

@app.get("/api/system/info")
async def system_info() -> dict:
    """
    Report where ShellMate is storing data and whether it is running frozen.

    The UI shows this so the user is never wrong about where their profiles
    live — particularly when the portable location was read-only and we
    silently fell back to per-user storage.
    """
    return {
        # Marker used by the single-instance check in backend/server.py to
        # confirm a responding port is actually us.
        "app":            "shellmate-portable",
        "data_dir":       str(paths.data_dir()),
        "using_fallback": paths.data_dir_is_fallback(),
        "portable":       paths.is_frozen(),
        "log_dir":        str(log_directory()),
    }


@app.get("/api/settings")
async def get_app_settings() -> dict:
    """Return current application settings (secrets masked, env flags included)."""
    return get_settings_for_ui()


@app.post("/api/settings")
async def save_app_settings(request: UpdateSettingsRequest) -> dict:
    """Persist updated settings and return the merged result."""
    try:
        return update_settings(request.settings)
    except VaultError as exc:
        # Most likely a locked master-password vault. The user needs to unlock
        # it, not see a 500.
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# REST — Chroma DB health check (used by the settings panel "Test" button)
# ---------------------------------------------------------------------------

@app.get("/api/chroma/health")
async def chroma_health() -> dict:
    """Return whether the configured Chroma DB is reachable."""
    return await chroma_client.health_check()


# ---------------------------------------------------------------------------
# REST — Jira integration
# ---------------------------------------------------------------------------

@app.get("/api/jira/config")
async def jira_config() -> dict:
    """Return whether Jira is configured and the project key."""
    configured = bool(JIRA_URL and JIRA_USER_EMAIL and JIRA_API_TOKEN and JIRA_PROJECT_KEY)
    return {"configured": configured, "project_key": JIRA_PROJECT_KEY, "jira_url": JIRA_URL}


@app.get("/api/jira/search")
async def jira_search(q: str = "") -> list[dict]:
    """Search Jira issues by text within the configured project."""
    if not all([JIRA_URL, JIRA_USER_EMAIL, JIRA_API_TOKEN, JIRA_PROJECT_KEY]):
        raise HTTPException(400, "Jira not configured")
    # Allow empty query — Jira picker returns recent issues
    from backend.jira_client import search_issues
    try:
        return await search_issues(JIRA_URL, JIRA_USER_EMAIL, JIRA_API_TOKEN, JIRA_PROJECT_KEY, q.strip())
    except Exception as e:
        raise HTTPException(502, f"Jira search error: {e}")


@app.get("/api/jira/issue-types")
async def jira_issue_types() -> list[str]:
    """Return available issue types for the configured project."""
    if not all([JIRA_URL, JIRA_USER_EMAIL, JIRA_API_TOKEN, JIRA_PROJECT_KEY]):
        raise HTTPException(400, "Jira not configured")
    from backend.jira_client import get_issue_types
    try:
        return await get_issue_types(JIRA_URL, JIRA_USER_EMAIL, JIRA_API_TOKEN, JIRA_PROJECT_KEY)
    except Exception as e:
        raise HTTPException(502, f"Jira error: {e}")


@app.post("/api/ai/session-summary")
async def ai_session_summary(request: Request) -> dict:
    """Return an AI-generated summary of the open terminal sessions and chat history."""
    body             = await request.json()
    open_session_ids = body.get("open_session_ids") or []
    chat_messages    = body.get("chat_messages") or []
    backend          = (body.get("backend") or DEFAULT_AI_BACKEND).strip()
    model            = body.get("model") or None

    from backend.ai.summarize import summarize_session
    try:
        summary = await summarize_session(
            open_session_ids=open_session_ids,
            chat_messages=chat_messages,
            backend=backend,
            session_manager=session_manager,
            model=model,
        )
    except Exception as e:
        logger.exception("session summary failed")
        raise HTTPException(502, f"Summary failed: {e}")
    return {"summary": summary}


@app.post("/api/jira/session")
async def post_session_to_jira(request: Request) -> dict:
    """Build a rich ADF document from session buffers + chat history and post to Jira."""
    if not all([JIRA_URL, JIRA_USER_EMAIL, JIRA_API_TOKEN, JIRA_PROJECT_KEY]):
        raise HTTPException(400, "Jira not configured — add JIRA_* vars to .env")

    body             = await request.json()
    summary          = body.get("summary", "ShellMate Session").strip() or "ShellMate Session"
    description      = body.get("description", "")
    issue_type       = body.get("issue_type", "Task")
    open_session_ids = body.get("open_session_ids") or []
    chat_messages    = body.get("chat_messages") or []
    existing_key     = (body.get("existing_issue_key") or "").strip().upper()

    # Collect terminal buffers from the session manager
    sessions = []
    for sid in open_session_ids:
        sess = session_manager.get_session(sid)
        if not sess:
            continue
        buf = sess.get("buffer")
        sessions.append({
            "label":           sess.get("display_label") or sess.get("hostname", sid[:8]),
            "hostname":        sess.get("hostname", ""),
            "connection_type": sess.get("connection_type", "ssh"),
            "buffer_text":     buf.get_text(500) if buf else "",
        })

    from backend.jira_client import build_adf, create_issue, add_comment
    adf = build_adf(description, sessions, chat_messages)

    try:
        if existing_key:
            # Add session as a comment on an existing issue
            await add_comment(JIRA_URL, JIRA_USER_EMAIL, JIRA_API_TOKEN, existing_key, adf)
            return {
                "issue_key": existing_key,
                "url": f"{JIRA_URL.rstrip('/')}/browse/{existing_key}",
                "mode": "comment",
            }
        else:
            # Create a brand new issue
            result = await create_issue(
                JIRA_URL, JIRA_USER_EMAIL, JIRA_API_TOKEN,
                JIRA_PROJECT_KEY, summary, adf, issue_type,
            )
            issue_key = result.get("key", "")
            return {
                "issue_key": issue_key,
                "url": f"{JIRA_URL.rstrip('/')}/browse/{issue_key}",
                "mode": "created",
            }
    except Exception as e:
        raise HTTPException(502, f"Jira API error: {e}")


# ---------------------------------------------------------------------------
# REST — Ollama model list
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# REST — Broadcast
# ---------------------------------------------------------------------------


class BroadcastRequest(BaseModel):
    """Body for POST /api/broadcast."""

    session_ids: list[str]
    # Either form works. `command` is the single-line original and may itself
    # contain newlines, which are split into separate commands; `commands` is
    # the explicit list. Both exist so the one-liner in the docs keeps working.
    command: str = ""
    commands: list[str] = []
    # Milliseconds between one command and the next on the same device. Devices
    # do not answer instantly, and a save in particular needs a moment before
    # it will accept anything else.
    wait_ms: int = 500
    # Enter is pressed unless the caller says otherwise. The alternative
    # default leaves a half-typed line sitting at the prompt on every device,
    # which is a worse thing to do by accident than running the command.
    execute: bool = True

    def command_list(self) -> list[str]:
        """Every command to send, in order, however the caller expressed it."""
        raw = list(self.commands) if self.commands else [self.command]
        out = []
        for item in raw:
            # A pasted block arrives as one string. Each line is a command;
            # blank lines are the paste's formatting, not instructions.
            out.extend(line.strip() for line in str(item).splitlines() if line.strip())
        return out


# A whole sequence runs inside one request, so it needs a ceiling. Long enough
# for a real save-and-verify across a rack, short enough that a mistyped wait
# cannot hold a connection open all afternoon.
BROADCAST_MAX_SECONDS = 180


@app.post("/api/broadcast")
async def broadcast(request: BroadcastRequest) -> dict:
    """
    Send one or more commands to several sessions.

    Deliberately compose-and-send rather than mirroring keystrokes into every
    tab. Mirroring means a stray keypress — or a half-typed command answered
    by a device that autocompletes — reaches the whole fleet, and the operator
    never sees the finished command before it lands. Here the commands are
    written once, the targets are named, and each result comes back
    individually so a partial failure is visible rather than assumed.

    Devices run **concurrently** and commands run **in order** on each one.
    That is the only arrangement that makes the wait mean what people expect:
    a two-second gap between save and verify, not two seconds multiplied by
    the number of switches.

    Every command still passes through that session's outbound pipeline, so
    alias expansion and (later) guardrails apply exactly as when typed.
    """
    commands = request.command_list()
    if not commands:
        raise HTTPException(status_code=400, detail="No command given.")
    if not request.session_ids:
        raise HTTPException(status_code=400, detail="No sessions selected.")

    wait = max(0, min(60_000, request.wait_ms)) / 1000

    async def run_one(session_id: str) -> dict:
        session = session_manager.get_session(session_id)
        if session is None:
            return {"session_id": session_id, "ok": False, "label": "",
                    "error": "Session not found", "sent": []}

        label = session.get("display_label") or session.get("hostname") or session_id[:8]
        handler = session["handler"]

        if not handler.is_connected:
            return {"session_id": session_id, "ok": False, "label": label,
                    "error": "Not connected", "sent": []}

        sent = []
        for index, command in enumerate(commands):
            try:
                outbound = session["pipeline"].process(
                    command + ("\r" if request.execute else ""))
                await asyncio.to_thread(
                    handler.send, outbound.encode("utf-8", errors="replace"))
                expansion = session["pipeline"].last_expansion
                sent.append(expansion[1] if expansion else command)
            except Exception as exc:
                logger.warning("Broadcast to %s failed on %r: %s", label, command, exc)
                # Stop this device rather than pressing on: the rest of a
                # sequence rarely makes sense once a step has failed, and
                # blindly continuing is how half-applied changes happen.
                return {"session_id": session_id, "ok": False, "label": label,
                        "error": f"{exc} (after {len(sent)} of {len(commands)})",
                        "sent": sent}

            if wait and index < len(commands) - 1:
                await asyncio.sleep(wait)

        return {"session_id": session_id, "ok": True, "label": label, "sent": sent}

    try:
        results = await asyncio.wait_for(
            asyncio.gather(*(run_one(sid) for sid in request.session_ids)),
            timeout=BROADCAST_MAX_SECONDS,
        )
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=504,
            detail=(f"The sequence was still running after {BROADCAST_MAX_SECONDS}s "
                    f"and was abandoned. Some commands will already have been sent — "
                    f"check the tabs."),
        ) from None

    sent = sum(1 for r in results if r["ok"])
    logger.info("Broadcast %s command(s) to %s of %s sessions",
                len(commands), sent, len(results))
    return {
        "commands": commands, "command": commands[0],
        "sent": sent, "total": len(results), "results": results,
    }


# ---------------------------------------------------------------------------
# REST — The saved command library
# ---------------------------------------------------------------------------


class SnippetRequest(BaseModel):
    """Body for PUT /api/snippets/{id}."""

    name: str = ""
    commands: list[str] = []
    description: str = ""
    platform: str = ""
    wait_ms: int = 500
    writes: bool = False


@app.get("/api/snippets")
async def snippets_list() -> dict:
    """Return the saved command library."""
    library = await asyncio.to_thread(snippets.load_snippets)
    return {
        "snippets": [s.as_dict() for s in library],
        "path": str(snippets.snippets_path()),
    }


@app.put("/api/snippets/{snippet_id}")
async def snippet_save(snippet_id: str, request: SnippetRequest) -> dict:
    """Create or update one snippet. Pass "new" as the id to create one."""
    fields = request.model_dump()
    fields["id"] = "" if snippet_id == "new" else snippet_id
    try:
        saved = await asyncio.to_thread(snippets.save_snippet, fields)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return saved.as_dict()


@app.delete("/api/snippets/{snippet_id}")
async def snippet_delete(snippet_id: str) -> dict:
    """Remove a snippet from the library."""
    removed = await asyncio.to_thread(snippets.delete_snippet, snippet_id)
    if not removed:
        raise HTTPException(status_code=404, detail="No such snippet.")
    return {"status": "ok", "id": snippet_id}


@app.post("/api/snippets/reset")
async def snippets_reset() -> dict:
    """Put the shipped library back, discarding every edit."""
    library = await asyncio.to_thread(snippets.reset_to_defaults)
    return {"snippets": [s.as_dict() for s in library]}


# ---------------------------------------------------------------------------
# REST — Platform definitions
# ---------------------------------------------------------------------------


class PlatformRequest(BaseModel):
    """Body for PUT /api/platforms/{id}."""

    name: str = ""
    paging_off: str = ""
    show_run: str = ""
    version_command: str = ""
    signatures: list[str] = []
    aliases: dict[str, str] = {}
    dangerous_commands: list[str] = []
    config_mode_markers: list[str] = []
    comment_prefix: str = "!"


@app.get("/api/platforms")
async def platforms_list() -> dict:
    """
    Return every platform definition.

    These drive paging-off, config retrieval, aliases and the dangerous-command
    list, and are editable both here and in platforms.json.
    """
    return {
        "platforms": {key: p.as_dict() for key, p in platforms_module.load_profiles().items()},
        "builtin": sorted(platforms_module.BUILTIN),
        "path": str(platforms_module.profiles_path()),
    }


@app.put("/api/platforms/{platform_id}")
async def platform_save(platform_id: str, request: PlatformRequest) -> dict:
    """Create or update one platform definition."""
    try:
        updated = await asyncio.to_thread(
            platforms_module.save_profile_edits, platform_id, request.model_dump(),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return updated.as_dict()


@app.delete("/api/platforms/{platform_id}")
async def platform_delete(platform_id: str) -> dict:
    """Delete a platform the user added. Built-ins cannot be removed."""
    try:
        removed = await asyncio.to_thread(platforms_module.delete_platform, platform_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not removed:
        raise HTTPException(status_code=404, detail="No such platform")
    return {"status": "ok"}


@app.post("/api/platforms/reset")
async def platforms_reset() -> dict:
    """Discard every edit and restore the built-in definitions."""
    try:
        profiles = await asyncio.to_thread(platforms_module.reset_to_defaults)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"platforms": {key: p.as_dict() for key, p in profiles.items()}}


@app.get("/api/providers/{provider}/test")
async def provider_test(provider: str) -> dict:
    """
    Test one AI provider and return the models it offers.

    Listing models doubles as the connection test: it needs a valid key, costs
    nothing, and returns something useful when it works.
    """
    result = await providers.check(provider)
    return result.as_dict()


@app.get("/api/providers/models")
async def provider_models() -> dict:
    """
    Return every model available across all configured providers.

    Backs the "refresh models" action, so the picker reflects what is actually
    reachable rather than a hardcoded list.
    """
    return await providers.check_all()


@app.get("/api/ollama/models")
async def ollama_models() -> list[dict]:
    """Return the list of models installed in the local Ollama instance."""
    from backend.config import OLLAMA_HOST
    import httpx
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{OLLAMA_HOST.rstrip('/')}/api/tags")
            resp.raise_for_status()
            data = resp.json()
            return [
                {
                    "name":   m["name"],
                    "size":   m.get("details", {}).get("parameter_size", ""),
                    "family": m.get("details", {}).get("family", ""),
                }
                for m in data.get("models", [])
            ]
    except Exception:
        return []


# REST — Session logs
# ---------------------------------------------------------------------------

@app.get("/api/logs")
async def list_logs() -> list[dict]:
    """Return a list of available session log files."""
    from datetime import datetime
    logs_dir = log_directory()
    if not logs_dir.exists():
        return []
    files = []
    for f in sorted(logs_dir.glob("*.log"), key=lambda x: x.stat().st_mtime, reverse=True):
        stat = f.stat()
        files.append({
            "filename": f.name,
            "size_bytes": stat.st_size,
            "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
        })
    return files


@app.get("/api/logs/{filename}")
async def download_log(filename: str) -> FileResponse:
    """Download a specific log file."""
    # Sanitize filename — only allow safe characters to prevent path traversal
    if not re.match(r'^[\w\-\.]+\.log$', filename):
        raise HTTPException(status_code=400, detail="Invalid filename")
    log_path = log_directory() / filename
    if not log_path.exists():
        raise HTTPException(status_code=404, detail="Log file not found")
    return FileResponse(str(log_path), filename=filename)


# ---------------------------------------------------------------------------
# WebSocket — terminal I/O
# ---------------------------------------------------------------------------

@app.websocket("/ws/terminal/{session_id}")
async def terminal_websocket(websocket: WebSocket, session_id: str) -> None:
    """
    Bidirectional WebSocket bridge between the browser's xterm.js and the
    device, whatever the transport underneath is.

    Each session_id gets its own WebSocket connection.  Multiple tabs in
    the browser each connect here with their own session_id — they are
    completely independent.
    """
    await websocket.accept()

    session = session_manager.get_session(session_id)
    if session is None:
        await websocket.send_text(
            json.dumps({"type": "output", "data": "\r\nError: session not found.\r\n"})
        )
        await websocket.close()
        return

    handler = session["handler"]
    hostname_sent = False  # Only send hostname_detected once per session

    async def read_from_client() -> None:
        """Forward browser keystrokes / resize events to the device."""
        try:
            while True:
                raw = await websocket.receive_text()
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    # If someone sends plain text, treat it as input
                    msg = {"type": "input", "data": raw}

                msg_type = msg.get("type")

                if msg_type == "input":
                    data: str = msg.get("data", "")
                    if data and handler.is_connected:
                        # Everything the user sends goes through the pipeline,
                        # which assembles keystrokes into lines and may rewrite
                        # one (an alias) before it reaches the device.
                        outbound = session["pipeline"].process(data)
                        await asyncio.to_thread(
                            handler.send, outbound.encode("utf-8", errors="replace")
                        )

                        # Say so when a command was rewritten. Silently sending
                        # something other than what was typed would be worse
                        # than not helping at all.
                        expansion = session["pipeline"].last_expansion
                        if expansion:
                            await websocket.send_text(json.dumps({
                                "type": "alias_expanded",
                                "typed": expansion[0],
                                "sent":  expansion[1],
                            }))

                        # Watch for anything that schedules itself: `reload in
                        # 10`, `commit confirmed`. Known here the moment Enter
                        # is pressed, which is a second or two before the
                        # device says anything about it.
                        await note_pending(
                            session["alerts"].observe_command(command)
                            for command in session["pipeline"].completed_commands
                        )

                elif msg_type == "resize":
                    cols = int(msg.get("cols", 80))
                    rows = int(msg.get("rows", 24))
                    await asyncio.to_thread(handler.resize, cols, rows)

                elif msg_type == "break":
                    # Serial only: the break signal used to drop a booting
                    # Cisco device into ROMMON.
                    if hasattr(handler, "send_break"):
                        await asyncio.to_thread(handler.send_break)

        except WebSocketDisconnect:
            pass
        except Exception as exc:
            logger.warning("read_from_client error (session %s): %s", session_id, exc)

    async def note_pending(changes) -> None:
        """
        Tell the browser when what is pending on this device has changed.

        Takes an iterable of "did something change" booleans and sends at most
        one message, because a batch of keystrokes can produce several and the
        interface only needs the resulting state.
        """
        if any(list(changes)):
            await websocket.send_text(json.dumps(session["alerts"].payload()))

    async def maybe_onboard() -> None:
        """
        Identify the device and apply its platform settings, once.

        Called on every pass of the read loop *including idle ones*. After the
        login banner a device says nothing until it is spoken to, so a check
        that only ran when new output arrived would not fire until the user
        started typing — and the paging command would then interleave with
        their first keystroke.
        """
        onboarder = session["onboarder"]
        if onboarder.done:
            return

        at_prompt = bool(session["transcript"].last_prompt)
        if not onboarder.ready(at_prompt):
            return

        summary = onboarder.run(session["transcript"].last_prompt)
        session["fingerprint"] = summary
        session["pipeline"].platform = summary["platform"]
        # Which patterns a pending reload is recognised by depends on the
        # platform, so the tracker is idle until the device is identified.
        session["alerts"].platform = summary["platform"]

        terminal_settings = get_settings().get("terminal", {})
        session["pipeline"].expand_aliases = bool(
            terminal_settings.get("expand_aliases", True)
        )

        await websocket.send_text(
            json.dumps({"type": "device_identified", **summary})
        )

        if (summary["paging_command"]
                and terminal_settings.get("auto_paging_off", True)
                and handler.is_connected):
            await asyncio.to_thread(
                handler.send, (summary["paging_command"] + "\r").encode()
            )

    async def read_from_channel() -> None:
        """Forward device output to the browser and the session buffer."""
        nonlocal hostname_sent
        try:
            while True:
                # See ConnectionHandler.recv: None means idle (keep waiting),
                # b"" means the far end closed.
                data_bytes = await asyncio.to_thread(handler.recv, 4096)

                if data_bytes is None:
                    # Idle, but onboarding may still be due — see maybe_onboard.
                    try:
                        await maybe_onboard()
                    except Exception as exc:
                        logger.warning("Onboarding failed for %s: %s", session_id, exc)
                    # An idle session is exactly where a reload deadline passes
                    # unnoticed, so retire it here rather than only when the
                    # device happens to say something.
                    try:
                        await note_pending([session["alerts"].expire()])
                    except Exception as exc:
                        logger.warning("Alert expiry failed for %s: %s", session_id, exc)
                    continue

                if not data_bytes:
                    # Channel closed (device disconnected or session ended)
                    session["is_connected"] = False
                    await websocket.send_text(
                        json.dumps({
                            "type": "output",
                            "data": "\r\n\r\n[Connection closed]\r\n",
                        })
                    )
                    break

                text = data_bytes.decode("utf-8", errors="replace")

                # Write to session buffer
                session_manager.write_to_buffer(session_id, text)

                # Reconstruct commands from the stream and record them. Any
                # failure here is logged and dropped: history is valuable, but
                # never worth interrupting a live session for.
                try:
                    for record in session["transcript"].feed(text):
                        store.add_command(session_id, record)
                except Exception as exc:
                    logger.warning("Transcript error on session %s: %s", session_id, exc)

                # File logging (if enabled in settings)
                _settings = get_settings()
                if _settings.get("logging", {}).get("enabled"):
                    _log_dir = log_directory()
                    _log_dir.mkdir(parents=True, exist_ok=True)
                    _log_file = _log_dir / f"{session_id[:8]}-{session.get('hostname', 'session')}.log"
                    from datetime import datetime
                    # Devices echo, so a password typed at a login prompt
                    # can land in a file that exists to be handed to someone
                    # else. The live terminal always shows the truth; only
                    # what is written to disk is masked.
                    _logged = (redact(text)
                               if _settings["logging"].get("redact_secrets", True)
                               else text)
                    with open(_log_file, "a", encoding="utf-8") as _lf:
                        _lf.write(f"[{datetime.now().isoformat()}] {_logged}")

                # Send output to browser
                await websocket.send_text(json.dumps({"type": "output", "data": text}))

                # Collect banner text for identification, then run the same
                # onboarding check the idle path uses.
                try:
                    session["onboarder"].observe(text)
                    await maybe_onboard()
                except Exception as exc:
                    logger.warning("Onboarding failed for session %s: %s", session_id, exc)

                # The device's own word on what it is about to do. This is the
                # authoritative timing and re-synchronises the countdown, so it
                # is read from every chunk rather than only the first.
                try:
                    await note_pending([
                        session["alerts"].observe_output(text),
                        session["alerts"].expire(),
                    ])
                except Exception as exc:
                    logger.warning("Alert tracking failed for %s: %s", session_id, exc)

                # Name the tab after the device. Uses the shared cross-vendor
                # prompt parser, so Junos and PAN-OS are recognised as well as
                # IOS — the old pattern here understood Cisco only.
                if not hostname_sent:
                    detected = detect_hostname(text)
                    if detected:
                        await websocket.send_text(
                            json.dumps({"type": "hostname_detected", "hostname": detected})
                        )
                        hostname_sent = True
                        # Connections are often opened by IP, so recording the
                        # real name is what makes searching by device work.
                        store.update_session_hostname(session_id, detected)
                        # Update the live session too, not just the database.
                        # Config snapshots are keyed by hostname, and keying
                        # them by IP would file the same device under two
                        # names depending on how it was reached that day.
                        previous_target = session["hostname"]
                        session["hostname"] = detected

                        # Remember it against the address so a profile saved
                        # after this point can still be named correctly.
                        session_manager.detected_hostnames[
                            f"{previous_target}:{session.get('port') or 0}"
                        ] = detected

                        # Name the saved connection after the device, so the
                        # welcome screen shows a name rather than an address.
                        # The address it dials is left alone — see
                        # record_detected_hostname.
                        try:
                            await asyncio.to_thread(
                                record_detected_hostname,
                                previous_target,
                                session.get("port") or 0,
                                session.get("username") or "",
                                detected,
                            )
                        except Exception as exc:
                            logger.debug("Could not record hostname on profile: %s", exc)

        except WebSocketDisconnect:
            pass
        except Exception as exc:
            logger.warning("read_from_channel error (session %s): %s", session_id, exc)
            session["is_connected"] = False

    # Run both directions concurrently; cancel the other when one finishes
    read_client_task = asyncio.create_task(read_from_client())
    read_channel_task = asyncio.create_task(read_from_channel())

    done, pending = await asyncio.wait(
        {read_client_task, read_channel_task},
        return_when=asyncio.FIRST_COMPLETED,
    )

    for task in pending:
        task.cancel()

    session["is_connected"] = False
    logger.info("WebSocket closed for session %s", session_id)


# ---------------------------------------------------------------------------
# WebSocket — AI chat
# ---------------------------------------------------------------------------

@app.websocket("/ws/chat")
async def chat_websocket(websocket: WebSocket) -> None:
    """
    Streaming AI chat WebSocket.

    Receives from browser:
      {"message": "...", "session_id": "...", "backend": "claude|ollama", "context_mode": "active|all|1|2..."}

    Streams to browser:
      {"type": "chunk",  "data": "..."}    — one per token
      {"type": "done"}                     — stream complete
      {"type": "error",  "message": "..."}  — on failure
    """
    await websocket.accept()
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_text(
                    json.dumps({"type": "error", "message": "Invalid JSON"})
                )
                continue

            user_message     = msg.get("message", "").strip()
            session_id       = msg.get("session_id")
            backend          = msg.get("backend", DEFAULT_AI_BACKEND)
            model            = msg.get("model") or None
            context_mode     = msg.get("context_mode", "active")
            open_session_ids = msg.get("open_session_ids") or None
            mode             = msg.get("mode") or None  # "learn" | "tshoot"

            if not user_message:
                continue

            try:
                async for chunk in stream_chat(
                    message=user_message,
                    active_session_id=session_id,
                    backend=backend,
                    context_mode=context_mode,
                    session_manager=session_manager,
                    open_session_ids=open_session_ids,
                    model=model,
                    mode=mode,
                ):
                    await websocket.send_text(
                        json.dumps({"type": "chunk", "data": chunk})
                    )

                await websocket.send_text(json.dumps({"type": "done"}))

            except Exception as exc:
                logger.error("AI chat error: %s", exc)
                await websocket.send_text(
                    json.dumps({"type": "error", "message": str(exc)})
                )

    except WebSocketDisconnect:
        pass
