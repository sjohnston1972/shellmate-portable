"""
test_deployments_ui.py — The Deployments area, and what it refuses to do.

Source assertions on the area's script and markup, plus one pass in a
browser: open the Ansible view, click the tab, see the empty state. The
flow itself is proven over the API in test_deployments_api.py; this is
about the interface keeping the rules the backend keeps.

**Apply follows the server's word.** The button is off when
`apply_blocked` is non-empty and the reason is written beside it. The
interface holds no gate of its own that could disagree with the backend's.

**The rows that need reading come first.** Conflicts, then creates, in a
plan; failures, then creates, in an outcome table.

**Nothing is innerHTML.** Site names and reasons are what a device or a
person printed.

    python test_deployments_ui.py
"""

import asyncio
import re
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

from backend import paths

_TEMP = Path(tempfile.mkdtemp(prefix="shellmate-deploy-ui-"))
paths._data_dir_cache = _TEMP

passed = 0
failed: list[str] = []

ROOT = Path(__file__).parent
JS = (ROOT / "frontend" / "js" / "ansible_deployments.js").read_text(encoding="utf-8")
HTML = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
CSS = (ROOT / "frontend" / "css" / "style.css").read_text(encoding="utf-8")


def check(name: str, condition: bool, detail: str = "") -> None:
    global passed
    if condition:
        passed += 1
        print(f"  OK  {name}")
    else:
        failed.append(f"{name}: {detail}")
        print(f"  FAIL {name}\n       {detail}")


def test_the_markup() -> None:
    print("\n-- The area exists --")
    check("a tab", 'data-av-go="deployments"' in HTML)
    check("a section", 'data-av-area="deployments"' in HTML)
    check("the script is loaded after the view",
          HTML.index("ansible_view.js") < HTML.index("ansible_deployments.js"))
    check("it registers with the view", "view.area('deployments'" in JS)
    check("the section says why there is no check mode",
          "no check mode" in HTML.split('id="av-deployments"')[1][:900])


def test_the_gate_is_the_servers() -> None:
    print("\n-- Apply follows the server --")
    check("the button is off on the server's word",
          "...(blocked ? { disabled: true } : {})" in JS,
          "no gate of its own that could disagree with the backend")
    check("and disabled is only ever set when true",
          "disabled: false" not in JS and "disabled: !!" not in JS,
          "el() sets any attribute it is given; disabled=\"false\" is disabled")
    check("the reason is written beside it",
          "class: 'av-dep-why'" in JS and "text: blocked" in JS,
          "a disabled button with no sentence is a puzzle")
    check("apply asks once more, naming what it does",
          "danger: true" in JS.split("async function startRun")[1][:900]
          and "live account" in JS)
    check("plan is off until published",
          "entry.last_published ? {} : { disabled: true }" in JS)


def test_the_rows_that_matter_come_first() -> None:
    print("\n-- Reading order --")
    check("plan: conflicts, then creates",
          "PLAN_ORDER = { conflict: 0, create: 1, update: 2, unchanged: 3 }" in JS)
    check("outcome: failures, then creates",
          "APPLY_ORDER = { failed: 0, created: 1, updated: 2, skipped: 3 }" in JS)
    check("network ids are shown on plan rows too",
          "r.network_id || '—'" in JS,
          "an unchanged or conflicting site says which network it matched, "
          "before any apply exists")
    check("ids per site are shown on outcome rows", "r.ids || {}" in JS)
    check("a site outside manage_prefix reads as planned, not touched",
          "'planned, not touched'" in JS,
          "a fact on the row, not something inferred from a blank")
    check("updated is an outcome the table expects",
          "r.outcome === 'updated'" in JS)
    check("VLANs attempted per site are shown", "r.vlans" in JS)
    check("the scheme form is a spec per provider, JSON otherwise",
          "SCHEME_FIELDS = {" in JS and "editSpecScheme" in JS
          and "azure: [" in JS and "manage_prefix" in JS,
          "a form invented for fields the playbook does not read would be a form that lies")
    check("Azure's scope is the subscription only, sent as an env var",
          "sent as AZURE_SUBSCRIPTION_ID" in JS and "azure_resource_group" not in JS)
    check("AWS has a spec too", "aws: [" in JS and "sg_rules" in JS)
    check("an sg- security-group prefix is refused with the reason",
          "reject: /^sg-/i" in JS and "f.reject.test(" in JS
          and "says nothing about why" in JS,
          "AWS forbids it and the module swallows the cause")
    check("the kit can be committed from the list",
          "kits/${encodeURIComponent(provider)}/commit" in JS,
          "nothing commits the runner's tree; a kit exists only on its disk "
          "until this does")
    check("a truncated list says so", "list truncated" in JS)
    check("only conflicts and failures are tinted",
          ".av-dep-conflict td, .av-dep-failed td" in CSS
          and ".av-dep-unchanged" not in CSS)


def test_teardown_is_behind_its_own_plan_and_a_typed_name() -> None:
    print("\n-- Teardown --")
    check("a destroy plan step, read-only, before destroy",
          "'Plan a teardown'" in JS and "startRun(entry, 'destroy_plan')" in JS)
    check("destroy is off on the server's word",
          "...(destroyBlocked ? { disabled: true } : {})" in JS)
    check("with the reason written beside it", "text: `Destroy: ${destroyBlocked}`" in JS)
    check("the dialog wants the deployment's name typed",
          "Type ${entry.name} to confirm" in JS
          and "(v.confirm || '').trim() === entry.name" in JS)
    check("and says, per provider, what destroy does NOT remove",
          "NOT_REMOVED = {" in JS and "default VPC and its default security group" in JS
          and "claimed devices: untested" in JS,
          "observed by the runner, not reasoned")
    check("the destroy table renders from outcome per row, never a list of names",
          "r[key] === 'failed'" in JS and "elements || []" in JS,
          "the runner's first version listed what was attempted as removed")
    check("failures first", "DESTROY_ORDER = { failed: 0" in JS)
    check("skip rows are shown with their reason, never hidden",
          "DESTROY_ORDER = { failed: 0, remove: 1, removed: 1, skip: 2, skipped: 2 }" in JS
          and "el('td', { text: r.reason || '' })" in JS,
          "a plan that lists only removals hides the fact that a site was excluded")
    check("elements are shown in removal order",
          "(r.elements || []).join(' → ')" in JS)
    check("a built site missing from a re-upload is named as orphaned",
          "no longer in the list" in JS and "orphaned" in JS,
          "destroy removes only what is in sites.yml")
    check("forget offers destroy first, and never does it silently",
          "'Take me to Destroy first'" in JS and "value: 'forget'" in JS)


def test_the_columns_are_asked() -> None:
    print("\n-- Columns asked, never guessed --")
    check("the upload previews first", "preview: true" in JS)
    check("and asks which column is the site name",
          "pick('name', 'Site name', true)" in JS)
    check("serials may be blank now", "filled in later" in JS)
    check("the last mapping is remembered",
          "remembered[field] && headers.includes(remembered[field])" in JS)


def test_nothing_is_markup() -> None:
    print("\n-- Text, not markup --")
    check("no innerHTML in the area",
          "innerHTML" not in JS and "html:" not in JS,
          "site names and reasons are what a device or a person printed")
    check("forgetting says the cloud is untouched",
          "Nothing in the cloud is" in JS)


async def browser_pass() -> None:
    from playwright.async_api import async_playwright
    import uvicorn
    from backend import app as app_module

    port = 8790 + int(time.time()) % 100
    server = uvicorn.Server(uvicorn.Config(app_module.app, host="127.0.0.1",
                                           port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    time.sleep(2.0)

    print("\n-- In a browser --")
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        await page.goto(f"http://127.0.0.1:{port}/")
        await page.wait_for_selector("#ansible-stage", state="attached", timeout=15000)
        await page.click("#sidebar-link-ansible")
        await page.wait_for_selector("#ansible-stage:not([hidden])", timeout=5000)
        await page.click('#av-nav .av-tab[data-av-go="deployments"]')
        # The area fetches /api/deployments before it renders. Wait for
        # the render, not for a number of milliseconds — the same race the
        # Ansible env-keys test had, and the same fix.
        try:
            await page.wait_for_function(
                "document.querySelector('#av-deployments-body').innerText"
                ".includes('Build it from a definition')", timeout=5000)
        except Exception:
            pass
        body = await page.inner_text("#av-deployments-body")
        check("the empty state renders", "Build it from a definition" in body, body[:200])
        check("and says the plan is the only preview",
              "only preview" in body, body[:400])
        await browser.close()
    server.should_exit = True


def main() -> int:
    print("=" * 52)
    print("  Deployments — the area")
    print("=" * 52)
    for test in (test_the_markup, test_the_gate_is_the_servers,
                 test_the_rows_that_matter_come_first,
                 test_teardown_is_behind_its_own_plan_and_a_typed_name,
                 test_the_columns_are_asked,
                 test_nothing_is_markup):
        try:
            test()
        except Exception as exc:
            failed.append(f"{test.__name__}: raised {type(exc).__name__}: {exc}")
            print(f"  FAIL {test.__name__} raised {type(exc).__name__}: {exc}")
    try:
        asyncio.run(browser_pass())
    except Exception as exc:
        failed.append(f"browser_pass: raised {type(exc).__name__}: {exc}")
        print(f"  FAIL browser_pass raised {type(exc).__name__}: {exc}")
    shutil.rmtree(_TEMP, ignore_errors=True)
    print("\n" + "=" * 52)
    print(f"  {passed} passed  |  {len(failed)} failed")
    print("=" * 52)
    if failed:
        print("\nFAILURES:")
        for item in failed:
            print(f"  {item}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
