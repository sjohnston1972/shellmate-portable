"""
scheduler.py — Configuration backups on a timer, per group (#408).

The capture code has always run on connect. This runs it on a schedule: a
group can ask for its devices' configurations every hour, day or week at a
given time, and a thread checks once a minute whether anything is due.

Rules:

- **Never while the user is on that device.** A profile with an open
  session is captured through that session's second channel if the device
  allows one, and skipped otherwise — a background login racing a person
  mid-change is exactly the kind of surprise this tool exists to avoid.
- **Headless sessions are short.** Connect, capture, disconnect. They use
  the saved credentials exactly as a tile click would, and a profile with
  none is reported as skipped rather than prompted for.
- **One at a time.** Devices are captured sequentially. Forty simultaneous
  logins from one laptop is how a TACACS server rate-limits you.
- **The record is on the group.** ``backup_last`` says when it ran, what
  succeeded and what did not, so the answer to "did last night's backup
  happen" is in the group's own menu rather than in a log.
- **And it says what did *not* happen** (#612). A scheduler that reports
  only what ran is indistinguishable from one that ran and found nothing:
  close ShellMate on Friday, open it on Monday, and "last run Friday, 12
  ok" is true and says nothing about the two nights that were owed. So a
  catch-up records how many slots it stood in for.
- **Catching up is a choice, and it is one run.** ShellMate catches up
  where the container's pipelines skip, because a stale configuration
  capture is the thing being avoided while a drift check answering about
  last week is worthless. But it catches up *once* however far behind, so
  three missed nights are one backup rather than three rounds of logins to
  every device.

The due-time arithmetic is a pure function so it can be tested without a
clock, a thread or a device.
"""

import logging
import threading
import time
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

EVERY = ("hourly", "daily", "weekly")
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

_thread: threading.Thread | None = None
_stop = threading.Event()
_running = threading.Lock()


def normalise(schedule) -> dict | None:
    """
    A schedule as stored on a group, or None when disabled/invalid.

    ``{"enabled": bool, "every": "hourly|daily|weekly", "at": "HH:MM",
    "day": "mon".."sun"}``. ``at`` is ignored for hourly; ``day`` only
    matters for weekly.
    """
    if not isinstance(schedule, dict) or not schedule.get("enabled"):
        return None
    every = str(schedule.get("every", "daily")).lower()
    if every not in EVERY:
        every = "daily"
    at = str(schedule.get("at", "02:00"))
    try:
        hour, minute = (int(x) for x in at.split(":", 1))
        if not (0 <= hour < 24 and 0 <= minute < 60):
            raise ValueError
    except ValueError:
        hour, minute = 2, 0
    day = str(schedule.get("day", "sun")).lower()[:3]
    if day not in DAYS:
        day = "sun"
    return {"enabled": True, "every": every, "at": f"{hour:02d}:{minute:02d}", "day": day}


def next_run(schedule: dict, after: datetime) -> datetime:
    """The first moment at or after ``after`` the schedule fires."""
    every = schedule["every"]
    if every == "hourly":
        base = after.replace(minute=0, second=0, microsecond=0)
        return base if base >= after else base + timedelta(hours=1)
    hour, minute = (int(x) for x in schedule["at"].split(":"))
    candidate = after.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if every == "daily":
        return candidate if candidate >= after else candidate + timedelta(days=1)
    wanted = DAYS.index(schedule["day"])
    delta = (wanted - candidate.weekday()) % 7
    candidate = candidate + timedelta(days=delta)
    if candidate < after:
        candidate += timedelta(days=7)
    return candidate


def is_due(schedule, last_run: float | None, now: float) -> bool:
    """
    Whether a run is owed at ``now`` (epoch seconds).

    Due when the first firing after the last run is at or before now. With
    no last run, the schedule fires at its next slot — not immediately, so
    switching a nightly backup on at 3pm does not start one at 3pm.
    """
    plan = normalise(schedule)
    if plan is None:
        return False
    anchor = datetime.fromtimestamp(last_run) if last_run else datetime.fromtimestamp(now)
    if last_run:
        fire = next_run(plan, anchor + timedelta(seconds=1))
    else:
        # First run: the next slot after the schedule was seen, tracked by
        # the caller storing `armed_at` as last_run once it passes.
        fire = next_run(plan, anchor)
        return False if fire.timestamp() > now else True
    return fire.timestamp() <= now


def run_group(key: str, profiles: list[dict], connect, capture, open_session_for, destroy) -> dict:
    """
    Back up every profile in a group, one after another.

    Injected callables keep this testable: ``open_session_for(profile)``
    returns a live session dict or None; ``connect(profile)`` returns a new
    headless session dict (or raises); ``capture(session)`` captures;
    ``destroy(session)`` closes a headless session.
    """
    started = time.time()
    ok: list[str] = []
    changed: list[str] = []
    failed: list[dict] = []
    skipped: list[dict] = []
    for profile in profiles:
        name = profile.get("name") or profile.get("hostname") or profile.get("id", "?")
        if (profile.get("connection_type") or "ssh") != "ssh":
            skipped.append({"name": name, "why": "not an SSH connection"})
            continue
        live = open_session_for(profile)
        session = live
        headless = False
        try:
            if session is None:
                if not profile.get("has_saved_credentials") and not profile.get("credential_ref"):
                    skipped.append({"name": name, "why": "no saved credentials"})
                    continue
                session = connect(profile)
                headless = True
            result = capture(session)
            ok.append(name)
            # Whether the configuration differed from the last one stored
            # is the single most valuable thing a nightly run produces —
            # "somebody changed core-2 overnight" — and it used to go into
            # a log line and nowhere else (#539).
            if result.get("stored"):
                changed.append(name)
            logger.info("Scheduled backup of %s: %s", name,
                        "changed" if result.get("stored") else "unchanged")
        except Exception as exc:
            failed.append({"name": name, "why": str(exc)[:200]})
            logger.warning("Scheduled backup of %s failed: %s", name, exc)
        finally:
            if headless and session is not None:
                try:
                    destroy(session)
                except Exception:
                    pass
    return {
        "at": started, "took_s": round(time.time() - started, 1),
        "ok": ok, "changed": changed, "failed": failed, "skipped": skipped,
        "group": key,
    }


# ------------------------------------------------------------------ digest
#
# Scheduled backups were built and then reported into a log file. The most
# valuable thing they produce — "somebody changed core-2 overnight", "the
# Glasgow run has failed three nights running", "nothing ran at all
# because the laptop was shut" — went nowhere anybody would see it.
#
# Two rules shape what follows:
#
# **Only what is worth saying.** A clean run where nothing changed is the
# normal night, and a digest that announces it every morning is a digest
# people learn to dismiss without reading — at which point it is worse
# than nothing, because the morning it matters looks like all the others.
#
# **A run that did not happen counts.** #612 records those, and a gap in a
# backup history looks exactly like a period in which nothing changed.
# That is the dangerous direction for this to fail in.


def _seen_before() -> float:
    """When somebody last read the digest. 0 if never."""
    try:
        from backend.settings_store import peek

        return float((peek("backups") or {}).get("digest_seen") or 0)
    except Exception:                                     # pragma: no cover
        return 0.0


def mark_seen(at: float | None = None) -> float:
    """Record that the digest has been read, so it stops asking."""
    from backend.settings_store import update_settings

    when = float(at if at is not None else time.time())
    update_settings({"backups": {"digest_seen": when}})
    return when


def digest(include_seen: bool = False) -> dict:
    """
    What the scheduled backups found that somebody should know about.

    Returns ``{"anything": False, "groups": []}`` when there is nothing —
    and nothing is the common case. Silence here is a feature: a report
    that fires every morning whether or not anything happened is one that
    gets dismissed unread, and then the morning something did happen looks
    exactly like every other morning.
    """
    from backend import groups as groups_module

    seen = 0.0 if include_seen else _seen_before()
    out: list[dict] = []
    for group in groups_module.list_groups():
        last = group.get("backup_last") or {}
        at = float(last.get("at") or 0)
        if not at or at <= seen:
            continue

        changed = list(last.get("changed") or [])
        failed = list(last.get("failed") or [])
        missed = int(last.get("missed") or 0)

        # What the compliance check found, when the group has one and it
        # ran with this backup (#543). Only the two states somebody acts
        # on: devices missing lines, and devices there is no evidence
        # about at all. "46 compliant" is not news and a digest that
        # leads with it gets skimmed.
        check = group.get("compliance_last") or {}
        check_at = float(check.get("at") or 0)
        counts = check.get("counts") or {}
        # Only if the check is from this run rather than from a click
        # three weeks ago — a stale finding reported as this morning's
        # news is worse than no finding.
        fresh_check = check_at and abs(check_at - at) < 3600
        non_compliant = int(counts.get("missing", 0)) if fresh_check else 0
        unverifiable = int(counts.get("never-captured", 0)) if fresh_check else 0

        # An ok run with nothing changed is not news. Reporting it is how
        # a digest becomes noise, and noise is how the one that matters
        # gets missed.
        if (not changed and not failed and not missed
                and not non_compliant and not unverifiable):
            continue

        out.append({
            "group": group.get("key") or last.get("group") or "",
            "name": group.get("name") or group.get("key") or "",
            "at": at,
            "changed": changed,
            "failed": failed,
            "skipped": list(last.get("skipped") or []),
            # The runs that did not happen (#612). Named as their own
            # thing rather than folded into failures: "it failed" and "it
            # never ran" send somebody to two different places.
            "missed": missed,
            "missed_from": last.get("missed_from"),
            "missed_to": last.get("missed_to"),
            # Named separately from the backup's own numbers: "the capture
            # failed" and "the capture worked and the standard is not
            # there" are different mornings' work.
            "non_compliant": non_compliant,
            "unverifiable": unverifiable,
            "compliance": (check.get("summary", "") if fresh_check else ""),
            "snippet": (check.get("snippet", "") if fresh_check else ""),
        })

    out.sort(key=lambda entry: entry["at"], reverse=True)
    return {
        "anything": bool(out),
        "groups": out,
        "changed": sum(len(entry["changed"]) for entry in out),
        "failed": sum(len(entry["failed"]) for entry in out),
        "missed": sum(entry["missed"] for entry in out),
        "non_compliant": sum(entry["non_compliant"] for entry in out),
        "unverifiable": sum(entry["unverifiable"] for entry in out),
        "seen_at": seen,
    }


def digest_line(report: dict) -> str:
    """
    One sentence for a toast, or "" when there is nothing to say.

    Counts rather than names: four devices do not fit in a toast, and a
    sentence that trails off mid-list is worse than one that says how many
    and offers to show them.
    """
    if not report.get("anything"):
        return ""
    parts = []
    if report["changed"]:
        parts.append(f"{report['changed']} changed")
    if report["failed"]:
        parts.append(f"{report['failed']} failed")
    if report["missed"]:
        parts.append(f"{report['missed']} run"
                     f"{'' if report['missed'] == 1 else 's'} missed")
    if report.get("non_compliant"):
        parts.append(f"{report['non_compliant']} missing standard lines")
    if report.get("unverifiable"):
        parts.append(f"{report['unverifiable']} with no capture to check")
    where = (report["groups"][0]["name"] if len(report["groups"]) == 1
             else f"{len(report['groups'])} groups")
    return f"Scheduled backups, {where}: " + ", ".join(parts) + "."


# ---------------------------------------------------------------- the thread
def start() -> None:
    """Start the once-a-minute check. Idempotent."""
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, daemon=True, name="backup-scheduler")
    _thread.start()


def stop() -> None:
    _stop.set()


_last_prune = 0.0
PRUNE_EVERY = 24 * 3600


def _housekeeping(now: float) -> None:
    """
    History pruning, once a day (#464). It used to run on every connect,
    under the store lock, so the first sweep after a retention was set on
    a large database landed on somebody opening a session.
    """
    global _last_prune
    if now - _last_prune < PRUNE_EVERY:
        return
    _last_prune = now
    from backend.store import store
    removed = store.prune()
    if removed:
        logger.info("History: pruned %d rows past the retention", removed)


def _loop() -> None:
    # A moment for the server to finish starting before the first check.
    _stop.wait(20)
    while not _stop.is_set():
        try:
            tick(time.time())
        except Exception as exc:                     # never let the thread die
            logger.warning("Backup scheduler: %s", exc)
        try:
            _housekeeping(time.time())
        except Exception as exc:
            logger.warning("History housekeeping: %s", exc)
        _stop.wait(60)


def missed_since(schedule, last_run: float | None, now: float) -> list[float]:
    """
    Every slot that came and went without a run (#612).

    The scheduler could say when a backup last happened and nothing about
    when one was owed and did not, so a weekend with ShellMate closed read
    identically to a weekend in which nothing changed. A gap in a backup
    history looking like a quiet period is the dangerous direction for this
    to fail in.

    Bounded at 200 slots so a year of downtime on an hourly schedule
    produces a number rather than a walk through nine thousand datetimes.
    """
    plan = normalise(schedule)
    if plan is None or not last_run:
        return []
    slots: list[float] = []
    fire = next_run(plan, datetime.fromtimestamp(last_run) + timedelta(seconds=1))
    while fire.timestamp() <= now and len(slots) < 200:
        slots.append(fire.timestamp())
        fire = next_run(plan, fire + timedelta(seconds=1))
    return slots


def tick(now: float) -> list[str]:
    """Run whatever is due. Returns the keys of the groups that ran."""
    from backend import groups as groups_module
    ran: list[str] = []
    for group in groups_module.list_groups():
        schedule = group.get("backup")
        plan = normalise(schedule)
        if plan is None:
            continue
        last = (group.get("backup_last") or {}).get("at")
        armed = (schedule or {}).get("armed_at")
        if not last and not armed:
            # First sighting: arm at the next slot rather than run now.
            groups_module.update_group(group["key"], {"backup": {**schedule, "armed_at": now}})
            continue
        if not is_due(schedule, last or armed, now):
            continue
        if not _running.acquire(blocking=False):
            return ran
        try:
            # What was owed and did not happen, worked out before the run
            # resets the clock. ShellMate catches up rather than skipping —
            # deliberately, and differently from the container's pipelines,
            # because a stale configuration capture is the thing being
            # avoided and a drift check answering about last week is not.
            #
            # It catches up **once, however far behind**, never once per
            # missed night: three missed nights are one backup, not three
            # logins to every device in the group. The rest are recorded as
            # missed rather than performed.
            owed = missed_since(schedule, last or armed, now)
            result = run_now(group["key"])
            if len(owed) > 1:
                result = dict(result)
                result["missed"] = len(owed) - 1
                result["missed_from"] = owed[0]
                result["missed_to"] = owed[-2]
                groups_module.update_group(group["key"], {"backup_last": result})
                logger.info("Scheduled backup for %s caught up: %d earlier "
                            "run(s) were missed while ShellMate was closed",
                            group["key"], len(owed) - 1)
            ran.append(group["key"])
            logger.info("Scheduled backup for %s: %d ok, %d failed, %d skipped",
                        group["key"], len(result["ok"]), len(result["failed"]), len(result["skipped"]))
        finally:
            _running.release()
    return ran


def recheck_compliance(key: str) -> dict | None:
    """
    Re-run a group's saved compliance check after its backup (#543).

    The check reads stored snapshots, so its answer is exactly as fresh as
    the last capture. Running it immediately after the nightly backup is
    the moment its evidence is newest — and it is the difference between a
    standard that is *verified* every night and one that was verified the
    afternoon somebody happened to click.

    Returns None when the group has not asked for one. Never raises: a
    compliance check is a report about a backup, and it must not be able to
    turn a completed backup into a failed one.
    """
    from backend import compliance as compliance_module
    from backend import groups as groups_module
    from backend import snippets as snippets_module
    from backend.profiles import profiles_tagged
    from backend.store import store as history

    try:
        group = next((g for g in groups_module.list_groups()
                      if g.get("key") == key), None)
        wanted = (group or {}).get("compliance") or {}
        if not wanted.get("enabled") or not wanted.get("snippet_id"):
            return None

        library = {s.id: s for s in snippets_module.load_snippets()}
        golden = library.get(wanted["snippet_id"])
        if golden is None:
            # The snippet was deleted after the group was set to check
            # against it. Said out loud rather than silently stopping: a
            # check that quietly stops running reports compliance by
            # omission, which is the worst way for this to fail.
            logger.warning("Group %s checks against snippet %s, which no "
                           "longer exists", key, wanted["snippet_id"])
            return {"group": key, "at": time.time(), "devices": [],
                    "counts": {}, "checked": 0,
                    "summary": "The snippet this group checks against has "
                               "been deleted, so nothing was checked.",
                    "limits": compliance_module.LIMITS,
                    "stale_after_days": compliance_module.STALE_AFTER_DAYS,
                    "snippet": ""}

        forbidden = library.get(wanted.get("must_not_have_id") or "")
        profiles = profiles_tagged(key, include_nested=True)
        report = compliance_module.check_group(
            key, profiles, lambda _platform: golden, history.latest_snapshot,
            (lambda _platform: forbidden) if forbidden else None)
        report["snippet"] = golden.name
        report["summary"] = compliance_module.summary_line(report)
        groups_module.update_group(key, {"compliance_last": report})
        return report
    except Exception as exc:
        logger.warning("Compliance re-check for %s failed: %s", key, exc)
        return None


def run_now(key: str) -> dict:
    """Back up a group immediately and record the result on it."""
    from backend import groups as groups_module
    from backend.profiles import profiles_tagged
    profiles = profiles_tagged(key, include_nested=True)
    group_id = next((g.get("id") for g in groups_module.list_groups() if g.get("key") == key), None)
    result = run_group(key, profiles, _connect, _capture, _open_session_for, _destroy)
    # Recorded on the group by id (#466): a rename that landed while the
    # backup ran used to make this create a ghost entry under the old key.
    current = next((g for g in groups_module.list_groups() if g.get("id") == group_id), None)
    groups_module.update_group(current["key"] if current else key, {"backup_last": result})

    # Immediately after, while the captures are as fresh as they will ever
    # be. Its result goes on the group and into the digest; it cannot fail
    # the backup that produced its evidence.
    compliance_report = recheck_compliance(current["key"] if current else key)
    if compliance_report:
        result = dict(result)
        result["compliance"] = compliance_report.get("summary", "")

    # Tell whatever is watching (#539). After the compliance re-check, not
    # before: its findings are attached to the group afterwards, and a
    # message sent earlier would disagree with the panel for exactly the
    # runs where the disagreement mattered most.
    #
    # It reads the digest rather than being handed `result`, so the numbers
    # it posts are the numbers on the screen. It cannot fail the backup —
    # the configurations are already stored, which is the part that
    # mattered.
    try:
        from backend import backup_webhook

        backup_webhook.notify_after_run()
    except Exception as exc:
        logger.warning("Could not post the backup digest: %s", exc)

    return result


# ---------------------------------------------------------------- glue
def _session_manager():
    from backend.app import session_manager
    return session_manager


def _open_session_for(profile: dict):
    manager = _session_manager()
    for session in manager.get_all_sessions():
        if session.get("profile_id") == profile.get("id"):
            return manager.get_session(session["session_id"])
    return None


def _connect(profile: dict) -> dict:
    """A headless session from a saved profile, credentials filled in server-side."""
    from backend.connections.base import ConnectionParams
    from backend.profiles import effective, load_credentials
    # The third connect path, through the same resolution as the other two
    # (#545): a group's jump host has to apply to a backup taken at 3 a.m.
    # as much as to a session somebody opens.
    profile = effective(profile)
    fields = {k: profile.get(k) for k in (
        "hostname", "port", "username", "private_key_path", "private_key_username",
        "jump_host", "jump_port", "jump_username", "jump_private_key_path") if profile.get(k)}
    params = ConnectionParams(connection_type="ssh", display_label=profile.get("name", ""), **fields)
    # One resolution (#466): load_credentials() already prefers the
    # connection's own credentials and falls back to its named set, so the
    # explicit resolve_set() before it was the same set resolved twice,
    # with a full profile scan each time, sequentially across the group.
    for field, value in load_credentials(profile.get("id", "")).items():
        if not getattr(params, field, ""):
            setattr(params, field, value)
    params.credential_source = "saved"
    created = _session_manager().create_session(params, profile.get("id", ""))
    return _session_manager().get_session(created["session_id"])


def _capture(session: dict) -> dict:
    from backend.configs import capture_config
    return capture_config(session)


def _destroy(session: dict) -> None:
    _session_manager().destroy_session(session["session_id"])
