"""
config_push.py — Apply configuration with a preview first, and a way back (#407).

`configs.py` captures and diffs; nothing wrote back. This closes the loop
the archive was built for:

**Preview.** The proposed lines are compared with the device's current
running configuration — the latest capture, or a fresh one — and each is
marked: new, already present, or a removal (a ``no …`` / ``delete …`` line
whose target exists). Nothing is sent. The engineer sees exactly what would
change before deciding.

**Apply.** The lines go into the live session, through the same channel the
engineer is typing on, wrapped in the platform's enter/exit config commands,
paced, and echoed on screen like anything typed — never silently. The
guardrail's dangerous-command list is checked first, and a hit refuses the
whole push unless it is explicitly overridden. Afterwards the configuration
is captured again and stored, so the change is a diff in the archive.

**Restore.** A previous capture can be turned back into a proposed change:
what the current configuration has that the capture lacked becomes a
removal, what it lacked becomes an addition. It is a best-effort inverse —
platforms differ in what a bare ``no`` undoes — so it arrives as a preview
for the engineer to read and edit, never as an automatic revert. The
honest name for it is "propose the way back", and the interface says so.
"""

import logging
import re
import time

from backend.advanced import get as advanced
from backend.connections.base import ConnectionError_
from backend.connections.ssh_handler import SSHHandler
from backend.platforms import BUILTIN, get_profile, matches_dangerous
from backend.store import store

logger = logging.getLogger(__name__)

#: Words that begin a removal on the platforms ShellMate knows.
_REMOVAL_PREFIXES = ("no ", "delete ", "undo ")


def _commands(platform: str) -> dict:
    """
    Enter, exit and save commands for a platform.

    A platforms.json written by an earlier version lacks the new fields, so
    a blank falls back to the built-in profile's value rather than refusing
    a push on a device the built-ins know perfectly well.
    """
    profile = get_profile(platform)
    builtin = BUILTIN.get(platform)
    def pick(name: str) -> str:
        value = getattr(profile, name, "") or ""
        if not value and builtin is not None:
            value = getattr(builtin, name, "") or ""
        return value
    return {"enter": pick("config_enter"), "exit": pick("config_exit"),
            "save": pick("save_command")}


def _clean_lines(text: str) -> list[str]:
    lines = []
    for raw in (text or "").splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        lines.append(line)
    return lines


def _current_config(session: dict, fresh: bool) -> tuple[str, dict | None]:
    """The running config to compare against: a fresh capture, or the latest stored."""
    from backend import configs
    hostname = session.get("hostname") or ""
    if fresh:
        result = configs.capture_config(session)
        snapshot_id = (result.get("snapshot") or {}).get("id")
        snapshot = store.get_snapshot(snapshot_id) if snapshot_id else None
        if snapshot:
            return snapshot.get("content") or "", snapshot
    latest = store.latest_snapshot(hostname) if hostname else None
    if latest:
        return latest.get("content") or "", latest
    return "", None


def check(snapshot_text: str, lines, platform: str = "") -> dict:
    """
    Classify lines against a stored configuration, sending nothing.

    Factored out of ``preview`` for the compliance check (#543), which asks
    the same question of two hundred devices from their stored snapshots
    rather than of one device from its live session. The classification is
    the whole of both features; only where the configuration comes from
    differs, and two copies of it would drift.

    Args:
        snapshot_text: The stored configuration to compare against.
        lines:         The block, as text or an already-split list.
        platform:      Reported back, for a caller assembling a table.

    Returns:
        ``{"platform", "lines": [{"text", "status"}], "counts"}`` where
        status is ``add`` (not in the configuration), ``present`` (there,
        verbatim) or ``remove`` (a ``no …`` whose target is there).

    **The limit, stated because it cannot be seen from the result.** Lines
    are matched as a *set*, stripped of indentation, so section context is
    ignored: ``description uplink`` under the wrong interface counts as
    present. That is right for the common case — a block of AAA or logging
    or NTP lines is flat — and wrong for anything whose meaning depends on
    the parent line above it. A check that overstates what it verified is
    worse than no check, so every caller has to say so on screen. An
    anchored mode is the follow-on named on the issue.

    **"Unexpected lines" needs no extra parameter.** A must-not-have list
    is the same call: anything that comes back ``present`` is a line that
    should not be there. Adding a second mode would be a second thing to
    keep correct for no gain.
    """
    current = {line.strip() for line in (snapshot_text or "").splitlines()
               if line.strip()}
    wanted = _clean_lines(lines if isinstance(lines, str) else "\n".join(lines))

    rows: list[dict] = []
    counts = {"add": 0, "present": 0, "remove": 0}
    for line in wanted:
        stripped = line.strip()
        lowered = stripped.lower()
        status = "add"
        if stripped in current:
            status = "present"
        elif any(lowered.startswith(p) for p in _REMOVAL_PREFIXES):
            target = stripped.split(" ", 1)[1].strip() if " " in stripped else ""
            status = "remove" if target and target in current else "add"
        counts[status] += 1
        rows.append({"text": line, "status": status})

    return {"platform": platform, "lines": rows, "counts": counts}


def preview(session: dict, text: str, fresh: bool = False) -> dict:
    """
    What applying ``text`` would change, without sending anything.

    Each line is classed ``add`` (not in the running config), ``present``
    (already there, verbatim) or ``remove`` (a ``no …`` whose target is
    there). Indented context lines — ``interface Gi0/1`` followed by
    `` description …`` — are compared stripped, which is right for the
    common case and says so in the summary.
    """
    from backend.configs import session_platform
    platform = session_platform(session)
    commands = _commands(platform)
    if not commands["enter"]:
        raise ConnectionError_(
            f"ShellMate has no configuration commands for platform '{platform}'. "
            f"Add config_enter / config_exit under Platform Definitions, or "
            f"push the lines by hand.")

    current_text, snapshot = _current_config(session, fresh)
    lines = _clean_lines(text)
    if not lines:
        raise ConnectionError_("There is nothing to apply.")

    # The classification itself lives in check(), which the compliance
    # report (#543) asks the same question with — from a stored snapshot
    # rather than a live session. Two copies would drift.
    classified = check(current_text, lines, platform)
    rows = classified["lines"]
    counts = classified["counts"]

    dangerous = _dangerous(session, lines)
    return {
        "platform":   platform,
        "commands":   commands,
        "lines":      rows,
        "counts":     counts,
        "dangerous":  dangerous,
        "compared_to": {
            "snapshot_id": snapshot.get("id") if snapshot else None,
            "captured_at": snapshot.get("captured_at") if snapshot else None,
            "fresh": fresh,
        } if snapshot else None,
        "summary": _summary(counts, snapshot, fresh),
    }


def _summary(counts: dict, snapshot, fresh: bool) -> str:
    bits = []
    if counts["add"]:
        bits.append(f"{counts['add']} new line{'s' if counts['add'] != 1 else ''}")
    if counts["remove"]:
        bits.append(f"{counts['remove']} removal{'s' if counts['remove'] != 1 else ''}")
    if counts["present"]:
        bits.append(f"{counts['present']} already in place")
    against = ("the configuration captured just now" if fresh else
               "the latest stored capture" if snapshot else
               "nothing — no capture of this device exists, so every line reads as new")
    return (", ".join(bits) or "nothing to change") + f", compared with {against}."


def _dangerous(session: dict, lines: list[str]) -> list[str]:
    """Lines the guardrail would hold, so the push can refuse them up front."""
    from backend.configs import session_platform
    try:
        platform = session_platform(session)
    except Exception:
        return []
    hits = []
    for line in lines:
        try:
            if matches_dangerous(platform, line.strip()):
                hits.append(line)
        except Exception:
            continue
    return hits


def apply(session: dict, text: str, save: bool = False, force: bool = False) -> dict:
    """
    Send the lines into the live session, then capture and diff.

    Paced by ``configs.push_line_delay_ms`` so a device's input buffer is not
    outrun. Every line is echoed by the device on the engineer's own screen.
    """
    handler = session.get("handler")
    if not isinstance(handler, SSHHandler) or not handler.is_connected:
        raise ConnectionError_("Applying configuration needs a connected SSH session.")
    from backend.configs import capture_config, diff_snapshots, session_platform

    platform = session_platform(session)
    commands = _commands(platform)
    if not commands["enter"]:
        raise ConnectionError_(f"No configuration commands are known for '{platform}'.")
    lines = _clean_lines(text)
    if not lines:
        raise ConnectionError_("There is nothing to apply.")
    hits = _dangerous(session, lines)
    if hits and not force:
        raise ConnectionError_(
            "Refused: the change contains a command the guardrail holds — "
            + "; ".join(hits[:3]) + (" …" if len(hits) > 3 else "")
            + ". Confirm the push to send it anyway.")

    # The state before, so the diff afterwards is against *now*, not against
    # whenever the last capture happened to be.
    before = capture_config(session)
    before_id = (before.get("snapshot") or {}).get("id")

    delay = max(0.0, float(advanced("capture.push_line_delay_ms")) / 1000.0)
    sequence = [commands["enter"], *lines, commands["exit"]]
    if save and commands["save"]:
        sequence.append(commands["save"])
    for line in sequence:
        handler.send((line + "\n").encode("utf-8", errors="replace"))
        time.sleep(delay)
    # Let the device finish echoing before the capture opens its channel.
    time.sleep(max(1.0, delay * 4))

    after = capture_config(session)
    after_id = (after.get("snapshot") or {}).get("id")
    diff = {}
    if before_id and after_id:
        old = store.get_snapshot(before_id)
        new = store.get_snapshot(after_id)
        if old and new:
            diff = diff_snapshots(old, new)
    logger.info("Applied %d lines to %s (%s); %s",
                len(lines), session.get("hostname"), platform,
                f"{diff.get('changed', 0)} lines differ" if diff else "no diff available")
    return {
        "sent":      sequence,
        "saved":     bool(save and commands["save"]),
        "before_id": before_id,
        "after_id":  after_id,
        "diff":      diff,
        "changed":   diff.get("changed", 0),
    }


def restore_proposal(session: dict, snapshot_id: int) -> dict:
    """
    A proposed change that would take the device back to ``snapshot_id``.

    Lines in the current configuration that the snapshot lacks become
    ``no <line>``; lines the snapshot has that the current lacks are added
    as they were. Section headers (unindented lines that own indented ones)
    are kept for context. This is a best-effort inverse for the engineer to
    read and edit — it is not applied here.
    """
    target = store.get_snapshot(int(snapshot_id))
    if not target:
        raise ConnectionError_("That capture no longer exists.")
    current_text, current_snapshot = _current_config(session, fresh=True)
    wanted = [l.rstrip() for l in (target.get("content") or "").splitlines() if l.strip()]
    have = [l.rstrip() for l in current_text.splitlines() if l.strip()]
    wanted_set = {l.strip() for l in wanted}
    have_set = {l.strip() for l in have}

    proposal: list[str] = []
    header = None
    for line in have:
        if not line.startswith((" ", "\t")):
            header = line
        if line.strip() in wanted_set:
            continue
        if line.strip().startswith(("!", "#")):
            continue
        if line.startswith((" ", "\t")) and header and header not in proposal:
            proposal.append(header)
        proposal.append((" " if line.startswith((" ", "\t")) else "") + "no " + line.strip())
    header = None
    for line in wanted:
        if not line.startswith((" ", "\t")):
            header = line
        if line.strip() in have_set:
            continue
        if line.strip().startswith(("!", "#")):
            continue
        if line.startswith((" ", "\t")) and header and header not in proposal:
            proposal.append(header)
        proposal.append(line)

    text = "\n".join(proposal)
    return {
        "target_id":   target.get("id"),
        "captured_at": target.get("captured_at"),
        "current_id":  current_snapshot.get("id") if current_snapshot else None,
        "text":        text,
        "line_count":  len(proposal),
        "note": ("A best-effort inverse: every line here should be read before it is "
                 "applied. Platforms differ in what a bare 'no' undoes, and a "
                 "removed section may need its own form."),
    }


__all__ = ["preview", "apply", "restore_proposal"]
_ = re  # kept for future pattern work; silences an unused-import lint
