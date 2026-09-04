"""
test_pipeline.py — What reaches the device when a block is pasted (#523).

A paste is the one gesture where somebody hands ShellMate sixty lines and
walks away from the keyboard, so the rules it obeys are the rules that matter
most, and each of them fails silently when it breaks.

**One line per prompt.** In prompt mode a line goes out only when the device
is idle at a bare prompt *and* has said something since the last line. The
second half is the subtle one: `idle_at_prompt` is recomputed only when output
arrives, so for the moment after a line is sent it still describes the prompt
before it. A batch that believed it would fire the whole block into a device
that had answered none of it — which is the accident this file exists to
prevent.

**A stall is a result, not an error.** A device that stops answering must not
have the rest of the block fired at it thirty seconds later, into whatever the
user has started doing; and "line 12 sent, no prompt seen" is the one thing
they need afterwards, so it is carried in the summary rather than inferred.

**A question pauses the clock.** A line held at the guardrail waits on a
person, and a person is slower than any device timeout.

    python test_pipeline.py
"""

import shutil
import sys
import tempfile
from pathlib import Path

from backend import paths

_TEMP = Path(tempfile.mkdtemp(prefix="shellmate-pipeline-"))
paths._data_dir_cache = _TEMP

from backend.pipeline import (MIN_LINE_DELAY, OutboundPipeline,  # noqa: E402
                              PasteBatch)

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


def _batch(**kwargs) -> PasteBatch:
    """A batch whose clock starts at zero, so the tests can drive it."""
    batch = PasteBatch(**kwargs)
    batch.hold(now=0.0)
    return batch


def test_one_line_per_prompt() -> None:
    print("\n-- One line per prompt --")
    batch = _batch(lines=["interface Gi0/1", "description uplink", "no shutdown"])

    check("nothing goes out while the device is busy",
          batch.next_line(at_prompt=False, now=0.1) is None)

    check("the first line goes at the prompt",
          batch.next_line(at_prompt=True, now=0.2) == "interface Gi0/1")

    check("and the second does not follow it immediately",
          batch.next_line(at_prompt=True, now=0.3) is None,
          "the batch raced its own output")

    batch.observe("interface Gi0/1\r\nsw1(config-if)#")
    check("  until the device has answered",
          batch.next_line(at_prompt=True, now=0.4) == "description uplink")

    check("nothing is still counted as sent twice",
          batch.sent == 2, str(batch.summary()))


def test_a_device_that_stops_answering() -> None:
    print("\n-- A device that stops answering --")
    batch = _batch(lines=["a", "b", "c"], timeout=5.0)

    check("the first line goes", batch.next_line(at_prompt=True, now=0.0) == "a")
    check("and nothing follows while it is quiet",
          batch.next_line(at_prompt=False, now=3.0) is None)
    check("  and the batch is still running",
          not batch.done, "gave up before its timeout")

    check("past the timeout it stops",
          batch.next_line(at_prompt=False, now=6.0) is None)
    check("  and says so", batch.done and batch.summary()["reason"] == "no-prompt",
          str(batch.summary()))
    check("  naming the line that was never answered",
          batch.summary()["stalled_at"] == 1, str(batch.summary()))
    check("  and how much never went",
          batch.summary()["remaining"] == 2, str(batch.summary()))

    check("and it stays stopped rather than resuming when the device returns",
          batch.next_line(at_prompt=True, now=30.0) is None,
          "the rest of the block was fired at a device 30s later")


def test_timed_mode_paces() -> None:
    print("\n-- Lines a fixed distance apart --")
    batch = _batch(lines=["a", "b"], wait_for_prompt=False, delay=0.5)

    check("nothing before the delay is up",
          batch.next_line(at_prompt=False, now=0.2) is None)
    check("then the first line, prompt or no prompt",
          batch.next_line(at_prompt=False, now=0.6) == "a",
          "timed mode is for devices with no prompt worth waiting for")
    check("and the second only after another delay",
          batch.next_line(at_prompt=False, now=0.7) is None)
    check("  which it then sends",
          batch.next_line(at_prompt=False, now=1.2) == "b")


def test_a_zero_delay_is_still_a_delay() -> None:
    print("\n-- A delay of zero --")
    batch = _batch(lines=["a", "b"], wait_for_prompt=False, delay=0.0)
    batch.next_line(at_prompt=False, now=1.0)
    check("zero is floored, so the block does not arrive at once",
          batch.next_line(at_prompt=False, now=1.0 + MIN_LINE_DELAY / 2) is None,
          "pacing with a zero delay is no pacing at all")


def test_a_question_pauses_the_clock() -> None:
    print("\n-- A held line pauses rather than skips --")
    batch = _batch(lines=["reload", "b"], timeout=5.0)
    check("the line goes to the pipeline", batch.next_line(True, now=0.0) == "reload")

    # The guardrail held it. The caller holds the clock each pass while
    # somebody is being asked, which is what stops the batch timing out
    # behind its own confirmation dialog.
    for tick in (1.0, 3.0, 6.0, 9.0, 20.0):
        batch.hold(now=tick)
        batch.next_line(at_prompt=False, now=tick)
    check("twenty seconds of being asked is not a stall",
          not batch.done, str(batch.summary()))

    # Answered "no": nothing reached the device, so there is nothing to wait
    # for and the next line may go.
    batch.resume(now=20.0)
    check("and the next line follows the answer",
          batch.next_line(at_prompt=True, now=20.1) == "b")


def test_typing_stops_it() -> None:
    print("\n-- A keystroke stops the batch --")
    batch = _batch(lines=["a", "b", "c"])
    batch.next_line(at_prompt=True, now=0.0)
    batch.abort("you started typing")

    check("it stops at once", batch.done)
    check("  nothing else is sent",
          batch.next_line(at_prompt=True, now=1.0) is None)
    summary = batch.summary()
    check("  and it says where it got to and why",
          summary["sent"] == 1 and summary["remaining"] == 2
          and summary["reason"] == "you started typing", str(summary))


def test_the_summary_is_honest() -> None:
    print("\n-- The summary --")
    batch = _batch(lines=["a", "b"])
    batch.next_line(at_prompt=True, now=0.0)
    batch.observe("sw1#")
    batch.next_line(at_prompt=True, now=1.0)
    batch.observe("sw1#")
    check("a finished batch reports nothing outstanding",
          batch.next_line(at_prompt=True, now=2.0) is None and batch.done)
    check("  with no reason and no stall",
          batch.summary() == {"sent": 2, "total": 2, "remaining": 0,
                              "stalled_at": 0, "reason": ""},
          str(batch.summary()))


def test_an_empty_paste_does_nothing() -> None:
    print("\n-- An empty paste --")
    batch = _batch(lines=[])
    check("nothing to send", batch.next_line(at_prompt=True, now=0.0) is None)
    check("  and it finishes at once", batch.done)


def test_every_line_still_goes_through_the_pipeline() -> None:
    print("\n-- The guardrail applies to a pasted line --")
    # The batch decides *when*; the pipeline decides *what*. A `reload` in a
    # pasted block has to be held exactly as a typed one is, which is why the
    # read loop sends every line through here rather than to the socket.
    pipeline = OutboundPipeline()
    pipeline.platform = "ios"
    batch = _batch(lines=["reload"])
    line = batch.next_line(at_prompt=True, now=0.0)
    pipeline.process(line + "\r")
    check("a pasted reload is held, not sent",
          pipeline.held_commands == ["reload"], str(pipeline.held_commands))
    check("  and the confirmation names it",
          pipeline.newly_held == ["reload"], str(pipeline.newly_held))


def main() -> int:
    print("=" * 52)
    print("  Pasting a block, a line at a time")
    print("=" * 52)
    for test in (
        test_one_line_per_prompt,
        test_a_device_that_stops_answering,
        test_timed_mode_paces,
        test_a_zero_delay_is_still_a_delay,
        test_a_question_pauses_the_clock,
        test_typing_stops_it,
        test_the_summary_is_honest,
        test_an_empty_paste_does_nothing,
        test_every_line_still_goes_through_the_pipeline,
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
