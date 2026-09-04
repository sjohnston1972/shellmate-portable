"""
test_history_range.py — The history search honours an explicit date range
(#575), in the browser and at the API.

Run: python test_history_range.py
"""

import asyncio
import os
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backend import paths  # noqa: E402

_TEMP = Path(tempfile.mkdtemp(prefix="shellmate-histrange-"))
paths._data_dir_cache = _TEMP
assert paths.data_dir() == _TEMP, "test isolation failed — refusing to run"

import uvicorn  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402

from backend.app import app  # noqa: E402
from backend.store import store  # noqa: E402
from backend.session.transcript import CommandRecord  # noqa: E402

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


def seed() -> tuple[datetime, datetime]:
    """Two commands: one three days ago, one today."""
    now = datetime.now()
    old_day = now - timedelta(days=3)
    store.start_session("s-old", {"hostname": "core-1", "connection_type": "ssh", "display_label": "core-1"})
    store.start_session("s-new", {"hostname": "core-1", "connection_type": "ssh", "display_label": "core-1"})
    store.add_command("s-old", CommandRecord(command="show version", output="ancient",
                                             prompt="core-1#", started_at=old_day.timestamp(), duration_ms=10))
    store.add_command("s-new", CommandRecord(command="show clock", output="recent",
                                             prompt="core-1#", started_at=now.timestamp(), duration_ms=10))
    return old_day, now


async def main_async(old_day: datetime, now: datetime) -> None:
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width": 1400, "height": 900})
        await page.goto(BASE, wait_until="networkidle")

        print("\n-- The API takes a since and an until --")
        both = await page.evaluate("async () => (await (await fetch('/api/history/search')).json()).count")
        check("both commands are recorded", both == 2, str(both))
        day = old_day.strftime("%Y-%m-%d")
        window = await page.evaluate("""async ({day}) => {
            const from = new Date(day + 'T00:00').getTime() / 1000;
            const to = from + 86400;
            const r = await fetch(`/api/history/search?since=${from}&until=${to}`);
            const d = await r.json();
            return d.results.map(x => x.command);
        }""", {"day": day})
        check("a one-day window finds only that day's command",
              window == ["show version"], str(window))

        print("\n-- The panel offers the range and applies it --")
        await page.evaluate("() => window.openHistory()")
        await page.wait_for_selector("#history-range", timeout=5000)
        options = await page.evaluate("() => [...document.querySelectorAll('#history-range option')].map(o => o.value)")
        check("'Between two dates' is offered", "between" in options, str(options))
        hidden_first = await page.evaluate("() => document.getElementById('history-dates').classList.contains('hidden')")
        check("the date fields are hidden until chosen", hidden_first)
        await page.select_option("#history-range", "between")
        await page.wait_for_timeout(300)
        shown = await page.evaluate("() => !document.getElementById('history-dates').classList.contains('hidden')")
        check("  and appear when it is", shown)
        await page.fill("#history-from", day)
        await page.fill("#history-to", day)
        await page.wait_for_timeout(800)
        text = await page.evaluate("() => document.getElementById('history-results').innerText")
        check("the results are that day's command only",
              "show version" in text and "show clock" not in text, text.replace("\n", " | ")[:200])
        await page.click("#history-dates-clear")
        await page.wait_for_timeout(800)
        text = await page.evaluate("() => document.getElementById('history-results').innerText")
        check("Clear brings everything back", "show clock" in text, text.replace("\n", " | ")[:200])
        await browser.close()


def main() -> int:
    print("=" * 52)
    print("  History date range")
    print("=" * 52)
    old_day, now = seed()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    time.sleep(3)
    try:
        asyncio.run(main_async(old_day, now))
    except Exception as exc:
        failed.append(f"harness: {exc!r}")
        print(f"  FAIL harness: {exc!r}")
    print("\n" + "=" * 52)
    print(f"  {passed} passed  |  {len(failed)} failed")
    print("=" * 52)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
