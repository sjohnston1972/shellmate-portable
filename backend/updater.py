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
_last_attempt: dict = {}     # what the helper's log said at startup, reported once


def attempt_log() -> Path:
    return updates_dir() / "apply-update.log"


def updates_dir() -> Path:
    return paths.data_dir() / "updates"


def state() -> dict:
    with _lock:
        out = dict(_state)
    out["licensed"] = licence.has_feature("updates")
    out["last_attempt"] = dict(_last_attempt)
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

            # Downloaded earlier and still intact: say so and stop. Opening the
            # modal twice used to fetch the file twice (#450).
            if _already_verified(target, expected, release["size"]):
                _set(phase="ready", path=str(target), received=target.stat().st_size, total=target.stat().st_size)
                logger.info("Update %s was already downloaded and verified: %s", version, target)
                return

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


def _already_verified(target: Path, expected: str, size: int) -> bool:
    """True when the file is there, the size the release says, and hashes right."""
    try:
        if not target.exists() or (size and target.stat().st_size != size):
            return False
        digest = hashlib.sha256()
        with open(target, "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        return digest.hexdigest() == expected
    except OSError:
        return False


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


def helper_script(current: Path, fresh: Path, port: int, pid: int, parent_pid: int = 0) -> str:
    """
    The batch file that does the swap after this process has gone.

    Waits for the PID to disappear — and the bootloader's, because a
    `--onefile` build is two processes and the parent keeps the executable
    open a moment after the child has exited — moves the running exe aside,
    moves the verified download into place, starts it, and gives it ninety
    seconds to answer on its port. If it does not, the old file comes back
    and is started instead.

    Three things learned the hard way (#450):

    - The helper has no console, so ``timeout`` fails at once and every
      wait was zero. Delays are ``ping`` against loopback instead.
    - The move is retried for half a minute rather than tried once, because
      the file is still held for a moment after the process is gone.
    - Nobody sees ``echo`` from a windowless script. Every step goes to
      ``apply-update.log`` beside it, ending ``OK`` or ``FAILED: why``, and
      the next start reads it (`tidy_after_launch`) so a failed update is
      said out loud rather than silently undone.
    """
    old = current.with_name(current.stem + ".old.exe")
    log = fresh.parent / "apply-update.log"
    lines = [
        "@echo off",
        "setlocal",
        "title ShellMate update",
        f"set CURRENT={current}",
        f"set FRESH={fresh}",
        f"set OLD={old}",
        f"set LOG={log}",
        f"set PID={pid}",
        f"set PORT={port}",
        'echo started %date% %time%> "%LOG%"',
        "echo Waiting for ShellMate to close...",
        ":wait",
    ]
    lines.append('tasklist /FI "PID eq %PID%" 2>NUL | find "%PID%" >NUL && (ping -n 2 127.0.0.1 >NUL & goto wait)')
    if parent_pid:
        lines.append(f'tasklist /FI "PID eq {parent_pid}" 2>NUL | find "{parent_pid}" >NUL && (ping -n 2 127.0.0.1 >NUL & goto wait)')
    lines += [
        'echo process gone>> "%LOG%"',
        "ping -n 2 127.0.0.1 >NUL",
        "set /a tries=0",
        ":aside",
        'if exist "%OLD%" del /f /q "%OLD%" >NUL 2>&1',
        'move /y "%CURRENT%" "%OLD%" >NUL 2>&1 && goto aside_done',
        "set /a tries+=1",
        "if %tries% lss 30 (ping -n 2 127.0.0.1 >NUL & goto aside)",
        'echo FAILED: could not move the old executable aside after 30 seconds; it is still in use>> "%LOG%"',
        "exit /b 1",
        ":aside_done",
        'move /y "%FRESH%" "%CURRENT%" >NUL 2>&1 || (echo FAILED: could not move the new executable into place>> "%LOG%" & move /y "%OLD%" "%CURRENT%" >NUL & exit /b 1)',
        'echo swapped; starting the new copy>> "%LOG%"',
        'start "" "%CURRENT%" --updated',
        "set /a tries=0",
        ":check",
        "ping -n 3 127.0.0.1 >NUL",
        f'powershell -NoProfile -Command "try {{ (Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 http://127.0.0.1:{port}/api/health).StatusCode }} catch {{ 0 }}" | find "200" >NUL && goto done',
        "set /a tries+=1",
        "if %tries% lss 45 goto check",
        'echo FAILED: the new copy did not answer on port %PORT% within 90 seconds. Putting the previous one back>> "%LOG%"',
        'taskkill /F /IM "' + current.name + '" >NUL 2>&1',
        "ping -n 2 127.0.0.1 >NUL",
        'move /y "%CURRENT%" "%FRESH%" >NUL 2>&1',
        'move /y "%OLD%" "%CURRENT%" >NUL 2>&1',
        'start "" "%CURRENT%"',
        "exit /b 1",
        ":done",
        'echo OK: the new copy answered on port %PORT%>> "%LOG%"',
        'del /f /q "%OLD%" >NUL 2>&1',
        "exit /b 0",
        "",
    ]
    return "\r\n".join(lines)


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
    parent = os.getppid() if paths.is_frozen() else 0      # the --onefile bootloader
    helper.write_text(helper_script(exe, fresh, port, os.getpid(), parent), encoding="utf-8")
    attempt_log().unlink(missing_ok=True)
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
    Called at startup. Reads what the helper's log says about the last swap:

    - ``OK`` — the helper finished; remove the log and any `.old.exe` it
      left. Returns the path removed, for the startup log.
    - ``FAILED`` — the helper put the previous copy back. Keep the reason in
      `_last_attempt` so the interface can say so once, and remove the log.
    - neither — the helper is still running its checks against this very
      copy. Touch nothing: it needs the `.old.exe` to roll back with.

    Without a log, a `.old.exe` older than ten minutes is a leftover from a
    helper that never got to its own cleanup, and goes.
    """
    global _last_attempt
    if not paths.is_frozen():
        return ""
    exe = Path(sys.executable)
    old = exe.with_name(exe.stem + ".old.exe")
    log = attempt_log()
    verdict = ""
    try:
        if log.exists():
            lines = [ln.strip() for ln in log.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()]
            verdict = lines[-1] if lines else ""
    except OSError:
        verdict = ""
    if verdict.startswith("FAILED"):
        _last_attempt = {"ok": False, "detail": verdict[len("FAILED:"):].strip()}
        logger.warning("The last update did not apply: %s", _last_attempt["detail"])
        log.unlink(missing_ok=True)
        return ""
    if log.exists() and not verdict.startswith("OK"):
        return ""                                    # the helper is mid-check
    if verdict.startswith("OK"):
        _last_attempt = {"ok": True, "detail": verdict[len("OK:"):].strip()}
        log.unlink(missing_ok=True)
    if old.exists():
        stale = verdict.startswith("OK") or (time.time() - old.stat().st_mtime) > 600
        if stale:
            try:
                old.unlink()
                logger.info("Removed the previous executable left by the updater.")
                return str(old)
            except OSError as exc:
                logger.info("The previous executable could not be removed yet: %s", exc)
    return ""
