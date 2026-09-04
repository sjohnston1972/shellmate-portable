"""
test_dashboard_live.py — The dashboard shows what is connected (#580), and
the tab hover card reads sensibly (#581).

A fake SSH device stands in for the switch; the app runs in-process on
its own port; Playwright drives the page. Run: python test_dashboard_live.py
"""

import asyncio
import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import paramiko  # noqa: E402

from backend import paths  # noqa: E402

_TEMP = Path(tempfile.mkdtemp(prefix="shellmate-dashlive-"))
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


class Device(paramiko.ServerInterface):
    def __init__(self) -> None:
        self.keep: list = []

    def get_allowed_auths(self, username):
        return "password"

    def check_auth_password(self, username, password):
        return paramiko.AUTH_SUCCESSFUL if password == "pw" else paramiko.AUTH_FAILED

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED if kind == "session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_pty_request(self, *a, **k):
        return True

    def check_channel_shell_request(self, channel):
        self.keep.append(channel)

        def talk():
            channel.send(b"\r\nsw1#")
            while True:
                try:
                    data = channel.recv(1024)
                except Exception:
                    return
                if not data:
                    return
                channel.send(data)
        threading.Thread(target=talk, daemon=True).start()
        return True


def start_device() -> int:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(4)
    port = listener.getsockname()[1]
    key = paramiko.RSAKey.generate(2048)
    server = Device()

    def run():
        while True:
            try:
                client, _ = listener.accept()
            except OSError:
                return
            t = paramiko.Transport(client)
            t.add_server_key(key)
            try:
                t.start_server(server=server)
            except Exception:
                continue
            server.keep.append(t)
    threading.Thread(target=run, daemon=True).start()
    return port


async def main_async() -> None:
    device_port = start_device()
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width": 1400, "height": 900})
        page.on("console", lambda m: print("   [browser]", m.text) if m.type == "error" else None)
        await page.goto(BASE, wait_until="networkidle")

        # A saved connection to the fake device, with its password remembered.
        profile = await page.evaluate("""async (port) => {
            const r = await fetch('/api/profiles', {method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({name: 'sw1', hostname: '127.0.0.1', port, username: 'eng', connection_type: 'ssh', tags: ['lab']})});
            return await r.json();
        }""", device_port)
        pid = profile.get("id") or (profile.get("profile") or {}).get("id")
        check("a profile is saved", bool(pid), str(profile)[:200])
        # The page loaded before the profile existed; load it again as a user would.
        await page.goto(BASE, wait_until="networkidle")
        await page.wait_for_selector(".tree-chip", timeout=5000)
        # Open the group so its leaves, and their dots, are drawn.
        if not await page.evaluate("() => !!document.querySelector('.tree-leaf-dot')"):
            await page.click(".tree-chip:not(.tree-chip-ungrouped)")
            await page.wait_for_selector(".tree-leaf-dot", timeout=3000)

        print("\n-- Connect from the dashboard and come back (#580) --")
        await page.evaluate("""async ({pid, port}) => {
            const r = await window.postSession({profile_id: pid, hostname: '127.0.0.1', port, username: 'eng',
                                                password: 'pw', connection_type: 'ssh', display_label: 'sw1'});
            window.createTab(r.data);            // as openProfile() does after a post
        }""", {"pid": pid, "port": device_port})
        await page.wait_for_function("() => (window.getOpenTabs() || []).length === 1", timeout=15000)
        tabs = await page.evaluate("() => window.getOpenTabs()")
        check("the tab carries the profile id", tabs[0].get("profileId") == pid, str(tabs))
        check("  and reports connected", tabs[0].get("isConnected") is True, str(tabs))
        # The tree beside the terminal, with the dashboard hidden, must light
        # without anyone leaving the terminal (#580).
        tree_beside = await page.evaluate("() => { const p = document.getElementById('group-tree'); return !!p && !p.classList.contains('hidden') && p.offsetParent !== null && !window.dashboardVisible(); }")
        if tree_beside:
            try:
                await page.wait_for_selector(".tree-leaf-dot.tree-leaf-live", timeout=3000)
                lit_beside = True
            except Exception:
                lit_beside = False
            check("the dot lights in the tree beside the terminal", lit_beside,
                  str(await page.evaluate("() => [...document.querySelectorAll('.tree-leaf-dot')].map(d => d.className)")))
        else:
            print("  (tree not shown beside the terminal in this layout; skipped)")
        await page.evaluate("() => window.showDashboard()")
        await page.wait_for_selector(".tree-chip", timeout=5000)
        # Open the group so its leaves, and their dots, are drawn.
        if not await page.evaluate("() => !!document.querySelector('.tree-leaf-dot')"):
            await page.click(".tree-chip:not(.tree-chip-ungrouped)")
            await page.wait_for_timeout(300)
        try:
            await page.wait_for_selector(".tree-leaf-dot.tree-leaf-live", timeout=5000)
            lit = True
        except Exception:
            lit = False
        dots = await page.evaluate("() => [...document.querySelectorAll('.tree-leaf-dot')].map(d => d.className)")
        check("the connection's dot lights on the dashboard", lit, str(dots))

        print("\n-- The hover card (#581) --")
        info = await page.evaluate("(sid) => window.tabTooltipInfo(sid)", tabs[0]["sessionId"])
        await page.hover(".tab")
        await page.wait_for_timeout(700)
        card = await page.evaluate("() => { const c = document.querySelector('.tab-tip'); return c ? c.innerText : ''; }")
        check("the card says 'Connected for' once", "connected for connected" not in card.lower()
              and "connected for" in card.lower(), card.replace("\n", " | ")[:200])
        check("  uptime is a bare duration", info and "connected" not in str(info.get("uptime", "")).lower(),
              str(info.get("uptime") if info else info))

        print("\n-- Opened by address alone, the saved connection with that address lights (#580) --")
        await page.evaluate("""async (port) => {
            const r = await window.postSession({hostname: '127.0.0.1', port, username: 'eng',
                                                password: 'pw', connection_type: 'ssh'});
            window.createTab(r.data);
        }""", device_port)
        await page.wait_for_function("() => (window.getOpenTabs() || []).length === 2", timeout=15000)
        await page.wait_for_timeout(1500)          # long enough for the device's prompt to name the tab
        second = (await page.evaluate("() => window.getOpenTabs()"))[1]
        check("the address-only tab has no profile id", not second.get("profileId"), str(second))
        await page.evaluate("(sid) => window.closeTabBySessionId(sid, {force: true})", tabs[0]["sessionId"])
        await page.wait_for_function("() => (window.getOpenTabs() || []).length === 1", timeout=10000)
        await page.evaluate("() => window.showDashboard()")
        try:
            await page.wait_for_selector(".tree-leaf-dot.tree-leaf-live", timeout=5000)
            lit = True
        except Exception:
            lit = False
        check("the dot still lights through the address match", lit, str(second))
        tabs = [second]

        print("\n-- Bulk delete under Ungrouped (#519) --")
        await page.evaluate("""async (port) => {
            for (const [n, host] of [['loose-a', '10.9.9.1'], ['loose-b', '10.9.9.2']]) {
                await fetch('/api/profiles', {method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({name: n, hostname: host, port, username: 'eng', connection_type: 'ssh'})});
            }
        }""", device_port)
        await page.goto(BASE, wait_until="networkidle")
        await page.wait_for_selector(".tree-chip-ungrouped", timeout=5000)
        check("the Ungrouped chip offers Delete all",
              await page.evaluate("() => !!document.querySelector('.tree-chip-ungrouped .tree-chip-action')"))
        if not await page.evaluate("() => !!document.querySelector('.tree-chip-ungrouped')?.closest('.tree-branch')?.querySelector('.tree-leaf')"):
            await page.click(".tree-chip-ungrouped")
            await page.wait_for_timeout(300)
        loose = page.locator(".tree-chip-ungrouped").locator("xpath=ancestor::div[contains(@class,'tree-branch')]").locator(".tree-leaf")
        n = await loose.count()
        check("both loose connections are listed", n >= 2, str(n))
        await loose.nth(0).click(modifiers=["Control"])
        await loose.nth(1).click(modifiers=["Control"])
        selected = await page.evaluate("() => document.querySelectorAll('.tree-leaf-selected').length")
        check("Ctrl+click selects both", selected == 2, str(selected))
        await loose.nth(1).click(button="right")
        await page.wait_for_timeout(300)
        menu = await page.evaluate("() => [...document.querySelectorAll('.context-menu *, .tab-context-menu *, .group-menu *')].map(e => e.textContent.trim()).filter(Boolean).join(' | ')")
        check("the menu speaks for the selection", "Delete 2 connections" in menu, menu[:200])
        await page.keyboard.press("Escape")

        print("\n-- Bulk delete under Ungrouped (#519) --")
        await page.evaluate("""async (port) => {
            for (const n of ['loose-a', 'loose-b']) {
                await fetch('/api/profiles', {method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({name: n, hostname: '10.9.9.' + n.length, port, username: 'eng', connection_type: 'ssh'})});
            }
        }""", device_port)
        await page.goto(BASE, wait_until="networkidle")
        await page.wait_for_selector(".tree-chip-ungrouped", timeout=5000)
        check("the Ungrouped chip offers Delete all",
              await page.evaluate("() => !!document.querySelector('.tree-chip-ungrouped .tree-chip-action')"))
        if not await page.evaluate("() => !!document.querySelector('.tree-chip-ungrouped')?.closest('.tree-branch')?.querySelector('.tree-leaf')"):
            await page.click(".tree-chip-ungrouped")
            await page.wait_for_timeout(300)
        loose = page.locator(".tree-chip-ungrouped").locator("xpath=ancestor::div[contains(@class,'tree-branch')]").locator(".tree-leaf")
        n = await loose.count()
        check("both loose connections are listed", n >= 2, str(n))
        await loose.nth(0).click(modifiers=["Control"])
        await loose.nth(1).click(modifiers=["Control"])
        selected = await page.evaluate("() => document.querySelectorAll('.tree-leaf-selected').length")
        check("Ctrl+click selects both", selected == 2, str(selected))
        await loose.nth(1).click(button="right")
        await page.wait_for_timeout(300)
        menu = await page.evaluate("() => [...document.querySelectorAll('.context-menu *, .tab-context-menu *, .group-menu *')].map(e => e.textContent.trim()).filter(Boolean).join(' | ')")
        check("the menu speaks for the selection", "Delete 2 connections" in menu, menu[:200])
        await page.keyboard.press("Escape")

        print("\n-- Close, and the dot goes dark --")
        await page.evaluate("(sid) => window.closeTabBySessionId(sid, {force: true})", tabs[0]["sessionId"])
        await page.wait_for_function("() => (window.getOpenTabs() || []).length === 0", timeout=10000)
        await page.evaluate("() => window.showDashboard()")
        await page.wait_for_timeout(500)
        live = await page.evaluate("() => document.querySelectorAll('.tree-leaf-dot.tree-leaf-live').length")
        check("no dot is lit after the close", live == 0, str(live))
        await browser.close()


def main() -> int:
    print("=" * 52)
    print("  Dashboard live state and the hover card")
    print("=" * 52)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    time.sleep(3)
    try:
        asyncio.run(main_async())
    except Exception as exc:
        failed.append(f"harness: {exc!r}")
        print(f"  FAIL harness: {exc!r}")
    print("\n" + "=" * 52)
    print(f"  {passed} passed  |  {len(failed)} failed")
    print("=" * 52)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
