"""
test_platform_notes.py — House rules per platform (#557).

An optional `assistant_notes` on a platform profile, appended to the
*cached* system preamble. A shop with a niche platform teaches the
assistant its conventions once instead of editing a global prompt that
every colleague also uses.

The property that carries the weight is the gate, and it is the same one
that decides whether ShellMate sends a device a paging command:

**Notes for the wrong platform are worse than none.** "Prefer set-format
output" handed to an IOS switch is advice that cannot be followed, and a
model that follows it anyway invents a command. So they are sent only when
the fingerprint is confident enough to act on, and never for the generic
profile — the never-guess rule applied to advice rather than to commands.

**The rule lives in one place.** `certain_enough_to_act` is the
fingerprint's own property, carried into the facts rather than
re-implemented beside them; a second copy of a threshold is a second thing
to keep in step.

    python test_platform_notes.py
"""

import shutil
import sys
import tempfile
from pathlib import Path

from backend import paths

_TEMP = Path(tempfile.mkdtemp(prefix="shellmate-notes-"))
paths._data_dir_cache = _TEMP

from backend import platforms                              # noqa: E402
from backend.ai import prompts                             # noqa: E402

passed = 0
failed: list[str] = []

NOTES = "Prefer `show run | section` over the whole configuration."


def check(name: str, condition: bool, detail: str = "") -> None:
    global passed
    if condition:
        passed += 1
        print(f"  OK  {name}")
    else:
        failed.append(f"{name}: {detail}")
        print(f"  FAIL {name}\n       {detail}")


def with_notes(platform_id="ios", notes=NOTES):
    profile = platforms.get_profile(platform_id)
    profile.assistant_notes = notes
    return profile


# ---------------------------------------------------------------------------

def test_the_field_exists_and_defaults_to_silence() -> None:
    print("\n-- The field --")

    fresh = platforms.PlatformProfile(id="x", name="X")
    check("a platform with no notes has none", fresh.assistant_notes == "")
    check("and the generic profile is no exception",
          platforms.get_profile("generic").assistant_notes == "",
          "the generic profile says nothing about anything, on purpose")


def test_the_gate() -> None:
    """
    The whole reason this is not simply appended whenever it is set.

    Advice that cannot be followed on the device in front of somebody is
    how a model ends up inventing a command.
    """
    print("\n-- Only when sure --")
    with_notes()

    check("a confident identification gets the notes",
          prompts._platform_notes(
              {"platform": "ios", "certain_enough_to_act": True}) == NOTES)

    check("an unsure one gets nothing",
          prompts._platform_notes(
              {"platform": "ios", "certain_enough_to_act": False}) == "",
          "a weak guess is exactly when platform-specific advice does the "
          "most harm")

    check("the generic profile stays silent",
          prompts._platform_notes(
              {"platform": "generic", "certain_enough_to_act": True}) == "")

    check("no platform at all is silent",
          prompts._platform_notes({"certain_enough_to_act": True}) == "")
    check("and so is an empty fact set",
          prompts._platform_notes({}) == "")

    check("a platform with no notes set contributes nothing",
          prompts._platform_notes(
              {"platform": "junos", "certain_enough_to_act": True}) == "",
          "an empty heading over nothing is noise in every request")


def test_it_reaches_the_cached_preamble() -> None:
    """
    The system block, not the fresh context.

    It is the same from one question to the next, so it belongs with the
    persona — where, on Claude, it is read from cache and costs nothing per
    turn (#498).
    """
    print("\n-- In the cached half --")
    with_notes()

    preamble = prompts.build_system_preamble(
        [], "sw1", {"platform": "ios", "name": "sw1",
                    "certain_enough_to_act": True})
    check("the notes are in the preamble", NOTES in preamble, preamble[-300:])
    check("under a heading that says what they are",
          "HOUSE RULES FOR THIS PLATFORM" in preamble, preamble[-300:])

    unsure = prompts.build_system_preamble(
        [], "sw1", {"platform": "ios", "name": "sw1",
                    "certain_enough_to_act": False})
    check("and absent when the identification is weak",
          NOTES not in unsure and "HOUSE RULES" not in unsure)

    # And not in the per-question context block, which would pay for them
    # again on every message.
    context = prompts.build_context_prompt(
        [], "output", "sw1", [],
        device_context={"platform": "ios", "certain_enough_to_act": True})
    check("they are not repeated in the fresh context",
          NOTES not in context,
          "sending them twice pays for them twice, every message")


def test_a_broken_profile_does_not_break_chat() -> None:
    print("\n-- Never fatal --")

    check("an unknown platform is silent, not an error",
          prompts._platform_notes(
              {"platform": "no-such-platform-anywhere",
               "certain_enough_to_act": True}) == "")

    # A hand-edited platforms.json is a supported thing to have.
    with_notes(notes=None)
    check("notes that are not a string do not raise",
          isinstance(prompts._platform_notes(
              {"platform": "ios", "certain_enough_to_act": True}), str))
    with_notes()


def test_the_rule_is_not_reimplemented() -> None:
    """
    `certain_enough_to_act` is the fingerprint's own property.

    A second copy of the threshold beside it is a second thing to keep in
    step, and this is the gate that decides whether ShellMate is allowed to
    be specific about a device.
    """
    print("\n-- One rule --")
    root = Path(__file__).parent

    prompts_src = (root / "backend" / "ai" / "prompts.py").read_text(encoding="utf-8")
    router_src = (root / "backend" / "ai" / "router.py").read_text(encoding="utf-8")

    check("the notes read the gate rather than a confidence number",
          "certain_enough_to_act" in prompts_src
          and "act_threshold" not in prompts_src,
          "comparing a confidence here would be a second copy of the rule")
    check("the router carries the fingerprint's own answer",
          "certain_enough_to_act" in router_src, "not carried into the facts")


def test_the_editor_can_reach_it() -> None:
    """A field nothing can edit is a field nobody has."""
    print("\n-- Editable --")
    root = Path(__file__).parent
    html = (root / "frontend" / "index.html").read_text(encoding="utf-8")
    js = (root / "frontend" / "js" / "platforms_editor.js").read_text(encoding="utf-8")
    app = (root / "backend" / "app.py").read_text(encoding="utf-8")

    check("there is a field in Platform Definitions",
          'id="platform-notes"' in html)
    check("the editor loads it", "setValue('platform-notes'" in js)
    check("and sends it back", "assistant_notes:" in js)
    check("the API accepts it", "assistant_notes: str" in app)
    check("it is a textarea, because it is prose",
          "<textarea id=\"platform-notes\"" in html,
          "a single-line input would encourage a single line")


def main() -> int:
    print("=" * 52)
    print("  Per-platform assistant notes")
    print("=" * 52)

    for test in (
        test_the_field_exists_and_defaults_to_silence,
        test_the_gate,
        test_it_reaches_the_cached_preamble,
        test_a_broken_profile_does_not_break_chat,
        test_the_rule_is_not_reimplemented,
        test_the_editor_can_reach_it,
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
