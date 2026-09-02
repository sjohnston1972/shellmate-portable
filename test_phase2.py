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

PORT = 8766
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
