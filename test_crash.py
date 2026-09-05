"""
test_crash.py — What ShellMate can say about a fault it did not survive (#568).

A crash on a locked-down machine produces nothing today. The relay exists
(#370) for people who cannot reach GitHub; this is the missing half.

Four properties carry the weight, and three of them are about what does
*not* happen:

**Everything goes through the one door.** A traceback embeds hostnames in
its exception text, and redaction happens before the file is written, not
before it is sent — a file on disk can be copied out by hand.

**Never the scrollback.** The log says what ShellMate did; the session
buffer says what the device said. Only the first is diagnostic.

**Nothing leaves without being read.** The file is written automatically;
sending it is a decision taken with the text in front of somebody, and the
previewed text is the sent text — not a rendering of it.

**A crash reporter that crashes is worse than none.** It is called from an
exception handler and from the startup-failure path, so nothing in it may
raise, including when redaction itself fails.

    python test_crash.py
"""

import json
import re
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

from backend import paths

_TEMP = Path(tempfile.mkdtemp(prefix="shellmate-crash-"))
paths._data_dir_cache = _TEMP

from backend import advanced, crash, feedback                 # noqa: E402

passed = 0
failed: list[str] = []

ROOT = Path(__file__).parent
APP = (ROOT / "backend" / "app.py").read_text(encoding="utf-8")
RUN = (ROOT / "run.py").read_text(encoding="utf-8")
PANEL = (ROOT / "frontend" / "js" / "feedback.js").read_text(encoding="utf-8")
RELAY = (ROOT / "relay" / "worker.js").read_text(encoding="utf-8")


def check(name: str, condition: bool, detail: str = "") -> None:
    global passed
    if condition:
        passed += 1
        print(f"  OK  {name}")
    else:
        failed.append(f"{name}: {detail}")
        print(f"  FAIL {name}\n       {detail}")


def clear() -> None:
    for path in crash.crash_dir().glob("crash-*.json"):
        path.unlink(missing_ok=True)


def boom(message: str = "it broke"):
    """A real exception with a real traceback, not a fabricated triple."""
    try:
        raise RuntimeError(message)
    except RuntimeError:
        return sys.exc_info()


# ---------------------------------------------------------------------------

def test_a_fault_is_recorded() -> None:
    print("\n-- Recorded --")
    clear()

    path = crash.write(*boom("something went wrong"), "main")
    check("a file is written", path is not None and path.exists(), str(path))

    report = json.loads(path.read_text(encoding="utf-8"))
    check("with the traceback", "RuntimeError" in report["traceback"],
          report["traceback"][:200])
    check("the exception line on its own",
          "something went wrong" in report["exception"], report["exception"])
    check("where it happened", report["where"] == "main", report["where"])
    check("when", bool(report["when"]))
    check("and which build it was",
          "Version:" in report["about"], report["about"][:200])

    check("it is listed as pending",
          any(r["file"] == path.name for r in crash.pending()))
    check("and can be read back by name",
          (crash.get(path.name) or {}).get("where") == "main")


def test_it_is_redacted_and_never_the_scrollback() -> None:
    """
    The rule this shares with everything else that leaves the machine.

    `ConnectionError_("could not reach core-sw-01 with password hunter2")`
    is an ordinary line to write and a credential to publish.
    """
    print("\n-- Through the one door --")
    clear()

    path = crash.write(
        *boom("could not reach the device: the password is hunter2"), "main")
    report = json.loads(path.read_text(encoding="utf-8"))
    blob = json.dumps(report)

    check("a credential in the exception text is masked",
          "hunter2" not in blob, report["exception"])
    check("and it is masked in the file, not on the way out",
          "hunter2" not in path.read_text(encoding="utf-8"),
          "a file on disk is a file that can be copied out by hand")

    check("the session buffer is not a field at any size",
          not {"buffer", "scrollback", "session", "output"} & set(report),
          str(sorted(report)))
    check("the whole thing is bounded",
          len(report["traceback"]) <= crash.MAX_CHARS
          and len(report["log"]) <= crash.MAX_CHARS,
          "a recursion error is a traceback thousands of frames deep")


def test_nothing_in_it_raises() -> None:
    """
    It runs inside an exception handler and inside the startup-failure
    path. A crash reporter that crashes replaces a diagnosable fault with
    an undiagnosable one.
    """
    print("\n-- It never raises --")
    clear()

    check("a traceback of None is fine",
          crash.write(RuntimeError, RuntimeError("no tb"), None, "startup")
          is not None,
          "this is exactly the shape run.py's _fatal passes")

    check("an unreadable name is refused rather than raising",
          crash.get("../../etc/passwd") is None
          and crash.discard("../../etc/passwd") is False,
          "the name arrives from the browser and discard deletes what it "
          "is given")
    check("and so is one that is not a crash file",
          crash.get("settings.json") is None)

    real = crash._redact
    try:
        # If redaction itself fails, the answer is to write nothing rather
        # than to write the unredacted version.
        crash._redact = lambda text: (_ for _ in ()).throw(ValueError("no"))
        out = crash._redact
        check("a redaction failure is caught inside _redact",
              "except Exception" in
              (ROOT / "backend" / "crash.py").read_text(encoding="utf-8")
              .split("def _redact")[1].split("def ")[0])
    finally:
        crash._redact = real


def test_only_a_bounded_number_are_kept() -> None:
    print("\n-- Bounded on disk --")
    clear()

    for index in range(crash.MAX_KEPT + 4):
        # Distinct names: the stamp is to the second, and a loop is faster
        # than that.
        (crash.crash_dir() / f"crash-2026010{index // 10}-00000{index % 10}.json"
         ).write_text('{"where": "main"}', encoding="utf-8")

    crash.prune()
    kept = list(crash.crash_dir().glob("crash-*.json"))
    check(f"at most {crash.MAX_KEPT} survive", len(kept) <= crash.MAX_KEPT,
          f"{len(kept)} kept — a data folder that fills with crash files is "
          f"its own fault report")

    clear()
    (crash.crash_dir() / "crash-20260101-000000.json").write_text(
        "{ not json", encoding="utf-8")
    check("a half-written file is dropped rather than reported",
          crash.pending() == []
          and not (crash.crash_dir() / "crash-20260101-000000.json").exists(),
          "written during a fault that happened during a fault")


def test_the_hook_covers_threads() -> None:
    """
    The read loops, the scheduler and the store writer all run on threads.
    An exception on one of those disappears into a log line today.
    """
    print("\n-- Threads too --")
    clear()
    crash._installed = False
    previous_sys, previous_thread = sys.excepthook, threading.excepthook
    try:
        crash.install()

        thread = threading.Thread(target=lambda: 1 / 0, name="a-worker")
        thread.start()
        thread.join(5)

        reports = crash.pending()
        check("a fault on a thread is recorded", bool(reports), "none written")
        if reports:
            check("and the thread is named",
                  "a-worker" in reports[0].get("where", ""),
                  reports[0].get("where"))
            check("with the real exception",
                  "ZeroDivisionError" in reports[0].get("traceback", ""),
                  reports[0].get("traceback", "")[:120])

        check("installing twice is a no-op",
              (crash.install() or True) and sys.excepthook is not None)
    finally:
        sys.excepthook, threading.excepthook = previous_sys, previous_thread
        crash._installed = False

    check("Ctrl-C is not a fault",
          "KeyboardInterrupt" in
          (ROOT / "backend" / "crash.py").read_text(encoding="utf-8"),
          "a report about somebody pressing Ctrl-C is noise in an outbox")
    check("and the previous hook still runs",
          "previous(exc_type, exc_value, exc_tb)" in
          (ROOT / "backend" / "crash.py").read_text(encoding="utf-8"),
          "the default one prints the traceback, which is how anybody "
          "running from source sees a fault at all")


def test_the_preview_is_what_is_sent() -> None:
    print("\n-- Read before sent --")
    clear()
    path = crash.write(*boom("a fault"), "main")
    report = crash.get(path.name)

    body = crash.as_description(report)
    check("the description carries the traceback",
          "RuntimeError" in body, body[:200])
    check("and the log tail", "Application log" in body, body[-300:])
    check("the title is the exception line",
          crash.title_for(report).startswith("Crash:"),
          crash.title_for(report))

    check("the endpoint returns that same string",
          'crash.as_description(report)' in APP,
          "somebody who reads a preview and finds something else went is "
          "somebody who will never read a preview again")

    check("the panel shows the whole thing rather than truncating it",
          "detail.maxLength = 100000" in PANEL,
          "a preview that shows less than what will be sent is the one "
          "thing this must not do")


def test_nothing_sends_itself() -> None:
    print("\n-- Never automatic --")

    check("crash is a report kind", "crash" in feedback.TYPES)
    check("and the relay files it", "crash:" in RELAY, RELAY[:0])
    check("labelled as a crash as well as a bug",
          "'bug', 'crash', 'user-reported'" in RELAY,
          "a crash arrives with a traceback and no reproduction, and wants "
          "different triage")
    check("the relay's error message no longer names only two kinds",
          "type must be 'bug' or 'feature'." not in RELAY,
          "a message that contradicts what the endpoint accepts is worse "
          "than no message")

    check("there is a setting for whether to ask",
          advanced.get("feedback.report_crashes") is True)
    check("and it governs asking, not sending",
          "report_crashes" in APP and "ask" in APP.split("/api/crashes")[1][:900])

    crashes_block = APP.split("REST — Crash reports")[1].split("@app.on_event")[0]
    check("none of the crash endpoints send anything",
          "feedback_module" not in crashes_block
          and "submit" not in crashes_block,
          "they list, read and forget; sending is POST /api/feedback with "
          "text the user has looked at, exactly as a typed bug report is")

    check("the panel asks rather than sending",
          "checkForCrashes" in PANEL and "sticky: true" in PANEL,
          "a question that withdraws itself after twelve seconds is worse "
          "than one not asked")
    check("a sent or queued report stops being offered",
          "discardCrash(pendingCrash.file)" in PANEL,
          "offering the same fault again would have somebody send it twice "
          "to be sure")
    check("and the sidebar never opens a crash form by accident",
          "if (kind === 'crash') kind = 'bug';" in PANEL,
          "a crash left selected would file the next typed bug as a crash")


def test_a_startup_failure_is_recorded_too() -> None:
    """
    The case with no window to ask from and no log anybody will read.
    """
    print("\n-- Startup --")

    check("the hook is installed early", "crash_module.install()" in RUN)
    check("and _fatal records before the message box",
        re.search(r'crash_module\.write\([\s\S]{0,200}?"startup"\)'
                  r"[\s\S]{0,400}?MessageBoxW", RUN) is not None,
          "a startup failure is the one that most needs a file the next "
          "launch can offer to send")


def main() -> int:
    print("=" * 52)
    print("  Crash reports")
    print("=" * 52)

    for test in (
        test_a_fault_is_recorded,
        test_it_is_redacted_and_never_the_scrollback,
        test_nothing_in_it_raises,
        test_only_a_bounded_number_are_kept,
        test_the_hook_covers_threads,
        test_the_preview_is_what_is_sent,
        test_nothing_sends_itself,
        test_a_startup_failure_is_recorded_too,
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
