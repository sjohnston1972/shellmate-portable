"""
test_compliance_ui.py — The compliance table on screen (#543).

`test_compliance.py` proves the verdicts are right. This proves they are
*shown* as three different things, which is a separate claim: a state that
is correct in the JSON and painted like its neighbour has not been
communicated.

Two properties carry the weight:

**Never-captured looks like neither.** Painted green it reports a device
nobody has looked at as verified; painted amber it sends an engineer to
fix a device that may be perfectly configured. It gets a neutral treatment
and says in words that nothing is being claimed.

**The caveat is above the table.** A reader who finds the limit after the
verdicts has already drawn a conclusion from them.

Run: python test_compliance_ui.py
"""

import asyncio
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backend import paths  # noqa: E402

_TEMP = Path(tempfile.mkdtemp(prefix="shellmate-compliance-ui-"))
paths._data_dir_cache = _TEMP
assert paths.data_dir() == _TEMP, "test isolation failed — refusing to run"

import uvicorn  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402

from backend.app import app  # noqa: E402
from backend import compliance as compliance_module  # noqa: E402


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


REPORT = {
    "group": "glasgow", "at": time.time(), "took_s": 0.4, "checked": 5,
    "summary": "2 of 5 missing lines, 1 never captured.",
    "limits": compliance_module.LIMITS,
    "stale_after_days": compliance_module.STALE_AFTER_DAYS,
    "counts": {"missing": 2, "never-captured": 1, "no-snippet": 1,
               "compliant": 1},
    "devices": [
        {"name": "sw-a", "hostname": "sw-a", "platform": "cisco_ios",
         "state": "missing", "missing": ["aaa new-model", "ntp server 10.0.0.1"],
         "unexpected": [], "present": 2, "captured_at": time.time() - 3600,
         "age_days": 0.04, "stale": False},
        {"name": "sw-b", "hostname": "sw-b", "platform": "cisco_ios",
         "state": "missing", "missing": [], "present": 4,
         "unexpected": ["transport input telnet"],
         "captured_at": time.time() - 86400, "age_days": 1.0, "stale": False},
        {"name": "sw-c", "hostname": "sw-c", "platform": "cisco_ios",
         "state": "never-captured", "missing": [], "unexpected": [],
         "present": 0, "captured_at": None, "age_days": None, "stale": False},
        {"name": "fw-d", "hostname": "fw-d", "platform": "mikrotik_routeros",
         "state": "no-snippet", "missing": [], "unexpected": [], "present": 0,
         "captured_at": time.time() - 7200, "age_days": 0.08, "stale": False},
        {"name": "sw-e", "hostname": "sw-e", "platform": "cisco_ios",
         "state": "compliant", "missing": [], "unexpected": [], "present": 4,
         "captured_at": time.time() - 45 * 86400, "age_days": 45.0,
         "stale": True},
    ],
}


async def main() -> None:
    threading.Thread(target=_serve, daemon=True).start()
    time.sleep(2.0)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))

        await page.goto(BASE)
        await page.wait_for_selector("#compliance-overlay", state="attached",
                                     timeout=15000)
        await page.evaluate(
            "r => window.shellmateCompliance.show({name: 'Glasgow', key: 'glasgow'}, r)",
            REPORT)
        await page.wait_for_timeout(200)

        text = await page.inner_text("#compliance-panel")

        print("\n-- The summary and the caveat --")
        check("the summary leads with what to act on",
              "2 of 5 missing lines" in text, text[:200])
        check("the caveat is on screen at all",
              "compared as a set" in text, text[:400])
        # Position matters: a limit found after the verdicts is a limit read
        # after a conclusion has already been drawn.
        order = await page.evaluate(
            "() => {"
            " const l = document.getElementById('compliance-limits');"
            " const b = document.getElementById('compliance-body');"
            " return l.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING ? 'before' : 'after';"
            "}")
        check("and it sits above the table, not under it",
              order == "before", f"limits are {order} the table")

        print("\n-- Three states, three treatments --")
        for state in ("missing", "never-captured", "compliant", "no-snippet"):
            found = await page.query_selector(f".compliance-{state}")
            check(f"a {state} row is rendered with its own class",
                  found is not None)

        # The one that must not look like either of its neighbours.
        colours = await page.evaluate(
            "() => ['missing','compliant','never-captured'].map(s => {"
            "  const el = document.querySelector('.compliance-' + s);"
            "  return el ? getComputedStyle(el).borderLeftColor : '';"
            "})")
        check("never-captured is painted like neither verdict",
              colours[2] != colours[0] and colours[2] != colours[1],
              f"missing={colours[0]} compliant={colours[1]} never={colours[2]}")
        check("and says in words that nothing is claimed",
              "Nothing is claimed about this device" in text, text[:900])

        print("\n-- What is missing, and what should not be there --")
        check("missing lines are listed",
              "aaa new-model" in text and "ntp server 10.0.0.1" in text)
        # Case-insensitively: the label is uppercased by CSS, and
        # `inner_text` returns what is rendered rather than what is in the
        # DOM — a text-transform is invisible to the author and not to this.
        check("unexpected lines are listed separately",
              "transport input telnet" in text
              and "should not be there" in text.lower(), text[:900])
        check("a device with the whole block but a forbidden line is not "
              "shown as compliant",
              await page.eval_on_selector_all(
                  ".compliance-compliant .compliance-name",
                  "e => e.map(x => x.textContent)") == ["sw-e"])

        print("\n-- The age of the evidence --")
        check("a fresh capture says so", "captured today" in text, text[:900])
        check("a stale one is flagged on the verdict itself",
              "this verdict is that old too" in text, text[:1200])
        check("and never-captured says there is no capture",
              "no capture stored" in text, text[:900])

        print("\n-- Open and fix --")
        buttons = await page.eval_on_selector_all(
            ".compliance-fix", "e => e.length")
        check("offered only where there are lines to add",
              buttons == 1,
              "sw-a has missing lines; the others have nothing to push")

        print("\n-- A platform with no block --")
        check("it says it was not checked rather than giving a verdict",
              "was not checked" in text, text[:1400])

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
