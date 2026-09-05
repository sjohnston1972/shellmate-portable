"""
test_attachment.py — Pointing at something (#551).

Mid-outage the engineer wants to say "these six lines", not ask a question
over a two-hundred-line window and hope the model picks the right ones out
of it. Three ways in — a terminal selection, the last command's output, a
paste — and one shape through the system.

Two properties carry the weight:

**The heading is not decoration.** Without one, the attached lines are
indistinguishable from the terminal output already in the context block,
and the model has no way to know they are what was pointed at. A paste
gets a *different* heading again, because it may be from another device
entirely and a model that assumes otherwise will answer confidently about
the wrong switch.

**It is redacted on the way out, not in the browser.** The browser is
where the unmasked text already is; the promise is about what leaves the
machine, so the masking lives at that boundary — one place to be right
rather than two.

    python test_attachment.py
"""

import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

from backend import paths

_TEMP = Path(tempfile.mkdtemp(prefix="shellmate-attachment-"))
paths._data_dir_cache = _TEMP

from backend.ai import router, turns                        # noqa: E402
from backend import settings_store                          # noqa: E402

passed = 0
failed: list[str] = []

SECRET = "username admin password 7 070C285F4D06485744"


def check(name: str, condition: bool, detail: str = "") -> None:
    global passed
    if condition:
        passed += 1
        print(f"  OK  {name}")
    else:
        failed.append(f"{name}: {detail}")
        print(f"  FAIL {name}\n       {detail}")


class Manager:
    def get_all_sessions(self):
        return []

    def get_session(self, _sid):
        return None


def ask(attachment=None):
    """Drive stream_chat and return (what the provider was sent, yields)."""
    import backend.ai.claude_client as claude

    real = claude.stream_response
    seen = {}

    async def fake(message, context_block, model=None, system_prompt=None,
                   history=None, tools=None, prior=None, attachment="",
                   **kwargs):
        seen["message"] = message
        seen["context"] = context_block
        seen["attachment"] = attachment
        seen["full"] = turns.user_content(context_block, message, attachment)
        yield "ok"

    claude.stream_response = fake

    async def drive():
        out = []
        async for item in router.stream_chat(
            message="what is wrong here?", active_session_id=None,
            backend="claude", context_mode="active",
            session_manager=Manager(), attachment=attachment,
        ):
            out.append(item)
        return out

    try:
        got = asyncio.run(drive())
    finally:
        claude.stream_response = real
    return seen, got


# ---------------------------------------------------------------------------

def test_the_three_kinds_get_three_headings() -> None:
    """
    A paste is not a selection, and the model must not treat it as one.

    Pasted text may be from another device, or from a file. A model that
    assumes it is the current session will answer confidently about the
    wrong switch.
    """
    print("\n-- Three headings --")

    selection = turns.attachment_block("selection", "Gi1/0/2 down down")
    record = turns.attachment_block("record", "show version\nCisco IOS")
    paste = turns.attachment_block("paste", "interface Gi0/1")

    check("a selection says it is what they are pointing at",
          "POINTING AT" in selection, selection[:80])
    check("a record says it is the last command they ran",
          "LAST COMMAND" in record, record[:80])
    check("a paste says it may not be this session at all",
          "do not assume it is this session" in paste, paste[:160])

    check("the three headings differ",
          len({selection.split(chr(10))[0], record.split(chr(10))[0],
               paste.split(chr(10))[0]}) == 3)
    check("an unknown kind falls back rather than losing the text",
          "Gi0/1" in turns.attachment_block("nonsense", "Gi0/1"))
    check("empty text produces nothing at all",
          turns.attachment_block("selection", "   ") == "")


def test_where_it_sits_in_the_prompt() -> None:
    """Context, then what is being pointed at, then the question."""
    print("\n-- The order --")

    full = turns.user_content(
        "=== ACTIVE SESSION ===\nsome output",
        "why is this down?",
        turns.attachment_block("selection", "Gi1/0/2 down down"))

    check("the context comes first",
          full.index("ACTIVE SESSION") < full.index("POINTING AT"), full[:200])
    check("then what they pointed at",
          full.index("POINTING AT") < full.index("ENGINEER'S QUESTION"),
          full[:300])
    # "ENGINEER'S QUESTION", not "ENGINEER": the attachment heading is
    # "THE LINES THE ENGINEER IS POINTING AT", so the shorter needle
    # matches inside the thing it is meant to come after.
    check("then the question",
          full.rstrip().endswith("why is this down?"), full[-60:])

    bare = turns.user_content("", "hello")
    check("no context and no attachment is the message alone",
          bare == "hello", bare)

    no_context = turns.user_content(
        "", "why?", turns.attachment_block("paste", "interface Gi0/1"))
    check("an attachment with no context still gets its heading",
          "PASTED IN" in no_context and "ENGINEER" in no_context,
          no_context[:200])


def test_it_is_redacted_on_the_way_out() -> None:
    """
    The browser has the unmasked text already; the promise is about what
    leaves the machine, so the masking is at that boundary.
    """
    print("\n-- Redaction --")
    settings_store.update_settings({"logging": {"redact_secrets": True}})

    seen, got = ask({"kind": "paste", "text": SECRET})

    check("the secret does not reach the provider",
          "070C285F4D06485744" not in seen["full"],
          "the attachment is the one path into the prompt that is not the "
          "session buffer, and it needs the same door")
    check("but the line is still there, masked",
          "username admin password" in seen["attachment"],
          seen["attachment"])

    check("and it does not reach the inspector unmasked either",
          "070C285F4D06485744" not in got[0]["context"],
          "the inspector shows what was sent, so it shows the masked form")


def test_the_inspector_shows_the_attachment_too() -> None:
    """
    It is part of what was sent, so it is part of what was seen.

    A context view that showed the block and omitted the attachment would
    be the reconstruction problem in miniature: it would show most of what
    the model got, which is worse than showing none of it because it looks
    complete.
    """
    print("\n-- In the inspector --")

    seen, got = ask({"kind": "selection", "text": "Gi1/0/2 down down"})
    shown = got[0]["context"]

    check("the attachment is in what the browser is shown",
          "Gi1/0/2 down down" in shown, shown[-200:])
    check("under its heading, so it reads as what it is",
          "POINTING AT" in shown, shown[-200:])
    check("and the session context is still there too",
          "ACTIVE SESSION" in shown, shown[:120])


def test_no_attachment_changes_nothing() -> None:
    print("\n-- Without one --")

    seen, got = ask(None)
    check("the provider gets no attachment", seen["attachment"] == "")
    check("the question is unchanged",
          seen["full"].rstrip().endswith("what is wrong here?"),
          seen["full"][-80:])
    check("and the inspector shows just the context",
          "POINTING AT" not in got[0]["context"])

    seen, got = ask({"kind": "selection", "text": "   "})
    check("whitespace is not an attachment", seen["attachment"] == "",
          "an empty heading over nothing is noise in every request")


def test_the_browser_half_is_wired() -> None:
    """A menu entry calling a function that does not exist is not a feature."""
    print("\n-- The browser half --")
    root = Path(__file__).parent
    chat = (root / "frontend" / "js" / "chat.js").read_text(encoding="utf-8")
    term = (root / "frontend" / "js" / "terminal.js").read_text(encoding="utf-8")
    html = (root / "frontend" / "index.html").read_text(encoding="utf-8")

    check("the terminal menu offers both entries",
          "Ask the assistant about this" in term
          and "Explain the last command" in term)
    check("and both call something chat.js exports",
          "shellmateChat.attach" in term and "shellmateChat.explainLast" in term)
    for name in ("attach,", "explainLast,"):
        check(f"chat.js exports {name.rstrip(',')}", name in chat)

    check("a long paste becomes an attachment",
          "PASTE_AS_ATTACHMENT" in chat and "handlePaste" in chat,
          "left as a message it is a question with two hundred lines of "
          "noise in front of it")
    check("the chip exists in the markup", 'id="chat-attachment"' in html)
    check("it is rendered as text, never as markup",
          "pre.textContent = attached.text" in chat,
          "a pasted configuration containing a tag is the ordinary case")
    check("and it clears once sent",
          "takeAttachment()" in chat,
          "an attachment that stuck would ride along with every later "
          "question, silently")


def main() -> int:
    print("=" * 52)
    print("  Pointing at something")
    print("=" * 52)

    for test in (
        test_the_three_kinds_get_three_headings,
        test_where_it_sits_in_the_prompt,
        test_it_is_redacted_on_the_way_out,
        test_the_inspector_shows_the_attachment_too,
        test_no_attachment_changes_nothing,
        test_the_browser_half_is_wired,
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
