"""
test_toolloop.py — Collecting tool calls, and answering the safe ones (#560).

The load-bearing claim is one sentence: **a read-only tool never touches
the device.** That is what makes it safe to answer without asking a person,
and it is the easiest thing here to lose — `configs.drift_report` is
exactly what `get_drift` looks like it should call, and it opens a second
channel to the switch first. A model asking "what changed?" would then be
reaching a device nobody approved it reaching, at a moment it chose.

So the central test replaces every route to a device with something that
raises, and then calls all three read-only tools. If any of them tries, the
test fails loudly rather than the feature failing quietly in a comms room.

The rest is accumulation: a provider streams a tool call in fragments, in
two different shapes, and the arguments arrive as partial JSON. Half a
tool call is not a tool call.

    python test_toolloop.py
"""

import json
import shutil
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

from backend import paths

_TEMP = Path(tempfile.mkdtemp(prefix="shellmate-toolloop-"))
paths._data_dir_cache = _TEMP

from backend.ai import toolloop                            # noqa: E402

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

def test_collecting_an_anthropic_call() -> None:
    """A tool call arrives as a start event and partial JSON deltas."""
    print("\n-- Anthropic's stream --")

    c = toolloop.AnthropicCollector()
    c.block_start(0, {"type": "text"})               # ignored
    c.block_start(1, {"type": "tool_use", "id": "t1", "name": "run_command"})
    c.block_delta(1, {"type": "input_json_delta", "partial_json": '{"comm'})
    c.block_delta(1, {"type": "input_json_delta", "partial_json": 'and": "show'})
    c.block_delta(1, {"type": "input_json_delta", "partial_json": ' version"}'})

    calls = c.calls()
    check("the text block is not mistaken for a call", len(calls) == 1,
          str(calls))
    check("the call is rebuilt whole",
          calls[0] == {"id": "t1", "name": "run_command",
                       "arguments": {"command": "show version"}},
          str(calls[0]))

    # Two calls in one turn, which Anthropic allows.
    c = toolloop.AnthropicCollector()
    c.block_start(0, {"type": "tool_use", "id": "a", "name": "get_drift"})
    c.block_delta(0, {"type": "input_json_delta", "partial_json": "{}"})
    c.block_start(1, {"type": "tool_use", "id": "b", "name": "search_history"})
    c.block_delta(1, {"type": "input_json_delta",
                      "partial_json": '{"query": "bgp"}'})
    check("two calls in one turn both survive, in order",
          [x["id"] for x in c.calls()] == ["a", "b"], str(c.calls()))


def test_collecting_an_openai_call() -> None:
    print("\n-- The OpenAI-shaped stream --")

    c = toolloop.OpenAICollector()
    c.delta([{"index": 0, "id": "o1",
              "function": {"name": "search_history", "arguments": '{"que'}}])
    c.delta([{"index": 0, "function": {"arguments": 'ry": "err-dis'}}])
    c.delta([{"index": 0, "function": {"arguments": 'able"}'}}])

    calls = c.calls()
    check("the fragments are appended, not overwritten",
          calls[0]["arguments"] == {"query": "err-disable"},
          str(calls[0]))
    check("the id survives arriving only once", calls[0]["id"] == "o1")
    check("so does the name", calls[0]["name"] == "search_history")

    c = toolloop.OpenAICollector()
    c.delta([{"index": 1, "id": "second", "function": {"name": "get_drift",
                                                       "arguments": "{}"}},
             {"index": 0, "id": "first", "function": {"name": "get_drift",
                                                      "arguments": "{}"}}])
    check("calls are returned in index order, not arrival order",
          [x["id"] for x in c.calls()] == ["first", "second"],
          str([x["id"] for x in c.calls()]))


def test_half_a_tool_call_is_not_a_tool_call() -> None:
    """
    A model that streamed broken JSON has made a mistake it should be told
    about — not one that should reach the engineer as a traceback.
    """
    print("\n-- Malformed arguments --")

    c = toolloop.AnthropicCollector()
    c.block_start(0, {"type": "tool_use", "id": "x", "name": "run_command"})
    c.block_delta(0, {"type": "input_json_delta",
                      "partial_json": '{"command": "show ver'})
    calls = c.calls()
    check("it does not raise", len(calls) == 1)
    check("and the arguments come back empty rather than broken",
          calls[0]["arguments"] == {}, str(calls[0]))

    c = toolloop.OpenAICollector()
    c.delta([{"index": 0, "id": "y", "function": {"name": "get_drift",
                                                  "arguments": "[1,2,3]"}}])
    check("a JSON value that is not an object is ignored too",
          c.calls()[0]["arguments"] == {}, str(c.calls()[0]))


def test_which_calls_need_a_person() -> None:
    print("\n-- Who answers --")

    here, ask = toolloop.partition([
        {"id": "1", "name": "get_drift", "arguments": {}},
        {"id": "2", "name": "run_command", "arguments": {"command": "reload"}},
        {"id": "3", "name": "search_history", "arguments": {"query": "x"}},
    ])
    check("run_command goes to the engineer",
          [c["id"] for c in ask] == ["2"], str(ask))
    check("the read-only ones are answered here",
          [c["id"] for c in here] == ["1", "3"], str(here))

    here, ask = toolloop.partition([
        {"id": "9", "name": "make_tea", "arguments": {}}])
    check("an unknown tool is answered rather than dropped",
          [c["id"] for c in here] == ["9"] and ask == [],
          "a silent drop leaves the model waiting for a result that never "
          "comes")


def test_a_read_only_tool_never_touches_the_device() -> None:
    """
    The claim the whole distinction rests on.

    Every route to a device is replaced with something that raises. If any
    read-only tool reaches one, this fails here rather than in a comms room
    at two in the morning.
    """
    print("\n-- Nothing reaches the switch --")

    from backend import configs as configs_module

    tripped: list[str] = []

    def trap(*args, **kwargs):
        tripped.append("capture")
        raise AssertionError("a read-only tool opened a channel to the device")

    real_capture = configs_module.capture_config
    real_live = configs_module.capture_config_live
    real_drift = configs_module.drift_report
    configs_module.capture_config = trap
    configs_module.capture_config_live = trap
    configs_module.drift_report = trap

    # A handler that would raise if anything asked it to send.
    handler = SimpleNamespace(
        is_connected=True,
        send=lambda *a, **k: tripped.append("send"),
        open_secondary_channel=lambda *a, **k: tripped.append("channel"))
    session = {"session_id": "s1", "hostname": "sw1", "handler": handler,
               "recent_records": []}

    try:
        for call in ({"id": "a", "name": "get_drift", "arguments": {}},
                     {"id": "b", "name": "get_parsed_output",
                      "arguments": {"command": "show version"}},
                     {"id": "c", "name": "search_history",
                      "arguments": {"query": "anything"}}):
            result = toolloop.execute(call, session)
            check(f"{call['name']} answered without raising",
                  isinstance(result.get("content"), str)
                  and result["content"] != "",
                  str(result))
    finally:
        configs_module.capture_config = real_capture
        configs_module.capture_config_live = real_live
        configs_module.drift_report = real_drift

    check("nothing was captured, sent or opened", tripped == [],
          f"a read-only tool did: {tripped}")


def test_get_drift_reads_the_archive() -> None:
    print("\n-- Drift, from what is stored --")
    from backend.store import store as history

    session = {"session_id": "s", "hostname": "drift-sw-01"}

    result = toolloop.execute({"id": "1", "name": "get_drift", "arguments": {}},
                              session)
    check("with nothing stored it says so, and says it did not capture",
          result["is_error"] is True
          and "Nothing was captured just now" in result["content"],
          result["content"])

    history.add_snapshot("drift-sw-01", "hostname sw1\nntp server 10.0.0.1\n", "s")
    result = toolloop.execute({"id": "2", "name": "get_drift", "arguments": {}},
                              session)
    check("with one capture it says there is nothing to compare with",
          "nothing to compare it with" in result["content"],
          result["content"])

    history.add_snapshot("drift-sw-01",
                         "hostname sw1\nntp server 10.0.0.2\n", "s")
    result = toolloop.execute({"id": "3", "name": "get_drift", "arguments": {}},
                              session)
    check("with two it diffs them",
          "10.0.0.2" in result["content"] and "10.0.0.1" in result["content"],
          result["content"][:200])
    check("and reports the counts",
          "1 line(s) added" in result["content"], result["content"][:200])

    check("a session with no device is refused",
          toolloop.execute({"id": "4", "name": "get_drift", "arguments": {}},
                           {"session_id": "x"})["is_error"] is True)


def test_parsed_output_reports_what_has_not_been_run() -> None:
    """
    Not run is a fact, not a reason to run it.

    A tool that quietly ran the command to answer the question would be
    the read-only rule broken by helpfulness.
    """
    print("\n-- Output that does not exist --")

    record = SimpleNamespace(command="show version", output="Cisco IOS",
                             prompt="sw1#", started_at=time.time(),
                             duration_ms=5)
    session = {"session_id": "s", "hostname": "sw1",
               "recent_records": [record]}

    result = toolloop.execute(
        {"id": "1", "name": "get_parsed_output",
         "arguments": {"command": "show ip bgp summary"}}, session)
    check("it says the command has not been run",
          "has not been run" in result["content"], result["content"])
    check("and lists what has, so the model can pick",
          "show version" in result["content"], result["content"])
    check("and points at the path with a person on it",
          "run_command" in result["content"],
          "the model should ask to run it, not have it run for it")

    empty = toolloop.execute(
        {"id": "2", "name": "get_parsed_output",
         "arguments": {"command": "show version"}},
        {"session_id": "s", "hostname": "sw1", "recent_records": []})
    check("a session with no records says so rather than erroring oddly",
          "No command output has been recorded" in empty["content"],
          empty["content"])


def test_search_history() -> None:
    print("\n-- Searching what was recorded --")
    from backend.store import store as history

    history.start_session("hist-1", {"hostname": "sw9", "label": "sw9",
                                     "connection_type": "ssh"})
    history.add_command("hist-1", SimpleNamespace(
        command="show interface status", output="Gi1/0/4 err-disabled",
        prompt="sw9#", started_at=time.time(), duration_ms=5))
    history.flush()

    result = toolloop.execute(
        {"id": "1", "name": "search_history",
         "arguments": {"query": "err-disabled"}}, None)
    check("it finds the command", "show interface status" in result["content"],
          result["content"][:200])
    check("and names the device it was on", "sw9" in result["content"])
    check("it works with no session at all",
          result["is_error"] is False,
          "history is not about the tab somebody is looking at")

    nothing = toolloop.execute(
        {"id": "2", "name": "search_history",
         "arguments": {"query": "nothing-like-this-anywhere"}}, None)
    check("no matches is stated, not an error",
          nothing["is_error"] is False and "Nothing matching" in nothing["content"],
          nothing["content"])
    history.delete_session("hist-1")


def test_an_unknown_tool_is_told_what_exists() -> None:
    print("\n-- A tool that is not there --")
    result = toolloop.execute({"id": "1", "name": "reboot_everything",
                               "arguments": {}}, None)
    check("it is an error the model can read", result["is_error"] is True)
    check("and it lists what there is",
          "run_command" in result["content"] and "get_drift" in result["content"],
          result["content"])


def test_a_huge_result_is_cut() -> None:
    """A tool that returns a whole configuration spends what it saved."""
    print("\n-- Bounded --")
    from backend.store import store as history

    history.start_session("big-1", {"hostname": "big", "label": "big",
                                    "connection_type": "ssh"})
    for n in range(60):
        history.add_command("big-1", SimpleNamespace(
            command=f"show run interface Gi1/0/{n}",
            output="x" * 400, prompt="big#",
            started_at=time.time(), duration_ms=1))
    history.flush()

    result = toolloop.execute(
        {"id": "1", "name": "search_history",
         "arguments": {"query": "show run"}}, None)
    check("the result is capped",
          len(result["content"]) <= toolloop.MAX_RESULT_CHARS + 200,
          str(len(result["content"])))
    if len(result["content"]) > toolloop.MAX_RESULT_CHARS:
        check("and says it was cut",
              "not included" in result["content"], result["content"][-200:])
    history.delete_session("big-1")


def main() -> int:
    print("=" * 52)
    print("  The tool loop — collecting, and answering safely")
    print("=" * 52)

    for test in (
        test_collecting_an_anthropic_call,
        test_collecting_an_openai_call,
        test_half_a_tool_call_is_not_a_tool_call,
        test_which_calls_need_a_person,
        test_a_read_only_tool_never_touches_the_device,
        test_get_drift_reads_the_archive,
        test_parsed_output_reports_what_has_not_been_run,
        test_search_history,
        test_an_unknown_tool_is_told_what_exists,
        test_a_huge_result_is_cut,
    ):
        try:
            test()
        except Exception as exc:
            failed.append(f"{test.__name__}: raised {type(exc).__name__}: {exc}")
            print(f"  FAIL {test.__name__} raised {type(exc).__name__}: {exc}")

    from backend.store import store as history
    history.close()
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
