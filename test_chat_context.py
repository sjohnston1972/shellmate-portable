"""
test_chat_context.py — What the assistant is actually shown, per context mode.

Choosing sessions in the tab picker looked like it did nothing (#213), and the
break was not where it seemed. The typed-message path worked; what failed was
everything around it. The auto-analysis path sent no selection at all, so the
choice "wore off" after the first approved command. And the selection was sent
*as* the open-tab list, which renumbered the assistant's session summary over
the subset — its tab numbers stopped matching the tab bar, and a
[SUGGEST_CMD:N] resolved against the wrong tab.

So the selection now travels in its own field, `context_session_ids`, and
these tests hold the contract at the router: the summary always describes the
real tab bar, the extras are exactly the chosen sessions, and the old spelling
(selection in `open_session_ids`) still works for a page that has not been
reloaded.

    python test_chat_context.py
"""

import asyncio
import re
import shutil
import sys
import tempfile
from pathlib import Path

from backend import paths

_TEMP = Path(tempfile.mkdtemp(prefix="shellmate-chat-context-"))
paths._data_dir_cache = _TEMP

from backend.ai import ollama_client, router                # noqa: E402
from backend.session.buffer import SessionBuffer            # noqa: E402

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


# ---------------------------------------------------------------------------
# Harness: three sessions, a stub manager, and a spy where the prompt is built
# ---------------------------------------------------------------------------

def _session(sid: str, label: str) -> dict:
    buffer = SessionBuffer(sid)
    buffer.write(f"{label}# show version\noutput from {label}\n")
    return {
        "session_id": sid,
        "display_label": label,
        "hostname": label,
        "connection_type": "ssh",
        "buffer": buffer,
    }


class StubManager:
    def __init__(self, sessions: dict) -> None:
        self._sessions = sessions

    def get_all_sessions(self) -> list[dict]:
        return list(self._sessions.values())

    def get_session(self, sid: str):
        return self._sessions.get(sid)


SESSIONS = {sid: _session(sid, label) for sid, label in
            [("s1", "rtr1"), ("s2", "rtr2"), ("s3", "rtr3")]}
MANAGER = StubManager(SESSIONS)


async def _fake_stream(message, context_block, model=None, system_prompt=None, history=None):
    yield "ok"


def run(context_mode: str,
        open_ids: list | None,
        context_ids: list | None,
        active: str = "s1") -> dict:
    """Drive stream_chat and capture what reached build_context_prompt."""
    captured: dict = {}
    original = router.build_context_prompt

    def spy(sessions_summary, active_buffer, active_label,
            command_history, extra_contexts=None, **kwargs):
        captured["summary"] = sessions_summary
        captured["extras"] = [e["label"] for e in (extra_contexts or [])]
        return ""

    router.build_context_prompt = spy
    ollama_client.stream_response = _fake_stream
    try:
        async def drive():
            async for _ in router.stream_chat(
                message="hi",
                active_session_id=active,
                backend="ollama",
                context_mode=context_mode,
                session_manager=MANAGER,
                open_session_ids=open_ids,
                context_session_ids=context_ids,
            ):
                pass
        asyncio.run(drive())
    finally:
        router.build_context_prompt = original
    return captured


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_selection_reaches_the_prompt() -> None:
    print("\n-- The picker's choice reaches the prompt --")
    got = run("selected", ["s1", "s2", "s3"], ["s2", "s3"])
    check("the chosen sessions arrive as extras",
          got["extras"] == ["rtr2", "rtr3"], repr(got["extras"]))


def test_summary_keeps_real_tab_numbers() -> None:
    print("\n-- Tab numbers still describe the tab bar --")
    got = run("selected", ["s1", "s2", "s3"], ["s3"])
    numbering = [(s["tab_num"], s["label"]) for s in got["summary"]]
    check("the summary covers every open tab, in tab-bar order",
          numbering == [(1, "rtr1"), (2, "rtr2"), (3, "rtr3")],
          f"{numbering} — a summary renumbered over the selection sends "
          f"[SUGGEST_CMD:N] to the wrong tab")


def test_active_session_is_not_doubled() -> None:
    print("\n-- Choosing the active tab does not send it twice --")
    got = run("selected", ["s1", "s2", "s3"], ["s1", "s2"])
    check("the active session appears only as the active buffer",
          got["extras"] == ["rtr2"], repr(got["extras"]))


def test_old_spelling_still_works() -> None:
    print("\n-- A page from before the field split --")
    # An un-reloaded page still sends the selection as open_session_ids and
    # nothing in context_session_ids.
    got = run("selected", ["s2", "s3"], None)
    check("the selection is honoured from open_session_ids",
          got["extras"] == ["rtr2", "rtr3"], repr(got["extras"]))


def test_active_mode_sends_no_extras() -> None:
    print("\n-- The default stays lean --")
    got = run("active", ["s1", "s2", "s3"], None)
    check("no extras when following the active tab",
          got["extras"] == [], repr(got["extras"]))


def test_frontend_wiring() -> None:
    """
    The browser half, read out of the source.

    The auto-analysis path regressing to "active tab only" is invisible at
    runtime — the reply still arrives, just built from less — which is how
    it shipped broken the first time.
    """
    print("\n-- The browser sends the same choice on every path --")
    root = Path(__file__).parent

    chat = (root / "frontend" / "js" / "chat.js").read_text(encoding="utf-8")

    sends = re.findall(r"chatWs\.send\(JSON\.stringify\(\{(.*?)\}\)\);",
                       chat, re.S)
    carrying = [s for s in sends if "context_session_ids" in s]
    check("both send sites exist to check", len(sends) >= 2,
          f"only {len(sends)} chatWs.send sites found — the pattern is blind")
    check("every send carries the picker's selection",
          len(carrying) == len(sends),
          f"{len(sends) - len(carrying)} send site(s) drop "
          f"context_session_ids — that path reverts to the active tab alone")

    backend_src = (root / "backend" / "app.py").read_text(encoding="utf-8")
    check("the websocket handler passes the field through",
          "context_session_ids=context_session_ids" in backend_src)


def main() -> int:
    print("\n" + "=" * 52)
    print("  Chat context")
    print("=" * 52)

    for test in (
        test_selection_reaches_the_prompt,
        test_summary_keeps_real_tab_numbers,
        test_active_session_is_not_doubled,
        test_old_spelling_still_works,
        test_active_mode_sends_no_extras,
        test_frontend_wiring,
    ):
        try:
            test()
        except Exception as exc:
            failed.append(f"{test.__name__} raised {exc!r}")
            print(f"  FAIL {test.__name__} raised\n       {exc!r}")

    print("\n" + "-" * 52)
    if failed:
        print(f"  {passed} passed, {len(failed)} FAILED")
        for line in failed:
            print(f"    - {line}")
    else:
        print(f"  all {passed} checks passed")
    print("-" * 52)

    shutil.rmtree(_TEMP, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
