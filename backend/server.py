"""
server.py — Startup orchestration for ShellMate: port selection and
single-instance enforcement.

A portable executable cannot assume its preferred port is free.  The user may
already have something on 8765, or may have double-clicked the exe twice.
Both cases have to resolve without an error dialog, because the person running
this is a network engineer with a console cable in one hand, not a developer
reading a stack trace.

Single-instance detection deliberately uses the running server as its own
lock.  A PID file goes stale whenever the app is killed or the machine loses
power — and on a USB stick pulled from a crashed laptop, that is not a rare
event.  Here the lock file only records where to look; the authority is
whether something actually answers on that port.  A stale lock resolves itself
because nothing responds.
"""

import json
import logging
import socket
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from backend import paths

logger = logging.getLogger(__name__)

# Identifies our own HTTP responses, so we never mistake an unrelated service
# on the recorded port for a running ShellMate.
APP_MARKER = "shellmate-portable"

# How many consecutive ports to try before giving up.
PORT_SCAN_ATTEMPTS = 20


def _scan_attempts() -> int:
    """How many consecutive ports to try. Read here rather than at import."""
    try:
        from backend.advanced import get as advanced
        return int(advanced("diag.port_scan_attempts"))
    except Exception:
        return PORT_SCAN_ATTEMPTS


# ---------------------------------------------------------------------------
# Port selection
# ---------------------------------------------------------------------------


def is_port_free(host: str, port: int) -> bool:
    """
    Return True if *port* can be bound on *host* right now.

    SO_REUSEADDR is deliberately NOT set.  On Linux it would let us bind a port
    another process is already listening on, which is the opposite of what this
    check is for.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((host, port))
            return True
        except OSError:
            return False


def port_is_free(host: str, port: int) -> bool:
    """
    Whether nothing is listening on a port.

    Tests by *connecting*, not by binding. On Windows a bind with
    SO_REUSEADDR succeeds against a port another socket is already listening
    on, so a bind probe would report every busy port as free — which is
    precisely backwards for the one caller that matters here.
    """
    import socket as _socket

    with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as probe:
        probe.settimeout(0.4)
        return probe.connect_ex((host, port)) != 0


def wait_for_port(host: str, port: int, timeout: float = 20.0) -> bool:
    """
    Wait for a port to be released, for a restart handing one over.

    The outgoing copy stops listening as it spawns its replacement, but not
    instantly. Without this wait the replacement scans past the port it was
    asked for and comes up on the next one, leaving the user's window pointing
    at an address nothing answers on.

    Returns False on timeout, in which case the caller scans as usual: coming
    up on a different port is worse than not coming up at all.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if port_is_free(host, port):
            return True
        time.sleep(0.25)
    logger.info("Port %s never freed up; scanning instead.", port)
    return False


#: The live uvicorn Server, and the callable that starts a new one. Held here
#: rather than in run.py because the restart path lives in desktop.py, and
#: desktop.py importing the entry point would be a cycle.
_live_server = None
_serve_again = None


def register_server(instance, serve_again=None) -> None:
    """Record the running server so a restart can release its port."""
    global _live_server, _serve_again
    _live_server = instance
    if serve_again is not None:
        _serve_again = serve_again


def stop_serving(host: str, port: int, timeout: float = 10.0) -> bool:
    """
    Stop listening, so a replacement process can take the port.

    Returns False if the socket is still answering when the timeout expires,
    which the caller should treat as "do not restart" — handing over a port
    that was never released produces two copies fighting over one address.
    """
    if _live_server is None:
        return False
    _live_server.should_exit = True
    return wait_for_port(host, port, timeout)


def resume_serving(port: int) -> bool:
    """
    Take the port back after a restart that could not start its replacement.

    Without this, a failed handover leaves a process that is alive, holds the
    tray icon and every open session, and answers nothing.
    """
    if _serve_again is None:
        return False
    try:
        threading.Thread(target=_serve_again, args=(port,), daemon=True).start()
        logger.info("Resumed serving on port %s after an abandoned restart.", port)
        return True
    except Exception as exc:
        logger.error("Could not resume serving on port %s: %s", port, exc)
        return False


def find_free_port(host: str, preferred: int, attempts: int | None = None) -> int:
    """
    Return the first free port at or above *preferred*.

    Args:
        host:      Interface to bind on, normally 127.0.0.1.
        preferred: The port to try first.
        attempts:  How many consecutive ports to try.

    Returns:
        A port number that was free a moment ago.

    Raises:
        OSError: No free port found in the scanned range.

    Note there is an unavoidable race here: the port is free when we check and
    could be taken before uvicorn binds it.  The window is microseconds and the
    caller surfaces the bind error, so this is not worth locking against.
    """
    if attempts is None:
        attempts = _scan_attempts()

    for offset in range(attempts):
        candidate = preferred + offset
        if candidate > 65535:
            break
        if is_port_free(host, candidate):
            return candidate

    raise OSError(
        f"No free port found in range {preferred}-{preferred + attempts - 1}. "
        f"Set SHELLMATE_PORT to choose a different starting point."
    )


# ---------------------------------------------------------------------------
# Single-instance lock
# ---------------------------------------------------------------------------


def _probe_instance(port: int, timeout: float = 1.5) -> bool:
    """
    Return True if a live ShellMate is answering on *port*.

    Checks for our marker in the response rather than merely finding an open
    socket, so an unrelated service that has since claimed the port does not
    get mistaken for us.
    """
    url = f"http://127.0.0.1:{port}/api/system/info"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return payload.get("app") == APP_MARKER
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
        return False


def running_instance_port() -> int | None:
    """
    Return the port of an already-running ShellMate, or None.

    A lock file naming a port nothing responds on is treated as stale and
    ignored — the caller will overwrite it.
    """
    lock = paths.lock_file()
    if not lock.exists():
        return None

    try:
        recorded = json.loads(lock.read_text(encoding="utf-8"))
        port = int(recorded.get("port", 0))
    except (OSError, ValueError, json.JSONDecodeError):
        return None

    if port and _probe_instance(port):
        return port

    logger.info("Ignoring stale lock file for port %s", port)
    return None


def write_lock(port: int) -> None:
    """Record the port this instance is serving on, for the next launch to find."""
    lock: Path = paths.lock_file()
    try:
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text(json.dumps({"app": APP_MARKER, "port": port}), encoding="utf-8")
    except OSError as exc:
        # Losing the lock only costs us single-instance detection, so it must
        # never prevent startup.
        logger.warning("Could not write lock file: %s", exc)


def clear_lock() -> None:
    """Remove the lock file on clean shutdown."""
    try:
        paths.lock_file().unlink(missing_ok=True)
    except OSError:
        pass
