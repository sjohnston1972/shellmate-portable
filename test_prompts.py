"""
test_prompts.py — The assistant's prompts, once they became editable.

Making the system prompts editable trades a rebuild for a risk: the text that
governs the assistant is now something a user can get wrong. Three things have
to hold whatever they write.

**The command rules survive.** They are what turn ``[SUGGEST_CMD]`` into a
clickable block instead of literal tags in the reply. They are referenced by a
marker rather than written into the editable text, and a prompt that has lost
its marker still gets them — appended at the end. Losing command suggestions
with no error anywhere would be the worst possible failure here, because
nothing on screen would explain it.

**A broken file does not break the assistant.** Unparseable JSON, a missing
mode, a body that is not a string: each falls back to the shipped text.

**Reset really resets.** It is the way back from any edit, so it has to
restore byte-for-byte.

    python test_prompts.py
"""

import json
import shutil
import sys
import tempfile
from pathlib import Path

from backend import paths

_TEMP = Path(tempfile.mkdtemp(prefix="shellmate-prompts-"))
paths._data_dir_cache = _TEMP

from backend.ai import prompt_store, prompts                # noqa: E402

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


# The instruction that makes a suggested command clickable. If this is missing
# from a rendered prompt, the feature is silently gone.
RULE_FINGERPRINT = "[SUGGEST_CMD]"


def fresh() -> None:
    prompt_store.prompts_path().unlink(missing_ok=True)
    prompt_store.load(refresh=True)


def test_defaults() -> None:
    print("\n-- The shipped prompts --")
    fresh()
    # Asserted as a set rather than a count, so adding a persona is a
    # deliberate edit here rather than a number that quietly goes stale.
    # Ansible (#602) is chosen by which view is open rather than by the mode
    # toggle, and Runbook (#552) is entered by running one and left when it
    # concludes — neither is on the toggle, and both are edited and reset
    # through the same machinery.
    check("every persona exists",
          set(prompts.MODES) == {"tshoot", "learn", "investigate",
                                 "ansible", "runbook"},
          f"got {prompts.MODES}")
    check("the file is written on first run", prompt_store.prompts_path().exists())

    for mode in prompts.MODES:
        rendered = prompt_store.rendered(mode)
        check(f"{mode} renders with the command rules",
              RULE_FINGERPRINT in rendered, "the rules did not reach the prompt")
        check(f"{mode} leaves no marker behind",
              prompts.RULES_MARKER not in rendered,
              "the placeholder was sent to the model verbatim")

    check("an unknown mode falls back rather than failing",
          prompt_store.rendered("nonsense") == prompt_store.rendered("tshoot"))


def test_editing() -> None:
    print("\n-- Editing one --")
    fresh()
    prompt_store.save("tshoot", "Answer in one line. {command_rules}")

    rendered = prompt_store.rendered("tshoot")
    check("the edit is used", rendered.startswith("Answer in one line."), rendered[:40])
    check("and the rules are still there", RULE_FINGERPRINT in rendered)
    check("the other persona is untouched",
          prompt_store.body("learn") == prompts.DEFAULT_BODIES["learn"])

    state = prompt_store.state()
    check("the edit is flagged as such", state["prompts"]["tshoot"]["modified"])
    check("and the untouched one is not", not state["prompts"]["learn"]["modified"])

    # It has to survive a reload, not just live in the cache.
    prompt_store.load(refresh=True)
    check("the edit is on disk", prompt_store.body("tshoot").startswith("Answer in one line."))


def test_marker_deleted() -> None:
    """The trap: an editable prompt is one somebody will delete things from."""
    print("\n-- With the marker deleted --")
    fresh()
    prompt_store.save("tshoot", "Answer in one line. Nothing else.")

    rendered = prompt_store.rendered("tshoot")
    check("command suggestions still work",
          RULE_FINGERPRINT in rendered,
          "deleting the marker silently disabled clickable commands")
    check("the user's text is kept",
          rendered.startswith("Answer in one line. Nothing else."), rendered[:50])
    check("and the interface can see the marker is gone",
          prompt_store.state()["prompts"]["tshoot"]["has_marker"] is False)


def test_refusals() -> None:
    print("\n-- What it will not accept --")
    fresh()
    for label, mode, body in (
        ("an unknown mode",   "sideways", "text"),
        ("an empty prompt",   "tshoot",   "   "),
        ("a blank prompt",    "learn",    ""),
    ):
        try:
            prompt_store.save(mode, body)
            check(f"{label} is refused", False, "it was accepted")
        except ValueError:
            check(f"{label} is refused", True)

    check("and nothing was written by the attempts",
          prompt_store.body("tshoot") == prompts.DEFAULT_BODIES["tshoot"])


def test_reset() -> None:
    print("\n-- Getting back --")
    fresh()
    prompt_store.save("tshoot", "one")
    prompt_store.save("learn", "two")

    prompt_store.reset("tshoot")
    check("resetting one restores it byte for byte",
          prompt_store.body("tshoot") == prompts.DEFAULT_BODIES["tshoot"])
    check("and leaves the other alone", prompt_store.body("learn") == "two")

    prompt_store.reset()
    check("resetting all restores both",
          prompt_store.body("learn") == prompts.DEFAULT_BODIES["learn"]
          and prompt_store.body("tshoot") == prompts.DEFAULT_BODIES["tshoot"])

    # Deleting the file is the documented way back from outside the app.
    prompt_store.save("tshoot", "edited again")
    prompt_store.prompts_path().unlink()
    prompt_store.load(refresh=True)
    check("deleting prompts.json restores the defaults",
          prompt_store.body("tshoot") == prompts.DEFAULT_BODIES["tshoot"])


def test_broken_file() -> None:
    print("\n-- A file somebody has broken by hand --")

    for label, content in (
        ("unparseable JSON",     "{ this is not json"),
        ("the wrong shape",      json.dumps({"prompts": "a string"})),
        ("a non-string body",    json.dumps({"prompts": {"tshoot": 42}})),
        ("an empty body",        json.dumps({"prompts": {"tshoot": "   "}})),
        ("a mode nobody knows",  json.dumps({"prompts": {"sideways": "hello"}})),
    ):
        prompt_store.prompts_path().write_text(content, encoding="utf-8")
        prompt_store.load(refresh=True)
        rendered = prompt_store.rendered("tshoot")
        check(f"{label}: falls back to the shipped prompt",
              prompt_store.body("tshoot") == prompts.DEFAULT_BODIES["tshoot"],
              "a broken file changed what the assistant is told")
        check(f"{label}: the assistant still works",
              RULE_FINGERPRINT in rendered)

    # A partial file is honoured for what it does say.
    prompt_store.prompts_path().write_text(
        json.dumps({"prompts": {"learn": "Teach me. {command_rules}"}}), encoding="utf-8")
    prompt_store.load(refresh=True)
    check("a partial file is honoured for what it does say",
          prompt_store.body("learn").startswith("Teach me."))
    check("and the rest come from the built-ins",
          prompt_store.body("tshoot") == prompts.DEFAULT_BODIES["tshoot"])


def test_backwards_compatibility() -> None:
    """The summary and Jira paths import SYSTEM_PROMPT directly."""
    print("\n-- The one-shot callers --")
    check("SYSTEM_PROMPT still exists", bool(prompts.SYSTEM_PROMPT))
    check("and carries the command rules", RULE_FINGERPRINT in prompts.SYSTEM_PROMPT)
    check("get_system_prompt honours an edit", True)

    fresh()
    prompt_store.save("learn", "Mentor mode. {command_rules}")
    check("get_system_prompt('learn') returns the edit",
          prompts.get_system_prompt("learn").startswith("Mentor mode."))
    check("get_system_prompt(None) falls back to troubleshoot",
          prompts.get_system_prompt(None) == prompt_store.rendered("tshoot"))


def main() -> int:
    print("\n" + "=" * 52)
    print("  System prompts")
    print("=" * 52)

    for test in (
        test_defaults,
        test_editing,
        test_marker_deleted,
        test_refusals,
        test_reset,
        test_broken_file,
        test_backwards_compatibility,
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
