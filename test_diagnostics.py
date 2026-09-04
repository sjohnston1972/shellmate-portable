"""
test_diagnostics.py — The self-checks say whether this install is healthy (#562).

The checks exist because every failure they report was already being computed
somewhere and then only logged. What has to hold for them to be worth having:

- **None of them raises.** A check that throws takes the whole panel with it,
  and the panel is what somebody opens when things are already going wrong. A
  deliberately broken check is planted here and the run must survive it.
- **The network probes stay off unless asked.** ShellMate promises to work
  air-gapped, and a Settings panel that reaches out on open spends that
  promise on the user's behalf.
- **The two silent degradations are reported as such.** A fallback data folder
  and a browser-fallback window frame are the two things a copy on a stick
  does quietly, and they arrive later as "my settings vanished" and "it opened
  in Chrome".

    python test_diagnostics.py
"""

import shutil
import sys
import tempfile
from pathlib import Path

from backend import paths

_TEMP = Path(tempfile.mkdtemp(prefix="shellmate-diag-"))
paths._data_dir_cache = _TEMP

from backend import diagnostics, support                     # noqa: E402

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


STATUSES = {diagnostics.OK, diagnostics.WARN, diagnostics.FAIL}


def test_shape() -> None:
    print("\n-- Every check answers in the same shape --")
    ids = [c.id for c in diagnostics.CHECKS]
    check("the ids are unique", len(ids) == len(set(ids)), str(ids))
    check("every check has a label", all(c.label for c in diagnostics.CHECKS))

    rows = diagnostics.run()
    check("a row per check that ran",
          len(rows) == len([c for c in diagnostics.CHECKS if not c.probe]),
          f"{len(rows)} rows")
    for row in rows:
        check(f"{row['id']}: has id, label, status, detail and fix",
              set(row) == {"id", "label", "status", "detail", "fix"}, str(row))
        check(f"{row['id']}: the status is one of the three",
              row["status"] in STATUSES, row["status"])
        check(f"{row['id']}: it says something", bool(row["detail"]), str(row))


def test_probes_are_opt_in() -> None:
    print("\n-- The air-gapped promise --")
    quiet = {row["id"] for row in diagnostics.run()}
    for probe in (c.id for c in diagnostics.CHECKS if c.probe):
        check(f"{probe} does not run unless it is asked for", probe not in quiet)
    check("there are network probes to ask for",
          any(c.probe for c in diagnostics.CHECKS))


def test_a_broken_check_does_not_break_the_run() -> None:
    print("\n-- A check that throws --")

    def explode() -> tuple[str, str, str]:
        raise RuntimeError("the disk caught fire")

    original = diagnostics.CHECKS
    diagnostics.CHECKS = original + (
        diagnostics.Check("boom", "Deliberately broken", explode),)
    try:
        rows = diagnostics.run()
    finally:
        diagnostics.CHECKS = original

    row = next((r for r in rows if r["id"] == "boom"), None)
    check("the run survives it", row is not None)
    check("and reports it rather than swallowing it",
          bool(row) and "could not run" in row["detail"] and "disk caught fire" in row["detail"],
          str(row))
    check("the other checks still ran", len(rows) > 1)


def test_the_two_silent_degradations() -> None:
    print("\n-- The two things a copy on a stick does quietly --")

    # A fallback data folder: settings, history and the vault stopped
    # travelling with the executable and nothing said so.
    was = paths._data_dir_is_fallback
    paths._data_dir_is_fallback = True
    try:
        status, detail, fix = diagnostics._data_folder()
    finally:
        paths._data_dir_is_fallback = was
    check("a fallback data folder is a warning, not silence",
          status == diagnostics.WARN, f"{status}: {detail}")
    check("  and says what to do about it", "writable" in fix.lower(), fix)

    # The window-frame ladder: the rung that won is now recorded.
    from backend import desktop

    check("the frame in use is reported", isinstance(desktop.frame_in_use(), str))
    original = desktop._frame
    try:
        desktop._frame = "default browser"
        status, detail, fix = diagnostics._frame()
        check("a browser fallback is a warning", status == diagnostics.WARN, detail)
        check("  and names WebView2 as the fix", "WebView2" in fix, fix)
        desktop._frame = "native window"
        status, _, _ = diagnostics._frame()
        check("a native window is fine", status == diagnostics.OK)
        desktop._frame = "not started"
        status, detail, _ = diagnostics._frame()
        check("and no window at all is not a fault — it is how a server runs",
              status == diagnostics.OK, detail)
    finally:
        desktop._frame = original


def test_summary_and_text() -> None:
    print("\n-- What it adds up to --")
    check("all clear says so",
          diagnostics.summarise([{"status": "ok"}, {"status": "ok"}]) == "All 2 checks passed.")
    check("a warning is not called a problem",
          "Nothing broken" in diagnostics.summarise([{"status": "ok"}, {"status": "warn"}]))
    check("a failure is",
          "1 problem found" in diagnostics.summarise([{"status": "fail"}]))

    text = diagnostics.as_text()
    for name in (c.label for c in diagnostics.CHECKS if not c.probe):
        check(f"the text form covers {name}", name in text, text[:200])
    check("the text form does not probe the network",
          not any(c.label in text for c in diagnostics.CHECKS if c.probe),
          "a bundle is often assembled on the machine with no internet")


def test_the_bundle_gets_them_free() -> None:
    print("\n-- And the support bundle --")
    check("there is a checks section", "checks" in support.SECTIONS_BY_ID)
    section = support.SECTIONS_BY_ID.get("checks")
    check("it is on by default — it describes ShellMate, not the estate",
          bool(section) and section.default_on and not section.device_data)
    collected = support.collect(["checks"])
    check("it collects without a session manager", "checks" in collected)
    check("and it is the same report", "self-checks" in collected["checks"].lower(),
          collected.get("checks", "")[:120])


def main() -> int:
    print("=" * 52)
    print("  Diagnostics self-checks")
    print("=" * 52)
    for test in (
        test_shape,
        test_probes_are_opt_in,
        test_a_broken_check_does_not_break_the_run,
        test_the_two_silent_degradations,
        test_summary_and_text,
        test_the_bundle_gets_them_free,
    ):
        try:
            test()
        except Exception as exc:
            failed.append(f"{test.__name__}: raised {type(exc).__name__}: {exc}")
            print(f"  FAIL {test.__name__} raised {type(exc).__name__}: {exc}")

    try:
        from backend.store import store
        store.close()
    except Exception:
        pass
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
