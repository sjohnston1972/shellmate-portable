"""
test_tools.py — What the assistant may ask for, and how it is shaped (#560).

Native tool use replaces the `[SUGGEST_CMD]` tags where a model supports
them. Three properties are load-bearing and each fails quietly:

**The two payload shapes come from one registry.** Anthropic and the
OpenAI-shaped providers describe tools differently and name every piece
differently. Written out twice, a tool added to one and not the other is a
model that can ask for something on Claude and not on xAI, with nothing
saying why.

**The result of a tool goes back in the shape that provider expects, and
the two disagree about the role.** Anthropic wants every result for one
assistant turn inside a single *user* message; the OpenAI shape wants one
message each, with a role of its own. Getting it wrong is a 400 at best
and a silently dropped result at worst.

**`run_command` is not read-only, and the other three are.** That
distinction is what decides whether a person is asked. A read-only tool
that could reach a device would be a device interaction nobody approved,
so the flag is asserted here rather than trusted.

    python test_tools.py
"""

import json
import shutil
import sys
import tempfile
from pathlib import Path

from backend import paths

_TEMP = Path(tempfile.mkdtemp(prefix="shellmate-tools-"))
paths._data_dir_cache = _TEMP

from backend.ai import tools, turns                        # noqa: E402

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

def test_the_registry() -> None:
    print("\n-- The vocabulary --")

    check("there are tools at all", len(tools.TOOLS) >= 4)
    names = [t.name for t in tools.TOOLS]
    check("names are unique", len(names) == len(set(names)), str(names))
    check("run_command is offered", "run_command" in tools.BY_NAME)

    check("run_command is NOT read-only",
          tools.BY_NAME["run_command"].read_only is False,
          "this flag is what decides whether a person is asked")
    for name in ("get_parsed_output", "get_drift", "search_history"):
        check(f"{name} is read-only", tools.BY_NAME[name].read_only is True)

    check("every tool explains itself to the model",
          all(len(t.description) > 40 for t in tools.TOOLS),
          str([t.name for t in tools.TOOLS if len(t.description) <= 40]))
    check("run_command's description says it does not run anything",
          "does NOT run" in tools.BY_NAME["run_command"].description,
          "a model that believes the tool executes will not explain itself "
          "first, and the person approving needs that explanation")

    for tool in tools.TOOLS:
        schema = tool.schema()
        check(f"{tool.name} has a valid-looking schema",
              schema["type"] == "object"
              and set(schema["required"]) <= set(schema["properties"]),
              str(schema))


def test_both_payload_shapes_come_from_the_one_registry() -> None:
    print("\n-- Two shapes, one source --")

    anthropic = tools.for_anthropic()
    openai = tools.for_openai()

    check("Anthropic gets name, description and input_schema",
          all({"name", "description", "input_schema"} <= set(t)
              for t in anthropic), str(anthropic[0]))
    check("OpenAI gets the function wrapper",
          all(t["type"] == "function" and "parameters" in t["function"]
              for t in openai), str(openai[0]))

    check("the two describe exactly the same tools",
          [t["name"] for t in anthropic]
          == [t["function"]["name"] for t in openai],
          "a tool on one and not the other is a model that can ask for "
          "something on Claude and not on xAI")
    check("and the same schemas",
          [t["input_schema"] for t in anthropic]
          == [t["function"]["parameters"] for t in openai])

    check("both are JSON-serialisable",
          bool(json.dumps(anthropic)) and bool(json.dumps(openai)))


def test_the_anthropic_conversation_shape() -> None:
    """Every result for one turn in a single user message."""
    print("\n-- Anthropic --")

    calls = [
        {"id": "a1", "name": "run_command",
         "arguments": {"command": "show ip int br"}},
        {"id": "a2", "name": "get_drift", "arguments": {}},
    ]
    results = [{"id": "a1", "content": "Gi1/0/1 up up"},
               {"id": "a2", "content": "no changes"}]

    out = turns.with_tool_exchange([], "anthropic", "Let me look.",
                                   calls, results)
    check("two messages, not three", len(out) == 2,
          str([m["role"] for m in out]))
    check("the request is the assistant's", out[0]["role"] == "assistant")
    check("its text travels with the tool blocks",
          out[0]["content"][0]["type"] == "text",
          "dropping it loses the reasoning a person reads before approving")
    check("both calls are in the one assistant message",
          [b["type"] for b in out[0]["content"]]
          == ["text", "tool_use", "tool_use"],
          str([b["type"] for b in out[0]["content"]]))

    check("the results come back as a USER message",
          out[1]["role"] == "user",
          "Anthropic has no tool role; a wrong role here is a 400")
    check("both results are in that one message",
          len(out[1]["content"]) == 2,
          "one message each would be two user turns in a row, which the "
          "API refuses")
    check("each carries the id it answers",
          [b["tool_use_id"] for b in out[1]["content"]] == ["a1", "a2"])

    # A tool that failed has to be sayable, or the model waits for an
    # answer that never comes.
    errored = turns.with_tool_exchange(
        [], "anthropic", "", [calls[0]],
        [{"id": "a1", "content": "no such command", "is_error": True}])
    check("an error result is marked as one",
          errored[1]["content"][0].get("is_error") is True)
    check("an assistant turn with no text carries only the tool block",
          [b["type"] for b in errored[0]["content"]] == ["tool_use"],
          "an empty text block is not something to send")


def test_the_openai_conversation_shape() -> None:
    """One message per result, with a role of its own."""
    print("\n-- OpenAI-shaped --")

    calls = [{"id": "o1", "name": "search_history",
              "arguments": {"query": "err-disable"}}]
    results = [{"id": "o1", "content": "3 matches"}]

    out = turns.with_tool_exchange([], "openai", "Checking.", calls, results)
    check("the assistant turn carries tool_calls",
          out[0]["role"] == "assistant" and "tool_calls" in out[0])
    check("the arguments are a JSON string, not an object",
          isinstance(out[0]["tool_calls"][0]["function"]["arguments"], str),
          "the OpenAI shape encodes them as a string; an object is a 400")
    check("and it round-trips",
          json.loads(out[0]["tool_calls"][0]["function"]["arguments"])
          == {"query": "err-disable"})

    check("the result has its own role",
          out[1]["role"] == "tool",
          "the opposite of Anthropic's rule, which is why this is not "
          "written out twice in the clients")
    check("and names the call it answers",
          out[1]["tool_call_id"] == "o1")

    silent = turns.with_tool_exchange([], "openai", "", calls, results)
    check("an assistant turn with no prose sends content as null",
          silent[0]["content"] is None,
          "some implementations reject an empty string beside tool_calls")


def test_a_conversation_is_extended_rather_than_replaced() -> None:
    print("\n-- Appending --")

    before = [{"role": "user", "content": "what is wrong?"}]
    after = turns.with_tool_exchange(
        before, "openai", "", [{"id": "x", "name": "get_drift", "arguments": {}}],
        [{"id": "x", "content": "none"}])

    check("the earlier conversation is kept",
          after[0] == before[0], str(after[0]))
    check("and the original list is not mutated",
          len(before) == 1,
          "a caller holding the history would find it changed underneath it")


def test_which_models_are_asked() -> None:
    print("\n-- Who can be asked --")

    check("the hosted providers are assumed to support tools",
          all(tools.supports(b, "any-model")
              for b in ("claude", "openai", "xai", "deepseek")))
    check("Ollama is not, until something says so",
          tools.supports("ollama", "qwen2.5:14b") is False,
          "support there is per model and per build; a declared list would "
          "be wrong within a month, and wrong towards sending what fails")

    tools.remember_support("ollama", "qwen2.5:14b")
    check("a model observed to work is remembered",
          tools.supports("ollama", "qwen2.5:14b") is True)

    tools.remember_refusal("claude", "some-old-model")
    check("a refusal is remembered so the next request does not pay again",
          tools.supports("claude", "some-old-model") is False)
    check("and it does not affect other models",
          tools.supports("claude", "claude-opus-5") is True)

    tools.remember_refusal("ollama", "qwen2.5:14b")
    check("a refusal overrides a previous success",
          tools.supports("ollama", "qwen2.5:14b") is False,
          "a model that worked and then stopped must not keep being tried")

    tools._NO_TOOLS.clear()
    tools._KNOWN_GOOD.clear()


def test_the_switch_actually_switches() -> None:
    """
    Off means the tags path, for every provider.

    A model that cannot use tools is not an error, it is a model — and
    somebody who has turned this off has said what they want.
    """
    print("\n-- The switch --")
    from backend import settings_store

    settings_store.update_settings({"advanced": {"ai.native_tools": False}})
    check("nothing is asked when the setting is off",
          not any(tools.supports(b, "m")
                  for b in ("claude", "openai", "xai", "deepseek", "ollama")),
          "the fallback to tags has to be reachable from the settings panel")

    settings_store.update_settings({"advanced": {}})
    check("and back on again by default", tools.supports("claude", "m") is True)


def test_recognising_a_refusal_about_tools() -> None:
    """
    Matched on the word, not on a provider's exact wording.

    The cost of a false positive is one silent fallback to tags; the cost
    of a false negative is a request that fails on every retry.
    """
    print("\n-- Reading a 400 --")

    for body in (b'{"error":{"message":"tools are not supported for this model"}}',
                 b'{"error":{"message":"Unknown parameter: tools"}}',
                 b'{"error":{"message":"invalid tool_choice"}}'):
        check(f"recognised: {body[:40].decode()}...",
              tools.looks_like_a_tools_refusal(body) is True)

    for body in (b'{"error":{"message":"rate limit exceeded"}}',
                 b'{"error":{"message":"temperature must be <= 1"}}',
                 b""):
        check(f"not mistaken for one: {body[:40].decode() or 'empty'}",
              tools.looks_like_a_tools_refusal(body) is False)


def main() -> int:
    print("=" * 52)
    print("  Tools — what the assistant may ask for")
    print("=" * 52)

    for test in (
        test_the_registry,
        test_both_payload_shapes_come_from_the_one_registry,
        test_the_anthropic_conversation_shape,
        test_the_openai_conversation_shape,
        test_a_conversation_is_extended_rather_than_replaced,
        test_which_models_are_asked,
        test_the_switch_actually_switches,
        test_recognising_a_refusal_about_tools,
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
