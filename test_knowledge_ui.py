"""
test_knowledge_ui.py — The knowledge folder, wired in (#561).

`knowledge.py` has its own suite for walking, chunking and searching. This
one is about the integration: the routes, the retrieval path through the
router, and the panel that tells somebody what to do next.

**It sits beside Chroma, not instead of it.** A site that has Chroma standing
has better retrieval than this, and both blocks reach the prompt. Replacing
one with the other would be taking a working feature away from the few people
who have it.

**Retrieval never runs on the event loop.** The loop is also serving every
live terminal session. A folder walk or an FTS query that stalled it would
drop keystrokes on a device somebody is mid-change on.

**Nothing indexes on its own.** A walk of fifty documents on every page load
makes a settings panel feel broken on a slow disk, so the button says what it
will do and the panel says what happened.

    python test_knowledge_ui.py
"""

import re
import shutil
import sys
import tempfile
from pathlib import Path

from backend import paths

_TEMP = Path(tempfile.mkdtemp(prefix="shellmate-knowui-"))
paths._data_dir_cache = _TEMP

from backend import knowledge                                 # noqa: E402
from backend.ai import prompts                                # noqa: E402

passed = 0
failed: list[str] = []

ROOT = Path(__file__).parent
APP = (ROOT / "backend" / "app.py").read_text(encoding="utf-8")
ROUTER = (ROOT / "backend" / "ai" / "router.py").read_text(encoding="utf-8")
PANEL = (ROOT / "frontend" / "js" / "knowledge.js").read_text(encoding="utf-8")
HTML = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
CSS = (ROOT / "frontend" / "css" / "style.css").read_text(encoding="utf-8")
DOCS = (ROOT / "frontend" / "docs" / "assistant.md").read_text(encoding="utf-8")


def check(name: str, condition: bool, detail: str = "") -> None:
    global passed
    if condition:
        passed += 1
        print(f"  OK  {name}")
    else:
        failed.append(f"{name}: {detail}")
        print(f"  FAIL {name}\n       {detail}")


# ---------------------------------------------------------------------------

def test_the_routes() -> None:
    print("\n-- The routes --")

    check("state", '@app.get("/api/knowledge")' in APP)
    check("reindex", '@app.post("/api/knowledge/reindex")' in APP)
    check("and a way to open the folder",
          '@app.post("/api/knowledge/reveal")' in APP,
          "the one thing this asks of a user is that they put files in a "
          "folder, and a path they must copy and paste is where most stop")

    check("the path is returned whether or not the folder exists",
          '"exists": folder.is_dir()' in APP,
          "'put your documents here' needs somewhere to point at")

    check("reindexing runs on a thread",
          "asyncio.to_thread(knowledge.reindex" in APP,
          "it is file I/O over a folder that may hold fifty documents, on "
          "the same loop as every live terminal session")
    check("and so does the availability check",
          "asyncio.to_thread(knowledge.is_configured)" in APP)

    check("the folder is made on request, not on startup",
          "asyncio.to_thread(folder.mkdir" in APP,
          "so that 'is there a folder' stays a question the state endpoint "
          "can answer honestly")


def test_the_router_retrieves_beside_chroma() -> None:
    print("\n-- Beside Chroma, not instead of it --")

    check("chroma is still queried",
          "chroma_client.query_design_guidelines" in ROUTER)
    check("and the knowledge folder as well",
          "_knowledge_snippets" in ROUTER)

    check("both start before the preamble and are collected after",
          re.search(r"knowledge_task = \(?asyncio\.create_task", ROUTER)
          is not None,
          "started first and collected last, so the round trips overlap the "
          "history reads rather than adding to them")
    check("the whole lookup is on a worker thread",
          re.search(r"asyncio\.to_thread\(\s*_knowledge_snippets", ROUTER)
          is not None,
          "the availability check is a sqlite read, and the point of the "
          "thread is that nothing on this path touches the loop")
    check("the last command is part of the query, not just the message",
          "_knowledge_query(" in ROUTER,
          "'is that right?' retrieves nothing; 'is `ip ospf network "
          "point-to-point` right?' retrieves the standards page")

    check("its snippets reach the prompt under their own heading",
          "knowledge_context=" in ROUTER
          and "knowledge_context" in prompts.build_context_prompt.__code__.co_varnames,
          "folded into the Chroma block they would be attributed to a "
          "server the user may not even run")

    block = prompts.build_context_prompt(
        [], "output", "sw1", [],
        knowledge_context="=== FROM YOUR KNOWLEDGE FOLDER ===\nuse ospf")
    check("and it is actually rendered", "FROM YOUR KNOWLEDGE FOLDER" in block,
          block[:400])
    check("an empty one adds nothing",
          "KNOWLEDGE" not in prompts.build_context_prompt([], "o", "sw1", []),
          "an 'I found nothing' header costs tokens on every message and "
          "invites the model to comment on the absence")


def test_the_lookup_never_raises() -> None:
    """
    On the path of an ordinary chat message.

    A missing snippet has to cost a little context, never the answer.
    """
    print("\n-- It never raises --")

    check("no index is not an error", knowledge.search("anything") == [])
    check("and neither is no folder", knowledge.is_configured() is False)
    check("formatting nothing gives nothing",
          knowledge.format_for_prompt([]) == "")


def test_the_panel_says_what_to_do_next() -> None:
    print("\n-- The panel --")

    check("there is a subsection", "Knowledge folder" in HTML)
    check("with the folder shown", 'id="knowledge-folder"' in HTML)
    check("a way to open it", 'id="knowledge-open"' in HTML)
    check("a way to index", 'id="knowledge-reindex"' in HTML)
    check("and a way to read everything again",
          'id="knowledge-rebuild"' in HTML,
          "the way out when an upgrade changes how documents are split up")

    for control in ("knowledge-reindex", "knowledge-rebuild"):
        tip = HTML.split(f'id="{control}"')[1].split(">")[0]
        check(f"{control} explains itself in two halves", tip.count("||") == 1,
              tip)

    check("a folder that does not exist yet says so",
          "That folder does not exist yet" in PANEL)
    check("an empty index says what to put in it",
          ".md or .txt files" in PANEL)
    check("and a build without FTS5 says why results are poor",
          "Full-text search is unavailable" in PANEL,
          "that fact should be visible somewhere other than the log")

    check("files that were passed over are named, with the reason",
          "renderSkipped" in PANEL,
          "a document silently skipped for being 4MB is a document somebody "
          "believes the assistant has read")
    check("as text, because they are names off the user's disk",
          "row.textContent" in PANEL)

    body = PANEL.split("function init")[1].split("\n  }")[0]
    check("nothing indexes on load",
          re.search(r"^\s*reindex\(", body, re.M) is None
          and "load();" in body,
          "reindex is reachable only from a click; a walk of fifty documents "
          "on every page load makes the panel feel broken on a slow disk")

    check("the script is loaded",
          'src="/static/js/knowledge.js"' in HTML)


def test_the_styling() -> None:
    print("\n-- Both themes --")

    block = CSS.split(".knowledge-folder")[1].split("\n}\n\n/*")[0] \
        if ".knowledge-folder" in CSS else ""
    check("there are styles for it", bool(block))
    hardcoded = re.findall(r"(?:background|color)\s*:\s*(#[0-9a-fA-F]{3,8}"
                           r"|rgba?\([^)]*\))",
                           CSS.split(".knowledge-folder")[1][:1200])
    check("no hardcoded colours", not hardcoded, str(hardcoded))
    check("a long path wraps rather than scrolling the panel sideways",
          "overflow-wrap: anywhere" in CSS)


def test_the_manual_says_it_replaces_nothing() -> None:
    print("\n-- The manual --")

    check("the knowledge folder is documented",
          "knowledge folder" in DOCS.lower())
    check("and Chroma still is",
          "Chroma" in DOCS,
          "a site that runs one has better retrieval than this")
    intro = DOCS.split("## Knowledge base")[1].split("###")[0]
    check("it says both are used when both are set",
          "both" in intro.lower(),
          "otherwise somebody with Chroma configured will assume this "
          f"turned it off — {intro[:200]}")


def main() -> int:
    print("=" * 52)
    print("  The knowledge folder, wired in")
    print("=" * 52)

    for test in (
        test_the_routes,
        test_the_router_retrieves_beside_chroma,
        test_the_lookup_never_raises,
        test_the_panel_says_what_to_do_next,
        test_the_styling,
        test_the_manual_says_it_replaces_nothing,
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
