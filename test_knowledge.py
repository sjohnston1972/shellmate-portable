"""
test_knowledge.py — The local knowledge folder (#561).

Retrieval over the team's own documents fails in ways nobody notices, which
is why these are asserted rather than eyeballed. Four of them would ship:

**A secret in a runbook reaches the API.** The folder holds site notes and
build guides, and those routinely carry the console password and the SNMP
community. The index is a local file and may hold the document as written,
but nothing may leave ``search()`` unmasked — and the masking has to happen
before the length cap, or a cut line hides the credential from the pattern
and not from the provider.

**A question with an IP address in it raises.** FTS5 has its own syntax in
which bare punctuation is a syntax error, so ``why is 10.1.1.1 flapping``
is not a query that returns nothing — it is an exception on the path of an
ordinary chat message.

**Retrieval costs more context than it saves.** Four un-capped sections of a
long runbook is a prompt bigger than the buffer they were meant to
supplement. Both caps are load-bearing: the per-snippet one stops a single
long section crowding the rest out, the total one is the actual promise.

**A file that was passed over is passed over silently.** Somebody drops a
PDF in and asks why the assistant has never heard of it. Every skip has to
carry its reason.

    python test_knowledge.py
"""

import os
import shutil
import sys
import tempfile
from pathlib import Path

from backend import paths

_TEMP = Path(tempfile.mkdtemp(prefix="shellmate-knowledge-"))
paths._data_dir_cache = _TEMP

from backend import knowledge                               # noqa: E402

passed = 0
failed: list[str] = []

FIXED_MTIME = 1_700_000_000


def check(name: str, condition: bool, detail: str = "") -> None:
    global passed
    if condition:
        passed += 1
        print(f"  OK  {name}")
    else:
        failed.append(f"{name}: {detail}")
        print(f"  FAIL {name}\n       {detail}")


def fresh() -> Path:
    """An empty knowledge folder and an empty index, for one test."""
    knowledge._close()
    folder = knowledge.knowledge_dir()
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        (_TEMP / f"knowledge.db{suffix}").unlink(missing_ok=True)
    return folder


def write(name: str, text: str, mtime: int = FIXED_MTIME) -> Path:
    """Write a document with a definite mtime, so re-indexing is testable."""
    path = knowledge.knowledge_dir() / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path


def reasons(report: dict) -> dict:
    """{filename: reason} from a reindex report."""
    return {entry["file"]: entry["reason"] for entry in report["skipped"]}


def headings(results: list[dict]) -> list[str]:
    return [r["heading"] for r in results]


# ---------------------------------------------------------------------------

def test_chunking_by_heading() -> None:
    """
    A heading is the author's own statement of what the block is about,
    which makes it both the retrieval unit and the only honest way for a
    snippet to say where it came from.
    """
    print("\n-- Chunked by heading --")
    fresh()

    write("site.md",
          "Site notes for Glasgow, written before any heading.\n"
          "\n"
          "# Console access\n"
          "\n"
          "The console server is a Lantronix on 10.9.9.9.\n"
          "\n"
          "## Baud rate\n"
          "\n"
          "Every switch on this site is 9600 8N1.\n")
    write("plain.txt", "A flat note with no headings at all, mentioning tacacs.\n")

    report = knowledge.reindex()
    check("both files are indexed", report["files"] == 2, str(report))
    check("the headed file becomes several chunks", report["chunks"] == 4,
          f"expected 3 from site.md (the preamble and two headings) plus 1 "
          f"from plain.txt, got {report['chunks']}")

    found = knowledge.search("baud rate")
    check("a section is found by its heading",
          "Baud rate" in headings(found), str(headings(found)))
    check("and the body comes with it",
          any("9600 8N1" in r["text"] for r in found), str(found))
    check("and it says which file it came from",
          all(r["source"] == "site.md" for r in found if r["heading"] == "Baud rate"),
          str(found))

    check("the heading line is not repeated in the body",
          all("# Baud rate" not in r["text"] for r in found),
          "it is printed by format_for_prompt, so leaving it in the body "
          "puts it in the prompt twice")

    lead = knowledge.search("Glasgow")
    check("text above the first heading is kept, not dropped",
          any("written before any heading" in r["text"] for r in lead), str(lead))

    flat = knowledge.search("tacacs")
    check("a file with no headings is one chunk with an empty heading",
          [r["heading"] for r in flat] == [""], str(flat))


def test_a_hash_inside_a_code_fence_is_not_a_heading() -> None:
    """
    Runbooks are mostly commands, and a shell comment starts with the same
    character as a heading. Splitting on one files half a procedure under a
    heading its author never wrote.
    """
    print("\n-- Fenced blocks --")
    fresh()

    write("reload.md",
          "# Reload procedure\n"
          "\n"
          "```bash\n"
          "# ssh to the terminal server first\n"
          "ssh admin@10.9.9.9\n"
          "```\n"
          "\n"
          "Then confirm the stack has rejoined.\n")

    report = knowledge.reindex()
    check("the whole procedure is one chunk", report["chunks"] == 1,
          str(report))

    found = knowledge.search("terminal server")
    check("and it is filed under the real heading",
          headings(found) == ["Reload procedure"], str(found))


def test_reindex_follows_mtime() -> None:
    print("\n-- Only what changed --")
    fresh()

    write("a.md", "# Alpha\n\nThe alpha runbook mentions ospf.\n")
    write("b.md", "# Bravo\n\nThe bravo runbook mentions eigrp.\n")

    first = knowledge.reindex()
    check("the first pass reads both", first["reindexed"] == 2, str(first))

    again = knowledge.reindex()
    check("the second pass reads nothing", again["reindexed"] == 0,
          "re-chunking forty-nine unchanged documents to catch the one that "
          "was edited is the whole reason mtime is stored")
    check("but the totals still describe the index",
          again["files"] == 2 and again["chunks"] == 2, str(again))

    write("b.md", "# Bravo\n\nThe bravo runbook now mentions isis.\n",
          mtime=FIXED_MTIME + 60)
    edited = knowledge.reindex()
    check("an edited file is read again", edited["reindexed"] == 1, str(edited))
    check("the new text is searchable",
          any("isis" in r["text"] for r in knowledge.search("isis")))
    check("and the old text is gone",
          knowledge.search("eigrp") == [],
          "an index that goes on matching text the document no longer "
          "contains is worse than no index, because the answer cites it")

    forced = knowledge.reindex(force=True)
    check("force re-reads everything", forced["reindexed"] == 2, str(forced))

    (knowledge.knowledge_dir() / "a.md").unlink()
    pruned = knowledge.reindex()
    check("a deleted file is dropped from the index",
          pruned["removed"] == 1 and pruned["files"] == 1, str(pruned))
    check("and stops being found", knowledge.search("ospf") == [])


def test_secrets_are_masked_on_the_way_out() -> None:
    """
    The #320/#463 rule. Documents in this folder are runbooks and site
    notes, and they carry credentials as a matter of course.
    """
    print("\n-- Redaction --")
    fresh()

    write("build.md",
          "# Password is swordfish\n"
          "\n"
          "The console password is hunter2 on every switch at this site.\n"
          "Set snmp-server community s3cr3tstring RO before handover.\n")

    knowledge.reindex()
    found = knowledge.search("console password handover")
    check("the passage is found", bool(found), "nothing matched")

    blob = "\n".join(r["text"] + r["heading"] for r in found)
    check("the password does not leave", "hunter2" not in blob, blob[:200])
    check("nor the community string", "s3cr3tstring" not in blob, blob[:200])
    check("nor one written into a heading", "swordfish" not in blob, blob[:200])
    check("and the shape of the line survives", "********" in blob, blob[:200])
    check("while the surrounding prose is intact", "every switch" in blob,
          "masking whole lines would leave a snippet nobody can use")


def test_redaction_runs_before_the_cap() -> None:
    """
    Order matters. Cutting first can split a line between the keyword and
    the value, leaving a credential in a form no pattern recognises — the
    same reason outbound.session_text cleans before it redacts.
    """
    print("\n-- Redact, then cut --")
    fresh()

    # The secret sits past the per-snippet cap, so a cap applied first would
    # have removed the text the pattern needs to see.
    filler = "The ospf adjacency notes go on for a while. " * 40
    write("late.md", f"# Handover\n\n{filler}\nThe enable password is hunter2.\n")

    knowledge.reindex()
    found = knowledge.search("ospf adjacency handover")
    blob = "".join(r["text"] for r in found)
    check("the document is found", bool(found), "nothing matched")
    check("a secret past the cap is still masked, not merely cut off",
          "hunter2" not in blob,
          "redacting the truncated string would mask what happened to "
          "survive the cut and nothing else")


def test_a_query_full_of_punctuation() -> None:
    """
    ``why is 10.1.1.1 flapping`` is an FTS5 syntax error unless every term
    is quoted — an exception on the path of an ordinary chat message.
    """
    print("\n-- Punctuation --")
    fresh()

    write("addresses.md",
          "# Core uplinks\n"
          "\n"
          "The uplink is Gi0/1 to 10.1.1.1 and it flaps in cold weather.\n")
    knowledge.reindex()

    for query in ("why does 10.1.1.1 keep flapping?",
                  "Gi0/1 errors",
                  "!!! ??? ---",
                  '"unbalanced quote AND NOT OR',
                  "*",
                  ""):
        try:
            results = knowledge.search(query)
            check(f"{query!r} does not raise", isinstance(results, list))
        except Exception as exc:
            check(f"{query!r} does not raise", False,
                  f"raised {type(exc).__name__}: {exc}")

    check("an address in a question still finds the line",
          any("10.1.1.1" in r["text"] for r in
              knowledge.search("why does 10.1.1.1 keep flapping?")),
          "quoting has to keep the term matchable, not just legal")
    check("and so does an interface name",
          any("Gi0/1" in r["text"] for r in knowledge.search("Gi0/1 errors")))
    check("a query of pure punctuation matches nothing rather than everything",
          knowledge.search("!!! ??? ---") == [])


def test_the_character_caps() -> None:
    print("\n-- Bounded by characters --")
    fresh()

    paragraph = ("The vlan migration steps for the distribution layer are "
                 "recorded here in full. ") * 12
    body = "\n\n".join([paragraph] * 12)
    write("long.md", f"# Vlan migration\n\n{body}\n")

    report = knowledge.reindex()
    check("a long section is split rather than stored whole",
          report["chunks"] >= 4, str(report))

    found = knowledge.search("vlan migration distribution", limit=4)
    check("several snippets come back", len(found) > 1, str(len(found)))
    check("no snippet exceeds the per-snippet cap",
          all(len(r["text"]) <= knowledge._MAX_SNIPPET_CHARS for r in found),
          str([len(r["text"]) for r in found]))
    total = sum(len(r["text"]) for r in found)
    check("and the lot fits the total cap",
          total <= knowledge._MAX_TOTAL_CHARS,
          f"{total} characters; retrieval that returns 40 KB spends the "
          f"context it was meant to save")
    check("the limit is honoured too", len(found) <= 4, str(len(found)))


def test_an_empty_folder() -> None:
    print("\n-- Nothing there --")
    fresh()

    check("nothing is configured before anything is indexed",
          knowledge.is_configured() is False)

    report = knowledge.reindex()
    check("an empty folder is not a failure",
          report["files"] == 0 and report["chunks"] == 0 and report["available"],
          str(report))
    check("and nothing was skipped", report["skipped"] == [], str(report))
    check("searching it returns nothing rather than raising",
          knowledge.search("anything at all") == [])
    check("and it is still not configured",
          knowledge.is_configured() is False,
          "a folder somebody made and left empty must not read as ready")

    stats = knowledge.stats()
    check("the stats say so plainly",
          stats["files"] == 0 and stats["chunks"] == 0
          and stats["indexed_at"] is None and stats["available"] is True,
          str(stats))

    write("first.md", "# Start here\n\nThe first note about qos.\n")
    knowledge.reindex()
    check("one indexed document is enough to be configured",
          knowledge.is_configured() is True)
    check("and the stats carry when it was indexed",
          knowledge.stats()["indexed_at"] is not None)


def test_what_gets_skipped_and_why() -> None:
    print("\n-- Skipped, with a reason --")
    fresh()

    write("good.md", "# Kept\n\nThis one is indexed and mentions mpls.\n")
    (knowledge.knowledge_dir() / "diagram.pdf").write_bytes(b"%PDF-1.4 not text")
    (knowledge.knowledge_dir() / "huge.md").write_bytes(
        b"# Huge\n\n" + b"x" * (knowledge._MAX_FILE_BYTES + 1))
    (knowledge.knowledge_dir() / "binary.txt").write_bytes(b"\x81\x8f\xfe\xff\x00")

    hidden = knowledge.knowledge_dir() / ".git"
    hidden.mkdir()
    (hidden / "COMMIT_EDITMSG").write_text("not a runbook", encoding="utf-8")

    report = knowledge.reindex()
    why = reasons(report)

    check("the readable one is still indexed",
          report["files"] == 1 and bool(knowledge.search("mpls")), str(report))
    check("the wrong extension is reported",
          "not a .md or .txt file" in why.get("diagram.pdf", ""), str(why))
    check("the oversized file is reported with the cap",
          "too large" in why.get("huge.md", ""), str(why))
    check("and undecodable bytes are reported as such",
          why.get("binary.txt") == "not UTF-8 text", str(why))
    check("a dot-directory is stepped over without comment",
          not any(entry["file"].startswith(".git") for entry in report["skipped"]),
          "a runbook folder that is a git clone would otherwise report "
          "several thousand skips to make one point")


def test_a_file_that_cannot_be_read() -> None:
    """
    A document locked by an editor, or one the account cannot open. It is
    reported, and — importantly — whatever was indexed before survives, so
    a two-second lock does not lose a runbook indexed yesterday.
    """
    print("\n-- Unreadable --")
    fresh()

    write("locked.md", "# Locked\n\nThe bgp dampening notes live here.\n")
    knowledge.reindex()
    check("it is indexed while it can be read",
          bool(knowledge.search("dampening")))

    original = knowledge._read_text

    def refuse(path):
        if path.name == "locked.md":
            raise PermissionError(13, "Permission denied")
        return original(path)

    knowledge._read_text = refuse
    try:
        write("locked.md", "# Locked\n\nEdited while unreadable.\n",
              mtime=FIXED_MTIME + 120)
        report = knowledge.reindex()
    finally:
        knowledge._read_text = original

    why = reasons(report)
    check("the failure is reported rather than swallowed",
          "cannot be read" in why.get("locked.md", ""), str(why))
    check("nothing was reindexed from it", report["reindexed"] == 0, str(report))
    check("and what was already indexed survives",
          bool(knowledge.search("dampening")),
          "a transient lock must not empty the index")


def test_without_fts5() -> None:
    """
    FTS5 ships with the SQLite bundled in CPython on Windows and macOS but
    is not guaranteed, which is why it is probed rather than assumed.
    Losing the ranking is acceptable; losing the folder is not.
    """
    print("\n-- No FTS5 --")
    fresh()

    write("fallback.md",
          "# Stack members\n\nRenumber the stack member before reseating it.\n")
    knowledge.reindex()

    knowledge._fts_enabled = False
    try:
        found = knowledge.search("renumber stack member")
        stats = knowledge.stats()
    finally:
        knowledge._fts_enabled = True

    check("the passage is still found by scanning",
          any("Renumber the stack" in r["text"] for r in found), str(found))
    check("and it still carries its source and heading",
          found and found[0]["source"] == "fallback.md"
          and found[0]["heading"] == "Stack members", str(found))
    check("with no invented relevance score",
          all(r["score"] == 0.0 for r in found),
          "a made-up number reads as relevance the scan never established")
    check("and the stats say which mode is in use",
          stats["search"] == "like", str(stats))


def test_format_for_prompt() -> None:
    print("\n-- The prompt block --")

    check("nothing to add adds nothing", knowledge.format_for_prompt([]) == "",
          "an 'I found nothing' header costs tokens on every message and "
          "invites the model to comment on the absence")
    check("and None is the same", knowledge.format_for_prompt(None) == "")

    block = knowledge.format_for_prompt([
        {"text": "9600 8N1.", "source": "site.md", "heading": "Baud rate",
         "score": 1.0},
        {"text": "A flat note.", "source": "plain.txt", "heading": "",
         "score": 0.0},
    ])
    check("the block says where it came from",
          block.startswith("=== FROM YOUR KNOWLEDGE FOLDER ==="), block[:60])
    check("each snippet names its file and heading",
          "--- site.md — Baud rate ---" in block, block)
    check("a headingless snippet still names its file",
          "--- plain.txt ---" in block, block)
    check("and the text is there", "9600 8N1." in block, block)


def main() -> int:
    print("=" * 52)
    print("  Knowledge - the folder the assistant can read")
    print("=" * 52)

    for test in (
        test_chunking_by_heading,
        test_a_hash_inside_a_code_fence_is_not_a_heading,
        test_reindex_follows_mtime,
        test_secrets_are_masked_on_the_way_out,
        test_redaction_runs_before_the_cap,
        test_a_query_full_of_punctuation,
        test_the_character_caps,
        test_an_empty_folder,
        test_what_gets_skipped_and_why,
        test_a_file_that_cannot_be_read,
        test_without_fts5,
        test_format_for_prompt,
    ):
        try:
            test()
        except Exception as exc:
            failed.append(f"{test.__name__}: raised {type(exc).__name__}: {exc}")
            print(f"  FAIL {test.__name__} raised {type(exc).__name__}: {exc}")

    knowledge._close()
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
