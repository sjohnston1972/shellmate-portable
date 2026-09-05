"""
test_citations.py — Pointing back at the evidence (#559).

The context carries a tag on every line, the model is asked to cite them,
and the chips scroll the terminal to the line. "Show me where you saw
that" in one click is the trust argument; the quieter one is that a model
asked to point at a line is less able to invent output, which the prompt
could previously only request.

Two properties carry the weight, and both fail in the direction of looking
right:

**The numbering is absolute, not per request.** A tag counted from the top
of each window renumbers every line whenever anything scrolls, so a
citation made three questions ago points at whatever has since slid into
that position. That is a citation system that is wrong precisely when the
conversation is long enough for citations to matter.

**Where the model does not cite, nothing changes.** Smaller local models
ignore the rule. The reply they produce must be the reply they produced
before — no chips, no gaps, no complaint.

    python test_citations.py
"""

import re
import shutil
import sys
import tempfile
from pathlib import Path

from backend import paths

_TEMP = Path(tempfile.mkdtemp(prefix="shellmate-citations-"))
paths._data_dir_cache = _TEMP

from backend import advanced, settings_store                # noqa: E402
from backend.ai import prompts                              # noqa: E402
from backend.session import outbound                        # noqa: E402
from backend.session.buffer import SessionBuffer            # noqa: E402

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


def a_session(lines=20, max_lines=5000):
    buf = SessionBuffer("s", max_lines=max_lines)
    for n in range(lines):
        buf.write(f"line {n}\n")
    return {"session_id": "s", "hostname": "sw1", "buffer": buf}


# ---------------------------------------------------------------------------

def test_every_line_gets_a_tag() -> None:
    print("\n-- Tagged --")
    settings_store.update_settings({"logging": {"redact_secrets": True}})

    text, first = outbound.numbered_session_text(a_session(5), 200)
    rows = text.split("\n")
    check("every line carries one", all(re.match(r"^L\d{4}\| ", r) for r in rows),
          str(rows))
    check("the content is still there", "line 0" in text and "line 4" in text)
    check("and the first number is reported", first == 1, str(first))

    check("a session with no buffer gives nothing",
          outbound.numbered_session_text({}, 200) == ("", 0))


def test_the_numbers_are_absolute() -> None:
    """
    The property the whole feature rests on.

    Numbered from the top of each window, a citation would point at
    whatever has since scrolled into that position — wrong exactly when
    the conversation is long enough for citations to matter.
    """
    print("\n-- Stable across scrolling --")

    session = a_session(lines=10)
    early, _ = outbound.numbered_session_text(session, 200)
    tag_for_line_7 = [r for r in early.split("\n") if r.endswith("line 7")][0]

    # More output arrives; the same line must keep the same number.
    for n in range(10, 40):
        session["buffer"].write(f"line {n}\n")

    later, _ = outbound.numbered_session_text(session, 200)
    still = [r for r in later.split("\n") if r.endswith("line 7")]
    check("a line keeps its number as the session grows",
          still and still[0] == tag_for_line_7,
          f"was {tag_for_line_7!r}, now {still[0] if still else 'gone'!r}")

    # And a narrower window does not renumber what it can still see.
    narrow, first = outbound.numbered_session_text(session, 5)
    check("a narrower window starts at a higher number, not at 1",
          first > 1, str(first))
    check("and the lines it shows keep the numbers they had",
          all(r.endswith(f"line {int(r[1:5]) - 1}") for r in narrow.split("\n")),
          narrow)


def test_evicted_lines_do_not_reset_the_count() -> None:
    """
    The buffer forgets; the session does not.

    If eviction reset the numbering, every long session would silently
    start citing line 1 again while the terminal held line 4,000.
    """
    print("\n-- After eviction --")

    session = a_session(lines=500, max_lines=50)
    text, first = outbound.numbered_session_text(session, 20)
    check("the numbering continues past what was evicted",
          first > 400, str(first))
    numbers = [int(r[1:5]) for r in text.split("\n")]
    check("and runs consecutively", numbers == list(range(numbers[0],
                                                          numbers[0] + len(numbers))),
          str(numbers[:5]))


def test_the_rule_is_in_the_prompt() -> None:
    print("\n-- What the model is told --")
    body = prompts.get_system_prompt("tshoot")

    check("it is asked to cite", "[L417]" in body, body[-400:])
    check("with a range form too", "[L417-L420]" in body)
    check("and told only to cite what it can see",
          "Cite only lines you can actually see" in body)
    check("and not to cite general explanation",
          "explaining general behaviour" in body,
          "a citation on a statement about how BGP works points at nothing")


def test_the_switch() -> None:
    print("\n-- The switch --")
    advanced.reset()
    check("it is on by default", advanced.get("ai.cite_lines") is True)

    keys = {s.key: s for s in advanced.SETTINGS}
    check("and says what it costs",
          "8%" in keys["ai.cite_lines"].tip, keys["ai.cite_lines"].tip[:200])
    check("and that a model which ignores it changes nothing",
          "nothing changes" in keys["ai.cite_lines"].tip,
          keys["ai.cite_lines"].tip[:250])


def test_the_browser_half() -> None:
    print("\n-- The browser half --")
    root = Path(__file__).parent
    chat = (root / "frontend" / "js" / "chat.js").read_text(encoding="utf-8")
    term = (root / "frontend" / "js" / "terminal.js").read_text(encoding="utf-8")

    check("citations are matched in the reply", "const CITATION" in chat)
    check("a range is matched as well as a single line",
          r"(?:\s*[-–]\s*L?(\d+))?" in chat, "only single lines would match")

    check("they are wired after the Markdown renderer",
          re.search(r"renderBubbleContent\(streamingBubble\);[\s\S]{0,400}"
                    r"wireCitations\(", chat) is not None,
          "wiring before it would have the renderer eat the chips")
    check("and never inside a code block",
          "closest('pre, code')" in chat,
          "a device can print something that looks like a citation, and "
          "turning that into a chip would be inventing a reference")

    check("the terminal can reveal a line", "window.revealTerminalLine" in term)
    check("it maps from the bottom up",
          "total - buf.length" in term,
          "the session and the scrollback agree at the newest line and "
          "nowhere else")
    check("a line that has scrolled out returns false",
          "if (row < 0 || row >= buf.length) return false" in term,
          "scrolling somewhere arbitrary and highlighting the wrong thing "
          "is worse than doing nothing, because it looks like an answer")
    check("and the chip then says so rather than failing silently",
          "citation-chip-gone" in chat,
          "a button that silently does nothing reads as broken")

    check("the cited line is not put at the very top",
          "term.rows / 3" in term,
          "a line with nothing above it reads as the start of the output, "
          "and the lines before it are usually what make it mean something")


def main() -> int:
    print("=" * 52)
    print("  Citations")
    print("=" * 52)

    for test in (
        test_every_line_gets_a_tag,
        test_the_numbers_are_absolute,
        test_evicted_lines_do_not_reset_the_count,
        test_the_rule_is_in_the_prompt,
        test_the_switch,
        test_the_browser_half,
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
