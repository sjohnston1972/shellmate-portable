"""
version.py — What this copy of ShellMate is.

There was no version string anywhere (#420). The only answer to "which build
is this?" was the executable's modification time, which Explorer rewrites
when a file is copied — so a stale copy on a USB stick could carry today's
date. A bug report that says "it is still broken" needs something that
identifies the build, not the file.

Two things are recorded:

**VERSION** is the release, set here by hand and tagged in git as ``v<VERSION>``
when it ships. It is the number a person compares against a release page.

**Build information** is written by ``build.spec`` at packaging time into
``build_info.json`` inside the bundle: when the build ran and which commit it
was built from. A source checkout has no build step, so from source those
fields say so rather than guessing.

Nothing here reads the network. The update check lives in ``app.py`` and is
off unless asked for, because the tool is meant to work air-gapped and a
startup that stalls on a DNS lookup would be a regression on every site that
matters.
"""

import json
import subprocess
from datetime import datetime
from pathlib import Path

from backend import paths

#: The release. Bump this, tag ``v<VERSION>``, and the CI workflow publishes
#: the executable under that tag.
VERSION = "1.1.1"

#: Where the GitHub release page lives; the update check asks its API.
RELEASES_REPO = "sjohnston1972/shellmate-portable"

_BUILD_FILE = "build_info.json"


def build_info() -> dict:
    """
    The build record, or what a source checkout can say about itself.

    Keys: ``version``, ``built`` (ISO-8601 local time or ""), ``commit``
    (short hash or ""), ``frozen`` (bool).
    """
    info = {"version": VERSION, "built": "", "commit": "", "frozen": paths.is_frozen()}
    record = paths.resource_dir() / _BUILD_FILE
    if record.exists():
        try:
            data = json.loads(record.read_text(encoding="utf-8"))
            info["built"] = str(data.get("built", "")) or ""
            info["commit"] = str(data.get("commit", "")) or ""
            # The bundle's own idea of its version wins over the module's,
            # so a bundle built from a tag says the tag even if this file is
            # later edited in a checkout that runs beside it.
            if data.get("version"):
                info["version"] = str(data["version"])
            return info
        except (OSError, ValueError):
            pass
    if not paths.is_frozen():
        info["commit"] = _git_commit()
    return info


def describe() -> str:
    """``ShellMate 1.0.0 (a1b2c3d, built 2026-09-02 16:17)`` — for titles and logs."""
    info = build_info()
    bits = []
    if info["commit"]:
        bits.append(info["commit"])
    if info["built"]:
        bits.append("built " + _short_time(info["built"]))
    elif not info["frozen"]:
        bits.append("from source")
    return f"ShellMate {info['version']}" + (f" ({', '.join(bits)})" if bits else "")


def _short_time(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return iso


def _git_commit() -> str:
    """The checkout's short commit hash, or "" when git is not there to ask."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=Path(__file__).resolve().parent.parent,
            capture_output=True, text=True, timeout=3,
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def parse(version: str) -> tuple[int, ...]:
    """``"v1.2.3"`` → ``(1, 2, 3)``; anything unparseable → ``()``."""
    text = (version or "").strip().lstrip("vV")
    parts: list[int] = []
    for piece in text.split("."):
        digits = ""
        for ch in piece:
            if ch.isdigit():
                digits += ch
            else:
                break
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def is_newer(candidate: str, current: str = VERSION) -> bool:
    """Whether ``candidate`` is a later release than ``current``."""
    a, b = parse(candidate), parse(current)
    return bool(a) and bool(b) and a > b
