"""
test_tooltips.py — Every settings row explains its trade-off (#279).

A settings panel with eighty rows and no explanation is a panel people change
things in and then cannot say why. The convention is a ``data-tip`` on the
row's ``<label class="setting-label">``, in two halves separated by ``||``:
what the setting does, then the consequence of the other choice.

The second half is the whole point, which is why the third test here exists.
A tooltip that restates its own label — ``Cursor Blink`` explained as *makes
the cursor blink* — passes a presence check, adds nothing, and is worse than
no tooltip at all because it teaches people the tooltips are not worth
reading.

Checked by parsing the markup rather than by eye: a row added six months from
now inherits the rule automatically, which is the only way a convention of
this shape survives.

    python test_tooltips.py
"""

import re
import sys
from pathlib import Path

INDEX = Path(__file__).parent / "frontend" / "index.html"

# Words that carry no meaning when comparing a tooltip against its label.
STOPWORDS = {
    "a", "an", "and", "the", "to", "of", "in", "on", "or", "is", "it",
    "with", "for", "at", "by", "as", "that", "this", "when", "each",
}

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
# Parsing
# ---------------------------------------------------------------------------


def settings_panel() -> str:
    """
    The markup of the settings panel alone.

    Scoped deliberately. Tooltips elsewhere — the connection dialog, the key
    generator — follow the same convention, but the rule being enforced here
    is about the settings panel being exhaustively documented, and a stray
    label in another overlay should not be able to fail that.
    """
    html = INDEX.read_text(encoding="utf-8")
    start = html.find('id="settings-panel"')
    if start == -1:
        raise AssertionError("could not find the settings panel in index.html")
    end = html.find('<div id="logs-overlay"', start)
    if end == -1:
        raise AssertionError("could not find the end of the settings panel")
    return html[start:end]


def rows() -> list[tuple[str, str]]:
    """
    Every setting-label row in the panel, as (label text, opening tag).

    The label text excludes any inline ``setting-hint`` span, because the two
    are different things: the hint is already on screen and a tooltip that
    expands on it is doing its job, while a tooltip that only repeats the
    label is the failure being looked for.

    The opening tag is kept whole rather than the parsed attribute, so a
    ``data-tip`` that is present but malformed is still visible to the tests
    below instead of quietly parsing as absent.
    """
    found = []
    for raw in re.findall(r'<label class="setting-label"(.*?)</label>',
                          settings_panel(), re.S):
        head, _, body = raw.partition(">")
        body = re.sub(r'<span class="setting-hint">.*?</span>', " ", body, flags=re.S)
        text = re.sub(r"<[^>]+>", " ", body)
        found.append((re.sub(r"\s+", " ", text).strip(), head))
    return found


def tip_of(head: str) -> str:
    match = re.search(r'data-tip="([^"]*)"', head)
    return match.group(1) if match else ""


def stem(word: str) -> str:
    """
    Crude suffix stripping, so *blink* and *blinks* are not two ideas.

    Without it the restatement test is trivially defeated by inflection: a
    tooltip reading `the blink of a cursor that blinks` shares no exact word
    with the label `Cursor Blink` and would pass.
    """
    for suffix in ("ing", "ies", "es", "ed", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)]
    return word


def significant(text: str) -> set[str]:
    words = re.findall(r"[a-z]+", text.lower())
    return {stem(w) for w in words if w not in STOPWORDS and len(w) > 2}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_every_row_carries_a_tooltip() -> None:
    print("\n-- Every row explains itself --")
    all_rows = rows()
    check("the panel was found and has rows in it",
          len(all_rows) > 50,
          f"only {len(all_rows)} setting-label rows were parsed")

    missing = [text[:60] for text, head in all_rows if "data-tip" not in head]
    check("no row is left without a data-tip",
          not missing,
          f"{len(missing)} without one: {missing[:6]}")


def test_both_halves_are_present() -> None:
    print("\n-- Both halves of every tooltip --")
    no_separator = []
    empty_half = []

    for text, head in rows():
        tip = tip_of(head)
        if not tip:
            continue
        if "||" not in tip:
            no_separator.append(text[:50])
            continue
        if any(not half.strip() for half in tip.split("||")):
            empty_half.append(text[:50])

    check("every tooltip has the || separator",
          not no_separator,
          f"{len(no_separator)} without it: {no_separator[:6]}")
    check("neither half is empty",
          not empty_half,
          f"{len(empty_half)} with an empty half: {empty_half[:6]}")


def test_no_tooltip_merely_restates_its_label() -> None:
    """
    The failure this guards against is the easy one to write.

    A first half whose meaningful words are all already in the label has told
    the reader nothing they could not see on screen. The comparison is against
    the label alone, not the inline hint, because a tooltip that expands on a
    hint is doing exactly what it should.

    Three new ideas is the bar, which is where the tersest of the honest
    tooltips currently sits — ``UI Text Size`` explained as *the base text
    size for the interface, in pixels* clears it, and nothing that merely
    rearranges its own label can.
    """
    print("\n-- No tooltip restates its own label --")
    restated = []
    too_short = []

    for label, head in rows():
        tip = tip_of(head)
        if not tip or "||" not in tip:
            continue

        first, _, second = tip.partition("||")

        if len(first.strip()) < 25 or len(second.strip()) < 25:
            too_short.append(f"{label} ({len(first)}/{len(second)} chars)")

        new_ideas = significant(first) - significant(label)
        if len(new_ideas) < 3:
            restated.append(f"{label} -> {first.strip()[:50]}")

    check("no first half is only the label said again",
          not restated,
          f"{len(restated)}: {restated[:6]}")
    check("no half is too short to be saying anything",
          not too_short,
          f"{len(too_short)}: {too_short[:6]}")


def test_the_attribute_is_escaped_safely() -> None:
    """
    A tooltip is markup. A stray angle bracket ends the tag it lives in and
    takes the rest of the row with it, which is a broken panel rather than a
    broken tooltip — and the double quote that would end the attribute early
    cannot be caught by the regex above, because it defines where it stops.
    """
    print("\n-- The attribute survives being markup --")
    unsafe = []
    stray_pipe = []

    for text, head in rows():
        tip = tip_of(head)
        if not tip:
            continue
        if "<" in tip or ">" in tip:
            unsafe.append(text[:50])
        if tip.replace("||", "").count("|"):
            stray_pipe.append(text[:50])

    check("no tooltip contains a raw angle bracket",
          not unsafe,
          f"{len(unsafe)}: {unsafe[:6]}")
    check("no lone pipe can be mistaken for the separator",
          not stray_pipe,
          f"{len(stray_pipe)}: {stray_pipe[:6]}")


def main() -> int:
    print("\n" + "=" * 52)
    print("  Settings tooltips")
    print("=" * 52)

    for test in (
        test_every_row_carries_a_tooltip,
        test_both_halves_are_present,
        test_no_tooltip_merely_restates_its_label,
        test_the_attribute_is_escaped_safely,
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
