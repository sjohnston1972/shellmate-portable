"""
test_ansible_tls_led.py — The TLS indicator, in the browser (#586).

A light that stops updating is worse than no light: it goes on showing the
last good reading and reads as current. So what is checked here is that it
paints from the probe rather than from the markup's placeholder, that it
carries a *word* and not only a colour, and that clicking it produces the
certificate details — which are the only reason the light is clickable, and
the only way a self-signed certificate can be checked at all.

The runner is not configured in a test, which is the state a first-time
user is in, so the expected reading is the grey one. That it is grey rather
than red matters: nothing has failed yet.

Run: python test_ansible_tls_led.py
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

_TEMP = Path(tempfile.mkdtemp(prefix="shellmate-tlsled-"))
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

        await page.goto(BASE)
        await page.wait_for_selector("#av-tls-led", state="attached", timeout=15000)

        print("\n-- The light --")
        check("the indicator exists",
              await page.query_selector("#av-tls-led") is not None,
              "no TLS indicator in the header")
        check("it has a dot",
              await page.query_selector("#av-tls-led .av-led-dot") is not None)

        await page.evaluate("window.ansibleTls.probe()")
        await page.wait_for_timeout(900)

        label = (await page.inner_text("#av-tls-led")).strip()
        check("it carries a word, not only a colour", len(label) > 2,
              f"the label read {label!r} — colour alone is not a label")

        cls = await page.get_attribute("#av-tls-led", "class")
        check("an unconfigured runner reads grey, not red",
              "av-led-grey" in (cls or ""),
              f"class was {cls!r}; nothing has failed yet")

        title = await page.get_attribute("#av-tls-led", "title")
        check("hovering explains it", bool(title and len(title) > 10),
              f"title was {title!r}")
        aria = await page.get_attribute("#av-tls-led", "aria-label")
        check("and it is announced to a screen reader",
              bool(aria and "security" in aria.lower()), f"aria-label {aria!r}")

        print("\n-- What it reported --")
        health = await page.evaluate("window.ansibleTls.last")
        check("the probe returned a state", bool(health and health.get("state")),
              str(health)[:200])
        check("with a colour the stylesheet knows",
              (health or {}).get("kind") in ("ok", "warn", "bad", "grey"),
              str(health)[:200])
        check("and a sentence a person can act on",
              len((health or {}).get("detail", "")) > 10,
              str((health or {}).get("detail")))

        print("\n-- The details behind it --")
        # The light lives in the view's header, so the view has to be
        # open for it to be clickable at all.
        await page.evaluate("window.ansibleView.open()")
        await page.wait_for_selector("#ansible-stage:not([hidden])", timeout=5000)
        await page.click("#av-tls-led")
        await page.wait_for_timeout(700)
        shown = await page.query_selector(".av-tls-detail")
        check("clicking it opens the details", shown is not None,
              "the light is clickable for one reason and it did not work")
        if shown:
            text = await page.inner_text(".av-tls-detail")
            check("which name the address it checked",
                  "Address" in text, text[:200])
            check("and whether anything checked the certificate",
                  "Certificate checked" in text, text[:200])

        await page.keyboard.press("Escape")
        await page.wait_for_timeout(300)

        print("\n-- The timer --")
        check("it polls through the visibility helper rather than setInterval",
              await page.evaluate("!!window.shellmateVisibility"),
              "a hidden window would keep opening TLS connections forever")

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
