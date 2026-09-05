"""
test_backup_digest.py — What the overnight backups found, said where somebody reads it (#539).

Scheduled backups were built and then reported into a log file. Their most
valuable output — "somebody changed core-2 overnight", "Glasgow has failed
three nights running", "nothing ran at all because the laptop was shut" —
went to a place nobody looks.

Two rules decide what this is worth, and both are about restraint:

- **Only what is worth saying.** A clean run where nothing changed is the
  normal night. A digest that announces it every morning is one people
  learn to dismiss unread — and then the morning something did happen
  looks exactly like all the others. Silence is a feature here, and it is
  asserted as one.
- **A run that did not happen counts.** #612 records those. A gap in a
  backup history looks exactly like a period in which nothing changed,
  which is the dangerous direction for this to fail in, so a missed run is
  its own category rather than folded into failures: "it failed" and "it
  never ran" send somebody to two different places.

The `changed` list is the other half. `run_group` had the answer already —
whether the capture differed from the last one stored — and threw it away
into a log line.

Run: python test_backup_digest.py
"""

import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backend import paths  # noqa: E402

_TEMP = Path(tempfile.mkdtemp(prefix="shellmate-digest-"))
paths._data_dir_cache = _TEMP
assert paths.data_dir() == _TEMP, "test isolation failed — refusing to run"

from backend import groups as groups_module  # noqa: E402
from backend import scheduler  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                        # pragma: no cover
    pass

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


def _record(key: str, **fields) -> None:
    last = {"at": time.time(), "ok": [], "changed": [], "failed": [],
            "skipped": [], "group": key}
    last.update(fields)
    groups_module.update_group(key, {"backup_last": last})


def a_run_records_what_changed() -> None:
    """
    `run_group` kept whether each capture differed, instead of logging it.

    Driven through the real function with injected callables, because the
    thing being asserted is that the `stored` flag survives the loop — and
    a test that built the result dict itself would assert nothing at all.
    """
    print("\n-- A run records which devices changed --")

    profiles = [
        {"name": "core-1", "connection_type": "ssh", "has_saved_credentials": True},
        {"name": "core-2", "connection_type": "ssh", "has_saved_credentials": True},
        {"name": "edge-1", "connection_type": "ssh", "has_saved_credentials": True},
        {"name": "console-3", "connection_type": "serial"},
        {"name": "no-creds", "connection_type": "ssh"},
    ]

    def capture(session):
        name = session["name"]
        if name == "edge-1":
            raise RuntimeError("authentication failed")
        return {"stored": name == "core-2"}

    result = scheduler.run_group(
        "glasgow", profiles,
        connect=lambda p: {"name": p["name"]},
        capture=capture,
        open_session_for=lambda p: None,
        destroy=lambda s: None)

    check("the devices that changed are named",
          result["changed"] == ["core-2"], str(result["changed"]))
    check("a device that was captured and did not change is not listed",
          "core-1" in result["ok"] and "core-1" not in result["changed"],
          str(result))
    check("a failure is still a failure, not a change",
          [f["name"] for f in result["failed"]] == ["edge-1"], str(result["failed"]))
    check("and what could not be attempted is skipped with a reason",
          {s["name"] for s in result["skipped"]} == {"console-3", "no-creds"}
          and all(s["why"] for s in result["skipped"]), str(result["skipped"]))


def nothing_to_report_says_nothing() -> None:
    print("\n-- Nothing to report says nothing at all --")

    check("with no groups at all there is nothing",
          scheduler.digest()["anything"] is False, str(scheduler.digest()))
    check("and no sentence to show",
          scheduler.digest_line(scheduler.digest()) == "",
          "a toast with no text in it is still a toast")

    groups_module.create_group("Quiet site")
    check("a group that has never run says nothing",
          scheduler.digest()["anything"] is False, str(scheduler.digest()))

    _record("quiet-site", ok=["sw-1", "sw-2"])
    check("a clean run where nothing changed says nothing",
          scheduler.digest()["anything"] is False,
          "a report that fires every morning is one nobody reads, and then "
          "the morning it matters looks like all the others")

    # Skipped devices alone are not news either: a serial console has no
    # address to back up and never will, so saying so nightly is a
    # standing complaint about a decision already made.
    _record("quiet-site", ok=["sw-1"], skipped=[{"name": "con-1", "why": "not SSH"}])
    check("devices that were never going to be backed up are not news",
          scheduler.digest()["anything"] is False, str(scheduler.digest()))


def what_it_does_report() -> None:
    print("\n-- What it does report --")

    groups_module.create_group("Glasgow")
    _record("glasgow", ok=["core-1", "core-2"], changed=["core-2"],
            failed=[{"name": "edge-1", "why": "authentication failed"}])

    report = scheduler.digest()
    check("a change is reported", report["changed"] == 1, str(report))
    check("and a failure with it", report["failed"] == 1, str(report))
    check("the group is named", report["groups"][0]["name"] == "Glasgow",
          str(report["groups"][0]))
    check("with the device that changed",
          report["groups"][0]["changed"] == ["core-2"], str(report["groups"][0]))
    check("and why the failure failed",
          "authentication" in report["groups"][0]["failed"][0]["why"],
          str(report["groups"][0]["failed"]))

    line = scheduler.digest_line(report)
    check("the sentence names the group and the counts",
          "Glasgow" in line and "1 changed" in line and "1 failed" in line, line)


def runs_that_did_not_happen() -> None:
    """
    A gap in a backup history looks exactly like a quiet period.

    Which is why a missed run is its own category rather than a failure:
    "it failed" sends somebody to the device, "it never ran" sends them to
    the machine ShellMate runs on, and reporting the second as the first
    wastes the morning.
    """
    print("\n-- Runs that did not happen --")

    groups_module.create_group("Weekend site")
    _record("weekend-site", ok=["sw-9"], missed=3,
            missed_from=time.time() - 3 * 86400, missed_to=time.time() - 86400)

    report = scheduler.digest()
    entry = next(g for g in report["groups"] if g["group"] == "weekend-site")
    check("a missed run is reported even when the run that did happen was clean",
          report["missed"] == 3, str(report["missed"]))
    check("and is not counted as a failure",
          entry["failed"] == [] and entry["missed"] == 3, str(entry))
    check("the sentence says so in its own words",
          "missed" in scheduler.digest_line(report),
          scheduler.digest_line(report))
    check("and pluralises honestly",
          "1 run missed" in scheduler.digest_line(
              {"anything": True, "changed": 0, "failed": 0, "missed": 1,
               "groups": [{"name": "x"}]}),
          "three runs missed and one run missed are different sentences")


def once_it_is_read_it_stops_asking() -> None:
    print("\n-- Once it is read, it stops asking --")

    check("there is something before it is read",
          scheduler.digest()["anything"] is True)
    scheduler.mark_seen()
    check("and nothing after", scheduler.digest()["anything"] is False,
          "a digest that keeps reporting what has been read is a digest "
          "people turn off")
    check("but it can still be asked for deliberately",
          scheduler.digest(include_seen=True)["anything"] is True,
          "the panel somebody opens on purpose is a different question "
          "from the toast that interrupts them")

    # Tonight's run is new again, even though the marker moved.
    time.sleep(0.01)
    _record("glasgow", ok=["core-1"], changed=["core-1"])
    check("a later run reports again",
          scheduler.digest()["anything"] is True,
          "the marker is a timestamp rather than a flag precisely so that "
          "'seen' survives the next night turning up more")


if __name__ == "__main__":
    a_run_records_what_changed()
    nothing_to_report_says_nothing()
    what_it_does_report()
    runs_that_did_not_happen()
    once_it_is_read_it_stops_asking()

    print("\n" + "=" * 52)
    print(f"  {passed} passed  |  {len(failed)} failed")
    print("=" * 52)
    for line in failed:
        print("  -", line)
    sys.exit(1 if failed else 0)
