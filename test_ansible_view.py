"""
test_ansible_view.py — The Ansible view opens, navigates, and says what it can't do (#586).

Ansible outgrew a side panel, so it became a third layer in the pane beside
the dashboard and the terminals. Three things about that are worth a test,
because all three fail silently:

- **The stage exists and the sidebar reaches it.** The link previously
  opened a drawer, and `ansible_view.js` replaces that handler at load.
  Get the ordering wrong and clicking the link opens the old panel, which
  looks like nothing happened at all.
- **Every area registers.** Each is its own script; one that throws leaves
  a nav tab that switches to a blank rectangle. Nothing in the browser
  reports it.
- **An unreachable runner still paints the library half.** No container is
  running in a test, which is exactly the state a first-time user is in.
  If the screen blanks because the runner did not answer, the feature looks
  broken before it has been configured.

Run: python test_ansible_view.py
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

_TEMP = Path(tempfile.mkdtemp(prefix="shellmate-ansview-"))
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

#: Every area the nav offers. A tab with no script behind it switches to a
#: blank rectangle and reports nothing, so the list is asserted, not implied.
AREAS = ["dashboard", "playbooks", "runs", "builder", "templates",
         "inventory", "environments", "deployments", "keys", "repositories"]


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

        await page.goto(BASE)
        await page.wait_for_selector("#ansible-stage", state="attached", timeout=15000)

        print("\n-- The stage --")
        check("the stage starts hidden",
              await page.is_hidden("#ansible-stage"),
              "the view was showing before anything asked for it")

        check("the view registered its own API",
              await page.evaluate("!!window.ansibleView"),
              "ansible_view.js did not load")

        registered = await page.evaluate(
            "Object.keys(window.ansibleView ? window.ansibleView.state : {})")
        check("the shared cache exists", "overview" in registered,
              f"state held {registered}")

        print("\n-- Opening it --")
        await page.click("#sidebar-link-ansible")
        await page.wait_for_selector("#ansible-stage:not([hidden])", timeout=5000)
        check("the sidebar link opens the view", True)

        check("the old side panel stayed shut",
              await page.is_hidden("#ansible-overlay"),
              "clicking the link opened the drawer as well as the view")

        # The terminals must still be there behind it: a device mid-reload
        # has to survive somebody looking at a playbook.
        check("the terminals are still in the document",
              await page.evaluate(
                  "!!document.getElementById('terminals-container')"),
              "the view replaced the terminals rather than covering them")

        print("\n-- Every area --")
        for area in AREAS:
            tab = f'#av-nav .av-tab[data-av-go="{area}"]'
            check(f"{area} has a nav tab",
                  await page.query_selector(tab) is not None,
                  "no tab in the nav")
            await page.click(tab)
            await page.wait_for_timeout(120)
            shown = await page.evaluate("window.ansibleView.current")
            visible = await page.is_visible(f'#av-{area}')
            body_text = (await page.inner_text(f"#av-{area}-body")).strip()
            check(f"{area} shows when its tab is clicked",
                  shown == area and visible,
                  f"current was {shown!r}, visible {visible}")
            check(f"{area} renders something",
                  len(body_text) > 0,
                  "the area is on screen but empty, which reads as a load failure")

        registered_areas = await page.evaluate(
            "Array.from(document.querySelectorAll('#av-body .av-area'))"
            ".map(s => s.dataset.avArea)")
        check("the nav and the sections agree",
              sorted(registered_areas) == sorted(AREAS),
              f"sections were {registered_areas}")

        print("\n-- With no runner configured --")
        await page.click('#av-nav .av-tab[data-av-go="dashboard"]')
        await page.wait_for_timeout(600)

        pill = (await page.inner_text("#av-runner-pill")).strip()
        check("the runner pill says it is not set up",
              pill.lower() in ("not set up", "unreachable"),
              f"the pill read {pill!r}")

        dash = await page.inner_text("#av-dashboard-body")
        check("the dashboard says what to do about it",
              "Settings" in dash or "runner" in dash.lower(),
              f"it said: {dash[:120]!r}")

        check("the library half still painted",
              "Templates" in dash and "Playbooks" in dash,
              "an unreachable runner blanked the counts, which are local")

        print("\n-- Leaving --")
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(200)
        check("Escape leaves the view",
              await page.is_hidden("#ansible-stage"),
              "the view stayed open")

        await page.click("#sidebar-link-ansible")
        await page.wait_for_timeout(200)
        remembered = await page.evaluate("window.ansibleView.current")
        check("it reopens where you left it", remembered == "dashboard",
              f"reopened on {remembered!r}")

        await page.click("#av-close")
        await page.wait_for_timeout(200)
        check("the close button leaves too",
              await page.is_hidden("#ansible-stage"),
              "the view stayed open")

        print("\n-- Nothing threw --")
        real = [e for e in errors if "favicon" not in e.lower()]
        check("no script errors along the way", not real,
              "; ".join(real[:3]))

        await browser.close()

    print("\n" + "=" * 52)
    print(f"  {passed} passed  |  {len(failed)} failed")
    print("=" * 52)
    for line in failed:
        print("  -", line)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    asyncio.run(main())
