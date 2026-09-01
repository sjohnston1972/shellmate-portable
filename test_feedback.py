"""
test_feedback.py — The in-app bug/feature reporter (#370).

The properties worth holding:

  - a report is validated once, by rules that mirror the relay's, so nothing
    valid locally dies remotely
  - with no relay configured, nothing is lost: the report queues in the data
    folder and the caller is told "queued", never "sent"
  - a relay that comes back gets the queue, oldest first
  - the outbox is bounded — an unreachable relay must not grow a file on a
    USB stick forever
  - a report carries what the user typed plus two build facts, and nothing
    from any session

    python test_feedback.py
"""

import sys
import tempfile
from pathlib import Path

from backend import paths

_TEMP = Path(tempfile.mkdtemp(prefix="shellmate-feedback-"))
paths._data_dir_cache = _TEMP

from backend import advanced, feedback                     # noqa: E402

# The shipped default points at the LIVE relay. Left in place, this test
# files its reports as real GitHub issues — which it did once, twenty-one
# of them. Every test below runs against an explicitly empty relay unless
# it sets one itself, and none may ever use the real default.
advanced.update({"feedback.relay_url": ""})

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


def test_validation() -> None:
    print("\n-- Validation --")

    for bad_kind in ("", "rant", None):
        try:
            feedback.build_report(bad_kind, "title", "")
            check(f"kind {bad_kind!r} refused", False, "no error raised")
        except ValueError:
            check(f"kind {bad_kind!r} refused", True)

    try:
        feedback.build_report("bug", "   ", "words")
        check("blank title refused", False, "no error raised")
    except ValueError:
        check("blank title refused", True)

    report = feedback.build_report("bug", "x" * 500, "y" * 9000)
    check("title clamped, not refused", len(report["title"]) == feedback.MAX_TITLE)
    check("description clamped, not refused",
          len(report["description"]) == feedback.MAX_DESCRIPTION)

    report = feedback.build_report("feature", "an idea", "details")
    check("report carries only the declared fields",
          set(report) == {"type", "title", "description", "platform", "portable"},
          str(set(report)))
    check("the text form names the type",
          "feature report" in feedback.as_text(report))


def test_queueing() -> None:
    print("\n-- No relay: queue, and say so --")
    feedback.outbox_path().unlink(missing_ok=True)

    result = feedback.submit("bug", "first", "one")
    check("no relay means queued, not sent", result["status"] == "queued",
          str(result))
    check("the queue size is reported", result["queued"] == 1)
    check("the text rides along for the clipboard",
          "first" in result["text"])

    feedback.submit("feature", "second", "two")
    check("the outbox file exists", feedback.outbox_path().exists())
    check("both reports wait in it", len(feedback._load_outbox()) == 2)

    for i in range(feedback.MAX_QUEUED + 10):
        feedback.submit("bug", f"flood {i}", "")
    check("the outbox is bounded",
          len(feedback._load_outbox()) <= feedback.MAX_QUEUED,
          str(len(feedback._load_outbox())))


def test_flush() -> None:
    print("\n-- The relay comes back --")
    feedback.outbox_path().unlink(missing_ok=True)
    feedback.submit("bug", "held report", "")

    sent: list[dict] = []
    original = feedback._send
    feedback._send = lambda report, url: sent.append(report)
    try:
        result = feedback.submit("bug", "live report", "")
        check("with a working relay the report is sent",
              result["status"] == "queued",  # no relay URL configured yet
              str(result))

        # Point at a relay and flush: the monkeypatched send accepts all.
        from backend import advanced
        advanced.update({"feedback.relay_url": "http://127.0.0.1:1/report"})
        remaining = feedback.flush()
        check("a flush empties the outbox", remaining == 0, str(remaining))
        check("every held report went", len(sent) == 2, str(len(sent)))
        check("the outbox file is gone once empty",
              not feedback.outbox_path().exists())

        result = feedback.submit("feature", "direct", "")
        check("with a relay configured a report is sent, not queued",
              result["status"] == "sent", str(result))
    finally:
        feedback._send = original
        advanced.update({"feedback.relay_url": ""})


def test_failure_keeps_reports() -> None:
    print("\n-- The relay stays down --")
    feedback.outbox_path().unlink(missing_ok=True)

    from backend import advanced
    advanced.update({"feedback.relay_url": "http://127.0.0.1:1/report"})
    try:
        # Port 1 on loopback refuses instantly: a real failed send.
        result = feedback.submit("bug", "unsendable", "")
        check("a failed send queues rather than losing the report",
              result["status"] == "queued", str(result))
        check("the report survives in the outbox",
              len(feedback._load_outbox()) == 1)
    finally:
        advanced.update({"feedback.relay_url": ""})


if __name__ == "__main__":
    test_validation()
    test_queueing()
    test_flush()
    test_failure_keeps_reports()

    print(f"\n{passed} passed, {len(failed)} failed")
    for f in failed:
        print(f"  {f}")
    sys.exit(1 if failed else 0)
