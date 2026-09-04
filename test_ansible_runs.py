"""
test_ansible_runs.py — Runs, grouped by what started them (#591).

The area shows two records that are not the same thing: the runner's, which
survives a restart and includes runs started elsewhere, and ShellMate's own
note of what this browser watched, which is where the tallies come from.
They are shown separately on purpose — a run in one and not the other says
either "started somewhere else" or "the runner has forgotten it", and
merging would hide exactly that.

What is worth testing is the grouping, because it answers the first
question about a run nobody watched happen: who asked for it. A job carries
the pipeline that fired it, or nothing when a person did.

And lateness. A pipeline due at three that ran at three is unremarkable;
one that ran forty minutes late is the container having been asleep — which
is the failure this deployment is specifically prone to, and the thing a
bare timestamp hides.

Run: python test_ansible_runs.py
"""

import asyncio
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backend import paths  # noqa: E402

_TEMP = Path(tempfile.mkdtemp(prefix="shellmate-runs-"))
paths._data_dir_cache = _TEMP
assert paths.data_dir() == _TEMP, "test isolation failed — refusing to run"

import uvicorn  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402

from backend.app import app  # noqa: E402


def _free_port() -> int:
    """A port nothing else holds: several suites run side by side."""
    import socket as _socket
    with _socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


PORT = _free_port()
BASE = f"http://127.0.0.1:{PORT}"

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


def _serve() -> None:
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_config=None,
                log_level="error")


async def main() -> None:
    threading.Thread(target=_serve, daemon=True).start()
    time.sleep(2.0)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))

        # The runner is not reachable in a test, so its record is stubbed —
        # what is under test is how ShellMate presents jobs, not whether a
        # container answers.
        await page.route("**/api/ansible/jobs", lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps([
                {"id": "a1", "playbook": "drift-site-4.yml",
                 "status": "successful", "pipeline": "nightly-drift",
                 "scheduled_for": "2026-09-04T03:00:00+00:00",
                 "started": "2026-09-04T03:40:00+00:00"},
                {"id": "a2", "playbook": "drift-site-4.yml",
                 "status": "failed", "pipeline": "nightly-drift",
                 "scheduled_for": "2026-09-03T03:00:00+00:00",
                 "started": "2026-09-03T03:00:20+00:00"},
                {"id": "b1", "playbook": "ntp.yml", "status": "successful",
                 "pipeline": None, "scheduled_for": None,
                 "started": "2026-09-04T10:00:00+00:00"},
            ])))

        await page.goto(BASE)
        await page.wait_for_selector("#ansible-stage", state="attached", timeout=15000)
        await page.evaluate("window.ansibleView.open('runs')")
        await page.wait_for_selector("#ansible-jobs .av-table", timeout=10000)

        print("\n-- Two records, kept apart --")
        check("the runner's record has its own block",
              await page.query_selector("#ansible-jobs") is not None)
        check("and this browser's notes have theirs",
              await page.query_selector("#ansible-history") is not None,
              "merging them would hide a run being in one and not the other")

        print("\n-- Grouped by what started them --")
        groups = await page.eval_on_selector_all(
            "#ansible-jobs .av-runs-group", "e => e.map(x => x.textContent)")
        check("there is a group per pipeline plus one for manual runs",
              len(groups) == 2, str(groups))
        check("manual runs come first, and are named as such",
              "by hand" in groups[0].lower(), str(groups))
        check("the pipeline is named", "nightly-drift" in groups[1], str(groups))
        check("and each group is counted",
              "2" in groups[1] and "1" in groups[0], str(groups))

        print("\n-- Lateness, which a timestamp alone hides --")
        text = await page.inner_text("#ansible-jobs")
        check("a run that fired 40 minutes late says so",
              "40 min late" in text, text[:300])
        check("one that fired on time does not",
              text.count("late") == 1,
              "an on-time run should not be labelled late")
        check("and a manual run is never called late",
              "late" not in text.split("nightly-drift")[0], text[:200])

        print("\n-- Nothing threw --")
        real = [e for e in errors if "favicon" not in e.lower()]
        check("no script errors along the way", not real, "; ".join(real[:3]))
        await browser.close()

    print("\n" + "=" * 52)
    print(f"  {passed} passed  |  {len(failed)} failed")
    print("=" * 52)
    for line in failed:
        print("  -", line)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    asyncio.run(main())
