"""
broadcast_collect.py — The replies a broadcast produced, and how they differ (#529).

`POST /api/broadcast` sends and then says "watch the tabs". For forty
switches that is not an answer, it is forty answers the operator has to hold
in their head — which is precisely the job people write Netmiko scripts for:
`show version | i Version` across an access layer before an upgrade, `show ip
bgp summary` across the edge after a maintenance. The interesting question is
almost never "did it run" but "which of these six is different".

Nothing new has to be parsed to answer that. Every session already segments
its stream into :class:`CommandRecord`s on prompt detection and keeps the last
twelve on ``session["recent_records"]``, and
``GET /api/sessions/{id}/last-output`` already waits for the record that
closed after a given moment. This module is that endpoint's multi-session
generalisation, plus the diff.

Four constraints shape it, and each one is a bug that was easy to write:

**Bounded, per device.** One switch that never returns to a prompt must not
hold the other thirty-nine open. The wait is therefore resolved per session
and every result carries how long *it* was waited for — a single global
"timed out" tells the operator nothing about which device to go and look at.

**Three ways of not having an answer, never merged.** ``timeout`` means the
device is still talking; ``not-captured`` means it came back to a prompt but
ShellMate could not tie a record to this command; ``gone`` means the session
is not there to answer. Those send somebody to three different places — wait
longer, check prompt detection for that platform, reconnect the tab — and a
combined "failed" sends them to none of them.

**``after`` is not optional.** Running `show version` twice would otherwise
match the previous run's record and report output from before the broadcast
was even sent. Honoured exactly as last-output honours it, one second of
slack included.

**Redacted on the way out.** A collected set goes into a comparison, into a
chat message, and quite possibly into a ticket that colleagues can search.
`show run` carries hashes, keys and community strings, so output leaves here
through :func:`backend.session.outbound.redact_text` and nowhere else.

And one rule over all of them: nothing in here raises. A broadcast to forty
devices that loses thirty-nine good answers to one malformed session dict is
worse than no collection at all.
"""

import asyncio
import difflib
import logging
import time

from backend.session import outbound

logger = logging.getLogger(__name__)

#: The states a device's reply can be in. Deliberately four rather than
#: "ok / failed" — see the module docstring.
STATES = ("collected", "timeout", "not-captured", "gone")

#: Slack allowed on the ``after`` guard, in seconds. ``started_at`` is stamped
#: when the parser *sees* the echoed command line, which is not the same
#: instant the caller decided to broadcast, and the two clocks are only as
#: close as the send took. The same second last-output allows, for the same
#: reason: a stricter guard rejects this run's own answer.
AFTER_SLACK = 1.0

#: Below this, a command is too short to be matched by abbreviation at all:
#: "s" prefixes every show command on the device, and matching it would
#: attribute the first thing that closed to whatever was asked for.
MIN_PREFIX = 3

#: Lines of unified diff carried per differing device. The diff is read in a
#: chat message and a results list, not in a code review, so a device whose
#: whole configuration changed is summarised rather than transcribed. The
#: added/removed counts are taken before this cut, so they stay true.
MAX_DIFF_LINES = 400

#: Devices named in the one-line summary before it stops listing them. Forty
#: hostnames in a sentence is not a sentence.
MAX_NAMED = 6


# ---------------------------------------------------------------------------
# Collecting
# ---------------------------------------------------------------------------


def _label(session_id: str, session: dict | None) -> str:
    """
    What to call this device in a result row.

    The same fallback chain `app.run_one` uses, so the collected list and the
    sent list name the same device the same way. Two spellings of one switch
    across two halves of one screen is a bug report waiting to happen.
    """
    if isinstance(session, dict):
        label = session.get("display_label") or session.get("hostname")
        if label:
            return str(label)
    return (session_id or "")[:8]


def _normalise(command: str) -> str:
    """Lower-cased, with runs of whitespace collapsed, for comparison only."""
    return " ".join(str(command or "").split()).lower()


def _matches(wanted: str, actual: str) -> bool:
    """
    Whether *actual* — a record's command — is the reply to *wanted*.

    Not equality, and emphatically not a substring test.

    Equality alone fails on the ordinary case. Devices echo their own
    completion of an abbreviation, so `sh ver` is sent and `show version` is
    what the parser records. Cisco-style abbreviation is per word, which is
    why this compares word by word rather than as one string: `show version`
    does not start with `sh ver`.

    A plain substring test is what equality usually gets loosened into, and it
    is wrong in the way that matters — it produces a confident wrong answer.
    `show version` is a substring of `show version | include Version` and of
    `do show version`, so the collector would file one command's output under
    another's name and the diff would then be comparing two different
    commands on two different devices. Requiring the same number of words
    rules that out: an extra `| include` is an extra word.

    The cost of being conservative is a missed match, which surfaces as
    ``not-captured`` — an honest "ShellMate could not tie a record to this",
    not a wrong answer. That is the right way round.

    Note for callers: pass the command **as it went out**. `app.run_one`
    already reports the pipeline's expansion rather than the alias, and an
    alias whose expansion is a different number of words will not match the
    alias itself.

    An empty *wanted* matches anything, which is how last-output behaves and
    is what makes "whatever closed most recently" expressible.
    """
    if not wanted:
        return True
    if not actual:
        return False
    if wanted == actual:
        return True
    if len(wanted) < MIN_PREFIX or len(actual) < MIN_PREFIX:
        return False

    left, right = wanted.split(), actual.split()
    if len(left) != len(right):
        return False
    return all(a == b or a.startswith(b) or b.startswith(a)
               for a, b in zip(left, right))


def _find_record(session: dict, wanted: str, after: float) -> tuple[object | None, bool]:
    """
    The record answering *wanted*, and whether anything closed since *after*.

    Returns ``(record, saw_new)``. The second value is what separates
    ``not-captured`` from ``timeout`` at the deadline: a device that has
    closed a record since the broadcast went out *is* back at a prompt and
    being parsed, so failing to match one is a statement about this command's
    text — an unrecognised echo, a platform whose prompt shape the parser
    only half-follows — not about the device still being busy. A device that
    has closed nothing at all is simply still talking, and telling somebody to
    go and look at prompt detection for it would be sending them to the wrong
    place.

    The newest match wins, as in last-output: within one collection there is
    only one candidate anyway, and where the slack window admits two, the
    later one is the one this broadcast caused.
    """
    records = list(session.get("recent_records") or [])
    saw_new = False
    found = None

    for record in reversed(records):
        ran_at = float(getattr(record, "started_at", 0) or 0)
        if after and ran_at < after - AFTER_SLACK:
            # A previous run of the same command, from before this broadcast.
            # Everything below it in the deque is older still.
            break
        saw_new = True
        if found is None and _matches(wanted, _normalise(getattr(record, "command", ""))):
            found = record

    return found, saw_new


def _connected(session: dict) -> bool:
    """
    Whether the session's transport still believes it is up.

    Best effort and deliberately optimistic: a handler that cannot be asked
    is treated as connected, so an unexpected shape costs a wait rather than
    a wrongly reported ``gone``.
    """
    handler = session.get("handler")
    if handler is None:
        return True
    try:
        return bool(handler.is_connected)
    except Exception:                                   # pragma: no cover
        return True


def _resolve(session_id: str, sessions: dict, wanted: str,
             after: float) -> tuple[dict | None, bool]:
    """
    One poll of one session.

    Returns ``(settled, saw_new)`` — a finished result dict once there is one
    and None while the device is still worth waiting for, plus whether
    anything closed on it since *after*. The second value is not a decision;
    it is what the deadline needs later to tell ``not-captured`` from
    ``timeout``, and it is deliberately *not* acted on here. A device that has
    just answered a different command may still be about to answer this one.

    The order of the checks is the whole of the logic. The record is looked
    for *first*, so a device that answered and then dropped — which is exactly
    what `reload` looks like — is reported with its answer rather than as a
    lost session.
    """
    session = sessions.get(session_id)
    if not isinstance(session, dict):
        # The tab was closed, or the manager never had it. Nothing will
        # arrive; do not spend the timeout finding that out.
        return {"state": "gone", "output": "", "ran_at": 0.0,
                "detail": "The session is no longer open."}, False

    try:
        record, saw_new = _find_record(session, wanted, after)
        if record is not None:
            return {
                "state": "collected",
                "command": str(getattr(record, "command", "") or "").strip(),
                # The one door out (`session/outbound.py`). These results are
                # bound for a diff, a chat message and possibly a ticket.
                "output": outbound.redact_text(
                    str(getattr(record, "output", "") or "")),
                "ran_at": float(getattr(record, "started_at", 0) or 0),
                "detail": "",
            }, saw_new
        if not _connected(session):
            return {"state": "gone", "output": "", "ran_at": 0.0,
                    "detail": "The session dropped before the reply arrived."
                    }, saw_new
        return None, saw_new
    except Exception as exc:
        # Rule five: one malformed session must not cost the other thirty-nine.
        # A session that cannot be read is not one that can be waited on, so
        # it settles now, with the reason attached rather than swallowed.
        logger.warning("Could not read session %s while collecting: %s",
                       session_id, exc)
        return {"state": "gone", "output": "", "ran_at": 0.0,
                "detail": f"ShellMate could not read this session ({exc})."}, False


async def collect(sessions: dict[str, dict], command: str, after: float,
                  timeout: float = 45.0, poll: float = 0.5) -> dict:
    """
    Wait, bounded, for each session's reply to *command*.

    Args:
        sessions: ``{session_id: session dict}`` — the dicts SessionManager
            holds, carrying ``recent_records``, ``hostname`` and
            ``display_label``. Read live on every poll, so records that close
            during the wait are picked up.
        command:  The command as sent. Matched loosely enough to survive
            alias expansion and abbreviation; see :func:`_matches`.
        after:    The moment the broadcast went out. Records older than this
            belong to a previous run and are not this command's answer.
        timeout:  The ceiling for the whole collection, in seconds.
        poll:     How often the records are re-read.

    Returns:
        ``{"command", "results": [...], "waited_s"}`` where each result is
        ``{"session_id", "label", "state", "output", "ran_at", "waited_s",
        "detail"}`` and ``state`` is one of :data:`STATES`. ``waited_s`` on a
        result is that device's own wait: the bound has to be reported per
        device, because "the broadcast timed out" names nothing to go and fix.
    """
    wanted = _normalise(command)
    ids = list(sessions or {})
    # A floor under the poll rather than trusting the caller: a zero would
    # spin the event loop for the whole timeout and starve the very read
    # loops that feed the records being waited for.
    poll = max(0.05, float(poll or 0.5))
    timeout = max(0.0, float(timeout or 0.0))

    started = time.monotonic()
    settled: dict[str, dict] = {}
    # Sessions that have closed a record since `after` without matching this
    # command. Kept rather than settled immediately: the device may simply
    # have answered something else first, and the matching record may still be
    # on its way. It decides the state at the deadline, nothing sooner.
    seen_activity: set[str] = set()

    while True:
        for session_id in ids:
            if session_id in settled:
                continue
            outcome, saw_new = _resolve(session_id, sessions or {}, wanted, after)
            if saw_new:
                seen_activity.add(session_id)
            if outcome is None:
                continue
            outcome["waited_s"] = round(time.monotonic() - started, 2)
            settled[session_id] = outcome

        if len(settled) == len(ids):
            break
        if time.monotonic() - started >= timeout:
            break
        # `poll`, or whatever is left of the budget — whichever is shorter, so
        # the bound is the bound rather than the bound rounded up to the next
        # poll.
        await asyncio.sleep(min(poll, max(0.0, timeout - (time.monotonic() - started))))

    waited = round(time.monotonic() - started, 2)

    results = []
    for session_id in ids:
        outcome = settled.get(session_id)
        if outcome is None:
            # The deadline decides between the two unanswered states, and the
            # distinction is the point of the exercise: "back at a prompt but
            # unmatched" is a prompt-detection problem on that platform, and
            # "still talking" is a slow device.
            if session_id in seen_activity:
                outcome = {
                    "state": "not-captured", "output": "", "ran_at": 0.0,
                    "detail": ("The device answered, but no record matched "
                               "this command. Its prompt shape may not be "
                               "recognised on this platform."),
                }
            else:
                outcome = {
                    "state": "timeout", "output": "", "ran_at": 0.0,
                    "detail": (f"No reply within {timeout:g}s. The device had "
                               f"not returned to a prompt."),
                }
            outcome["waited_s"] = waited

        session = (sessions or {}).get(session_id)
        results.append({
            "session_id": session_id,
            "label":      _label(session_id, session),
            "state":      outcome["state"],
            "output":     outcome.get("output", ""),
            "ran_at":     outcome.get("ran_at", 0.0),
            "waited_s":   outcome.get("waited_s", waited),
            "detail":     outcome.get("detail", ""),
            # What the device actually recorded, which may be the expanded
            # form of what was asked for. Worth keeping: an operator who sent
            # `sh ver` and got `show version` back should be able to see that
            # is why the two lists read differently.
            "command":    outcome.get("command", command),
        })

    collected = sum(1 for r in results if r["state"] == "collected")
    logger.info("Collected %s of %s replies to %r in %.1fs",
                collected, len(results), command, waited)
    return {"command": command, "results": results, "waited_s": waited}


# ---------------------------------------------------------------------------
# Comparing
# ---------------------------------------------------------------------------


def _comparable(text: str) -> list[str]:
    """
    Output reduced to the lines a difference would be *about*.

    Trailing whitespace and the blank lines a pager leaves around a reply are
    not differences anybody acts on, and a diff that reports them buries the
    one line that matters. Nothing else is touched — not case, not order, not
    the numbers — because every one of those is a real difference between two
    devices.
    """
    lines = [line.rstrip() for line in str(text or "").splitlines()]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return lines


def _diff(baseline_label: str, baseline: list[str],
          label: str, lines: list[str]) -> tuple[str, int, int]:
    """A unified diff of one device against the baseline, and its two counts."""
    # n=2 rather than the default 3: this is read in a chat panel beside
    # thirty-nine other devices, not in a code review.
    parts = list(difflib.unified_diff(
        baseline, lines, fromfile=baseline_label, tofile=label,
        lineterm="", n=2))

    added = sum(1 for line in parts
                if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in parts
                  if line.startswith("-") and not line.startswith("---"))

    if len(parts) > MAX_DIFF_LINES:
        kept = parts[:MAX_DIFF_LINES]
        kept.append(f"... {len(parts) - MAX_DIFF_LINES} more lines of diff")
        parts = kept

    return "\n".join(parts), added, removed


def _named(labels: list[str]) -> str:
    """``sw-14, sw-22`` — or the first few and a count, past a sentence's worth."""
    if len(labels) <= MAX_NAMED:
        return ", ".join(labels)
    head = ", ".join(labels[:MAX_NAMED])
    return f"{head} and {len(labels) - MAX_NAMED} more"


def compare(results: list[dict]) -> dict:
    """
    Diff every collected device against the first collected one.

    The first is the baseline rather than a vote or an average, and it is
    named in the result. "Different from sw-01" is a fact an operator can
    check; "different from the consensus" is a claim this has no standing to
    make — six devices upgraded and thirty-four not is a majority that is
    wrong.

    Args:
        results: The ``results`` list from :func:`collect`, states and all.

    Returns:
        ``{"baseline", "identical", "differing", "uncollected", "summary"}``.
        ``identical`` counts the baseline among its own labels, so the two
        lists plus ``uncollected`` add up to the number of devices — a
        summary whose numbers do not reconcile with the list beside it is one
        nobody trusts twice.
    """
    rows = [r for r in (results or []) if isinstance(r, dict)]
    collected = [r for r in rows if r.get("state") == "collected"]
    uncollected = [str(r.get("label") or r.get("session_id") or "?")
                   for r in rows if r.get("state") != "collected"]

    out = {"baseline": "", "identical": [], "differing": [],
           "uncollected": uncollected, "summary": ""}

    if not collected:
        # Nothing to compare, and — rule two — nothing may be implied about
        # the devices that did not answer. They are named, and that is all.
        if not rows:
            out["summary"] = "Nothing to compare."
        else:
            out["summary"] = (f"Nothing collected from {len(uncollected)} "
                              f"device{'s' if len(uncollected) != 1 else ''}, "
                              f"so nothing to compare: {_named(uncollected)}.")
        return out

    first = collected[0]
    baseline_label = str(first.get("label") or first.get("session_id") or "?")
    baseline = _comparable(first.get("output", ""))
    out["baseline"] = baseline_label

    identical = [baseline_label]
    differing = []
    for row in collected[1:]:
        label = str(row.get("label") or row.get("session_id") or "?")
        lines = _comparable(row.get("output", ""))
        if lines == baseline:
            identical.append(label)
            continue
        text, added, removed = _diff(baseline_label, baseline, label, lines)
        differing.append({"label": label, "diff": text,
                          "added": added, "removed": removed})

    out["identical"] = identical
    out["differing"] = differing

    # The summary leads with what somebody acts on. A line that opens "38
    # identical" is a line that gets skimmed, and the two names after the
    # comma are the entire reason the comparison was run.
    if differing:
        head = (f"{len(differing)} differ: "
                f"{_named([d['label'] for d in differing])}")
        summary = f"{head}; {len(identical)} identical"
    elif len(identical) == 1:
        summary = f"Only {baseline_label} answered; nothing to compare it with"
    else:
        summary = f"All {len(identical)} identical"

    if uncollected:
        # Named separately, and never counted as agreement. A device that did
        # not answer is not a device that matched.
        summary += (f"; {len(uncollected)} not collected: "
                    f"{_named(uncollected)}")

    out["summary"] = summary + "."
    return out
