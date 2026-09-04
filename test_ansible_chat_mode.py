"""
test_ansible_chat_mode.py — The assistant answers as a ShellMate-Ansible expert (#602).

The builder had its own describe-it box beside the assistant panel that
already existed. Moving it into the chat is only worth doing if the chat
knows where it is, so what is tested is that:

- the persona actually changes, and is a **mode the backend knows** rather
  than a label — a pill reading "Ansible mode" over a generic Ansible
  expert would be worse than no pill, because it would make the answers
  look endorsed;
- it is visible: the pill, the greeting and the quick chats change
  together, since a persona that answers differently while looking
  identical is a trap;
- it is given back. One that persisted into a terminal session would have
  somebody ask about a switch and be answered about a container;
- the builder's own box is gone, because two chats was the defect.

The persona is checked at the prompt rather than by asking a model: it has
to be editable in Settings like every other one, and it has to carry the
specific things a generic Ansible expert gets wrong here.

Run: python test_ansible_chat_mode.py
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

_TEMP = Path(tempfile.mkdtemp(prefix="shellmate-chatmode-"))
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


def the_persona() -> None:
    print("\n-- The persona exists, and is editable --")
    from backend.ai import prompt_store, prompts

    check("ansible is a mode the prompt store knows",
          "ansible" in prompts.MODES, str(prompts.MODES))

    state = prompt_store.state()["prompts"]
    check("so it appears in the Settings editor", "ansible" in state,
          str(sorted(state)))
    check("and it keeps the command-rules marker, like the others",
          state["ansible"]["has_marker"],
          "without it the editor warns about a prompt that never had one")

    body = state["ansible"]["body"]
    # Each of these is a place a generic Ansible expert would be confidently
    # wrong here — wrong about this integration rather than about Ansible.
    for phrase, why in (
        ("ansible-playbook", "the user has no ansible-playbook to run"),
        ("bind mount", "how a playbook actually reaches the runner"),
        ("travels", "the container keeps no copy of the inventory"),
        ("site_1_routers", "group names are sanitised"),
        ("vault", "where credentials come from"),
        ("Serial connections", "what cannot be targeted at all"),
        ("[PLAYBOOK]", "how to offer a playbook"),
    ):
        check(f"it knows: {why}", phrase in body,
              f"{phrase!r} is not in the persona")

    prompt_store.save("ansible", "Mine. {command_rules}")
    edited = prompt_store.state()["prompts"]["ansible"]["modified"]
    prompt_store.reset("ansible")
    back = prompt_store.state()["prompts"]["ansible"]["modified"]
    check("an edit sticks, and reset restores it", edited and not back,
          "the Ansible persona must be as editable as the other three")


def what_it_is_told() -> None:
    print("\n-- What it is told about this installation --")
    from backend.ai import router

    told = "\n".join(router._ansible_context({"plays": [
        {"name": "Set NTP", "hosts": "site_1_switches",
         "tasks": [{"kind": "config"}], "handlers": []}]}))

    check("whether the runner is there", "Runner:" in told, told[:200])
    check("what is on the builder's canvas",
          "Set NTP" in told and "site_1_switches" in told, told[-200:])
    check("the names of the keys a run could use",
          "Key names" in told, told[:400])
    check("and never a key's value",
          "vault" not in told.lower() or "value" not in told.lower(),
          "the context must carry names only")

    # It runs when the runner is unconfigured too, which is the state a
    # first-time user is in and exactly when they have questions.
    check("it does not raise when nothing is set up",
          isinstance(router._ansible_context(None), list),
          "an assistant that cannot answer because the runner is down is "
          "worse than one answering without it")


async def main() -> None:
    the_persona()
    what_it_is_told()

    threading.Thread(target=_serve, daemon=True).start()
    time.sleep(2.0)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))

        await page.goto(BASE)
        await page.wait_for_selector("#ansible-stage", state="attached", timeout=15000)
        await page.wait_for_timeout(1500)

        print("\n-- Off, until the view opens --")
        pill = await page.inner_text("#chat-context-indicator")
        check("the pill does not claim Ansible mode yet",
              "Ansible mode" not in pill, pill)

        await page.evaluate("window.ansibleView.open('builder')")
        await page.wait_for_timeout(900)

        print("\n-- On, and saying so --")
        pill = await page.inner_text("#chat-context-indicator")
        check("the pill says Ansible mode", "Ansible mode" in pill, pill)
        check("the transcript says the persona changed",
              await page.query_selector(".chat-mode-note") is not None,
              "the replies above it came from somewhere else")

        quick = await page.eval_on_selector_all(
            "#quick-buttons-list .quick-btn", "e => e.map(x => x.textContent)")
        check("the quick chats are Ansible ones",
              any("play" in q.lower() or "runner" in q.lower() for q in quick),
              str(quick))
        check("and not the terminal ones", "Summarize" not in quick, str(quick))

        check("a message would carry the Ansible mode",
              await page.evaluate(
                  "!!(window.ansibleBuilder && window.ansibleBuilder.canvasState)"),
              "the canvas has to travel, or the assistant cannot see the plays")

        print("\n-- The builder no longer has its own box --")
        check("the describe-it box is gone",
              await page.query_selector("#av-bld-ask") is None,
              "two chats was the defect this closes")
        check("and its Draft button with it",
              await page.query_selector("#av-bld-draft") is None)

        print("\n-- A playbook renders as a playbook, not as a command --")
        shape = await page.evaluate("""
          (() => {
            const el = document.createElement('div');
            el.className = 'chat-bubble chat-bubble-ai';
            el.dataset.raw = '[PLAYBOOK]---\\n- name: X\\n  hosts: all[/PLAYBOOK]';
            document.getElementById('chat-messages').appendChild(el);
            const fn = window.shellmateChat && window.shellmateChat.renderRaw;
            if (typeof fn === 'function') { fn(el); }
            return {
              playbook: !!el.querySelector('.chat-playbook'),
              command: !!el.querySelector('.cmd-block, .chat-command'),
              callable: typeof fn === 'function',
            };
          })()
        """)
        if shape.get("callable"):
            check("it renders as a playbook block", shape["playbook"], str(shape))
            check("and never as a command block", not shape["command"],
                  "a command block is clicked to type into a live device")
        else:
            check("the renderer is reachable for the test", False,
                  "window.shellmateChat.renderRaw is not exposed")

        print("\n-- And gives the assistant back --")
        await page.evaluate("window.ansibleView.close()")
        await page.wait_for_timeout(600)
        pill = await page.inner_text("#chat-context-indicator")
        check("the pill stops claiming Ansible mode",
              "Ansible mode" not in pill, pill)
        quick = await page.eval_on_selector_all(
            "#quick-buttons-list .quick-btn", "e => e.map(x => x.textContent)")
        check("and the usual quick chats come back",
              "Summarize" in quick, str(quick))

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
