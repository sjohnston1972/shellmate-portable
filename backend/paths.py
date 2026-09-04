"""
paths.py — Single source of truth for every filesystem location ShellMate uses.

ShellMate ships as a portable single-file executable, which makes path
resolution subtle enough to be worth centralising here.

Under PyInstaller --onefile the process unpacks itself into a temporary
directory and sets ``sys._MEIPASS`` to point at it.  In that mode
``__file__`` resolves *inside that temp directory*, which the bootloader
deletes when the process exits.  Any code that writes user data relative to
``__file__`` therefore appears to work and then silently loses everything on
close.  Nothing in this codebase may derive a writable path from ``__file__``
— use :func:`data_dir` instead.

Three distinct roots, deliberately kept separate:

``app_dir()``       Where the executable itself lives.  Stable across runs.
``resource_dir()``  Read-only assets bundled *into* the executable.  Ephemeral.
``data_dir()``      Where user data is written.  Stable across runs, writable.
"""

import os
import sys
import tempfile
from pathlib import Path

# ---------------------------------------------------------------------------
# Frozen-vs-source detection
# ---------------------------------------------------------------------------

# The name of the data folder created alongside the executable.
DATA_DIR_NAME = "ShellMate-Data"

# Resolved once on first use and reused — probing writability on every call
# would mean disk I/O on every settings read.
_data_dir_cache: Path | None = None
_data_dir_is_fallback: bool = False


def is_frozen() -> bool:
    """True when running from a PyInstaller-built executable."""
    return getattr(sys, "frozen", False)


def app_dir() -> Path:
    """
    Return the directory the application lives in.

    Frozen: the folder containing the .exe — this is what makes the app
    portable, because it moves with the executable onto a USB stick.
    Source: the repository root.
    """
    if is_frozen():
        return Path(sys.executable).parent.resolve()
    return Path(__file__).parent.parent.resolve()


def resource_dir() -> Path:
    """
    Return the directory holding read-only bundled assets (frontend, vendor JS).

    Frozen: PyInstaller's temporary extraction directory, which exists only
    for the lifetime of the process.  Never write here — it is deleted on exit.
    Source: the repository root.
    """
    if is_frozen():
        # _MEIPASS is only defined by the PyInstaller bootloader
        return Path(getattr(sys, "_MEIPASS")).resolve()
    return Path(__file__).parent.parent.resolve()


# ---------------------------------------------------------------------------
# Writable data directory
# ---------------------------------------------------------------------------


def _is_writable(directory: Path) -> bool:
    """
    Return True if we can actually create and write files in *directory*.

    Checking permission bits is not reliable on Windows — a directory can look
    writable and still reject writes because of ACLs, a read-only USB stick, or
    a controlled-folder-access policy.  The only trustworthy test is to try.
    """
    try:
        directory.mkdir(parents=True, exist_ok=True)
        # delete=False then unlink, because Windows will not let us reopen a
        # NamedTemporaryFile that is still open.
        with tempfile.NamedTemporaryFile(dir=str(directory), delete=False) as probe:
            probe_path = Path(probe.name)
            probe.write(b"shellmate-write-probe")
        probe_path.unlink(missing_ok=True)
        return True
    except Exception:
        return False


def _user_data_fallback() -> Path:
    """
    Return the per-user data directory to fall back on when the app directory
    is not writable (running from Program Files, a read-only share, or a
    write-protected USB stick).
    """
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if base:
            return Path(base) / "ShellMatePortable"
    elif sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "ShellMatePortable"
    else:
        xdg = os.environ.get("XDG_DATA_HOME")
        if xdg:
            return Path(xdg) / "shellmate-portable"
        return Path.home() / ".local" / "share" / "shellmate-portable"

    return Path.home() / ".shellmate-portable"


def data_dir() -> Path:
    """
    Return the writable directory holding settings, profiles, logs and the
    session database.

    Prefers ``<app_dir>/ShellMate-Data`` so that a USB stick carries its own
    data and leaves no trace on the host machine.  Falls back to the per-user
    application data directory when that location cannot be written to.

    The result is cached for the life of the process, so the writability probe
    runs at most once.
    """
    global _data_dir_cache, _data_dir_is_fallback

    if _data_dir_cache is not None:
        return _data_dir_cache

    portable = app_dir() / DATA_DIR_NAME
    if _is_writable(portable):
        _data_dir_cache = portable
        _data_dir_is_fallback = False
        return _data_dir_cache

    fallback = _user_data_fallback()
    fallback.mkdir(parents=True, exist_ok=True)
    _data_dir_cache = fallback
    _data_dir_is_fallback = True
    return _data_dir_cache


def data_dir_is_fallback() -> bool:
    """
    True when the portable location was unavailable and per-user storage is in
    use instead.  The UI surfaces this so the user is never wrong about where
    their profiles actually live.

    Calls :func:`data_dir` first, since the answer is only known once the
    location has been resolved.
    """
    data_dir()
    return _data_dir_is_fallback


# ---------------------------------------------------------------------------
# Concrete file and directory locations
# ---------------------------------------------------------------------------


def settings_file() -> Path:
    """Path to settings.json."""
    return data_dir() / "settings.json"


def profiles_file() -> Path:
    """Path to the saved connection profiles."""
    return data_dir() / "profiles.json"


def known_hosts_file() -> Path:
    """
    Path to the host keys ShellMate has decided to trust (#528).

    Its own file, in the data directory, rather than the user's
    ``~/.ssh/known_hosts``. Two reasons, and both are the portable rule:
    a stick carried to another machine has to carry what it trusts with it,
    and ShellMate must never rewrite a file OpenSSH also owns — an entry
    added here is a decision made in ShellMate, and it stays where the rest
    of ShellMate's data is. The system file is still *read*, so a host
    already known to OpenSSH is already known here.
    """
    return data_dir() / "known_hosts"


def logs_dir() -> Path:
    """Directory holding session log files.  Created on demand."""
    return data_dir() / "logs"


def db_file() -> Path:
    """Path to the SQLite session store."""
    return data_dir() / "shellmate.db"


def lock_file() -> Path:
    """Path to the single-instance lock."""
    return data_dir() / "shellmate.lock"


def env_file() -> Path:
    """
    Path to the user's .env.

    Read from the app directory rather than the data directory so that a
    pre-seeded .env can ship next to the executable, which is how an
    administrator would hand out a preconfigured copy.
    """
    return app_dir() / ".env"


def frontend_dir() -> Path:
    """Directory holding the served frontend assets (read-only)."""
    return resource_dir() / "frontend"


# ---------------------------------------------------------------------------
# Migration from the pre-portable layout
# ---------------------------------------------------------------------------


def migrate_legacy_data() -> list[str]:
    """
    Move data written by the pre-portable layout into the data directory.

    Earlier versions wrote settings.json, profiles/saved.json and logs/ to the
    project root.  Anyone upgrading in place would otherwise appear to lose
    their profiles, so relocate them once.  Only moves a file when the new
    location is empty, so this is safe to run on every startup and will never
    overwrite newer data.

    Returns:
        Human-readable descriptions of what was moved, for the startup log.
    """
    moved: list[str] = []
    root = app_dir()
    target = data_dir()

    # Nothing to do when the old and new locations are the same folder.
    if root.resolve() == target.resolve():
        return moved

    simple_moves = [
        (root / "settings.json", target / "settings.json"),
        (root / "profiles" / "saved.json", target / "profiles.json"),
    ]

    for source, destination in simple_moves:
        if source.exists() and not destination.exists():
            try:
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(source.read_bytes())
                moved.append(f"{source.name} -> {destination}")
            except Exception:
                # A failed migration must never stop the app starting; the
                # user just gets defaults and can re-enter their settings.
                pass

    legacy_logs = root / "logs"
    if legacy_logs.is_dir():
        for log in legacy_logs.glob("*.log"):
            destination = logs_dir() / log.name
            if destination.exists():
                continue
            try:
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(log.read_bytes())
                moved.append(f"logs/{log.name}")
            except Exception:
                pass

    return moved
