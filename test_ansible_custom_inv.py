"""
test_ansible_custom_inv.py — Building an inventory, and pointing a run at it (#608).

The backend half is covered by `test_ansible_inventories.py`, which is
mostly about refusal. This is the other half: whether a person can actually
get from "these four switches" or "this CSV somebody sent me" to a run
pointed at exactly those, without the interface quietly deciding anything
on their behalf.

Three things are worth driving through a browser rather than asserting
about functions:

- **Ticking hosts and saving them as a list.** The rows have to carry what
  ShellMate already knew — address, name, port, platform — because a
  curated list that stores only addresses silently loses the username and
  the run fails on authentication three screens later.
- **The header tick box changing the parse, not just the picture.** An
  override that re-renders the preview but is not passed back when the file
  is read is the worst possible outcome: the screen shows two devices and
  the saved list holds one.
- **The run dialog offering it.** A list nothing can be pointed at is not
  a feature, and the count in the dialog has to come from the same place
  the targets do.

The generated-INI block is asserted *gone*. It was the bottom third of the
area and answered a question already answered twice above it, in tables.

Run: python test_ansible_custom_inv.py
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

_TEMP = Path(tempfile.mkdtemp(prefix="shellmate-anscustinv-"))
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


#: A headerless export whose first row is a real device. The whole point of
#: the override: read as a header, this file loses a switch and says so
#: nowhere.
HEADLESS_CSV = "10.55.0.1,core-1\n10.55.0.2,core-2\n"


async def _open_inventory(page) -> None:
    if await page.is_hidden("#ansible-stage"):
        await page.click("#sidebar-link-ansible")
        await page.wait_for_selector("#ansible-stage:not([hidden])", timeout=5000)
    await page.click('#av-nav .av-tab[data-av-go="inventory"]')
    # The estate table arrives after two fetches, not one — waiting on a
    # timeout here made the first assertions race the second of them.
    await page.wait_for_selector(".av-inv-hosts", timeout=8000)


async def main() -> None:
    # Two SSH connections to curate from, and one serial that cannot be
    # reached — so the "left out" half is exercised alongside.
    profiles_module.save_profile({
        "name": "access-sw-01", "hostname": "10.44.0.1", "port": 2201,
        "username": "netops", "connection_type": "ssh", "platform": "ios",
    })
    profiles_module.save_profile({
        "name": "access-sw-02", "hostname": "10.44.0.2", "port": 22,
        "username": "netops", "connection_type": "ssh",
    })

    threading.Thread(target=_serve, daemon=True).start()
    time.sleep(2.0)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()

        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))

        await page.goto(BASE)
        await page.wait_for_selector("#ansible-stage", state="attached", timeout=15000)
        await _open_inventory(page)

        # ---------------------------------------------------------------
        print("\n-- The INI dump is gone --")
        body = await page.inner_text("#av-inventory-body")
        check("the generated-INI block is no longer rendered",
              await page.query_selector(".av-inv-ini") is None,
              "the INI block is still on the screen")
        check("and the hosts it summarised are still shown as a table",
              "10.44.0.1" in body and "10.44.0.2" in body,
              f"got: {body[:300]!r}")
        check("the area still says what was left out",
              "left out" in body.lower(), f"got: {body[:300]!r}")

        # ---------------------------------------------------------------
        print("\n-- Curating a list out of the estate --")
        check("the save control is disabled with nothing ticked",
              await page.is_disabled("#av-inv-curate"),
              "it offered to save a list of nothing")

        await page.check('#av-inventory-body input[data-host="10.44.0.1"]')
        await page.wait_for_timeout(200)
        label = await page.inner_text("#av-inv-curate")
        check("ticking one host enables it and says how many",
              not await page.is_disabled("#av-inv-curate") and "1" in label,
              f"the button read {label!r}")

        await page.click("#av-inv-curate")
        await page.wait_for_selector(".sm-dialog", timeout=3000)
        await page.fill("#sm-dialog-field-0", "Weekend upgrade")
        await page.click(".sm-dialog-actions .btn-primary")
        await page.wait_for_timeout(600)

        listed = await page.inner_text("#av-inventory-body")
        check("the list appears among the custom inventories",
              "Weekend upgrade" in listed, f"not found in: {listed[:400]!r}")
        check("and says it was picked out of the estate",
              "picked from the estate" in listed, f"got: {listed[:400]!r}")

        # What was stored, rather than what was drawn: a curated list that
        # keeps only addresses loses the username, and the run then fails
        # on authentication with nothing pointing back to here.
        saved = await page.evaluate(
            "fetch('/api/ansible/inventories').then(r => r.json())")
        entry = next((i for i in saved["inventories"]
                      if i["name"] == "Weekend upgrade"), None)
        check("it holds exactly the one host that was ticked",
              entry is not None and entry["hosts"] == 1, str(entry))
        rows = await page.evaluate(
            "id => fetch('/api/ansible/inventories/' + id).then(r => r.json())",
            entry["id"])
        row = (rows.get("hosts") or [{}])[0]
        check("the row carries what ShellMate already knew",
              row.get("host") == "10.44.0.1" and row.get("user") == "netops"
              and row.get("port") == 2201,
              str(row))
        check("including the platform, rather than the Ansible name for it",
              row.get("platform") == "ios",
              "reversing cisco.ios.ios back to ios in the browser would be a "
              "second copy of a map that already exists in the backend")

        # ---------------------------------------------------------------
        print("\n-- The shipped examples --")
        row = await page.query_selector(".av-inv-examples")
        check("the area offers worked examples", row is not None,
              "somebody with no file to hand has nothing to look at")
        if row:
            text = await row.inner_text()
            check("including a plain list and a spreadsheet",
                  "plain list" in text.lower() and "spreadsheet" in text.lower(),
                  f"got: {text!r}")

        await page.click('.av-inv-examples button[data-example="meraki"]')
        await page.wait_for_selector(".av-inv-upload", timeout=5000)
        check("trying one loads it into the same mapping step an upload uses",
              await page.input_value("#av-inv-map-host") == "LAN IP",
              "the example is meant to demonstrate the mapping, so its own "
              "mapping is filled in — the empty form demonstrates nothing")
        example_panel = (await page.inner_text(".av-inv-upload")).lower()
        check("and its rows are shown",
              "10.20.0.5" in example_panel, f"got: {example_panel[:200]!r}")
        await page.click(".av-inv-upload-head .btn-tertiary")
        await page.wait_for_timeout(300)

        # ---------------------------------------------------------------
        print("\n-- Uploading a file --")
        upload = _TEMP / "headless.csv"
        upload.write_text(HEADLESS_CSV, encoding="utf-8")
        await page.set_input_files("#av-inv-file", str(upload))
        await page.wait_for_selector(".av-inv-upload", timeout=5000)

        # Lower-cased: the block title is upper-cased by CSS, so the text
        # this reads back is not the text the file says.
        panel = (await page.inner_text(".av-inv-upload")).lower()
        check("the file is read and its rows shown",
              "2 rows" in panel and "10.55.0.1" in panel, f"got: {panel[:300]!r}")
        check("a first row holding an address is not read as a heading",
              not await page.is_checked("#av-inv-headed"),
              "reading it as a header would lose a device, and nobody "
              "notices one switch missing from a run of forty")
        check("the host column is left for the user to nominate",
              await page.input_value("#av-inv-map-host") == "",
              "a column was pre-picked, which is exactly the guess this "
              "screen exists not to make")

        # The override has to reach the parse, not only the picture of it.
        await page.check("#av-inv-headed")
        await page.wait_for_timeout(500)
        reread = (await page.inner_text(".av-inv-upload")).lower()
        check("saying the first row is a heading re-reads the file",
              "1 row" in reread, f"the count did not change: {reread[:200]!r}")
        await page.uncheck("#av-inv-headed")
        await page.wait_for_timeout(500)

        await page.select_option("#av-inv-map-host", "column 1")
        await page.select_option("#av-inv-map-name", "column 2")
        await page.fill("#av-inv-name", "Site 55")
        await page.select_option("#av-inv-platform", "nxos")
        await page.click("#av-inv-save-upload")
        await page.wait_for_timeout(700)

        after = await page.inner_text("#av-inventory-body")
        check("the uploaded list is saved and listed",
              "Site 55" in after, f"not found in: {after[:500]!r}")
        check("and it is labelled with the file it came from",
              "headless.csv" in after, f"got: {after[:500]!r}")

        stored = await page.evaluate(
            "fetch('/api/ansible/inventories').then(r => r.json())")
        site = next((i for i in stored["inventories"] if i["name"] == "Site 55"), None)
        check("both rows survived the override",
              site is not None and site["hosts"] == 2, str(site))

        # ---------------------------------------------------------------
        print("\n-- Choosing one in the Run dialog --")
        await page.evaluate("window._ansible.openRunDialog("
                            "'site.yml', 'library')")
        await page.wait_for_timeout(900)
        check("the run dialog opened",
              not await page.is_hidden("#ansible-run-overlay"),
              "the dialog did not open, so the picker cannot be checked")

        radio = 'input[name="ansible-target"][value="custom"]'
        check("a custom inventory is offered as a target",
              await page.query_selector(radio) is not None
              and not await page.is_disabled(radio),
              "with two lists saved, the choice should be available")

        await page.check(radio)
        await page.wait_for_timeout(400)
        check("choosing it shows the picker",
              not await page.is_hidden("#ansible-target-custom"),
              "the pane stayed hidden")

        options = await page.inner_text("#ansible-custom-select")
        check("both saved lists are in the picker",
              "Weekend upgrade" in options and "Site 55" in options,
              f"got: {options!r}")

        await page.select_option("#ansible-custom-select", label="Site 55 (2)")
        await page.wait_for_timeout(500)
        preview = await page.inner_text("#ansible-custom-preview")
        check("the preview names the hosts it would run against",
              "10.55.0.1" in preview and "10.55.0.2" in preview,
              f"got: {preview!r}")
        check("and says what platform travels with it",
              "nxos" in preview, f"got: {preview!r}")

        await page.select_option("#ansible-custom-select", label="Weekend upgrade (1)")
        await page.wait_for_timeout(500)
        preview = await page.inner_text("#ansible-custom-preview")
        check("a list with no platform says so rather than staying silent",
              "no platform" in preview.lower(),
              "an unstated platform is a fact worth reading before a run, "
              "not an absence to notice afterwards")

        # ---------------------------------------------------------------
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
