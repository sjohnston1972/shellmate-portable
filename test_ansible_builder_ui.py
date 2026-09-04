"""
test_ansible_builder_ui.py — The nested canvas, driven (#600).

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
    # A real estate to drag out of. Two groups, one nested, and a serial
    # connection that has no address — so the rail has something to show
    # and something to say is missing.
    from backend import groups, profiles

    for name in ("site-1", "site-1/routers", "site-1/switches"):
        groups.create_group(name, "green")
    profiles.save_profile({"name": "S1-R1", "hostname": "192.168.20.33",
                           "connection_type": "ssh", "platform": "ios",
                           "tags": ["site-1/routers"]})
    profiles.save_profile({"name": "S1-S1", "hostname": "192.168.20.34",
                           "connection_type": "ssh", "platform": "ios",
                           "tags": ["site-1/switches"]})
    profiles.save_profile({"name": "Console", "hostname": "", "port": 9600,
                           "connection_type": "serial",
                           "tags": ["site-1/switches"]})

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
        await page.wait_for_selector("#av-bld-canvas", timeout=10000)

        print("\n-- The canvas is drawn as the thing it builds --")
        check("there is a playbook box",
              await page.query_selector(".av-node-playbook") is not None,
              "the canvas did not render")
        check("with a Run button on it",
              await page.query_selector(".av-node-playbook .av-run") is not None)
        check("which is disabled with no plays",
              await page.is_disabled(".av-run"),
              "Run offered on an empty playbook would fail on the first click")
        check("and an add affordance for a play",
              "add play" in (await page.inner_text("#av-bld-canvas")).lower(),
              await page.inner_text("#av-bld-canvas"))

        print("\n-- Inventory is the real estate, beside the canvas --")
        check("the rail exists",
              await page.query_selector("#av-bld-rail") is not None)
        check("it is outside the playbook box",
              await page.query_selector(".av-node-playbook #av-bld-rail") is None,
              "drawing inventory inside the playbook implies it owns the groups")
        check("it renders the shared estate tree, not a bespoke list",
              await page.query_selector("#av-bld-rail .av-est-tree") is not None,
              "the builder must not carry a second inventory implementation")
        check("there is one estate module, shared",
              await page.evaluate("!!window.ansibleEstate"),
              "no shared source — two areas would each fetch and could disagree")

        groups = await page.eval_on_selector_all(
            "#av-bld-rail .av-est-group .av-est-label", "e => e.map(x => x.textContent)")
        check("groups appear by the name ShellMate uses",
              any("site" in g.lower() for g in groups), str(groups))
        check("and are draggable",
              await page.get_attribute("#av-bld-rail .av-est-group", "draggable") == "true",
              "the whole point is dragging them onto a play")

        hosts = await page.eval_on_selector_all(
            "#av-bld-rail .av-est-host .av-est-label", "e => e.map(x => x.textContent)")
        check("individual connections are offered too",
              len(hosts) > 0,
              "a play targeting one host is legitimate and has to be reachable")

        print("\n-- Adding a play --")
        await page.evaluate("""
          (async () => {
            const dlg = window.shellmateDialog;
            window.shellmateDialog = Object.assign({}, dlg, {
              form: async (o) => (o.title || '').includes('play')
                ? { name: 'Configure the switches', hosts: 'switches' }
                : { kind: 'config' },
            });
          })()
        """)
        await page.click('.av-node-playbook > .av-add')
        await page.wait_for_timeout(500)
        check("a play appears inside the playbook",
              await page.query_selector(".av-node-playbook .av-node-play") is not None,
              "the play did not nest")
        text = await page.inner_text(".av-node-play")
        check("the play says what it targets", "targets: switches" in text,
              text[:200])
        check("and carries its own add-task affordance",
              "add task" in text.lower(), text[:200])
        check("Run is enabled once there is a play",
              not await page.is_disabled(".av-run"))

        print("\n-- Adding a task inside that play --")
        await page.evaluate("""
          window.shellmateDialog = Object.assign({}, window.shellmateDialog, {
            form: async (o) => (o.title || '').includes('New task')
              ? { kind: 'config' }
              : { label: 'configure vlan', lines: 'vlan 20', parents: '' },
          });
        """)
        await page.click('.av-node-play .av-add:has-text("add task")')
        await page.wait_for_timeout(700)
        check("the task nests inside the play",
              await page.query_selector(".av-node-play .av-node-task") is not None,
              "a task must be inside the play it belongs to")
        task = await page.inner_text(".av-node-task")
        check("it is numbered and named", "Task 1" in task and "configure vlan" in task,
              task[:200])
        check("a task that changes the device is marked as such",
              await page.query_selector(".av-node-task.av-node-writes") is not None,
              "pushing configuration must not look like reading")

        print("\n-- The playbook it produced --")
        await page.wait_for_timeout(800)
        yaml_text = await page.input_value("#av-bld-text")
        check("two levels of nesting reached the YAML",
              "hosts: 'switches'" in yaml_text and "tasks:" in yaml_text,
              yaml_text[:220])
        check("the task's own name survived", "configure vlan" in yaml_text,
              yaml_text[:220])

        found = await page.inner_text("#av-bld-found")
        check("and the read-back agrees with it",
              "ios_config" in found or "cli_config" in found, found[:200])

        print("\n-- Dragging a group onto a play --")
        before = await page.inner_text(".av-node-play")
        moved = await page.evaluate("""
          (() => {
            const item = document.querySelector('#av-bld-rail .av-est-group');
            const play = document.querySelector('.av-node-play');
            if (!item || !play) return 'missing';
            const dt = new DataTransfer();
            item.dispatchEvent(new DragEvent('dragstart',
              { dataTransfer: dt, bubbles: true }));
            play.dispatchEvent(new DragEvent('drop',
              { dataTransfer: dt, bubbles: true, cancelable: true }));
            return dt.getData('application/x-shellmate-target') || 'no payload';
          })()
        """)
        check("the drag carries a typed payload",
              moved not in ("missing", "no payload"), str(moved))
        await page.wait_for_timeout(600)
        after = await page.inner_text(".av-node-play")
        check("and the play it was dropped on changed what it targets",
              after != before, f"before {before[:80]!r} after {after[:80]!r}")

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
