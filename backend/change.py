"""
change.py — A piece of work, bracketed (#544).

`config_push.apply` already captures before, sends, captures after and
diffs — but only for a push. A change typed by hand gets nothing until the
next login, when "since your last visit" reports it, mixed in with whatever
else happened in between and attributed to nobody.

The evidence a change board wants is "what did you type, and what changed",
produced at the end of the window rather than reconstructed a week later
from a diff and a memory. So: a change is opened, the configuration is
captured and pinned as the baseline, and closing it captures again, diffs
against the pin, and lists the commands typed in between.

**Keyed on the hostname, and persisted.** This is the whole design
constraint and it is the issue's own stated risk. A change that spans a
reload is not the awkward case, it is the *normal* case — reloading is
frequently the change — and the session is exactly what a reload destroys.
A record living on the session dict would evaporate at the moment it became
most valuable, and the person who typed the commands would have nothing to
show for the window they just spent.

Two consequences worth stating because they look like bugs otherwise:

**One change per device at a time.** Two overlapping windows on one device
would produce two diffs of the same lines with no way to say which change
owned which line, so opening a second is refused and says what is already
open. Deliberately not a queue: the answer to "somebody else is already
changing this switch" is a person talking to a person.

**A change survives ShellMate restarting.** The file is the record. If that
were not true, closing the application during a maintenance window would
silently discard the evidence for it, which is worse than never having
offered the feature.
"""

import logging
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

from backend import jsonfile, paths

logger = logging.getLogger(__name__)

#: A change nobody closed. Left open indefinitely a stale record would
#: silently claim a window that ended days ago, and the diff at the end
#: would carry every unrelated thing that happened since. Reported as
#: stale rather than deleted — the capture at the start is still evidence,
#: and throwing away somebody's baseline because they went home is not
#: ours to do.
STALE_AFTER_SECONDS = 7 * 24 * 3600


@dataclass
class Change:
    """One open change window on one device."""

    id: str
    hostname: str
    started_at: float
    #: The snapshot taken when the window opened. May be absent: a device
    #: that would not give up its configuration still gets a window, and
    #: the record says so rather than refusing to bracket the work.
    before_id: int | None = None
    note: str = ""
    ticket: str = ""
    operator: str = ""
    #: Why there is no baseline, when there is not one.
    capture_error: str = ""
    #: The label the tab carried, for a record that reads as the device
    #: somebody knows rather than a hostname they may never have seen.
    label: str = ""

    @property
    def stale(self) -> bool:
        return (time.time() - self.started_at) > STALE_AFTER_SECONDS

    @property
    def age_seconds(self) -> float:
        return max(0.0, time.time() - self.started_at)

    def as_dict(self) -> dict:
        out = asdict(self)
        out["stale"] = self.stale
        out["age_seconds"] = round(self.age_seconds, 1)
        return out


def _file():
    return paths.data_dir() / "changes.json"


def _key(hostname: str) -> str:
    """
    How a device is identified across sessions.

    Case-folded, because a device that answers `Core-SW-01#` on one login
    and `core-sw-01#` on the next is one device, and a change opened under
    one spelling must be findable under the other. Trimmed for the same
    reason.
    """
    return (hostname or "").strip().lower()


def _load() -> dict[str, dict]:
    data = jsonfile.read(_file(), {}, expect=dict)
    return {k: v for k, v in data.items() if isinstance(v, dict)}


def _save(records: dict[str, dict]) -> None:
    jsonfile.write(_file(), records)


def _as_change(raw: dict) -> Change | None:
    """Rebuild a record, tolerating a file edited by hand."""
    try:
        return Change(
            id=str(raw.get("id") or ""),
            hostname=str(raw.get("hostname") or ""),
            started_at=float(raw.get("started_at") or 0),
            before_id=(int(raw["before_id"])
                       if raw.get("before_id") is not None else None),
            note=str(raw.get("note") or ""),
            ticket=str(raw.get("ticket") or ""),
            operator=str(raw.get("operator") or ""),
            capture_error=str(raw.get("capture_error") or ""),
            label=str(raw.get("label") or ""),
        )
    except (TypeError, ValueError):
        logger.warning("Ignoring an unreadable change record: %r", raw)
        return None


def active(hostname: str) -> Change | None:
    """The open change on this device, or None."""
    raw = _load().get(_key(hostname))
    return _as_change(raw) if raw else None


def open_changes() -> list[Change]:
    """Every open change, newest first."""
    out = [c for c in (_as_change(r) for r in _load().values()) if c]
    out.sort(key=lambda c: c.started_at, reverse=True)
    return out


def start(hostname: str, note: str = "", ticket: str = "",
          operator: str = "", label: str = "",
          before_id: int | None = None, capture_error: str = "") -> Change:
    """
    Open a change window on a device.

    Args:
        hostname:      The device. Everything is keyed on this.
        note:          What the change is for, in the operator's words.
        ticket:        A ticket reference, if there is one.
        operator:      Who is doing it.
        label:         The tab's display name, for the record.
        before_id:     The baseline snapshot, if one was captured.
        capture_error: Why there is no baseline, if there is not one.

    Raises:
        ValueError: No hostname, or a change is already open on this device.
    """
    key = _key(hostname)
    if not key:
        raise ValueError("A change needs a device to be about.")

    records = _load()
    existing = _as_change(records.get(key) or {}) if key in records else None
    if existing:
        opened = time.strftime("%Y-%m-%d %H:%M",
                               time.localtime(existing.started_at))
        raise ValueError(
            f"A change is already open on {existing.hostname or hostname}, "
            f"started {opened}"
            + (f" — {existing.note}" if existing.note else "")
            + ". End that one first.")

    record = Change(
        id=uuid.uuid4().hex[:12],
        hostname=hostname.strip(),
        started_at=time.time(),
        before_id=before_id,
        note=note.strip(),
        ticket=ticket.strip(),
        operator=operator.strip(),
        capture_error=capture_error.strip(),
        label=label.strip(),
    )
    records[key] = record.as_dict()
    # `stale` and `age_seconds` are derived; storing them would freeze a
    # value that is only true at the moment it was written.
    records[key].pop("stale", None)
    records[key].pop("age_seconds", None)
    _save(records)
    logger.info("Change %s opened on %s (baseline %s)",
                record.id, record.hostname, before_id or "none")
    return record


def end(hostname: str) -> Change | None:
    """
    Close the window and return the record that was open, or None.

    Returns the record rather than a bare success, because the caller needs
    the baseline id and the start time to build the diff and gather the
    commands, and re-reading it after the delete would be a race with
    itself.
    """
    key = _key(hostname)
    records = _load()
    raw = records.pop(key, None)
    if raw is None:
        return None
    _save(records)
    record = _as_change(raw)
    if record:
        logger.info("Change %s closed on %s after %.0fs",
                    record.id, record.hostname, record.age_seconds)
    return record


def abandon(hostname: str) -> bool:
    """
    Drop an open change without producing a record.

    Its own function rather than a flag on ``end``, because the two are
    different acts: one produces evidence and one says there is none to
    produce. A change opened on the wrong device should not leave a diff
    behind implying somebody did something there.
    """
    key = _key(hostname)
    records = _load()
    if key not in records:
        return False
    records.pop(key)
    _save(records)
    logger.info("Change abandoned on %s", hostname)
    return True


def commands_in_window(hostname: str, since: float,
                       until: float | None = None) -> list[dict]:
    """
    What was typed on this device during the window.

    Read from the history store rather than tracked on the session, which
    is the same reason the record itself is: the session may be a different
    one by the time the change ends, or three different ones.

    Never raises. A change record with no command list is worth less than
    one with it, and worth far more than an error where a record should be.
    """
    from backend.store import store as history

    try:
        hits = history.search(hostname=hostname, since=since, until=until,
                              limit=500)
    except Exception as exc:                              # pragma: no cover
        logger.info("Could not read the commands for a change on %s: %s",
                    hostname, exc)
        return []

    out: list[dict] = []
    for hit in hits:
        row = hit.as_dict() if hasattr(hit, "as_dict") else dict(hit)
        out.append({
            "command": row.get("command", ""),
            "ran_at": row.get("ran_at", 0),
            "session_id": row.get("session_id", ""),
        })
    out.sort(key=lambda r: r.get("ran_at") or 0)
    return out


# ---------------------------------------------------------------------------
# A change across a group (#544)
#
# One change on one switch is the common case; a maintenance window across a
# site is the one that produces a CAB pack. The mechanism is the same window
# opened on each member, through the scheduler's own injected-callable
# harness — so a device with no session open gets connected to headlessly,
# exactly as a nightly backup does, and the two cannot drift apart in which
# devices they can reach.
#
# **One record per device, never one merged diff.** Eight switches' hunks in
# one diff loses which line belonged to which device, which is the first
# thing anybody reading a change record needs. They are gathered under one
# group result instead.
# ---------------------------------------------------------------------------


def _device_of(profile: dict) -> str:
    """What a change on this profile would be keyed on."""
    return (str(profile.get("hostname") or "").strip()
            or str(profile.get("name") or "").strip())


def start_group(key: str, profiles: list[dict], connect, capture,
                open_session_for, destroy, note: str = "", ticket: str = "",
                operator: str = "") -> dict:
    """
    Open a change window on every member of a group.

    Injected callables, the same four the scheduler uses, for the same
    reason: a device with no session open has to be reachable, and a test
    must not need a switch.

    Returns:
        ``{"group", "at", "took_s", "started": [...], "skipped": [...],
           "failed": [...]}``. Partial success is the normal outcome and is
        reported rather than raised — a site where six of eight switches
        answered is six bracketed changes, not a failure.
    """
    started_at = time.time()
    started: list[dict] = []
    skipped: list[dict] = []
    failed: list[dict] = []

    for profile in profiles:
        name = (profile.get("name") or profile.get("hostname")
                or profile.get("id", "?"))
        hostname = _device_of(profile)

        if (profile.get("connection_type") or "ssh") != "ssh":
            skipped.append({"name": name, "why": "not an SSH connection"})
            continue
        if not hostname:
            skipped.append({"name": name, "why": "no device name to key it on"})
            continue
        # Somebody may already have opened a window on one member by hand.
        # Skipped, not failed, and certainly not overwritten: their baseline
        # is evidence and taking a second one would spend it.
        if active(hostname) is not None:
            skipped.append({"name": name, "why": "a change is already open"})
            continue

        session = open_session_for(profile)
        headless = False
        try:
            if session is None:
                if (not profile.get("has_saved_credentials")
                        and not profile.get("credential_ref")):
                    skipped.append({"name": name, "why": "no saved credentials"})
                    continue
                session = connect(profile)
                headless = True

            before_id, capture_error = None, ""
            try:
                result = capture(session)
                before_id = (result.get("snapshot") or {}).get("id")
            except Exception as exc:
                # The same rule as a single change: a device that will not
                # give up its configuration still gets a window, carrying
                # the reason. It is often the one most worth bracketing.
                capture_error = str(exc)[:200]

            record = start(hostname, note=note, ticket=ticket,
                           operator=operator, label=str(profile.get("name") or ""),
                           before_id=before_id, capture_error=capture_error)
            started.append({"name": name, "hostname": hostname,
                            "before_id": before_id,
                            "capture_error": capture_error})
        except Exception as exc:
            failed.append({"name": name, "why": str(exc)[:200]})
            logger.warning("Could not open a change on %s: %s", name, exc)
        finally:
            if headless and session is not None:
                try:
                    destroy(session)
                except Exception:
                    pass

    return {
        "group": key, "at": started_at,
        "took_s": round(time.time() - started_at, 1),
        "started": started, "skipped": skipped, "failed": failed,
        "note": note, "ticket": ticket,
    }


def end_group(key: str, profiles: list[dict], connect, capture,
              open_session_for, destroy) -> dict:
    """
    Close every open change window in a group and build the records.

    Only members that actually have a window open are touched. A group whose
    membership changed mid-window — somebody added a switch this afternoon —
    would otherwise have the new one reported as a failed close, when the
    truth is that no change was ever opened on it.
    """
    started_at = time.time()
    records: list[dict] = []
    skipped: list[dict] = []
    failed: list[dict] = []

    for profile in profiles:
        name = (profile.get("name") or profile.get("hostname")
                or profile.get("id", "?"))
        hostname = _device_of(profile)
        record = active(hostname) if hostname else None
        if record is None:
            skipped.append({"name": name, "why": "no change was open"})
            continue

        session = open_session_for(profile)
        headless = False
        after_id, capture_error = None, ""
        try:
            if session is None:
                if (not profile.get("has_saved_credentials")
                        and not profile.get("credential_ref")):
                    capture_error = "no saved credentials to reconnect with"
                else:
                    session = connect(profile)
                    headless = True
            if session is not None:
                try:
                    result = capture(session)
                    after_id = (result.get("snapshot") or {}).get("id")
                except Exception as exc:
                    capture_error = str(exc)[:200]
        except Exception as exc:
            capture_error = str(exc)[:200]
        finally:
            if headless and session is not None:
                try:
                    destroy(session)
                except Exception:
                    pass

        try:
            records.append(_close_one(record, after_id, capture_error))
        except Exception as exc:                          # pragma: no cover
            failed.append({"name": name, "why": str(exc)[:200]})

    return {
        "group": key, "at": started_at,
        "took_s": round(time.time() - started_at, 1),
        "records": records, "skipped": skipped, "failed": failed,
        "changed": [r["hostname"] for r in records if r.get("changed")],
        "unmeasurable": [r["hostname"] for r in records
                         if not r.get("comparable")],
    }


def _close_one(record: "Change", after_id: int | None,
               capture_error: str) -> dict:
    """
    Build one device's record and close its window.

    The same shape the single-device endpoint returns, so one renderer
    draws both — a group record that displayed differently from the record
    for one switch would be two things to learn for one idea.
    """
    from backend.configs import diff_snapshots
    from backend.store import store as history

    diff: dict = {}
    if record.before_id and after_id:
        old = history.get_snapshot(record.before_id)
        new = history.get_snapshot(after_id)
        if old and new:
            diff = diff_snapshots(old, new)

    commands = commands_in_window(record.hostname, record.started_at)

    # Closed only once the record above is assembled, for the same reason
    # the single-device path does it last: a failure in between would lose
    # the baseline id, which is the half that cannot be recovered.
    end(record.hostname)

    return {
        "change": record.as_dict(),
        "hostname": record.hostname,
        "old_id": record.before_id,
        "new_id": after_id,
        "diff": diff.get("diff", ""),
        "added": diff.get("added", 0),
        "removed": diff.get("removed", 0),
        "changed": diff.get("changed", 0),
        "days_since": round(record.age_seconds / 86400, 2),
        "window_seconds": round(record.age_seconds, 1),
        "commands": commands,
        "pending": None,
        "capture_error": capture_error or record.capture_error,
        "comparable": bool(record.before_id and after_id),
    }


def prune_stale() -> list[Change]:
    """
    Remove records older than the stale bound, returning what went.

    Called on startup. A change left open for a fortnight is not evidence
    of a fortnight-long change, it is evidence of somebody forgetting — and
    the diff it would produce spans everything that happened in between,
    which reads as one enormous change nobody made.
    """
    records = _load()
    dropped: list[Change] = []
    keep: dict[str, dict] = {}
    for key, raw in records.items():
        record = _as_change(raw)
        if record and record.stale:
            dropped.append(record)
        else:
            keep[key] = raw
    if dropped:
        _save(keep)
        logger.info("Dropped %d stale change record(s)", len(dropped))
    return dropped
