"""
test_tool_turn.py — A whole tool turn, end to end (#560).

`test_tools.py` covers the shapes and `test_toolloop.py` the executors.
This drives `stream_chat` against a fake provider and checks the thing
both of those exist to protect:

**Nothing reaches a device without a person.** A model asking for
`run_command` gets a command block, not a command. The turn *stops* there
— the model does not get to carry on as though it had been run, and the
device's handler is a trap that fails the test if anything sends to it.

**Read-only tools do not stop the turn.** They are answered and the model
continues, which is the whole point: a question needing drift and history
is one turn, not three requests that each start over.

**A model that never asks for anything behaves exactly as before.** The
tags path is what every model without tool support still uses, and it must
be untouched.

    python test_tool_turn.py
"""

import asyncio
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

from backend import paths

_TEMP = Path(tempfile.mkdtemp(prefix="shellmate-tool-turn-"))
paths._data_dir_cache = _TEMP

from backend.ai import router                              # noqa: E402
from backend.ai import tools as tool_registry              # noqa: E402

passed = 0
failed: list[str] = []

#: Anything the fake device was asked to do. Must stay empty.
touched: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    global passed
    if condition:
        passed += 1
        print(f"  OK  {name}")
    else:
        failed.append(f"{name}: {detail}")
        print(f"  FAIL {name}\n       {detail}")


class FakeManager:
    """Just enough SessionManager for the router."""

    def __init__(self, session):
        self._session = session

    def get_all_sessions(self):
        return [self._session]

    def get_session(self, session_id):
        return self._session if session_id == self._session["session_id"] else None


def a_session():
    def trap(*args, **kwargs):
        touched.append("device")
        raise AssertionError("something reached the device without approval")

    return {
        "session_id": "s1",
        "hostname": "sw1",
        "display_label": "sw1",
        "connection_type": "ssh",
        "buffer": SimpleNamespace(get_text=lambda n=200: "sw1#show version\nCisco IOS\n",
                                  line_count=lambda: 2),
        "recent_records": [],
        "handler": SimpleNamespace(is_connected=True, send=trap,
                                   open_secondary_channel=trap),
    }


def fake_provider(script):
    """
    A stream_response that replays `script`, one entry per round.

    Each entry is a list of things to yield — strings for text, dicts for
    tool calls and usage — so a multi-round turn is written out as it
    would actually arrive.
    """
    state = {"round": 0}

    # **kwargs, for the same reason the pre-existing fakes needed it: a
    # fake that enumerates the provider contract has to be edited every
    # time the contract grows, and this one broke on `attachment` an
    # hour after it was written.
    async def stream_response(message, context_block, model=None,
                              system_prompt=None, history=None,
                              tools=None, prior=None, **kwargs):
        index = min(state["round"], len(script) - 1)
        state["round"] += 1
        # Recorded so a test can assert what the model was offered and
        # what it was shown of its own earlier exchange.
        state.setdefault("seen", []).append(
            {"tools": tools, "prior": list(prior or [])})
        for item in script[index]:
            yield item

    return stream_response, state


def install(monkey, state=None):
    """Point every provider import in the router at the fake."""
    import backend.ai.claude_client as claude

    claude.stream_response = monkey


async def run(message="what is wrong?", session=None, resume=None):
    out = []
    async for chunk in router.stream_chat(
        message=message,
        active_session_id="s1",
        backend="claude",
        context_mode="active",
        session_manager=FakeManager(session or a_session()),
        open_session_ids=["s1"],
        model="claude-opus-5",
        resume=resume,
    ):
        out.append(chunk)
    return out


# ---------------------------------------------------------------------------

def test_a_command_request_stops_the_turn() -> None:
    """The rule the whole feature is built around."""
    print("\n-- Asking to run something --")
    touched.clear()

    monkey, state = fake_provider([
        ["Let me look at the interfaces. ",
         {"tool_calls": [{"id": "c1", "name": "run_command",
                          "arguments": {"command": "show ip int brief",
                                        "why": "to see which are down"}}]},
         {"usage": {"input": 100, "output": 20, "provider": "anthropic"}}],
        # A second round the model must never get, because the turn stops.
        ["I have now run it and everything is fine."],
    ])
    install(monkey)

    out = asyncio.run(run())
    text = "".join(c for c in out if isinstance(c, str))
    requests = [c["tool_request"] for c in out
                if isinstance(c, dict) and "tool_request" in c]

    check("nothing reached the device", touched == [], str(touched))
    check("what the model said on the way is streamed",
          "Let me look at the interfaces" in text, text[:120])
    check("a tool request is handed to the browser", len(requests) == 1,
          str(out))
    check("carrying the command", requests and
          requests[0]["calls"][0]["arguments"]["command"] == "show ip int brief",
          str(requests[0]["calls"] if requests else None))
    check("and the id it must be answered with",
          requests and requests[0]["calls"][0]["id"] == "c1")
    check("and the provider shape, so the answer is built correctly",
          requests and requests[0]["shape"] == "anthropic")

    check("the turn stopped rather than carrying on",
          "everything is fine" not in text,
          "the model must not proceed as though the command had been run")
    check("the provider was asked exactly once",
          state["round"] == 1, str(state["round"]))
    check("the usage still arrives", any(
        isinstance(c, dict) and "usage" in c for c in out))


def test_read_only_tools_do_not_stop_the_turn() -> None:
    """A question needing two lookups is one turn, not three requests."""
    print("\n-- Looking things up --")
    touched.clear()

    monkey, state = fake_provider([
        ["Checking what has changed. ",
         {"tool_calls": [{"id": "d1", "name": "get_drift", "arguments": {}}]}],
        ["And what we have seen before. ",
         {"tool_calls": [{"id": "h1", "name": "search_history",
                          "arguments": {"query": "err-disable"}}]}],
        ["Nothing has changed and there is no history of this.",
         {"usage": {"input": 300, "output": 40, "provider": "anthropic"}}],
    ])
    install(monkey)

    out = asyncio.run(run())
    text = "".join(c for c in out if isinstance(c, str))

    check("nothing reached the device", touched == [], str(touched))
    check("no tool request was handed to the browser",
          not any(isinstance(c, dict) and "tool_request" in c for c in out),
          "read-only tools reach nothing, so there is nothing to approve")
    check("the provider was asked three times, in one turn",
          state["round"] == 3, str(state["round"]))
    check("everything the model said is on screen, in order",
          text.index("Checking what has changed")
          < text.index("And what we have seen before")
          < text.index("Nothing has changed"), text[:200])

    # The second request must carry the first exchange, or the model has
    # asked and been ignored.
    second = state["seen"][1]["prior"]
    check("the second request carries the first request and its answer",
          len(second) == 2 and second[0]["role"] == "assistant"
          and second[1]["role"] == "user",
          str([m["role"] for m in second]))
    check("and the third carries both exchanges",
          len(state["seen"][2]["prior"]) == 4,
          str(len(state["seen"][2]["prior"])))
    check("the usage is yielded once, at the end",
          sum(1 for c in out if isinstance(c, dict) and "usage" in c) == 1,
          "yielding it per round would count one answer several times")


def test_the_loop_is_bounded() -> None:
    """
    A model that keeps looking things up has to stop somewhere.

    Bounded by ai.investigate_max_steps, which is the number somebody has
    already tuned for exactly this — not a second one to keep in step.
    """
    print("\n-- Bounded --")
    touched.clear()
    from backend import settings_store

    settings_store.update_settings({"advanced": {"ai.investigate_max_steps": 3}})

    monkey, state = fake_provider([
        ["thinking ",
         {"tool_calls": [{"id": "x", "name": "get_drift", "arguments": {}}]}],
    ])
    install(monkey)

    out = asyncio.run(run())
    text = "".join(c for c in out if isinstance(c, str))

    check("it stops at the bound", state["round"] == 3, str(state["round"]))
    check("and says so rather than just ending",
          "reached its limit" in text, text[-160:])
    check("nothing reached the device", touched == [], str(touched))

    settings_store.update_settings({"advanced": {}})


def test_a_model_that_asks_for_nothing_is_untouched() -> None:
    """The tags path, which every model without tool support still uses."""
    print("\n-- No tools at all --")
    touched.clear()

    monkey, state = fake_provider([
        ["Try [SUGGEST_CMD]show version[/SUGGEST_CMD] and see.",
         {"usage": {"input": 50, "output": 10, "provider": "anthropic"}}],
    ])
    install(monkey)

    out = asyncio.run(run())
    text = "".join(c for c in out if isinstance(c, str))

    check("the reply arrives exactly as it was streamed",
          "[SUGGEST_CMD]show version[/SUGGEST_CMD]" in text, text)
    check("one request, no loop", state["round"] == 1)
    check("no tool request", not any(
        isinstance(c, dict) and "tool_request" in c for c in out))


def test_tools_are_not_offered_when_the_switch_is_off() -> None:
    print("\n-- The switch --")
    from backend import settings_store

    monkey, state = fake_provider([["fine", {"usage": {"input": 1, "output": 1}}]])
    install(monkey)

    settings_store.update_settings({"advanced": {"ai.native_tools": False}})
    asyncio.run(run())
    check("no tool definitions are sent",
          state["seen"][0]["tools"] is None,
          "the fallback to tags has to be reachable from the settings panel")

    settings_store.update_settings({"advanced": {}})
    monkey, state = fake_provider([["fine", {"usage": {"input": 1, "output": 1}}]])
    install(monkey)
    asyncio.run(run())
    check("and they are sent again when it is on",
          state["seen"][0]["tools"] is not None
          and any(t["name"] == "run_command"
                  for t in state["seen"][0]["tools"]),
          str(state["seen"][0]["tools"]))


def test_an_unsupported_model_gets_no_tools() -> None:
    print("\n-- A model that cannot --")
    tool_registry.remember_refusal("claude", "claude-opus-5")

    monkey, state = fake_provider([["fine", {"usage": {"input": 1, "output": 1}}]])
    install(monkey)
    asyncio.run(run())
    check("a model that refused once is not asked again",
          state["seen"][0]["tools"] is None,
          "the second request should not pay for the refusal too")

    tool_registry._NO_TOOLS.clear()


def test_resuming_after_an_approval() -> None:
    """
    The output of an approved command comes back as a tool result.

    That is what makes it the model's own exchange rather than something
    it is told about afterwards in prose.
    """
    print("\n-- After the engineer approves --")
    touched.clear()
    from backend.ai import turns

    resume = turns.with_tool_exchange(
        [], "anthropic", "Let me look at the interfaces. ",
        [{"id": "c1", "name": "run_command",
          "arguments": {"command": "show ip int brief"}}],
        [{"id": "c1", "content": "Gi1/0/1 up up\nGi1/0/2 down down"}])

    monkey, state = fake_provider([
        ["Gi1/0/2 is down.", {"usage": {"input": 200, "output": 8}}]])
    install(monkey)

    out = asyncio.run(run(resume=resume))
    text = "".join(c for c in out if isinstance(c, str))

    check("the model answers from the output", "Gi1/0/2 is down" in text, text)
    check("the request carried the earlier exchange",
          len(state["seen"][0]["prior"]) == 2,
          str(state["seen"][0]["prior"]))
    check("the model's own request is in it, as an assistant turn",
          state["seen"][0]["prior"][0]["role"] == "assistant")
    check("and the output as the result of that request",
          state["seen"][0]["prior"][1]["content"][0]["tool_use_id"] == "c1",
          str(state["seen"][0]["prior"][1]))
    check("nothing reached the device", touched == [], str(touched))


def main() -> int:
    print("=" * 52)
    print("  A tool turn, end to end")
    print("=" * 52)

    import backend.ai.claude_client as claude
    real = claude.stream_response

    for test in (
        test_a_command_request_stops_the_turn,
        test_read_only_tools_do_not_stop_the_turn,
        test_the_loop_is_bounded,
        test_a_model_that_asks_for_nothing_is_untouched,
        test_tools_are_not_offered_when_the_switch_is_off,
        test_an_unsupported_model_gets_no_tools,
        test_resuming_after_an_approval,
    ):
        try:
            test()
        except Exception as exc:
            failed.append(f"{test.__name__}: raised {type(exc).__name__}: {exc}")
            print(f"  FAIL {test.__name__} raised {type(exc).__name__}: {exc}")

    claude.stream_response = real
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
