"""
test_ai_turns.py — What the assistant is told, beyond the terminal output.

Three things the AI layer gained at once, each with a way to go wrong that
nothing on screen would show:

- **Memory** (#402): earlier turns travel with the request. Anthropic refuses
  two turns from one side in a row, and a conversation that starts with the
  assistant — so the shaping has to merge, trim and re-anchor, and the only
  symptom of getting it wrong is a 400 from one provider and not another.
- **Device facts** (#401): what the fingerprint, the alert tracker and the
  archive established is rendered into the prompt. A wording that overstated
  a guess as a certainty would be worse than the guessing it replaced.
- **Caching and usage** (#416): the cache breakpoints sit on the stable
  prefix, never on the fresh context, and a usage dict from a client must be
  passed through the router, not concatenated into the reply.

    python test_ai_turns.py
"""

import asyncio
import json
import shutil
import sys
import tempfile
from pathlib import Path

from backend import paths

_TEMP = Path(tempfile.mkdtemp(prefix="shellmate-turns-"))
paths._data_dir_cache = _TEMP

from backend.ai import turns                                    # noqa: E402
from backend.ai.prompts import (                                # noqa: E402
    build_context_prompt, build_system_preamble, render_device_facts)

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


def test_history_is_shaped_for_the_apis() -> None:
    print("\n-- Earlier turns --")
    raw = [
        {"role": "ai",   "text": "Hello, how can I help?"},          # leading assistant
        {"role": "user", "text": "Why is Gi0/2 down?"},
        {"role": "ai",   "text": "It shows err-disabled."},
        {"role": "ai",   "text": "The output confirms BPDU guard."},  # auto-analysis
        {"role": "user", "text": ""},                                  # empty
        {"role": "user", "text": "And the other interface?"},
        {"role": "ai",   "text": "Gi0/3 is administratively down."},
        {"role": "user", "text": "unanswered"},                        # trailing
    ]
    shaped = turns.normalise(raw, max_turns=10)
    roles = [m["role"] for m in shaped]
    check("starts with the user", roles and roles[0] == "user", str(roles))
    check("ends with the assistant", roles and roles[-1] == "assistant", str(roles))
    check("strictly alternates",
          all(a != b for a, b in zip(roles, roles[1:])), str(roles))
    check("consecutive assistant turns are merged",
          any("err-disabled" in m["content"] and "BPDU" in m["content"] for m in shaped))
    check("empty and trailing user turns are dropped",
          not any("unanswered" in m["content"] for m in shaped))

    trimmed = turns.normalise(raw, max_turns=1)
    check("trimming keeps whole turns from the end",
          [m["role"] for m in trimmed] == ["user", "assistant"]
          and "other interface" in trimmed[0]["content"], str(trimmed))
    check("zero turns means no memory", turns.normalise(raw, max_turns=0) == [])
    check("garbage in the list is ignored",
          turns.normalise([None, 3, {"role": "x", "text": "y"}], max_turns=5) == [])


def test_history_is_trimmed_in_blocks() -> None:
    """A prefix that changes on every request is never read back (#498)."""
    print("\n-- Trimming in blocks --")

    def conversation(turns_count: int) -> list[dict]:
        out = []
        for i in range(1, turns_count + 1):
            out += [{"role": "user", "text": f"q{i}"}, {"role": "ai", "text": f"a{i}"}]
        return out

    at_limit = turns.normalise(conversation(8), max_turns=8)
    check("at the limit nothing is dropped", len(at_limit) == 16, str(len(at_limit)))
    over = turns.normalise(conversation(9), max_turns=8)
    check("one over the limit cuts back to four turns, not eight",
          len(over) == 8 and over[0]["content"] == "q6", str([m["content"] for m in over]))
    check("  and it stays cut for the next few requests",
          turns.normalise(conversation(12), max_turns=8)[0]["content"] == "q9"
          and len(turns.normalise(conversation(12), max_turns=8)) == 8)
    check("  a whole turn is kept, user first",
          over[0]["role"] == "user" and over[-1]["role"] == "assistant")
    small = turns.normalise(conversation(3), max_turns=2)
    check("a small limit trims by a smaller block",
          len(small) == 2 and small[0]["content"] == "q3", str(small))
    check("a limit of one is still one",
          len(turns.normalise(conversation(5), max_turns=1)) == 2)
    check("the block sizes", [turns.trim_block(n) for n in (1, 2, 4, 8, 50)] == [0, 1, 2, 4, 4])


def test_stable_context_sits_in_the_system_block() -> None:
    """The persona alone is under the cacheable minimum; the steady facts join it (#498)."""
    print("\n-- The stable preamble --")
    sessions = [{"tab_num": 1, "label": "sw1", "hostname": "10.0.0.1", "connection_type": "ssh"}]
    facts = {"platform": "ios", "name": "Cisco IOS", "confidence": 0.95, "source": "banner",
             "connection_type": "ssh", "last_capture": "2026-09-01 20:39",
             "pending": {"kind": "reload", "seconds_left": 90, "cancel_command": "reload cancel"}}

    preamble = build_system_preamble(sessions, "sw1", facts)
    check("the preamble lists the open sessions",
          "=== OPEN SESSIONS ===" in preamble and "Tab 1: sw1" in preamble, preamble)
    check("  and the steady device facts",
          "Platform: Cisco IOS" in preamble and "last captured" in preamble, preamble)
    check("  but not the countdown, which changes every message",
          "PENDING" not in preamble, preamble)

    context = build_context_prompt(sessions, "out", "sw1", [], device_context=facts,
                                   stable_in_system=True)
    check("the context block then leaves those out",
          "OPEN SESSIONS" not in context and "Platform:" not in context, context)
    check("  and keeps the countdown", "PENDING on this device: reload" in context, context)
    check("  and the terminal output", "=== ACTIVE SESSION: sw1 ===" in context)

    plain = build_context_prompt(sessions, "out", "sw1", [], device_context=facts)
    check("without the flag the context block is as it was",
          "OPEN SESSIONS" in plain and "Platform:" in plain and "PENDING" in plain)
    check("no sessions still says so, stably",
          "(no active sessions)" in build_system_preamble([], "", None))


def test_provider_shapes() -> None:
    print("\n-- Provider shapes --")
    history = [{"role": "user", "text": "q1"}, {"role": "ai", "text": "a1"}]

    msgs = turns.openai_messages("SYS", history, "CONTEXT\nq2")
    check("OpenAI: system first, question last",
          msgs[0] == {"role": "system", "content": "SYS"}
          and msgs[-1]["role"] == "user" and msgs[-1]["content"].endswith("q2"))
    check("OpenAI: the earlier turns sit between",
          [m["role"] for m in msgs] == ["system", "user", "assistant", "user"])

    from backend import advanced
    advanced.reset()
    system, messages = turns.anthropic_request("SYS", history, "CONTEXT\nq2")
    check("Anthropic: system is a block list with a cache marker",
          isinstance(system, list) and system[0]["text"] == "SYS"
          and system[0].get("cache_control") == {"type": "ephemeral"})
    last_prior = messages[-2]
    check("Anthropic: the cache breakpoint is on the last earlier turn",
          isinstance(last_prior["content"], list)
          and last_prior["content"][0].get("cache_control") == {"type": "ephemeral"})
    check("Anthropic: the fresh context is never marked cacheable",
          isinstance(messages[-1]["content"], str))
    check("Anthropic: strictly alternating, ending on the question",
          [m["role"] for m in messages] == ["user", "assistant", "user"])

    advanced.update({"ai.prompt_caching": False})
    system, messages = turns.anthropic_request("SYS", history, "q")
    check("caching off: no markers anywhere",
          "cache_control" not in system[0]
          and all(isinstance(m["content"], str) for m in messages))
    advanced.reset()


def test_device_facts_are_worded_honestly() -> None:
    print("\n-- Device facts --")
    sure = render_device_facts({
        "platform": "ios", "name": "Cisco IOS", "version": "15.2(7)E",
        "confidence": 0.95, "source": "version-command", "connection_type": "ssh",
        "pending": {"kind": "reload", "seconds_left": 250, "cancel_command": "reload cancel"},
        "last_capture": "2026-09-01 20:39", "baseline": True,
    })
    text = "\n".join(sure)
    check("names the platform and version", "Cisco IOS 15.2(7)E" in text, text)
    check("says how sure and how it knows",
          "certain" in text and "version command" in text, text)
    check("the pending reload is loud, with the way out",
          "PENDING" in text and "4m 10s" in text and "reload cancel" in text, text)
    check("the archive's last capture and baseline are stated",
          "2026-09-01 20:39" in text and "baseline is set" in text, text)

    guess = render_device_facts({"platform": "nxos", "name": "Cisco NX-OS",
                                 "confidence": 0.4, "source": "prompt"})
    check("a low-confidence identification is called a guess",
          "a guess" in "\n".join(guess), "\n".join(guess))
    none = render_device_facts({"platform": "generic", "confidence": 0.0})
    check("an unidentified device says not to assume a vendor",
          "do not assume a vendor" in "\n".join(none), "\n".join(none))

    prompt = build_context_prompt([], "out", "sw1", [], device_context={"platform": "generic"})
    check("the facts block sits in the context prompt",
          "=== DEVICE FACTS: sw1 ===" in prompt and prompt.index("DEVICE FACTS") < prompt.index("ACTIVE SESSION"))
    check("no facts, no block",
          "DEVICE FACTS" not in build_context_prompt([], "out", "sw1", []))


def test_usage_passes_through_the_router() -> None:
    print("\n-- Usage --")
    from backend.ai import router, ollama_client
    from backend.connections.manager import SessionManager

    async def fake(user_message, context_block, model=None, system_prompt=None, history=None):
        yield "hel"
        yield "lo"
        yield {"usage": {"provider": "ollama", "input": 12, "output": 2, "cache_read": 0}}

    original = ollama_client.stream_response
    ollama_client.stream_response = fake
    try:
        async def drive():
            out = []
            async for item in router.stream_chat(
                message="hi", active_session_id=None, backend="ollama",
                context_mode="active", session_manager=SessionManager(),
                history=[{"role": "user", "text": "earlier"}, {"role": "ai", "text": "reply"}],
            ):
                out.append(item)
            return out
        got = asyncio.run(drive())
    finally:
        ollama_client.stream_response = original
    check("text chunks arrive as text", got[:2] == ["hel", "lo"], str(got))
    check("the usage dict arrives last, intact",
          isinstance(got[-1], dict) and got[-1]["usage"]["input"] == 12, str(got))


def test_session_notes_are_not_troubleshooting() -> None:
    """The summary runs under its own persona, not the one that suggests commands (#502)."""
    print("\n-- Session notes --")
    from backend.ai import ollama_client, summarize
    from backend.ai.prompts import SYSTEM_PROMPT

    check("the notes persona never mentions command tags",
          "SUGGEST_CMD" not in summarize.NOTES_SYSTEM_PROMPT
          and "suggest commands" in summarize.NOTES_SYSTEM_PROMPT)
    check("  where the default persona does", "SUGGEST_CMD" in SYSTEM_PROMPT)

    seen: dict = {}

    async def fake(user_message, context_block, model=None, system_prompt=None, history=None):
        seen.update({"message": user_message, "context": context_block, "system": system_prompt})
        yield "notes"

    class FakeManager:
        def get_session(self, sid):
            return None

    original = ollama_client.stream_response
    ollama_client.stream_response = fake
    try:
        text = asyncio.run(summarize.summarize_session([], None, "ollama", FakeManager()))
    finally:
        ollama_client.stream_response = original
    check("the summary is asked for under the notes persona",
          seen.get("system") == summarize.NOTES_SYSTEM_PROMPT, str(seen.get("system"))[:80])
    check("  with the task as the message and no context block",
          seen.get("context") == "" and "TASK:" in seen.get("message", ""))
    check("  and the reply comes back whole", text == "notes", text)

    check("no context block means no question heading",
          turns.user_content("", "TASK: write notes") == "TASK: write notes")
    framed = turns.user_content("=== ACTIVE SESSION ===\nout", "why?")
    check("  a context block is followed by the question under its heading",
          framed.startswith("=== ACTIVE SESSION ===") and framed.endswith("=== ENGINEER'S QUESTION ===\nwhy?"),
          framed)


def test_ollama_error_inside_the_stream() -> None:
    """Ollama reports a mid-stream failure as an error line on a 200 (#500)."""
    print("\n-- Ollama: an error line on a 200 --")
    import httpx
    from backend.ai import ollama_client

    def handler(request: httpx.Request) -> httpx.Response:
        lines = [{"message": {"role": "assistant", "content": "part"}, "done": False},
                 {"error": "model runner has unexpectedly stopped"}]
        body = "".join(json.dumps(line) + "\n" for line in lines)
        return httpx.Response(200, content=body.encode(), headers={"content-type": "application/x-ndjson"})

    real_client = httpx.AsyncClient

    class Patched(real_client):
        def __init__(self, *a, **kw):
            kw["transport"] = httpx.MockTransport(handler)
            super().__init__(*a, **kw)

    ollama_client.httpx.AsyncClient = Patched
    try:
        async def go():
            out = []
            async for chunk in ollama_client.stream_response("hi", "", model="qwen"):
                out.append(chunk)
            return out
        try:
            asyncio.run(go())
            check("the error line raises", False, "the stream ended quietly")
        except ValueError as exc:
            check("the error line raises with Ollama's message",
                  "unexpectedly stopped" in str(exc), str(exc))
    finally:
        ollama_client.httpx.AsyncClient = real_client


def main() -> int:
    print("=" * 52)
    print("  AI turns, device facts and usage")
    print("=" * 52)
    for test in (test_history_is_shaped_for_the_apis, test_history_is_trimmed_in_blocks,
                 test_stable_context_sits_in_the_system_block, test_provider_shapes,
                 test_device_facts_are_worded_honestly, test_usage_passes_through_the_router,
                 test_ollama_error_inside_the_stream, test_session_notes_are_not_troubleshooting):
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
