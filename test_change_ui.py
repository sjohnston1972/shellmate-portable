"""
test_change_ui.py — The change record on screen (#544).

The record is drawn by the diff window rather than a panel of its own,
because a change record is mostly a diff and there is already a window
that renders one. What this checks is the block drift.js grows for the
parts a change has and a drift report does not.

One of those parts carries the whole weight. **A change that could not be
measured must not read as a change that did nothing.** "0 lines added, 0
removed" is what a diff renders for both — for two identical captures, and
for a device that went away before the second one — and those are opposite
facts. A device reloading mid-change is the ordinary case, frequently the
change itself, so this is not a corner: it is a normal Tuesday.

Run: python test_change_ui.py
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

_TEMP = Path(tempfile.mkdtemp(prefix="shellmate-change-ui-"))
paths._data_dir_cache = _TEMP
assert paths.data_dir() == _TEMP, "test isolation failed — refusing to run"

import uvicorn  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402

from backend.app import app  # noqa: E402


def _free_port() -> int:
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


DIFF = ("--- core-sw-01\n+++ core-sw-01\n@@ -1,3 +1,3 @@\n"
        " interface Gi1/0/1\n-description old uplink\n+description new uplink\n")

MEASURED = {
    "change": {
        "id": "abc123", "hostname": "core-sw-01", "started_at": time.time() - 900,
        "note": "Replacing the uplink SFP", "ticket": "NET-1042",
        "operator": "steven", "before_id": 7, "capture_error": "", "label": "",
    },
    "hostname": "core-sw-01", "old_id": 7, "new_id": 8,
    "diff": DIFF, "added": 1, "removed": 1, "changed": 2,
    "days_since": 0.01, "window_seconds": 900,
    "commands": [
        {"command": "conf t", "ran_at": time.time() - 800, "session_id": "s"},
        {"command": "interface Gi1/0/1", "ran_at": time.time() - 790, "session_id": "s"},
        {"command": "description new uplink", "ran_at": time.time() - 780, "session_id": "s"},
    ],
    "pending": None, "capture_error": "", "comparable": True,
}

# The device answered at the start and not at the end: it reloaded. A diff
# renders this exactly as it renders "nothing changed".
UNMEASURED = {
    "change": {
        "id": "def456", "hostname": "edge-rtr-02", "started_at": time.time() - 300,
        "note": "Firmware upgrade", "ticket": "", "operator": "steven",
        "before_id": 11, "capture_error": "", "label": "",
    },
    "hostname": "edge-rtr-02", "old_id": 11, "new_id": None,
    "diff": "", "added": 0, "removed": 0, "changed": 0,
    "days_since": 0.003, "window_seconds": 300,
    "commands": [{"command": "reload", "ran_at": time.time() - 250, "session_id": "s"}],
    "pending": {"kind": "reload", "seconds_left": 540, "source": "typed"},
    "capture_error": "The session is no longer connected.",
    "comparable": False,
}

IDENTICAL = dict(MEASURED, comparable=True, changed=0, added=0, removed=0,
                 diff="", commands=[])


async def show(page, record):
    await page.evaluate(
        "record => window.shellmateChange.show(record, "
        "{sessionId: 's1', label: record.hostname})", record)
    await page.wait_for_timeout(150)


async def main() -> None:
    threading.Thread(target=_serve, daemon=True).start()
    time.sleep(2.0)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))

        await page.goto(BASE)
        await page.wait_for_selector("#diff-overlay", state="attached", timeout=15000)

        print("\n-- A change that was measured --")
        await show(page, MEASURED)
        text = await page.inner_text("#diff-panel")

        check("the window is titled as a change record, not as drift",
              "change record" in text.lower(), text[:200])
        check("the note is on it", "Replacing the uplink SFP" in text)
        check("so is the ticket", "NET-1042" in text)
        check("and the operator", "steven" in text)
        check("the window length is in minutes, not rounded to a day",
              "15 minutes" in text, text[:400])
        check("the diff is rendered", "new uplink" in text)
        check("the commands are counted on the summary",
              "3 commands typed" in text, text[:600])
        check("no incomparable warning on a measured change",
              await page.query_selector("#diff-change .change-incomparable") is None)

        print("\n-- Identical before and after --")
        await show(page, IDENTICAL)
        text = await page.inner_text("#diff-panel")
        check("it says so in as many words",
              "identical before and after" in text.lower(), text[:300])
        check("and does not claim it could not be measured",
              await page.query_selector("#diff-change .change-incomparable") is None)
        check("no commands is stated rather than left blank",
              "No commands were recorded" in text, text[:600])

        print("\n-- A change that could NOT be measured --")
        await show(page, UNMEASURED)
        text = await page.inner_text("#diff-panel")

        check("the warning is shown",
              await page.query_selector("#diff-change .change-incomparable") is not None,
              "this renders identically to 'nothing changed' without it")
        check("the summary refuses to claim anything about the configuration",
              "could not be measured" in text.lower(), text[:400])
        check("it does not say zero lines changed",
              "0 lines added" not in text,
              "a diff of nothing and a diff that could not be taken are "
              "opposite facts")
        check("the reason the capture failed is carried",
              "no longer connected" in text, text[:500])
        check("and the pending reload is named",
              "reload" in text.lower() and "outstanding" in text.lower(),
              text[:500])
        check("the baseline is still reported as evidence",
              "Firmware upgrade" in text)

        print("\n-- An ordinary drift view is untouched --")
        await page.evaluate(
            "() => window.showConfigDiff("
            "{hostname: 'sw9', diff: '', changed: 0, added: 0, removed: 0,"
            " days_since: 3}, {display_label: 'sw9'})")
        await page.wait_for_timeout(150)
        hidden = await page.eval_on_selector(
            "#diff-change", "e => e.classList.contains('hidden')")
        check("the change block is hidden for a drift report", hidden is True)
        text = await page.inner_text("#diff-panel")
        check("and it is titled as configuration history",
              "configuration history" in text.lower(), text[:200])

        print("\n-- Nothing threw --")
        real = [e for e in errors if "favicon" not in e.lower()]
        check("no script errors along the way", not real, "; ".join(real[:3]))
        await browser.close()

    print("\n" + "=" * 52)
    print(f"  {passed} passed  |  {len(failed)} failed")
    print("=" * 52)
    for line in failed:
        print("  -", line)


if __name__ == "__main__":
    asyncio.run(main())
    sys.exit(1 if failed else 0)
