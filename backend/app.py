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
import contextlib
import datetime
import json
import logging
import re
from collections import deque

import httpx
import sys
import os
import shutil
import threading
import time
from pathlib import Path

from fastapi import (
    FastAPI, File, HTTPException, Request, Response, UploadFile, WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from backend import auth, config_archive, desktop, paths
from backend import groups as groups_module
from backend import schemes as schemes_module
from backend.configs import capture_config, diff_snapshots, drift_report
from backend.connections.base import (ConnectionError_, ConnectionParams,
                                      HostKeyChanged, InteractiveRequired)
from backend.connections import sftp
from backend.connections.manager import SessionManager
from backend.connections.serial_handler import available_ports
from backend import profiles as profiles_module
from backend.profiles import (
    CREDENTIAL_FIELDS, delete_profile, forget_credentials, get_profiles,
    load_credentials, record_detected_hostname, save_credentials,
    save_plaintext_credentials, save_profile,
)
from backend import discovery
from backend import feedback as feedback_module
from backend import platforms as platforms_module
from backend import snippets
from backend import support
from backend import advanced as advanced_settings
from backend.advanced import get as advanced_setting
from backend import keys as ssh_keys
from backend.onboard import OnConnectScript, as_chosen, summarise
from backend import pipeline as pipeline_module
from backend.session import outbound
from backend.session.redact import redact
from backend.session.transcript import detect_hostname
from backend.settings_store import (
    get_settings, get_settings_for_ui, log_directory, migrate_plaintext_secrets,
    update_settings,
)
from backend.store import store
from backend.vault import VaultError, vault
from backend.ai import prompt_store
from backend.ai.router import stream_chat
from backend.ai import chroma_client, providers
from backend.config import DEFAULT_AI_BACKEND, JIRA_URL, JIRA_USER_EMAIL, JIRA_API_TOKEN, JIRA_PROJECT_KEY
from backend import version as app_version
from backend import config_push, scheduler
from backend import licence as licence_module
from backend import updater
from backend.connections import forwards as forwards_module
from backend.config import HOST as BIND_HOST

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

#: What counts as one of our own pages. Used by the CORS middleware *and* by
#: the WebSocket handshake check below — CORS does not cover WebSockets, and
#: two definitions of the same thing is how one of them ends up wrong.
ALLOWED_ORIGIN = re.compile(r"^http://(localhost|127\.0\.0\.1)(:\d+)?$")

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=ALLOWED_ORIGIN.pattern,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Static file serving
# ---------------------------------------------------------------------------

# Serve everything under frontend/ at /static (css, js, etc.)
app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


# ---------------------------------------------------------------------------
# Cache control
#
# ShellMate is upgraded by replacing one executable, and the window then talks
# to a new backend with whatever frontend it cached. The symptoms are
# arbitrary — a panel that is empty, a button that does nothing, a feature
# that appears to have been deleted — and none of them points at the cause.
# It has cost real time twice.
#
# Two mechanisms, because either alone has a hole:
#
# `Cache-Control: no-cache` makes the browser revalidate, which over loopback
# costs nothing and lets the ETag do its job instead of being overridden by
# heuristic caching. Without any header at all a browser may reuse a response
# without asking, which is what happened.
#
# A token on every asset URL covers the case where a cache ignores that —
# WebView2 keeps a persistent user-data folder and an in-memory cache for the
# life of the window. A URL that changes cannot be served from a cache keyed
# on the old one.
#
# Everything gets the same treatment, including the fonts. Exempting them was
# the obvious optimisation and turned out to be pointless: the woff2 files are
# referenced from *inside* `vendor/fonts.css` by relative URL, so they never
# carry the token and could not have been safely pinned anyway. Over loopback
# a revalidation is a stat and a 304, which is not worth a special case that
# only ever applied to the stylesheet.
# ---------------------------------------------------------------------------


#: What the Host header may say while ShellMate is bound to loopback.
LOOPBACK_HOST = re.compile(r"^(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$")


def cross_site_refusal(method: str, headers) -> str:
    """
    Why a request must be refused as not coming from our own page, or "".

    Two holes the Origin regex and CORS left open (#424):

    **DNS rebinding.** A page at evil.example whose name re-resolves to
    127.0.0.1 is, to the browser, same-origin with whatever answers there —
    so it sends no Origin, CORS never applies, and it can read everything.
    The tell is the Host header, which the browser fills in from the URL it
    believes it is talking to: ``evil.example``, never ``127.0.0.1``. While
    ShellMate is bound to loopback, any other Host is refused. Bound wider
    (the token deployment), the Host is whatever the reverse proxy passed and
    the rule does not apply.

    **Cross-site writes.** CORS stops a foreign page *reading* a response,
    not sending the request. A form post or a multipart upload needs no
    preflight, so a page anywhere could fire one at a state-changing
    endpoint. Browsers say where such a request came from: ``Sec-Fetch-Site:
    cross-site`` on anything modern, and an ``Origin`` that does not match
    the Host. Either refuses the request for any method that changes
    something. Reads are left to CORS, which already covers them.

    Non-browser clients — a script with the API token, curl, the test suite
    — send a loopback Host and no Origin, and pass.
    """
    host = (headers.get("host") or "").strip().lower()
    if auth.is_loopback(BIND_HOST) and not LOOPBACK_HOST.match(host):
        return ("Refused: the Host header is not a loopback address. ShellMate "
                "answers only to its own address on this machine.")
    if method.upper() in ("GET", "HEAD", "OPTIONS"):
        return ""
    if (headers.get("sec-fetch-site") or "").strip().lower() == "cross-site":
        return "Refused: a cross-site request cannot change anything here."
    origin = (headers.get("origin") or "").strip()
    if origin:
        origin_host = origin.split("://", 1)[-1].split("/", 1)[0].lower()
        if origin_host != host and not ALLOWED_ORIGIN.match(origin):
            return "Refused: the request's origin is not this server."
    return ""


@app.middleware("http")
async def _same_site_only(request: Request, call_next):
    """Refuse anything that did not come from our own page — see above."""
    refusal = cross_site_refusal(request.method, request.headers)
    if refusal:
        logger.warning("%s %s: %s", request.method, request.url.path, refusal)
        return JSONResponse({"detail": refusal}, status_code=403)
    return await call_next(request)


@app.middleware("http")
async def _require_login(request: Request, call_next):
    """
    Refuse anything that is not the login page until a token is presented.

    Only active when SHELLMATE_AUTH_TOKEN is set, which is the container and
    reverse-proxy case. The portable single-user installation this is built
    for never sees it.
    """
    if not auth.enabled() or auth.is_public(request.url.path):
        return await call_next(request)

    if auth.check_cookie(request.cookies.get(auth.COOKIE, "")):
        return await call_next(request)

    # A browser asking for a page gets sent to log in; anything else gets an
    # answer it can act on rather than a page it cannot render.
    if "text/html" in request.headers.get("accept", ""):
        return RedirectResponse("/login", status_code=303)
    return JSONResponse({"detail": "Not authenticated."}, status_code=401)


@app.get("/login")
async def login_page() -> Response:
    """The one page reachable without a token."""
    if not auth.enabled():
        return RedirectResponse("/", status_code=303)
    return Response(content=_LOGIN_PAGE, media_type="text/html")


class LoginRequest(BaseModel):
    token: str = ""


@app.post("/api/login")
async def login(request: LoginRequest) -> JSONResponse:
    """
    Exchange the token for a cookie.

    A cookie rather than a header because the WebSocket path is the one that
    matters — /ws/chat reads every open session — and browser WebSocket
    clients cannot set headers.
    """
    if not auth.check_token(request.token):
        logger.warning("Rejected a login attempt")
        return JSONResponse({"detail": "That is not the right token."},
                            status_code=401)

    response = JSONResponse({"status": "ok"})
    response.set_cookie(
        auth.COOKIE, auth.cookie_value(),
        httponly=True,      # not readable from script
        samesite="lax",     # not sent on a cross-site request
        max_age=60 * 60 * 12,
    )
    return response


#: Deliberately self-contained: it has to render before anything is trusted.
_LOGIN_PAGE = """<!doctype html>
<meta charset="utf-8"><title>ShellMate</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  body { background:#131313; color:#E5E2E1; font-family:system-ui,sans-serif;
         display:flex; align-items:center; justify-content:center;
         height:100vh; margin:0; }
  form { background:#202020; padding:32px; border-radius:8px;
         border:1px solid rgba(70,69,85,0.4); width:320px; }
  h1 { font-size:18px; margin:0 0 6px; }
  p  { font-size:12px; color:rgba(229,226,225,0.6); margin:0 0 18px; line-height:1.5; }
  input { width:100%; box-sizing:border-box; padding:10px; font-size:14px;
          background:#2A2A2A; color:#E5E2E1; border:1px solid rgba(70,69,85,0.5);
          border-radius:4px; }
  button { width:100%; margin-top:12px; padding:10px; font-size:14px;
           background:#4F46E5; color:#fff; border:0; border-radius:4px;
           cursor:pointer; }
  .err { color:#FFB4AB; font-size:12px; margin-top:10px; min-height:16px; }
</style>
<form onsubmit="go(event)">
  <h1>ShellMate</h1>
  <p>This instance is reachable beyond your own machine, so it asks for the
     access token before opening any session.</p>
  <input id="t" type="password" autocomplete="current-password" autofocus
         placeholder="Access token">
  <button type="submit">Unlock</button>
  <div class="err" id="e"></div>
</form>
<script>
async function go(ev) {
  ev.preventDefault();
  const r = await fetch('/api/login', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ token: document.getElementById('t').value }),
  });
  if (r.ok) { window.location = '/'; return; }
  document.getElementById('e').textContent =
    (await r.json()).detail || 'That did not work.';
}
</script>
"""


@app.middleware("http")
async def _cache_headers(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/static/") or request.url.path == "/":
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response


def _asset_token() -> str:
    """
    A short token that changes whenever any frontend file does.

    Derived from the files themselves rather than from a version number
    somebody has to remember to bump — which is the mechanism that failed:
    three script tags carried a hand-written `?v=2` and thirty did not.
    """
    try:
        newest = max((f.stat().st_mtime for f in FRONTEND_DIR.rglob("*")
                      if f.is_file()), default=0.0)
    except OSError:
        newest = 0.0
    return f"{int(newest):x}"


#: The rewritten page, keyed by token. Rebuilt when the token changes, which
#: during development is whenever a frontend file is saved.
_index_cache: dict[str, str] = {}

_ASSET_REF = re.compile(r'((?:src|href)=")(/static/[^"?]+)((?:\?[^"]*)?)(")')


@app.get("/")
async def serve_index() -> Response:
    """
    Serve the main page with a build token on every asset it references.

    Rewritten here rather than written into the markup so that adding a script
    cannot forget it. Any existing query string is discarded — the three
    hand-written `?v=` markers this replaces would otherwise sit alongside a
    mechanism that already covers them.
    """
    token = _asset_token()

    if token not in _index_cache:
        source = (FRONTEND_DIR / "index.html").read_text(encoding="utf-8")
        _index_cache.clear()
        _index_cache[token] = _ASSET_REF.sub(
            lambda m: f"{m.group(1)}{m.group(2)}?b={token}{m.group(4)}", source)

    return Response(content=_index_cache[token], media_type="text/html")


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
    #: A named credential to authenticate with, for a device that has no
    #: profile of its own yet. Names only ever travel; the value is resolved
    #: here, exactly as it is for profile_id — a saved password must not
    #: reach the browser, and that rule is what makes the whole design work.
    credential_ref: str = ""
    remember_credentials: bool = False
    # "vault" (encrypted, the default) or "plaintext". The two are alternatives
    # rather than independent choices — a credential lives in one place.
    credential_storage: str = "vault"
    # Answers to a keyboard-interactive challenge from a previous attempt
    # (#406). Never stored; the vault does not want a one-time code.
    interactive_answers: list[str] = []
    # The user's answer to "this device's host key has changed" (#528). Only
    # ever True on the retry that follows the 409, and only because somebody
    # read two fingerprints and said the new one is the device.
    trust_new_host_key: bool = False

    def to_params(self) -> ConnectionParams:
        """Convert to the transport-layer parameter object."""
        fields = self.model_dump()
        # Everything about *where a credential comes from* stays here. The
        # transport takes credentials, not instructions for finding them.
        fields.pop("profile_id", None)
        fields.pop("credential_ref", None)
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
    #: "password" | "key" | "". Which of the two SSH forms the connection uses.
    #: Deliberately not part of connection_type: both are the SSH transport,
    #: and profiles.identity() treats connection_type as part of what makes
    #: two saved connections the same device — so a separate transport value
    #: would turn one switch reached both ways into two profiles.
    auth_method: str = ""

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
    #: Free-text groupings. Tags rather than folders because the useful ones
    #: overlap — a device is both "glasgow" and "production" and "access".
    tags: list[str] = []
    # Port forwards to start with every session from this profile (#405):
    # [{kind, listen_port, host, port}].
    forwards: list[dict] = []


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
    params.credential_source = "typed" if request.password else ""

    # A shared credential, chosen in the dialog for a device with no profile.
    # Filled first so a password typed in the dialog still wins over it, the
    # same precedence a profile's own saved credentials have.
    filled_from_set: set[str] = set()
    if request.credential_ref and not request.password:
        # resolve_set, not _read_credentials: the secrets are in the vault and
        # the username is on the set entry, so reading one store yields half a
        # credential — the password applied, the username left blank, and the
        # device asked to log in as "".
        for field, value in profiles_module.resolve_set(request.credential_ref).items():
            if not getattr(params, field, ""):
                setattr(params, field, value)
                filled_from_set.add(field)
                if field == "password":
                    params.credential_source = "saved"

    if request.profile_id:
        for field, value in load_credentials(request.profile_id).items():
            if not getattr(params, field, ""):
                setattr(params, field, value)
                # So a failure can say "the saved password was refused"
                # rather than "check your password". Two different next
                # actions, and this is the only layer that knows which.
                if field == "password":
                    params.credential_source = "saved"

        # What this connection's groups lend it (#545). Done here, on the one
        # path every session goes through, because the browser sends what is
        # on the profile — and an inherited jump host is by definition not on
        # it. `load_credentials()` above has already resolved an inherited
        # shared credential, so only the transport fields are left.
        saved = profiles_module.find_profile(request.profile_id)
        borrowed = profiles_module.inherited_for(saved)["values"] if saved else {}
        for field in ("username", "jump_host", "jump_username"):
            if borrowed.get(field) and not getattr(params, field, ""):
                setattr(params, field, borrowed[field])
        # A port is only lent where nobody chose one: the profile has none and
        # what arrived is the bare transport default. Anything else is a
        # number somebody typed, and a group must not overrule that.
        if borrowed.get("port") and int(params.port or 0) in (
                0, profiles_module.DEFAULT_PORTS.get(params.connection_type, 22)):
            params.port = int(borrowed["port"])
        # The jump port belongs with the jump host, so it travels only when
        # the host it dials came from the group as well.
        if (borrowed.get("jump_port") and params.jump_host == borrowed.get("jump_host")
                and int(params.jump_port or 0) in (0, 22)):
            params.jump_port = int(borrowed["jump_port"])

    # Captured before connecting because the handler scrubs them from params
    # the moment authentication succeeds.
    #
    # A value that came from a shared credential set stays with the set
    # (#324). Snapshotting it onto the profile would work today and rot
    # later: a profile's own credentials permanently shadow the set, so the
    # day the shared password is rotated, this one device keeps the stale
    # copy — precisely the forty-switch failure the sets exist to prevent —
    # and nothing on screen shows the fork happened.
    to_remember = {
        field: getattr(params, field, "")
        for field in CREDENTIAL_FIELDS
        if field not in filled_from_set
    } if (request.remember_credentials and request.profile_id) else {}

    try:
        # Every transport blocks while connecting, so run it off the event loop.
        session = await asyncio.to_thread(session_manager.create_session, params,
                                          request.profile_id)
    except HostKeyChanged as exc:
        # Not a failure either: the device is not the one we trusted (#528).
        # 409 with both fingerprints; the interface asks, and the retry
        # carries `trust_new_host_key`. Never accepted here — a tool that
        # decides this on the user's behalf is quieter than PuTTY on the one
        # warning every engineer has been saved by.
        logger.warning("Host key changed for %s: %s is now %s",
                       exc.hostname, exc.old_fingerprint, exc.new_fingerprint)
        raise HTTPException(status_code=409,
                            detail={"host_key": exc.as_dict()}) from exc
    except InteractiveRequired as exc:
        # Not a failure: the device wants an answer only the user has (#406).
        # 409 with the prompts; the interface asks and posts again with
        # `interactive_answers`.
        logger.info("%s is asking: %s", params.hostname,
                    ", ".join(p["text"] for p in exc.prompts))
        raise HTTPException(status_code=409,
                            detail={"interactive": exc.as_dict()}) from exc
    except ConnectionError_ as exc:
        # Already phrased for the user by the handler.
        logger.info("Connection failed: %s", exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Unexpected error creating session")
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Only remember credentials that actually worked — storing them before the
    # handshake would persist a typo the user is about to correct.
    # A profile's saved forwards start with the session (#405). Failures are
    # reported on the session rather than failing the connect: a port in use
    # on this machine is not a reason to be unable to reach the device.
    if request.profile_id:
        await asyncio.to_thread(_start_profile_forwards, session, request.profile_id)

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


@app.delete("/api/history")
async def history_clear(hostname: str = "", days: int = 0,
                        snapshots: bool = True) -> dict:
    """
    Clear recorded history, optionally for one device or older than N days.

    Scoped by the same two filters the panel already has, so "clear" means
    "clear what I am looking at" rather than opening a second, differently
    shaped question. Choosing a device and then being offered only
    all-or-nothing is how the wrong thing gets deleted.

    `days` is inverted relative to the filter: the panel shows the last N
    days, and this removes everything *older* than N — so clearing while a
    range is selected keeps what is on screen. Nothing here can be undone, so
    the surprising reading is the dangerous one.
    """
    if days < 0:
        raise HTTPException(status_code=400, detail="days cannot be negative")

    before = (time.time() - days * 86400) if days else None
    removed = await asyncio.to_thread(
        store.clear_history, hostname, before, snapshots)
    return {"status": "ok", **removed}


@app.get("/api/history/commands")
async def history_commands(hostname: str = "", q: str = "",
                           limit: int = 50) -> dict:
    """
    Distinct commands run on a device, newest first (#522).

    What Ctrl+R in a terminal reads. The commands you ran on this switch last
    month are the ones you want again, and ShellMate had them recorded with no
    way to reach them short of searching the history panel.
    """
    commands = await asyncio.to_thread(
        store.recent_commands, hostname.strip(), q.strip(), limit)
    return {"hostname": hostname, "count": len(commands), "commands": commands}


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


@app.get("/api/configs/archive")
async def config_archive_summary() -> dict:
    """
    Describe the configuration archive: where it is and what is in it.

    Declared before ``/api/configs/{hostname}``, which would otherwise match
    "archive" as a device name and return an empty list — a bug that would
    look like an empty archive rather than a routing mistake.
    """
    return await asyncio.to_thread(config_archive.summary)


@app.post("/api/configs/archive/reveal")
async def config_archive_reveal() -> dict:
    """
    Open the folder holding the archived configurations (#275).

    Declared beside the summary and before ``/api/configs/{hostname}`` for
    the same routing reason.
    """
    from backend.settings_store import config_directory

    folder = config_directory()
    try:
        folder.mkdir(parents=True, exist_ok=True)
        opened = await asyncio.to_thread(desktop.reveal, folder)
    except Exception as exc:
        logger.info("Could not open the configuration archive: %s", exc)
        opened = False
    return {"opened": opened, "folder": str(folder)}


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


class BaselineRequest(BaseModel):
    """Body for POST /api/configs/baseline."""

    hostname: str = ""
    snapshot_id: int = 0
    note: str = ""


@app.get("/api/configs/baseline/{hostname}")
async def config_baseline_get(hostname: str) -> dict:
    """
    The pinned baseline for a device, or an empty object.

    Content is not returned: the list already carries what the interface needs
    to mark which row is pinned, and the configuration itself is available
    through the snapshot endpoint like any other.
    """
    baseline = await asyncio.to_thread(store.get_baseline, hostname)
    if not baseline:
        return {}
    return {
        "snapshot_id": baseline.get("id"),
        "captured_at": baseline.get("captured_at"),
        "set_at":      baseline.get("baseline_set_at"),
        "note":        baseline.get("baseline_note", ""),
        "line_count":  baseline.get("line_count"),
    }


@app.post("/api/configs/baseline")
async def config_baseline_set(request: BaselineRequest) -> dict:
    """
    Pin a snapshot as what this device should be measured against.

    "Since your last visit" is an accident of when somebody logged in, and
    looking at a device consumes it. A baseline is a moment somebody chose.
    """
    snapshot = await asyncio.to_thread(store.get_snapshot, request.snapshot_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="No such snapshot")

    hostname = request.hostname or snapshot.get("hostname", "")
    if not hostname:
        raise HTTPException(status_code=400,
                            detail="That snapshot has no device to pin it to.")

    await asyncio.to_thread(store.set_baseline, hostname,
                            request.snapshot_id, request.note)
    return {"status": "ok", "hostname": hostname,
            "snapshot_id": request.snapshot_id}


@app.delete("/api/configs/baseline/{hostname}")
async def config_baseline_clear(hostname: str) -> dict:
    """Unpin, going back to comparing against the last visit."""
    return {"status": "ok",
            "cleared": await asyncio.to_thread(store.clear_baseline, hostname)}


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
    try:
        report = await asyncio.to_thread(drift_report, session)
        # Kept on the session so the assistant can be told what changed
        # without capturing the configuration a second time (#549).
        session["drift"] = report
        # And what the capture learned about the device (#536): kept against
        # the saved connection, so the estate answers "which version is
        # where" without opening anything.
        facts = (report.get("capture") or {}).get("inventory") or report.get("inventory") or {}
        if facts:
            await asyncio.to_thread(
                profiles_module.record_inventory,
                session.get("address") or session.get("hostname") or "",
                int(session.get("port") or 0), session.get("username") or "", facts,
                session.get("profile_id") or "",
            )
        return report
    except (OSError, ConnectionError_) as exc:
        # The device dropped the session while the capture ran; that is a
        # state of the world, not a server fault.
        raise HTTPException(status_code=409, detail=f"Could not capture the configuration: {exc}") from exc


# ---------------------------------------------------------------------------
# REST — Telling ShellMate what a device is
# ---------------------------------------------------------------------------


class IdentifyRequest(BaseModel):
    """Body for POST /api/sessions/{session_id}/platform."""

    platform: str


@app.post("/api/sessions/{session_id}/platform")
async def set_session_platform(session_id: str, request: IdentifyRequest) -> dict:
    """
    Identify this session's device as a platform the user has named.

    Automatic identification is deliberately cautious, and there are two
    common devices it cannot win against: one whose banner is a legal warning
    rather than a version string, and anything behind a terminal server.  Both
    are identified from the prompt alone, score below the acting threshold and
    are correctly sent nothing — which leaves paging-off switched on and doing
    nothing at all.  This is the way out of that.

    Applies immediately: aliases and reload patterns switch to the chosen
    platform, and the paging command is sent if the setting allows it.  What
    was sent comes back in the response so the interface can say so — a
    command typed into someone's live session is never silent here.
    """
    session = _require_session(session_id)

    try:
        fingerprint = as_chosen(request.platform)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    terminal_settings = get_settings().get("terminal", {})
    summary = summarise(
        fingerprint, auto_paging=bool(terminal_settings.get("auto_paging_off", True))
    )

    session["fingerprint"] = summary

    # Version and model are known the moment the device is identified;
    # keep them against the saved connection (#536) rather than losing
    # them with the tab.
    try:
        facts = {k: str((summary.get("device") or {}).get(k) or "")
                 for k in ("version", "model")}
        if any(facts.values()):
            await asyncio.to_thread(
                profiles_module.record_inventory,
                session.get("address") or "", int(session.get("port") or 0),
                session.get("username") or "", facts,
                    session.get("profile_id") or "")
    except Exception as exc:
        logger.debug("Could not record what the device is: %s", exc)
    session["pipeline"].platform = summary["platform"]
    session["alerts"].platform = summary["platform"]
    # Onboarding is now moot; stop it overwriting a decision the user made.
    session["onboarder"].stand_down()

    handler = session["handler"]
    if summary["paging_command"] and handler.is_connected:
        await asyncio.to_thread(
            handler.send, (summary["paging_command"] + "\r").encode()
        )

    # Remember it, so the next connection starts where this one ended up.
    # This is the one source that is not a guess, and it was being discarded
    # on disconnect while every guess stayed in the database.
    remembered = await asyncio.to_thread(
        profiles_module.remember_platform,
        # The address, not the name. The device has already announced itself
        # by the time anybody sets a platform by hand, so `hostname` is
        # "S3-R1" and no profile is filed under that.
        session.get("address") or session.get("hostname", ""),
        session.get("port", 0),
        session.get("username", ""), summary["platform"],
    )
    summary["remembered_saved"] = remembered

    logger.info("Session %s identified as %s by the user%s", session_id[:8],
                summary["profile_name"],
                f"; sent '{summary['paging_command']}'" if summary["paging_command"]
                else "")
    return summary


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


class VaultBackupRequest(BaseModel):
    """Body for POST /api/vault/backup."""

    passphrase: str = ""


class VaultRestoreRequest(BaseModel):
    """Body for POST /api/vault/restore."""

    #: The backup file's contents, read by the browser rather than uploaded.
    text: str = ""
    passphrase: str = ""
    #: False merges into whatever is here; True replaces it outright.
    replace: bool = False


@app.post("/api/vault/backup")
async def vault_backup(request: VaultBackupRequest) -> dict:
    """
    Write a passphrase-protected copy of the whole vault (#565).

    Saved where the user chooses through the platform's folder dialog, and
    into the data folder when there is no native window to raise one. The file
    holds every secret in the installation, so it goes through the vault's own
    atomic write and is named for what it is.
    """
    folder = ""
    if desktop.has_native_window():
        folder = await asyncio.to_thread(
            desktop.pick_directory, "Save the vault backup to…")
        if not folder:
            return {"cancelled": True}
    try:
        target = await asyncio.to_thread(
            vault.write_backup, folder or paths.data_dir(), request.passphrase)
    except VaultError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if folder:
        with contextlib.suppress(Exception):
            await asyncio.to_thread(desktop.reveal, folder)
    return {"path": str(target), "status": vault.status()}


@app.post("/api/vault/restore")
async def vault_restore(request: VaultRestoreRequest) -> dict:
    """Merge or replace this machine's vault from a backup file (#565)."""
    try:
        document = json.loads(request.text or "")
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail="That file is not a ShellMate vault backup.") from exc
    try:
        result = await asyncio.to_thread(
            vault.import_backup, document, request.passphrase, request.replace)
    except VaultError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {**result, "status": vault.status()}


@app.post("/api/vault/set-aside")
async def vault_set_aside() -> dict:
    """
    Rename a vault this machine cannot read and start a new one (#565).

    Renamed, never deleted: unreadable here is not unreadable everywhere, and
    the account that wrote it can still open it.
    """
    try:
        target = await asyncio.to_thread(vault.set_aside)
    except VaultError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"path": str(target) if target else "", "status": vault.status()}


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


# ---------------------------------------------------------------------------
# Managing saved credentials
#
# A remembered password used to be invisible from the moment it was saved.
# Nothing listed them, nothing changed one, and removing one meant finding the
# connection it belonged to and reconnecting with the box unticked.
#
# One rule governs everything below: the *listing* never carries a secret, and
# exactly one endpoint returns one — the reveal, which reads the plaintext file
# and refuses for anything encrypted. That asymmetry is not an oversight to be
# tidied up later. A vault that decrypts on demand for the interface is most of
# the way to not being a vault; a plaintext credential is already readable in a
# text file, so refusing to show it in the interface protects nothing and only
# sends people to a text editor.
# ---------------------------------------------------------------------------


class CredentialWriteRequest(BaseModel):
    """Body for PUT /api/credentials/{profile_id}/{field}."""

    value: str = ""
    #: "vault" or "plaintext". Where it should end up.
    storage: str = "vault"


def _credential_rows() -> dict:
    """Every saved credential, described but never disclosed."""
    locked = vault.is_locked()
    entries = []

    # One read of the plaintext store for the whole listing (#328), not one
    # per profile — the identical per-row-reload pattern get_profiles() had.
    plaintext_store = profiles_module._load_plaintext()
    for profile in get_profiles():
        profile_id = profile.get("id", "")
        for field, storage in profiles_module.credential_fields(
                profile_id, plaintext_store).items():
            entries.append({
                "profile_id":      profile_id,
                "profile_name":    profile.get("name") or profile.get("hostname") or "",
                "target": (profile.get("serial_port")
                           if profile.get("connection_type") == "serial"
                           else profile.get("hostname")) or "",
                "username":        profile.get("username") or "",
                "connection_type": profile.get("connection_type") or "ssh",
                "field":           field,
                "field_label":     profiles_module.FIELD_LABELS.get(field, field),
                "storage":         storage,
                # The interface shows a Reveal button on exactly these.
                "can_reveal":      storage == "plaintext",
            })

    return {
        "entries": entries,
        "vault_locked": locked,
        "vault_mode": vault.status().get("mode", ""),
        # Said plainly, because a locked vault can only report the plaintext
        # ones — and a short list that looks complete is worse than a warning.
        "note": ("The vault is locked, so credentials kept in it are not "
                 "listed. Unlock it to see the full picture.") if locked else "",
    }


@app.get("/api/credentials")
async def list_credentials() -> dict:
    """List saved device credentials — which, and in which store. No values."""
    return await asyncio.to_thread(_credential_rows)


@app.post("/api/credentials/{profile_id}/{field}/reveal")
async def reveal_credential(profile_id: str, field: str) -> dict:
    """
    Show one credential that was saved unencrypted.

    Refuses for anything in the vault rather than decrypting it. POST rather
    than GET so it cannot be reached by a link, a prefetch or a cache.
    """
    def _read() -> dict:
        # STORED_SECRETS, not CREDENTIAL_FIELDS: the enable password (#532) is
        # a secret kept for a connection without being one it logs in with,
        # and a plaintext credential the panel lists but refuses to show is a
        # button that does nothing.
        if field not in profiles_module.STORED_SECRETS:
            raise HTTPException(status_code=404, detail=f"No such credential: {field}")

        where = profiles_module.credential_fields(profile_id).get(field, "")
        if where == "vault":
            raise HTTPException(
                status_code=400,
                detail="That credential is encrypted and cannot be displayed. "
                       "Delete it and save a new one if you no longer know it.")
        if where != "plaintext":
            raise HTTPException(status_code=404, detail="Nothing is saved there.")

        logger.warning("Revealing the plaintext %s for profile %s at the "
                       "user's request", field, profile_id)
        return {"field": field,
                "value": profiles_module.read_plaintext_credential(profile_id, field)}

    return await asyncio.to_thread(_read)


@app.put("/api/credentials/{profile_id}/{field}")
async def write_credential(profile_id: str, field: str,
                           request: CredentialWriteRequest) -> dict:
    """Save or change one credential without reconnecting to the device."""
    def _write() -> dict:
        try:
            where = profiles_module.set_credential(
                profile_id, field, request.value, request.storage)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        if request.storage == "plaintext" and where == "plaintext":
            logger.warning("Saving the %s for profile %s in PLAIN TEXT at the "
                           "user's request", field, profile_id)
        return {"status": "ok", "storage": where}

    return await asyncio.to_thread(_write)


@app.delete("/api/credentials/{profile_id}/{field}")
async def delete_credential(profile_id: str, field: str) -> dict:
    """Forget one credential, from whichever store holds it."""
    def _delete() -> dict:
        try:
            return {"status": "ok",
                    "removed": profiles_module.forget_credential(profile_id, field)}
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    return await asyncio.to_thread(_delete)


@app.post("/api/credentials/{profile_id}/encrypt")
async def encrypt_credentials(profile_id: str) -> dict:
    """
    Move a profile's plaintext credentials into the vault.

    Offered in one direction only. There is no case where moving a credential
    *out* of encryption is something somebody needed a button for.
    """
    def _move() -> dict:
        moved = profiles_module.move_to_vault(profile_id)
        if not moved and vault.is_locked():
            raise HTTPException(
                status_code=400,
                detail="The vault is locked, so nothing was moved. The "
                       "plaintext copy has been left where it is.")
        return {"status": "ok", "moved": moved}

    return await asyncio.to_thread(_move)


class CredentialSetRequest(BaseModel):
    """Body for POST /api/credential-sets."""

    name: str = ""
    username: str = ""
    password: str = ""
    storage: str = "vault"
    #: Given, this updates an existing set rather than creating one.
    id: str = ""


class AttachSetRequest(BaseModel):
    """Body for PUT /api/profiles/{profile_id}/credential-set."""

    credential_ref: str = ""


@app.put("/api/profiles/{profile_id}/credential-set")
async def attach_credential_set_endpoint(
        profile_id: str, request: AttachSetRequest) -> dict:
    """
    Point a saved connection at a named credential, or at nothing.

    `attach_credential_set` already existed and only the scanner could reach
    it, so a connection saved any other way could never be given a shared
    login without editing profiles.json. A reference, never a copy: forty
    copies of one lab password is forty entries to update the day it changes,
    with nothing recording they were ever the same credential.
    """
    ok = await asyncio.to_thread(
        profiles_module.attach_credential_set, profile_id, request.credential_ref)
    if not ok:
        raise HTTPException(status_code=404, detail="No such connection")
    return {"status": "ok", "credential_ref": request.credential_ref}


@app.get("/api/credential-sets")
async def list_credential_sets() -> dict:
    """
    Named credentials, and how many connections use each. No values.

    Same promise as every other listing: this says a credential exists and
    where it is kept, and nothing anywhere returns the credential itself
    except the one reveal endpoint, which refuses for anything encrypted.
    """
    return {"sets": await asyncio.to_thread(profiles_module.credential_sets),
            "vault_locked": vault.is_locked()}


@app.post("/api/credential-sets")
async def save_credential_set(request: CredentialSetRequest) -> dict:
    """Create or update a credential that several connections can share."""
    def _save() -> dict:
        try:
            return profiles_module.save_credential_set(
                request.name, request.username, request.password,
                request.storage, request.id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return await asyncio.to_thread(_save)


@app.delete("/api/credential-sets/{set_id}")
async def delete_credential_set(set_id: str) -> dict:
    """
    Forget a named credential and detach every connection using it.

    Detaching matters: a profile left pointing at a credential that no longer
    exists would keep reporting itself ready to connect with nothing to
    connect with.
    """
    detached = await asyncio.to_thread(
        profiles_module.delete_credential_set, set_id)
    return {"status": "ok", "detached": detached}


@app.delete("/api/credentials/plaintext")
async def forget_plaintext_credentials() -> dict:
    """Empty the plaintext credential store entirely."""
    def _forget() -> dict:
        count = profiles_module.forget_all_plaintext()
        logger.warning("Forgot every plaintext credential (%d profiles)", count)
        return {"status": "ok", "profiles": count}

    return await asyncio.to_thread(_forget)


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
    # Streamed from the spooled upload, not read into memory whole (#480).
    size = getattr(file, "size", None)
    try:
        return await asyncio.to_thread(sftp.write_file, session, path, file.file, size)
    except ConnectionError_ as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _start_profile_forwards(session_view: dict, profile_id: str) -> None:
    profile = next((p for p in get_profiles() if p.get("id") == profile_id), None)
    wanted = (profile or {}).get("forwards") or []
    if not wanted:
        return
    session = session_manager.get_session(session_view.get("session_id", ""))
    if not session:
        return
    try:
        manager = forwards_module.manager_for(session)
    except ConnectionError_:
        return
    for spec in wanted:
        try:
            manager.add(spec.get("kind", "local"), int(spec.get("listen_port", 0)),
                        spec.get("host", ""), int(spec.get("port", 0) or 0))
        except Exception as exc:
            logger.warning("Forward from profile %s not started: %s", profile_id, exc)


class ForwardRequest(BaseModel):
    kind: str = "local"
    listen_port: int
    host: str = ""
    port: int = 0
    remember: bool = False


@app.get("/api/sessions/{session_id}/forwards")
async def list_forwards(session_id: str) -> dict:
    """The port forwards a session holds (#405)."""
    session = _require_session(session_id)
    manager = session.get("forwards")
    return {"forwards": manager.list() if manager else [],
            "limit": int(advanced_setting("ssh.max_forwards"))}


@app.post("/api/sessions/{session_id}/forwards")
async def add_forward(session_id: str, request: ForwardRequest) -> dict:
    """Start a forward on a session, and optionally save it to its profile."""
    session = _require_session(session_id)
    try:
        manager = forwards_module.manager_for(session)
        forward = await asyncio.to_thread(
            manager.add, request.kind, request.listen_port, request.host, request.port)
    except ConnectionError_ as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if request.remember and session.get("profile_id"):
        spec = {"kind": request.kind, "listen_port": request.listen_port,
                "host": request.host, "port": request.port}
        await asyncio.to_thread(profiles_module.set_forwards, session["profile_id"], spec, True)
    return forward


@app.delete("/api/sessions/{session_id}/forwards/{forward_id}")
async def remove_forward(session_id: str, forward_id: str, forget: bool = False) -> dict:
    """Stop a forward; with forget=true also drop it from the profile."""
    session = _require_session(session_id)
    manager = session.get("forwards")
    listed = next((f for f in (manager.list() if manager else []) if f["id"] == forward_id), None)
    removed = bool(manager and manager.remove(forward_id))
    if forget and listed and session.get("profile_id"):
        spec = {k: listed[k] for k in ("kind", "listen_port", "host", "port")}
        await asyncio.to_thread(profiles_module.set_forwards, session["profile_id"], spec, False)
    return {"removed": removed}


class ConfigTextRequest(BaseModel):
    text: str
    fresh: bool = False
    save: bool = False
    force: bool = False


@app.post("/api/configs/{session_id}/preview")
async def config_preview(session_id: str, request: ConfigTextRequest) -> dict:
    """What applying the text would change, without sending anything (#407)."""
    session = _require_session(session_id)
    try:
        return await asyncio.to_thread(config_push.preview, session, request.text, request.fresh)
    except ConnectionError_ as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/configs/{session_id}/apply")
async def config_apply(session_id: str, request: ConfigTextRequest) -> dict:
    """Send the lines into the live session, then capture and diff."""
    session = _require_session(session_id)
    try:
        return await asyncio.to_thread(config_push.apply, session, request.text,
                                       request.save, request.force)
    except ConnectionError_ as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/configs/{session_id}/restore/{snapshot_id}")
async def config_restore_proposal(session_id: str, snapshot_id: int) -> dict:
    """A proposed change back to an earlier capture, for the engineer to read."""
    session = _require_session(session_id)
    try:
        return await asyncio.to_thread(config_push.restore_proposal, session, snapshot_id)
    except ConnectionError_ as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/groups/{key:path}/backup/run")
async def run_group_backup(key: str) -> dict:
    """Back up every device in a group now (#408)."""
    try:
        return await asyncio.to_thread(scheduler.run_now, key)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


class SftpRenameRequest(BaseModel):
    path: str
    new_path: str


class SftpPathRequest(BaseModel):
    path: str


class SftpModeRequest(BaseModel):
    path: str
    mode: str


@app.post("/api/sftp/{session_id}/rename")
async def sftp_rename(session_id: str, request: SftpRenameRequest) -> dict:
    """Rename or move a remote file or directory (#418)."""
    session = _require_session(session_id)
    try:
        return await asyncio.to_thread(sftp.rename, session, request.path, request.new_path)
    except ConnectionError_ as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/sftp/{session_id}/mkdir")
async def sftp_mkdir(session_id: str, request: SftpPathRequest) -> dict:
    """Create a remote directory (#418)."""
    session = _require_session(session_id)
    try:
        return await asyncio.to_thread(sftp.make_directory, session, request.path)
    except ConnectionError_ as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/sftp/{session_id}/chmod")
async def sftp_chmod(session_id: str, request: SftpModeRequest) -> dict:
    """Change permissions on a remote path (#418)."""
    session = _require_session(session_id)
    try:
        return await asyncio.to_thread(sftp.set_permissions, session, request.path, request.mode)
    except ConnectionError_ as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/api/sftp/{session_id}/directory")
async def sftp_rmdir(session_id: str, path: str) -> dict:
    """Delete a remote directory and its contents (#418)."""
    session = _require_session(session_id)
    try:
        return await asyncio.to_thread(sftp.delete_directory, session, path)
    except ConnectionError_ as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/sftp/{session_id}/download-directory")
async def sftp_download_directory(session_id: str, path: str) -> Response:
    """Download a remote directory as a zip (#418)."""
    session = _require_session(session_id)
    try:
        data = await asyncio.to_thread(sftp.read_directory_zip, session, path)
    except ConnectionError_ as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    filename = re.sub(r'[^\w\-. ]', "_", path.rstrip("/").rsplit("/", 1)[-1] or "folder") + ".zip"
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.delete("/api/sftp/{session_id}/file")
async def sftp_delete(session_id: str, path: str) -> dict:
    """Delete a remote file."""
    session = _require_session(session_id)
    try:
        return await asyncio.to_thread(sftp.delete_file, session, path)
    except ConnectionError_ as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Network discovery
#
# The first feature that reaches *out* across the network on its own
# initiative, rather than to a device the user named. The server still binds
# to 127.0.0.1 and nothing about that changes — but the posture is different
# enough to be worth stating where the code is, not only in the panel.
#
# Everything is bounded, and every bound is a Stockton setting: how many
# probes are in flight, how long each waits, how many addresses one sweep may
# cover, and how long the whole thing may run. A sweep that cannot be stopped
# is a sweep people will not start.
# ---------------------------------------------------------------------------


class DiscoveryScanRequest(BaseModel):
    """Body for POST /api/discovery/scans."""

    targets: str = ""
    #: Overrides the Stockton default for this scan only.
    ports: str = ""


class DiscoverySaveRequest(BaseModel):
    """Body for POST /api/discovery/save."""

    #: Rows from a scan result, each with at least an address.
    devices: list[dict] = []
    #: Applied to every device saved. A scan cannot know the username; the
    #: person running it does, and is otherwise about to type it forty times.
    username: str = ""
    #: A named credential to attach to all of them. Forty devices found on one
    #: subnet almost always share one login.
    credential_ref: str = ""


def _discovery_settings() -> dict:
    return {
        "concurrency": advanced_setting("discovery.concurrency"),
        "timeout":     advanced_setting("discovery.timeout"),
        "fetch_http":  advanced_setting("discovery.fetch_http"),
        "max_seconds": advanced_setting("discovery.max_seconds"),
    }


def _discovery_ports(requested: str) -> list[int]:
    """Parse a port list, falling back to the configured default."""
    text = requested or str(advanced_setting("discovery.ports"))
    ports = []
    for piece in re.split(r"[,\s]+", text.strip()):
        try:
            port = int(piece)
        except ValueError:
            continue
        if 1 <= port <= 65535 and port not in ports:
            ports.append(port)
    return ports or [22]


@app.get("/api/discovery/subnets")
async def discovery_subnets() -> dict:
    """The subnets this machine is on, offered as the default target."""
    return {
        "subnets": await asyncio.to_thread(discovery.local_subnets),
        "max_hosts": advanced_setting("discovery.max_hosts"),
        "ports": str(advanced_setting("discovery.ports")),
    }


@app.post("/api/discovery/preview")
async def discovery_preview(request: DiscoveryScanRequest) -> dict:
    """
    How many addresses a target specification comes to, without scanning.

    So the confirmation before a large sweep can name the number. "Scan
    10.0.0.0/16?" means nothing; "that is 65,534 addresses" means quite a lot.
    """
    try:
        targets = discovery.parse_targets(
            request.targets, int(advanced_setting("discovery.max_hosts")))
    except discovery.TargetError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {"count": len(targets), "first": targets[:3], "last": targets[-1]}


@app.post("/api/discovery/scans")
async def discovery_start(request: DiscoveryScanRequest) -> dict:
    """Start a sweep. Returns at once; poll the scan for progress."""
    try:
        targets = discovery.parse_targets(
            request.targets, int(advanced_setting("discovery.max_hosts")))
    except discovery.TargetError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    ports = _discovery_ports(request.ports)
    logger.info("Discovery: scanning %d address(es) on ports %s",
                len(targets), ports)
    scan = discovery.start(targets, ports, _discovery_settings())
    return scan.state()


@app.get("/api/discovery/scans/{scan_id}")
async def discovery_progress(scan_id: str) -> dict:
    """Progress and results so far. Results appear as they are found."""
    scan = discovery.get(scan_id)
    if scan is None:
        raise HTTPException(status_code=404, detail="That scan is no longer held.")
    return scan.state()


@app.post("/api/discovery/scans/{scan_id}/cancel")
async def discovery_cancel(scan_id: str) -> dict:
    """Stop a sweep. Probes already in flight finish; nothing new starts."""
    if not discovery.cancel(scan_id):
        raise HTTPException(status_code=404,
                            detail="That scan is not running.")
    return {"status": "ok"}


@app.delete("/api/discovery/scans/{scan_id}")
async def discovery_forget(scan_id: str) -> dict:
    """
    Discard a scan and its results.

    Reaching the server matters: the last few finished scans are held in
    memory, so blanking the view alone would tidy the display and leave the
    data exactly where it was.
    """
    return {"status": "ok", "discarded": discovery.forget(scan_id)}


@app.post("/api/discovery/save")
async def discovery_save(request: DiscoverySaveRequest) -> dict:
    """
    Save discovered devices as connection profiles.

    A bulk call rather than new storage: save_profile() already strips secrets
    and, since #73, already refuses to create a second entry for a device it
    has — so a repeated scan updates the existing profiles rather than
    doubling everybody's list.
    """
    def _save() -> dict:
        saved, existing = [], []
        for device in request.devices:
            address = (device.get("address") or "").strip()
            if not address:
                continue

            kind = device.get("suggested_type") or "ssh"
            # Trim the DNS domain from a hostname ("sw1.example.com" → "sw1"),
            # but never from a bare address — splitting an IP on dots named
            # every device on a subnet "192" (#363).
            hostname = (device.get("hostname") or "").strip()
            profile = save_profile({
                "name": hostname.split(".")[0] if hostname else address,
                "hostname": address,
                "port": 22 if kind == "ssh" else 23,
                "username": request.username,
                "connection_type": kind,
                # What the scan worked out, carried across so the profile
                # arrives knowing its platform rather than rediscovering it.
                "platform": device.get("platform") or "",
                "discovered": True,
            })

            # A reference, never a copy. Forty copies of one lab password is
            # forty entries to update the day it changes, with nothing
            # recording that they were ever the same credential.
            if request.credential_ref:
                profiles_module.attach_credential_set(
                    profile["id"], request.credential_ref)

            (existing if profile.get("already_saved") else saved).append(profile)

        return {"saved": len(saved), "already_saved": len(existing),
                "attached": bool(request.credential_ref),
                "profiles": saved + existing}

    result = await asyncio.to_thread(_save)
    logger.info("Discovery: saved %d new profile(s), %d already known",
                result["saved"], result["already_saved"])
    return result


class ReachabilityRequest(BaseModel):
    """Body for POST /api/reachability."""

    #: A group, and everything nested under it.
    group: str = ""
    #: Or a hand-made selection, which wins where both are given.
    profile_ids: list[str] = []


def _reach_target(profile: dict) -> dict:
    """One saved connection as something the prober can take."""
    kind = (profile.get("connection_type") or "ssh").lower()
    address = "" if kind == "serial" else (profile.get("hostname") or "").strip()
    return {
        "id":      profile.get("id", ""),
        "name":    profile.get("name") or address or profile.get("serial_port", ""),
        "address": address,
        "port":    int(profile.get("port") or (23 if kind == "telnet" else 22)),
        "kind":    kind,
        "why":     "a serial console is a cable, not an address" if kind == "serial" else "",
    }


@app.post("/api/reachability")
async def start_reachability(request: ReachabilityRequest) -> dict:
    """
    Check whether a group, or a selection, answers on its own ports (#538).

    An explicit action and never a timer: two hundred sites probed on a
    schedule is traffic somebody's IDS will report. It reuses the discovery
    scan's concurrency, timeout and overall deadline, so a sweep cannot be
    harsher than a scan by accident, and it says "port open" rather than
    "healthy" — a device with a dead control plane still answers a TCP probe.
    """
    def _targets() -> list[dict]:
        if request.profile_ids:
            wanted = set(request.profile_ids)
            chosen = [p for p in profiles_module.get_profiles() if p.get("id") in wanted]
        elif request.group:
            chosen = profiles_module.profiles_tagged(request.group, include_nested=True)
        else:
            chosen = []
        return [_reach_target(profile) for profile in chosen]

    targets = await asyncio.to_thread(_targets)
    if not targets:
        raise HTTPException(status_code=400,
                            detail="There are no connections to check.")
    sweep = discovery.start_sweep(targets, _discovery_settings(),
                                  on_finish=profiles_module.record_last_seen)
    return sweep.state()


@app.get("/api/reachability/{sweep_id}")
async def reachability_progress(sweep_id: str) -> dict:
    """Progress and results so far. Results appear as they arrive."""
    sweep = discovery.get_sweep(sweep_id)
    if sweep is None:
        raise HTTPException(status_code=404, detail="That check is no longer held.")
    return sweep.state()


# ---------------------------------------------------------------------------
# Feedback (#370)
# ---------------------------------------------------------------------------

class FeedbackRequest(BaseModel):
    """Body for POST /api/feedback."""

    kind: str
    title: str
    description: str = ""


@app.post("/api/feedback")
async def send_feedback(request: FeedbackRequest) -> dict:
    """
    File a bug report or feature request.

    The report goes to the feedback relay (which turns it into a GitHub
    issue), or into the local outbox when the relay is unreachable — the
    response says which, so the interface never claims "sent" for "saved".
    """
    try:
        return await asyncio.to_thread(
            feedback_module.submit,
            request.kind, request.title, request.description)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.on_event("shutdown")
async def _close_ai_clients() -> None:
    """The shared provider clients (#503)."""
    from backend.ai import http as ai_http
    await ai_http.aclose_all()


@app.on_event("startup")
async def _start_backup_scheduler() -> None:
    """The once-a-minute check for scheduled backups (#408)."""
    scheduler.start()
    # Sessions the last process left open (#464): close the ones with
    # commands, drop the empty ones. Never worth failing startup over.
    try:
        await asyncio.to_thread(store.close_abandoned)
    except Exception as exc:
        logger.warning("History housekeeping at startup: %s", exc)


@app.on_event("startup")
async def _flush_feedback_outbox() -> None:
    """Retry reports queued while the relay was unreachable. Fire and
    forget — a slow relay must not delay the server coming up."""
    asyncio.get_running_loop().run_in_executor(None, feedback_module.flush)


class ProfileForwardsRequest(BaseModel):
    forwards: list[dict] = []


@app.put("/api/profiles/{profile_id}/forwards")
async def set_profile_forwards(profile_id: str, request: ProfileForwardsRequest) -> dict:
    """Replace the forwards a profile starts with (#405)."""
    try:
        return await asyncio.to_thread(profiles_module.replace_forwards, profile_id, request.forwards)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


class OnConnectRequest(BaseModel):
    """Body for PUT /api/profiles/{profile_id}/on-connect."""
    on_connect: list[str] = []


@app.put("/api/profiles/{profile_id}/on-connect")
async def set_profile_on_connect(profile_id: str, request: OnConnectRequest) -> dict:
    """
    Replace the commands a saved connection sends itself on connect (#532).

    Its own endpoint rather than part of a profile save, because saving
    merges and never overwrites with nothing — so emptying the box would
    silently leave the old script in place.
    """
    try:
        profile = await asyncio.to_thread(
            profiles_module.replace_on_connect, profile_id, request.on_connect)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"status": "ok", "on_connect": profile.get("on_connect", [])}


class TagsRequest(BaseModel):
    """Body for PUT /api/profiles/{profile_id}/tags."""
    tags: list[str] = []


# ---------------------------------------------------------------------------
# REST — Groups on the dashboard
#
# A group is a tag with presentation. Membership goes through the tag
# endpoints above; these carry only the name, colour, favourite and position.
# ---------------------------------------------------------------------------


class GroupRequest(BaseModel):
    """Body for creating or updating a group."""

    name: str = ""
    colour: str = ""
    icon: str = ""
    favourite: bool | None = None
    order: int | None = None
    # A backup schedule (#408): {enabled, every, at, day}. None leaves it.
    backup: dict | None = None
    # What the group lends its connections (#545): {username, port, platform,
    # credential_ref, jump_host, jump_port, jump_username}. None leaves them;
    # {} clears them. A secret here is refused, not stripped.
    defaults: dict | None = None


class GroupOrderRequest(BaseModel):
    """The hand-arranged order of the dashboard, front to back."""

    keys: list[str] = []


class GroupMemberRequest(BaseModel):
    """Put a connection in a group, or take it out."""

    profile_id: str
    member: bool = True


# ---------------------------------------------------------------------------
# REST — Terminal colour schemes
# ---------------------------------------------------------------------------


class SchemeRequest(BaseModel):
    """A custom colour scheme."""

    name: str
    label: str = ""
    theme: dict = {}


@app.get("/api/schemes")
async def list_schemes() -> dict:
    """Every scheme, built-in and custom, with the keys a theme needs."""
    schemes = await asyncio.to_thread(schemes_module.all_schemes)
    return {"schemes": schemes, "keys": list(schemes_module.KEYS),
            "default": schemes_module.DEFAULT}


@app.post("/api/schemes")
async def save_scheme(request: SchemeRequest) -> dict:
    """
    Create or replace a custom scheme.

    Forgiving about the theme it is handed: unknown keys are dropped and
    missing ones inherit from the default, because somebody pasting a scheme
    from elsewhere should not have it refused over a stray field.
    """
    try:
        scheme = await asyncio.to_thread(
            schemes_module.save_scheme, request.name, request.label, request.theme)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # Reported, never enforced — see schemes.contrast.
    scheme["contrast"] = schemes_module.contrast(scheme["theme"])
    return scheme


@app.delete("/api/schemes/{name}")
async def delete_scheme(name: str) -> dict:
    """Remove a custom scheme. A built-in can only be shadowed, not deleted."""
    if not await asyncio.to_thread(schemes_module.delete_scheme, name):
        raise HTTPException(status_code=404, detail="No such custom scheme")
    return {"status": "ok"}


@app.get("/api/groups")
async def list_groups_endpoint() -> dict:
    """Every group, including tags that have never been given a colour."""
    return {"groups": await asyncio.to_thread(groups_module.list_groups),
            "colours": list(groups_module.COLOURS),
            # The picker's options come from here rather than from a list in
            # the frontend, so there is one place a name can be added and it
            # is the place the font is subsetted from (#180).
            "icons": list(groups_module.ICONS)}


@app.get("/api/groups/defaults")
async def group_defaults_endpoint(tags: str = "") -> dict:
    """
    What a connection in these groups would inherit, and from where (#545).

    Declared before the /{key:path} routes for the reason set out there, and
    answered by the same function every connect path uses - the dialog must
    not work out inheritance for itself, or it will eventually say one thing
    while the connection does another.
    """
    wanted = profiles_module.normalise_tags((tags or "").split(","))
    return await asyncio.to_thread(groups_module.defaults_for, wanted)


@app.post("/api/groups")
async def create_group_endpoint(request: GroupRequest) -> dict:
    """Create a group, or adopt an existing tag as one."""
    try:
        return await asyncio.to_thread(
            groups_module.create_group, request.name,
            request.colour or groups_module.DEFAULT_COLOUR, request.icon)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.put("/api/groups/order")
async def set_group_order_endpoint(request: GroupOrderRequest) -> dict:
    """
    Store the dashboard arrangement.

    Declared **before** the /{key} route below. FastAPI matches in declaration
    order, so the other way round this path is read as a group named "order" —
    and PUT /api/groups/order would try to rename it, retagging connections.
    """
    return {"groups": await asyncio.to_thread(groups_module.set_order, request.keys)}



# A group key can contain a slash.
#
# Nesting is expressed in the name — `site-004/firewalls` is a branch under
# `site-004` — so the key reaching these routes is a path, not a segment.
# `encodeURIComponent` sends it as %2F, but the server decodes the path before
# routing, so `{key}` matched nothing and every one of these 404'd for any
# subgroup: rename, recolour, pin, delete, membership, connect-all. The
# failure was silent from the interface, which showed the change and then
# re-read the unchanged group.
#
# `{key:path}` matches across the slash. The `/members` route is declared
# first so its literal suffix wins over the greedy match.


@app.post("/api/groups/{key:path}/members")
async def set_group_member_endpoint(key: str, request: GroupMemberRequest) -> dict:
    """
    Add a connection to a group, or remove it.

    Adding, never moving: a connection carries as many groups as it has tags.
    """
    try:
        tags = await asyncio.to_thread(
            groups_module.set_membership, request.profile_id, key, request.member)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"status": "ok", "tags": tags}


@app.put("/api/groups/{key:path}")
async def update_group_endpoint(key: str, request: GroupRequest) -> dict:
    """Rename, recolour, favourite or reposition a group."""
    changes = request.model_dump(exclude_none=True)
    # An empty string is "not given" for these two, but False is a real value
    # for `favourite` — which exclude_none already keeps.
    if not changes.get("name"):
        changes.pop("name", None)
    if not changes.get("colour"):
        changes.pop("colour", None)
    if not changes.get("icon"):
        changes.pop("icon", None)
    try:
        return await asyncio.to_thread(groups_module.update_group, key, changes)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/api/groups/{key:path}")
async def delete_group_endpoint(key: str, connections: str = "keep") -> dict:
    """
    Remove a group. The connections in it survive unless
    ``?connections=delete`` — see groups.delete_group for exactly which go.
    """
    if connections not in ("keep", "delete"):
        raise HTTPException(
            status_code=400,
            detail="connections must be 'keep' or 'delete'")
    return await asyncio.to_thread(
        groups_module.delete_group, key, connections == "delete")


@app.get("/api/profiles/tags")
async def list_tags() -> dict:
    """Every tag in use, with how many connections carry it."""
    return {"tags": await asyncio.to_thread(profiles_module.all_tags)}


@app.put("/api/profiles/{profile_id}/tags")
async def set_profile_tags(profile_id: str, request: TagsRequest) -> dict:
    """Replace a connection's tags."""
    return {"status": "ok",
            "tags": await asyncio.to_thread(
                profiles_module.set_tags, profile_id, request.tags)}


async def _open_profiles(targets: list[dict]) -> list[dict]:
    """
    Open a session to each of these saved connections, and report on each.

    Bounded concurrency, for the same reason broadcast has it: forty
    simultaneous SSH handshakes through one bastion buries it, and a fleet is
    exactly where somebody would use this.

    Each result comes back individually. A partial failure has to be visible
    rather than assumed - the same reasoning broadcast already records, and it
    matters more here because the whole point is not watching each one.

    Shared by the tag route and by a hand-picked selection (#537), so the
    pacing and the per-device reporting cannot come to differ between them.
    """
    # The same reading of broadcast.concurrency as broadcast itself (#333):
    # 0 is documented as "no limit". max(1, 0) turned the default into
    # Semaphore(1), and "Connect all" on fifty devices opened them strictly
    # one at a time - worst case fifty connect timeouts end to end.
    concurrency = int(advanced_setting("broadcast.concurrency"))
    limit = asyncio.Semaphore(concurrency) if concurrency else None
    results: list[dict] = []

    async def open_one(profile: dict) -> None:
        async with (limit or contextlib.nullcontext()):
            # Every connect path resolves what the groups lend the same way
            # (#545): what a tile says and what a tab does must agree.
            dialled = profiles_module.effective(profile)
            kind = dialled.get("connection_type") or "ssh"
            params = ConnectionParams(
                connection_type=kind,
                hostname=dialled.get("hostname", ""),
                port=int(dialled.get("port") or (22 if kind == "ssh" else 23)),
                username=dialled.get("username", ""),
                display_label=dialled.get("name", ""),
                serial_port=dialled.get("serial_port", ""),
                jump_host=dialled.get("jump_host", ""),
                jump_port=int(dialled.get("jump_port") or 22),
                jump_username=dialled.get("jump_username", ""),
                private_key_path=dialled.get("private_key_path", ""),
            )
            for field, value in load_credentials(profile.get("id", "")).items():
                if not getattr(params, field, ""):
                    setattr(params, field, value)
            params.credential_source = "saved"

            try:
                session = await asyncio.to_thread(
                    session_manager.create_session, params,
                    profile.get("id", ""))
                results.append({"ok": True, "name": profile.get("name", ""),
                                "session": session})
            except Exception as exc:
                logger.info("Batch connect failed for %s: %s",
                            profile.get("name", ""), exc)
                results.append({"ok": False, "name": profile.get("name", ""),
                                "error": str(exc)})

    await asyncio.gather(*(open_one(p) for p in targets))
    return results


# Same reason as the group routes above: a tag is a group key, and a
# nested one carries a slash.
@app.post("/api/tags/{tag:path}/connect")
async def connect_tag(tag: str) -> dict:
    """
    Open a session to every connection carrying a tag.

    Paced and reported per device by ``_open_profiles``.
    """
    # Everything beneath it too (#207). A site holds no devices directly —
    # they carry their subgroup's tag only — so an exact match found nothing
    # and "Connect all" on a site reported it was empty, while the tree
    # beside it showed a count of fifty.
    targets = await asyncio.to_thread(
        profiles_module.profiles_tagged, tag, True)
    if not targets:
        raise HTTPException(status_code=404,
                            detail=f"Nothing is tagged '{tag}'.")

    results = await _open_profiles(targets)

    opened = sum(1 for r in results if r["ok"])
    logger.info("Connected %s of %s devices tagged '%s'",
                opened, len(targets), tag)
    return {"tag": tag, "opened": opened, "total": len(targets),
            "results": results}


class ConnectManyRequest(BaseModel):
    """Body for POST /api/sessions/many."""

    profile_ids: list[str] = []


@app.post("/api/sessions/many")
async def connect_many(request: ConnectManyRequest) -> dict:
    """
    Open a session to each of a hand-picked set of connections (#537).

    The tag route above answers "connect this group"; this answers "connect
    these six", which is the question somebody actually has after looking at
    a tree. Same pacing, same per-device reporting.
    """
    wanted = [pid for pid in request.profile_ids if pid]
    saved = {p.get("id"): p for p in await asyncio.to_thread(get_profiles)}
    targets = [saved[pid] for pid in dict.fromkeys(wanted) if pid in saved]
    if not targets:
        raise HTTPException(status_code=404,
                            detail="None of those connections are saved any more.")

    results = await _open_profiles(targets)
    opened = sum(1 for r in results if r["ok"])
    logger.info("Connected %s of %s selected devices", opened, len(targets))
    return {"opened": opened, "total": len(targets), "results": results}


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


def _resolve_local(path: str) -> Path:
    """
    Turn a path from the interface into the path the application would use.

    Relative names are the point of this. `logs` and `configs` are stored
    relative so a ShellMate folder can be copied to another machine and still
    work, and `settings_store` resolves them against `data_dir()`. Python
    resolves them against the *working directory*, which for a double-clicked
    executable is wherever Explorer happened to leave us — so browsing from
    `configs` opened somewhere unrelated, or nowhere at all.

    Absolute paths, including UNC paths to a share, are used exactly as given.
    """
    candidate = Path(path).expanduser()
    return candidate if candidate.is_absolute() else paths.data_dir() / candidate



def _nearest_existing(path: Path) -> Path:
    """The path itself, or the closest folder above it that exists."""
    while not path.exists() and path.parent != path:
        path = path.parent
    return path if path.exists() else Path.home()


class PickFileRequest(BaseModel):
    """Body for POST /api/pick-file."""

    title: str = "Select a file"
    directory: str = ""
    # ("Key files (*.pem;*.ppk)", "All files (*.*)") — the platform dialog's
    # own format, passed through untouched.
    file_types: list[str] = []
    # A folder rather than a file: where captured configurations are archived.
    folder: bool = False


@app.post("/api/pick-file")
async def pick_file(request: PickFileRequest) -> dict:
    """
    Raise the platform's file or folder dialog.

    ``available`` is false when there is no native window, which is not an
    error — it tells the interface to open its own browser instead. Reporting
    that as a failure would leave the button looking broken in exactly the
    situation where a working alternative exists.
    """
    if not desktop.has_native_window():
        return {"available": False, "path": ""}

    # Resolved the same way the in-app picker resolves it, or "configs" would
    # open two different places depending on which picker the machine can show.
    start = str(_nearest_existing(_resolve_local(request.directory)))         if request.directory else ""

    if request.folder:
        chosen = await asyncio.to_thread(
            desktop.pick_directory, request.title, start,
        )
        return {"available": True, "path": chosen or "", "cancelled": not chosen}

    path = await asyncio.to_thread(
        desktop.pick_file, request.title, start, tuple(request.file_types),
    )
    return {"available": True, "path": path or "", "cancelled": not path}


@app.get("/api/local/resolve")
async def local_resolve(path: str = "") -> dict:
    """
    Say where a path in a settings field actually lands.

    "logs" in a text box tells nobody where their logs are. It resolves
    against the data folder, which itself moves depending on whether the
    portable location beside the executable turned out to be writable — so the
    answer is not something a user can work out by reading the box.
    """
    if not path:
        return {"path": "", "resolved": "", "exists": False, "absolute": False}

    resolved = _resolve_local(path)
    return {
        "path": path,
        "resolved": str(resolved),
        "exists": resolved.exists(),
        "absolute": Path(path).expanduser().is_absolute(),
    }


class CreateFolderRequest(BaseModel):
    """Body for POST /api/local/mkdir."""
    path: str


@app.post("/api/local/mkdir")
async def local_mkdir(request: CreateFolderRequest) -> dict:
    """
    Create a folder the user has just been told does not exist.

    Only ever reached from the file picker, after it has said which folder is
    missing and been told to go ahead — so this creates one level and no more.
    `parents=True` would turn a mistyped path into a tree of empty folders
    somebody then has to find and remove.
    """
    target = _resolve_local(request.path)

    def _create() -> dict:
        if target.exists():
            return {"path": str(target), "created": False}
        if not target.parent.exists():
            raise HTTPException(
                status_code=400,
                detail=f"{target.parent} does not exist either. "
                       f"Browse to a folder that does and create it there.")
        try:
            target.mkdir()
        except OSError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"path": str(target), "created": True}

    return await asyncio.to_thread(_create)


@app.get("/api/local/browse")
async def local_browse(path: str = "", hidden: int = 0) -> dict:
    """
    List a directory on this machine, for the in-app file picker.

    Directories and file names only — never contents. Unreadable entries are
    skipped rather than failing the whole listing, because one locked folder
    should not make a directory unbrowsable.
    """
    def _listing() -> dict:
        # Default somewhere useful: this is reached for from the SSH key field.
        base = _resolve_local(path) if path else (Path.home() / ".ssh")

        # A path that does not exist yet is answered from the nearest folder
        # above it that does, with the missing part named. Falling back to the
        # home directory instead — which is what happened before — loses the
        # only piece of information the caller had.
        missing = ""
        if not base.exists():
            missing = str(base)
            while not base.exists() and base.parent != base:
                base = base.parent
            if not base.exists():
                base = Path.home()

        base = base.resolve()

        if base.is_file():
            base = base.parent

        entries = []
        try:
            for item in sorted(base.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
                # ~/.ssh is hidden on every platform, which is exactly where
                # keys live — so this is switchable rather than absolute.
                if not hidden and item.name.startswith("."):
                    continue
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
        return {"path": str(base), "parent": parent, "entries": entries,
                "missing": missing}

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


@app.post("/api/sessions/{session_id}/disconnect")
async def disconnect_session(session_id: str) -> dict:
    """
    End the connection but keep the session.

    Deliberately not `DELETE`, which destroys the session and takes the buffer
    with it. Ending the connection while keeping what is on screen is a
    different thing and the one people ask for: the transcript stays
    readable, and Reconnect is right there — the same state a session that
    dropped on its own leaves behind (#208).

    Idempotent: disconnecting something already disconnected is not an error,
    because "make sure this is closed" is exactly the intent.
    """
    session = session_manager.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    handler = session.get("handler")
    if handler is not None:
        await asyncio.to_thread(handler.disconnect)
    session["is_connected"] = False

    return {"status": "ok", "session_id": session_id}


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
    """
    Return all saved connection profiles.

    Each carries ``ready_to_connect``: whether clicking its tile can open a
    session outright, or has to open the dialog and ask for something first.
    Decided here rather than in the browser because two of the three reasons
    to say no — a locked vault, a COM port that is no longer plugged in — are
    only knowable on this side.
    """
    profiles = await asyncio.to_thread(get_profiles)

    # Enumerated once, and only when something needs it: listing serial ports
    # touches the registry on Windows and is not free.
    serial_ports: set[str] | None = None

    for profile in profiles:
        kind = (profile.get("connection_type") or "ssh").lower()

        if kind == "serial":
            if serial_ports is None:
                try:
                    serial_ports = {
                        (port.get("device") or "").upper()
                        for port in await asyncio.to_thread(available_ports)
                    }
                except Exception:
                    serial_ports = set()
            ready = (profile.get("serial_port") or "").upper() in serial_ports
            profile["reason_not_ready"] = "" if ready else "The COM port is not connected."

        elif kind == "telnet":
            # Telnet devices commonly prompt for credentials in-band, so having
            # none saved is normal rather than a reason to ask.
            ready = True
            profile["reason_not_ready"] = ""

        else:
            if not profile.get("has_saved_credentials"):
                ready = False
                profile["reason_not_ready"] = "No saved password."
            elif profile.get("credential_storage") == "vault" and vault.is_locked():
                ready = False
                profile["reason_not_ready"] = "The vault is locked."
            else:
                ready = True
                profile["reason_not_ready"] = ""

        profile["ready_to_connect"] = ready

    return profiles


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


class ProfileImportRequest(BaseModel):
    """Body for POST /api/profiles/import."""

    #: The CSV itself — pasted from a spreadsheet or read from a file.
    text: str = ""
    #: False previews, True writes. The same parse either way, so the counts
    #: shown before cannot disagree with what happens after.
    apply: bool = False


class ProfileExportRequest(BaseModel):
    """Body for POST /api/profiles/export."""

    #: One group and everything nested under it. Empty means the estate.
    group: str = ""


@app.post("/api/profiles/import")
async def import_profiles(request: ProfileImportRequest) -> dict:
    """
    Read an estate out of a CSV (#535), previewing it or saving it.

    A file carrying a password column is refused outright rather than having
    the column stripped — see profiles.import_csv.
    """
    try:
        return await asyncio.to_thread(
            profiles_module.import_csv, request.text, request.apply)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/profiles/export.csv")
async def export_profiles_csv(group: str = "") -> Response:
    """The estate, or one group, as CSV. Never carries a secret."""
    text = await asyncio.to_thread(profiles_module.export_csv, group)
    stem = "shellmate-" + (re.sub(r"[^a-z0-9._-]+", "-", group.lower()) or "connections")
    return Response(
        content=text, media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{stem}.csv"'})


@app.post("/api/profiles/export")
async def save_profiles_csv(request: ProfileExportRequest) -> dict:
    """
    Save the CSV where the user chooses, through the platform's own folder
    dialog — the shape POST /api/logs/{filename}/save established, because in
    the native window a browser download does nothing anybody can see.

    Answers 409 without a native window, and the page falls back to fetching
    /api/profiles/export.csv as a blob.
    """
    if not desktop.has_native_window():
        raise HTTPException(status_code=409, detail="No native window; use the browser download.")
    folder = await asyncio.to_thread(desktop.pick_directory, "Export connections to…")
    if not folder:
        return {"cancelled": True}

    text = await asyncio.to_thread(profiles_module.export_csv, request.group)
    stem = "shellmate-" + (re.sub(r"[^a-z0-9._-]+", "-", request.group.lower()) or "connections")
    target = Path(folder) / f"{stem}.csv"
    try:
        # utf-8-sig: Excel reads a plain UTF-8 CSV as the system code page and
        # mangles every non-ASCII device name in the estate.
        await asyncio.to_thread(target.write_text, text, "utf-8-sig")
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Could not write the file: {exc}") from exc
    with contextlib.suppress(Exception):
        await asyncio.to_thread(desktop.reveal, str(Path(folder)))
    return {"path": str(target)}


class BulkProfileRequest(BaseModel):
    """Body for PUT /api/profiles/bulk."""

    profile_ids: list[str] = []
    #: Only the fields being changed. An absent one is left alone; one sent
    #: empty is cleared. profiles.BULK_FIELDS says which are allowed, and a
    #: secret or an inventory fact is refused rather than dropped.
    changes: dict = {}


@app.put("/api/profiles/bulk")
async def update_profiles_bulk(request: BulkProfileRequest) -> dict:
    """
    Change the same fields on many saved connections at once (#537).

    Declared **before** any /api/profiles/{id} route for the reason the group
    routes record: FastAPI matches in declaration order, and "bulk" would
    otherwise be read as a profile id.
    """
    try:
        return await asyncio.to_thread(
            profiles_module.update_many, request.profile_ids, request.changes)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/api/profiles/untagged")
async def delete_untagged_profiles() -> dict:
    """Delete every connection that is in no group (#454). Returns the count."""
    deleted = await asyncio.to_thread(profiles_module.delete_untagged)
    return {"deleted": deleted}


@app.delete("/api/profiles/{profile_id}")
async def remove_profile(profile_id: str) -> dict:
    """Delete a saved profile."""
    if not delete_profile(profile_id):
        raise HTTPException(status_code=404, detail="Profile not found")
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# REST — Settings
# ---------------------------------------------------------------------------

@app.get("/api/health")
async def health() -> dict:
    """
    The liveness probe, and nothing else.

    Public even when auth is on (#329): `wait_until_serving()` and the
    single-instance probe both poll before anyone can have logged in, and a
    401 read as "not started" — the windowed build refused to launch, and a
    second instance treated the lock file as stale and started over the same
    data directory. This carries only the marker; `/api/system/info` keeps
    its filesystem paths behind the login.
    """
    return {"app": "shellmate-portable", "version": app_version.VERSION}


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
        # When this copy was built.
        #
        # A --onefile binary carries no visible clue that the source beside it
        # has moved on, so an executable twenty minutes older than a fix is
        # indistinguishable from the fix not working. That cost real time
        # once, so it is reported rather than left to be inferred.
        "built":          _build_time(),
        # The release and the commit (#420): the two things a bug report
        # needs that a timestamp cannot give.
        "version":        app_version.build_info()["version"],
        "commit":         app_version.build_info()["commit"],
        "describe":       app_version.describe(),
    }


@app.get("/api/system/checks")
async def system_checks(probes: bool = False) -> dict:
    """
    Run the self-checks and say whether this install is healthy (#562).

    `probes=true` adds the two that go over the network. They are opt-in and
    per request rather than on every Settings open, because ShellMate promises
    to work air-gapped and a panel that reaches out the moment it is opened
    spends that promise on the user's behalf.
    """
    from backend import diagnostics

    rows = await asyncio.to_thread(diagnostics.run, probes)
    return {"checks": rows, "summary": diagnostics.summarise(rows),
            "probed": bool(probes)}


@app.get("/api/system/update")
async def update_check() -> dict:
    """
    Compare this build with the latest GitHub release (#420).

    Only ever on request — the button in Diagnostics, or the startup check
    when `diag.update_check` is switched on. Nothing leaves the machine but
    the version number in the User-Agent, and on an air-gapped site the
    answer is "could not reach GitHub", not a stall: six seconds, then give
    up. The lookup is the updater's own, so the check and the download
    agree on what "latest" means on the chosen channel (#567).
    """
    current = app_version.build_info()["version"]
    try:
        release = await asyncio.to_thread(
            updater.latest_release, app_version.RELEASES_REPO, None, 6.0)
    except httpx.HTTPError as exc:
        return {"current": current,
                "error": f"Could not reach GitHub ({exc.__class__.__name__})."}
    except Exception as exc:
        return {"current": current, "error": str(exc)}
    if not release.get("version"):
        return {"current": current, "latest": "", "newer": False,
                "channel": release.get("channel", updater.channel()),
                "note": release.get("note") or "No release has been published yet."}
    lic = licence_module.status()
    return {
        "current":   current,
        "latest":    release["version"],
        "newer":     app_version.is_newer(release["version"], current),
        "prerelease": release.get("prerelease", False),
        "channel":   release.get("channel", "stable"),
        "url":       release.get("url", ""),
        "published": release.get("published", ""),
        # For the in-app modal (#442): the notes, the size, and whether the
        # download may be started from here (#448).
        "notes":     release.get("notes", ""),
        "size":      release.get("size", 0),
        "has_checksum": bool(release.get("checksum_url")),
        "licence":   {"valid": lic["valid"], "state": lic["state"], "detail": lic["detail"]},
    }


# The desktop shell registers its quit here (#452), so the status bar's Exit
# ends the tray, the window and the server together — the same path the tray's
# Quit takes. Without a shell (--no-window, --browser) the process just ends.
_quit_hook = None


def register_quit_hook(fn) -> None:
    global _quit_hook
    _quit_hook = fn


@app.post("/api/system/quit")
async def system_quit() -> dict:
    """Exit ShellMate: every session closes and the process ends (#452)."""
    from backend import server as server_module
    try:
        server_module.clear_lock()
    except Exception as exc:
        logger.warning("Could not clear the instance lock on quit: %s", exc)
    live = [s.get("display_label") or s.get("hostname") or "a device"
            for s in session_manager.get_all_sessions() if s.get("is_connected", True)]
    logger.info("Exit requested from the interface; %d session(s) open.", len(live))

    def go() -> None:
        time.sleep(0.4)                      # let the answer leave first
        store.flush(timeout=2.0)             # queued history lands before the exit
        hook = _quit_hook
        if hook is not None:
            try:
                hook(confirm=False)
                return
            except Exception as exc:
                logger.warning("The desktop shell could not quit cleanly: %s", exc)
        os._exit(0)
    threading.Thread(target=go, daemon=True, name="quit").start()
    return {"quitting": True, "sessions": len(live)}


@app.get("/api/system/update/status")
async def update_status() -> dict:
    """Where a download stands (#443)."""
    return updater.state()


@app.post("/api/system/update/download")
async def update_download() -> dict:
    """Fetch the latest release into the data folder and verify it (#443)."""
    try:
        return await asyncio.to_thread(updater.start_download, app_version.RELEASES_REPO)
    except PermissionError as exc:
        raise HTTPException(status_code=402, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/system/update/cancel")
async def update_cancel() -> dict:
    return updater.cancel_download()


@app.post("/api/system/update/apply")
async def update_apply(request: Request) -> dict:
    """Swap the executable and relaunch (#444). Returns only on refusal."""
    # The port the server actually bound (#479): behind a proxy the request
    # URL may carry none, and a hardcoded 8765 made the helper poll the
    # wrong port for 90 s and roll the update back.
    from backend import server as server_module
    port = getattr(server_module, "listening_port", None) or request.url.port or 8765
    try:
        return await asyncio.to_thread(updater.apply, session_manager, port)
    except PermissionError as exc:
        raise HTTPException(status_code=402, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


class LicenceKeyRequest(BaseModel):
    key: str


@app.get("/api/licence")
async def licence_status() -> dict:
    """What the installed licence means today (#446)."""
    return licence_module.status()


@app.post("/api/licence")
async def licence_install(request: LicenceKeyRequest) -> dict:
    """Verify and keep a key."""
    try:
        licence_module.install(request.key)
    except licence_module.LicenceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    licence_module.announce_async("activate")       # the service learns where the key went
    return licence_module.status()


@app.delete("/api/licence")
async def licence_remove() -> dict:
    current = licence_module.load()
    if licence_module.remove() and current is not None:
        licence_module.announce_async("deactivate", current.id)
    return licence_module.status()


@app.post("/api/licence/refresh")
async def licence_refresh() -> dict:
    """Ask the licence service about the installed key: renewal, revocation."""
    return await asyncio.to_thread(licence_module.refresh)


#: Last CPU sample, for the utilisation delta below. Module state rather than
#: per-request, because utilisation only means anything *between* two readings.
_CPU_SAMPLE = {"process": 0.0, "wall": 0.0}


def _process_stats() -> dict:
    """
    ShellMate's own footprint (#266) — this process, not the machine.

    No psutil: a dependency is a real cost in a portable build, and both
    numbers are available for free. CPU is the process_time delta over the
    wall-clock delta since the last call (it can exceed 100% — the server is
    threaded and the number is per-process, not per-core). Memory is the
    working set via psapi on Windows, /proc on anything else. Either being
    unavailable reports None, never an error.
    """
    import time as _time

    cpu = None
    now_process, now_wall = _time.process_time(), _time.monotonic()
    last_process, last_wall = _CPU_SAMPLE["process"], _CPU_SAMPLE["wall"]
    _CPU_SAMPLE["process"], _CPU_SAMPLE["wall"] = now_process, now_wall
    if last_wall and now_wall > last_wall:
        cpu = max(0.0, min(999.0,
                           (now_process - last_process)
                           / (now_wall - last_wall) * 100.0))

    memory_mb = None
    try:
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            class _MemoryCounters(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            # Explicit signatures, not windll defaults: the untyped call
            # truncates the pseudo-handle on 64-bit and fails with no error
            # to read — it simply reports zero.
            psapi = ctypes.WinDLL("psapi")
            kernel32 = ctypes.WinDLL("kernel32")
            kernel32.GetCurrentProcess.restype = wintypes.HANDLE
            psapi.GetProcessMemoryInfo.argtypes = [
                wintypes.HANDLE, ctypes.POINTER(_MemoryCounters), wintypes.DWORD]
            psapi.GetProcessMemoryInfo.restype = wintypes.BOOL

            counters = _MemoryCounters()
            counters.cb = ctypes.sizeof(counters)
            if psapi.GetProcessMemoryInfo(
                    kernel32.GetCurrentProcess(),
                    ctypes.byref(counters), counters.cb):
                memory_mb = counters.WorkingSetSize / (1024 * 1024)
        else:
            with open("/proc/self/status", encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("VmRSS:"):
                        memory_mb = float(line.split()[1]) / 1024
                        break
    except Exception:
        pass

    return {"cpu_percent": cpu, "memory_mb": memory_mb}


@app.get("/api/system/stats")
async def system_stats() -> dict:
    """The process's CPU and memory, for the status bar (#266)."""
    return await asyncio.to_thread(_process_stats)


def _build_time() -> str:
    """
    When the running copy was produced, as a readable local timestamp.

    Frozen: the executable's own modification time. From source: the newest of
    the backend modules, which is the closest honest answer available — a
    source run has no build step to timestamp.
    """
    # The build record, when there is one, is the truth (#420); the file
    # time is the fallback for a checkout, which has no build step.
    built = app_version.build_info().get("built", "")
    if built:
        try:
            return datetime.datetime.fromisoformat(built).strftime("%Y-%m-%d %H:%M")
        except ValueError:
            return built
    try:
        if paths.is_frozen():
            stamp = Path(sys.executable).stat().st_mtime
        else:
            stamp = max((f.stat().st_mtime
                         for f in Path(__file__).parent.rglob("*.py")),
                        default=0.0)
        if not stamp:
            return ""
        return datetime.datetime.fromtimestamp(stamp).strftime("%Y-%m-%d %H:%M")
    except OSError:
        return ""


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

    # Collect terminal buffers from the session manager.
    #
    # Through the outbound helper, which masks credentials. This is the path
    # that most needs it: a Jira ticket persists, colleagues read it, and on
    # most instances it is searchable across the whole organisation — more
    # "handed to someone else" than a session log on disk ever is.
    sessions = []
    for sid in open_session_ids:
        sess = session_manager.get_session(sid)
        if not sess:
            continue
        sessions.append({
            "label":           sess.get("display_label") or sess.get("hostname", sid[:8]),
            "hostname":        sess.get("hostname", ""),
            "connection_type": sess.get("connection_type", "ssh"),
            "buffer_text":     outbound.session_text(sess, 500),
        })

    # The conversation goes into the ticket too, and the assistant quotes
    # device output back at you constantly. The key is "text" (#320): that is
    # what the frontend sends and what build_adf reads — this used to redact
    # a "content" key the messages never had, so every secret in the chat
    # went into the ticket under a comment claiming otherwise.
    chat_messages = [
        {**m, "text": outbound.redact_text(str(m.get("text", "")))}
        if isinstance(m, dict) else m
        for m in chat_messages
    ]

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
    # it will accept anything else. None means "use the configured default";
    # an explicit 0 means no wait, and is honoured as such (#337).
    wait_ms: int | None = None
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


# The ceiling on a whole sequence now lives in Stockton as
# `broadcast.max_seconds`. The constant it replaced stayed behind and went on
# being quoted in the timeout message, so a sequence abandoned at 600s
# reported 180 — a number that matched nothing the user had set, on the one
# message telling them how long their devices had been running commands.


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
    alias expansion and the guardrail apply exactly as when typed.
    """
    commands = request.command_list()
    if not commands:
        raise HTTPException(status_code=400, detail="No command given.")
    if not request.session_ids:
        raise HTTPException(status_code=400, detail="No sessions selected.")

    # `is None`, not `or` (#337): an explicit 0 is "no wait", a valid choice
    # for a read-only sequence, and `or` silently replaced it with the default.
    wait_ms = (advanced_setting("broadcast.default_wait")
               if request.wait_ms is None else request.wait_ms)
    wait = max(0, min(60_000, wait_ms)) / 1000

    # A cap on how many devices are in flight. Two hundred at once is two
    # hundred SSH channels through the same jump host.
    limit = advanced_setting("broadcast.concurrency")
    gate = asyncio.Semaphore(limit) if limit else None

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

        pipeline = session["pipeline"]

        # The user may be mid-line in this tab (#332). The device's input
        # buffer already holds their echo, so a broadcast would append to it
        # — "conf t" plus "show version" executes "conf tshow version". Clear
        # both sides first: Ctrl-U wipes the device's line, reset() wipes the
        # assembler's, and the user's half-typed text goes rather than being
        # silently merged into a command nobody wrote.
        if pipeline.current_line:
            await asyncio.to_thread(handler.send, b"\x15")
            pipeline.reset()

        sent = []
        for index, command in enumerate(commands):
            try:
                outbound = pipeline.process(
                    command + ("\r" if request.execute else ""))

                # Held by the guardrail, and there is nobody here to ask
                # (#330): broadcast is not interactive. Nothing is sent for
                # this command — not even the echo — and the device is failed
                # honestly rather than counted as a success it never ran.
                if pipeline.newly_held:
                    for held in list(pipeline.newly_held):
                        pipeline.drop(held)
                    note = (f'"{command}" needs confirmation (destructive on '
                            f"this platform) — broadcast cannot ask. "
                            f"Run it in the device's own tab.")
                    if advanced_setting("broadcast.stop_on_error"):
                        return {"session_id": session_id, "ok": False,
                                "label": label,
                                "error": f"{note} (after {len(sent)} of {len(commands)})",
                                "sent": sent}
                    sent.append(f"{command}  [held: needs confirmation]")
                    continue

                # Taken before the await (#477): process() resets these on
                # every call, and a keystroke in this tab during the send
                # would empty them.
                completed_commands = list(pipeline.completed_commands)
                expansion = pipeline.last_expansion
                await asyncio.to_thread(
                    handler.send, outbound.encode("utf-8", errors="replace"))
                # The alert tracker has to see "reload in 10" however it was
                # sent (#330) — typed or broadcast, it schedules the same
                # reload.
                for completed in completed_commands:
                    try:
                        session["alerts"].observe_command(completed)
                    except Exception:
                        pass
                sent.append(expansion[1] if expansion else command)
            except Exception as exc:
                logger.warning("Broadcast to %s failed on %r: %s", label, command, exc)
                # Stop this device rather than pressing on: the rest of a
                # sequence rarely makes sense once a step has failed, and
                # blindly continuing is how half-applied changes happen.
                # Switchable: a run of read-only commands may reasonably
                # carry on past one failure.
                if advanced_setting("broadcast.stop_on_error"):
                    return {"session_id": session_id, "ok": False, "label": label,
                            "error": f"{exc} (after {len(sent)} of {len(commands)})",
                            "sent": sent}
                sent.append(f"{command}  [failed: {exc}]")

            if wait and index < len(commands) - 1:
                await asyncio.sleep(wait)

        return {"session_id": session_id, "ok": True, "label": label, "sent": sent}

    async def run_gated(session_id: str) -> dict:
        if gate is None:
            return await run_one(session_id)
        async with gate:
            return await run_one(session_id)

    try:
        results = await asyncio.wait_for(
            asyncio.gather(*(run_gated(sid) for sid in request.session_ids)),
            timeout=advanced_setting("broadcast.max_seconds"),
        )
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=504,
            detail=(f"The sequence was still running after "
                    f"{int(advanced_setting('broadcast.max_seconds'))}s and was "
                    f"abandoned. Some commands will already have been sent — "
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
    #: Offered on a tab's right-click menu.
    quick: bool = False
    #: False types the command in and leaves it, rather than running it.
    send_return: bool = True


@app.get("/api/snippets/quick")
async def snippets_quick() -> dict:
    """
    The snippets offered on a tab's right-click menu.

    Its own endpoint rather than a filter in the browser: the tab menu opens
    on a right-click and should not be waiting on the whole 137-entry library
    to decide what to show.
    """
    return {"snippets": [s.as_dict()
                         for s in await asyncio.to_thread(snippets.quick_snippets)]}


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


# ---------------------------------------------------------------------------
# REST — Stockton, the advanced settings
# ---------------------------------------------------------------------------


class AdvancedRequest(BaseModel):
    """Body for the Stockton endpoints."""

    values: dict = {}
    key: str = ""
    category: str = ""


@app.get("/api/restart")
async def restart_available() -> dict:
    """
    Whether ShellMate can relaunch itself, and what is still connected.

    Asked before the button is offered: a restart drops every session, which
    is the one thing the desktop shell is built around not doing, so the
    interface names them rather than presenting this as a reload.
    """
    live = [
        s.get("display_label") or s.get("hostname") or s["session_id"][:8]
        for s in session_manager.get_all_sessions() if s.get("is_connected")
    ]
    return {"available": desktop.can_restart(), "sessions": live}


@app.post("/api/restart")
async def restart_now() -> dict:
    """
    Start a fresh copy and stop this one.

    Returns only on failure — on success the process is gone before the
    response could be written, which the interface treats as the signal to
    wait and reconnect.
    """
    if not desktop.can_restart():
        raise HTTPException(
            status_code=400,
            detail="ShellMate cannot relaunch itself from here. Quit from the "
                   "tray and start it again.",
        )

    # On a thread: the handover waits for the replacement to answer, and doing
    # that on the event loop would stop this server responding — including to
    # the health check the new copy's lock depends on.
    async def go() -> None:
        await asyncio.to_thread(desktop.restart, desktop.active_desktop())

    asyncio.create_task(go())
    return {"restarting": True}


@app.get("/api/advanced")
async def advanced_list() -> dict:
    """
    The whole registry: every setting, its default, its bounds and its value.

    The panel renders itself from this rather than from hand-written markup,
    so a setting cannot exist in the code and be missing from the interface,
    or be described there as something it no longer is.
    """
    return await asyncio.to_thread(advanced_settings.describe)


@app.post("/api/advanced")
async def advanced_update(request: AdvancedRequest) -> dict:
    """
    Store a partial update.

    Values are clamped to their declared bounds here rather than trusted from
    the browser — this API is documented and scriptable, and settings.json is
    a text file people are encouraged to edit.
    """
    return await asyncio.to_thread(advanced_settings.update, request.values)


@app.post("/api/advanced/reset")
async def advanced_reset(request: AdvancedRequest) -> dict:
    """Restore one setting, one category, or all of them."""
    return await asyncio.to_thread(
        advanced_settings.reset, request.key or None, request.category or None)


# ---------------------------------------------------------------------------
# REST — SSH keys
#
# No endpoint here returns private key material, in the same way none returns
# a password. The interface sees the public half, the fingerprints and the
# metadata; the private key exists on disk and nowhere else.
# ---------------------------------------------------------------------------


class GenerateKeyRequest(BaseModel):
    """Body for POST /api/keys."""

    name: str = "id_shellmate"
    kind: str = "ed25519"
    bits: int = 3072
    curve: str = "p256"
    passphrase: str = ""
    comment: str = ""


class KeyPathRequest(BaseModel):
    """Body for the endpoints that act on an existing key."""

    path: str
    # Only used by the passphrase change.
    old_passphrase: str = ""
    new_passphrase: str = ""
    name: str = ""


@app.get("/api/keys")
async def keys_list() -> dict:
    """Every key ShellMate is looking after."""
    return {
        "keys":   await asyncio.to_thread(ssh_keys.listing),
        "folder": str(ssh_keys.keys_dir()),
        "types":  list(ssh_keys.KEY_TYPES),
        "rsa_sizes": list(ssh_keys.RSA_SIZES),
        "curves": sorted(ssh_keys.CURVES),
    }


@app.post("/api/keys")
async def key_generate(request: GenerateKeyRequest) -> dict:
    """Create a key pair."""
    try:
        info = await asyncio.to_thread(
            ssh_keys.generate, request.name, request.kind, request.bits,
            request.curve, request.passphrase, request.comment,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return info.as_dict()


@app.post("/api/keys/import")
async def key_import(request: KeyPathRequest) -> dict:
    """Copy an existing key into ShellMate's keys folder."""
    try:
        info = await asyncio.to_thread(ssh_keys.import_key, request.path, request.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return info.as_dict()


@app.post("/api/keys/passphrase")
async def key_passphrase(request: KeyPathRequest) -> dict:
    """Add, change or remove a key's passphrase."""
    try:
        info = await asyncio.to_thread(
            ssh_keys.change_passphrase, request.path,
            request.old_passphrase, request.new_passphrase,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return info.as_dict()


@app.post("/api/keys/delete")
async def key_delete(request: KeyPathRequest) -> dict:
    """
    Remove a key and its public half.

    A POST rather than a DELETE with the path in the URL: a Windows path
    carries backslashes and colons, and round-tripping one through a URL
    segment is a source of bugs nobody needs here.
    """
    try:
        removed = await asyncio.to_thread(ssh_keys.delete, request.path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"removed": removed}


@app.get("/api/keys/known-hosts")
async def list_known_hosts() -> dict:
    """The host keys ShellMate has decided to trust (#528)."""
    rows = await asyncio.to_thread(ssh_keys.known_hosts)
    return {"hosts": rows, "file": str(ssh_keys.known_hosts_path())}


class ForgetHostRequest(BaseModel):
    """Body for POST /api/keys/known-hosts/forget."""
    host: str = ""


@app.post("/api/keys/known-hosts/forget")
async def forget_known_host(request: ForgetHostRequest) -> dict:
    """
    Forget one host's keys, so the next connection is a first connection.

    The way back from having trusted the wrong thing, and the way to clear an
    entry for a device that has been decommissioned.
    """
    removed = await asyncio.to_thread(ssh_keys.forget_host, request.host)
    return {"status": "ok", "removed": removed}


class CertificateRequest(BaseModel):
    """Body for POST /api/keys/certificate."""

    text: str = ""


@app.post("/api/keys/certificate")
async def inspect_certificate(request: CertificateRequest) -> dict:
    """
    Read an OpenSSH certificate and report what it permits (#301).

    Nothing is stored and no private key is involved — this reads a public
    certificate and says what it claims: who it is for, when it stops
    working, and which CA signed it. `ssh-keygen -L` answers the same
    questions and is absent from a locked-down Windows machine, which is
    exactly where somebody is debugging a refused login.
    """
    import time as _time

    from backend import certs

    info = await asyncio.to_thread(certs.parse, request.text)
    return {**info.as_dict(), "verdict": certs.verdict(info, _time.time())}


# ---------------------------------------------------------------------------
# REST — Support bundle
# ---------------------------------------------------------------------------


class SupportRequest(BaseModel):
    """Body for the support endpoints."""

    sections: list[str] = []
    note: str = ""


@app.get("/api/support/sections")
async def support_sections() -> dict:
    """What can go in a support bundle, and what defaults to being included."""
    return {"sections": support.describe(), "folder": str(support.bundle_dir())}


@app.post("/api/support/preview")
async def support_preview(request: SupportRequest) -> dict:
    """
    Gather the chosen sections without writing anything.

    Everything is readable in the panel before it leaves — the manual already
    tells people to read the log before sending it, and an instruction nobody
    follows is worse than a preview nobody can avoid.
    """
    collected = await asyncio.to_thread(
        support.collect, request.sections, session_manager)
    return {"sections": collected}


@app.post("/api/support/bundle")
async def support_bundle(request: SupportRequest) -> dict:
    """Write the chosen sections as one zip in the data folder."""
    collected = await asyncio.to_thread(
        support.collect, request.sections, session_manager)
    try:
        path = await asyncio.to_thread(support.write_bundle, collected, request.note)
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Could not write the bundle: {exc}",
        ) from exc
    return {"path": str(path), "folder": str(path.parent),
            "bytes": path.stat().st_size, "sections": sorted(collected)}


@app.post("/api/support/reveal")
async def support_reveal(request: SupportRequest) -> dict:
    """Open the folder holding the bundles, where the platform allows it."""
    folder = support.bundle_dir()
    try:
        folder.mkdir(parents=True, exist_ok=True)
        opened = await asyncio.to_thread(desktop.reveal, folder)
    except Exception as exc:
        logger.info("Could not open the bundle folder: %s", exc)
        opened = False
    return {"opened": opened, "folder": str(folder)}


# ---------------------------------------------------------------------------
# REST — The assistant's system prompts
# ---------------------------------------------------------------------------


class PromptRequest(BaseModel):
    """Body for PUT /api/prompts/{mode}."""

    body: str


@app.get("/api/prompts")
async def prompts_list() -> dict:
    """
    Return every persona, its shipped default, and whether it has been edited.

    The command-suggestion rules come back too, so the editor can show what
    the ``{command_rules}`` marker expands to rather than describing it.
    """
    return await asyncio.to_thread(prompt_store.state)


@app.put("/api/prompts/{mode}")
async def prompt_save(mode: str, request: PromptRequest) -> dict:
    """Persist an edited persona."""
    try:
        await asyncio.to_thread(prompt_store.save, mode, request.body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return await asyncio.to_thread(prompt_store.state)


@app.post("/api/prompts/reset")
async def prompt_reset(mode: str = "") -> dict:
    """Restore one persona, or every one of them, to the shipped text."""
    try:
        await asyncio.to_thread(prompt_store.reset, mode or None)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return await asyncio.to_thread(prompt_store.state)


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


@app.get("/api/providers/cached")
async def provider_models_cached() -> dict:
    """
    The model list as discovered last time, without touching the network.

    Backs the picker on page load. The list hardcoded in index.html is only a
    first-run fallback; once a discovery has succeeded, a fresh page shows the
    providers as they were last seen rather than as the HTML remembers them.
    """
    return await asyncio.to_thread(providers.load_cached)


@app.get("/api/ollama/models")
async def ollama_models() -> list[dict]:
    """Return the list of models installed in the local Ollama instance."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            # Through the same host resolution as discovery, so a host set in
            # Settings is honoured here too instead of only in .env.
            resp = await client.get(providers._ollama_url())
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
    """
    Return a list of available session log files.

    Zero-byte files are swept rather than listed (#235): a log with nothing
    in it says nothing, and the writer no longer creates one on purpose — any
    that exist are leftovers from failures or older builds.
    """
    from datetime import datetime
    logs_dir = log_directory()
    if not logs_dir.exists():
        return []
    files = []
    for f in logs_dir.glob("*.log"):
        try:
            stat = f.stat()
            if stat.st_size == 0:
                f.unlink()
                continue
        except OSError:
            # Deleted or unreadable between glob and stat — not worth a 500.
            continue
        files.append({
            "filename": f.name,
            "size_bytes": stat.st_size,
            "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
        })
    files.sort(key=lambda entry: entry["modified"], reverse=True)
    return files


def _log_path(filename: str) -> Path:
    """The log file a request names, or a 400/404 (see download_log)."""
    if ("/" in filename or "\\" in filename or filename in (".", "..")
            or not filename.endswith(".log")):
        raise HTTPException(status_code=400, detail="Invalid filename")
    directory = log_directory().resolve()
    log_path = (directory / filename).resolve()
    if log_path.parent != directory:
        raise HTTPException(status_code=400, detail="Invalid filename")
    if not log_path.exists():
        raise HTTPException(status_code=404, detail="Log file not found")
    return log_path


@app.post("/api/logs/{filename}/save")
async def save_log_copy(filename: str) -> dict:
    """
    Save a copy of a log where the user chooses, through the platform's own
    folder dialog, and open that folder (#578).

    In the native window the browser's download machinery does nothing
    visible, so Download appeared to do nothing at all. Without a native
    window this answers 409 and the page falls back to a browser download.
    """
    source = _log_path(filename)
    if not desktop.has_native_window():
        raise HTTPException(status_code=409, detail="No native window; use the browser download.")
    folder = await asyncio.to_thread(desktop.pick_directory, "Save the log to…")
    if not folder:
        return {"cancelled": True}
    target = Path(folder) / filename
    try:
        await asyncio.to_thread(shutil.copyfile, source, target)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Could not save the copy: {exc}") from exc
    try:
        await asyncio.to_thread(desktop.reveal, str(Path(folder)))
    except Exception:
        pass
    return {"path": str(target)}


class ScrollbackSaveRequest(BaseModel):
    """Body for POST /api/scrollback/save."""
    filename: str = "scrollback.log"
    text: str = ""


@app.post("/api/scrollback/save")
async def save_scrollback(request: ScrollbackSaveRequest) -> dict:
    """
    Save what a terminal is holding to a file the user chooses (#534).

    The tab nobody thought to log is the case this exists for: the change is
    done, the evidence is on screen, and turning logging on now would record
    only what happens next.

    Redacted by the same one switch that covers session logs and captured
    configurations. This writes device output to a file whose whole purpose
    is to be handed to somebody else, which is exactly what that promise is
    about — and redacting here rather than in the browser is what keeps it
    true on both paths below.

    Where there is a native window the platform's folder dialog runs, as it
    does for a log copy (#578). Where there is not, the prepared text comes
    back for the browser to download: a 409 with nothing in it would have
    left the page to save the raw text it happens to be holding, which is the
    one thing redaction is for.
    """
    text = request.text
    if get_settings().get("logging", {}).get("redact_secrets", True):
        text = await asyncio.to_thread(redact, text)

    # Windows forbids <>:"/\|?* in filenames, and a detected IPv6 target
    # carries colons — the same substitution the log writer makes.
    name = re.sub(r'[<>:"/\\|?*]', "-", request.filename.strip()) or "scrollback"
    if not name.endswith(".log"):
        name += ".log"

    if not desktop.has_native_window():
        return {"filename": name, "text": text}

    folder = await asyncio.to_thread(desktop.pick_directory, "Save the scrollback to…")
    if not folder:
        return {"cancelled": True}
    target = Path(folder) / name
    try:
        await asyncio.to_thread(target.write_text, text, "utf-8", "replace")
    except OSError as exc:
        raise HTTPException(
            status_code=500, detail=f"Could not save the file: {exc}") from exc
    with contextlib.suppress(Exception):
        await asyncio.to_thread(desktop.reveal, str(Path(folder)))
    return {"path": str(target)}


@app.get("/api/logs/{filename}")
async def download_log(filename: str) -> FileResponse:
    """
    Download a specific log file.

    Traversal is blocked by refusing separators and then checking the
    resolved path is still inside the log directory — not by a character
    allowlist. The old `[\\w\\-\\.]+` regex quietly rejected names the writer
    happily produces: a detected hostname with a space, an IPv6 address with
    colons (#234). The writer and the reader must accept the same names.
    """
    if ("/" in filename or "\\" in filename or filename in (".", "..")
            or not filename.endswith(".log")):
        raise HTTPException(status_code=400, detail="Invalid filename")
    directory = log_directory().resolve()
    log_path = (directory / filename).resolve()
    if log_path.parent != directory:
        raise HTTPException(status_code=400, detail="Invalid filename")
    if not log_path.exists():
        raise HTTPException(status_code=404, detail="Log file not found")
    return FileResponse(str(log_path), filename=filename)


@app.post("/api/logs/reveal")
async def logs_reveal() -> dict:
    """
    Open the session-log folder, where the platform allows it (#245).

    The downloaded copy lands wherever the browser keeps downloads, which
    the server cannot see — but the originals live here, and "show me the
    files" is the intent behind clicking a download toast.
    """
    folder = log_directory()
    try:
        folder.mkdir(parents=True, exist_ok=True)
        opened = await asyncio.to_thread(desktop.reveal, folder)
    except Exception as exc:
        logger.info("Could not open the log folder: %s", exc)
        opened = False
    return {"opened": opened, "folder": str(folder)}


# ---------------------------------------------------------------------------
# WebSocket — terminal I/O
# ---------------------------------------------------------------------------

async def _origin_allowed(websocket: WebSocket) -> bool:
    """
    Whether a WebSocket handshake came from a page we serve.

    CORSMiddleware does not apply to WebSockets. The browser sends an `Origin`
    header on the handshake and the server is expected to reject unknown ones
    itself; Starlette does not do it for anybody.

    This matters because of what the security model actually promises.
    Binding to 127.0.0.1 with no authentication is defensible against other
    programs on the machine — they would need to be running here already. It
    is *not* defensible against a web page the user merely visits, because
    their browser is on this machine and will open a socket to loopback on the
    page's behalf. `/ws/chat` needs no secret at all, and `context_mode: all`
    builds a prompt from every open session's buffer, so an accepted socket
    there reads what is on every screen and streams it back to the caller.
    The port is not a secret either: the scan walks upward from 8765.

    A missing Origin is allowed. Non-browser clients — a script, curl, a test —
    send none, and they have no ambient authority to borrow: nothing is
    attaching the user's cookies or their loopback access on their behalf. It
    is the *browser* case that needs the check, and browsers always send it.
    """
    # Authentication first where it is switched on: an allowed origin says
    # the page is one of ours, not that whoever is driving it may be here.
    if auth.enabled() and not auth.check_cookie(
            websocket.cookies.get(auth.COOKIE, "")):
        logger.warning("Refused an unauthenticated WebSocket on %s",
                       websocket.url.path)
        await websocket.close(code=1008)
        return False

    origin = websocket.headers.get("origin")
    if origin is None:
        return True
    if ALLOWED_ORIGIN.match(origin):
        return True

    logger.warning("Refused a WebSocket handshake from origin %r on %s",
                   origin, websocket.url.path)
    await websocket.close(code=1008)   # policy violation
    return False


#: Shown in the terminal when a held command is refused.
#:
#: The line was already cleared from the device's input, so without this
#: nothing happens and nothing says why — a command that silently vanishes is
#: its own kind of confusion.
CANCELLED_NOTE = "\r\n\x1b[33m[not sent: {command}]\x1b[0m\r\n"

#: The most lines one paste may be sent as (#523). Not a Stockton setting: it
#: bounds a message the browser sends rather than a behaviour anyone would
#: want to tune, and the honest answer above it is "not like this" — a
#: configuration that size belongs in a file, applied with a push.
PASTE_MAX_LINES = 5000


def _load_watch_rules(session: dict) -> None:
    """
    Arm this session's output watch from the user's colour rules (#521).

    Off the event loop, like `_open_session_log`, and for the same reason: it
    reads and deep-merges settings.json, which is a file read no terminal
    should ever wait behind.
    """
    session["watch"].load(get_settings().get("highlight"))


def _open_session_log(session: dict, session_id: str) -> None:
    """
    Decide where this session's log goes — without touching the disk.

    The file itself is created by the first successful write, not here.
    Opening in "a" mode creates the file the moment the handle exists, and
    every path between that moment and the first write — a settings save
    closing the handle, the socket dropping, the first write itself failing —
    left a zero-byte file behind (#235). Deciding the path is free; only a
    line actually written should cost a file.
    """
    settings = get_settings()
    # The tab's own answer wins over the global switch (#534); None means it
    # has not given one and follows the setting, exactly as keep_alive does.
    override = session.get("log_override")
    enabled = (bool(settings.get("logging", {}).get("enabled"))
               if override is None else bool(override))
    if not enabled:
        session["_log_path"] = None
        return

    session["_log_redact"] = bool(settings["logging"].get("redact_secrets", True))
    # Windows forbids <>:"/\|?* in filenames, and a detected IPv6 target
    # carries colons — the first write raised OSError and _append_to_log
    # permanently gave up, so logging was silently unavailable for every
    # IPv6/odd-named session (#338). Replaced rather than rejected: the log
    # must still exist, whatever the device is called.
    host = re.sub(r'[<>:"/\\|?*]', "-",
                  str(session.get("hostname") or "session"))
    session["_log_path"] = log_directory() / f"{session_id[:8]}-{host}.log"


def _discard_if_empty(path) -> None:
    """A zero-byte log says nothing; it is tidier gone than listed."""
    try:
        if path is not None and path.exists() and path.stat().st_size == 0:
            path.unlink()
    except OSError:
        pass


def _redact_for_log(session: dict, line: str) -> str | None:
    """
    One line, masked — or None when masking itself failed.

    Withheld rather than written raw on failure: a log written without
    redaction is the worse outcome, and one bad line must not cost the
    session (#235).
    """
    try:
        return redact(line) if session.get("_log_redact", True) else line
    except Exception as exc:
        logger.warning("Log redaction failed; line withheld: %s", exc)
        return None


def _append_to_log(session: dict, text: str) -> None:
    """
    Buffer device output and write whole stamped lines.

    Stamping every chunk as it arrived put a timestamp between every echoed
    keystroke — a typed command read back as one character per stamp (#246).
    Output now coalesces until a newline, and each complete line gets one
    stamp. Redaction runs on the assembled line too, which is the first
    moment it can see a credential whole rather than split across chunks.

    Open-and-write stay in one call, on one thread, so nothing can land
    between them and orphan an empty file (#235). Flushed per batch so a
    crash does not lose the tail.
    """
    path = session.get("_log_path")
    if path is None:
        return

    buffer = session.get("_log_buffer", "") + text
    if "\n" not in buffer:
        if len(buffer) < 4096:
            session["_log_buffer"] = buffer
            return
        # A screen redrawn in place can go pages without a newline; flush it
        # as one line rather than holding it forever.
        lines = [buffer]
        session["_log_buffer"] = ""
    else:
        lines = buffer.split("\n")
        session["_log_buffer"] = lines.pop()

    handle = session.get("_log_handle")
    try:
        if handle is None:
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = open(path, "a", encoding="utf-8")
            session["_log_handle"] = handle
            logger.info("Logging session to %s", path)
        stamp = datetime.datetime.now().isoformat()
        for line in lines:
            masked = _redact_for_log(session, line.rstrip("\r"))
            if masked is not None:
                handle.write(f"[{stamp}] {masked}\n")
        handle.flush()
    except (OSError, ValueError) as exc:
        logger.warning("Session log write failed, giving up on it: %s", exc)
        try:
            if handle is not None:
                handle.close()
        except Exception:
            pass
        session["_log_handle"] = None
        session["_log_path"] = None
        session.pop("_log_buffer", None)
        _discard_if_empty(path)


def _close_session_log(session: dict) -> None:
    """
    Flush the partial line, close the handle, and drop an empty file.

    The tail matters: a session usually ends mid-line, and the last thing
    typed is often the interesting thing.
    """
    handle = session.pop("_log_handle", None)
    path = session.pop("_log_path", None)
    tail = (session.pop("_log_buffer", "") or "").rstrip("\r")
    try:
        if tail.strip():
            if handle is None and path is not None:
                path.parent.mkdir(parents=True, exist_ok=True)
                handle = open(path, "a", encoding="utf-8")
                session["_log_handle"] = handle
            if handle is not None:
                masked = _redact_for_log(session, tail)
                if masked is not None:
                    handle.write(
                        f"[{datetime.datetime.now().isoformat()}] {masked}\n")
    except (OSError, ValueError):
        pass
    finally:
        session.pop("_log_handle", None)
        if handle is not None:
            try:
                handle.close()
            except Exception:
                pass
    _discard_if_empty(path)



def _ensure_reader_thread(session: dict, handler, bank_lock: threading.Lock) -> None:
    """
    Start the session's reader thread if it is not running (#471).

    Reads only while a browser is attached (``_recv_notify`` set), banks
    bytes under ``bank_lock``, wakes the attached consumer on the loop, and
    ends when the far end closes. Never raises into anything.
    """
    existing = session.get("_reader")
    if existing is not None and existing.is_alive():
        return

    def run() -> None:
        session_id = session.get("session_id", "?")
        try:
            while True:
                notify = session.get("_recv_notify")
                if notify is None:
                    if not handler.is_connected:
                        break
                    time.sleep(0.2)
                    continue
                try:
                    data = handler.recv(4096)
                except Exception as exc:
                    logger.info("Reader for %s stopped: %s", session_id, exc)
                    data = b""
                if data is None:
                    continue
                if data == b"":
                    session["_reader_closed"] = True
                    loop, wake = notify
                    try:
                        loop.call_soon_threadsafe(wake.set)
                    except RuntimeError:
                        pass
                    break
                with bank_lock:
                    session["_recv_pending"] = session.get("_recv_pending", b"") + data
                loop, wake = notify
                try:
                    loop.call_soon_threadsafe(wake.set)
                except RuntimeError:
                    pass                          # the loop is gone; the bank keeps the bytes
        finally:
            session["_reader"] = None

    thread = threading.Thread(target=run, daemon=True, name=f"reader-{session.get('session_id', '')[:8]}")
    session["_reader"] = thread
    thread.start()

@app.websocket("/ws/terminal/{session_id}")
async def terminal_websocket(websocket: WebSocket, session_id: str) -> None:
    """
    Bidirectional WebSocket bridge between the browser's xterm.js and the
    device, whatever the transport underneath is.

    Each session_id gets its own WebSocket connection.  Multiple tabs in
    the browser each connect here with their own session_id — they are
    completely independent.

    An accepted socket here can type into a live device, so the handshake is
    checked before it is accepted rather than after.
    """
    if not await _origin_allowed(websocket):
        return
    await websocket.accept()

    session = session_manager.get_session(session_id)
    if session is None:
        await websocket.send_text(
            json.dumps({"type": "output", "data": "\r\nError: session not found.\r\n"})
        )
        await websocket.close()
        return

    handler = session["handler"]

    # A reattach after a refresh or a dropped socket (#331). The old teardown
    # marked the session disconnected unconditionally and nothing ever set it
    # back, so after the first F5 every consumer of the flag was permanently
    # wrong: the tray's quit confirmation saw zero live sessions and skipped
    # itself, /api/sessions and the dashboard badges reported everything
    # dead, and live capture refused to run. The handler knows whether the
    # transport is actually up; believe it.
    if handler.is_connected and not session.get("is_connected"):
        session["is_connected"] = True

    # Once per *session*, not per socket (#479): a browser refresh used to
    # re-run the profile rewrite and the store update.
    session.setdefault("_hostname_sent", False)

    async def read_from_client() -> None:
        """Forward browser keystrokes / resize events to the device."""
        try:
            while True:
                raw = await websocket.receive_text()
                try:
                    await handle_client_message(raw)
                except WebSocketDisconnect:
                    raise
                except Exception as exc:
                    # One message that could not be delivered — a send that
                    # timed out because the device stopped reading, a
                    # resize with a bad number — used to end this loop,
                    # which closed the bridge and greyed out the tab (#470).
                    # Say so and carry on; the session is still up.
                    logger.warning("Could not handle a message for session %s: %s", session_id, exc)
                    try:
                        await websocket.send_text(json.dumps({
                            "type": "output",
                            "data": f"\r\n\x1b[33m[ShellMate: {exc}]\x1b[0m\r\n"}))
                    except Exception:
                        pass
        except WebSocketDisconnect:
            logger.info("Browser closed the terminal socket for session %s", session_id)
        except Exception as exc:
            logger.warning("read_from_client error (session %s): %s", session_id, exc)

    async def handle_client_message(raw: str) -> None:
        """One message from the browser: input, a resize, or a guardrail answer."""
        if True:
            if True:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    # If someone sends plain text, treat it as input
                    msg = {"type": "input", "data": raw}

                msg_type = msg.get("type")

                if msg_type == "input":
                    data: str = msg.get("data", "")

                    # Typing ends a live capture immediately, and the session
                    # goes straight back. The alternative — holding the
                    # keystrokes until the capture finished — keeps the screen
                    # tidier, but a terminal that ignores you for two seconds
                    # is alarming in a way a truncated capture is not, and
                    # interfering with the live session is the one thing this
                    # feature promised not to do. A capture is a nicety; their
                    # session is the job.
                    capture = session.get("live_capture")
                    if data and capture is not None and capture.state != "done":
                        capture.abort("you started typing")

                    # A line-paced paste stops on a keystroke for the same
                    # reason, and it matters more here: the batch is typing
                    # into the session, so a user reaching for the keyboard is
                    # either stopping it or about to collide with it (#523).
                    batch = session.get("paste_batch")
                    if data and batch is not None and not batch.done:
                        batch.abort("you started typing")
                        await _paste_finished(batch)

                    # Typing is activity. Without this the keep-alive would
                    # measure only the device's silence, and nudge a session
                    # somebody is actively working in but which has not
                    # answered yet.
                    if data:
                        session["_last_activity"] = time.monotonic()

                    if data and handler.is_connected:
                        # Everything the user sends goes through the pipeline,
                        # which assembles keystrokes into lines and may rewrite
                        # one (an alias) before it reaches the device.
                        outbound = session["pipeline"].process(data)
                        await asyncio.to_thread(
                            handler.send, outbound.encode("utf-8", errors="replace")
                        )

                        # A destructive command is held rather than sent. The
                        # device has had CTRL_U — enough to clear what it
                        # echoed — and nothing else, so an unanswered prompt
                        # leaves it untouched. One prompt per hold (#343): a
                        # pasted batch can hold more than one, and reading a
                        # single slot silently discarded all but the last.
                        for held in session["pipeline"].newly_held:
                            await websocket.send_text(json.dumps({
                                "type":    "guardrail_prompt",
                                "command": held,
                                # Named, because the whole point of this is the
                                # tab you thought you were in.
                                "device":  session.get("hostname")
                                           or session.get("display_label")
                                           or "this device",
                            }))

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

                elif msg_type == "keep_alive":
                    # Per-tab override (#138). None means "follow the setting";
                    # True and False are this tab's own answer, which is the
                    # point — the one session worth keeping is usually a
                    # console during an upgrade, and turning it on globally to
                    # protect one session is the wrong shape.
                    wanted = msg.get("enabled")
                    session["keep_alive"] = None if wanted is None else bool(wanted)
                    session["_last_activity"] = time.monotonic()
                    session["_keep_alive_told"] = False

                elif msg_type == "dismiss_pending":
                    # The user's explicit "stop tracking this" (#265). The
                    # tracker cannot always see a cancel scroll past, and a
                    # pending with no readable time otherwise sits in the
                    # status bar for the six-hour stale limit. Nothing is
                    # sent to the device — only the tracking ends.
                    await note_pending([session["alerts"].clear()])

                elif msg_type in ("logging_changed", "logging"):
                    # "logging_changed": the browser saved the setting. One
                    # round trip on a deliberate action, rather than reading
                    # settings.json for every chunk of device output — which
                    # is what this replaced.
                    #
                    # "logging": this tab's own answer (#534), the same shape
                    # as keep_alive. Absent means "follow the setting"; True
                    # and False are this tab's, which is the point — "start
                    # logging this one now", ten minutes into a change on the
                    # core, should not turn logging on for eleven other tabs.
                    # A message with no `enabled` key only asks for the state,
                    # so a reattached socket can learn it without wiping the
                    # override the session is already carrying.
                    if msg_type == "logging" and "enabled" in msg:
                        wanted = msg.get("enabled")
                        session["log_override"] = (
                            None if wanted is None else bool(wanted))
                    await asyncio.to_thread(_close_session_log, session)
                    # Decided now rather than at the next chunk of output, so
                    # the answer sent back is the real one: a tab logging to
                    # nothing while its menu says "on" is the failure this
                    # whole feature is about noticing.
                    await asyncio.to_thread(_open_session_log, session, session_id)
                    session["_log_checked"] = True
                    session.pop("_log_recheck", None)
                    log_path = session.get("_log_path")
                    await websocket.send_text(json.dumps({
                        "type": "logging_state",
                        "enabled": log_path is not None,
                        "filename": log_path.name if log_path is not None else "",
                        "override": session.get("log_override"),
                    }))

                elif msg_type == "watch_changed":
                    # Colour rules were saved. Same round trip, same reason
                    # (#521): a rule marked "alert" has to start watching now,
                    # not at the next tab — it is usually written in the middle
                    # of the change it is meant to watch.
                    session["_watch_recheck"] = True

                elif msg_type == "guardrail_answer":
                    # The answer to a held command. Confirmed, it goes now;
                    # refused, it is dropped and the terminal says so. The
                    # answer names its command (#343) so two prompts answered
                    # out of order each act on their own hold; an old client
                    # that sends no name gets the oldest.
                    pipeline = session["pipeline"]
                    if msg.get("confirmed"):
                        outbound = pipeline.release(msg.get("command") or None)
                        if outbound and handler.is_connected:
                            logger.info("Confirmed at the guardrail and sent: %r",
                                        outbound.strip())
                            await asyncio.to_thread(
                                handler.send,
                                outbound.encode("utf-8", errors="replace"),
                            )
                            # Same watch as any other command: `reload in 10`
                            # schedules itself whether it was confirmed or not.
                            await note_pending(
                                [session["alerts"].observe_command(outbound.strip())]
                            )
                    else:
                        dropped = pipeline.drop(msg.get("command") or None)
                        if dropped:
                            logger.info("Cancelled at the guardrail: %r", dropped)
                            # The tracker armed at Enter, before this question
                            # was answered — declining must take the pending
                            # with it, or the bar counts down to a reload the
                            # device never received (#248).
                            await note_pending([session["alerts"].retract()])
                            await websocket.send_text(json.dumps({
                                "type": "output",
                                "data": CANCELLED_NOTE.format(command=dropped),
                            }))

                    # A line-paced paste pauses at a held line rather than
                    # skipping past it, and this is where it starts again
                    # (#523). Confirmed, something has just gone to the device
                    # and the batch waits for the answer; refused, nothing
                    # reached it and there is nothing to wait for.
                    batch = session.get("paste_batch")
                    if batch is not None and not batch.done:
                        if msg.get("confirmed"):
                            batch.wait_for_device()
                        else:
                            batch.resume()

                elif msg_type == "resize":
                    cols = int(msg.get("cols", 80))
                    rows = int(msg.get("rows", 24))
                    await asyncio.to_thread(handler.resize, cols, rows)

                elif msg_type == "break":
                    # Serial only: the break signal used to drop a booting
                    # Cisco device into ROMMON.
                    if hasattr(handler, "send_break"):
                        await asyncio.to_thread(handler.send_break)

                elif msg_type == "paste_lines":
                    # A block the user has already seen and approved, to be
                    # sent a line at a time (#523). Paced here rather than in
                    # the browser because the pacing that matters is "the
                    # device is back at its prompt", which only this side can
                    # see — and it has to work on serial, telnet and devices
                    # nothing could identify, which is what config push
                    # cannot do.
                    await _start_paste(msg)

                elif msg_type == "paste_stop":
                    stopping = session.get("paste_batch")
                    if stopping is not None and not stopping.done:
                        stopping.abort("you stopped it")
                        await _paste_finished(stopping)

        return

    async def _start_paste(msg: dict) -> None:
        """Take a paste batch from the browser and start it."""
        running = session.get("paste_batch")
        if running is not None and not running.done:
            running.abort("another paste started")
            await _paste_finished(running)

        lines = [str(line) for line in (msg.get("lines") or [])]
        if len(lines) > PASTE_MAX_LINES:
            # Refused rather than truncated. A configuration block cut off
            # at line 5000 is the worst of both: it reaches the device, and
            # nobody knows where it stopped.
            await websocket.send_text(json.dumps({
                "type": "paste_batch", "state": "refused",
                "total": len(lines), "limit": PASTE_MAX_LINES}))
            return
        if not lines:
            return

        def bounded(key: str, value) -> int:
            """Whatever the browser sent, inside the registry's own bounds."""
            setting = advanced_settings.SETTINGS_BY_KEY[key]
            return int(setting.clamp(advanced_setting(key)
                                     if value is None else value))

        wait = str(msg.get("mode") or "prompt") != "lines"
        batch = pipeline_module.PasteBatch(
            lines=lines,
            wait_for_prompt=wait,
            delay=bounded("terminal.paste_line_delay",
                          msg.get("delay_ms")) / 1000.0,
            timeout=float(bounded("terminal.paste_prompt_timeout",
                                  msg.get("timeout_s"))),
        )
        # The clock starts now, not when the object was built: the timeout is
        # "this line waited too long", and the first line has not waited at
        # all yet. Nothing is typed into the session before this, so the
        # prompt the transcript is describing is the current one and the
        # first line may go against it.
        batch.hold()
        session["paste_batch"] = batch
        logger.info("Pasting %d lines into %s (%s)", len(lines),
                    session.get("hostname") or session_id[:8],
                    "at the prompt" if wait else "timed")
        await websocket.send_text(json.dumps({
            "type": "paste_batch", "state": "start",
            "total": len(lines), "mode": "prompt" if wait else "lines"}))

    async def drive_paste_batch() -> None:
        """
        Send the next line of a pasted block, when the device is ready for it.

        Runs on the idle path of the read loop, alongside the on-connect
        script and live capture, and under the same two rules. **One line per
        prompt**: nothing goes out until the device is idle at a bare prompt
        with nothing half-typed and no guardrail question outstanding.
        **Through the pipeline**: a `reload` in a pasted block is held for
        confirmation exactly as a typed one is, and the batch waits for the
        answer rather than sending the next line past it.
        """
        batch = session.get("paste_batch")
        if batch is None or batch.done:
            return

        if not handler.is_connected:
            batch.abort("the session dropped")
            await _paste_finished(batch)
            return

        pipeline = session["pipeline"]
        if pipeline.held_commands:
            # Waiting on a person, not on a device: hold the clock, or the
            # batch times out behind its own confirmation dialog.
            batch.hold()
            return

        at_prompt = bool(getattr(session["transcript"], "idle_at_prompt", False)) \
            and not pipeline.current_line

        line = batch.next_line(at_prompt)
        if line is None:
            if batch.done:
                await _paste_finished(batch)
            return

        outbound_line = pipeline.process(line + "\r")
        if outbound_line:
            await asyncio.to_thread(
                handler.send, outbound_line.encode("utf-8", errors="replace"))

        for held in pipeline.newly_held:
            await websocket.send_text(json.dumps({
                "type":    "guardrail_prompt",
                "command": held,
                "device":  session.get("hostname")
                           or session.get("display_label") or "this device",
            }))

        expansion = pipeline.last_expansion
        if expansion:
            await websocket.send_text(json.dumps({
                "type": "alias_expanded",
                "typed": expansion[0], "sent": expansion[1]}))

        await note_pending(
            session["alerts"].observe_command(command)
            for command in pipeline.completed_commands
        )

        await websocket.send_text(json.dumps({
            "type": "paste_batch", "state": "progress",
            **batch.summary()}))

    async def _paste_finished(batch) -> None:
        """Say how far it got. The half that did not go out is the point."""
        session["paste_batch"] = None
        summary = batch.summary()
        if summary["reason"]:
            logger.info("Paste stopped after %d of %d lines (%s)",
                        summary["sent"], summary["total"], summary["reason"])
        await websocket.send_text(json.dumps({
            "type": "paste_batch", "state": "done", **summary}))

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

        # A bare prompt with nothing typed after it (#474). last_prompt is
        # never cleared, so it alone meant "has ever seen a prompt", and the
        # paging command landed in a line the user had started.
        at_prompt = bool(getattr(session["transcript"], "idle_at_prompt", False)) \
            and not session["pipeline"].current_line
        if not onboarder.ready(at_prompt):
            return

        terminal_settings = get_settings().get("terminal", {})

        # The setting is resolved before the summary is built, not after, so
        # "paging_command" means "this was sent" everywhere. It previously
        # reported the platform's command regardless, and the note in the
        # corner claimed to have sent something to a user who had turned the
        # feature off.
        # What the user last said this device is, if they ever said. Also
        # picks up the platform a scan wrote from the device's SSH banner,
        # which was stored and read by nothing.
        remembered = await asyncio.to_thread(
            profiles_module.remembered_platform,
            session.get("address") or session.get("hostname", ""),
            session.get("port", 0),
            session.get("username", ""),
        )

        summary = onboarder.run(
            session["transcript"].last_prompt,
            auto_paging=bool(terminal_settings.get("auto_paging_off", True)),
            remembered=remembered,
        )
        session["fingerprint"] = summary

        # Version and model are known the moment the device is identified;
        # keep them against the saved connection (#536) rather than losing
        # them with the tab.
        try:
            facts = {k: str((summary.get("device") or {}).get(k) or "")
                     for k in ("version", "model")}
            if any(facts.values()):
                await asyncio.to_thread(
                    profiles_module.record_inventory,
                    session.get("address") or "", int(session.get("port") or 0),
                    session.get("username") or "", facts,
                    session.get("profile_id") or "")
        except Exception as exc:
            logger.debug("Could not record what the device is: %s", exc)
        session["pipeline"].platform = summary["platform"]
        # Which patterns a pending reload is recognised by depends on the
        # platform, so the tracker is idle until the device is identified.
        session["alerts"].platform = summary["platform"]

        session["pipeline"].expand_aliases = bool(
            terminal_settings.get("expand_aliases", True)
        )

        await websocket.send_text(
            json.dumps({"type": "device_identified", **summary})
        )

        if summary["paging_command"] and handler.is_connected:
            await asyncio.to_thread(
                handler.send, (summary["paging_command"] + "\r").encode()
            )
            # The on-connect script must not follow it into a device that has
            # not answered it yet (#532). `idle_at_prompt` still describes the
            # prompt this command was sent at until output arrives.
            session["_on_connect_wait"] = True

    async def maybe_run_on_connect() -> None:
        """
        Send the saved connection's own lines, once, after onboarding (#532).

        The first thirty seconds on every device are the same thirty seconds,
        so a connection may carry its own: `enable`, `terminal monitor`, a
        screen width, entering a VDOM. It is also the only honest answer for
        the platforms where paging-off is per-user or per-context and cannot
        be a platform default.

        Runs on the same idle path `drive_live_capture` does, and under the
        same two rules onboarding obeys. **One line per prompt**: the device
        must be idle at a bare prompt, with nothing half-typed and no
        guardrail question outstanding, before anything else is sent.
        **Through the pipeline**: a `reload` written into a script is held for
        confirmation exactly as one that was typed, and an alias in a script
        expands exactly as it would from the keyboard.
        """
        if not session.get("_on_connect_ready"):
            # Nothing before the device has been identified and the paging
            # command has gone out: two things typing into the session at
            # once is the collision this whole area exists to avoid.
            if not session["onboarder"].done:
                return
            session["_on_connect_ready"] = True
            lines = await asyncio.to_thread(
                profiles_module.on_connect_for, session.get("profile_id") or "")
            script = OnConnectScript(lines=lines) if lines else None
            session["on_connect"] = script
            paging_just_sent = session.pop("_on_connect_wait", False)
            if script is not None:
                if paging_just_sent:
                    script.wait_for_device()
                # Announced before anything is sent, not after. The rule is
                # that the user can account for everything that reached the
                # device, and a list that arrives afterwards is a receipt
                # rather than a warning.
                logger.info("Running the on-connect script for %s: %s",
                            session.get("hostname") or session_id[:8], lines)
                await websocket.send_text(json.dumps({
                    "type": "on_connect", "state": "start", "lines": lines}))

        script = session.get("on_connect")
        if script is None or script.done:
            return

        if not handler.is_connected:
            script.finish("disconnected")
            await _on_connect_finished(script)
            return

        pipeline = session["pipeline"]
        at_prompt = bool(getattr(session["transcript"], "idle_at_prompt", False)) \
            and not pipeline.current_line and not pipeline.held_commands

        line = script.next_line(at_prompt)
        if line is None:
            if script.done:
                await _on_connect_finished(script)
            return

        outbound = pipeline.process(line + "\r")
        if outbound:
            await asyncio.to_thread(
                handler.send, outbound.encode("utf-8", errors="replace"))

        # The guardrail held it. Same message the keyboard path sends, so the
        # same dialog appears — and the script waits, because held_commands
        # keeps at_prompt False until somebody answers.
        for held in pipeline.newly_held:
            await websocket.send_text(json.dumps({
                "type":    "guardrail_prompt",
                "command": held,
                "device":  session.get("hostname")
                           or session.get("display_label") or "this device",
            }))

        expansion = pipeline.last_expansion
        if expansion:
            await websocket.send_text(json.dumps({
                "type": "alias_expanded",
                "typed": expansion[0], "sent": expansion[1]}))

        await note_pending(
            session["alerts"].observe_command(command)
            for command in pipeline.completed_commands
        )

    async def _on_connect_send_enable(script) -> None:
        """
        Answer the `Password:` an `enable` produced.

        The password is read from the vault here, at the moment it is typed,
        and held nowhere else — not on the session, not on the connection
        parameters, which are scrubbed the instant authentication succeeds.

        Sent raw rather than through the pipeline, exactly as the paging
        command is. A password is not a command: put through the pipeline it
        would be assembled into a line, matched against the alias table and
        the dangerous-command list, and published as a command that was run —
        which is how a secret ends up in a transcript.
        """
        secret = await asyncio.to_thread(
            profiles_module.enable_password, session.get("profile_id") or "")
        if not secret:
            # Said out loud rather than left as a session that quietly never
            # became privileged, with the rest of the script failing one
            # unhelpful line at a time.
            script.finish("no-enable-password")
            await _on_connect_finished(script)
            return
        await asyncio.to_thread(
            handler.send, (secret + "\r").encode("utf-8", errors="replace"))
        script.answered()
        await websocket.send_text(json.dumps({
            "type": "on_connect", "state": "enable"}))

    async def _on_connect_finished(script) -> None:
        """Say what was sent and what was not. The second half is the point."""
        summary = script.summary()
        if summary["skipped"] or summary["reason"]:
            logger.info("On-connect script stopped after %d of %d lines (%s)",
                        len(summary["sent"]),
                        len(summary["sent"]) + len(summary["skipped"]),
                        summary["reason"] or "finished")
        await websocket.send_text(json.dumps({
            "type": "on_connect", "state": "done", **summary}))

    async def maybe_keep_alive() -> None:
        """
        Stop the *device* idling this session out.

        `ssh.keepalive_seconds` already covers the network: an SSH keepalive
        keeps a firewall or jump host from dropping the TCP connection. It
        never reaches the shell, so it does nothing about `exec-timeout` —
        which is what actually closes the session on IOS, and what people mean
        when they ask for this. Only input the device *sees* resets that.

        So this types, and what it types is chosen to net to nothing: a space
        and then a backspace. The device counts two characters of input and
        the input line is left exactly as it was found. A bare backspace would
        be quieter but rings the bell on some platforms; a carriage return
        would submit whatever is half-typed, which is unthinkable.

        Three conditions, each of which is a way this goes wrong:

        - **Only at a prompt.** `transcript.last_prompt` is the existing
          "device is idle" signal. A nudge landing mid-command would insert a
          space into what somebody is typing.
        - **Only when the session has been quiet.** An active session never
          needs it and must never be touched.
        - **Only when asked for**, globally or for this tab. It is off by
          default because it is the one feature that types on a timer.

        Announced once per session rather than every time. ShellMate's rule is
        that nothing is sent silently, and forty announcements an hour would
        obey the letter of that while destroying its purpose.
        """
        # The two values are cached with a short shelf life (#340). This runs
        # on every idle poll — twice a second per session — and get_settings()
        # is a file read, a JSON parse and a deep merge each call; a dozen
        # idle tabs cost ~48 reads a second forever. The comment on the
        # logging path below records this exact pattern being removed once
        # already. A five-second staleness on a toggle that acts at 120s
        # intervals costs nothing.
        now = time.monotonic()
        cached = session.get("_keep_alive_cache")
        if not cached or now - cached[0] > 5.0:
            terminal_cfg = get_settings().get("terminal", {})
            cached = (now,
                      bool(terminal_cfg.get("keep_alive", False)),
                      float(terminal_cfg.get("keep_alive_seconds", 120)) or 120)
            session["_keep_alive_cache"] = cached
        _, default_wanted, interval = cached

        wanted = session.get("keep_alive")
        if wanted is None:
            wanted = default_wanted
        if not wanted or not handler.is_connected:
            return
        last = session.get("_last_activity") or now
        if now - last < interval:
            return

        # Mid-command is the one moment this must not happen.
        if not session["transcript"].last_prompt:
            return

        session["_last_activity"] = now
        # The nudge is a space then a backspace, so it nets to nothing.
        # \x08 spelled out rather than embedded as a raw byte: an invisible
        # control character in a string literal defeats every reader and
        # every search tool that ever looks at this line.
        await asyncio.to_thread(handler.send, b" \x08")

        if not session.get("_keep_alive_told"):
            session["_keep_alive_told"] = True
            await websocket.send_text(json.dumps({
                "type": "keep_alive_active",
                "seconds": int(interval),
            }))

    async def drive_live_capture() -> None:
        """
        Start or time out a capture running through the user's own session.

        Called from the idle path of the read loop, which is the only place
        that knows the device has stopped talking. capture_config_live() waits
        on a thread for this to finish; it never reads the channel itself,
        because two readers on one channel race for every byte.
        """
        capture = session.get("live_capture")
        if capture is None:
            return

        if capture.ready_to_start(bool(session["transcript"].last_prompt)):
            capture.started()
            await asyncio.to_thread(
                handler.send, (capture.command + "\r").encode())
            return

        # A device that goes quiet mid-configuration without printing its
        # prompt again would otherwise hold the capture open to the timeout.
        capture.tick()

    async def read_from_channel() -> None:
        """Forward device output to the browser and the session buffer."""
        # One reader thread per session (#471), started on the first attach
        # and living as long as the connection. It used to be a to_thread()
        # per recv: every open tab permanently held one slot of the loop's
        # default pool (12 on an 8-core laptop), and every other to_thread
        # in the application — each keystroke's send, session creation,
        # every store query, SFTP — queued behind them. With more tabs
        # than slots, sessions starved each other for input and output.
        #
        # The thread banks bytes into the session under a lock and wakes
        # whichever browser is attached; nothing is consumed off the channel
        # by a task that then gets cancelled (#344), and the read-modify-
        # write on the bank is no longer a race between two threads. While
        # no browser is attached it does not read at all, so output waits
        # in the channel as it always did rather than piling up in memory.
        loop = asyncio.get_running_loop()
        wake = asyncio.Event()
        bank_lock = session.setdefault("_recv_lock", threading.Lock())
        session["_recv_notify"] = (loop, wake)
        _ensure_reader_thread(session, handler, bank_lock)

        def take_banked() -> bytes:
            with bank_lock:
                return session.pop("_recv_pending", b"")

        try:
            while True:
                # Banked output first — bytes the reader put by while this
                # task was being (re)created (#344).
                data_bytes = take_banked()
                if not data_bytes:
                    if session.get("_reader_closed"):
                        data_bytes = b""              # the far end closed
                    else:
                        # See ConnectionHandler.recv: None means idle. The
                        # half-second tick is what the idle work below —
                        # onboarding, live capture, keep-alive — runs on.
                        #
                        # A running paste is the exception (#523): at half a
                        # second a batch asked for a line every 200ms would
                        # get one every 500, and a device that came back to
                        # its prompt would sit there for the rest of the tick.
                        # It costs one wake-up every 50ms, and only while a
                        # paste is actually running.
                        idle_tick = 0.05 if session.get("paste_batch") else 0.5
                        try:
                            await asyncio.wait_for(wake.wait(), timeout=idle_tick)
                            wake.clear()
                            continue
                        except asyncio.TimeoutError:
                            data_bytes = None

                if data_bytes is None:
                    # Idle, but onboarding may still be due — see maybe_onboard.
                    try:
                        await maybe_onboard()
                    except Exception as exc:
                        logger.warning("Onboarding failed for %s: %s", session_id, exc)
                    # Idle is also where a live capture starts: the device is
                    # at a prompt and saying nothing, which is the only safe
                    # moment to type into somebody else's session.
                    try:
                        await drive_live_capture()
                    except Exception as exc:
                        logger.warning("Live capture failed for %s: %s", session_id, exc)
                    # And the saved connection's own lines (#532), for the
                    # same reason: idle at a prompt is the only safe moment
                    # to type into somebody's session.
                    try:
                        await maybe_run_on_connect()
                    except Exception as exc:
                        logger.warning("On-connect script failed for %s: %s",
                                       session_id, exc)
                    # And the next line of a pasted block (#523) — same idle
                    # path, same reason: at a prompt and saying nothing is
                    # the only safe moment to type into somebody's session.
                    try:
                        await drive_paste_batch()
                    except Exception as exc:
                        logger.warning("Paste batch failed for %s: %s",
                                       session_id, exc)
                    try:
                        await maybe_keep_alive()
                    except Exception as exc:
                        logger.warning("Keep-alive failed for %s: %s", session_id, exc)
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
                # Real output means the session is not idle, so the keep-alive
                # timer restarts. Without this it would nudge a busy session.
                session["_last_activity"] = time.monotonic()

                # A live capture takes its output here and nothing else sees
                # it. This has to come before every consumer below, not after
                # a convenient one: the browser would show a configuration
                # nobody asked for, the buffer would feed it to the AI as
                # context, the transcript would record it as a command that
                # was typed, and the log would file it as evidence of what
                # happened in the session. Withholding it from four of five
                # places is the same bug four times over.
                capture = session.get("live_capture")
                if capture is not None and capture.active:
                    answer = capture.feed(text)
                    if answer:
                        # A pager, answered rather than disabled — changing
                        # the user's terminal length to suit us would outlast
                        # the capture.
                        await asyncio.to_thread(handler.send, answer.encode())
                    continue

                # Write to session buffer
                session_manager.write_to_buffer(session_id, text)

                # Reconstruct commands from the stream and record them. Any
                # failure here is logged and dropped: history is valuable, but
                # never worth interrupting a live session for.
                try:
                    for record in session["transcript"].feed(text):
                        # Off the loop: the store's writer thread commits
                        # it while output keeps flowing (#459).
                        store.submit(store.add_command, session_id, record)
                        # The last few, kept on the session for the
                        # assistant's structured view (#404). Bounded.
                        recent = session.get("recent_records")
                        if recent is None:
                            recent = session["recent_records"] = deque(maxlen=12)
                        recent.append(record)
                except Exception as exc:
                    logger.warning("Transcript error on session %s: %s", session_id, exc)

                # File logging, if it is switched on.
                #
                # This used to read settings.json, parse it, deep-merge it,
                # read it a second time for the directory, stat and mkdir,
                # then open/write/close the log — once per chunk of device
                # output, all of it blocking, all of it on the event loop
                # that every other session's terminal shares. A `show
                # tech-support` on one tab made the others stutter.
                #
                # The handle is opened once per session, not once per chunk —
                # and re-evaluated when the setting changes, which is signalled
                # rather than polled.
                #
                # "Applies to the next tab" was the wrong call here. That is
                # right for a font size, which is a property of how the
                # terminal was built. Logging is a decision about what to
                # record, and it is usually made in the middle of something:
                # ten minutes into a change you decide you want a record of
                # it, and it has to start now.
                if session.get("_log_recheck") or session.get("_log_checked") is None:
                    session.pop("_log_recheck", None)
                    session["_log_checked"] = True
                    await asyncio.to_thread(_open_session_log, session, session_id)

                if session.get("_log_path") is not None:
                    # Raw text in — buffering, per-line stamping and redaction
                    # all happen inside (#246), and still off the loop: a log
                    # directory on a network share can block for as long as
                    # the share feels like it.
                    await asyncio.to_thread(_append_to_log, session, text)

                # Send output to browser
                await websocket.send_text(json.dumps({"type": "output", "data": text}))

                # Collect banner text for identification, then run the same
                # onboarding check the idle path uses.
                try:
                    session["onboarder"].observe(text)
                    await maybe_onboard()
                except Exception as exc:
                    logger.warning("Onboarding failed for session %s: %s", session_id, exc)

                # A paste batch watches output for one thing: that the device
                # answered the last line at all (#523). Without it the batch
                # races its own output — see PasteBatch.
                try:
                    pasting = session.get("paste_batch")
                    if pasting is not None:
                        pasting.observe(text)
                except Exception as exc:
                    logger.warning("Paste batch failed for %s: %s", session_id, exc)

                # The on-connect script watches output for two things: that
                # the device answered the last line at all, and the
                # `Password:` an `enable` produces (#532).
                try:
                    running = session.get("on_connect")
                    if running is not None and running.observe(text):
                        await _on_connect_send_enable(running)
                    await maybe_run_on_connect()
                except Exception as exc:
                    logger.warning("On-connect script failed for %s: %s",
                                   session_id, exc)

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

                # The user's own watch rules (#521). Matched here rather than
                # in the browser because the browser only ever sees the tab in
                # front of somebody, and the whole point is the tab that is
                # not. Rules are loaded once and reloaded on a settings save,
                # for the reason session logging is: reading settings.json per
                # chunk of output is what that pattern replaced.
                if session.get("_watch_recheck") or session.get("_watch_loaded") is None:
                    session.pop("_watch_recheck", None)
                    session["_watch_loaded"] = True
                    await asyncio.to_thread(_load_watch_rules, session)

                try:
                    for hit in session["watch"].observe(text):
                        await websocket.send_text(json.dumps({"type": "watch_hit", **hit}))
                        # A critical watch also goes to the notification area,
                        # which is the only channel that reaches somebody with
                        # the window hidden. No tray, no notification, no
                        # error — the toast is waiting when they come back.
                        if hit.get("severity") == "critical":
                            await asyncio.to_thread(
                                desktop.notify,
                                f"{session.get('hostname') or session_id[:8]}: {hit['line']}",
                            )
                except Exception as exc:
                    logger.warning("Output watch failed for %s: %s", session_id, exc)

                # Name the tab after the device. Uses the shared cross-vendor
                # prompt parser, so Junos and PAN-OS are recognised as well as
                # IOS — the old pattern here understood Cisco only.
                if not session.get("_hostname_sent"):
                    detected = detect_hostname(text)
                    if detected:
                        await websocket.send_text(
                            json.dumps({"type": "hostname_detected", "hostname": detected})
                        )
                        session["_hostname_sent"] = True
                        # Connections are often opened by IP, so recording the
                        # real name is what makes searching by device work.
                        store.submit(store.update_session_hostname, session_id, detected)
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
    # The reader thread keeps running for the next attach; it just has
    # nobody to wake until then (#471).
    session["_recv_notify"] = None

    # No `is_connected = False` here (#331). The bridge ending says the
    # *browser* went away — a refresh, a hidden window — not that the device
    # did. The genuine close signals are recv() returning b"" and the error
    # path in read_from_channel, and both already write the flag.

    # A paste is somebody's hand on the keyboard, and the hand has gone: the
    # browser closed, or refreshed. Nothing may go on typing into a device
    # with nobody watching the result (#523).
    pasting = session.get("paste_batch")
    if pasting is not None and not pasting.done:
        pasting.abort("the window closed")
    session["paste_batch"] = None

    # The log handle is per-session now rather than per-chunk, so it has an
    # end as well as a beginning — including the partial last line.
    await asyncio.to_thread(_close_session_log, session)
    session.pop("_log_checked", None)

    logger.info("WebSocket closed for session %s", session_id)


# ---------------------------------------------------------------------------
# WebSocket — AI chat
# ---------------------------------------------------------------------------

def _auto_analysis_prompt(auto: dict, platform_id: str = "") -> str:
    """
    Compose the prompt for output that appeared after an approved command.

    Built here rather than in the browser so the device's reply goes through
    `outbound.redact_text()` like everything else that leaves the machine.
    Composed in the browser it did not: the message arrived as an ordinary
    user message with the configuration already inside it.

    Also capped. The watcher collects everything within its idle window with
    no limit, so an approved `show running-config` on a large chassis sent the
    lot — worth bounding on its own, before considering what is in it.
    """
    from backend.session.outbound import redact_text

    command = str(auto.get("command", ""))[:400]
    output = str(auto.get("output", ""))

    limit = int(advanced_setting("ai.analyse_output_lines"))
    lines = output.splitlines()
    trimmed = lines[-limit:] if limit and len(lines) > limit else lines
    dropped = len(lines) - len(trimmed)

    body = redact_text("\n".join(trimmed))
    if dropped > 0:
        body = f"[{dropped} earlier lines not sent]\n{body}"

    # The same output as rows, when a template covers the command (#404).
    # Parsed from the redacted text, so nothing masked above reappears.
    table = ""
    if platform_id and advanced_setting("ai.parse_output"):
        try:
            from backend.session import parsed
            rows = parsed.parse(platform_id, command, body)
            if rows is not None:
                table = "\n\n" + parsed.render(command, rows)
        except Exception:
            table = ""

    return (
        f"The user just ran `{command}` in the terminal and this output "
        f"appeared:\n```\n{body}\n```{table}\nAnalyse it naturally, as if you are "
        f"watching the terminal in real time. Do not say \"you ran\" or "
        f"reference receiving a message — just respond as an engineer who can "
        f"see the screen."
    )


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

    Checked before accepting: this endpoint needs no session id and no secret
    of any kind, and `context_mode: all` reads every open session.
    """
    if not await _origin_allowed(websocket):
        return
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
            context_session_ids = msg.get("context_session_ids") or None
            mode             = msg.get("mode") or None  # "learn" | "tshoot"
            # Earlier turns, as the browser kept them (#402). Trimmed and
            # shaped per provider in ai/turns.py.
            history          = msg.get("history") if isinstance(msg.get("history"), list) else None

            # Approving a suggested command used to also ship whatever the
            # device said back to the provider, with no setting governing it
            # and no redaction — the browser built the whole prompt from raw
            # xterm output, so the server-side masking never saw it. An
            # approved `show running-config` sent the configuration, hashes
            # and community strings included.
            #
            # The browser now sends the command and the output as data, and
            # the prompt is composed here, where the output can be recognised
            # as device output and masked like anything else leaving the
            # machine.
            auto = msg.get("auto_analysis")
            if auto:
                if not advanced_setting("ai.analyse_output"):
                    # Enforced here rather than in the browser. The browser
                    # check is a convenience; this is the guarantee.
                    continue
                platform_id = ""
                sess = session_manager.get_session(session_id) if session_id else None
                if sess:
                    try:
                        from backend.configs import session_platform
                        platform_id = session_platform(sess)
                    except Exception:
                        platform_id = ""
                user_message = _auto_analysis_prompt(auto, platform_id)

            # Explain a configuration diff (#549): the drift report for this
            # session, or any two stored captures. Composed server-side for
            # the same reason as the block above — it carries configuration,
            # so `outbound` has to see it before a provider does.
            diff_request = msg.get("diff_request")
            if isinstance(diff_request, dict):
                from backend.ai import explain
                sess = session_manager.get_session(session_id) if session_id else None
                user_message = await asyncio.to_thread(
                    explain.diff_prompt, diff_request, sess)
                if not user_message:
                    await websocket.send_text(json.dumps({
                        "type": "error",
                        "message": "There is no configuration diff to explain — "
                                   "ShellMate has no capture of this device yet.",
                    }))
                    # No "done" as well: the browser's error handler already
                    # closes the streaming bubble, and a second close pops the
                    # queue of pending analyses behind it.
                    continue

            # Review a proposed configuration change before it is applied
            # (#550). Nothing reaches the device: the preview is recomputed
            # against the *stored* capture, and the classified lines and the
            # stanzas they land in are masked here, not in the browser.
            review = msg.get("review_request")
            if isinstance(review, dict) and review.get("text"):
                sess = session_manager.get_session(session_id) if session_id else None
                if sess is None:
                    await websocket.send_text(json.dumps({
                        "type": "error",
                        "message": "That session is no longer open, so there is "
                                   "nothing to review the change against.",
                    }))
                    continue
                try:
                    from backend.ai import explain
                    user_message = await asyncio.to_thread(
                        explain.push_review_prompt, sess, str(review.get("text")))
                except Exception as exc:
                    await websocket.send_text(json.dumps({
                        "type": "error",
                        "message": f"The change could not be reviewed: {exc}",
                    }))
                    continue

            # Investigate mode (#403): how many steps have been approved.
            step = msg.get("investigate_step")
            investigate_step = int(step) if isinstance(step, (int, float)) else None

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
                    context_session_ids=context_session_ids,
                    model=model,
                    mode=mode,
                    history=history,
                    investigate_step=investigate_step,
                ):
                    if isinstance(chunk, dict) and "usage" in chunk:
                        # Real counts from the provider (#416), so the meter
                        # can stop estimating.
                        await websocket.send_text(
                            json.dumps({"type": "usage", **chunk["usage"]})
                        )
                        continue
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
