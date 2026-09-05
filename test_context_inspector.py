"""
test_context_inspector.py — Showing what the assistant saw (#553).

The model is told to say when it cannot see enough, and until now "I
cannot see that" was an assertion nobody could check. Two halves fix that,
and both are tested here:

**The horizon.** The buffer knows how many lines it is holding, how many it
has evicted, and when the oldest visible one arrived. The heading over the
terminal output says so, so the model's claim becomes a claim about a
stated boundary.

**The block itself.** The exact string the provider received reaches the
browser, so somebody can read it. That it is *the same string* is the whole
value: a reconstruction would show what ShellMate believes it sent, which
is precisely the thing under question. It is also where redaction is
provable rather than asserted.

    python test_context_inspector.py
"""

import asyncio
import shutil
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

from backend import paths

_TEMP = Path(tempfile.mkdtemp(prefix="shellmate-context-"))
paths._data_dir_cache = _TEMP

from backend.ai import prompts, router                     # noqa: E402
from backend.session.buffer import SessionBuffer           # noqa: E402

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

def test_the_buffer_knows_its_horizon() -> None:
    print("\n-- What is out of sight --")

    buf = SessionBuffer("s", max_lines=100)
    for n in range(40):
        buf.write(f"line {n}\n")

    h = buf.horizon(200)
    check("asking for more than exists gives what exists",
          h["visible"] == 40, str(h))
    check("with nothing hidden", h["hidden"] == 0, str(h))
    check("and a time for the oldest visible line", h["since"] > 0)

    h = buf.horizon(10)
    check("a smaller window hides the rest",
          h["visible"] == 10 and h["hidden"] == 30, str(h))

    # Past the buffer's own limit: evicted lines are hidden too, and
    # counting only the ones still in memory would understate it.
    buf = SessionBuffer("s", max_lines=50)
    for n in range(500):
        buf.write(f"line {n}\n")
    h = buf.horizon(20)
    check("evicted lines count as hidden",
          h["hidden"] == 480, str(h))
    check("and the total is the whole session, not the buffer",
          h["total"] == 500, str(h))

    check("an empty buffer claims nothing",
          SessionBuffer("s").horizon(200)["visible"] == 0)


def test_the_oldest_visible_time_moves_with_the_window() -> None:
    """The time has to be the oldest *visible* line, not the oldest kept."""
    print("\n-- When the window starts --")

    buf = SessionBuffer("s", max_lines=100)
    buf.write("old\n")
    time.sleep(0.05)
    buf.write("new\n")

    wide = buf.horizon(10)["since"]
    narrow = buf.horizon(1)["since"]
    check("a narrower window starts later", narrow > wide,
          f"wide={wide} narrow={narrow}")


def test_the_heading_says_what_is_missing() -> None:
    print("\n-- The heading --")

    line = prompts._horizon_line({"visible": 200, "hidden": 1400,
                                  "since": time.time()})
    check("it counts what is visible", "200 most recent" in line, line)
    check("and says when that starts", "from " in line, line)
    check("and names what is not visible", "1400 earlier line(s) are NOT" in line,
          line)
    check("and tells the model what to do about it",
          "say so rather than guessing" in line, line)

    line = prompts._horizon_line({"visible": 12, "hidden": 0,
                                  "since": time.time()})
    check("with nothing hidden it does not invent a warning",
          "NOT visible" not in line, line)

    check("with no horizon at all it falls back quietly",
          prompts._horizon_line(None) == "--- Terminal output ---")
    check("and so does a horizon with nothing in it",
          prompts._horizon_line({"visible": 0}) == "--- Terminal output ---")


def test_the_heading_reaches_the_context_block() -> None:
    print("\n-- In the block --")

    block = prompts.build_context_prompt(
        [], "sw1#show version\nCisco IOS", "sw1", [],
        horizon={"visible": 2, "hidden": 900, "since": time.time()})
    check("the block carries the horizon",
          "900 earlier line(s) are NOT visible" in block, block[:400])
    check("and still carries the output",
          "Cisco IOS" in block)

    plain = prompts.build_context_prompt([], "x", "sw1", [])
    check("a caller that passes none gets the plain heading",
          "--- Terminal output ---" in plain, plain[:200])


def test_the_block_reaches_the_browser_first() -> None:
    """
    Before the first chunk, so it is on the bubble whether or not the
    answer finishes — a reply that failed halfway is exactly the one
    somebody wants to inspect.
    """
    print("\n-- To the browser --")

    import backend.ai.claude_client as claude
    real = claude.stream_response

    async def fake(message, context_block, model=None, system_prompt=None,
                   history=None, tools=None, prior=None):
        yield "an answer"

    claude.stream_response = fake

    class Manager:
        def get_all_sessions(self):
            return []

        def get_session(self, _sid):
            return None

    async def drive():
        out = []
        async for item in router.stream_chat(
            message="why?", active_session_id=None, backend="claude",
            context_mode="active", session_manager=Manager(),
        ):
            out.append(item)
        return out

    try:
        got = asyncio.run(drive())
    finally:
        claude.stream_response = real

    check("the context is the first thing yielded",
          isinstance(got[0], dict) and "context" in got[0], str(got[0])[:120])
    check("it is a dict, not text in the reply",
          not any(isinstance(x, str) and "ACTIVE SESSION" in x for x in got),
          "it would otherwise be read out to the user as part of the answer")
    check("the answer still arrives after it",
          "an answer" in [x for x in got if isinstance(x, str)], str(got))


def test_what_is_shown_is_what_was_sent() -> None:
    """
    The claim the whole feature makes.

    A reconstruction would show what ShellMate believes it sent. The value
    here is that it is the same object — so if redaction had not run, the
    inspector would show that rather than hide it.
    """
    print("\n-- The same string --")

    import backend.ai.claude_client as claude
    real = claude.stream_response
    seen = {}

    async def fake(message, context_block, model=None, system_prompt=None,
                   history=None, tools=None, prior=None):
        seen["sent"] = context_block
        yield "ok"

    claude.stream_response = fake

    class Manager:
        def get_all_sessions(self):
            return []

        def get_session(self, _sid):
            return None

    async def drive():
        out = []
        async for item in router.stream_chat(
            message="why?", active_session_id=None, backend="claude",
            context_mode="active", session_manager=Manager(),
        ):
            out.append(item)
        return out

    try:
        got = asyncio.run(drive())
    finally:
        claude.stream_response = real

    shown = got[0]["context"]
    check("the browser gets exactly what the provider got",
          shown == seen["sent"],
          "a reconstruction would show what ShellMate believes it sent, "
          "which is the thing being questioned")


def test_the_browser_half_is_wired() -> None:
    """Read out of the source: a control nothing calls is not a feature."""
    print("\n-- The browser half --")
    root = Path(__file__).parent
    chat = (root / "frontend" / "js" / "chat.js").read_text(encoding="utf-8")
    html = (root / "frontend" / "index.html").read_text(encoding="utf-8")

    check("the socket branch exists",
          "msg.type === 'context'" in chat)
    check("and it stores the block on the bubble",
          "attachContext" in chat and "_shellmateContext" in chat)
    check("the panel exists in the markup",
          'id="context-overlay"' in html and 'id="context-body"' in html)
    check("the block is rendered as text, never as markup",
          "body.textContent = text" in chat,
          "a running configuration containing a tag is the ordinary case")
    check("only the recent replies keep theirs",
          "CONTEXT_KEEP" in chat,
          "twenty context blocks held forever is a tab that dies")
    check("and a bubble that has lost its copy says so",
          "No longer kept" in chat,
          "a button that silently does nothing reads as a broken feature")


def main() -> int:
    print("=" * 52)
    print("  What the assistant saw")
    print("=" * 52)

    for test in (
        test_the_buffer_knows_its_horizon,
        test_the_oldest_visible_time_moves_with_the_window,
        test_the_heading_says_what_is_missing,
        test_the_heading_reaches_the_context_block,
        test_the_block_reaches_the_browser_first,
        test_what_is_shown_is_what_was_sent,
        test_the_browser_half_is_wired,
    ):
        try:
            test()
        except Exception as exc:
            failed.append(f"{test.__name__}: raised {type(exc).__name__}: {exc}")
            print(f"  FAIL {test.__name__} raised {type(exc).__name__}: {exc}")

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
