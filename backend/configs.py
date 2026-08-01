"""
configs.py — Configuration capture, storage and drift reporting.

Turns every login into a free change check.  On connect, the device's running
configuration is captured and compared against the last time you were there:
*"you were last here 12 days ago, 4 lines have changed."*  Nobody has to
remember to diff anything, and configuration drift stops being something you
only discover when it breaks.

The capture runs on a **second SSH channel**, opened on the transport the tab
already has (see ``SSHHandler.open_secondary_channel``).  That matters: the
alternative is typing into the user's live session, which would scroll their
screen and interleave a page of configuration with whatever they were doing.

Not every device cooperates.  Some switches cap concurrent sessions at one,
serial and telnet cannot multiplex at all, and the command differs by
platform.  All three are handled by returning a clear reason rather than
raising — a failed snapshot is a missing nicety, not a broken session.

The commands used come from the device's fingerprinted platform profile
(``backend/platforms.py``), which is user-editable, so a platform ShellMate
does not yet know can be taught rather than patched.
"""

import difflib
import logging
import re
import time

from backend import config_archive
from backend.connections.base import ConnectionError_
from backend.connections.ssh_handler import SSHHandler
from backend.fingerprint import identify
from backend.platforms import get_profile
from backend.session.ansi import clean, strip_pager_prompts
from backend.store import store

logger = logging.getLogger(__name__)

# How long to wait for a configuration to finish printing. A big chassis
# config over a slow WAN link genuinely takes this long.
CAPTURE_TIMEOUT = 60.0

# Quiet period after the last byte before deciding the output has finished.
# Long enough to survive a pause mid-transfer, short enough not to feel stuck.
IDLE_SETTLE = 1.5

# Platform-specific commands now live in backend/platforms.py, where they
# are user-editable, rather than being duplicated here.


def session_platform(session: dict) -> str:
    """
    Return the platform this session was fingerprinted as.

    Falls back to identifying from the prompt when onboarding has not finished
    — capture can be triggered manually before the first second is up.
    """
    stored = session.get("fingerprint")
    if stored and stored.get("platform"):
        return stored["platform"]

    transcript = session.get("transcript")
    prompt = transcript.last_prompt if transcript else ""
    return identify(banner="", prompt=prompt).platform


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------


def _read_until_idle(channel, timeout: float = CAPTURE_TIMEOUT) -> str:
    """
    Read from a channel until it goes quiet or the timeout expires.

    There is no reliable end marker: the trailing prompt varies by platform
    and mode, and matching on one risks stopping early on a configuration that
    happens to contain something prompt-shaped. Waiting for the device to stop
    talking is cruder but does not truncate.
    """
    import socket

    chunks: list[str] = []
    deadline = time.time() + timeout
    last_data = time.time()

    while time.time() < deadline:
        try:
            data = channel.recv(65536)
            if not data:
                break
            chunks.append(data.decode("utf-8", errors="replace"))
            last_data = time.time()
        except socket.timeout:
            if time.time() - last_data > IDLE_SETTLE and chunks:
                break
        except Exception:
            break

    return "".join(chunks)


def capture_config(session: dict) -> dict:
    """
    Capture the running configuration over a secondary channel.

    Args:
        session: The session dict from SessionManager.

    Returns:
        The result of storing it, including whether it differed from the
        previous snapshot.

    Raises:
        ConnectionError_: With a message explaining why capture is not
            possible on this session.
    """
    handler = session.get("handler")
    if not isinstance(handler, SSHHandler):
        raise ConnectionError_(
            f"Configuration capture needs SSH. This tab is "
            f"{session.get('connection_type', 'unknown')}, which cannot run a "
            f"command without disturbing your session."
        )
    if not handler.is_connected:
        raise ConnectionError_("The session is no longer connected.")

    channel = handler.open_secondary_channel()
    if channel is None:
        raise ConnectionError_(
            "This device refused a second channel, which many switches do when "
            "they limit concurrent sessions. Capture is skipped rather than "
            "typing into your live session."
        )

    platform = session_platform(session)
    profile = get_profile(platform)
    commands = {
        "paging_off": profile.paging_off or "terminal length 0",
        "show_run":   profile.show_run or "show running-config",
    }

    try:
        # Clear the login banner before asking anything.
        _read_until_idle(channel, timeout=3.0)

        channel.send((commands["paging_off"] + "\n").encode())
        _read_until_idle(channel, timeout=5.0)

        channel.send((commands["show_run"] + "\n").encode())
        raw = _read_until_idle(channel)
    finally:
        try:
            channel.close()
        except Exception:
            pass

    config = _tidy_config(raw, commands["show_run"])
    if not config.strip():
        raise ConnectionError_(
            f"The device returned nothing for '{commands['show_run']}'. It may "
            f"use a different command, or the account may lack privilege."
        )

    hostname = session.get("hostname") or ""
    result = store.add_snapshot(hostname, config, session.get("session_id", ""))
    result["platform"] = platform
    result["line_count"] = config.count("\n") + 1

    # A copy as a file, where the user asked for it. Redacted, capped, and
    # never allowed to turn a successful capture into a failed one — the
    # snapshot above is already stored and the drift check depends on it.
    try:
        result["archive"] = config_archive.archive(
            hostname, config, changed=bool(result.get("stored")))
    except Exception as exc:                                # pragma: no cover
        logger.warning("Could not archive the capture from %s: %s", hostname, exc)
        result["archive"] = {"written": False, "reason": str(exc)}

    logger.info(
        "Captured %s lines of config from %s (%s)%s",
        result["line_count"], hostname, "changed" if result["stored"] else "unchanged",
        f"; saved to {result['archive']['path']}" if result["archive"].get("written") else "",
    )
    return result


def _tidy_config(raw: str, command: str) -> str:
    """
    Reduce captured output to just the configuration.

    Strips the echoed command, pager artefacts and the trailing prompt, so two
    snapshots of an unchanged device hash identically instead of differing by
    a prompt.
    """
    text = strip_pager_prompts(clean(raw))
    lines = text.splitlines()

    # Drop everything up to and including the echo of the command itself.
    for index, line in enumerate(lines):
        if command in line:
            lines = lines[index + 1:]
            break

    # Drop a trailing prompt left by the device waiting for more input.
    from backend.session.transcript import match_prompt
    while lines and (not lines[-1].strip() or match_prompt(lines[-1])):
        lines.pop()

    return "\n".join(lines).strip("\n")


# ---------------------------------------------------------------------------
# Drift
# ---------------------------------------------------------------------------


def diff_snapshots(old: dict, new: dict) -> dict:
    """
    Return a unified diff between two snapshots.

    Args:
        old: The earlier snapshot row.
        new: The later snapshot row.

    Returns:
        The diff text plus added/removed counts.
    """
    old_lines = (old.get("content") or "").splitlines()
    new_lines = (new.get("content") or "").splitlines()

    diff = list(difflib.unified_diff(
        old_lines, new_lines,
        fromfile=f"{old.get('hostname', '')} @ {_when(old.get('captured_at'))}",
        tofile=f"{new.get('hostname', '')} @ {_when(new.get('captured_at'))}",
        lineterm="",
    ))

    # Count only real changes: the +++/--- header lines start with the same
    # characters and would otherwise inflate the totals by one each.
    added = sum(1 for line in diff if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in diff if line.startswith("-") and not line.startswith("---"))

    return {
        "diff":    "\n".join(diff),
        "added":   added,
        "removed": removed,
        "changed": added + removed,
        "old_id":  old.get("id"),
        "new_id":  new.get("id"),
    }


def drift_report(session: dict) -> dict:
    """
    Compare the device's configuration now against the last visit.

    Returns a structure the UI can show as a one-line summary. Never raises:
    a device that will not give up its configuration should produce an
    explanation, not an error banner on an otherwise fine session.
    """
    hostname = session.get("hostname") or ""

    if not config_archive.capture_enabled():
        return {
            "available": False, "hostname": hostname,
            "reason": "Configuration capture is switched off under "
                      "Settings → Session Logging.",
        }

    previous = store.latest_snapshot(hostname)

    try:
        result = capture_config(session)
    except ConnectionError_ as exc:
        return {"available": False, "reason": str(exc), "hostname": hostname}

    # What was saved as a file, if anything — the confirmation the user gets
    # for a capture that is otherwise entirely invisible to them.
    archive = result.get("archive") or {}

    if previous is None:
        return {
            "available": True, "first_visit": True, "hostname": hostname,
            "archive": archive,
            "summary": f"First configuration snapshot of {hostname} stored "
                       f"({result['line_count']:,} lines).",
        }

    if result.get("unchanged"):
        return {
            "available": True, "first_visit": False, "changed": 0,
            "hostname": hostname,
            "archive": archive,
            "days_since": _days_since(previous.get("captured_at")),
            "summary": _unchanged_summary(hostname, previous.get("captured_at")),
        }

    current = store.latest_snapshot(hostname)
    comparison = diff_snapshots(previous, current or {})

    return {
        "available":   True,
        "first_visit": False,
        "hostname":    hostname,
        "archive":     archive,
        "days_since":  _days_since(previous.get("captured_at")),
        "changed":     comparison["changed"],
        "added":       comparison["added"],
        "removed":     comparison["removed"],
        "diff":        comparison["diff"],
        "summary":     _changed_summary(
            previous.get("captured_at"), comparison["changed"],
        ),
    }


def _days_since(timestamp: float | None) -> int | None:
    if not timestamp:
        return None
    return max(0, int((time.time() - timestamp) / 86400))


def _when(timestamp: float | None) -> str:
    if not timestamp:
        return "unknown"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(timestamp))


def _ago(timestamp: float | None) -> str:
    """Phrase an interval the way someone would say it out loud."""
    days = _days_since(timestamp)
    if days is None:
        return "previously"
    if days == 0:
        return "earlier today"
    if days == 1:
        return "yesterday"
    return f"{days} days ago"


def _unchanged_summary(hostname: str, timestamp: float | None) -> str:
    return f"You were last on {hostname} {_ago(timestamp)}. Nothing has changed since."


def _changed_summary(timestamp: float | None, changed: int) -> str:
    lines = "line has" if changed == 1 else "lines have"
    return f"You were last here {_ago(timestamp)}, and {changed} {lines} changed since."
