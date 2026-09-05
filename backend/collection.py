"""
collection.py — Show commands on the backup timer, stored and diffable (#547).

Configuration drift is one kind of drift. "Which interfaces started erroring
this week" is the other, and it is the one monitoring rarely answers per
port. The scheduler already logs into every device in a group overnight; the
marginal cost of asking it three more questions while it is there is a few
seconds, and the answers land in History where they can be searched and
compared with last night's.

Three rules, and the first is not negotiable:

**Read-only, checked twice.** Only snippets not marked `writes` may be
scheduled, and every command is checked against the platform's dangerous
list before it is sent — the same check `config_push` makes. A scheduled
overnight job is the worst possible place for a command that changes
something: nobody is watching, and the first anybody hears of it is the
device that did not come back.

**One synthetic session per device per run.** Stored through the same
`store` every live session uses, with `connection_type = "collection"` so
History can tell them apart, and so a filter can find "every collection
of `show interfaces status` on the access layer from last night" without
knowing which run it was.

**Bounded, or it eats the disk.** Output is capped per command by the same
`history.max_output_chars` a live session respects; the number of runs kept
per device is capped by `history.collection_keep`; and both are enforced
here rather than assumed. Unbounded growth was the stated risk, and it is
the one that turns this from useful into a folder somebody has to go and
clear at 2 a.m.
"""
from __future__ import annotations

import difflib
import logging
import time
import uuid

from backend.advanced import get as advanced

logger = logging.getLogger(__name__)

#: The connection type a collection session is stored under. History and
#: the retention sweep both key on it.
KIND = "collection"

#: How long to wait for one command's output to stop arriving. Generous:
#: `show tech` is not on the list, but `show interfaces` on a 48-port
#: stack takes a while.
COMMAND_TIMEOUT = 20.0


class CollectionError(Exception):
    """A snippet that must not be scheduled, or a run that could not start."""


# ---------------------------------------------------------------------------
# What may be scheduled
# ---------------------------------------------------------------------------

def eligible(snippet) -> str:
    """
    Why a snippet may not be collected, or "" when it may.

    A reason rather than a bool, because the schedule dialog has to say
    which of three different things is wrong: it writes, it has no
    commands, or one of its commands is on the dangerous list for the
    platform it names. "Not eligible" sends somebody to the snippet editor
    to guess.
    """
    if getattr(snippet, "writes", False):
        return "it is marked as changing the device"
    commands = [c for c in (getattr(snippet, "commands", None) or []) if c.strip()]
    if not commands:
        return "it has no commands in it"

    platform = getattr(snippet, "platform", "") or ""
    if platform:
        from backend.platforms import matches_dangerous

        for command in commands:
            try:
                hit = matches_dangerous(platform, command.strip())
            except Exception:
                hit = ""
            if hit:
                return f"`{command.strip()}` is on the dangerous list for {platform}"
    return ""


def eligible_snippets() -> list[dict]:
    """
    Every snippet with the reason it cannot be scheduled, if any.

    All of them rather than only the eligible ones, so the dialog can show
    a greyed entry with its reason beside it. A list that silently omitted
    the write snippets would have somebody wondering where theirs went.
    """
    from backend.snippets import load_snippets

    out = []
    for snippet in load_snippets():
        out.append({
            "id":       snippet.id,
            "name":     snippet.name,
            "platform": snippet.platform,
            "commands": list(snippet.commands),
            "reason":   eligible(snippet),
        })
    return out


def normalise(chosen) -> list[str]:
    """
    The snippet ids a schedule will collect, checked again on the way in.

    Checked here as well as in the dialog, because the dialog is one of two
    routes — the group is also a JSON file people are told they may edit —
    and a `writes` snippet that reached the schedule through the file would
    otherwise run at 2 a.m. with nobody watching.
    """
    from backend.snippets import load_snippets

    by_id = {s.id: s for s in load_snippets()}
    kept: list[str] = []
    for raw in (chosen or []):
        snippet_id = str(raw or "").strip()
        snippet = by_id.get(snippet_id)
        if snippet is None:
            continue
        why = eligible(snippet)
        if why:
            logger.warning("Not collecting %s on the schedule: %s",
                           snippet.name, why)
            continue
        if snippet_id not in kept:
            kept.append(snippet_id)
    return kept


# ---------------------------------------------------------------------------
# Running one
# ---------------------------------------------------------------------------

def _run_command(channel, command: str) -> str:
    """One command on an open second channel, output up to the cap."""
    from backend.configs import _read_until_idle

    channel.send((command + "\n").encode())
    raw = _read_until_idle(channel, timeout=COMMAND_TIMEOUT)
    cap = int(advanced("history.max_output_chars"))
    text = raw if len(raw) <= cap else raw[:cap] + "\n… (truncated)"
    return _strip_echo(text, command)


def _strip_echo(text: str, command: str) -> str:
    """The device echoes the command back first; History stores it apart."""
    lines = text.splitlines()
    if lines and lines[0].strip() == command.strip():
        lines = lines[1:]
    return "\n".join(lines).strip("\r\n")


def collect(session: dict, snippet_ids: list[str], run_id: str = "") -> dict:
    """
    Run each snippet's commands on *session* and store the results.

    Called by the scheduler after `capture`, while the headless session it
    opened is still up. One synthetic History session per device per run,
    labelled with the run so "last night" is a thing that can be found.

    Never raises for a command that fails — the next one still runs, and
    the failure is stored as that command's output, because "the command
    produced an error" is itself the finding on a device that has just
    lost a feature. It does raise when the session cannot open a second
    channel at all, so the scheduler records the device as failed rather
    than as quietly collected.
    """
    from backend.connections.ssh_handler import SSHHandler
    from backend.session.transcript import CommandRecord
    from backend.snippets import load_snippets
    from backend.store import store

    handler = session.get("handler")
    if not handler or not getattr(handler, "is_connected", False):
        raise CollectionError("The session is no longer connected.")
    if not isinstance(handler, SSHHandler):
        raise CollectionError("Collection needs SSH — a serial or telnet "
                              "session cannot open a second channel.")

    by_id = {s.id: s for s in load_snippets()}
    snippets = [by_id[i] for i in snippet_ids if i in by_id]
    # Re-checked at the moment of running, not only when scheduled. A
    # snippet edited to `writes` after it was scheduled must not run.
    snippets = [s for s in snippets if not eligible(s)]
    if not snippets:
        return {"stored": 0, "session_id": "", "commands": 0}

    hostname = session.get("hostname") or session.get("display_label") or "?"
    run_id = run_id or time.strftime("%Y%m%d-%H%M")
    synthetic_id = f"collection-{uuid.uuid4()}"

    channel = handler.open_secondary_channel()
    try:
        from backend.configs import _read_until_idle, session_platform
        from backend.platforms import get_profile

        _read_until_idle(channel, timeout=3.0)
        paging_off = get_profile(session_platform(session)).paging_off
        if paging_off:
            channel.send((paging_off + "\n").encode())
            _read_until_idle(channel, timeout=5.0)

        store.start_session(synthetic_id, {
            "display_label":   f"{hostname} · collection {run_id}",
            "hostname":        hostname,
            "connection_type": KIND,
            "username":        session.get("username") or "",
            "target":          run_id,
        })

        stored = 0
        for snippet in snippets:
            for command in snippet.commands:
                command = command.strip()
                if not command:
                    continue
                started = time.time()
                try:
                    output = _run_command(channel, command)
                except Exception as exc:
                    output = f"(collection failed: {exc})"
                record = CommandRecord(
                    command=command, output=output,
                    prompt=hostname, started_at=started,
                    duration_ms=max(0, int((time.time() - started) * 1000)))
                if store.add_command(synthetic_id, record) != -1:
                    stored += 1
    finally:
        try:
            channel.close()
        except Exception:
            pass
        try:
            store.end_session(synthetic_id)
        except Exception:
            pass

    prune(hostname)
    logger.info("Collected %d command(s) from %s into History", stored, hostname)
    return {"stored": stored, "session_id": synthetic_id,
            "commands": sum(len(s.commands) for s in snippets)}


# ---------------------------------------------------------------------------
# Finding and comparing
# ---------------------------------------------------------------------------

def runs_for(hostname: str, limit: int = 30) -> list[dict]:
    """The collection sessions for a device, newest first."""
    from backend.store import store

    return [s for s in store.list_sessions(limit * 4, hostname)
            if s.get("connection_type") == KIND][:limit]


def compare(session_id: str) -> dict:
    """
    Every command in one collection run against the same command in the
    run before it on the same device.

    Matched by command text, not by position: a snippet edited between
    runs shifts positions, and comparing `show ip route` against last
    night's `show interfaces` is a diff that is all noise and looks like
    all signal.
    """
    from backend.store import store

    current = store.get_session(session_id)
    if not current or current.get("connection_type") != KIND:
        raise CollectionError("That is not a collection run.")

    earlier = [s for s in runs_for(current.get("hostname") or "", 50)
               if s.get("id") != session_id
               and float(s.get("started_at") or 0) < float(current.get("started_at") or 0)]
    if not earlier:
        return {"session_id": session_id, "previous": None, "commands": [],
                "summary": "This is the first collection from this device, so "
                           "there is nothing yet to compare it with."}

    previous = store.get_session(earlier[0]["id"]) or {}
    before = {c.get("command"): c.get("output") or ""
              for c in previous.get("commands") or []}

    out = []
    changed = 0
    for entry in current.get("commands") or []:
        command = entry.get("command") or ""
        now_text = entry.get("output") or ""
        then_text = before.get(command)
        if then_text is None:
            out.append({"command": command, "state": "new", "diff": "",
                        "added": 0, "removed": 0})
            continue
        if then_text == now_text:
            out.append({"command": command, "state": "same", "diff": "",
                        "added": 0, "removed": 0})
            continue
        diff = list(difflib.unified_diff(
            then_text.splitlines(), now_text.splitlines(),
            fromfile="previous run", tofile="this run", lineterm="", n=2))
        added = sum(1 for l in diff[2:] if l.startswith("+"))
        removed = sum(1 for l in diff[2:] if l.startswith("-"))
        changed += 1
        out.append({"command": command, "state": "changed",
                    "diff": "\n".join(diff), "added": added, "removed": removed})

    same = sum(1 for c in out if c["state"] == "same")
    summary = (f"{changed} command{'s' if changed != 1 else ''} changed since "
               f"the previous run, {same} identical."
               if changed else
               f"Nothing changed since the previous run ({same} "
               f"command{'s' if same != 1 else ''} identical).")
    return {"session_id": session_id, "previous": earlier[0]["id"],
            "previous_at": earlier[0].get("started_at"),
            "commands": out, "changed": changed, "summary": summary}


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------

def prune(hostname: str) -> int:
    """
    Keep only the newest `history.collection_keep` runs for a device.

    Per device rather than global, because a group of forty is forty times
    the growth and the cap has to scale with it. Returns how many went.
    """
    from backend.store import store

    keep = int(advanced("history.collection_keep"))
    runs = runs_for(hostname, keep + 50)
    removed = 0
    for old in runs[keep:]:
        try:
            store.delete_session(old["id"])
            removed += 1
        except Exception as exc:
            logger.debug("Could not prune collection %s: %s", old.get("id"), exc)
    return removed
