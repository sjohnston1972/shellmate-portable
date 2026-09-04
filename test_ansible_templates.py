"""
test_ansible_templates.py — A template goes from a body full of holes to a
saved playbook, and the one refusal that matters along the way is visible (#586).

Three things here fail silently if they break:

- **The undescribed-hole refusal is shown, not swallowed.** `save_template`
  in `backend/ansible_library.py` refuses a `{{ hole }}` nothing describes,
  and it names the variable — the point of showing that message by the body
  rather than as a toast is that a toast has usually scrolled off by the
  time somebody looks back to fix it.
- **Filling a template in actually calls the server to render it.**
  Substitution is deliberately not client-side (the backend's docstring is
  explicit about why: this runs in ShellMate, and a template engine with
  Python access behind a text box is a remote-code path), so a preview that
  quietly rendered wrong would look identical to one that never called the
  endpoint at all.
- **Save-as-playbook actually lands in the library.** Rendering and saving
  are the same endpoint with one extra field; a bug that dropped `save_as`
  would still show a preview and nobody would notice until Playbooks looked
  empty.

Run: python test_ansible_templates.py
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

_TEMP = Path(tempfile.mkdtemp(prefix="shellmate-anstpl-"))
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

#: A play with two holes. Real enough that a rendered copy is valid YAML —
#: save-as-playbook refuses anything that is not, so a fabricated fragment
#: that renders to nonsense would fail one step later for the wrong reason.
BODY = (
    "- hosts: all\n"
    "  gather_facts: false\n"
    "  tasks:\n"
    "    - name: Set interface description\n"
    "      ios_config:\n"
    "        lines:\n"
    "          - description {{ desc }}\n"
    "        parents: interface {{ iface }}\n"
)


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

        await page.click("#sidebar-link-ansible")
        await page.click('#av-nav .av-tab[data-av-go="templates"]')
        await page.wait_for_timeout(150)

        print("\n-- Empty state --")
        empty_text = (await page.inner_text("#av-templates-body")).strip()
        check("says what a template is before there are any",
              "playbook" in empty_text.lower(),
              f"empty state read: {empty_text[:120]!r}")

        print("\n-- Creating a template --")
        await page.locator('button:has-text("New template")').first.click()
        await page.wait_for_selector(".av-tpl-form")

        await page.locator('.av-tpl-form > .form-group:has(label:has-text("Name")) input') \
            .fill("Set interface description")
        await page.locator('.av-tpl-form > .form-group:has(label:has-text("Description")) input') \
            .fill("Sets the description on an interface")
        await page.locator('.av-tpl-form > .form-group:has(label:has-text("Platform")) input') \
            .fill("ios")
        await page.fill(".av-tpl-body-input", BODY)

        check("Name is marked required",
              await page.locator('.av-tpl-form > .form-group:has(label:has-text("Name")) .required')
                        .count() > 0,
              "no required marker on Name")

        await page.click('.av-tpl-vars-head button:has-text("Add variable")')
        row0 = page.locator(".av-tpl-var-row").nth(0)
        await row0.locator('.form-group:has(label:has-text("Variable name")) input').fill("iface")
        await row0.locator('.form-group:has(label:has-text("Label")) input').fill("Interface")
        await row0.locator('.form-group:has(label:has-text("Default")) input') \
            .fill("GigabitEthernet0/1")

        print("\n-- The undescribed-hole refusal --")
        await page.click('.av-tpl-form-actions button:has-text("Create template")')
        await page.wait_for_selector(".av-tpl-body-error", timeout=5000)
        refusal = (await page.inner_text(".av-tpl-body-error")).strip()
        check("the refusal names the missing variable",
              "desc" in refusal,
              f"refusal read: {refusal!r}")
        check("still on the editor rather than losing the draft",
              await page.is_visible(".av-tpl-body-input"),
              "the failed save left the editor")

        print("\n-- The detected-holes helper --")
        await page.click('.av-notice-info button:has-text("Add row")')
        await page.wait_for_timeout(100)
        check("a row was added for the missing hole",
              await page.locator(".av-tpl-var-row").count() == 2,
              "still one row after the helper ran")

        row1 = page.locator(".av-tpl-var-row").nth(1)
        await row1.locator('.form-group:has(label:has-text("Default")) input').fill("Uplink to core")

        await page.click('.av-tpl-form-actions button:has-text("Create template")')
        await page.wait_for_selector(".av-tpl-card", timeout=5000)
        check("saving succeeded once every hole was described",
              await page.locator(".av-tpl-card:has-text(\"Set interface description\")").count() == 1,
              "the template did not land in the list")

        print("\n-- The list --")
        card = page.locator(".av-tpl-card", has_text="Set interface description")
        check("shows the writes mark",
              "Writes" in await card.inner_text(),
              "a writing template did not say so")
        check("shows the variable count",
              "2 variable" in await card.inner_text(),
              "variable count missing or wrong")

        print("\n-- Filling it in --")
        await card.locator('button:has-text("Fill in")').click()
        await page.wait_for_selector(".av-tpl-fill")

        iface_field = page.locator('.av-tpl-fill > .form-group:has(label:has-text("Interface")) input')
        desc_field = page.locator('.av-tpl-fill > .form-group:has(label:has-text("desc")) input')
        check("the interface field carried the template default over",
              await iface_field.input_value() == "GigabitEthernet0/1",
              "the default was not prefilled")
        await iface_field.fill("GigabitEthernet0/2")
        await desc_field.fill("Uplink to distribution")

        await page.click('.av-tpl-form-actions button:has-text("Preview")')
        await page.wait_for_selector(".av-tpl-preview-yaml", timeout=5000)
        rendered = (await page.inner_text(".av-tpl-preview-yaml")).strip()
        check("the preview substituted both values",
              "GigabitEthernet0/2" in rendered and "Uplink to distribution" in rendered,
              f"rendered text: {rendered[:200]!r}")

        print("\n-- Saving it as a playbook --")
        await page.click('.av-tpl-form-actions button:has-text("Save as playbook")')
        await page.wait_for_selector("#sm-dialog-input", timeout=5000)
        await page.fill("#sm-dialog-input", "core-desc-test")
        await page.click(".sm-dialog-actions .btn-primary")
        await page.wait_for_selector(".av-tpl-notice-ok", timeout=5000)
        confirmation = (await page.inner_text(".av-tpl-notice-ok")).strip()
        check("says what it was saved as",
              "core-desc-test" in confirmation,
              f"confirmation read: {confirmation!r}")

        library = await page.evaluate(
            "fetch('/api/ansible/playbooks').then(r => r.json())")
        names = [p.get("name") for p in library.get("library", [])]
        check("the playbook is actually in the library",
              "core-desc-test.yml" in names,
              f"library held {names}")

        print("\n-- Deleting the template --")
        await page.click('.av-tpl-panel-head button:has-text("Back to templates")')
        await page.wait_for_selector(".av-tpl-card")
        # By name, not by position or count: the library ships example
        # templates now (#590), so "no cards left" stopped meaning "the one
        # I made is gone" and the first card is no longer necessarily mine.
        mine = page.locator('.av-tpl-card:has-text("Set interface description")')
        await mine.locator('button[title="Delete template"]').click()
        await page.wait_for_selector(".sm-dialog-actions .btn-danger", timeout=5000)
        await page.click(".sm-dialog-actions .btn-danger")
        await page.wait_for_timeout(400)
        check("the template is gone", await mine.count() == 0,
              "the card survived the delete")
        check("and the examples were left alone",
              await page.locator(".av-tpl-card").count() > 0,
              "deleting one template took the shipped examples with it")

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
