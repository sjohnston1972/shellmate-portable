"""
test_ansible_env_keys.py — Driving the Environments and Keys areas for real (#586).

`test_ansible_library.py` checks the backend refuses what it should.
`test_ansible_view.py` checks the view opens, navigates, and every area
paints something. Neither drives a form: an environment created here has
to actually show its force-check pill, and a key created here has to
actually never appear in the page — two things a unit test cannot see and
a placeholder ("renders something") does not check.

Three things matter enough to assert directly rather than trust by reading
the code:

- **Force check is visible.** An environment with it on shows a pill
  saying so on the card it lands on, not just in a tooltip nobody opens.
- **A key's value never reaches the page.** Not the list, not the network
  response the list is built from — the backend already promises this
  (`test_ansible_library.py`'s "listing carries no value at all"), and
  this is the same promise kept end to end through the browser.
- **Editing a key without retyping the value keeps it working.** The UI
  lets the value field sit empty on an edit; if that silently blanked the
  stored value, nothing would say so until a run failed against it. The
  check here is `ansible_keys.resolve()`, called the way a run would call
  it, right after the edit.

Run: python test_ansible_env_keys.py
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

_TEMP = Path(tempfile.mkdtemp(prefix="shellmate-ansenvkeys-"))
paths._data_dir_cache = _TEMP
assert paths.data_dir() == _TEMP, "test isolation failed — refusing to run"

import uvicorn  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402

from backend import ansible_keys as key_store  # noqa: E402
from backend.app import app  # noqa: E402


def _free_port() -> int:
    """A port nothing else holds: several suites run side by side."""
    import socket as _socket
    with _socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


PORT = _free_port()
BASE = f"http://127.0.0.1:{PORT}"

#: Never allowed to appear in the rendered page or in a network response
#: the page reads. If this string turns up anywhere it looks at, the key
#: store's one guarantee — no value ever comes back out — has a hole.
SECRET_VALUE = "a9f3-super-secret-do-not-leak-4471"

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


async def _open_dialog(page, button_selector: str) -> None:
    await page.click(button_selector)
    await page.wait_for_selector(".sm-dialog-overlay", timeout=5000)


async def _accept(page, danger: bool = False) -> None:
    button = ".sm-dialog-actions .btn-danger" if danger else ".sm-dialog-actions .btn-primary"
    await page.click(button)
    await page.wait_for_selector(".sm-dialog-overlay", state="detached", timeout=5000)


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
        await page.wait_for_selector("#ansible-stage:not([hidden])", timeout=5000)

        # ---------------------------------------------------------------
        print("\n-- Environments: creating one with force check on --")
        await page.click('#av-nav .av-tab[data-av-go="environments"]')
        await page.wait_for_timeout(150)

        await _open_dialog(page, '#av-environments-body button:has-text("New environment")')
        await page.fill("#sm-dialog-field-0", "Production")            # name
        await page.fill("#sm-dialog-field-1", "The live fleet")        # description
        await page.fill("#sm-dialog-field-5", "core-*")                # limit
        await page.check("#sm-dialog-field-8")                         # force_check

        await page.click('.av-env-vars-head button:has-text("Add variable")')
        await page.fill(".av-env-var-key", "ntp_server")
        await page.fill(".av-env-var-value", "10.0.0.1")

        await _accept(page)
        await page.wait_for_timeout(200)

        body = await page.inner_text("#av-environments-body")
        check("the new environment is listed", "Production" in body, body[:200])
        check("its force-check pill shows on the card",
              "Forces check mode" in body,
              "force_check was set but the card does not say so")
        check("its variable shows as a chip",
              "ntp_server=10.0.0.1" in body, body[:300])

        print("\n-- Environments: editing opens with what was saved --")
        await page.click('#av-environments-body .av-env-card button[title="Edit"]')
        await page.wait_for_selector(".sm-dialog-overlay", timeout=5000)
        name_value = await page.input_value("#sm-dialog-field-0")
        check("the edit dialog is pre-filled with the saved name",
              name_value == "Production", f"got {name_value!r}")
        checked = await page.is_checked("#sm-dialog-field-8")
        check("force check is still shown on", checked, "checkbox lost its state")
        # Cancel rather than resave — this step only checks the pre-fill.
        await page.click(".sm-dialog-actions .btn-secondary")
        await page.wait_for_selector(".sm-dialog-overlay", state="detached", timeout=5000)

        print("\n-- Environments: deleting, with a confirm --")
        await page.click('#av-environments-body .av-env-card button[title="Delete"]')
        await page.wait_for_selector(".sm-dialog-overlay", timeout=5000)
        await _accept(page, danger=True)
        await page.wait_for_timeout(200)
        after_delete = await page.inner_text("#av-environments-body")
        # Back to the empty state, which is now the shared centred one
        # rather than a dashed box saying "none yet" (#595). Asserted on
        # what it is for rather than on a form of words, so rewording the
        # copy does not fail a test about deletion.
        # Matched case-insensitively: inner_text returns *rendered* text, and
        # btn-primary is text-transform:uppercase, so the button reads
        # "NEW ENVIRONMENT" however it is written in the source.
        check("the environment is gone after confirming delete",
              "one choice" in after_delete.lower()
              and "new environment" in after_delete.lower(),
              after_delete[:200])

        # ---------------------------------------------------------------
        print("\n-- Keys: the area states its limits up front --")
        await page.click('#av-nav .av-tab[data-av-go="keys"]')
        await page.wait_for_timeout(150)
        keys_body = await page.inner_text("#av-keys-body")
        check("it says there is no way to read a value back",
              "no way to see a stored value" in keys_body.lower(), keys_body[:300])
        check("it says the value does reach the runner",
              "does reach the runner" in keys_body.lower(), keys_body[:300])
        check("it does not claim secrets are simply 'safe'",
              "are safe" not in keys_body.lower() and "is safe" not in keys_body.lower(),
              "found a bare safety reassurance instead of a stated limit")

        print("\n-- Keys: creating one --")
        await _open_dialog(page, '#av-keys-body button:has-text("New key")')
        await page.fill("#sm-dialog-field-0", "azure_secret")          # name
        await page.fill("#sm-dialog-field-4", "Azure client secret")   # description
        await page.fill("#sm-dialog-field-5", SECRET_VALUE)            # value
        await _accept(page)
        await page.wait_for_timeout(200)

        keys_body = await page.inner_text("#av-keys-body")
        check("the new key is listed", "azure_secret" in keys_body, keys_body[:200])
        check("its target defaulted to the shouting-case name",
              "AZURE_SECRET" in keys_body, keys_body[:300])
        check("it shows as readable", "Readable" in keys_body, keys_body[:300])

        print("\n-- Keys: the value is nowhere to be found --")
        rendered = await page.content()
        check("the secret is not in the rendered page",
              SECRET_VALUE not in rendered, "the value leaked into the DOM")
        api_text = await page.evaluate(
            "() => fetch('/api/ansible/keys').then(r => r.text())")
        check("the secret is not in the API response the list is built from",
              SECRET_VALUE not in api_text, "the value leaked into /api/ansible/keys")

        print("\n-- Keys: editing the target without retyping the value --")
        await page.click('#av-keys-body button[title="Edit"]')
        await page.wait_for_selector(".sm-dialog-overlay", timeout=5000)
        target_value = await page.input_value("#sm-dialog-field-3")
        check("the target field shows the current target",
              target_value == "AZURE_SECRET", f"got {target_value!r}")
        value_field = await page.input_value("#sm-dialog-field-5")
        check("the value field starts empty on an edit",
              value_field == "", f"got {value_field!r}")
        await page.fill("#sm-dialog-field-3", "AZURE_CLIENT_SECRET")
        await _accept(page)
        await page.wait_for_timeout(200)

        keys_body = await page.inner_text("#av-keys-body")
        check("the new target is shown", "AZURE_CLIENT_SECRET" in keys_body, keys_body[:300])

        env, extra, unreadable = key_store.resolve(["azure_secret"])
        check("the value still resolves after the edit",
              env.get("AZURE_CLIENT_SECRET") == SECRET_VALUE, str((env, unreadable)))
        check("nothing was left unreadable", unreadable == [], str(unreadable))

        print("\n-- Keys: deleting, with a confirm that says the value goes too --")
        await page.click('#av-keys-body button[title="Delete"]')
        await page.wait_for_selector(".sm-dialog-overlay", timeout=5000)
        confirm_body = await page.inner_text(".sm-dialog")
        check("the delete confirm says the value is deleted with it",
              "deleted with it" in confirm_body.lower() or "cannot be undone" in confirm_body.lower(),
              confirm_body[:200])
        await _accept(page, danger=True)
        await page.wait_for_timeout(200)

        keys_body = await page.inner_text("#av-keys-body")
        check("the key is gone after confirming delete",
              "held in the vault" in keys_body.lower()
              and "new key" in keys_body.lower(),
              keys_body[-200:])

        env, extra, unreadable = key_store.resolve(["azure_secret"])
        check("and its value is gone from the vault too",
              "azure_secret" in unreadable, str((env, unreadable)))

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
