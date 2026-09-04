"""
explain.py — The prompts ShellMate composes itself, from device data it holds.

An ordinary chat message is text the engineer typed. These are not: they carry
a device's configuration — a drift diff, a classified set of push lines — and
that is the whole reason they are built here rather than in the browser.

`session/outbound.py` is the one door out, and it can only mask what it is
shown. A prompt assembled in JavaScript and posted as `message` arrives at the
provider with the configuration already inside it; nothing server-side ever
sees it as device output. `_auto_analysis_prompt` in `app.py` learned that the
hard way and this module is the same lesson applied to the two places that send
*configuration* rather than terminal output:

- **A diff** (#549) — the drift report from this visit, or any two stored
  snapshots — with the question the engineer actually has: what do these
  changes do, and could they cause what I am seeing?
- **A proposed change** (#550) — the preview's own classification of every
  line, plus the stanzas of the running configuration those lines land in,
  reviewed before anything is applied.

Two rules hold for everything here:

**Nothing touches the device.** The diff comes from the archive; the review
re-runs `config_push.preview()` against the *stored* capture, never a fresh
one. A review that reached out to the switch would be a second, unannounced
conversation with it at exactly the moment the engineer is deciding whether to
have the first.

**Everything is capped.** A running configuration is thousands of lines, and
an uncapped prompt is a bill. What does not fit is announced — "N more lines
not shown" — rather than quietly truncated, because a model that is not told
it is reading a fragment will answer as though it read the whole thing.
"""

import logging

from backend.advanced import get as advanced
from backend.session.outbound import redact_text

logger = logging.getLogger(__name__)

#: The question a drift diff exists to answer.
DIFF_QUESTION = (
    "What do these configuration changes do, and could they cause what I am "
    "seeing on this device? Call out anything risky or unintended, and say so "
    "plainly if the change looks routine."
)


def _cap(text: str, limit: int, what: str = "diff") -> str:
    """Trim to *limit* lines, saying how much was left out."""
    lines = (text or "").splitlines()
    if limit <= 0:
        return f"[{len(lines)} lines of {what} not sent — the limit is set to zero]"
    if len(lines) <= limit:
        return "\n".join(lines)
    return "\n".join(lines[:limit]) + f"\n[{len(lines) - limit} more {what} lines not shown]"


def drift_facts(session: dict) -> dict | None:
    """
    The drift report this session's connect-time check produced, if any.

    Cached on the session by ``/api/sessions/{id}/drift`` so the assistant can
    read it without re-capturing — the capture already happened, on connect,
    and running it again to answer a question would be a second command sent
    to the device for nothing.
    """
    report = session.get("drift")
    if not isinstance(report, dict) or not report.get("available"):
        return None
    if not report.get("diff"):
        return None
    return {
        "changed":    int(report.get("changed") or 0),
        "added":      int(report.get("added") or 0),
        "removed":    int(report.get("removed") or 0),
        "days_since": report.get("days_since"),
        "diff":       _cap(redact_text(report.get("diff") or ""),
                           int(advanced("ai.drift_lines"))),
    }


def _diff_between(old_id, new_id) -> tuple[str, str]:
    """A diff between two stored snapshots, and a phrase naming them."""
    from backend.configs import diff_snapshots
    from backend.store import store

    old = store.get_snapshot(int(old_id))
    new = store.get_snapshot(int(new_id))
    if not old or not new:
        return "", ""
    comparison = diff_snapshots(old, new)
    header = (f"two stored captures of {new.get('hostname') or 'this device'}"
              f" — {comparison.get('added', 0)} lines added, "
              f"{comparison.get('removed', 0)} removed")
    return comparison.get("diff") or "", header


def diff_prompt(request: dict, session: dict | None) -> str:
    """
    Compose the "explain these changes" message for the chat socket.

    ``request`` is ``{"old_id": n, "new_id": n}`` for any two captures, or
    empty for this session's drift report. Returns "" when there is nothing
    to explain, which the caller treats as a message not worth sending.
    """
    old_id, new_id = request.get("old_id"), request.get("new_id")
    header = ""
    diff = ""

    if old_id and new_id:
        try:
            diff, header = _diff_between(old_id, new_id)
        except Exception as exc:                 # a missing snapshot is not a fault
            logger.debug("Could not diff %s..%s: %s", old_id, new_id, exc)
            return ""
        diff = _cap(redact_text(diff), int(advanced("ai.drift_lines")))
    elif session is not None:
        facts = drift_facts(session)
        if facts:
            diff = facts["diff"]
            days = facts.get("days_since")
            header = (
                f"the configuration of {session.get('hostname') or 'this device'} "
                f"since it was last visited"
                + (f", {days} day{'' if days == 1 else 's'} ago" if days else ""))

    if not diff.strip():
        return ""

    return (
        f"These are the changes to {header or 'this device'}, as a unified "
        f"diff:\n```diff\n{diff}\n```\n{DIFF_QUESTION}"
    )


__all__ = ["diff_prompt", "drift_facts", "DIFF_QUESTION"]
