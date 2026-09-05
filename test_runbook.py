"""
test_runbook.py — A vetted sequence, walked with approval (#552).

Investigate decides what to do next. A runbook has already been decided,
and that is the whole difference — it is why this is a fourth persona
rather than a flag on the third. The value of a vetted sequence is that a
junior gets the same one a lead wrote; a model that reorders it has quietly
handed them a different runbook.

Three properties carry the weight:

**The prompt forbids improvement, and names the one exception.** Not
reordering, not skipping, not merging two steps into one command — with a
single stated escape for a result that makes the remaining steps unsafe or
pointless, because a runbook written last quarter cannot know what the
device says today.

**The step count moves only on an approval.** The browser owns it, because
the browser is where the click is. A server-side count would advance on a
proposal and report a step done that nobody ran.

**The approval gate does not move.** A runbook is a sequence of
suggestions, not a licence to run them.

    python test_runbook.py
"""

import re
import shutil
import sys
import tempfile
from pathlib import Path

from backend import paths

_TEMP = Path(tempfile.mkdtemp(prefix="shellmate-runbook-"))
paths._data_dir_cache = _TEMP

from backend.ai import prompt_store, prompts                # noqa: E402

passed = 0
failed: list[str] = []

CHAT = Path(__file__).parent / "frontend" / "js" / "chat.js"


def check(name: str, condition: bool, detail: str = "") -> None:
    global passed
    if condition:
        passed += 1
        print(f"  OK  {name}")
    else:
        failed.append(f"{name}: {detail}")
        print(f"  FAIL {name}\n       {detail}")


# ---------------------------------------------------------------------------

def test_the_persona_exists_and_is_editable() -> None:
    print("\n-- A fourth persona --")

    check("runbook is a mode", "runbook" in prompts.MODES, str(prompts.MODES))
    check("it has a body", bool(prompts.DEFAULT_BODIES.get("runbook")))
    check("the command rules are substituted into it",
          "{command_rules}" not in prompts.get_system_prompt("runbook"))
    check("SUGGEST_CMD reaches it, so it can suggest at all",
          "[SUGGEST_CMD]" in prompts.get_system_prompt("runbook"))

    state = prompt_store.state()
    check("it appears in the editable set",
          "runbook" in state["prompts"], str(sorted(state["prompts"])))
    check("and it can be saved and reset like the others",
          prompt_store.save("runbook", "a custom body") is not None
          and "a custom body" in prompt_store.body("runbook"))
    prompt_store.reset("runbook")
    check("resetting brings the shipped one back",
          "RUNBOOK" in prompt_store.body("runbook"))


def test_the_prompt_forbids_improving_on_the_sequence() -> None:
    """
    The whole reason it is not Investigate with a list attached.

    A model that reorders a vetted sequence has handed somebody a different
    runbook without telling them, which is worse than refusing to run one.
    """
    print("\n-- Follow it, do not improve it --")
    body = prompts.DEFAULT_BODIES["runbook"]

    for rule, why in (
        ("Do NOT reorder", "order is what a vetted sequence is"),
        ("do NOT skip", "skipping because the answer seems known is exactly "
                        "how a junior gets a different runbook"),
        ("do NOT merge", "two steps in one command cannot be approved "
                         "separately"),
    ):
        check(f"it says: {rule}", rule in body, why)

    check("and it names the one exception",
          "unsafe or pointless" in body,
          "a runbook written last quarter cannot know what the device says "
          "today, and ploughing on is worse than stopping")
    check("which must be announced rather than done quietly",
          "Say plainly that you are departing" in body, body[-900:])

    check("it still never runs anything itself",
          "you never run anything" in body, body[:900])
    check("and still refuses to invent output",
          "must not make up device output" in body, body[-300:])


def test_the_runbook_reaches_the_context_as_data() -> None:
    """
    Given as a block, not folded into the question.

    The model has to see which steps are done and which are left without
    re-reading the conversation and counting — counting is exactly the
    thing it is worst at.
    """
    print("\n-- In the context --")

    block = prompts.build_context_prompt(
        [], "output", "sw1", [],
        runbook={"name": "Check BGP health", "done": 1,
                 "steps": ["show ip bgp summary", "show ip route bgp",
                           "show log | inc BGP"]})

    check("there is a runbook block", "=== RUNBOOK ===" in block, block[:300])
    check("it is named", "Check BGP health" in block)
    check("the done step is ticked", "1. [x] show ip bgp summary" in block, block[:400])
    check("the next is not", "2. [ ] show ip route bgp" in block, block[:400])
    check("and so is the one after", "3. [ ] show log" in block, block[:400])
    check("the order is stated as binding",
          "Do not reorder or merge" in block, block[:400])

    check("no runbook means no block",
          "RUNBOOK" not in prompts.build_context_prompt([], "o", "sw1", []))
    check("and an empty step list is not a runbook either",
          "RUNBOOK" not in prompts.build_context_prompt(
              [], "o", "sw1", [], runbook={"name": "x", "steps": []}),
          "an empty heading is noise in every request")


def test_the_step_count_moves_only_on_an_approval() -> None:
    print("\n-- Who counts --")
    chat = CHAT.read_text(encoding="utf-8")

    check("the count lives in the browser", "let runbookDone = 0" in chat)
    check("and advances where a command is actually sent",
          re.search(r"runbookDone \+= 1", chat) is not None)
    check("in the same block as the investigate count",
          re.search(r"runbookDone \+= 1;[\s\S]{0,200}_investigation\.steps \+= 1",
                    chat) is not None,
          "both are 'a person approved something', and they should move "
          "in the same place")
    check("it travels with every question while one is open",
          "runbook: runbookPayload()" in chat)
    check("clearing the chat ends it",
          re.search(r"conversationId = newConversationId\(\);[\s\S]{0,260}"
                    r"endRunbook\(\)", chat) is not None,
          "otherwise the next question carries steps nobody is walking")


def test_starting_one() -> None:
    print("\n-- Starting --")
    chat = CHAT.read_text(encoding="utf-8")

    check("a snippet can be started", "function startRunbook" in chat)
    check("an empty one is refused",
          "has no commands in it" in chat)
    check("one that writes is confirmed first",
          "snippet.writes" in chat and "changes the device" in chat,
          "the trust that made somebody save a sequence should not silently "
          "extend to one that changes configuration")
    check("it switches to the runbook persona",
          "setShellmateMode('runbook')" in chat)

    check("there is a /run shortcut", "/run" in chat)
    check("it matches a partial name",
          "toLowerCase().includes(needle)" in chat,
          "making somebody get the capitals right is a command-line "
          "affectation")
    check("an ambiguous name is named rather than guessed at",
          "Several runbooks match" in chat,
          "picking one of several runs a sequence nobody asked for, which "
          "for a runbook is the whole thing going wrong at once")


def test_saving_a_plan_back_as_a_runbook() -> None:
    print("\n-- Saving one --")
    chat = CHAT.read_text(encoding="utf-8")

    check("a plan card can be saved", "function saveAsRunbook" in chat)
    check("the commands come from the card, not from re-parsing the reply",
          ".plan-step code" in chat,
          "the card is what was looked at and approved; anything that "
          "renders differently from what gets saved is a runbook nobody "
          "has reviewed")
    check("it is offered on any plan with commands, not only a concluded one",
          "wirePlanCards" in chat)
    check("and it is saved read-only until somebody says otherwise",
          "writes: false" in chat,
          "an investigation is made of show commands, and a runbook saved "
          "as writing carries a confirmation it never earned")
    check("the snippet list is told, rather than going stale",
          "shellmate:snippets-changed" in chat)


def test_the_library_offers_it() -> None:
    print("\n-- From the library --")
    root = Path(__file__).parent
    broadcast = (root / "frontend" / "js" / "broadcast.js").read_text(encoding="utf-8")
    html = (root / "frontend" / "index.html").read_text(encoding="utf-8")
    editor = (root / "frontend" / "js" / "prompts_editor.js").read_text(
        encoding="utf-8")

    check("there is a run-with-the-assistant button",
          "snippet-walk" in broadcast)
    check("and it calls what chat.js exports",
          "shellmateChat.startRunbook" in broadcast)

    check("the prompt editor offers the persona",
          'value="runbook"' in html,
          "a persona the editor cannot show is one nobody can correct")
    check("and the reset wording no longer says 'both'",
          "both prompts" not in editor,
          "there were two prompts when that was written and there are five "
          "now — a count in a sentence goes quietly wrong every time one "
          "is added")


def main() -> int:
    print("=" * 52)
    print("  Runbooks")
    print("=" * 52)

    for test in (
        test_the_persona_exists_and_is_editable,
        test_the_prompt_forbids_improving_on_the_sequence,
        test_the_runbook_reaches_the_context_as_data,
        test_the_step_count_moves_only_on_an_approval,
        test_starting_one,
        test_saving_a_plan_back_as_a_runbook,
        test_the_library_offers_it,
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
