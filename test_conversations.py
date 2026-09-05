"""
test_conversations.py — Keeping the reasoning trail (#558).

ShellMate survives a window close by design — the sessions live in the
server process, and closing the window only hides it. The chat did not: it
was an array in the browser, and a reload threw away how a conclusion was
reached, at exactly the moment somebody has to write it up.

Three properties, in the order they would hurt if wrong:

**Stored text is redacted.** The easiest thing here to argue out of — the
text is in the browser already and the model has seen it — and the wrong
call. A stored conversation is searched, exported, sent to Jira and pasted
into tickets, which is further than a session log ever goes.

**The raw text is stored, markers and all.** `[SUGGEST_CMD]` and `[PLAN]`
are what make a reply a command block and a checklist. Storing rendered
HTML would keep the appearance and lose the behaviour.

**A conversation is not a session.** It frequently spans several devices,
and one that switched tabs half way through belongs to neither of them.

    python test_conversations.py
"""

import shutil
import sys
import tempfile
import time
from pathlib import Path

from backend import paths

_TEMP = Path(tempfile.mkdtemp(prefix="shellmate-conversations-"))
paths._data_dir_cache = _TEMP

from backend import report, settings_store                  # noqa: E402
from backend.store import store                             # noqa: E402

passed = 0
failed: list[str] = []

SECRET = "the password is 070C285F4D06485744 by the way"


def check(name: str, condition: bool, detail: str = "") -> None:
    global passed
    if condition:
        passed += 1
        print(f"  OK  {name}")
    else:
        failed.append(f"{name}: {detail}")
        print(f"  FAIL {name}\n       {detail}")


# ---------------------------------------------------------------------------

def test_storing_and_reading_back() -> None:
    print("\n-- A conversation --")
    settings_store.update_settings({"logging": {"redact_secrets": True}})

    store.add_chat_message("conv-1", "user", "why is Gi1/0/2 down?", "s1")
    store.add_chat_message("conv-1", "ai", "It is administratively down.", "s1")

    messages = store.get_conversation("conv-1")
    check("both messages come back", len(messages) == 2, str(messages))
    check("in the order they were said",
          [m["role"] for m in messages] == ["user", "ai"])
    check("with the session they were about",
          all(m["session_id"] == "s1" for m in messages))
    check("and a time", all(m["said_at"] > 0 for m in messages))

    check("an unknown conversation is empty, not an error",
          store.get_conversation("nope") == [])
    check("an empty message is not stored",
          store.add_chat_message("conv-1", "user", "   ") == -1)
    check("nor one with no conversation to belong to",
          store.add_chat_message("", "user", "orphan") == -1)


def test_it_is_redacted() -> None:
    """
    The one that is easiest to argue out of, and wrong to.

    Writing this found a real gap. The redactor masks whatever follows
    `password`, which is right for `username admin password 7 <hash>` and
    wrong for prose — "the password is hunter2" masked the word *is* and
    stored the password. A chat message is the first thing to go through
    that door that is not a device line. The pattern now hops a linking
    word, which no configuration on any platform writes.

    It is a partial fix and this test says so. Prose is genuinely hard,
    and a test that pretended otherwise would be worse than one that does
    not.
    """
    print("\n-- Masked --")
    settings_store.update_settings({"logging": {"redact_secrets": True}})

    store.add_chat_message("conv-secret", "user", SECRET, "s1")
    stored = store.get_conversation("conv-secret")[0]["text"]

    check("a secret written as prose is masked",
          "070C285F4D06485744" not in stored, stored)
    check("and the sentence still reads",
          "the password is" in stored, stored)

    from backend.session.redact import redact

    check("a configuration line is unchanged by the prose allowance",
          redact("username admin password 7 070C285F4D06485744")
          == "username admin password 7 ********",
          redact("username admin password 7 070C285F4D06485744"))
    check("and so is enable secret",
          "abc" not in redact("enable secret 5 $1$abc$xyz"))
    check("a colon form is masked too",
          "hunter2" not in redact("password: hunter2"),
          redact("password: hunter2"))

    # Stated rather than hidden. A longer phrase between the keyword and
    # the value still defeats it, and somebody reading this should know
    # that rather than infer a guarantee the redactor does not make.
    check("a longer phrase still defeats it, and this test says so",
          "hunter2" in redact("the password for the box is hunter2"),
          "if this ever starts passing, the limitation has been fixed — "
          "invert this assertion rather than deleting it")


def test_a_conversation_is_not_a_session() -> None:
    """
    It frequently spans several devices, and one that switched tabs half
    way through belongs to neither of them.
    """
    print("\n-- Across devices --")

    store.add_chat_message("conv-2", "user", "compare these two", "s1")
    store.add_chat_message("conv-2", "ai", "sw1 has an extra VLAN", "s2")

    messages = store.get_conversation("conv-2")
    check("messages keep their own session ids",
          [m["session_id"] for m in messages] == ["s1", "s2"],
          str([m["session_id"] for m in messages]))
    check("and the conversation still holds them together",
          len(messages) == 2)


def test_the_listing_titles_by_what_was_asked() -> None:
    """
    A generated summary would be better prose and a worse label: what
    somebody recognises a fortnight later is the words they typed.
    """
    print("\n-- Finding one again --")

    rows = {r["conversation_id"]: r for r in store.list_conversations()}
    check("every conversation is listed", "conv-1" in rows and "conv-2" in rows,
          str(sorted(rows)))
    check("titled by the first thing the engineer said",
          rows["conv-1"]["title"] == "why is Gi1/0/2 down?",
          rows["conv-1"]["title"])
    check("and counted", rows["conv-1"]["messages"] == 2)
    # The property, not a position. Several messages in one test land
    # inside a single clock tick on Windows, so asserting that a named
    # conversation is not first is a test that fails on a fast machine.
    stamps = [r["last_at"] for r in store.list_conversations()]
    check("newest first", stamps == sorted(stamps, reverse=True), str(stamps))

    store.add_chat_message("conv-3", "ai", "an answer with no question", "")
    titled = {r["conversation_id"]: r for r in store.list_conversations()}
    check("a conversation with no question still has a label",
          titled["conv-3"]["title"] == "(no question)",
          titled["conv-3"]["title"])


def test_searching_inside_conversations() -> None:
    print("\n-- Searching --")

    hits = store.search_chat("administratively")
    check("it finds the message", len(hits) >= 1, str(hits))
    check("and says which conversation it was in",
          hits[0]["conversation_id"] == "conv-1", str(hits[0]))
    check("with a snippet to recognise it by",
          "administratively" in hits[0]["snippet"].lower(), hits[0]["snippet"])

    check("nothing matching is an empty list, not an error",
          store.search_chat("nothing-like-this-anywhere") == [])
    check("an empty query returns nothing rather than everything",
          store.search_chat("  ") == [],
          "returning the whole history for an empty box is a surprise")

    # An address is an FTS5 syntax error unless it is quoted.
    store.add_chat_message("conv-4", "user", "check 10.1.1.1 please", "s1")
    check("an address does not blow up the search",
          any(h["conversation_id"] == "conv-4"
              for h in store.search_chat("10.1.1.1")),
          "bare punctuation is an FTS5 syntax error unless quoted")


def test_deleting_and_pruning() -> None:
    print("\n-- Forgetting --")

    store.add_chat_message("conv-old", "user", "ancient history", "s1")
    check("deleting removes every message",
          store.delete_conversation("conv-old") == 1)
    check("and it is gone from the listing",
          all(r["conversation_id"] != "conv-old"
              for r in store.list_conversations()))
    check("deleting nothing reports nothing",
          store.delete_conversation("conv-old") == 0)

    # Pruning takes whole conversations by their last message, not
    # individual messages — half a conversation is worse than none.
    store.add_chat_message("conv-stale", "user", "old", "s1")
    with store._lock:
        store.connect().execute(
            "UPDATE chat_messages SET said_at = ? WHERE conversation_id = ?",
            (time.time() - 400 * 86400, "conv-stale"))
        store.connect().commit()

    gone = store.prune_conversations(time.time() - 90 * 86400)
    check("a stale conversation is pruned", gone >= 1, str(gone))
    check("and a current one is not",
          store.get_conversation("conv-1") != [],
          "pruning by age must not take a conversation somebody is in")


def test_the_export_unwraps_the_markers() -> None:
    """
    `[SUGGEST_CMD]` and `[PLAN]` are how the panel draws a command block
    and a checklist. Left in the text they are noise to a reader who has
    never seen them; stripped out entirely, the reader loses the commands
    that were suggested — which is most of what a reasoning trail is for.
    """
    print("\n-- Exporting --")

    messages = [
        {"role": "user", "text": "why is it down?", "said_at": time.time()},
        {"role": "ai", "said_at": time.time(),
         "text": "It is admin down. Try "
                 "[SUGGEST_CMD]show run int Gi1/0/2[/SUGGEST_CMD] to confirm."},
        {"role": "ai", "said_at": time.time(),
         "text": "[PLAN]\n1. [x] show ip int brief — which are down\n"
                 "2. [ ] show interfaces Gi1/0/2\n[/PLAN]"},
    ]
    title, blocks = report.conversation_report("conv-x", messages)
    markdown = report.to_markdown(blocks)

    check("the title names it", "Conversation" in title, title)
    check("both sides are labelled",
          "You" in markdown and "The assistant" in markdown, markdown[:400])
    check("no marker survives into the document",
          "[SUGGEST_CMD]" not in markdown and "[PLAN]" not in markdown,
          markdown)
    check("the suggested command does",
          "show run int Gi1/0/2" in markdown, markdown)
    check("and it is a code block, not a sentence",
          "```" in markdown and "Suggested:" in markdown, markdown)
    check("the plan's steps survive",
          "show interfaces Gi1/0/2" in markdown, markdown)
    check("the prose around a marker is kept on both sides",
          "It is admin down" in markdown and "to confirm" in markdown,
          "splitting on a marker must not swallow the sentence it sits in")

    # And it renders as HTML from the same blocks, like every other report.
    page = report.to_html(title, blocks)
    check("the HTML form escapes device text",
          "<script" not in page and page.startswith("<!doctype html>"))

    empty_title, empty_blocks = report.conversation_report("conv-y", [])
    check("an empty conversation says so rather than producing a blank page",
          "no messages" in report.to_markdown(empty_blocks),
          report.to_markdown(empty_blocks))


def test_the_routes() -> None:
    print("\n-- Over the API --")
    from fastapi.testclient import TestClient

    from backend.app import app

    client = TestClient(app, base_url="http://127.0.0.1")

    res = client.post("/api/chat/conversations/route-1/messages",
                      json={"role": "user", "text": "a question", "session_id": "s"})
    check("a message can be stored", res.status_code == 200
          and res.json()["stored"] is True, res.text[:160])

    res = client.get("/api/chat/conversations/route-1")
    check("and read back", res.status_code == 200
          and len(res.json()["messages"]) == 1, res.text[:160])

    res = client.get("/api/chat/conversations")
    check("the listing includes it",
          any(c["conversation_id"] == "route-1"
              for c in res.json()["conversations"]), res.text[:200])

    res = client.get("/api/chat/conversations?q=question")
    check("searching returns matches rather than conversations",
          res.json()["matches"] and not res.json()["conversations"],
          res.text[:200])

    res = client.post("/api/chat/conversations/route-1/messages",
                      json={"role": "user", "txet": "typo"})
    check("a misspelled field is a 422", res.status_code == 422,
          f"HTTP {res.status_code}")

    res = client.get("/api/chat/conversations/no-such-thing")
    check("an unknown conversation is a 404", res.status_code == 404)

    res = client.post("/api/reports", json={"kind": "conversation",
                                            "conversation_id": "route-1"})
    check("it can be exported as a report", res.status_code == 200,
          res.text[:160])

    res = client.post("/api/reports", json={"kind": "conversation",
                                            "conversation_id": "nope"})
    check("exporting one that does not exist is a 404",
          res.status_code == 404, f"HTTP {res.status_code}")

    res = client.delete("/api/chat/conversations/route-1")
    check("and it can be forgotten", res.json()["deleted"] == 1, res.text[:120])


def test_the_browser_half() -> None:
    print("\n-- The browser half --")
    chat = (Path(__file__).parent / "frontend" / "js" / "chat.js").read_text(
        encoding="utf-8")

    check("messages are persisted as they happen",
          chat.count("persistMessage(") >= 3, "user, assistant and the helper")
    check("the raw text is what is stored",
          "persistMessage('ai', streamingBubble.dataset.raw" in chat,
          "storing rendered HTML would keep the appearance and lose the "
          "command blocks")
    check("a restored reply goes back through the same renderer",
          "renderBubbleContent(bubble)" in chat,
          "so a restored command block is a command block")
    check("clearing the chat starts a new conversation",
          "conversationId = newConversationId()" in chat,
          "otherwise the next conversation is appended to the last one")
    check("a restored conversation says its command blocks are stale",
          "no longer open" in chat,
          "a block bound to a tab from last week must not look live")


def main() -> int:
    print("=" * 52)
    print("  Conversations")
    print("=" * 52)

    for test in (
        test_storing_and_reading_back,
        test_it_is_redacted,
        test_a_conversation_is_not_a_session,
        test_the_listing_titles_by_what_was_asked,
        test_searching_inside_conversations,
        test_deleting_and_pruning,
        test_the_export_unwraps_the_markers,
        test_the_routes,
        test_the_browser_half,
    ):
        try:
            test()
        except Exception as exc:
            failed.append(f"{test.__name__}: raised {type(exc).__name__}: {exc}")
            print(f"  FAIL {test.__name__} raised {type(exc).__name__}: {exc}")

    store.close()
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
