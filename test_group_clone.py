"""
test_group_clone.py — Cloning a group, and the four ways it must refuse (#598).

Sites resemble each other, so building the fifth by retyping ten subgroups
is work the shape of the data says nobody should do. What is worth testing
is less the copying than the refusals, because each one is a way to quietly
corrupt a tree:

- **into itself, or its own subtree** — recurses, and the name *is* the
  identity so there is no cycle detection to fall back on;
- **onto a name already taken** — group identity is the full path, so
  "cloning" onto a live group would silently merge two trees rather than
  copy one;
- **a destination that does not exist** — would create a group under a
  path segment nobody made.

And one thing about what it copies: connections are **tagged** into the new
groups, never duplicated. A second profile for one device is two places to
change its password and two rows claiming to be the same switch.

Run: python test_group_clone.py
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

_TEMP = Path(tempfile.mkdtemp(prefix="shellmate-clone-"))
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
    from backend import groups, profiles

    for name in ("site-4", "site-4/core switches", "site-4/access switches"):
        groups.create_group(name, "green")
    kept = profiles.save_profile({
        "name": "core-1", "hostname": "10.0.0.1", "connection_type": "ssh",
        "tags": ["site-4/core switches"]})

    threading.Thread(target=_serve, daemon=True).start()
    time.sleep(2.0)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        await page.goto(BASE)
        await page.wait_for_timeout(2000)

        print("\n-- The menu offers it --")
        # The tree hides itself when there is nothing to show, and rows are
        # keyed by data-key rather than data-group-key.
        await page.evaluate("document.getElementById('group-tree')"
                            ".classList.remove('hidden')")
        row = page.locator('#group-tree .tree-chip[data-key="site-4"]').first
        await row.wait_for(state="visible", timeout=10000)
        await row.click(button="right")
        await page.wait_for_selector(".context-menu, .group-menu", timeout=5000)
        menu = await page.inner_text(".context-menu, .group-menu")
        check("the group menu has a clone entry", "Clone to" in menu,
              menu[:300])

        await page.click('text=Clone to')
        await page.wait_for_selector(".sm-dialog", timeout=5000)
        dialog = await page.inner_text(".sm-dialog")
        check("the dialog explains what it copies",
              "subgroup" in dialog.lower(), dialog[:200])
        check("and that connections are not copied by default",
              "off by default" in dialog.lower()
              or "starts empty" in dialog.lower(), dialog[:300])

        options = await page.eval_on_selector_all(
            ".sm-dialog select option", "els => els.map(e => e.value)")
        check("the top level is offered as a destination", "" in options,
              str(options))
        check("the group cannot be cloned into itself",
              "site-4" not in options,
              f"it offered {options} — cloning into its own subtree recurses")

        print("\n-- Cloning it --")
        await page.fill(".sm-dialog input[type=text]", "site-5")
        await page.click('.sm-dialog button:has-text("Clone")')
        await page.wait_for_timeout(900)

        made = sorted(g["key"] for g in groups.list_groups())
        check("the group was copied", "site-5" in made, str(made))
        check("and every subgroup came with it",
              "site-5/core switches" in made and "site-5/access switches" in made,
              str(made))
        check("the original is untouched",
              "site-4/core switches" in made, str(made))

        check("the copy is empty, because the addresses would be wrong",
              profiles.find_profile(kept["id"])["tags"] == ["site-4/core switches"],
              str(profiles.find_profile(kept["id"])["tags"]))

        print("\n-- With the connections --")
        result = groups.clone_group("site-4", name="site-6",
                                    include_connections=True)
        check("cloning with connections reports how many it touched",
              result["connections"] == 1, str(result))
        tags = profiles.find_profile(kept["id"])["tags"]
        check("the connection is tagged into the copy",
              "site-6/core switches" in tags, str(tags))
        check("and is still in the original",
              "site-4/core switches" in tags, str(tags))
        check("nothing was duplicated",
              len([p for p in profiles.get_profiles()
                   if p["hostname"] == "10.0.0.1"]) == 1,
              "a second profile for one device is two places to change a "
              "password")

        print("\n-- What it refuses --")
        for args, why in (
            ({"destination": "site-4", "name": "inner"}, "into its own subtree"),
            ({"name": "site-5"}, "onto a name already taken"),
            ({"destination": "nowhere", "name": "x"}, "into a group that does not exist"),
            ({"name": ""}, "with no name"),
        ):
            try:
                groups.clone_group("site-4", **{
                    "destination": args.get("destination", ""),
                    "name": args.get("name", ""),
                })
                # An empty name reuses the original's, which is legal only
                # when the destination differs — so that one lands on the
                # duplicate check rather than on a missing name.
                check(f"refuses cloning {why}", False, "it was accepted")
            except ValueError as exc:
                check(f"refuses cloning {why}", True)
                check(f"  and says why ({why})", len(str(exc)) > 20, str(exc))

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
