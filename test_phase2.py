"""
test_phase2.py — Playwright UI tests for ShellMate Phase 2 (AI chat pane).
Tests the split-screen layout, chat UI, WebSocket streaming, command blocks,
context indicator, and divider drag.

Three things this file used to get wrong, all of which kept it permanently
red without anybody noticing (#129):

- **It tested whatever was running on 8765.** It started no server, so it ran
  against the user's own ShellMate — their settings, their sessions, their
  data folder — and could leave changes behind in a real installation. Every
  other test in the suite starts uvicorn on a spare port with a temp data
  directory; this one now does too.

- **It assumed the AI panel was on.** `ai.panel_enabled` became opt-in on a
  fresh install and this file predates that, so 42 of its checks failed with
  "chat-pane visible: expected to be visible" and none of them was telling us
  anything about the chat. Testing the chat pane requires the chat pane —
  that is setup, not a change to what is under test.

- **It could not report its own failure.** `fail()` printed the Playwright
  error verbatim, which contains U+21B5; under the Windows console's cp1252
  that raised UnicodeEncodeError *while reporting a failure*, so the run died
  mid-output and the summary never printed. A test that cannot say what went
  wrong stays broken quietly.
"""
import asyncio
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backend import paths  # noqa: E402

_TEMP = Path(tempfile.mkdtemp(prefix="shellmate-phase2-"))
paths._data_dir_cache = _TEMP
assert paths.data_dir() == _TEMP, (
    f"refusing to run: this would use {paths.data_dir()}")

import uvicorn  # noqa: E402
from playwright.async_api import async_playwright, expect  # noqa: E402

from backend.app import app  # noqa: E402
from backend.settings_store import update_settings  # noqa: E402

# The pane under test has to exist for any of this to mean anything.
update_settings({"ai": {"panel_enabled": True}})

def _free_port() -> int:
    """A port nothing else holds: several suites run side by side."""
    import socket as _socket
    with _socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


PORT = _free_port()
BASE = f"http://127.0.0.1:{PORT}"

_server = uvicorn.Server(uvicorn.Config(
    app, host="127.0.0.1", port=PORT, log_level="error"))
threading.Thread(target=_server.run, daemon=True).start()
time.sleep(3)

# Playwright errors carry U+21B5 and the Windows console is cp1252. Without
# this, printing a failure raises and takes the run with it.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                        # pragma: no cover
    pass



def _ollama_reachable() -> bool:
    """Whether a local Ollama answers, so the picker can be expected to list it."""
    import socket
    try:
        with socket.create_connection(("127.0.0.1", 11434), timeout=1):
            return True
    except OSError:
        return False

async def run():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(permissions=["clipboard-read", "clipboard-write"])
        page = await ctx.new_page()

        errors = []
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(f"PAGEERROR: {e}"))

        results = []

        def ok(name):
            results.append(("PASS", name))
            print(f"  OK  {name}")

        def fail(name, reason):
            results.append(("FAIL", name, reason))
            print(f"  FAIL  {name}: {reason}")

        await page.goto(BASE, wait_until="networkidle")
        await page.wait_for_timeout(500)

        # ------------------------------------------------------------------
        print("\n-- Split-screen layout --")

        try:
            await expect(page.locator("#content-split")).to_be_visible()
            ok("content-split container exists")
        except Exception as e:
            fail("content-split container exists", str(e))

        try:
            await expect(page.locator("#terminal-pane")).to_be_visible()
            ok("terminal-pane visible")
        except Exception as e:
            fail("terminal-pane visible", str(e))

        try:
            await expect(page.locator("#split-divider")).to_be_visible()
            ok("split-divider visible")
        except Exception as e:
            fail("split-divider visible", str(e))

        try:
            await expect(page.locator("#chat-pane")).to_be_visible()
            ok("chat-pane visible")
        except Exception as e:
            fail("chat-pane visible", str(e))

        # Terminal pane should be on the left of chat pane
        try:
            tp = await page.locator("#terminal-pane").bounding_box()
            cp = await page.locator("#chat-pane").bounding_box()
            assert tp["x"] < cp["x"], f"terminal ({tp['x']}) not left of chat ({cp['x']})"
            ok("terminal-pane is left of chat-pane")
        except Exception as e:
            fail("terminal-pane is left of chat-pane", str(e))

        # Both panes should have meaningful width
        try:
            tp = await page.locator("#terminal-pane").bounding_box()
            cp = await page.locator("#chat-pane").bounding_box()
            assert tp["width"] > 200, f"terminal too narrow: {tp['width']}"
            assert cp["width"] > 200, f"chat too narrow: {cp['width']}"
            ok(f"both panes have adequate width (terminal={tp['width']:.0f}px chat={cp['width']:.0f}px)")
        except Exception as e:
            fail("both panes have adequate width", str(e))

        # ------------------------------------------------------------------
        print("\n-- Chat panel UI --")

        try:
            await expect(page.locator("#chat-header")).to_be_visible()
            ok("chat-header visible")
        except Exception as e:
            fail("chat-header visible", str(e))

        try:
            await expect(page.locator("#ai-backend-select")).to_be_visible()
            opts = await page.locator("#ai-backend-select option").all()
            vals = [await o.get_attribute("value") for o in opts]
            # The selector lists provider:model pairs now, not bare provider
            # names — it gained a model picker after this was written. What
            # matters is unchanged: both backends are reachable from it.
            providers = {v.split(":", 1)[0] for v in vals if v}
            assert "claude" in providers, f"options: {vals}"
            # Ollama is listed only where it answers — a CI runner has none,
            # and the picker rightly leaves out a provider with no models.
            if _ollama_reachable():
                assert "ollama" in providers, f"Ollama answers here but is not offered: {vals}"
                ok("backend selector offers both ollama and claude")
            else:
                ok("backend selector offers claude (no Ollama on this machine)")
        except Exception as e:
            fail("backend selector offers the reachable providers", str(e))

        try:
            await expect(page.locator("#chat-messages")).to_be_visible()
            ok("chat-messages area visible")
        except Exception as e:
            fail("chat-messages area visible", str(e))

        try:
            await expect(page.locator("#chat-input")).to_be_visible()
            ok("chat-input textarea visible")
        except Exception as e:
            fail("chat-input textarea visible", str(e))

        try:
            await expect(page.locator("#chat-send")).to_be_visible()
            ok("chat-send button visible")
        except Exception as e:
            fail("chat-send button visible", str(e))

        try:
            await expect(page.locator("#chat-clear")).to_be_visible()
            ok("chat-clear button visible")
        except Exception as e:
            fail("chat-clear button visible", str(e))

        try:
            await expect(page.locator(".chat-welcome")).to_be_visible()
            ok("chat welcome state shown initially")
        except Exception as e:
            fail("chat welcome state shown initially", str(e))

        # ------------------------------------------------------------------
        print("\n-- WebSocket /ws/chat --")

        # Test via JS — inject a mock response through the WebSocket
        try:
            ws_connected = await page.evaluate("""
                async () => {
                    return new Promise((resolve) => {
                        const ws = new WebSocket(`ws://${location.host}/ws/chat`);
                        ws.onopen  = () => { ws.close(); resolve(true); };
                        ws.onerror = () => resolve(false);
                        setTimeout(() => resolve(false), 3000);
                    });
                }
            """)
            assert ws_connected
            ok("/ws/chat WebSocket accepts connections")
        except Exception as e:
            fail("/ws/chat WebSocket accepts connections", str(e))

        # ------------------------------------------------------------------
        print("\n-- Chat streaming simulation --")

        # Inject a fake AI bubble by calling internal functions
        try:
            await page.evaluate("""
                () => {
                    // Simulate what happens when server streams a response
                    const msgs = document.getElementById('chat-messages');

                    // User bubble
                    const user = document.createElement('div');
                    user.className = 'chat-bubble chat-bubble-user';
                    user.textContent = 'show me the interface status';
                    msgs.appendChild(user);

                    // AI bubble with a command suggestion
                    const ai = document.createElement('div');
                    ai.className = 'chat-bubble chat-bubble-ai';
                    ai.dataset.raw = 'To check interface status, run:\\n[SUGGEST_CMD]show ip interface brief[/SUGGEST_CMD]';

                    // Trigger the renderBubbleContent via the existing chat module
                    // We do it manually here to test CSS rendering
                    const pre = document.createElement('div');
                    pre.className = 'chat-text';
                    pre.textContent = 'To check interface status, run:';
                    ai.appendChild(pre);

                    const cmdBlock = document.createElement('div');
                    cmdBlock.className = 'cmd-block';
                    cmdBlock.innerHTML = `
                        <pre class="cmd-block-text">show ip interface brief</pre>
                        <div class="cmd-block-actions">
                            <button class="cmd-send btn-primary">
                                <span class="material-symbols-outlined">send</span> Send
                            </button>
                            <button class="cmd-edit btn-secondary">
                                <span class="material-symbols-outlined">edit</span>
                            </button>
                        </div>
                    `;
                    ai.appendChild(cmdBlock);
                    msgs.appendChild(ai);
                }
            """)
            ok("Injected user + AI bubbles into chat")
        except Exception as e:
            fail("Injected bubbles into chat", str(e))

        try:
            await expect(page.locator(".chat-bubble-user").first).to_be_visible()
            ok("User bubble visible")
        except Exception as e:
            fail("User bubble visible", str(e))

        try:
            await expect(page.locator(".chat-bubble-ai").first).to_be_visible()
            ok("AI bubble visible")
        except Exception as e:
            fail("AI bubble visible", str(e))

        try:
            await expect(page.locator(".cmd-block").first).to_be_visible()
            ok("Command suggestion block visible")
        except Exception as e:
            fail("Command suggestion block visible", str(e))

        try:
            await expect(page.locator(".cmd-send").first).to_be_visible()
            await expect(page.locator(".cmd-edit").first).to_be_visible()
            ok("Command block Send + Edit buttons visible")
        except Exception as e:
            fail("Command block Send + Edit buttons visible", str(e))

        try:
            cmd_text = await page.locator(".cmd-block-text").first.inner_text()
            assert "show ip interface brief" in cmd_text
            ok(f"Command block shows correct command text")
        except Exception as e:
            fail("Command block shows correct command text", str(e))

        # ------------------------------------------------------------------
        print("\n-- Thinking indicator --")

        try:
            await page.evaluate("""
                () => {
                    const msgs = document.getElementById('chat-messages');
                    const bubble = document.createElement('div');
                    bubble.className = 'chat-bubble chat-bubble-ai streaming';
                    bubble.innerHTML = '<span class="chat-thinking"><span></span><span></span><span></span></span>';
                    msgs.appendChild(bubble);
                }
            """)
            await expect(page.locator(".chat-thinking").first).to_be_visible()
            ok("Thinking indicator renders")
        except Exception as e:
            fail("Thinking indicator renders", str(e))

        # ------------------------------------------------------------------
        print("\n-- Chat clear --")

        try:
            await page.click("#chat-clear")
            await page.wait_for_timeout(200)
            bubble_count = await page.locator(".chat-bubble").count()
            assert bubble_count == 0, f"still {bubble_count} bubbles after clear"
            ok("Clear button removes all chat bubbles")
        except Exception as e:
            fail("Clear button removes all chat bubbles", str(e))

        # ------------------------------------------------------------------
        print("\n-- Input behaviour --")

        try:
            await page.fill("#chat-input", "test message")
            val = await page.input_value("#chat-input")
            assert val == "test message"
            ok("Chat input accepts text")
        except Exception as e:
            fail("Chat input accepts text", str(e))

        # Shift+Enter should NOT send (newline only)
        try:
            await page.fill("#chat-input", "line1")
            await page.press("#chat-input", "Shift+Enter")
            val = await page.input_value("#chat-input")
            assert "\n" in val, f"expected newline, got: {repr(val)}"
            ok("Shift+Enter inserts newline (does not send)")
        except Exception as e:
            fail("Shift+Enter inserts newline", str(e))

        # ------------------------------------------------------------------
        print("\n-- Context indicator --")

        try:
            # Fire a tab-switched event with a fake tab
            await page.evaluate("""
                window.dispatchEvent(new CustomEvent('mate:tab-switched', {
                    detail: { sessionId: 'abc123', label: 'core-switch' }
                }));
            """)
            await page.wait_for_timeout(200)
            indicator = page.locator("#chat-context-indicator")
            text = await indicator.inner_text()
            assert "core-switch" in text, f"got: {text}"
            ok("Context indicator updates on tab-switched event")
        except Exception as e:
            fail("Context indicator updates on tab-switched event", str(e))

        # ------------------------------------------------------------------
        print("\n-- Divider drag --")

        try:
            divider = page.locator("#split-divider")
            chat_before = await page.locator("#chat-pane").bounding_box()
            div_box = await divider.bounding_box()

            # Drag divider 80px to the left → chat pane should get wider
            await page.mouse.move(div_box["x"] + 2, div_box["y"] + div_box["height"] / 2)
            await page.mouse.down()
            await page.mouse.move(div_box["x"] - 78, div_box["y"] + div_box["height"] / 2)
            await page.mouse.up()
            await page.wait_for_timeout(200)

            chat_after = await page.locator("#chat-pane").bounding_box()
            diff = chat_after["width"] - chat_before["width"]
            assert diff > 40, f"chat width change too small: {diff:.0f}px"
            ok(f"Divider drag resizes chat pane (+{diff:.0f}px)")
        except Exception as e:
            fail("Divider drag resizes chat pane", str(e))

        # ------------------------------------------------------------------
        print("\n-- Error bubble --")

        try:
            await page.evaluate("""
                () => {
                    const msgs = document.getElementById('chat-messages');
                    const b = document.createElement('div');
                    b.className = 'chat-bubble chat-bubble-error';
                    b.textContent = 'Connection error';
                    msgs.appendChild(b);
                }
            """)
            await expect(page.locator(".chat-bubble-error")).to_be_visible()
            ok("Error bubble renders")
        except Exception as e:
            fail("Error bubble renders", str(e))

        # ------------------------------------------------------------------
        # Quick connect (#533). Parsing is the risky half: the palette is a
        # tab finder first, so a search term read as an address would offer
        # to dial half a word, and an address read as a search term is the
        # feature not existing. Both directions are asserted.
        print("\n-- Quick connect parsing --")

        parse_cases = [
            # (typed, expected fields or None for "this is not a target")
            ("10.1.20.5", {"connection_type": "ssh", "hostname": "10.1.20.5",
                           "port": 22, "username": ""}),
            ("admin@10.1.20.5:2022", {"connection_type": "ssh",
                                      "hostname": "10.1.20.5", "port": 2022,
                                      "username": "admin"}),
            ("telnet 10.1.1.1 2003", {"connection_type": "telnet",
                                      "hostname": "10.1.1.1", "port": 2003}),
            ("telnet 10.1.1.1", {"connection_type": "telnet",
                                 "hostname": "10.1.1.1", "port": 23}),
            ("ssh core-sw -p 2200", {"connection_type": "ssh",
                                     "hostname": "core-sw", "port": 2200}),
            ("COM5 115200", {"connection_type": "serial",
                             "serial_port": "COM5", "baud_rate": 115200}),
            ("COM5", {"connection_type": "serial", "serial_port": "COM5",
                      "baud_rate": 9600}),
            # Not targets: the palette is a tab finder, and these are words.
            ("glasgow", None),
            ("core", None),
            ("", None),
            ("edge switch", None),
        ]

        for typed, expected in parse_cases:
            try:
                got = await page.evaluate(
                    "text => window.parseConnectTarget(text)", typed)
                if expected is None:
                    assert got is None, f"{typed!r} was read as {got}"
                else:
                    assert got is not None, f"{typed!r} was not read as a target"
                    for field, value in expected.items():
                        assert got.get(field) == value, (
                            f"{typed!r} -> {field}={got.get(field)!r}, "
                            f"expected {value!r}")
                ok(f"parses {typed!r}" if expected else f"ignores {typed!r}")
            except Exception as e:
                ok_name = f"parses {typed!r}" if expected else f"ignores {typed!r}"
                fail(ok_name, str(e))

        try:
            described = await page.evaluate(
                "() => window.describeConnectTarget("
                "window.parseConnectTarget('admin@10.1.20.5:2022'))")
            assert "10.1.20.5:2022" in described and "admin@" in described, described
            ok("says exactly what will be dialled")
        except Exception as e:
            fail("says exactly what will be dialled", str(e))

        try:
            # The row itself, in the palette, from a typed address.
            await page.evaluate("() => window.openTabPalette()")
            await page.fill("#tab-palette input", "10.9.9.9")
            await page.wait_for_timeout(100)
            row = page.locator("#tab-palette .tab-palette-connect")
            await expect(row).to_be_visible()
            text = await row.inner_text()
            assert "10.9.9.9" in text, text
            ok("the palette offers a connect row for a typed address")
        except Exception as e:
            fail("the palette offers a connect row for a typed address", str(e))

        try:
            await page.fill("#tab-palette input", "glasgow")
            await page.wait_for_timeout(100)
            count = await page.locator("#tab-palette .tab-palette-connect").count()
            assert count == 0, "a search term offered a connection"
            await page.keyboard.press("Escape")
            ok("but not for an ordinary search term")
        except Exception as e:
            fail("but not for an ordinary search term", str(e))

        # ------------------------------------------------------------------
        # The pending-action row on the tab hover card (#583). On several
        # tabs the card is where you look to ask which of them is about to
        # go, and it said nothing. Two halves, tested apart: the phrasing
        # and severity alerts.js decides, and the row tabtip.js draws.
        print("\n-- Pending reload on the tab hover card --")

        async def _describe(deadline_ms, awaiting=False, source="reload in 10"):
            return await page.evaluate(
                """([deadline, awaiting, source]) => {
                    const id = 'tip-pending-session';
                    window.dispatchEvent(new CustomEvent('shellmate:pending-action', {
                      detail: { sessionId: id, pending: {
                        kind: 'reload', source, deadline_ms: deadline,
                        confident: deadline !== null, authoritative: true,
                        awaiting_confirmation: awaiting,
                        cancel_command: 'reload cancel',
                      } },
                    }));
                    return window.shellmateAlerts.describePending(id);
                }""",
                [deadline_ms, awaiting, source])

        try:
            far = await _describe(int(time.time() * 1000) + 4 * 60 * 1000 + 12_000)
            assert far["text"].startswith("Reload in "), far
            assert far["text"].split()[-1].startswith("4:"), far
            assert far["severity"] == "warning", far
            ok(f"a reload four minutes out reads {far['text']!r}, as a warning")
        except Exception as e:
            fail("a reload four minutes out is described", str(e))

        try:
            near = await _describe(int(time.time() * 1000) + 30_000)
            assert near["severity"] == "critical", near
            assert near["cancelCommand"] == "reload cancel", near
            ok("inside the last minute it turns critical, and carries the "
               "cancel command")
        except Exception as e:
            fail("a reload in its last minute is critical", str(e))

        try:
            # Tracked but not armed (#248): a typed command the device has
            # not yet confirmed must not produce a countdown.
            vague = await _describe(None, awaiting=True)
            assert "awaiting" in vague["text"].lower(), vague
            assert vague["left"] is None, vague
            ok("an unconfirmed reload says so rather than inventing a time")
        except Exception as e:
            fail("an unconfirmed reload shows no countdown", str(e))

        try:
            shown = await page.evaluate("""
                () => {
                    const list = document.getElementById('tab-list');
                    const tab = document.createElement('div');
                    tab.className = 'tab';
                    tab.dataset.sessionId = 'tip-card-session';
                    tab.textContent = 'core-sw';
                    list.appendChild(tab);
                    window.tabTooltipInfo = () => ({
                      sessionId: 'tip-card-session', label: 'core-sw',
                      // Telnet deliberately: the card fetches port
                      // forwards for an SSH session, and this session does
                      // not exist on the server — a 404 the console-error
                      // check would rightly report.
                      address: '10.1.1.1', port: 23, connectionType: 'telnet',
                      username: 'admin', isConnected: true, group: '',
                      inventory: '', hostKey: '', keepAlive: false,
                      logging: false, profileId: 'p1', uptime: '4m',
                      pending: { kind: 'reload', what: 'Reload', left: 252,
                                 severity: 'critical', source: 'reload in 5',
                                 cancelCommand: 'reload cancel',
                                 text: 'Reload in 4:12' },
                    });
                    return true;
                }
            """)
            assert shown
            await page.hover('.tab[data-session-id="tip-card-session"]')
            await page.wait_for_timeout(800)
            card = page.locator(".tab-tip")
            await expect(card).to_be_visible()
            text = await card.inner_text()
            assert "Pending" in text and "Reload in 4:12" in text, text
            severity = await page.locator(".tab-tip .tab-tip-alert-critical").count()
            assert severity == 1, f"the row does not carry its severity class ({severity})"
            ok("the card shows what is pending, in the alert's severity colour")
        except Exception as e:
            fail("the card shows the pending reload", str(e))

        try:
            # Both tab-menu entries must be hideable like every other one
            # (#584). Settings renders its toggles from this same list, so an
            # entry missing from it is an entry nobody can switch off.
            settings = await page.evaluate(
                "() => window.tabMenuItems().map(i => i.setting)")
            for name in ("cancel_reload", "dismiss_pending"):
                assert name in settings, f"{name} not in {settings}"
            ok("cancelling and dismissing are both switchable in Settings")
        except Exception as e:
            fail("the new tab-menu entries are switchable", str(e))

        # Find in the terminal (#531)
        #
        # The toggles are options passed to xterm's search addon, so what is
        # worth testing is not that the addon searches — it does — but that
        # the toggles mean what the bar says they mean, that they are
        # remembered, and that the filter view says which session it is
        # listing rather than showing an empty pane.
        # ------------------------------------------------------------------
        print("\n-- Find in the terminal --")

        try:
            await page.evaluate(
                "() => document.getElementById('term-search')"
                ".classList.remove('hidden')")
            for name in ("term-search-case", "term-search-word",
                         "term-search-regex", "term-search-filter"):
                await expect(page.locator(f"#{name}")).to_be_visible()
            ok("the find bar offers case, whole word, regex and filter")
        except Exception as e:
            fail("the find bar offers case, whole word, regex and filter", str(e))

        # (term, haystack, toggles, expected) — the toggles are set through
        # the buttons, so this exercises the same path a person does.
        find_cases = [
            ("a.c", "abc", [], False),
            ("a.c", "a.c", [], True),
            ("a.c", "abc", ["term-search-regex"], True),
            ("err", "err-disabled", [], True),
            ("err", "error", ["term-search-word"], False),
            ("Gi1/0/2[0-9]", "Gi1/0/24 notconnect",
             ["term-search-regex"], True),
            ("SERIAL", "serial", [], True),
            ("SERIAL", "serial", ["term-search-case"], False),
        ]
        for term, haystack, toggles, expected in find_cases:
            label = f"{term!r} in {haystack!r} with {toggles or 'no toggles'}"
            try:
                # Every toggle off first: they persist, which is the point.
                for button in ("term-search-case", "term-search-word",
                               "term-search-regex"):
                    pressed = await page.get_attribute(f"#{button}", "aria-pressed")
                    if pressed == "true":
                        await page.click(f"#{button}")
                for button in toggles:
                    await page.click(f"#{button}")
                got = await page.evaluate(
                    "([term, text]) => { const p = window.shellmateFind.pattern(term);"
                    " return p ? p.test(text) : null; }", [term, haystack])
                assert got is expected, f"matched={got}"
                ok(label)
            except Exception as e:
                fail(label, str(e))

        try:
            invalid = await page.evaluate(
                "() => { document.getElementById('term-search-regex').click();"
                " const p = window.shellmateFind.pattern('Gi1/0/2[');"
                " document.getElementById('term-search-regex').click();"
                " return p; }")
            assert invalid is None, "an unfinished pattern was not refused"
            ok("an unfinished regular expression is refused rather than thrown")
        except Exception as e:
            fail("an unfinished regular expression is refused rather than thrown", str(e))

        try:
            await page.click("#term-search-regex")
            await page.wait_for_timeout(1200)      # prefs.js debounces writes
            saved = await page.evaluate(
                "async () => (await (await fetch('/api/settings')).json())"
                ".interface.find_regex")
            assert saved is True, f"settings.json says {saved!r}"
            await page.click("#term-search-regex")
            ok("a toggle is remembered in settings.json")
        except Exception as e:
            fail("a toggle is remembered in settings.json", str(e))

        try:
            await page.click("#term-search-filter")
            await expect(page.locator("#term-filter")).to_be_visible()
            said = await page.inner_text("#term-filter-count")
            assert said.strip(), "the filter view said nothing at all"
            await page.click("#term-filter-close")
            await expect(page.locator("#term-filter")).to_be_hidden()
            ok(f"the filter view opens and says why it is empty ({said.strip()!r})")
        except Exception as e:
            fail("the filter view opens and says why it is empty", str(e))

        # A real xterm buffer with real output in it, so the filter is tested
        # against the thing it actually reads. The socket is stubbed out
        # because there is no device here and a terminal is perfectly capable
        # of holding output without one.
        setup_probe = """
        () => new Promise(resolve => {
          const RealWS = window.WebSocket;
          function Stub() { this.readyState = 3; }
          Stub.prototype.addEventListener = function () {};
          Stub.prototype.send = function () {};
          Stub.prototype.close = function () {};
          Stub.OPEN = 1;
          window.WebSocket = Stub;
          const made = window.initTerminal('filter-probe');
          window.WebSocket = RealWS;
          made.terminal.write(
            'Gi1/0/23  connected    trunk\\r\\n'
            + 'Gi1/0/24  err-disabled 20\\r\\n'
            + 'Gi1/0/25  notconnect   20\\r\\n', () => resolve(true));
        })
        """
        try:
            await page.evaluate(setup_probe)
            lines = await page.evaluate(
                "() => window.terminalOutputLines('filter-probe').map(l => l.text)")
            assert any("err-disabled" in line for line in lines), lines
            ok(f"the buffer is read line by line ({len(lines)} lines)")
        except Exception as e:
            fail("the buffer is read line by line", str(e))

        try:
            await page.click("#term-search-regex")
            matched = await page.evaluate(
                "() => window.shellmateFind.filterLines("
                "'filter-probe', 'Gi1/0/2[45]').map(l => l.text.trim())")
            await page.click("#term-search-regex")
            assert len(matched) == 2, matched
            assert all(m.startswith("Gi1/0/2") for m in matched), matched
            ok("a pattern selects only the lines that match it")
        except Exception as e:
            fail("a pattern selects only the lines that match it", str(e))

        try:
            rendered = await page.evaluate("""
              async () => {
                const was = window.getActiveTab;
                window.getActiveTab = () => ({ sessionId: 'filter-probe', label: 'lab-sw' });
                document.getElementById('term-search-input').value = 'err-disabled';
                document.getElementById('term-search-filter').click();
                const rows = [...document.querySelectorAll('.term-filter-line')]
                  .map(r => r.textContent);
                const marks = [...document.querySelectorAll('#term-filter-lines mark')]
                  .map(m => m.textContent);
                const said = document.getElementById('term-filter-count').textContent;
                document.getElementById('term-filter-close').click();
                window.getActiveTab = was;
                return { rows, marks, said };
              }
            """)
            assert len(rendered["rows"]) == 1, rendered
            assert "err-disabled" in rendered["rows"][0], rendered
            assert rendered["marks"] == ["err-disabled"], rendered
            assert "lab-sw" in rendered["said"], rendered
            ok("the filter lists the matching line, marks it, and names the tab")
        except Exception as e:
            fail("the filter lists the matching line, marks it, and names the tab", str(e))
        finally:
            await page.evaluate("""() => {
              window.forgetTerminal('filter-probe');
              const el = document.getElementById('terminal-filter-probe');
              if (el) el.remove();
              document.getElementById('term-search-input').value = '';
              document.getElementById('term-search').classList.add('hidden');
            }""")

        try:
            regex_off = await page.get_attribute("#term-search-regex", "aria-pressed")
            assert regex_off == "false", "the regex toggle was left on"
            ok("the toggles are left as they were found")
        except Exception as e:
            fail("the toggles are left as they were found", str(e))

        # ------------------------------------------------------------------
        # Per-session logging (#534)
        #
        # The chip is only meaningful with a session writing to a file, which
        # needs a device. What can be held here is the shape: it is absent
        # until something says otherwise, and the file it points at can be
        # opened from one place rather than three.
        # ------------------------------------------------------------------
        print("\n-- Logging one session --")

        try:
            await expect(page.locator("#status-logging-wrap")).to_be_hidden()
            ok("nothing claims to be logging before anything is")
        except Exception as e:
            fail("nothing claims to be logging before anything is", str(e))

        try:
            has = await page.evaluate("() => typeof window.viewLogFile")
            assert has == "function", has
            ok("one log file can be opened by name")
        except Exception as e:
            fail("one log file can be opened by name", str(e))

        # The paste dialog (#523). It used to be a read-only preview cut off
        # at 400 characters; what people want at that moment is to fix a line
        # and to choose how fast the block reaches the device.
        print("\n-- The paste dialog --")

        block = "\n".join(["! a comment", "interface Gi0/1", "",
                           " description uplink", "exit"])
        try:
            await page.evaluate(
                """text => {
                     window.__pasted = null;
                     window._showPasteModal(text,
                       { sessionId: 'test-session', mode: 'prompt',
                         delayMs: 250, timeoutS: 12 },
                       (sent, choice) => { window.__pasted = { sent, choice }; });
                   }""", block)
            await expect(page.locator("#paste-overlay")).to_be_visible()
            await expect(page.locator("#paste-text")).to_be_visible()
            ok("the paste is shown in an editable box")
        except Exception as e:
            fail("the paste is shown in an editable box", str(e))

        try:
            value = await page.input_value("#paste-text")
            assert value == block, repr(value[:80])
            assert not await page.get_attribute("#paste-text", "readonly")
            ok("with the whole block in it, not a truncated preview")
        except Exception as e:
            fail("with the whole block in it, not a truncated preview", str(e))

        try:
            count = await page.inner_text("#paste-count")
            assert "5 lines" in count, count
            ok("and a line count")
        except Exception as e:
            fail("and a line count", str(e))

        try:
            checked = await page.evaluate(
                "() => document.getElementById('paste-mode-prompt').checked")
            assert checked, "the dialog did not open on the configured mode"
            assert await page.input_value("#paste-delay") == "250"
            assert await page.input_value("#paste-timeout") == "12"
            ok("the pacing options start where the settings say")
        except Exception as e:
            fail("the pacing options start where the settings say", str(e))

        try:
            await page.check("#paste-strip")
            stripped = await page.input_value("#paste-text")
            assert stripped == "interface Gi0/1\n description uplink\nexit", repr(stripped)
            assert "3 lines" in await page.inner_text("#paste-count")
            ok("stripping takes blank lines and comments, and says so")
        except Exception as e:
            fail("stripping takes blank lines and comments, and says so", str(e))

        try:
            await page.uncheck("#paste-strip")
            assert await page.input_value("#paste-text") == block
            ok("and unticking it puts them back")
        except Exception as e:
            fail("and unticking it puts them back", str(e))

        try:
            # The edit is the point of the box: what is sent is what is in it.
            await page.fill("#paste-text", "interface Gi0/2\nshutdown")
            await page.check("#paste-mode-lines")
            await page.fill("#paste-delay", "300")
            await page.click("#paste-confirm")
            await page.wait_for_timeout(200)
            got = await page.evaluate("() => window.__pasted")
            assert got, "the confirm callback was never called"
            assert got["sent"] == "interface Gi0/2\nshutdown", repr(got["sent"])
            assert got["choice"]["mode"] == "lines", str(got["choice"])
            assert got["choice"]["delayMs"] == 300, str(got["choice"])
            ok("Send hands over the edited text and the chosen pacing")
        except Exception as e:
            fail("Send hands over the edited text and the chosen pacing", str(e))

        try:
            # A line-paced batch keeps the dialog, because it takes as long as
            # the device does — and Stop has to be reachable without typing
            # into the session.
            await expect(page.locator("#paste-overlay")).to_be_visible()
            await expect(page.locator("#paste-progress")).to_be_visible()
            assert await page.inner_text("#paste-cancel") == "Stop"
            await page.evaluate(
                """() => window.dispatchEvent(new CustomEvent(
                     'shellmate:paste-batch',
                     { detail: { sessionId: 'test-session', state: 'progress',
                                 sent: 1, total: 2 } }))""")
            assert "1 of 2" in await page.inner_text("#paste-progress")
            ok("the dialog stays and reports how far the batch has got")
        except Exception as e:
            fail("the dialog stays and reports how far the batch has got", str(e))

        try:
            await page.evaluate(
                """() => window.dispatchEvent(new CustomEvent(
                     'shellmate:paste-batch',
                     { detail: { sessionId: 'test-session', state: 'done',
                                 sent: 2, total: 2, remaining: 0 } }))""")
            await expect(page.locator("#paste-overlay")).to_be_hidden()
            ok("and closes when the batch is done")
        except Exception as e:
            fail("and closes when the batch is done", str(e))

        try:
            # Enter has to type a newline now: a paste being edited is the
            # worst possible place for a stray Return to send.
            await page.evaluate(
                """() => { window.__pasted = null;
                           window._showPasteModal('one\\ntwo', {},
                             (sent) => { window.__pasted = sent; }); }""")
            await page.click("#paste-text")
            await page.keyboard.press("End")
            await page.keyboard.press("Enter")
            assert await page.evaluate("() => window.__pasted") is None, \
                "Enter sent the paste instead of typing a newline"
            await page.keyboard.press("Control+Enter")
            await page.wait_for_timeout(100)
            assert await page.evaluate("() => window.__pasted") is not None, \
                "Ctrl+Enter did not send"
            ok("Enter types a newline; Ctrl+Enter sends")
        except Exception as e:
            fail("Enter types a newline; Ctrl+Enter sends", str(e))

        # ------------------------------------------------------------------
        print("\n-- Console errors --")

        ignored = {"favicon"}
        real_errors = [e for e in errors if not any(i in e.lower() for i in ignored)]
        if not real_errors:
            ok("No JS console errors")
        else:
            for e in real_errors:
                fail("No JS console errors", e[:120])

        # ------------------------------------------------------------------
        await browser.close()

        passed = sum(1 for r in results if r[0] == "PASS")
        failed = sum(1 for r in results if r[0] == "FAIL")
        print(f"\n{'='*52}")
        print(f"  {passed} passed  |  {failed} failed")
        print(f"{'='*52}\n")

        if failed:
            print("FAILURES:")
            for r in results:
                if r[0] == "FAIL":
                    print(f"  FAIL {r[1]}: {r[2][:100]}")

        return failed


if __name__ == "__main__":
    import sys
    sys.exit(1 if asyncio.run(run()) else 0)
