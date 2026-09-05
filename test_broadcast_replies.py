"""
test_broadcast_replies.py — Broadcast that collects what came back (#529).

`broadcast_collect.py` already has its own suite for the waiting and the
diffing. This one is about the join: the endpoint, the panel, and the path
into the assistant. Four properties carry the weight.

**The clock is the server's.** The collector discards records older than the
broadcast, so if the browser supplied that timestamp a laptop four minutes
fast would throw away every reply it got. `/api/broadcast` issues `sent_at`
and the panel hands it straight back.

**Nothing is sent to collect.** `/compare` reads records that already closed.
A chat box that could broadcast is a broadcast nobody confirmed, and the
confirmation is the safety mechanism the whole feature rests on.

**A device that did not answer is on the list.** Timeouts and unrecognised
prompts get rows. An absent row reads as agreement, which is the one wrong
answer this must never give.

**The comparison ships with the results.** One response, so the summary line
and the list under it cannot disagree about how many devices differ.

    python test_broadcast_replies.py
"""

import re
import shutil
import sys
import tempfile
from pathlib import Path

from backend import paths

_TEMP = Path(tempfile.mkdtemp(prefix="shellmate-bcast-"))
paths._data_dir_cache = _TEMP

from backend import advanced                                  # noqa: E402
from backend.ai import turns                                  # noqa: E402

passed = 0
failed: list[str] = []

ROOT = Path(__file__).parent
APP = (ROOT / "backend" / "app.py").read_text(encoding="utf-8")
PANEL = (ROOT / "frontend" / "js" / "broadcast.js").read_text(encoding="utf-8")
CHAT = (ROOT / "frontend" / "js" / "chat.js").read_text(encoding="utf-8")
HTML = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
CSS = (ROOT / "frontend" / "css" / "style.css").read_text(encoding="utf-8")


def check(name: str, condition: bool, detail: str = "") -> None:
    global passed
    if condition:
        passed += 1
        print(f"  OK  {name}")
    else:
        failed.append(f"{name}: {detail}")
        print(f"  FAIL {name}\n       {detail}")


# ---------------------------------------------------------------------------

def test_the_endpoint_exists_and_is_bounded() -> None:
    print("\n-- The endpoint --")

    check("there is a collect route",
          '@app.post("/api/broadcast/collect")' in APP)
    check("it takes the sessions, the command and the moment it went out",
          all(f"{field}:" in APP.split("class BroadcastCollectRequest")[1][:900]
              for field in ("session_ids", "command", "sent_at", "timeout")),
          APP.split("class BroadcastCollectRequest")[1][:900])

    check("the wait is bounded by a setting rather than a literal",
          'advanced_setting("broadcast.collect_seconds")' in APP,
          "a device on an unidentified platform never closes a record, and "
          "without a ceiling the panel waits for a prompt that is not coming")
    check("and the setting is declared",
          advanced.get("broadcast.collect_seconds") == 45,
          str(advanced.get("broadcast.collect_seconds")))
    setting = next(s for s in advanced.SETTINGS
                   if s.key == "broadcast.collect_seconds")
    check("bounded at both ends",
          setting.clamp(99999) == 300 and setting.clamp(0) == 5,
          "the API is scriptable and settings.json is a text file people "
          "are told to edit")

    check("an empty command is refused rather than collected",
          "No command to collect." in APP)
    check("and sessions that have all closed are a 404, not an empty list",
          "None of those sessions are open any more." in APP,
          "an empty comparison would read as 'they all agree'")


def test_the_clock_is_the_servers() -> None:
    """
    The join between sending and collecting.

    `collect()` discards records older than `after`. Let the browser supply
    it and a laptop whose clock is four minutes fast discards every reply.
    """
    print("\n-- Whose clock --")

    check("the broadcast response carries sent_at",
          '"sent_at": started_at' in APP)
    check("read before the first byte leaves, not after the last",
          re.search(r"started_at = time\.time\(\)[\s\S]{0,400}"
                    r"async def run_one", APP) is not None,
          "a record that closes while the fleet is still being worked "
          "through is a real answer to this broadcast")
    check("and the panel hands it back rather than making its own",
          "sent_at: sentAt" in PANEL and "new Date()" not in
          PANEL.split("async function collectReplies")[1].split("}\n")[0])


def test_the_panel_only_collects_when_asked() -> None:
    print("\n-- The panel --")

    check("there is a toggle", 'id="broadcast-collect"' in HTML)
    check("it is off by default",
          'id="broadcast-collect" class="setting-checkbox" />' in HTML,
          "no `checked`: sending is quick and waiting for forty devices is "
          "not, and the common case is push-and-go")
    check("its tooltip has both halves",
          HTML.split('for="broadcast-collect"')[1].split("</label>")[0]
              .count("||") == 1)

    check("the panel asks only when the box is ticked",
          "if (collectWanted() && data.sent)" in PANEL)
    check("and only about the last command",
          "commands[commands.length - 1]" in PANEL,
          "a sequence is usually setup-then-the-one-that-answers, and "
          "collecting `terminal length 0` buries the reply that was wanted")
    check("only from the devices it actually reached",
          "data.results.filter(r => r.ok)" in PANEL,
          "waiting on a device the send failed on is waiting for an answer "
          "to a command it never got")


def test_every_device_gets_a_row() -> None:
    print("\n-- Nobody is left out --")

    for state, reading in (
        ("timeout", "no reply in time"),
        ("not-captured", "prompt not recognised"),
        ("gone", "session gone"),
    ):
        check(f"{state} reads as \"{reading}\"", reading in PANEL,
              "an absent row would read as agreement")

    check("the baseline is named on the ones that differ",
          "differs from ${comparison.baseline}" in PANEL,
          "'different from sw-01' is a fact somebody can check; 'different "
          "from the consensus' is a claim this has no standing to make")
    check("the ones that differ sort to the top",
          "differs:        ['differs', 0]" in PANEL,
          "the two rows worth reading should not be under thirty-eight "
          "that say the same thing")
    check("and open, while the rest stay collapsed",
          "item.open = state === 'differs'" in PANEL)
    check("a row with no output says so rather than showing an empty box",
          "(nothing was captured)" in PANEL)

    check("the device name is set as text, not markup",
          "name.textContent = row.label" in PANEL,
          "a hostname is whatever the device printed")


def test_the_comparison_travels_with_the_results() -> None:
    print("\n-- One response --")

    check("the endpoint compares before returning",
          'out["comparison"] = broadcast_collect.compare' in APP)
    check("derived from what is already in the response",
          'compare(out.get("results") or [])' in APP,
          "two endpoints would be two chances for the summary line and the "
          "list under it to disagree")


def test_one_renderer_for_clipboard_file_and_assistant() -> None:
    print("\n-- Copy, save, ask --")

    check("there is a copy button", "'Copy as text'" in PANEL)
    check("and a save button", "'Save as file'" in PANEL)
    check("and one that hands it to the assistant",
          "'Compare with the assistant'" in PANEL)

    check("all three render through the same function",
          PANEL.count("collectionText(data)") == 4,  # three uses, one definition
          "three renderers would be three chances for the file somebody "
          "keeps as evidence to say something the screen did not")
    check("the object URL is revoked, but not in the same frame",
          "setTimeout(() => URL.revokeObjectURL" in PANEL,
          "revoked synchronously it downloads zero bytes")


def test_the_assistant_path() -> None:
    print("\n-- Into the chat --")

    check("the panel calls what chat.js exports",
          "chat.attachComparison(" in PANEL)
    check("and chat.js exports it",
          "attachComparison,\n    startRunbook" in CHAT)

    check("it goes as an attachment, not as the message",
          "attach('compare', text, null)" in CHAT,
          "forty near-identical outputs pasted into a question is exactly "
          "the case where a model merges them")
    check("there is a heading for that kind",
          "compare" in turns.ATTACHMENT_HEADINGS)
    check("which says each block is a different device",
          "do not merge them into one device"
          in turns.ATTACHMENT_HEADINGS["compare"],
          turns.ATTACHMENT_HEADINGS["compare"])
    check("and the chip has a label, so it is not 'Attached'",
          "compare: 'What each device said'" in CHAT)


def test_compare_sends_nothing() -> None:
    print("\n-- /compare --")

    check("there is a /compare shortcut",
          r"text.match(/^\/compare\s+(.+)$/i)" in CHAT)
    check("it reads history rather than sending",
          "sent_at: 0, timeout: 0" in CHAT,
          "a broadcast from the chat box is a broadcast nobody confirmed, "
          "and the confirmation is the safety mechanism this rests on")
    check("it never posts to /api/broadcast",
          "'/api/broadcast'" not in CHAT,
          "only /api/broadcast/collect, which sends nothing")

    check("it uses the chat's own context selection",
          "window.getChatContextSelection()" in CHAT)
    check("fewer than two sessions is refused with the way to fix it",
          "Comparing needs at least two sessions" in CHAT)
    check("and sessions with no such record are named, not counted",
          "nothing recorded on: ${missing}" in CHAT,
          "'only one device has run that' leaves somebody guessing which, "
          "and the answer is usually that they typed it differently")


def test_the_styling_follows_the_theme() -> None:
    print("\n-- Both themes --")

    block = CSS.split(".broadcast-collect-block")[1].split(
        "Documentation")[0]
    hardcoded = re.findall(r"(?:background|color)\s*:\s*(#[0-9a-fA-F]{3,8}"
                           r"|rgba?\([^)]*\))", block)
    check("no hardcoded colours in the collected-replies styles",
          not hardcoded, str(hardcoded))
    check("only the states that need attention are coloured",
          ".broadcast-chip-differs" in block
          and ".broadcast-chip-identical" not in block,
          "thirty-eight green chips saying 'same' draw the eye away from "
          "the two that are the answer")
    check("a long output scrolls inside its own box",
          "max-height: 260px" in block and "overflow: auto" in block,
          "a running config from one device must not make the panel scroll")


def main() -> int:
    print("=" * 52)
    print("  Broadcast, collected")
    print("=" * 52)

    for test in (
        test_the_endpoint_exists_and_is_bounded,
        test_the_clock_is_the_servers,
        test_the_panel_only_collects_when_asked,
        test_every_device_gets_a_row,
        test_the_comparison_travels_with_the_results,
        test_one_renderer_for_clipboard_file_and_assistant,
        test_the_assistant_path,
        test_compare_sends_nothing,
        test_the_styling_follows_the_theme,
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
