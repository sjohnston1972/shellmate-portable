"""
test_tool_ui.py — The approval gate, in the browser (#560).

The backend tests prove a tool request stops the turn. This proves the
browser then puts a *button* in front of a person rather than doing
anything — which is the same claim made one layer up, and the layer where
it would actually be lost.

Three things are checked, and the first is the whole feature:

**A tool request renders as a command block and runs nothing.** No
terminal socket is opened, no command is injected, and the block carries
the call id so the answer can be tied back to the request.

**It is bound to the session the question was asked about**, not to
whatever tab is active when somebody clicks — the wrong-session approval
that #308 and #316 exist to prevent.

**Declining is a real answer.** The model is told, rather than left
waiting for a result that never comes.

Run: python test_tool_ui.py
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

_TEMP = Path(tempfile.mkdtemp(prefix="shellmate-tool-ui-"))
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


REQUEST = {
    "type": "tool_request",
    "shape": "anthropic",
    "text": "Let me look at the interfaces. ",
    "answered": [],
    "read_only_calls": [],
    "calls": [{"id": "call-1", "name": "run_command",
               "arguments": {"command": "show ip interface brief",
                             "why": "to see which interfaces are down"}}],
}


async def main() -> None:
    threading.Thread(target=_serve, daemon=True).start()
    time.sleep(2.0)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))

        await page.goto(BASE)
        await page.wait_for_selector("#chat-messages", state="attached",
                                     timeout=15000)

        # An assistant bubble bound to a session, as a real answer would be.
        # The request is then delivered through the same function the
        # socket calls — the entry point chat.js exports for exactly this
        # and for replaying a saved conversation, not a test-only hook.
        await page.evaluate("""() => {
          const messages = document.getElementById('chat-messages');
          const bubble = document.createElement('div');
          bubble.className = 'chat-bubble chat-bubble-ai';
          bubble.dataset.contextSession = 'sess-abc';
          messages.appendChild(bubble);
        }""")

        delivered = await page.evaluate(
            "r => (typeof window.shellmateChatMessage === 'function')"
            "  ? (window.shellmateChatMessage({data: JSON.stringify(r)}), true)"
            "  : false", REQUEST)

        check("the socket entry point is reachable", delivered is True,
              "without it the approval gate is only ever tested one "
              "layer down")
        if not delivered:
            await browser.close()
            _report()
            return

        await page.wait_for_timeout(200)

        print("\n-- It renders as a command block --")
        block = await page.query_selector(".cmd-block-tool")
        check("a tool command block is rendered", block is not None)
        text = await page.inner_text("#chat-messages")
        check("the command is shown", "show ip interface brief" in text, text[:200])
        check("and the reason the model gave",
              "to see which interfaces are down" in text, text[:300])

        check("it carries the call id, so the answer ties back",
              await page.eval_on_selector(
                  ".cmd-block-tool", "e => e.dataset.toolCallId") == "call-1")
        check("it is bound to the session the question was about",
              await page.eval_on_selector(
                  ".cmd-block-tool", "e => e.dataset.targetSession") == "sess-abc",
              "binding at click time is how a command meant for a core "
              "switch reaches a firewall")

        print("\n-- Nothing has run --")
        # No tabs are open, so anything that tried to inject would have
        # appended "No active terminal session to send command to."
        # Its absence is the proof that rendering the block ran nothing.
        check("no tabs are open, so an injection would be visible",
              await page.evaluate(
                  "() => (window.getOpenTabs ? window.getOpenTabs().length : 0)")
              == 0)
        check("nothing tried to reach a terminal",
              "No active terminal session" not in text,
              "the whole gate is that a tool call is a request, not a run")

        print("\n-- The controls --")
        check("there is a Send button", await page.query_selector(
            ".cmd-block-tool .cmd-send") is not None)
        check("an Edit button, so the command can be corrected first",
              await page.query_selector(".cmd-block-tool .cmd-edit") is not None)
        check("and a Decline, so the model is told rather than left waiting",
              await page.query_selector(".cmd-block-tool .cmd-decline") is not None)

        print("\n-- Declining --")
        # Dispatched rather than clicked: the AI panel is collapsed in a
        # fresh profile, so the button is present and not visible.
        # Whether the panel is open is a different feature; what is
        # under test is what the button does.
        await page.evaluate(
            "() => document.querySelector('.cmd-block-tool .cmd-decline').click()")
        await page.wait_for_timeout(200)
        check("the block is struck through once declined",
              await page.eval_on_selector(
                  ".cmd-block-tool",
                  "e => e.classList.contains('cmd-block-declined')") is True)
        check("and its buttons are disabled, so it cannot also be sent",
              await page.eval_on_selector_all(
                  ".cmd-block-tool button", "e => e.every(b => b.disabled)") is True,
              "a declined request that can still be approved is two answers "
              "to one question")

        print("\n-- Nothing threw --")
        real = [e for e in errors if "favicon" not in e.lower()]
        check("no script errors along the way", not real, "; ".join(real[:3]))
        await browser.close()

    _report()


def _report() -> None:
    print("\n" + "=" * 52)
    print(f"  {passed} passed  |  {len(failed)} failed")
    print("=" * 52)
    for line in failed:
        print("  -", line)


if __name__ == "__main__":
    asyncio.run(main())
    sys.exit(1 if failed else 0)
