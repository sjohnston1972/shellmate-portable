"""
config_archive.py — Keeping the captured configurations as files.

ShellMate already captures a device's running configuration on connect and
stores it in ``shellmate.db``, which is what the drift check reads.  That is
the right home for comparing one visit against the last, and the wrong home
for everything else people want from a configuration: mailing it to a vendor,
dropping it into a change record, grepping a hundred of them at once, or
keeping them on a share where the backup system will see them.

So this writes a second copy as a plain file, where the user says.  Three
rules shape it, and each exists because of what a configuration file actually
contains and where it is likely to end up:

**Redacted like a session log.**  A running configuration carries password
hashes, pre-shared keys and community strings.  It is written to a folder the
user chose, which may be a share and will probably be backed up, so the same
masking that applies to session logs applies here.  Governed by the same
setting, because two switches for one promise is a promise nobody can rely on.

**Bounded.**  A capture per login, across a fleet, is unbounded growth in
somebody's Documents folder.  Three limits, all applied: how many are kept per
device, how old they may get, and how much room the whole archive may take.

**Never fatal.**  A failed write is a missing copy, not a broken session.  The
device is still there and the snapshot is still in the database; nothing here
is allowed to interrupt someone who is in the middle of working on a switch.
"""

import logging
import re
import time
from pathlib import Path

from backend.session.redact import redact
from backend.settings_store import config_directory, get_settings

logger = logging.getLogger(__name__)

# Captures are written with this extension so a folder of them can be
# associated with an editor, and so the sweep below never deletes anything it
# did not write.
SUFFIX = ".cfg"

# Anything a filesystem will not take, plus the separators — a device called
# "core/1" must not become a directory.
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _logging_settings(settings: dict | None = None) -> dict:
    """The logging section, read once and passed down (#468)."""
    if settings is not None:
        return settings
    return get_settings().get("logging", {}) or {}


def enabled(settings: dict | None = None) -> bool:
    """Whether captures should also be written out as files."""
    settings = _logging_settings(settings)
    return bool(settings.get("capture_configs", True)
                and settings.get("save_config_files", False))


def capture_enabled() -> bool:
    """
    Whether to capture configurations at all.

    Separate from :func:`enabled` because capture also feeds the drift check
    and the configuration history, both of which work with no files at all.
    """
    return bool((get_settings().get("logging", {}) or {}).get("capture_configs", True))


def safe_name(hostname: str) -> str:
    """Reduce a device name to something a filesystem will accept."""
    cleaned = _UNSAFE.sub("-", (hostname or "device").strip()).strip("-._")
    return cleaned[:64] or "device"


def archive(hostname: str, config: str, changed: bool = True,
            captured_at: float | None = None) -> dict:
    """
    Write one captured configuration to the archive.

    Args:
        hostname: The device it came from; becomes a folder.
        config: The configuration text, unredacted.
        changed: Whether it differs from the last capture of this device. An
            identical copy per login would spend the retention budget on
            duplicates and bury the captures that record an actual change —
            so an unchanged configuration is not written again, unless this
            device has nothing in the archive yet.
        captured_at: When it was captured. Defaults to now.

    Returns:
        A result the interface can show: ``{"written": bool, "path": str,
        "bytes": int, "redacted": bool, "reason": str}``. ``reason`` explains
        a ``written`` of false, and is empty otherwise.
    """
    settings = _logging_settings()
    if not enabled(settings):
        return _skip("Saving captures as files is switched off.")
    if not (config or "").strip():
        return _skip("The device returned no configuration to save.")

    settings = get_settings().get("logging", {}) or {}
    should_redact = bool(settings.get("redact_secrets", True))
    body = redact(config) if should_redact else config

    device = safe_name(hostname)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(captured_at or time.time()))
    root = config_directory()
    folder = root / device

    if not changed and _captures_in(folder):
        return _skip("Unchanged since the last capture, so nothing new was saved.")

    try:
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{device}-{stamp}{SUFFIX}"

        # Two logins inside the same second would otherwise overwrite each
        # other silently, which is the one outcome an archive must not have.
        counter = 2
        while path.exists():
            path = folder / f"{device}-{stamp}-{counter}{SUFFIX}"
            counter += 1

        path.write_text(body, encoding="utf-8")
    except OSError as exc:
        logger.warning("Could not write a configuration capture for %s: %s", hostname, exc)
        return _skip(f"Could not write to the archive folder: {exc}")

    result = {
        "written":  True,
        "path":     str(path),
        "folder":   str(path.parent),
        "bytes":    len(body.encode("utf-8", errors="replace")),
        "lines":    body.count("\n") + 1,
        "redacted": should_redact,
        "reason":   "",
    }

    # Pruning after the write, not before: the copy that was just taken is the
    # one worth having, and a sweep that failed must not stop it being kept.
    try:
        result["removed"] = prune(settings, device=device, root=root)
    except Exception as exc:                                # pragma: no cover
        logger.warning("Configuration archive sweep failed: %s", exc)
        result["removed"] = 0

    return result


_last_sweep = 0.0
SWEEP_EVERY = 24 * 3600


def prune(settings: dict | None = None, device: str | None = None, root: Path | None = None) -> int:
    """
    Apply the retention limits.

    With ``device`` given, only that device's per-device limit is applied —
    the cheap pass, run on every capture. The whole-archive sweep by age
    and total size, which stats every file (4,000 of them at 200 devices
    with 20 kept, possibly across a share), runs at most once a day
    (#468); the first capture after that interval pays for it.

    Three limits rather than one because they answer different worries: "I do
    not want fifty copies of the same switch" is not the same worry as "this
    folder must not fill the disk", and neither is "anything older than a year
    is no longer evidence of anything".

    Returns:
        How many files were removed.
    """
    global _last_sweep
    settings = _logging_settings(settings)
    keep_each = max(0, int(settings.get("config_keep_per_device", 20) or 0))
    max_age_days = max(0, int(settings.get("config_max_age_days", 365) or 0))
    max_total_mb = max(0, int(settings.get("config_max_total_mb", 200) or 0))

    root = root if root is not None else config_directory()
    if not root.exists():
        return 0

    removed = 0

    # Per device, newest first.
    folders = [root / device] if device else sorted(p for p in root.iterdir() if p.is_dir())
    for folder in folders:
        if not folder.is_dir():
            continue
        captures = _captures_in(folder)
        if keep_each and len(captures) > keep_each:
            for path, _mtime, _size in captures[keep_each:]:
                removed += _remove(path)

    if device and time.time() - _last_sweep < SWEEP_EVERY:
        return removed
    _last_sweep = time.time()

    # By age, then by total size — both across every device, since a limit on
    # the archive is a limit on the archive.
    remaining = _captures_in(root, recursive=True)

    if max_age_days:
        cutoff = time.time() - max_age_days * 86400
        for path, mtime, _size in list(remaining):
            if mtime < cutoff:
                removed += _remove(path)
                remaining.remove((path, mtime, _size))

    if max_total_mb:
        budget = max_total_mb * 1024 * 1024
        total = sum(size for _path, _mtime, size in remaining)
        # Oldest first: the newest capture of a device is the one most likely
        # to be wanted, and the one the drift check compares against.
        for path, mtime, size in reversed(remaining):
            if total <= budget:
                break
            removed += _remove(path)
            total -= size

    if removed:
        logger.info("Configuration archive sweep removed %s file(s)", removed)
    return removed


def summary() -> dict:
    """Describe the archive, for the settings panel."""
    root = config_directory()
    captures = _captures_in(root, recursive=True) if root.exists() else []
    devices = len({path.parent for path, _m, _s in captures})
    return {
        "path":     str(root),
        "exists":   root.exists(),
        "captures": len(captures),
        "devices":  devices,
        "bytes":    sum(size for _p, _m, size in captures),
        "newest":   max((mtime for _p, mtime, _s in captures), default=0),
    }


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _captures_in(folder: Path, recursive: bool = False) -> list[tuple[Path, float, int]]:
    """Return (path, mtime, size) for every capture, newest first."""
    pattern = f"**/*{SUFFIX}" if recursive else f"*{SUFFIX}"
    found: list[tuple[Path, float, int]] = []
    try:
        for path in folder.glob(pattern):
            try:
                stat = path.stat()
            except OSError:
                continue
            found.append((path, stat.st_mtime, stat.st_size))
    except OSError:
        return []
    found.sort(key=lambda item: item[1], reverse=True)
    return found


def _remove(path: Path) -> int:
    try:
        path.unlink()
        return 1
    except OSError as exc:
        logger.debug("Could not remove %s: %s", path, exc)
        return 0


def _skip(reason: str) -> dict:
    return {"written": False, "path": "", "folder": "", "bytes": 0,
            "lines": 0, "redacted": False, "reason": reason, "removed": 0}
