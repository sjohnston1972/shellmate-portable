"""
test_firstrun.py — The four questions ShellMate already knows to ask (#564).

`update.js announceIfNew` has had a branch that means exactly "fresh
install" since the What's New modal was written, and it only recorded the
version. Two decisions were being taken silently on that same run, and both
bite weeks later — where saved passwords live, and where the data folder is.

Four properties:

**It asks once, and it never blocks.** The branch it hangs off fires once
per install; the card is dismissible; every choice has a working default
already in force, so closing it leaves the application exactly as it was.

**The vault trade-off is stated, not implied.** A DPAPI vault does not
travel. Somebody who copies their folder to a laptop and finds their saved
passwords gone has been failed by one missing sentence.

**The fallback case is a warning, not a status.** "Portable" and "From
source" are both intentional. A read-only application folder is neither —
somebody is carrying a stick that will turn out to be empty.

**Nothing here writes a setting a second way.** The AI toggle goes through
`toggleAiPanel`, which re-reads settings afterwards; writing the value
directly would leave settings.js holding a stale copy that undoes it on the
next Save.

    python test_firstrun.py
"""

import re
import sys
from pathlib import Path

passed = 0
failed: list[str] = []

ROOT = Path(__file__).parent
CARD = (ROOT / "frontend" / "js" / "firstrun.js").read_text(encoding="utf-8")
UPDATE = (ROOT / "frontend" / "js" / "update.js").read_text(encoding="utf-8")
HTML = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
CSS = (ROOT / "frontend" / "css" / "style.css").read_text(encoding="utf-8")
APP = (ROOT / "backend" / "app.py").read_text(encoding="utf-8")


def check(name: str, condition: bool, detail: str = "") -> None:
    global passed
    if condition:
        passed += 1
        print(f"  OK  {name}")
    else:
        failed.append(f"{name}: {detail}")
        print(f"  FAIL {name}\n       {detail}")


# ---------------------------------------------------------------------------

def test_it_hangs_off_the_branch_that_already_existed() -> None:
    print("\n-- Once, on a fresh install --")

    check("the fresh-install branch offers the card",
          re.search(r"seen === ''[\s\S]{0,400}shellmateFirstRun\.offer\(\)",
                    UPDATE) is not None,
          "the branch has been there since the What's New modal was written "
          "and only ever recorded the version")
    check("and still records the version, so it fires once",
          re.search(r"seen === ''[\s\S]{0,400}remember\(\{ last_seen_version",
                    UPDATE) is not None,
          "without this it would open on every launch")

    check("a second call while it is open is a no-op",
          "if (card) return;" in CARD)
    check("it lives in the home screen, not over the application",
          "getElementById('welcome-content')" in CARD
          and "firstrun-overlay" not in CARD,
          "a scrim intercepts every click until dismissed — the UI tests "
          "found that before a user did")
    check("it is loaded", 'src="/static/js/firstrun.js"' in HTML)


def test_it_never_blocks_reaching_a_device() -> None:
    print("\n-- Never in the way --")

    check("Escape closes it", "e.key === 'Escape'" in CARD)
    check("and so does the button", "'Start using ShellMate'" in CARD)
    check("a failed probe means no card at all",
          "catch (_) {" in CARD.split("async function offer")[1][:600],
          "not worth blocking a first run over — the defaults are already "
          "in force and this card only offers to change them")
    check("it says so on the card itself",
          "closing this card leaves" in CARD,
          "somebody who does not want to answer should be told that not "
          "answering is fine")


def test_the_vault_question_states_the_trade_off() -> None:
    """
    The one that actually matters, and the reason this issue exists.
    """
    print("\n-- Where saved passwords live --")

    check("both modes are offered",
          "'A master password'" in CARD and "'This Windows account'" in CARD)
    check("through the endpoint that re-encrypts, not by writing a file",
          "'/api/vault/mode'" in CARD and '@app.post("/api/vault/mode")' in APP)

    check("the trade-off is stated in full",
          "will not come with it" in CARD and "you type it once per session"
          in CARD,
          "a DPAPI vault does not travel, and somebody who copies their "
          "folder to a laptop and finds the passwords gone has been failed "
          "by one missing sentence")
    check("and that a lost master password is not recoverable",
          "no way to recover it" in CARD,
          "said before it is chosen, not after it is lost")
    check("only the saved passwords are lost with it",
          "nothing else is" in CARD,
          "otherwise it reads as though the whole install is at stake, "
          "which would push people to the option that does not travel")

    check("an existing vault is left alone",
          "vault.exists" in CARD and "already exists on this machine" in CARD,
          "changing the mode is a re-encryption, which belongs in Settings "
          "with its own confirmation rather than on a welcome card")
    check("and a machine without DPAPI is not offered it",
          "vault.dpapi_available" in CARD)


def test_the_fallback_case_is_a_warning() -> None:
    print("\n-- Portable, or not --")

    check("there is a chip", 'id="status-portable"' in HTML)
    check("wrapped with its separator",
          'id="status-portable-wrap" class="hidden"' in HTML,
          "an unanswered probe should hide both rather than leave a bare pipe")
    check("it opens Diagnostics", "openSettingsSection('Diagnostics')" in CARD)

    check("three states, not two",
          CARD.count("label = '") == 3, "Portable, From source, Not portable")
    check("and only the fallback is a warning",
          re.search(r"info\.using_fallback[\s\S]{0,200}warn = true", CARD)
          is not None,
          "'Portable' and 'From source' are both intentional; a read-only "
          "application folder is neither")
    check("the warning names the folder the data actually went to",
          "${info.data_dir}" in CARD)
    check("and says what to do about it",
          "not Program Files" in CARD,
          "a warning with no next step is a warning people learn to ignore")

    check("it is permanent, not first-run-only",
          "setTimeout(paintChip" in CARD,
          "it answers 'is this the portable copy', which people ask of a "
          "machine they did not set up")


def test_no_setting_is_written_two_ways() -> None:
    print("\n-- One writer per setting --")

    check("the assistant goes through toggleAiPanel",
          "window.toggleAiPanel()" in CARD,
          "that function moves the pane, saves, and re-reads settings; "
          "settings.js keeps its own copy for the form and a stale one "
          "there undoes the change on the next Save")
    check("and not by posting panel_enabled from here",
          "panel_enabled: enabled" not in CARD)
    check("it does nothing when the value already matches",
          "if (on === enabled) return;" in CARD,
          "toggleAiPanel toggles; calling it when it already agrees would "
          "turn the assistant off for somebody who asked for it on")

    check("the theme is applied as well as saved",
          "shellmateTheme.apply(value)" in CARD,
          "a theme picker that does nothing until you restart is a theme "
          "picker nobody believes")


def test_the_styling() -> None:
    print("\n-- Both themes --")

    block = CSS.split(".firstrun-card {")[1].split("#status-portable")[0]
    hardcoded = re.findall(r"(?:background|color)\s*:\s*(#[0-9a-fA-F]{3,8}"
                           r"|rgba?\([^)]*\))", block)
    check("no hardcoded colours", not hardcoded, str(hardcoded))
    check("and no scrim", ".firstrun-overlay" not in CSS)


def main() -> int:
    print("=" * 52)
    print("  The first run")
    print("=" * 52)

    for test in (
        test_it_hangs_off_the_branch_that_already_existed,
        test_it_never_blocks_reaching_a_device,
        test_the_vault_question_states_the_trade_off,
        test_the_fallback_case_is_a_warning,
        test_no_setting_is_written_two_ways,
        test_the_styling,
    ):
        try:
            test()
        except Exception as exc:
            failed.append(f"{test.__name__}: raised {type(exc).__name__}: {exc}")
            print(f"  FAIL {test.__name__} raised {type(exc).__name__}: {exc}")

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
