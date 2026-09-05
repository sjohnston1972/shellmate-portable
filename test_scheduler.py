"""
test_scheduler.py — Scheduled backups fire when they should and not otherwise.

The arithmetic is checked without a clock or a thread: what "daily at 02:00"
means from a given moment, that a schedule just switched on does not run
immediately, that weekly picks the right day. The run itself is exercised
with injected callables, so a device that fails, one with an open session
and one with no credentials each end up in the right column of the result.

    python test_scheduler.py
"""

import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from backend import paths

_TEMP = Path(tempfile.mkdtemp(prefix="shellmate-sched-"))
paths._data_dir_cache = _TEMP

from backend import scheduler                                             # noqa: E402

passed = 0
failed: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    global passed
    if condition:
        passed += 1
        print(f"  OK  {name}")
    else:
        failed.append(f"{name}: {detail}")
        print(f"  FAIL {name}\n       {detail}")


def ts(text: str) -> float:
    return datetime.fromisoformat(text).timestamp()


def test_when() -> None:
    print("\n-- When --")
    daily = {"enabled": True, "every": "daily", "at": "02:00"}
    check("normalise keeps a good schedule", scheduler.normalise(daily)["at"] == "02:00")
    check("a disabled schedule is None", scheduler.normalise({"enabled": False, "every": "daily"}) is None)
    check("a bad time falls back to 02:00", scheduler.normalise({"enabled": True, "at": "25:99"})["at"] == "02:00")
    check("a bad cadence falls back to daily", scheduler.normalise({"enabled": True, "every": "fortnightly"})["every"] == "daily")

    plan = scheduler.normalise(daily)
    nxt = scheduler.next_run(plan, datetime.fromisoformat("2026-09-02T15:00:00"))
    check("daily at 02:00 from 15:00 is tomorrow 02:00", nxt.isoformat() == "2026-09-03T02:00:00", nxt.isoformat())
    nxt = scheduler.next_run(plan, datetime.fromisoformat("2026-09-02T01:30:00"))
    check("  and from 01:30 is today 02:00", nxt.isoformat() == "2026-09-02T02:00:00", nxt.isoformat())

    weekly = scheduler.normalise({"enabled": True, "every": "weekly", "at": "03:00", "day": "sun"})
    nxt = scheduler.next_run(weekly, datetime.fromisoformat("2026-09-02T15:00:00"))   # a Wednesday
    check("weekly on Sunday picks the coming Sunday", nxt.isoformat() == "2026-09-06T03:00:00", nxt.isoformat())

    hourly = scheduler.normalise({"enabled": True, "every": "hourly"})
    nxt = scheduler.next_run(hourly, datetime.fromisoformat("2026-09-02T15:20:00"))
    check("hourly is the top of the next hour", nxt.isoformat() == "2026-09-02T16:00:00", nxt.isoformat())

    check("switched on at 15:00, it is not due at 15:01",
          not scheduler.is_due(daily, None, ts("2026-09-02T15:01:00")))
    check("last run yesterday 02:00, it is due at 02:00 today",
          scheduler.is_due(daily, ts("2026-09-01T02:00:00"), ts("2026-09-02T02:00:30")))
    check("  and not at 01:59", not scheduler.is_due(daily, ts("2026-09-01T02:00:00"), ts("2026-09-02T01:59:00")))
    check("a missed slot is still owed", scheduler.is_due(daily, ts("2026-08-30T02:00:00"), ts("2026-09-02T15:00:00")))
    check("armed at 15:00 today, it is due at 02:00 tomorrow",
          scheduler.is_due(daily, ts("2026-09-02T15:00:00"), ts("2026-09-03T02:00:05")))


def test_run() -> None:
    print("\n-- A run --")
    profiles = [
        {"id": "a", "name": "core-1", "connection_type": "ssh", "has_saved_credentials": True},
        {"id": "b", "name": "core-2", "connection_type": "ssh", "has_saved_credentials": True},
        {"id": "c", "name": "console", "connection_type": "serial"},
        {"id": "d", "name": "no-creds", "connection_type": "ssh", "has_saved_credentials": False},
        {"id": "e", "name": "open-now", "connection_type": "ssh", "has_saved_credentials": True},
    ]
    destroyed: list = []
    connected: list = []

    def open_session_for(profile):
        return {"session_id": "live", "profile_id": "e"} if profile["id"] == "e" else None

    def connect(profile):
        connected.append(profile["id"])
        if profile["id"] == "b":
            raise RuntimeError("Authentication failed")
        return {"session_id": "headless-" + profile["id"]}

    def capture(session):
        return {"stored": True}

    def destroy(session):
        destroyed.append(session["session_id"])

    result = scheduler.run_group("lab", profiles, connect, capture, open_session_for, destroy)
    check("the good one is captured", "core-1" in result["ok"], str(result))
    check("the one with an open session is captured through it and not logged into",
          "open-now" in result["ok"] and "e" not in connected)
    check("a failed login is reported with the reason",
          any(f["name"] == "core-2" and "Authentication" in f["why"] for f in result["failed"]), str(result["failed"]))
    check("serial and credential-less devices are skipped, not failed",
          {s["name"] for s in result["skipped"]} == {"console", "no-creds"}, str(result["skipped"]))
    check("headless sessions are closed afterwards", destroyed == ["headless-a"], str(destroyed))
    check("the live session is left alone", "live" not in destroyed)
    check("the record says when and how long", result["at"] > 0 and "took_s" in result)


def test_stored_on_the_group() -> None:
    print("\n-- On the group --")
    from backend import groups
    groups.update_group("lab", {"backup": {"enabled": True, "every": "weekly", "at": "3:5", "day": "friday"}})
    entry = next(g for g in groups.list_groups() if g["key"] == "lab")
    check("a schedule is stored normalised", entry["backup"] == {"enabled": True, "every": "weekly", "at": "03:05", "day": "fri"}, str(entry["backup"]))
    groups.update_group("lab", {"backup_last": {"at": 1.0, "ok": ["x"], "failed": [], "skipped": []}})
    entry = next(g for g in groups.list_groups() if g["key"] == "lab")
    check("the last result is listed with the group", entry["backup_last"]["ok"] == ["x"])
    groups.update_group("lab", {"backup": None})
    entry = next(g for g in groups.list_groups() if g["key"] == "lab")
    check("clearing the schedule removes it", entry["backup"] is None)


def main() -> int:
    print("=" * 52)
    print("  Scheduled backups")
    print("=" * 52)
    for test in (test_when, test_run, test_stored_on_the_group,
                 test_what_did_not_happen):
        try:
            test()
        except Exception as exc:
            failed.append(f"{test.__name__}: raised {type(exc).__name__}: {exc}")
            print(f"  FAIL {test.__name__} raised {type(exc).__name__}: {exc}")
    shutil.rmtree(_TEMP, ignore_errors=True)
    print("\n" + "=" * 52)
    print(f"  {passed} passed  |  {len(failed)} failed")
    print("=" * 52)
    if failed:
        print("\nFAILURES:")
        for item in failed:
            print(f"  {item}")
    return 1 if failed else 0


def test_what_did_not_happen() -> None:
    """
    A scheduler that reports only what ran (#612).

    "Last run Friday, 12 ok" is true after a weekend with ShellMate closed
    and says nothing about the two nights that were owed. A gap in a backup
    history looks exactly like a period in which nothing changed, which is
    the dangerous direction for this to fail in.
    """
    print("\n-- What was owed and did not happen --")
    from datetime import datetime

    # A pinned clock, not the wall one.
    #
    # This read `time.time()` and asserted that twelve hours on a nightly
    # schedule owes nothing — which is true between 14:00 and 02:00 and
    # false the rest of the day, because the 02:00 slot falls inside the
    # gap. It passed every time it was run until it was run at half two in
    # the morning. A test whose answer depends on when it is run is not
    # testing the thing it names.
    #
    # 10 March is deliberate as well: the UK clocks change at the end of
    # the month, and a nightly slot counted across that boundary is a
    # different question from this one.
    now = datetime(2026, 3, 10, 1, 0).timestamp()
    nightly = {"every": "daily", "at": "02:00", "enabled": True}

    check("nothing owed when the last run was recent",
          scheduler.missed_since(nightly, now - 12 * 3600, now) == [],
          "13:00 yesterday to 01:00 today crosses no 02:00 slot")

    weekend = scheduler.missed_since(nightly, now - 3 * 86400, now)
    check("a long weekend owes three slots", len(weekend) == 3, str(len(weekend)))
    check("and they are in order, oldest first",
          weekend == sorted(weekend), str(weekend))

    month = scheduler.missed_since(nightly, now - 30 * 86400, now)
    check("a month owes thirty", len(month) == 30, str(len(month)))

    # Bounded, or a year of downtime on an hourly schedule walks nine
    # thousand datetimes to produce a number nobody reads that precisely.
    hourly = scheduler.missed_since({"every": "hourly", "enabled": True},
                                    now - 400 * 86400, now)
    check("a year of downtime is counted, not enumerated forever",
          len(hourly) <= 200, str(len(hourly)))

    check("an unscheduled group owes nothing",
          scheduler.missed_since({"enabled": False}, now - 86400, now) == [])
    check("and neither does one that has never run",
          scheduler.missed_since(nightly, None, now) == [],
          "with no last run there is no gap to describe")


if __name__ == "__main__":
    sys.exit(main())
