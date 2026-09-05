"""
test_broadcast_collect.py — Collecting a broadcast's replies (#529).

Sending to forty devices is the easy half. The half that goes wrong quietly
is deciding what happened afterwards, and four ways of getting it wrong are
each checked here because each produces a plausible-looking answer:

**Folding the states.** ``timeout``, ``not-captured`` and ``gone`` send an
operator to three different places — wait longer, look at prompt detection
for that platform, reconnect the tab. A combined "failed" sends them to
none of them, and "collected 38 of 40" with no reason for the other two is
the report this whole feature exists to replace.

**Ignoring ``after``.** `show version` run twice a day apart matches the
same record. Without the guard the collector cheerfully returns yesterday's
output as this broadcast's answer, and the diff that follows is then a
comparison between two moments rather than two devices.

**An unbounded wait.** One switch mid-`reload` must not hold the other
thirty-nine open, and the bound has to be visible per device — "the
broadcast timed out" names nothing to go and look at.

**Unredacted output.** These results go into a diff, a chat message and
quite possibly a ticket. `show run` carries hashes and community strings.

And the summary line, which is the one sentence anybody actually reads: it
has to lead with the devices that differ, and it must not quietly count a
device that never answered as one that agreed.

    python test_broadcast_collect.py
"""

import asyncio
import shutil
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

from backend import paths

_TEMP = Path(tempfile.mkdtemp(prefix="shellmate-collect-"))
paths._data_dir_cache = _TEMP

from backend import broadcast_collect as bc                # noqa: E402

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


# ---------------------------------------------------------------------------
# Fakes. No devices, no sockets — a session is a dict and a record is anything
# with .command, .output and .started_at, which is all the collector reads.
# ---------------------------------------------------------------------------

VERSION = ("Cisco IOS Software, C2960X Software, Version 15.2(7)E3\n"
           "uptime is 41 weeks, 2 days\n")


def record(command: str, output: str, started_at: float | None = None):
    return SimpleNamespace(command=command, output=output,
                           started_at=time.time() if started_at is None else started_at,
                           prompt="sw#", duration_ms=12)


def session(label: str, records=(), connected: bool = True) -> dict:
    return {
        "display_label": label,
        "hostname": label,
        "recent_records": list(records),
        "handler": SimpleNamespace(is_connected=connected),
    }


def run(coro):
    return asyncio.run(coro)


def by_label(result: dict) -> dict:
    return {r["label"]: r for r in result["results"]}


def collected(label: str, output: str) -> dict:
    """A result row of the shape compare() consumes."""
    return {"session_id": f"id-{label}", "label": label, "state": "collected",
            "output": output, "ran_at": 1.0, "waited_s": 0.1, "detail": ""}


def missing(label: str, state: str) -> dict:
    return {"session_id": f"id-{label}", "label": label, "state": state,
            "output": "", "ran_at": 0.0, "waited_s": 45.0, "detail": ""}


# ---------------------------------------------------------------------------

def test_the_four_states() -> None:
    """The whole reason this is not ok/failed."""
    print("\n-- Collected, timeout, not-captured, gone --")

    now = time.time()
    sessions = {
        # Answered.
        "s1": session("sw-01", [record("show version", VERSION, now + 1)]),
        # Still talking: nothing has closed since the broadcast went out.
        "s2": session("sw-02", [record("show run", "hostname sw-02", now - 600)]),
        # Back at a prompt, but nothing the parser produced matches.
        "s3": session("sw-03", [record("terminal length 0", "", now + 1)]),
        # The tab is gone.
        "s4": None,
    }

    out = run(bc.collect(sessions, "show version", after=now,
                         timeout=0.3, poll=0.05))
    rows = by_label(out)

    check("the device that answered is collected",
          rows["sw-01"]["state"] == "collected", str(rows["sw-01"]))
    check("and carries its output",
          "Version 15.2(7)E3" in rows["sw-01"]["output"], rows["sw-01"]["output"])

    check("the silent device is a timeout",
          rows["sw-02"]["state"] == "timeout", str(rows["sw-02"]))
    check("and says how long it was waited for",
          rows["sw-02"]["waited_s"] > 0, str(rows["sw-02"]["waited_s"]))

    check("the device that answered something else is not-captured",
          rows["sw-03"]["state"] == "not-captured",
          "a device back at a prompt whose record did not match is a "
          "prompt-detection problem, not a slow device — reporting it as a "
          "timeout sends somebody to wait for output that already arrived")

    check("the closed tab is gone", rows["s4"]["state"] == "gone",
          str(rows["s4"]))
    check("and is not confused with a timeout",
          rows["s4"]["state"] != "timeout")

    check("every state is one of the four",
          all(r["state"] in bc.STATES for r in out["results"]),
          str([r["state"] for r in out["results"]]))
    check("and each unanswered one says why",
          all(r["detail"] for r in out["results"] if r["state"] != "collected"),
          str([(r["label"], r["detail"]) for r in out["results"]]))


def test_a_dropped_session_is_gone_not_a_wait() -> None:
    """A session whose transport is down settles at once."""
    print("\n-- Disconnected --")

    now = time.time()
    sessions = {"s1": session("sw-09", [], connected=False)}
    started = time.monotonic()
    out = run(bc.collect(sessions, "show version", after=now,
                         timeout=5.0, poll=0.05))
    elapsed = time.monotonic() - started

    check("a disconnected session is gone",
          out["results"][0]["state"] == "gone", str(out["results"][0]))
    check("and does not spend the timeout finding out",
          elapsed < 1.0, f"{elapsed:.2f}s")


def test_an_answer_that_arrives_late_is_still_collected() -> None:
    """The records are re-read on every poll, not sampled once."""
    print("\n-- The reply arrives during the wait --")

    now = time.time()
    live = session("sw-05", [])
    sessions = {"s1": live}

    async def scenario():
        async def answer_later():
            await asyncio.sleep(0.15)
            live["recent_records"].append(record("show version", VERSION))
        task = asyncio.ensure_future(answer_later())
        result = await bc.collect(sessions, "show version", after=now,
                                  timeout=3.0, poll=0.05)
        await task
        return result

    out = run(scenario())
    check("a record that closes mid-wait is picked up",
          out["results"][0]["state"] == "collected", str(out["results"][0]))
    check("and the collection stops as soon as it has everything",
          out["waited_s"] < 1.5, str(out["waited_s"]))


def test_the_after_guard() -> None:
    """Yesterday's run of the same command is not today's answer."""
    print("\n-- after --")

    now = time.time()
    yesterday = record("show version", "Version 15.0(2)SE", now - 86400)
    sessions = {"s1": session("sw-07", [yesterday])}

    out = run(bc.collect(sessions, "show version", after=now,
                         timeout=0.2, poll=0.05))
    check("a record from before the broadcast is not its reply",
          out["results"][0]["state"] != "collected",
          "matching it returns output from before the command was even sent, "
          "and the diff that follows compares two moments, not two devices")
    check("and the output is not carried over",
          out["results"][0]["output"] == "", out["results"][0]["output"])

    # The same session, asked without a moment to be after.
    out = run(bc.collect(sessions, "show version", after=0.0,
                         timeout=0.2, poll=0.05))
    check("with no `after` the newest matching record is taken",
          out["results"][0]["state"] == "collected", str(out["results"][0]))

    # A record stamped a fraction before the caller's moment is this run's:
    # started_at is when the echo was parsed, not when the caller decided.
    nearly = record("show version", VERSION, now - 0.4)
    sessions = {"s1": session("sw-08", [nearly])}
    out = run(bc.collect(sessions, "show version", after=now,
                         timeout=0.2, poll=0.05))
    check("a record inside the slack window still counts",
          out["results"][0]["state"] == "collected",
          "a stricter guard rejects the collection's own answer, because the "
          "two clocks are only as close as the send took")


def test_the_command_is_matched_without_being_guessed_at() -> None:
    print("\n-- Which record is the reply --")

    now = time.time()
    sessions = {
        # The pipeline expanded the alias before sending; the device recorded
        # the expansion.
        "s1": session("sw-11", [record("show version", VERSION, now + 1)]),
        # Same command inside a longer one. Not the reply.
        "s2": session("sw-12", [record("do show version | include Version",
                                       "Version 15.2", now + 1)]),
    }
    out = run(bc.collect(sessions, "sh ver", after=now, timeout=0.2, poll=0.05))
    rows = by_label(out)

    check("an abbreviation matches its expansion",
          rows["sw-11"]["state"] == "collected", str(rows["sw-11"]))
    check("but a command that merely contains it does not",
          rows["sw-12"]["state"] == "not-captured",
          "a substring test files one command's output under another's name, "
          "and the diff then compares two different commands")


def test_output_is_redacted() -> None:
    print("\n-- Redaction --")

    now = time.time()
    secret = ("hostname sw-13\n"
              "snmp-server community s3cr3t RO\n"
              "username admin password 7 070C285F4D06\n")
    sessions = {"s1": session("sw-13", [record("show run", secret, now + 1)])}

    out = run(bc.collect(sessions, "show run", after=now, timeout=0.2, poll=0.05))
    text = out["results"][0]["output"]

    check("the community string does not leave", "s3cr3t" not in text, text)
    check("nor does the password hash", "070C285F4D06" not in text, text)
    check("while the rest of the configuration survives",
          "hostname sw-13" in text, text)


def test_the_bound_actually_bounds() -> None:
    """One device mid-reload must not hold the other thirty-nine open."""
    print("\n-- Bounded --")

    now = time.time()
    sessions = {f"s{i}": session(f"sw-{i:02d}", []) for i in range(40)}
    sessions["s0"] = session("sw-00", [record("show version", VERSION, now + 1)])

    started = time.monotonic()
    out = run(bc.collect(sessions, "show version", after=now,
                         timeout=0.4, poll=0.1))
    elapsed = time.monotonic() - started

    check("the wait ends at the bound", elapsed < 2.0, f"{elapsed:.2f}s")
    check("and reports it", out["waited_s"] >= 0.4, str(out["waited_s"]))
    check("the one that answered is not lost with the rest",
          by_label(out)["sw-00"]["state"] == "collected")
    check("the rest each carry their own wait, not a global verdict",
          all(r["waited_s"] >= 0.4
              for r in out["results"] if r["state"] != "collected"),
          "'the broadcast timed out' names nothing to go and look at")
    check("and every device is accounted for",
          len(out["results"]) == 40, str(len(out["results"])))


def test_one_bad_session_does_not_lose_the_others() -> None:
    print("\n-- Nothing raises --")

    now = time.time()

    class Exploding:
        @property
        def is_connected(self):
            raise RuntimeError("transport gone")

    broken = session("sw-bad", [])
    broken["recent_records"] = None
    broken["handler"] = Exploding()

    sessions = {
        "s1": session("sw-14", [record("show version", VERSION, now + 1)]),
        "s2": broken,
        "s3": "not a session dict at all",
    }

    out = run(bc.collect(sessions, "show version", after=now,
                         timeout=0.2, poll=0.05))
    rows = by_label(out)

    check("the good device still answers",
          rows["sw-14"]["state"] == "collected", str(rows["sw-14"]))
    check("the malformed one is reported, not raised",
          rows["sw-bad"]["state"] in bc.STATES, str(rows["sw-bad"]))
    check("and so is the thing that is not a session at all",
          len(out["results"]) == 3, str(out["results"]))


def test_compare_splits_identical_from_differing() -> None:
    print("\n-- The comparison --")

    base = "Version 15.2(7)E3\nuptime is 41 weeks\n"
    same = "Version 15.2(7)E3\nuptime is 41 weeks   \n\n"      # padding only
    other = "Version 15.2(4)E1\nuptime is 3 weeks\n"

    report = bc.compare([collected("sw-01", base), collected("sw-02", same),
                         collected("sw-14", other)])

    check("the first collected device is the baseline",
          report["baseline"] == "sw-01", report["baseline"])
    check("the baseline counts among the identical, so the numbers reconcile",
          report["identical"] == ["sw-01", "sw-02"], str(report["identical"]))
    check("trailing whitespace is not a difference",
          "sw-02" not in [d["label"] for d in report["differing"]])
    check("the differing device is named",
          [d["label"] for d in report["differing"]] == ["sw-14"],
          str(report["differing"]))

    diff = report["differing"][0]
    check("with a unified diff", "---" in diff["diff"] and "+++" in diff["diff"],
          diff["diff"])
    check("naming both sides",
          "sw-01" in diff["diff"] and "sw-14" in diff["diff"], diff["diff"])
    check("and counting what changed",
          (diff["added"], diff["removed"]) == (2, 2),
          f'+{diff["added"]} -{diff["removed"]}')

    check("nothing is uncollected here", report["uncollected"] == [])


def test_compare_never_claims_anything_about_the_uncollected() -> None:
    print("\n-- Devices that did not answer --")

    report = bc.compare([collected("sw-01", "a\n"), collected("sw-02", "a\n"),
                         missing("sw-30", "timeout"), missing("sw-31", "gone")])

    check("they are named separately",
          report["uncollected"] == ["sw-30", "sw-31"], str(report["uncollected"]))
    check("and never counted as agreement",
          "sw-30" not in report["identical"], str(report["identical"]))
    check("nor as a difference",
          "sw-30" not in [d["label"] for d in report["differing"]])
    check("the summary says so plainly",
          "2 not collected: sw-30, sw-31" in report["summary"],
          report["summary"])


def test_compare_with_nothing_collected() -> None:
    print("\n-- Nothing came back --")

    report = bc.compare([missing("sw-01", "timeout"),
                         missing("sw-02", "not-captured"),
                         missing("sw-03", "gone")])

    check("there is no baseline", report["baseline"] == "", report["baseline"])
    check("nothing is claimed identical", report["identical"] == [])
    check("nothing is claimed differing", report["differing"] == [])
    check("all three are named", report["uncollected"] ==
          ["sw-01", "sw-02", "sw-03"], str(report["uncollected"]))
    check("and the summary says nothing was collected",
          "Nothing collected from 3 devices" in report["summary"],
          report["summary"])
    check("without asserting they agreed",
          "identical" not in report["summary"], report["summary"])

    empty = bc.compare([])
    check("an empty set is not a pass either",
          empty["summary"] == "Nothing to compare.", empty["summary"])


def test_the_summary_leads_with_what_to_act_on() -> None:
    print("\n-- One sentence --")

    rows = [collected(f"sw-{i:02d}", "same\n") for i in range(1, 39)]
    rows += [collected("sw-14", "different\n"), collected("sw-22", "other\n")]
    line = bc.compare(rows)["summary"]

    check("it leads with the devices that differ",
          line.startswith("2 differ: sw-14, sw-22"), line)
    check("and still gives the count that agreed",
          "38 identical" in line, line)
    check("without leading with the good news",
          not line.startswith("38"),
          "a summary that opens '38 identical' gets skimmed past, and the two "
          "names after the comma are the entire reason it was run")

    allsame = bc.compare([collected(f"sw-{i}", "same\n") for i in range(12)])
    check("a clean run says so plainly",
          allsame["summary"] == "All 12 identical.", allsame["summary"])

    lonely = bc.compare([collected("sw-01", "x\n"), missing("sw-02", "gone")])
    check("one device alone is not 'all identical'",
          "nothing to compare it with" in lonely["summary"], lonely["summary"])

    many = bc.compare([collected("sw-00", "base\n")] +
                      [collected(f"sw-{i:02d}", f"{i}\n") for i in range(1, 20)])
    check("a long list of names is cut rather than printed whole",
          "more" in many["summary"] and len(many["summary"]) < 160,
          many["summary"])


def main() -> int:
    print("=" * 52)
    print("  Broadcast — collecting and comparing the replies")
    print("=" * 52)

    for test in (
        test_the_four_states,
        test_a_dropped_session_is_gone_not_a_wait,
        test_an_answer_that_arrives_late_is_still_collected,
        test_the_after_guard,
        test_the_command_is_matched_without_being_guessed_at,
        test_output_is_redacted,
        test_the_bound_actually_bounds,
        test_one_bad_session_does_not_lose_the_others,
        test_compare_splits_identical_from_differing,
        test_compare_never_claims_anything_about_the_uncollected,
        test_compare_with_nothing_collected,
        test_the_summary_leads_with_what_to_act_on,
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
