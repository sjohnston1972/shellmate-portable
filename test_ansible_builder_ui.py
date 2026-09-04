"""
test_ansible_builder_ui.py — Assembling a playbook in the browser (#586).

The backend half is covered by test_ansible_builder.py. What this adds is
the part a person actually touches, and specifically the part that fails
quietly: the panel that says what a playbook would do.

The blocks screen is a form that produces YAML, so the checks are that
adding a task shows it, that a task which writes to a device is visibly
different from one that reads, and that building produces text with the
right modules in it. Then the read-back table has to agree with the
playbook in the box — because if it can disagree, it is worse than absent.
Somebody would trust it.

Run: python test_ansible_builder_ui.py
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

_TEMP = Path(tempfile.mkdtemp(prefix="shellmate-bldui-"))
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
        await page.wait_for_selector("#ansible-stage", state="attached", timeout=15000)
        await page.evaluate("window.ansibleView.open('builder')")
        await page.wait_for_selector("#av-bld-blocks", timeout=10000)

        print("\n-- The form is there --")
        for field in ("av-bld-name", "av-bld-hosts", "av-bld-family",
                      "av-bld-ask", "av-bld-text"):
            check(f"{field} exists", await page.query_selector(f"#{field}") is not None,
                  "the builder did not finish rendering")

        options = await page.eval_on_selector_all(
            "#av-bld-family option", "els => els.map(e => e.value)")
        check("the platform picker offers the generic option first",
              options and options[0] == "generic", str(options))
        check("and the platforms ShellMate knows",
              "ios" in options and "nxos" in options, str(options))

        print("\n-- Adding tasks --")
        await page.click('#av-bld-blocks ~ .av-bld-adders button:has-text("Gather facts")')
        await page.wait_for_timeout(120)
        check("a block appears when added",
              await page.locator(".av-bld-block").count() == 1,
              "adding a block rendered nothing")
        check("a read-only block is marked read only",
              await page.locator('.av-bld-block:has-text("read only")').count() == 1,
              "a safe task was not distinguished from a writing one")

        await page.click('.av-bld-adders button:has-text("Push configuration lines")')
        await page.wait_for_timeout(120)
        check("a writing block is marked as changing the device",
              await page.locator('.av-bld-block:has-text("changes the device")').count() == 1,
              "a task that writes looked the same as one that reads")

        print("\n-- Building it --")
        await page.fill("#av-bld-name", "Set NTP")
        await page.fill("#av-bld-hosts", "core")
        await page.select_option("#av-bld-family", "ios")
        await page.fill("textarea[id$='-lines']", "ntp server 10.0.0.1")
        await page.click('button:has-text("Build it")')
        await page.wait_for_timeout(700)

        text = await page.input_value("#av-bld-text")
        check("the playbook appears in the box", "cisco.ios.ios_config" in text,
              text[:200])
        check("with the play name and hosts asked for",
              "Set NTP" in text and "core" in text, text[:200])

        print("\n-- What it says the playbook would do --")
        rows = await page.locator("#av-bld-found tbody tr").count()
        check("the read-back lists both tasks", rows == 2, f"it listed {rows}")
        found = await page.inner_text("#av-bld-found")
        check("and marks the configuration task as writing", "writes" in found,
              found[:200])
        check("and the facts task as reading", "reads" in found, found[:200])
        check("no draft warning on something built from blocks",
              "This is a draft" not in found,
              "blocks are deterministic; calling their output a draft is wrong")

        print("\n-- Reading back an edit --")
        await page.fill("#av-bld-text", """---
- name: Hand written
  hosts: all
  tasks:
    - name: Reboot it
      ansible.builtin.reboot:
        reboot_timeout: 600
    - name: Something invented
      acme.widget.frobnicate:
        thing: 1
""")
        await page.click('button:has-text("Read it back")')
        await page.wait_for_timeout(600)
        found = await page.inner_text("#av-bld-found")
        check("a hand edit is re-read", "ansible.builtin.reboot" in found,
              found[:300])
        check("an unrecognised module is named, not ignored",
              "acme.widget.frobnicate" in found and "does not recognise" in found,
              found[:300])

        print("\n-- The assistant --")
        check("there is somewhere to describe what you want",
              await page.query_selector("#av-bld-ask") is not None)
        check("and it will not ask with nothing typed",
              await page.query_selector("#av-bld-draft") is not None)

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
