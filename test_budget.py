"""
test_budget.py — A token budget, and a price you entered (#556).

Two questions asked by two people. The lead's is "what does an incident
cost"; the engineer's is "did I just ship forty thousand tokens without
noticing".

The decisions worth holding, all of which are easy to get wrong in the
direction of sounding more authoritative than the data supports:

**No price ships as a default.** Published rates go stale, differ by region
and by contract, and a wrong number is worse than no number because
somebody would plan against it. Zero shows nothing.

**Two rates, not one.** Input and output differ by a factor of three to
five on most providers, so a single average is wrong in both directions
depending on the conversation.

**The budget is per conversation in this browser**, not per API key. It
cannot see what anything else spends. Saying so is the difference between
a useful meter and a spending cap that is not one.

**Asked once, and again at double.** A dialog on every message after the
first overrun is one people click through without reading, at which point
the budget has stopped meaning anything.

    python test_budget.py
"""

import re
import shutil
import sys
import tempfile
from pathlib import Path

from backend import paths

_TEMP = Path(tempfile.mkdtemp(prefix="shellmate-budget-"))
paths._data_dir_cache = _TEMP

from backend import advanced, settings_store                # noqa: E402

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

def test_the_settings_ship_off() -> None:
    """
    All three default to zero, and that is the whole point.

    A budget nobody asked for that starts interrupting, or a price
    ShellMate guessed, would both be worse than the feature not existing.
    """
    print("\n-- Off until asked for --")
    advanced.reset()

    for key in ("ai.conversation_token_budget", "ai.price_per_million_in",
                "ai.price_per_million_out"):
        check(f"{key} is off by default", advanced.get(key) == 0, str(advanced.get(key)))

    keys = {s.key: s for s in advanced.SETTINGS}
    check("the budget says zero turns it off",
          "Zero turns it off" in keys["ai.conversation_token_budget"].summary,
          keys["ai.conversation_token_budget"].summary)
    check("the price says it is not guessed",
          "goes stale" in keys["ai.price_per_million_in"].tip
          or "stale" in keys["ai.price_per_million_in"].tip,
          keys["ai.price_per_million_in"].tip[:160])
    check("and that it is per browser conversation, not per key",
            "not per API key" in keys["ai.conversation_token_budget"].tip,
          "a budget that sounds like a spending cap and is not one is worse "
          "than none")


def test_the_bounds_hold() -> None:
    print("\n-- Bounded --")
    advanced.reset()

    settings_store.update_settings({"advanced": {
        "ai.conversation_token_budget": -5,
        "ai.price_per_million_in": -1,
        "ai.price_per_million_out": 99999,
    }})
    check("a negative budget clamps to zero, which is off",
          advanced.get("ai.conversation_token_budget") == 0,
          str(advanced.get("ai.conversation_token_budget")))
    check("a negative price clamps to zero",
          advanced.get("ai.price_per_million_in") == 0,
          str(advanced.get("ai.price_per_million_in")))
    check("an absurd price clamps to the maximum",
          advanced.get("ai.price_per_million_out") == 1000,
          str(advanced.get("ai.price_per_million_out")))
    settings_store.update_settings({"advanced": {}})


def test_the_meter_reads_the_budget() -> None:
    print("\n-- The meter --")
    chat = CHAT.read_text(encoding="utf-8")

    check("it reads the setting",
          "ai.conversation_token_budget" in chat)
    check("it measures what the conversation spent, both directions",
          "totalUsage.input + totalUsage.output" in chat,
          "input alone understates a long answer")
    check("the colour takes the worse of the two",
          "Math.max(pct, budgetPct)" in chat,
          "a conversation inside its context window but past its budget is "
          "not green, and the reverse is equally true")
    check("and the tooltip says it resets with the chat",
          "cleared with the chat" in chat,
          "otherwise it reads as a cap on the key")


def test_the_price_is_only_ever_multiplied_by_real_counts() -> None:
    print("\n-- Money --")
    chat = CHAT.read_text(encoding="utf-8")

    check("both rates are read",
          "ai.price_per_million_in" in chat and "ai.price_per_million_out" in chat)
    check("nothing is shown when neither is set",
          "if (!inRate && !outRate) return ''" in chat,
          "a cost of zero shown as a number reads as a measurement")
    check("the two rates are applied separately",
          "totalUsage.input / 1_000_000) * inRate" in chat
          and "totalUsage.output / 1_000_000) * outRate" in chat,
          "one average is wrong in both directions")
    check("the figure is hedged, because the rates are the user's",
          "About " in chat and "at the rates you entered" in chat,
          chat[chat.find("_conversationCost"):][:400])
    check("cache reads are not given an invented discount",
          "inventing a cache rate" in chat,
          "a rate ShellMate was never told is a number it made up")


def test_it_asks_once_and_again_at_double() -> None:
    """
    A dialog on every message after the first overrun is one people click
    through without reading, at which point the budget means nothing.
    """
    print("\n-- Asking --")
    chat = CHAT.read_text(encoding="utf-8")

    check("there is an acknowledgement flag",
          "budgetAcknowledged" in chat)
    check("and it stops asking until double",
          "spent < budget * 2" in chat,
          '"you are over" and "you are twice over" are different facts')
    check("clearing the chat asks again",
          re.search(r"_resetUsage\(\);[\s\S]{0,220}budgetAcknowledged = false",
                    chat) is not None,
          "a fresh conversation is a fresh budget")
    check("the question is asked before anything is drawn",
          re.search(r"_budgetAllows\(\)\)\) return;[\s\S]{0,200}"
                    r"startStreamingBubble\(\)", chat) is not None,
          "declining should leave the question in the box, not a "
          "half-started reply on screen")


def test_a_large_single_request_is_flagged() -> None:
    """
    Nothing about typing a short question suggests it is about to send two
    hundred thousand tokens; the bill arrives a month later.
    """
    print("\n-- One big request --")
    chat = CHAT.read_text(encoding="utf-8")

    check("there is a check before sending",
          "_largeRequestAllows" in chat)
    check("with no budget set it says nothing",
          "if (!budget || estimate < budget / 2) return true" in chat,
          "no budget means no threshold; inventing one would interrupt "
          "somebody who never asked to be interrupted")
    check("and it suggests what to do instead",
          "Narrowing the tab selection" in chat,
          "a warning with no way out is a warning people learn to dismiss")


def main() -> int:
    print("=" * 52)
    print("  The token budget")
    print("=" * 52)

    for test in (
        test_the_settings_ship_off,
        test_the_bounds_hold,
        test_the_meter_reads_the_budget,
        test_the_price_is_only_ever_multiplied_by_real_counts,
        test_it_asks_once_and_again_at_double,
        test_a_large_single_request_is_flagged,
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
