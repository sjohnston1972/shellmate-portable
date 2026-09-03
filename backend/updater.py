"""
updater.py — Download a release and swap the executable (#443, #444, #448).

`app.py` already asks GitHub whether a newer release exists (#420). This is
the rest: fetching the executable into the data folder with progress,
checking it against the checksum CI publishes beside it, and handing off to
a helper that replaces the running file after this process has gone.

Rules that keep it safe:

- **Nothing is executed that has not been verified.** The download is
  refused, and deleted, if its SHA-256 does not match the release's
  ``.sha256`` asset. A release without one is refused too.
- **One download at a time, bounded by what the release says.** Size is
  taken from the release metadata; a stream that overruns it is abandoned.
- **The swap happens after this process exits.** A running executable
  cannot be overwritten on Windows, so a helper script waits for the process
  to go, renames the old file aside, moves the new one in, starts it, and
  puts the old one back if the new one does not come up.
- **Not while a device is mid-change.** Applying an update closes every
  session; a pending reload or commit-confirm refuses it outright.
- **Licensed only** (#448). The check is free; the download and the swap ask
  ``licence.has_feature("updates")`` and answer with the reason otherwise.
"""

import hashlib
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from backend import licence, paths

logger = logging.getLogger(__name__)

RELEASES_API = "https://api.github.com/repos/{repo}/releases/latest"
ASSET_NAME = "ShellMate-Portable.exe"
CHECKSUM_NAME = "ShellMate-Portable.exe.sha256"

_lock = threading.Lock()
_state: dict = {
    "phase": "idle",        # idle | downloading | verifying | ready | failed | applying
    "version": "",
    "received": 0,
    "total": 0,
    "path": "",
    "error": "",
    "started": 0.0,
}
_cancel = threading.Event()


def updates_dir() -> Path:
    return paths.data_dir() / "updates"


def state() -> dict:
    with _lock:
        out = dict(_state)
    out["licensed"] = licence.has_feature("updates")
    return out


def _set(**changes) -> None:
    with _lock:
        _state.update(changes)


# ---------------------------------------------------------------- release
def latest_release(repo: str) -> dict:
    """The latest release's version, notes, size, and asset URLs."""
    import httpx

    with httpx.Client(timeout=10.0, follow_redirects=True) as client:
        resp = client.get(RELEASES_API.format(repo=repo), headers={
            "Accept": "application/vnd.github+json", "User-Agent": "ShellMate-updater"})
    if resp.status_code == 404:
        return {"version": "", "note": "No release has been published yet."}
    if resp.status_code != 200:
        raise RuntimeError(f"GitHub answered {resp.status_code}.")
    data = resp.json()
    assets = {a.get("name"): a for a in data.get("assets") or []}
    exe = assets.get(ASSET_NAME) or {}
    sha = assets.get(CHECKSUM_NAME) or {}
    return {
        "version": str(data.get("tag_name") or "").lstrip("vV"),
        "published": data.get("published_at", ""),
        "url": data.get("html_url", ""),
        "notes": data.get("body") or "",
        "size": int(exe.get("size") or 0),
        "asset_url": exe.get("browser_download_url", ""),
        "checksum_url": sha.get("browser_download_url", ""),
    }


# ---------------------------------------------------------------- download
def start_download(repo: str) -> dict:
    """Begin fetching the latest release on a thread. Returns the state."""
    if not licence.has_feature("updates"):
        raise PermissionError(licence.status()["detail"])
    with _lock:
        if _state["phase"] in ("downloading", "verifying", "applying"):
            return dict(_state)
    _cancel.clear()
    _set(phase="downloading", received=0, total=0, error="", path="", started=time.time())
    threading.Thread(target=_download, args=(repo,), daemon=True, name="update-download").start()
    return state()


def cancel_download() -> dict:
    _cancel.set()
    return state()


def _download(repo: str) -> None:
    import httpx

    try:
        release = latest_release(repo)
        if not release.get("asset_url"):
            raise RuntimeError(release.get("note") or "The latest release carries no executable.")
        if not release.get("checksum_url"):
            raise RuntimeError("The release has no checksum beside the executable, so it cannot be verified.")
        version = release["version"]
        _set(version=version, total=release["size"])

        updates_dir().mkdir(parents=True, exist_ok=True)
        target = updates_dir() / f"ShellMate-Portable-{version}.exe"
        partial = target.with_suffix(".exe.part")

        with httpx.Client(timeout=30.0, follow_redirects=True) as client:
            expected = client.get(release["checksum_url"], headers={"User-Agent": "ShellMate-updater"}).text
            expected = expected.strip().split()[0].lower() if expected.strip() else ""
            if len(expected) != 64:
                raise RuntimeError("The checksum file is not a SHA-256.")

            digest = hashlib.sha256()
            received = 0
            limit = release["size"] or (200 * 1024 * 1024)
            with client.stream("GET", release["asset_url"], headers={"User-Agent": "ShellMate-updater"}) as resp:
                if resp.status_code != 200:
                    raise RuntimeError(f"The download answered {resp.status_code}.")
                with open(partial, "wb") as handle:
                    for chunk in resp.iter_bytes(65536):
                        if _cancel.is_set():
                            raise InterruptedError("Cancelled.")
                        handle.write(chunk)
                        digest.update(chunk)
                        received += len(chunk)
                        if received > limit + 4096:
                            raise RuntimeError("The download is larger than the release says it is.")
                        _set(received=received)

        _set(phase="verifying")
        actual = digest.hexdigest()
        if actual != expected:
            partial.unlink(missing_ok=True)
            raise RuntimeError("The download is not what the release says it is (checksum mismatch). It was deleted.")
        if target.exists():
            target.unlink()
        partial.rename(target)
        _set(phase="ready", path=str(target), received=received, total=received)
        logger.info("Update %s downloaded and verified: %s", version, target)
    except InterruptedError:
        _cleanup_partials()
        _set(phase="idle", error="", received=0, total=0)
    except Exception as exc:
        _cleanup_partials()
        logger.warning("Update download failed: %s", exc)
        _set(phase="failed", error=str(exc))


def _cleanup_partials() -> None:
    try:
        for part in updates_dir().glob("*.part"):
            part.unlink(missing_ok=True)
    except OSError:
        pass


# ---------------------------------------------------------------- apply
def blockers(session_manager) -> list[str]:
    """What stops the swap right now: devices with something pending."""
    names = []
    for session in session_manager.get_all_sessions():
        full = session_manager.get_session(session.get("session_id", ""))
        tracker = (full or {}).get("alerts")
        try:
            pending = tracker.payload().get("pending") if tracker else None
        except Exception:
            pending = None
        if pending:
            names.append(session.get("display_label") or session.get("hostname") or "a device")
    return names


def helper_script(current: Path, fresh: Path, port: int, pid: int) -> str:
    """
    The batch file that does the swap after this process has gone.

    Waits for the PID to disappear, moves the running exe aside, moves the
    verified download into place, starts it, and gives it a minute to answer
    on its port. If it does not, the old file comes back and is started
    instead. The `.old` file is left for the new copy to remove on a good
    launch (see `tidy_after_launch`).
    """
    old = current.with_name(current.stem + ".old.exe")
    return "\r\n".join([
        "@echo off",
        "setlocal",
        "title ShellMate update",
        f"set CURRENT={current}",
        f"set FRESH={fresh}",
        f"set OLD={old}",
        f"set PID={pid}",
        "echo Waiting for ShellMate to close...",
        ":wait",
        'tasklist /FI "PID eq %PID%" 2>NUL | find "%PID%" >NUL',
        "if not errorlevel 1 (timeout /t 1 /nobreak >NUL & goto wait)",
        "timeout /t 1 /nobreak >NUL",
        'if exist "%OLD%" del /f /q "%OLD%"',
        'move /y "%CURRENT%" "%OLD%" >NUL || (echo Could not move the old executable aside. & goto fail)',
        'move /y "%FRESH%" "%CURRENT%" >NUL || (echo Could not move the new executable into place. & move /y "%OLD%" "%CURRENT%" >NUL & goto fail)',
        'echo Starting the new ShellMate...',
        'start "" "%CURRENT%" --updated',
        "set /a tries=0",
        ":check",
        "timeout /t 2 /nobreak >NUL",
        f'powershell -NoProfile -Command "try {{ (Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 http://127.0.0.1:{port}/api/health).StatusCode }} catch {{ 0 }}" | find "200" >NUL',
        "if not errorlevel 1 goto done",
        "set /a tries+=1",
        "if %tries% lss 30 goto check",
        'echo The new ShellMate did not start. Putting the previous one back.',
        'taskkill /F /IM "' + current.name + '" >NUL 2>&1',
        'move /y "%CURRENT%" "%FRESH%" >NUL',
        'move /y "%OLD%" "%CURRENT%" >NUL',
        'start "" "%CURRENT%"',
        ":fail",
        "echo Update not applied. Press any key to close.",
        "pause >NUL",
        "exit /b 1",
        ":done",
        "exit /b 0",
        "",
    ])


def apply(session_manager, port: int) -> dict:
    """
    Hand the verified download to the helper and exit this process.

    Returns only on refusal; on success the process is gone before the
    response could be written, which the interface treats as the signal to
    wait for the new copy.
    """
    if not licence.has_feature("updates"):
        raise PermissionError(licence.status()["detail"])
    current = state()
    if current["phase"] != "ready" or not current["path"]:
        raise RuntimeError("There is no verified download to apply.")
    if not paths.is_frozen():
        raise RuntimeError("Running from source: replace the checkout with git, not with the updater.")
    if sys.platform != "win32":
        raise RuntimeError("The in-app swap is written for Windows; on this platform replace the file by hand.")
    held = blockers(session_manager)
    if held:
        raise RuntimeError("Not while something is pending on " + ", ".join(held)
                           + ". Let it finish or cancel it first.")

    exe = Path(sys.executable).resolve()
    fresh = Path(current["path"]).resolve()
    if not fresh.exists():
        raise RuntimeError("The downloaded file is gone. Download it again.")
    helper = updates_dir() / "apply-update.cmd"
    helper.write_text(helper_script(exe, fresh, port, os.getpid()), encoding="utf-8")
    _set(phase="applying")

    from backend import server
    try:
        server.clear_lock()
    except Exception as exc:
        logger.warning("Could not clear the instance lock before updating: %s", exc)

    creation = 0x00000008 | 0x00000200          # DETACHED_PROCESS | NEW_PROCESS_GROUP
    subprocess.Popen(["cmd", "/c", str(helper)], cwd=str(exe.parent), close_fds=True,
                     creationflags=creation, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    logger.info("Update helper started; this copy is exiting for the swap.")

    def leave() -> None:
        time.sleep(0.5)
        os._exit(0)
    threading.Thread(target=leave, daemon=True).start()
    return {"applying": True}


def tidy_after_launch() -> str:
    """
    Called at startup: a `.old.exe` beside the executable means the last
    update worked (this copy is running). Remove it and say so.
    """
    if not paths.is_frozen():
        return ""
    exe = Path(sys.executable)
    old = exe.with_name(exe.stem + ".old.exe")
    if old.exists():
        try:
            old.unlink()
            logger.info("Removed the previous executable left by the updater.")
            return str(old)
        except OSError as exc:
            logger.info("The previous executable could not be removed yet: %s", exc)
    return ""
