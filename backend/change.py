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
