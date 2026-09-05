"""
test_ansible_repo_inv.py — The Ansible view's Repositories and Inventory areas (#586).

Two areas, two different failure modes worth catching before a person does:

- **Repositories** is a record, not a sync — there is no runner call behind
  it at all, just a URL kept honest by a regex. The thing to prove is that
  the honesty holds: a bad URL is refused with the server's own words, a
  good one is listed, and deleting it removes only the record.
- **Inventory** has to say something sensible with *nothing* configured —
  no runner, no estate — because that is exactly the state a first-time
  user is in. An empty estate rendering a blank rectangle reads as the
  screen having failed to load rather than as "add a connection first".
  Once a connection exists, this also checks the one fact the area exists
  to state plainly: a device ShellMate has not identified gets no
  `ansible_network_os`, not a guess.

Run: python test_ansible_repo_inv.py
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

_TEMP = Path(tempfile.mkdtemp(prefix="shellmate-ansrepoinv-"))
paths._data_dir_cache = _TEMP
assert paths.data_dir() == _TEMP, "test isolation failed — refusing to run"

import uvicorn  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402

from backend import profiles as profiles_module  # noqa: E402
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


async def _open_ansible(page, area: str) -> None:
    if await page.is_hidden("#ansible-stage"):
        await page.click("#sidebar-link-ansible")
        await page.wait_for_selector("#ansible-stage:not([hidden])", timeout=5000)
    await page.click(f'#av-nav .av-tab[data-av-go="{area}"]')
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
        await page.wait_for_selector("#ansible-stage", state="attached", timeout=15000)

        # ---------------------------------------------------------------
        print("\n-- Repositories: what this area is honest about --")
        await _open_ansible(page, "repositories")

        notice = (await page.inner_text("#av-repositories-body")).strip()
        check("it says this is a record, not a sync",
              "record" in notice.lower() and "sync" not in notice.lower().split("not a ")[0],
              f"the notice read: {notice[:200]!r}")
        check("it explains how a playbook actually reaches the runner",
              "bind mount" in notice.lower() or "project directory" in notice.lower(),
              "no mention of the bind mount, which is the whole point of this area")
        check("it offers no 'Sync now' — there is nothing here to sync",
              "sync now" not in notice.lower(),
              "the area implied a sync button that does not exist")

        print("\n-- Repositories: a bad URL is refused --")
        await page.click('#av-repositories-body >> text=Add repository')
        await page.wait_for_selector(".sm-dialog", timeout=3000)
        await page.fill("#sm-dialog-field-0", "Bad repo")
        await page.fill("#sm-dialog-field-1", "not-a-url-at-all")
        await page.click(".sm-dialog-actions .btn-primary")

        # The client only checks "required"; the URL scheme is the server's
        # rule, so refusing it round-trips through view.toast() -> a second
        # dialog carrying the server's own words.
        await page.wait_for_timeout(400)
        alert_text = ""
        for _ in range(20):
            box = await page.query_selector('.sm-dialog[role="alertdialog"]')
            if box:
                alert_text = (await box.inner_text()).strip()
                break
            await page.wait_for_timeout(100)
        check("the server's refusal reaches the screen",
              "https" in alert_text.lower() or "ssh" in alert_text.lower(),
              f"no refusal dialog appeared; saw {alert_text!r}")
        if alert_text:
            await page.click('.sm-dialog[role="alertdialog"] .btn-primary')
            await page.wait_for_timeout(150)
        # The form dialog itself may still be open behind the alert — close it.
        if await page.query_selector(".sm-dialog"):
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(150)

        after_bad = (await page.inner_text("#av-repositories-body")).strip()
        check("nothing was added for the refused URL",
              "Bad repo" not in after_bad,
              "the refused repository appeared in the list anyway")

        print("\n-- Repositories: a good one is added and listed --")
        await page.click('#av-repositories-body >> text=Add repository')
        await page.wait_for_selector(".sm-dialog", timeout=3000)
        await page.fill("#sm-dialog-field-0", "Netops playbooks")
        await page.fill("#sm-dialog-field-1", "https://example.com/netops.git")
        await page.fill("#sm-dialog-field-2", "main")
        await page.click(".sm-dialog-actions .btn-primary")
        await page.wait_for_timeout(400)

        listed = (await page.inner_text("#av-repositories-body")).strip()
        check("the new repository is listed",
              "Netops playbooks" in listed and "example.com/netops.git" in listed,
              f"not found in: {listed[:300]!r}")

        print("\n-- Repositories: delete removes the record --")
        await page.click('tr:has-text("Netops playbooks") button[title="Delete"]')
        await page.wait_for_selector('.sm-dialog', timeout=3000)
        confirm_text = (await page.inner_text(".sm-dialog")).strip()
        check("deleting asks first",
              "delete" in confirm_text.lower(),
              f"the confirm dialog read: {confirm_text[:150]!r}")
        await page.click('.sm-dialog .btn-danger, .sm-dialog .btn-primary')
        await page.wait_for_timeout(400)

        gone = (await page.inner_text("#av-repositories-body")).strip()
        check("the repository is gone after deleting",
              "Netops playbooks" not in gone,
              "it was still listed after deletion")

        # ---------------------------------------------------------------
        print("\n-- Inventory: nothing configured yet --")
        await _open_ansible(page, "inventory")
        await page.wait_for_timeout(500)

        empty_text = (await page.inner_text("#av-inventory-body")).strip()
        check("the area renders something, not a blank rectangle",
              len(empty_text) > 0, "the inventory area was empty")
        check("it says plainly there is nothing to browse on the runner",
              "no endpoint" in empty_text.lower() or "nothing here" in empty_text.lower(),
              f"the runner's-own explanation was missing: {empty_text[:200]!r}")
        check("with no saved connections it says so sensibly",
              "no saved ssh connections" in empty_text.lower()
              or "0 host" in empty_text.lower(),
              f"got: {empty_text[:200]!r}")
        check("with nothing to skip it says nothing was left out",
              "nothing was left out" in empty_text.lower(),
              "the left-out section did not account for the empty case")

        print("\n-- Inventory: a saved connection appears --")
        profiles_module.save_profile({
            "name": "core-sw-01", "hostname": "10.9.9.9", "port": 22,
            "username": "netops", "connection_type": "ssh",
        })
        # No profile-changed event reaches this area on its own — a saved
        # connection is not part of the shared cache the view refreshes.
        # The refresh control is how somebody actually gets the new state.
        await page.click('#av-inventory-body button[title="Rebuild the inventory"]')
        await page.wait_for_timeout(500)

        seeded_text = (await page.inner_text("#av-inventory-body")).strip()
        check("the seeded host shows up",
              "10.9.9.9" in seeded_text, f"not found in: {seeded_text[:400]!r}")
        check("its name shows up too",
              "core-sw-01" in seeded_text, f"not found in: {seeded_text[:400]!r}")
        check("an unidentified device gets no platform guess",
              "none" in seeded_text.lower() and "unidentified" in seeded_text.lower(),
              "the platform column did not say the honest default")
        # The generated INI used to be printed at the bottom of this area
        # and is gone (#608): it answered a question the two tables above
        # it had already answered. What replaced it is where a list
        # somebody built gets made, so that is what is asserted instead.
        check("the area offers somewhere to build a list of its own",
              "custom inventories" in seeded_text.lower(),
              f"got: {seeded_text[:400]!r}")

        # ---------------------------------------------------------------
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
