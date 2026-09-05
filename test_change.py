"""
test_change.py — A piece of work, bracketed (#544).

One property matters more than the rest, and it is the issue's own stated
risk: **a change must outlive the session it started in.** Reloading is
frequently the change being made, and a reload is exactly what destroys a
session. A record keyed on the session id would evaporate at the moment it
became most valuable — the engineer would have spent the window and have
nothing to show a change board for it. So the tests here start a change,
throw the session away, and end it anyway.

Two more that are less obvious and fail quietly:

**One change per device.** Two overlapping windows on one switch produce
two diffs of the same lines with no way to say which change owned which.

**A record survives ShellMate restarting.** If it did not, closing the
application during a maintenance window would silently discard the evidence
for it, which is worse than never offering the feature.

    python test_change.py
"""

import shutil
import sys
import tempfile
import time
from pathlib import Path

from backend import paths

_TEMP = Path(tempfile.mkdtemp(prefix="shellmate-change-"))
paths._data_dir_cache = _TEMP

from backend import change                                 # noqa: E402

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


def reset() -> None:
    path = change._file()
    if path.exists():
        path.unlink()


# ---------------------------------------------------------------------------

def test_opening_and_closing() -> None:
    print("\n-- A window --")
    reset()

    check("nothing is open to begin with",
          change.active("core-sw-01") is None)

    opened = change.start("core-sw-01", note="Replacing the uplink SFP",
                          ticket="NET-1042", operator="steven",
                          label="core-sw-01", before_id=7)
    check("starting one returns the record", opened.id != "")
    check("it carries the note", opened.note == "Replacing the uplink SFP")
    check("and the ticket", opened.ticket == "NET-1042")
    check("and the baseline snapshot", opened.before_id == 7)
    check("and is not stale the moment it opens", opened.stale is False)

    live = change.active("core-sw-01")
    check("it is findable while open", live is not None and live.id == opened.id)

    closed = change.end("core-sw-01")
    check("ending it returns the record that was open",
          closed is not None and closed.id == opened.id,
          "the caller needs the baseline id to build the diff, and re-reading "
          "it after the delete would race with itself")
    check("and nothing is open afterwards",
          change.active("core-sw-01") is None)
    check("ending again returns None rather than raising",
          change.end("core-sw-01") is None)


def test_it_outlives_the_session() -> None:
    """
    The whole reason the record is not on the session dict.

    A reload is frequently the change. Nothing here holds a session at all,
    which is the point — the module has no way to know one existed.
    """
    print("\n-- Surviving the session --")
    reset()

    change.start("edge-rtr-02", note="Firmware upgrade", before_id=11)

    # The session is gone: a reload, a dropped SSH connection, ShellMate
    # restarted. Nothing is handed to the module to stand in for it.
    reloaded = change.active("edge-rtr-02")
    check("the change is still there with no session in sight",
          reloaded is not None and reloaded.note == "Firmware upgrade")
    check("and its baseline survived", reloaded.before_id == 11)

    ended = change.end("edge-rtr-02")
    check("it can be ended from a different session entirely",
          ended is not None and ended.before_id == 11)


def test_it_survives_a_restart() -> None:
    """The file is the record; the module holds nothing in memory."""
    print("\n-- Surviving a restart --")
    reset()

    change.start("dist-sw-03", note="ACL change", before_id=21)

    # What a restart actually is, from this module's point of view: nothing
    # cached, everything re-read. Reaching into a module private on purpose
    # — the alternative is a test that passes because of a warm cache.
    import importlib

    importlib.reload(change)
    paths._data_dir_cache = _TEMP

    after = change.active("dist-sw-03")
    check("the change is still open after a reload of the module",
          after is not None and after.note == "ACL change",
          "closing ShellMate mid-window would discard the evidence for it")
    change.end("dist-sw-03")


def test_one_change_per_device() -> None:
    print("\n-- Two at once --")
    reset()

    change.start("core-sw-01", note="First", operator="steven")

    refused = ""
    try:
        change.start("core-sw-01", note="Second")
    except ValueError as exc:
        refused = str(exc)

    check("a second change on the same device is refused", bool(refused))
    check("and the refusal says what is already open",
          "First" in refused and "core-sw-01" in refused, refused)
    check("the first one is untouched",
          (change.active("core-sw-01") or change.Change("", "", 0)).note == "First")

    # A different device is a different window, which is the ordinary case
    # during a maintenance evening.
    other = change.start("edge-fw-02", note="Elsewhere")
    check("a change on another device is fine", other.note == "Elsewhere")
    check("and both are listed", len(change.open_changes()) == 2,
          str([c.hostname for c in change.open_changes()]))

    change.end("core-sw-01")
    change.end("edge-fw-02")


def test_the_hostname_is_matched_the_way_devices_actually_answer() -> None:
    """
    A device answering `Core-SW-01#` and `core-sw-01#` is one device.

    Prompt case is not stable across logins — it follows the configured
    hostname, which people change — and a change findable only under the
    spelling it was opened with is a change that gets lost.
    """
    print("\n-- One device, two spellings --")
    reset()

    change.start("Core-SW-01", note="Cased")
    check("it is found under a different case",
          change.active("core-sw-01") is not None)
    check("and under surrounding whitespace",
          change.active("  CORE-sw-01  ") is not None)
    check("the record keeps the spelling it was given",
          change.active("core-sw-01").hostname == "Core-SW-01",
          "matching loosely is not the same as rewriting what the device said")

    check("a second one under the other spelling is still refused",
          _raises(lambda: change.start("core-sw-01", note="Second")))
    change.end("CORE-SW-01")
    check("and ending under a third spelling worked",
          change.active("Core-SW-01") is None)


def test_a_device_that_would_not_be_captured() -> None:
    """
    No baseline is a reason to say so, not a reason to refuse the window.

    A device that will not give up its configuration is exactly the one
    somebody most wants a record of working on, and "we could not capture
    the before" is a fact the record should carry rather than a reason to
    have no record.
    """
    print("\n-- No baseline --")
    reset()

    opened = change.start("odd-box-09", note="Poking at it",
                          capture_error="The device did not answer show run.")
    check("the window opens anyway", opened.before_id is None)
    check("and says why there is no baseline",
          "did not answer" in opened.capture_error, opened.capture_error)
    change.end("odd-box-09")


def test_abandoning_is_not_ending() -> None:
    print("\n-- Abandon --")
    reset()

    change.start("wrong-box-01", note="Opened on the wrong device")
    check("abandoning it reports that there was one",
          change.abandon("wrong-box-01") is True)
    check("and it is gone", change.active("wrong-box-01") is None)
    check("abandoning nothing says so", change.abandon("wrong-box-01") is False)


def test_stale_records() -> None:
    print("\n-- Left open --")
    reset()

    fresh = change.start("fresh-01", note="Today")
    check("a new change is not stale", fresh.stale is False)

    # Reach into the file rather than waiting a week.
    records = change._load()
    records["old-01"] = {
        "id": "deadbeef", "hostname": "old-01",
        "started_at": time.time() - change.STALE_AFTER_SECONDS - 60,
        "before_id": 3, "note": "Forgotten", "ticket": "", "operator": "",
        "capture_error": "", "label": "",
    }
    change._save(records)

    old = change.active("old-01")
    check("an old one reports itself stale", old is not None and old.stale)

    dropped = change.prune_stale()
    check("pruning removes it", [c.hostname for c in dropped] == ["old-01"],
          str([c.hostname for c in dropped]))
    check("and leaves the fresh one alone",
          change.active("fresh-01") is not None,
          "pruning by age must not take a change somebody is inside")
    change.end("fresh-01")


def test_a_file_edited_by_hand() -> None:
    """
    settings.json is a text file people are told to edit, and so is this.

    A record that will not parse must not take the others with it — the
    whole point of the file is that a maintenance window's evidence is in
    it, and losing three because a fourth is malformed is the worst
    outcome available.
    """
    print("\n-- A file somebody edited --")
    reset()

    change.start("good-01", note="Fine")
    records = change._load()
    records["broken-01"] = {"id": "x", "started_at": "not a number"}
    records["also-broken"] = "not even a dict"
    change._save(records)

    check("the good record still reads",
          change.active("good-01") is not None)
    check("the broken ones are skipped rather than raising",
          all(c.hostname != "broken-01" for c in change.open_changes()),
          str([c.hostname for c in change.open_changes()]))
    check("and listing does not raise", isinstance(change.open_changes(), list))


def test_no_hostname() -> None:
    print("\n-- Nothing to be about --")
    reset()
    check("a change with no device is refused",
          _raises(lambda: change.start("", note="About what?")))
    check("and so is one that is only whitespace",
          _raises(lambda: change.start("   ")))


def _raises(fn) -> bool:
    try:
        fn()
    except ValueError:
        return True
    return False


def main() -> int:
    print("=" * 52)
    print("  Change records — bracketing a piece of work")
    print("=" * 52)

    for test in (
        test_opening_and_closing,
        test_it_outlives_the_session,
        test_it_survives_a_restart,
        test_one_change_per_device,
        test_the_hostname_is_matched_the_way_devices_actually_answer,
        test_a_device_that_would_not_be_captured,
        test_abandoning_is_not_ending,
        test_stale_records,
        test_a_file_edited_by_hand,
        test_no_hostname,
    ):
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


if __name__ == "__main__":
    sys.exit(main())
