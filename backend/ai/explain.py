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


# ---------------------------------------------------------------------------
# Reviewing a proposed change before it is applied (#550)
# ---------------------------------------------------------------------------

#: What a review is for. Fixed here rather than typed by the engineer: the
#: value of a second pair of eyes at this moment is that it looks at the same
#: five things every time, including on the change that seems obvious.
REVIEW_QUESTION = """\
Review this proposed configuration change before it is applied. Cover, in
this order and briefly:

1. **Intended effect** — what this change does, in one or two sentences.
2. **Ordering** — anything that must come before something else, or that will
   fail as written because the thing it refers to does not exist yet.
3. **Omissions** — a missing `no shutdown`, a missing `commit` or `write`, an
   interface left without an address, an ACL applied but never defined.
4. **Blast radius** — what else on this device is affected, and whether any of
   it could drop the session you are typing into.
5. **The way back** — the lines that would undo this, as a block.

Say plainly if it looks routine. Do not suggest running anything: nothing has
been applied, and this is a review, not a next step."""


def _stanzas(config_text: str, wanted: set[str]) -> list[str]:
    """
    The sections of the running configuration a change lands in.

    A stanza is an unindented line and the indented lines under it — how every
    platform ShellMate knows prints an interface, an ACL or a routing process.
    Sending the whole configuration would answer the question and cost a
    fortune; sending only the changed lines answers a different question,
    because "add `ip address …` under `interface Gi0/2`" reads very
    differently when Gi0/2 already has one.
    """
    out: list[str] = []
    current: list[str] | None = None
    header = ""
    for raw in (config_text or "").splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        if not line.startswith((" ", "\t")):
            if current and header.strip().lower() in wanted:
                out.extend(current)
            header = line
            current = [line]
        elif current is not None:
            current.append(line)
    if current and header.strip().lower() in wanted:
        out.extend(current)
    return out


def _targets(rows: list[dict]) -> set[str]:
    """The unindented lines a change touches, as stanza headers to look for."""
    wanted: set[str] = set()
    for row in rows:
        text = str(row.get("text", ""))
        if text.startswith((" ", "\t")):
            continue
        stripped = text.strip().lower()
        for prefix in ("no ", "delete ", "undo "):
            if stripped.startswith(prefix):
                stripped = stripped[len(prefix):].strip()
                break
        if stripped:
            wanted.add(stripped)
    return wanted


def push_review_prompt(session: dict, text: str) -> str:
    """
    Compose the "review this before I apply it" message (#550).

    Everything here is already computed: ``config_push.preview()`` classes
    every line as an addition, a line already present or a removal, names the
    capture it compared against and lists what the guardrail would hold. The
    only thing missing was somebody to read it.

    Deliberately re-runs the preview with ``fresh=False``. The dialog the
    engineer is looking at may have compared against a fresh capture, but a
    review must not open a second conversation with the device at the moment
    they are deciding whether to have the first — so this reads the stored
    capture, and says which one it read.
    """
    from backend import config_push
    from backend.store import store

    report = config_push.preview(session, text, fresh=False)
    rows = report.get("lines") or []

    marks = {"add": "+", "remove": "-", "present": "="}
    classified = "\n".join(
        f"{marks.get(row.get('status'), '?')} {row.get('text', '')}" for row in rows)

    hostname = session.get("hostname") or ""
    snapshot = store.latest_snapshot(hostname) if hostname else None
    context = _stanzas((snapshot or {}).get("content") or "", _targets(rows))
    context_text = _cap(redact_text("\n".join(context)),
                        int(advanced("ai.review_context_lines")), "configuration")

    commands = report.get("commands") or {}
    dangerous = report.get("dangerous") or []

    parts = [
        f"I am about to apply this change to {hostname or 'a device'} "
        f"({report.get('platform', 'unknown platform')}). Nothing has been sent "
        f"to it — this is the dry run.",
        "",
        f"ShellMate's preview says: {report.get('summary', '')}",
        f"The lines would be wrapped in `{commands.get('enter', '')}` … "
        f"`{commands.get('exit', '')}`.",
        "",
        "The proposed change, with ShellMate's own classification "
        "(+ new, = already present, - a removal whose target exists):",
        "```",
        redact_text(classified),
        "```",
    ]
    if dangerous:
        parts += ["",
                  "The guardrail would hold these lines, and they need "
                  "confirming before they are sent:",
                  "```", redact_text("\n".join(dangerous)), "```"]
    if context_text.strip():
        parts += ["",
                  "The parts of the running configuration these lines land in, "
                  "from ShellMate's latest capture:",
                  "```", context_text, "```"]
    elif hostname and not snapshot:
        parts += ["",
                  "ShellMate holds no capture of this device, so every line "
                  "above reads as new and there is no current configuration to "
                  "compare against. Say so if it matters to your answer."]
    parts += ["", REVIEW_QUESTION]
    return "\n".join(parts)


__all__ = ["diff_prompt", "drift_facts", "push_review_prompt",
           "DIFF_QUESTION", "REVIEW_QUESTION"]
