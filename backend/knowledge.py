"""
knowledge.py — The team's own documents, retrievable without a server (#561).

`chroma_client.py` promises retrieval over your own material and then asks
for a running Chroma with server-side embeddings. A network engineer with a
portable executable on a memory stick, on a site network with no route out,
cannot stand that up — so the feature exists and nobody has it. This is the
same promise served from a folder: drop `.md` and `.txt` runbooks, standards
and site notes into ``ShellMate-Data/knowledge/`` and the assistant can quote
them back.

Four constraints shape everything below.

**Its own database file.** ``knowledge.db``, not the session store. The store
is the record of what happened on real devices and it is written to from the
terminal read loop; an index that is rebuilt wholesale whenever somebody edits
a runbook has no business sharing a connection, a lock or a file with it.

**Redaction on the way out, not on the way in.** Runbooks routinely carry the
console password for a site, the SNMP community, the pre-shared key that was
never rotated. The index holds the document as written — it is a local file
that the user already has — but nothing leaves ``search()`` without going
through ``outbound.redact_text``. That is the #320/#463 rule: one door out,
masked at the point of sending.

**A cap in characters, on each snippet and on the lot.** Retrieval exists to
spend fewer tokens than pasting the manual would. Four un-capped sections of a
long runbook is forty kilobytes of prompt, which is retrieval costing more than
the thing it replaced.

**Never raise into chat.** Every public function here swallows and logs.
A malformed file, a missing folder or an sqlite build without FTS5 must cost
the user a snippet, never an answer.

Lexical retrieval is what this is: it matches words, so a question phrased
entirely differently from the document will miss it. That is the honest limit,
and it is still the difference between having the team's runbooks and not.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
import time
from pathlib import Path

from backend import paths
from backend.session.outbound import redact_text

logger = logging.getLogger(__name__)

#: The database lives beside the exe like everything else writable, and is
#: named for what it is so a user clearing a bad index knows which file to
#: delete. Never derived from __file__ — see the portable-runtime rules.
_DB_NAME = "knowledge.db"

#: What counts as a document. Deliberately narrow: a PDF or a .docx read as
#: bytes indexes as noise, and indexing noise is worse than skipping it,
#: because the noise wins retrieval slots from the material that is readable.
_EXTENSIONS = {".md", ".txt"}

#: A single file cap. A megabyte of Markdown is roughly a 300-page manual;
#: past that the thing in the folder is almost certainly a log or an export
#: somebody dropped there by accident, and chunking it would take the reindex
#: from instant to noticeable.
_MAX_FILE_BYTES = 1024 * 1024

#: A chunk cap, applied *within* a heading. A 40 KB section under one heading
#: is one row that wins on term frequency and then blows the snippet budget on
#: its own; splitting it keeps both the ranking and the cap meaningful.
_MAX_CHUNK_CHARS = 2000

#: What one snippet may contribute to a prompt, and what all of them may
#: contribute together. Both are needed: the per-snippet cap stops one long
#: section crowding out the other three, and the total cap is the actual
#: promise — that retrieval costs a bounded amount of context no matter how
#: many snippets matched or how big the folder is.
_MAX_SNIPPET_CHARS = 1200
_MAX_TOTAL_CHARS = 4000

#: A snippet shorter than this is not worth a heading and a separator, so when
#: the total budget has this little left the block simply ends.
_MIN_SNIPPET_CHARS = 200

#: How many words of a question are worth searching on. A chat message plus
#: the last command can run long, and every extra term costs a scan and adds
#: noise to the ranking rather than precision.
_MAX_TERMS = 24

#: Words that match everything and therefore rank nothing.
_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "what", "why", "how",
    "when", "which", "does", "did", "are", "was", "were", "you", "your", "our",
    "can", "should", "would", "there", "here", "have", "has", "not", "but",
    "into", "onto", "about", "any", "all", "its", "it's", "get", "got",
}

#: How many skipped files to name. The reason a skip is reported at all is so
#: somebody can see why their runbook is not being found; a folder with two
#: thousand JPEGs in it would otherwise return a two-thousand-entry list to
#: the browser to make that one point.
_MAX_SKIPPED = 50

# One connection behind one lock, for the same reason store.py does it: reads
# arrive from asyncio worker threads while a reindex may be committing from
# another. check_same_thread=False disables Python's guard; it does not make
# the connection safe, so the lock is what actually does it. Reentrant because
# reindex() calls the same helpers a caller may already hold the lock for.
_connection: sqlite3.Connection | None = None
_lock = threading.RLock()
_fts_enabled = False

#: An ATX heading. Setext (underlined) headings are not recognised, and that
#: is a deliberate limit rather than an oversight — a line of dashes under a
#: line of prose is also how people draw a rule, and splitting on it would cut
#: chunks in the middle of a sentence.
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")

#: A fenced code block. Runbooks are mostly commands, and a shell comment
#: inside a fence begins with the same character as a heading — without this,
#: ``# ssh into the console server`` in a bash block starts a new chunk with a
#: heading that was never a heading.
_FENCE_RE = re.compile(r"^\s*(```|~~~)")


# ---------------------------------------------------------------------------
# Where it lives
# ---------------------------------------------------------------------------

def knowledge_dir() -> Path:
    """
    The folder the user drops documents into.

    A pure path — this does not create anything, because ``is_configured()``
    is partly "does the folder exist", and a getter that quietly creates the
    folder would make that question answer itself.
    """
    return paths.data_dir() / "knowledge"


# ---------------------------------------------------------------------------
# The index
# ---------------------------------------------------------------------------

def _connect() -> sqlite3.Connection:
    """Open (and on first call create) the knowledge index."""
    global _connection

    if _connection is not None:
        return _connection

    path = paths.data_dir() / _DB_NAME
    path.parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(str(path), check_same_thread=False)
    connection.row_factory = sqlite3.Row
    # WAL so a search during a reindex reads rather than blocks: the reindex
    # is the slow operation here and it is exactly when somebody is likely to
    # be asking a question about what they have just added.
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")

    _create_schema(connection)
    _connection = connection
    return connection


def _create_schema(connection: sqlite3.Connection) -> None:
    """Create the tables, and probe for FTS5 rather than assuming it."""
    global _fts_enabled

    connection.executescript(
        """
        -- One row per indexed file, carrying the mtime that decides whether
        -- it has to be read again. Keyed on the path relative to the
        -- knowledge folder, so moving ShellMate-Data does not invalidate
        -- every row in the index.
        CREATE TABLE IF NOT EXISTS files (
            path       TEXT PRIMARY KEY,
            mtime      REAL NOT NULL,
            size       INTEGER NOT NULL,
            chunks     INTEGER NOT NULL DEFAULT 0,
            indexed_at REAL NOT NULL
        );

        -- The chunks themselves. This table is the source of truth; the FTS
        -- index below is external-content over it, so when FTS5 is missing
        -- the content is all still here to be scanned with LIKE.
        CREATE TABLE IF NOT EXISTS chunks (
            id      INTEGER PRIMARY KEY,
            path    TEXT NOT NULL,
            heading TEXT NOT NULL DEFAULT '',
            text    TEXT NOT NULL DEFAULT ''
        );

        CREATE INDEX IF NOT EXISTS idx_chunks_path ON chunks(path);
        """
    )

    # FTS5 ships with the SQLite bundled in CPython on Windows and macOS but
    # is not guaranteed, so this is a probe and not an assumption — the same
    # pattern as store.py. Losing ranking is much better than losing the
    # feature, so the failure sets a flag and search() scans with LIKE.
    try:
        connection.executescript(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
                heading, text,
                content='chunks', content_rowid='id',
                tokenize='porter unicode61'
            );

            CREATE TRIGGER IF NOT EXISTS knowledge_ai AFTER INSERT ON chunks BEGIN
                INSERT INTO knowledge_fts(rowid, heading, text)
                VALUES (new.id, new.heading, new.text);
            END;

            -- A file is reindexed by deleting its chunks and inserting the
            -- new ones, so without the delete trigger the index would go on
            -- matching text the document no longer contains — which is worse
            -- than not indexing it, because the answer would cite it.
            CREATE TRIGGER IF NOT EXISTS knowledge_ad AFTER DELETE ON chunks BEGIN
                INSERT INTO knowledge_fts(knowledge_fts, rowid, heading, text)
                VALUES ('delete', old.id, old.heading, old.text);
            END;
            """
        )
        _fts_enabled = True
    except sqlite3.OperationalError as exc:
        logger.warning("Knowledge full-text search unavailable, "
                       "falling back to LIKE: %s", exc)
        _fts_enabled = False

    connection.commit()


def _close() -> None:
    """
    Drop the connection.

    For shutdown and for tests, which point ``paths.data_dir()`` somewhere
    else between cases and would otherwise keep talking to the first file
    they opened.
    """
    global _connection, _fts_enabled
    with _lock:
        if _connection is not None:
            try:
                _connection.close()
            except Exception:                                # pragma: no cover
                pass
        _connection = None
        _fts_enabled = False


# ---------------------------------------------------------------------------
# Reading and chunking
# ---------------------------------------------------------------------------

def _read_text(path: Path) -> str:
    """
    Read one document as text.

    ``utf-8-sig`` because Notepad writes a BOM and a leading ``\\ufeff`` on the
    first heading stops it matching. Strict otherwise: a file that is not text
    is reported as skipped rather than decoded into mojibake and indexed,
    because mojibake in the index is retrieval noise nobody can trace.
    """
    return path.read_text(encoding="utf-8-sig")


def _chunk(text: str) -> list[tuple[str, str]]:
    """
    Split a document into ``(heading, body)`` pairs.

    By heading, because a heading is the author's own statement of what the
    block below it is about — which makes it both a good retrieval unit and
    the only honest way for a snippet to say where it came from. A document
    with no headings is one chunk; text above the first heading is a chunk
    with an empty heading rather than being silently dropped.

    The heading line itself is kept out of the body: it is indexed as its own
    column (and weighted above the body when ranking), and
    ``format_for_prompt`` prints it, so leaving it in the body would put it in
    the prompt twice.
    """
    sections: list[tuple[str, list[str]]] = [("", [])]
    fenced = False

    for line in text.splitlines():
        if _FENCE_RE.match(line):
            fenced = not fenced
            sections[-1][1].append(line)
            continue

        match = None if fenced else _HEADING_RE.match(line)
        if match:
            sections.append((match.group(2).strip(), []))
        else:
            sections[-1][1].append(line)

    chunks: list[tuple[str, str]] = []
    for heading, lines in sections:
        body = "\n".join(lines).strip()
        if not body and not heading:
            continue
        for piece in _split_long(body):
            chunks.append((heading, piece))
    return chunks


def _split_long(body: str) -> list[str]:
    """
    Break an over-long section on blank lines, keeping paragraphs whole.

    A section is split rather than truncated because the tail of a long
    procedure is exactly the part somebody is looking for — the rollback step
    is at the bottom.
    """
    if len(body) <= _MAX_CHUNK_CHARS:
        return [body]

    pieces: list[str] = []
    current = ""
    for paragraph in body.split("\n\n"):
        # A single paragraph longer than the cap has no blank line to break
        # on, so it is cut on length. Rare, and better than one enormous row.
        while len(paragraph) > _MAX_CHUNK_CHARS:
            pieces.append(paragraph[:_MAX_CHUNK_CHARS])
            paragraph = paragraph[_MAX_CHUNK_CHARS:]
        if current and len(current) + len(paragraph) + 2 > _MAX_CHUNK_CHARS:
            pieces.append(current)
            current = paragraph
        else:
            current = f"{current}\n\n{paragraph}" if current else paragraph
    if current.strip():
        pieces.append(current)
    return [piece for piece in pieces if piece.strip()]


def _walk(folder: Path, skipped: list[dict]) -> list[Path]:
    """
    Every candidate document under the folder, noting what was passed over.

    Dot-directories are stepped over without comment. Somebody who keeps their
    runbooks in a git clone would otherwise get several thousand skip entries
    describing ``.git`` — a report that long is not a report.
    """
    found: list[Path] = []
    try:
        entries = sorted(folder.rglob("*"))
    except OSError as exc:                                   # pragma: no cover
        logger.warning("Cannot walk %s: %s", folder, exc)
        return found

    for path in entries:
        try:
            parts = path.relative_to(folder).parts
        except ValueError:                                   # pragma: no cover
            continue
        if any(part.startswith(".") for part in parts):
            continue
        if not path.is_file():
            continue
        if path.suffix.lower() not in _EXTENSIONS:
            skipped.append({"file": "/".join(parts),
                            "reason": "not a .md or .txt file"})
            continue
        found.append(path)
    return found


# ---------------------------------------------------------------------------
# Indexing
# ---------------------------------------------------------------------------

def reindex(force: bool = False) -> dict:
    """
    Walk the knowledge folder and bring the index up to date.

    A file is read again only when its mtime or size has changed, because the
    common case is a folder of fifty documents of which one was edited, and
    re-chunking the other forty-nine buys nothing. ``force`` re-reads
    everything, which is the way out when a chunking change means the index
    was built by an older version of this module.

    Returns:
        ``files`` and ``chunks`` are the totals now in the index — what a
        settings panel wants to show. ``reindexed`` and ``removed`` describe
        this pass. ``skipped`` names what was passed over and why, capped in
        length. ``available`` is whether there is an index at all.
    """
    started = time.monotonic()
    result: dict = {"files": 0, "chunks": 0, "skipped": [], "took_s": 0.0,
                    "available": False, "reindexed": 0, "removed": 0}
    skipped: list[dict] = []

    try:
        with _lock:
            connection = _connect()
            result["available"] = True

            folder = knowledge_dir()
            # Created here rather than in knowledge_dir(): a reindex is an
            # explicit act, and after it the user has somewhere to put files.
            folder.mkdir(parents=True, exist_ok=True)

            known = {row["path"]: row for row in
                     connection.execute("SELECT path, mtime, size FROM files")}
            seen: set[str] = set()

            for path in _walk(folder, skipped):
                relative = "/".join(path.relative_to(folder).parts)
                try:
                    stat = path.stat()
                except OSError as exc:
                    skipped.append({"file": relative,
                                    "reason": f"cannot be read: {exc}"})
                    continue

                if stat.st_size > _MAX_FILE_BYTES:
                    skipped.append({
                        "file": relative,
                        "reason": (f"too large ({stat.st_size // 1024} KB; "
                                   f"the cap is {_MAX_FILE_BYTES // 1024} KB)")})
                    continue

                seen.add(relative)
                previous = known.get(relative)
                if (previous is not None and not force
                        and previous["mtime"] == stat.st_mtime
                        and previous["size"] == stat.st_size):
                    continue

                try:
                    text = _read_text(path)
                except UnicodeDecodeError:
                    skipped.append({"file": relative,
                                    "reason": "not UTF-8 text"})
                    continue
                except OSError as exc:
                    # Whatever was indexed before stays. A file locked by an
                    # editor for the two seconds this ran is not a reason to
                    # lose a runbook that was indexed fine yesterday.
                    skipped.append({"file": relative,
                                    "reason": f"cannot be read: {exc}"})
                    continue

                _replace(connection, relative, stat.st_mtime, stat.st_size,
                         _chunk(text))
                result["reindexed"] += 1

            for relative in known:
                if relative not in seen:
                    _forget(connection, relative)
                    result["removed"] += 1

            connection.commit()

            counts = connection.execute(
                "SELECT (SELECT COUNT(*) FROM files) AS files, "
                "       (SELECT COUNT(*) FROM chunks) AS chunks").fetchone()
            result["files"] = counts["files"]
            result["chunks"] = counts["chunks"]

    except Exception as exc:
        # A broken index must not break the chat, so this reports rather than
        # raises; `available` false is how the panel says so.
        logger.warning("Knowledge reindex failed: %s", exc)

    if len(skipped) > _MAX_SKIPPED:
        extra = len(skipped) - _MAX_SKIPPED
        skipped = skipped[:_MAX_SKIPPED]
        skipped.append({"file": "", "reason": f"and {extra} more not listed"})
    result["skipped"] = skipped
    result["took_s"] = round(time.monotonic() - started, 3)
    return result


def _replace(connection: sqlite3.Connection, relative: str, mtime: float,
             size: int, chunks: list[tuple[str, str]]) -> None:
    """Swap one file's chunks for the ones just read."""
    connection.execute("DELETE FROM chunks WHERE path = ?", (relative,))
    connection.executemany(
        "INSERT INTO chunks(path, heading, text) VALUES (?, ?, ?)",
        [(relative, heading, body) for heading, body in chunks])
    connection.execute(
        "INSERT OR REPLACE INTO files(path, mtime, size, chunks, indexed_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (relative, mtime, size, len(chunks), time.time()))


def _forget(connection: sqlite3.Connection, relative: str) -> None:
    """Drop a file that is no longer in the folder."""
    connection.execute("DELETE FROM chunks WHERE path = ?", (relative,))
    connection.execute("DELETE FROM files WHERE path = ?", (relative,))


# ---------------------------------------------------------------------------
# Searching
# ---------------------------------------------------------------------------

def _terms(query: str) -> list[str]:
    """
    The words worth searching on.

    Anything with no letter or digit in it is dropped: a term of pure
    punctuation contributes nothing to the ranking and, quoted into an FTS5
    expression, tokenises to nothing at all.
    """
    words: list[str] = []
    for raw in (query or "").replace('"', " ").split():
        word = raw.strip(".,;:!?()[]{}<>'\"`")
        if len(word) < 2:
            continue
        if not any(character.isalnum() for character in word):
            continue
        if word.lower() in _STOPWORDS:
            continue
        words.append(word)
        if len(words) >= _MAX_TERMS:
            break
    return words


def _fts_query(terms: list[str]) -> str:
    """
    Turn words into a safe FTS5 expression.

    Two things this has to get right, and they pull in opposite directions.

    **Quoting.** FTS5 has its own syntax in which bare punctuation is a
    *syntax error*, so a question mentioning ``10.1.1.1`` or ``Gi0/1`` would
    raise rather than match. Every term is quoted, which also neutralises a
    word that happens to be an operator — ``NOT``, ``OR``, ``NEAR``.

    **OR, not AND.** ``store.py`` ANDs its terms, and is right to: that is a
    search box where somebody types the words they want all of. This is fed a
    whole chat message and the last command run, and ANDing twenty words from
    a sentence matches no document ever written. Ranking is what separates the
    results here, not the match.
    """
    return " OR ".join(f'"{term}"' for term in terms)


def search(query: str, limit: int = 4) -> list[dict]:
    """
    Find passages relevant to a question.

    Returns a list of ``{"text", "source", "heading", "score"}``, capped both
    per snippet and in total, and redacted. Returns ``[]`` for anything that
    goes wrong — no index, no match, an sqlite build without FTS5 and a query
    that matches nothing under LIKE either. It never raises: this is called on
    the path of an ordinary chat message, and a missing snippet has to cost a
    little context rather than the answer.
    """
    try:
        terms = _terms(query)
        if not terms or limit <= 0:
            return []

        with _lock:
            connection = _connect()
            rows: list[sqlite3.Row] = []

            if _fts_enabled:
                try:
                    rows = connection.execute(
                        """
                        SELECT c.path, c.heading, c.text,
                               -bm25(knowledge_fts, 2.0, 1.0) AS score
                        FROM knowledge_fts
                        JOIN chunks c ON c.id = knowledge_fts.rowid
                        WHERE knowledge_fts MATCH ?
                        ORDER BY bm25(knowledge_fts, 2.0, 1.0)
                        LIMIT ?
                        """,
                        # The heading is weighted above the body: a section
                        # called "Console password reset" is about that, while
                        # a body mentioning it in passing may not be.
                        (_fts_query(terms), int(limit)),
                    ).fetchall()
                except sqlite3.Error as exc:
                    logger.warning("Knowledge search failed, "
                                   "falling back to LIKE: %s", exc)
                    rows = []

            if not rows:
                rows = _like_scan(connection, terms, int(limit))

        return _snippets(rows)

    except Exception as exc:
        logger.warning("Knowledge search error: %s", exc)
        return []


def _like_scan(connection: sqlite3.Connection, terms: list[str],
               limit: int) -> list[sqlite3.Row]:
    """
    The fallback when FTS5 is absent — or when it is present and matched
    nothing, since a LIKE finds a term inside a longer word that the
    tokeniser split differently.
    """
    clauses = " OR ".join(["c.heading LIKE ? OR c.text LIKE ?"] * len(terms))
    params: list = []
    for term in terms:
        params.extend([f"%{term}%", f"%{term}%"])
    params.append(limit)
    try:
        return connection.execute(
            f"SELECT c.path, c.heading, c.text, 0.0 AS score "
            f"FROM chunks c WHERE {clauses} ORDER BY c.id LIMIT ?",
            params).fetchall()
    except sqlite3.Error as exc:                             # pragma: no cover
        logger.warning("Knowledge LIKE scan failed: %s", exc)
        return []


def _snippets(rows) -> list[dict]:
    """
    Redact, then cap — in that order, and the order matters.

    Truncating first can cut a line in half between the keyword and the value,
    leaving a secret in a form no pattern recognises. This is the same
    reasoning as ``outbound.session_text`` cleaning before redacting: the
    masking step only works on text that still has the shape it was written
    in. The heading goes through it too — "Core switch password is hunter2" is
    a real heading somebody has written.

    A score of ``0.0`` means the LIKE fallback ranked nothing; inventing a
    number there would read as relevance the search never established.
    """
    out: list[dict] = []
    budget = _MAX_TOTAL_CHARS

    for row in rows:
        if budget < _MIN_SNIPPET_CHARS:
            break
        text = redact_text(row["text"] or "")
        allowance = min(_MAX_SNIPPET_CHARS, budget)
        if len(text) > allowance:
            text = text[:allowance - 1] + "…"
        if not text.strip():
            continue
        budget -= len(text)
        out.append({
            "text": text,
            "source": row["path"],
            "heading": redact_text(row["heading"] or ""),
            "score": row["score"],
        })
    return out


def format_for_prompt(snippets: list[dict] | None) -> str:
    """
    Render snippets as a prompt block, or "" when there are none.

    Empty means empty: an "I found nothing" header still costs tokens on every
    message and invites the model to comment on the absence.
    """
    if not snippets:
        return ""
    lines = ["=== FROM YOUR KNOWLEDGE FOLDER ==="]
    for snippet in snippets:
        source = snippet.get("source") or "knowledge"
        heading = snippet.get("heading") or ""
        lines.append(f"--- {source} — {heading} ---" if heading
                     else f"--- {source} ---")
        lines.append(snippet.get("text") or "")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# What the panel asks
# ---------------------------------------------------------------------------

def is_configured() -> bool:
    """
    Whether there is anything to retrieve.

    Both halves are needed. A folder somebody made and left empty is not
    configured, and neither is an index left behind by a folder that has since
    been deleted — answering yes to either puts a "searching your documents"
    claim in front of a user whose documents are not there.
    """
    try:
        if not knowledge_dir().is_dir():
            return False
        with _lock:
            connection = _connect()
            row = connection.execute(
                "SELECT COUNT(*) AS n FROM chunks").fetchone()
            return bool(row and row["n"])
    except Exception as exc:
        logger.warning("Knowledge availability check failed: %s", exc)
        return False


def stats() -> dict:
    """
    What is indexed, for the settings panel.

    ``search`` is not part of the contract the router uses; it is here because
    "why are my results poor" has two very different answers depending on
    whether this build has FTS5, and that fact should be visible somewhere
    other than the log.
    """
    result = {"files": 0, "chunks": 0, "indexed_at": None,
              "available": False, "search": "none"}
    try:
        with _lock:
            connection = _connect()
            row = connection.execute(
                "SELECT (SELECT COUNT(*) FROM files) AS files, "
                "       (SELECT COUNT(*) FROM chunks) AS chunks, "
                "       (SELECT MAX(indexed_at) FROM files) AS indexed_at"
            ).fetchone()
            result["files"] = row["files"]
            result["chunks"] = row["chunks"]
            result["indexed_at"] = row["indexed_at"]
            result["available"] = True
            result["search"] = "fts5" if _fts_enabled else "like"
    except Exception as exc:
        logger.warning("Knowledge stats failed: %s", exc)
    return result
