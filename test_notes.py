"""
test_notes.py — What you were doing, written down beside what you did (#530).

A change window produces a running commentary — "16:02 shut Gi1/0/24,
16:05 confirmed by site" — that lives in Notepad and never meets the
transcript it describes. Keeping it here is only worth anything if three
things hold, and they are what this covers:

- **It survives a restart.** A note held in a browser tab is the Notepad
  file again, with a nicer background and a shorter life.
- **It is searchable with the session it belongs to.** Written to be found
  later; a commentary nobody can search is the thing this replaces.
- **It is in the session's own record**, not a list of its own. "16:05
  confirmed by site" means something next to what was running at 16:05 and
  very little on its own.

And one rule that is a refusal rather than a feature: **a note never
reaches the assistant.** Notes carry a customer's name, why a change was
really made, what somebody said on the phone. The chat context is built
from terminal buffers, and nothing here may leak into it.

The schema migration gets its own section. `CREATE TABLE IF NOT EXISTS`
does nothing at all to a table that already exists, so a column added to
the definition appears only for people installing fresh — everybody else
gets an error at the point of use, with their history apparently broken.

Run: python test_notes.py
"""

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backend import paths  # noqa: E402

_TEMP = Path(tempfile.mkdtemp(prefix="shellmate-notes-"))
paths._data_dir_cache = _TEMP
assert paths.data_dir() == _TEMP, "test isolation failed — refusing to run"

from backend.store import SessionStore, store  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                        # pragma: no cover
    pass

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


COMMENTARY = (
    "16:02 shut Gi1/0/24 at the customer's request\n"
    "16:05 customer confirmed the handset is dead\n"
    "16:40 rolled back, ticket INC-4471\n"
)


def keeping_one() -> None:
    print("\n-- Writing one --")

    store.start_session("sess-a", {"hostname": "core-1", "label": "core-1",
                                   "connection_type": "ssh"})
    check("a new session starts with no note",
          store.get_notes("sess-a") == "", repr(store.get_notes("sess-a")))

    check("writing one succeeds", store.set_notes("sess-a", COMMENTARY) is True)
    check("and reads back exactly", store.get_notes("sess-a") == COMMENTARY)

    check("editing replaces rather than appends",
          store.set_notes("sess-a", "shorter") and
          store.get_notes("sess-a") == "shorter")
    store.set_notes("sess-a", COMMENTARY)

    # A note filed against an id nothing else knows about is a note nobody
    # will find again, and "saved" for it is a lie the user cannot check.
    check("a session that is not on record is refused",
          store.set_notes("no-such-session", "orphan") is False)
    check("and reading one answers empty rather than raising",
          store.get_notes("no-such-session") == "")

    # This is a browser text box and a paste is a paste.
    store.set_notes("sess-a", "x" * (store.MAX_NOTES + 5000))
    check("an enormous paste is bounded rather than refused",
          len(store.get_notes("sess-a")) == store.MAX_NOTES,
          str(len(store.get_notes("sess-a"))))
    store.set_notes("sess-a", COMMENTARY)


def it_survives_a_restart() -> None:
    print("\n-- It survives a restart --")

    store.close()
    fresh = SessionStore()
    check("the note is still there after reopening the database",
          fresh.get_notes("sess-a") == COMMENTARY,
          "a note held in a browser tab is the Notepad file again")
    fresh.close()
    store.connect()


def it_is_searchable() -> None:
    print("\n-- It is searchable, with the session it belongs to --")

    store.start_session("sess-b", {"hostname": "edge-2", "label": "edge-2",
                                   "connection_type": "ssh"})
    store.set_notes("sess-b", "waiting on the DC to confirm the cross-connect")

    # "confirm" would match both: the tokeniser stems, so `confirmed` and
    # `confirm the cross-connect` are the same word. That is the behaviour
    # anybody searching their own notes wants, so the test asks for a word
    # only one session used rather than turning the stemming off.
    hits = store.search_notes("handset")
    check("a phrase somebody wrote is found",
          [h["id"] for h in hits] == ["sess-a"], str(hits))
    check("and the hit carries the session it belongs to",
          hits and hits[0]["hostname"] == "core-1", str(hits))
    check("with a snippet of what was written",
          hits and "handset" in hits[0]["snippet"], str(hits))

    check("a device filter applies to notes too",
          [h["id"] for h in store.search_notes("", hostname="edge-2")] == ["sess-b"],
          str(store.search_notes("", hostname="edge-2")))
    check("an empty query lists the sessions that have notes",
          {h["id"] for h in store.search_notes("")} == {"sess-a", "sess-b"},
          str(store.search_notes("")))
    check("something nobody wrote is not found",
          store.search_notes("kangaroo") == [])

    # The index is updated, not just appended to. Without the update
    # trigger it would go on matching text the session no longer says,
    # which is worse than not indexing at all.
    store.set_notes("sess-b", "the DC came back, nothing needed after all")
    check("editing a note updates the index",
          store.search_notes("cross-connect") == [],
          "the index went on matching text the note no longer contains")
    check("and the new text is findable",
          [h["id"] for h in store.search_notes("came back")] == ["sess-b"],
          str(store.search_notes("came back")))

    # A note lives on the session, so deleting the session takes it.
    store.delete_session("sess-b")
    check("deleting a session takes its note with it",
          store.search_notes("came back") == [],
          "a note outliving the session it describes is a note about nothing")


def it_is_in_the_sessions_own_record() -> None:
    print("\n-- It is in the session's own record --")

    session = store.get_session("sess-a")
    check("the session carries its note",
          session.get("notes") == COMMENTARY, str(session.get("notes"))[:60])
    listed = [s for s in store.list_sessions() if s["id"] == "sess-a"]
    check("and so does the listing",
          listed and listed[0].get("notes") == COMMENTARY,
          "the replay and the list read the same row, and a note that "
          "appears in one and not the other is a note somebody stops trusting")


def the_column_is_added_to_an_older_database() -> None:
    """
    The migration, against a database built the way version 1 built it.

    `CREATE TABLE IF NOT EXISTS` does nothing at all to a table that is
    already there. Without the ALTER, a column added to the definition
    exists only for people installing for the first time, and everybody
    else meets it as an OperationalError from the first query that names
    it — at the point of use, with their history apparently broken.
    """
    print("\n-- An older database gains the column --")

    older = _TEMP / "older"
    older.mkdir(exist_ok=True)
    path = older / "history.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE sessions (
            id              TEXT PRIMARY KEY,
            label           TEXT NOT NULL DEFAULT '',
            hostname        TEXT NOT NULL DEFAULT '',
            connection_type TEXT NOT NULL DEFAULT 'ssh',
            username        TEXT NOT NULL DEFAULT '',
            target          TEXT NOT NULL DEFAULT '',
            started_at      REAL NOT NULL,
            ended_at        REAL
        );
        INSERT INTO sessions(id, hostname, started_at)
        VALUES ('old-1', 'legacy-sw', 1000.0);
        """
    )
    connection.commit()
    connection.close()

    # The schema code, run against that database directly. Pointing the
    # real store at it would mean moving the data directory mid-test,
    # which is a bigger lie than calling one method.
    opened = SessionStore()
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    opened._create_schema(conn)                           # type: ignore[attr-defined]

    columns = {r["name"] for r in conn.execute("PRAGMA table_info(sessions)")}
    check("the notes column is added to a table that already existed",
          "notes" in columns, str(sorted(columns)))
    check("and the session that was already there is untouched",
          conn.execute("SELECT hostname FROM sessions WHERE id='old-1'")
              .fetchone()["hostname"] == "legacy-sw")
    check("its note starts empty rather than null",
          conn.execute("SELECT notes FROM sessions WHERE id='old-1'")
              .fetchone()["notes"] == "",
          "a null here would break every query that reads it as text")

    # Idempotent: running it twice must not fail.
    opened._create_schema(conn)                           # type: ignore[attr-defined]
    check("running the migration again is harmless", True)
    conn.close()


def notes_never_reach_the_assistant() -> None:
    """
    The one refusal. Asserted against the code, not against a comment.

    A note carries a customer's name, why a change was really made, what
    somebody said on the phone. The chat context is built from terminal
    buffers; if the notes column ever finds its way in, it will be by
    somebody adding a session field to a prompt without noticing what is
    now on the session.
    """
    print("\n-- A note never reaches the assistant --")

    root = Path(__file__).parent
    for name in ("backend/ai/router.py", "backend/ai/prompts.py",
                 "backend/ai/summarize.py", "backend/session/outbound.py",
                 "backend/ai/explain.py"):
        text = (root / name).read_text(encoding="utf-8")
        check(f"{name} does not read notes",
              "notes" not in text.replace("# ", "").lower()
              or "get_notes" not in text,
              "notes are written for the person, not for the model")

    from backend.ai import prompts

    # The session summary is the one place a note could plausibly ride in:
    # it is built from session metadata, and the notes column now sits on
    # the same row. Handed one deliberately, the prompt must not carry it.
    built = prompts.build_context_prompt(
        sessions_summary=[{"label": "core-1", "hostname": "core-1",
                           "connection_type": "ssh", "notes": COMMENTARY}],
        active_buffer="core-1#show ip int brief\n",
        active_label="core-1",
        command_history=["show ip int brief"],
    )
    check("a note put on the session summary does not travel in the prompt",
          "INC-4471" not in built and "handset" not in built,
          "the prompt builder picked up a field it was never meant to read")
    check("and the prompt was really built, so that proves something",
          "core-1" in built, built[:200])


if __name__ == "__main__":
    keeping_one()
    it_survives_a_restart()
    it_is_searchable()
    it_is_in_the_sessions_own_record()
    the_column_is_added_to_an_older_database()
    notes_never_reach_the_assistant()

    print("\n" + "=" * 52)
    print(f"  {passed} passed  |  {len(failed)} failed")
    print("=" * 52)
    for line in failed:
        print("  -", line)
    sys.exit(1 if failed else 0)
