"""
store.py — Persistent session history in SQLite.

Session logs used to be flat text in a folder, which makes "what did I change
on the Glasgow core last Tuesday" a grep exercise rather than a question.
This turns the same material into something you can query: commands, their
output, which device they ran on, and when.

Uses the standard library's ``sqlite3`` — no extra dependency, and the file
lives in the portable data directory so history travels with the executable.
Full-text search is FTS5, which ships with the SQLite bundled in CPython on
Windows and macOS.  Where it is absent the store degrades to LIKE matching
rather than failing, because losing search is much better than losing history.

**Threading.** The WebSocket bridge runs blocking work in worker threads, so
writes arrive from several threads. SQLite connections are not thread-safe, so
this uses one connection with ``check_same_thread=False`` behind a lock.
Writes are one small row per command, so the lock is never contended for long,
and a single connection keeps WAL mode and the FTS index consistent.
"""

import logging
import queue
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Any

from backend import paths

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 2

# Cap on stored output per command. A "show tech-support" runs to megabytes,
# and storing it whole would bloat the database for no practical gain.
MAX_OUTPUT_CHARS = 256 * 1024



def _redacted(text: str) -> str:
    """
    Command output and configuration snapshots go into the database
    redacted whenever `logging.redact_secrets` is on (#463). The setting is
    described as one switch covering session logs and archived
    configurations; the history file sits on the same stick as the vault
    and used to hold every `show run` with its secrets intact.
    """
    if not text:
        return text
    try:
        from backend.settings_store import peek
        if not peek("logging", "redact_secrets", True):
            return text
        from backend.session.redact import redact
        return redact(text)
    except Exception:
        return text

def _recording_enabled() -> bool:
    """Whether to record at all. Off means no search, replay or drift check."""
    try:
        from backend.advanced import get as advanced
        return bool(advanced("history.record"))
    except Exception:
        return True


def _retention_days() -> int:
    """Zero means keep forever, which is the default."""
    try:
        from backend.advanced import get as advanced
        return int(advanced("history.retention_days"))
    except Exception:
        return 0


def _max_output_chars() -> int:
    """The cap, honouring the Stockton override. A full `show tech` exceeds it."""
    try:
        from backend.advanced import get as advanced
        return int(advanced("history.max_output_chars"))
    except Exception:
        return MAX_OUTPUT_CHARS


@dataclass
class SearchHit:
    """One command matching a search."""

    command_id: int
    session_id: str
    hostname: str
    label: str
    command: str
    snippet: str
    ran_at: float

    def as_dict(self) -> dict:
        return {
            "command_id": self.command_id,
            "session_id": self.session_id,
            "hostname":   self.hostname,
            "label":      self.label,
            "command":    self.command,
            "snippet":    self.snippet,
            "ran_at":     self.ran_at,
        }


class SessionStore:
    """SQLite-backed history of every session, command and config snapshot."""

    def __init__(self) -> None:
        self._connection: sqlite3.Connection | None = None
        # Reentrant on purpose. Reads take the lock now, and add_snapshot()
        # consults latest_snapshot() before deciding whether to insert — with
        # a plain Lock that is a deadlock, and splitting them so it is not
        # would put the check and the write back on opposite sides of the
        # lock, which is the race the issue was about. RLock lets one thread
        # hold both and makes the pair atomic.
        self._lock = threading.RLock()
        self._fts_enabled = False
        # One writer thread, fed by a queue (#459). The terminal read loop
        # used to call add_command() inline: a SELECT, an INSERT and a
        # commit (an fsync) on the event loop for every command on any tab,
        # and under the same lock a history search can hold for most of a
        # second. Now the loop hands the record over and carries on.
        self._queue: queue.Queue = queue.Queue()
        self._writer: threading.Thread | None = None
        self._writer_lock = threading.Lock()
        # The next sequence number per session, so a write does not begin
        # with a SELECT MAX() over the session's rows.
        self._sequences: dict[str, int] = {}

    # ------------------------------------------------------------------
    # Connection and schema
    # ------------------------------------------------------------------

    def connect(self) -> sqlite3.Connection:
        """Open the database, creating and migrating the schema as needed."""
        if self._connection is not None:
            return self._connection

        path = paths.db_file()
        path.parent.mkdir(parents=True, exist_ok=True)

        connection = sqlite3.connect(str(path), check_same_thread=False)
        connection.row_factory = sqlite3.Row
        # WAL lets a long read (browsing history) run while a session is still
        # writing commands, instead of the two blocking each other.
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")

        self._connection = connection
        self._create_schema(connection)
        return connection

    def _create_schema(self, connection: sqlite3.Connection) -> None:
        """Create tables and the search index if they are not there."""
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id              TEXT PRIMARY KEY,
                label           TEXT NOT NULL DEFAULT '',
                hostname        TEXT NOT NULL DEFAULT '',
                connection_type TEXT NOT NULL DEFAULT 'ssh',
                username        TEXT NOT NULL DEFAULT '',
                target          TEXT NOT NULL DEFAULT '',
                started_at      REAL NOT NULL,
                ended_at        REAL,
                -- What the person was doing, in their own words (#530).
                -- On the session rather than in a table of its own: a note
                -- is about one session and there is one of them, and a
                -- separate table would make "show me this session" two
                -- queries and let the two drift apart.
                notes           TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS commands (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id  TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                sequence    INTEGER NOT NULL,
                command     TEXT NOT NULL,
                output      TEXT NOT NULL DEFAULT '',
                prompt      TEXT NOT NULL DEFAULT '',
                ran_at      REAL NOT NULL,
                duration_ms INTEGER NOT NULL DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS idx_commands_session ON commands(session_id, sequence);
            CREATE INDEX IF NOT EXISTS idx_commands_ran_at  ON commands(ran_at DESC);
            CREATE INDEX IF NOT EXISTS idx_sessions_started ON sessions(started_at DESC);
            CREATE INDEX IF NOT EXISTS idx_sessions_host    ON sessions(hostname);

            CREATE TABLE IF NOT EXISTS config_snapshots (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                hostname    TEXT NOT NULL,
                session_id  TEXT,
                captured_at REAL NOT NULL,
                content     TEXT NOT NULL,
                sha256      TEXT NOT NULL,
                line_count  INTEGER NOT NULL DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS idx_snapshots_host
                ON config_snapshots(hostname, captured_at DESC);

            -- A configuration somebody chose as the thing to measure against.
            --
            -- "Since your last visit" is an accident of when you happened to
            -- log in, and simply *looking* at a device consumes that baseline:
            -- connect, see four lines changed, reconnect an hour later to
            -- investigate, and you are told nothing has changed. One row per
            -- device, because the question is "against what" and there is one
            -- answer at a time.
            CREATE TABLE IF NOT EXISTS config_baselines (
                hostname    TEXT PRIMARY KEY,
                snapshot_id INTEGER NOT NULL,
                set_at      REAL NOT NULL,
                note        TEXT NOT NULL DEFAULT ''
            );

            -- What a device says is on the other end of each of its
            -- ports (#542). One row per edge, replaced when the same edge
            -- is seen again, so "what is on Gi1/0/24" has one answer
            -- rather than a history of the same answer.
            --
            -- Keyed on the local end alone: a port has one neighbour, and
            -- a key that included the remote name would keep the old one
            -- alongside the new after a device was swapped — which is
            -- precisely the moment somebody needs to be told what changed
            -- rather than shown both.
            CREATE TABLE IF NOT EXISTS neighbours (
                hostname     TEXT NOT NULL,
                local_port   TEXT NOT NULL,
                remote_host  TEXT NOT NULL DEFAULT '',
                remote_port  TEXT NOT NULL DEFAULT '',
                remote_addr  TEXT NOT NULL DEFAULT '',
                platform     TEXT NOT NULL DEFAULT '',
                protocol     TEXT NOT NULL DEFAULT '',
                seen_at      REAL NOT NULL,
                PRIMARY KEY (hostname, local_port)
            );

            CREATE INDEX IF NOT EXISTS idx_neighbours_remote
                ON neighbours(remote_host);

            -- The reasoning trail (#558).
            --
            -- The application survives a window close by design; the
            -- chat did not, because it was an array in the browser.
            -- Reloading the page threw away how a conclusion was
            -- reached, at exactly the moment somebody has to write it
            -- up.
            --
            -- Not keyed to a session: a conversation frequently spans
            -- several devices, and one that switched tabs half way
            -- through belongs to neither. `session_id` records which
            -- tab a message was asked about, which is a different
            -- question from which conversation it belongs to.
            CREATE TABLE IF NOT EXISTS chat_messages (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT NOT NULL,
                role            TEXT NOT NULL DEFAULT 'ai',
                text            TEXT NOT NULL DEFAULT '',
                session_id      TEXT NOT NULL DEFAULT '',
                said_at         REAL NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_chat_conversation
                ON chat_messages(conversation_id, id);
            CREATE INDEX IF NOT EXISTS idx_chat_said_at
                ON chat_messages(said_at DESC);

            CREATE TABLE IF NOT EXISTS meta (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )

        self._add_missing_columns(connection)

        # FTS5 is compiled into most SQLite builds but not guaranteed. Probe
        # rather than assume, and fall back to LIKE if it is missing.
        try:
            connection.executescript(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS commands_fts USING fts5(
                    command, output,
                    content='commands', content_rowid='id',
                    tokenize='porter unicode61'
                );

                CREATE TRIGGER IF NOT EXISTS commands_ai AFTER INSERT ON commands BEGIN
                    INSERT INTO commands_fts(rowid, command, output)
                    VALUES (new.id, new.command, new.output);
                END;

                CREATE TRIGGER IF NOT EXISTS commands_ad AFTER DELETE ON commands BEGIN
                    INSERT INTO commands_fts(commands_fts, rowid, command, output)
                    VALUES ('delete', old.id, old.command, old.output);
                END;

                -- Notes, indexed the same way (#530). A note is written to
                -- be found later — "customer confirmed", "rolled back at
                -- 16:40" — and a running commentary nobody can search is
                -- the Notepad file this replaces.
                --
                -- Unlike a command, a note is edited rather than inserted
                -- once, so it needs the update trigger as well: without
                -- it the index would go on matching text the session no
                -- longer says, which is worse than not indexing at all.
                CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
                    notes,
                    content='sessions', content_rowid='rowid',
                    tokenize='porter unicode61'
                );

                CREATE TRIGGER IF NOT EXISTS sessions_notes_ai
                AFTER INSERT ON sessions BEGIN
                    INSERT INTO notes_fts(rowid, notes) VALUES (new.rowid, new.notes);
                END;

                CREATE TRIGGER IF NOT EXISTS sessions_notes_ad
                AFTER DELETE ON sessions BEGIN
                    INSERT INTO notes_fts(notes_fts, rowid, notes)
                    VALUES ('delete', old.rowid, old.notes);
                END;

                CREATE TRIGGER IF NOT EXISTS sessions_notes_au
                AFTER UPDATE OF notes ON sessions BEGIN
                    INSERT INTO notes_fts(notes_fts, rowid, notes)
                    VALUES ('delete', old.rowid, old.notes);
                    INSERT INTO notes_fts(rowid, notes) VALUES (new.rowid, new.notes);
                END;

                -- Conversations (#558). Insert and delete only: a
                -- message is said once and never edited, unlike a
                -- session note, so there is no update trigger to get
                -- right and none to get wrong.
                CREATE VIRTUAL TABLE IF NOT EXISTS chat_fts USING fts5(
                    text,
                    content='chat_messages', content_rowid='id',
                    tokenize='porter unicode61'
                );

                CREATE TRIGGER IF NOT EXISTS chat_ai
                AFTER INSERT ON chat_messages BEGIN
                    INSERT INTO chat_fts(rowid, text) VALUES (new.id, new.text);
                END;

                CREATE TRIGGER IF NOT EXISTS chat_ad
                AFTER DELETE ON chat_messages BEGIN
                    INSERT INTO chat_fts(chat_fts, rowid, text)
                    VALUES ('delete', old.id, old.text);
                END;
                """
            )
            self._fts_enabled = True
        except sqlite3.OperationalError as exc:
            logger.warning("Full-text search unavailable, falling back to LIKE: %s", exc)
            self._fts_enabled = False

        connection.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        connection.commit()

    def _add_missing_columns(self, connection: sqlite3.Connection) -> None:
        """
        Add columns a newer schema wants to a table that already exists.

        `CREATE TABLE IF NOT EXISTS` does nothing at all to a table that is
        already there, so a column added to the definition above appears
        only for people installing for the first time — and everybody else
        gets an OperationalError on the first query that names it, at the
        point of use, with their history apparently broken.

        Additive and idempotent, checked against the table rather than
        against a version number: a database restored from a backup, or
        one whose meta row was lost, still ends up with the columns the
        code expects.
        """
        wanted = {
            "sessions": {"notes": "TEXT NOT NULL DEFAULT ''"},
        }
        for table, columns in wanted.items():
            try:
                have = {row["name"] for row in
                        connection.execute(f"PRAGMA table_info({table})")}
            except sqlite3.Error:                         # pragma: no cover
                continue
            if not have:
                continue
            for name, definition in columns.items():
                if name in have:
                    continue
                try:
                    connection.execute(
                        f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
                    logger.info("History schema: added %s.%s", table, name)
                except sqlite3.Error as exc:              # pragma: no cover
                    logger.warning("Could not add %s.%s: %s", table, name, exc)

    def close(self) -> None:
        """Close the database."""
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def start_session(self, session_id: str, metadata: dict) -> None:
        """Record the start of a session, unless recording is switched off."""
        if not _recording_enabled():
            return

        # Pruning is the scheduler's job now (#464): once a day, off the
        # connect path, so the first sweep of a large database after a
        # retention is set never lands on someone opening a session.
        with self._lock:
            connection = self.connect()
            connection.execute(
                """
                INSERT OR REPLACE INTO sessions
                    (id, label, hostname, connection_type, username, target, started_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    metadata.get("display_label", ""),
                    metadata.get("hostname", ""),
                    metadata.get("connection_type", "ssh"),
                    metadata.get("username", ""),
                    metadata.get("target", ""),
                    time.time(),
                ),
            )
            connection.commit()

    def end_session(self, session_id: str) -> None:
        """
        Mark a session finished — or forget it, if it recorded nothing.

        A session that never completed a command has nothing to search,
        replay or compare; keeping its row meant the table grew by one per
        connection forever (740 rows for 44 commands on one machine, #464)
        and every hostname list and session list scanned them all.
        """
        with self._lock:
            connection = self.connect()
            count = connection.execute(
                "SELECT COUNT(*) FROM commands WHERE session_id = ?", (session_id,)).fetchone()[0]
            if count:
                connection.execute(
                    "UPDATE sessions SET ended_at = ? WHERE id = ?", (time.time(), session_id),
                )
            else:
                connection.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            self._sequences.pop(session_id, None)
            connection.commit()

    def close_abandoned(self) -> int:
        """
        At startup: sessions the last process left open. Ones with commands
        get an end time so they read as finished; empty ones go. Returns
        how many rows were touched.
        """
        with self._lock:
            connection = self.connect()
            gone = connection.execute(
                """
                DELETE FROM sessions WHERE ended_at IS NULL
                  AND id NOT IN (SELECT DISTINCT session_id FROM commands)
                """).rowcount
            closed = connection.execute(
                "UPDATE sessions SET ended_at = started_at WHERE ended_at IS NULL").rowcount
            connection.commit()
        if gone or closed:
            logger.info("History: removed %d empty sessions and closed %d left open", gone, closed)
        return gone + closed

    def update_session_hostname(self, session_id: str, hostname: str) -> None:
        """
        Fill in the hostname once it has been parsed from the device prompt.

        Connections are often opened by IP, so the real hostname is not known
        until the device says it. Recording it is what makes searching by
        device name work at all.
        """
        with self._lock:
            connection = self.connect()
            connection.execute(
                "UPDATE sessions SET hostname = ? WHERE id = ? AND (hostname = '' OR hostname LIKE '%.%')",
                (hostname, session_id),
            )
            connection.commit()

    def prune(self) -> int:
        """
        Delete history older than the retention setting. Returns rows removed.

        `history.retention_days` was declared, given a reader, and never
        called — so a row labelled "Discard history after" accepted a number,
        wrote it to settings.json and discarded nothing. Somebody running from
        a stick, which is the stated reason for the setting, would believe the
        disk problem was handled.

        Zero means keep forever and is the default, so this is a no-op until
        somebody asks for it.

        Sessions are removed only once they have no commands left, rather than
        by their own start time: a session opened last month and still running
        would otherwise take its recent commands with it.
        """
        days = _retention_days()
        if days <= 0:
            return 0

        cutoff = time.time() - (days * 86400)

        # A pinned baseline is exempt when the setting says so. Pruning is
        # oldest-first, so without this the baseline is the *first* thing
        # discarded — which is precisely backwards, since being old is what
        # makes it a baseline.
        protected: set[int] = set()
        try:
            from backend.advanced import get as _advanced
            if _advanced("capture.keep_baseline"):
                protected = self.baseline_snapshot_ids()
        except Exception:
            protected = self.baseline_snapshot_ids()

        try:
            with self._lock:
                connection = self.connect()
                cursor = connection.execute(
                    "DELETE FROM commands WHERE ran_at < ?", (cutoff,))
                removed = cursor.rowcount or 0
                connection.execute(
                    """
                    DELETE FROM sessions
                     WHERE id NOT IN (SELECT DISTINCT session_id FROM commands)
                       AND started_at < ?
                    """, (cutoff,))

                # Configuration snapshots were never pruned by anything, so
                # they grew without limit — and they are the largest rows in
                # the database by a wide margin.
                keep = ",".join(str(int(i)) for i in protected) or "-1"
                snapshots = connection.execute(
                    f"""
                    DELETE FROM config_snapshots
                     WHERE captured_at < ?
                       AND id NOT IN ({keep})
                    """, (cutoff,))
                removed += snapshots.rowcount or 0
                connection.commit()
        except sqlite3.Error as exc:
            # Never worth breaking a session over.
            logger.warning("Could not prune history: %s", exc)
            return 0

        if removed:
            logger.info("Pruned %d command(s) older than %d days", removed, days)
        return removed

    # ------------------------------------------------------------------
    # Baselines
    # ------------------------------------------------------------------

    def set_baseline(self, hostname: str, snapshot_id: int, note: str = "") -> dict:
        """Pin a snapshot as what this device should be measured against."""
        with self._lock:
            connection = self.connect()
            connection.execute(
                """
                INSERT INTO config_baselines (hostname, snapshot_id, set_at, note)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(hostname) DO UPDATE SET
                    snapshot_id = excluded.snapshot_id,
                    set_at      = excluded.set_at,
                    note        = excluded.note
                """,
                (hostname, int(snapshot_id), time.time(), note or ""),
            )
            connection.commit()
        logger.info("Baseline for %s pinned to snapshot %s", hostname, snapshot_id)
        return self.get_baseline(hostname) or {}

    def get_baseline(self, hostname: str) -> dict | None:
        """The pinned snapshot for a device, with its content, or None."""
        with self._lock:
            connection = self.connect()
            row = connection.execute(
                """
                SELECT s.*, b.set_at AS baseline_set_at, b.note AS baseline_note
                  FROM config_baselines b
                  JOIN config_snapshots s ON s.id = b.snapshot_id
                 WHERE b.hostname = ?
                """,
                (hostname,),
            ).fetchone()
        return dict(row) if row else None

    def clear_baseline(self, hostname: str) -> bool:
        """Unpin. Returns whether there was one."""
        with self._lock:
            connection = self.connect()
            cursor = connection.execute(
                "DELETE FROM config_baselines WHERE hostname = ?", (hostname,))
            connection.commit()
        return bool(cursor.rowcount)

    def baseline_snapshot_ids(self) -> set[int]:
        """
        Every pinned snapshot id.

        Used by the prune paths. `prune()` deletes oldest-first, so without
        this a pinned baseline is the *first* thing discarded — which is
        precisely backwards, since being old is what makes it a baseline.
        """
        with self._lock:
            connection = self.connect()
            rows = connection.execute(
                "SELECT snapshot_id FROM config_baselines").fetchall()
        return {int(r[0]) for r in rows}

    # ------------------------------------------------------------------
    # The writer thread
    # ------------------------------------------------------------------

    def submit(self, fn, *args) -> None:
        """
        Run one store write on the writer thread, in order, off the loop.

        Fire and forget: the caller is a terminal read loop that must not
        wait on disk. Failures are logged inside the write itself.
        """
        self._ensure_writer()
        self._queue.put((fn, args))

    def flush(self, timeout: float = 2.0) -> bool:
        """Wait for queued writes to land. True if they all did in time."""
        deadline = time.monotonic() + timeout
        while not self._queue.empty() or self._queue.unfinished_tasks:
            if time.monotonic() > deadline:
                return False
            time.sleep(0.01)
        return True

    def _ensure_writer(self) -> None:
        with self._writer_lock:
            if self._writer is not None and self._writer.is_alive():
                return
            self._writer = threading.Thread(target=self._drain, daemon=True, name="history-writer")
            self._writer.start()

    def _drain(self) -> None:
        while True:
            fn, args = self._queue.get()
            try:
                fn(*args)
            except Exception as exc:                   # never let the thread die
                logger.warning("History write failed: %s", exc)
            finally:
                self._queue.task_done()

    def add_command(self, session_id: str, record: Any) -> int:
        """
        Store one command and its output.

        Returns:
            The new row id, or -1 if nothing was written. History is valuable
            but never worth breaking a live session over, so failures are
            logged and swallowed.
        """
        # Checked here as well as in start_session(). Guarding only the parent
        # row meant switching recording off skipped the `sessions` entry and
        # went on writing every command and every byte of output — which is
        # the opposite of what the setting says, and the people who turn it
        # off are the ones who most need it to be true.
        if not _recording_enabled():
            return -1

        # Which commands ran is the audit trail people need for a change
        # record; the output is the part carrying configurations and secrets.
        # Recording one without the other is the middle ground between
        # history.record on and off.
        from backend.advanced import get as _advanced

        try:
            keep_output = bool(_advanced("history.record_output"))
        except Exception:
            keep_output = True

        output = _redacted(record.output or "") if keep_output else ""
        cap = _max_output_chars()
        if len(output) > cap:
            kept = cap
            output = (
                output[:kept]
                + f"\n\n[... truncated, {len(record.output) - kept:,} more characters]"
            )

        try:
            with self._lock:
                connection = self.connect()
                sequence = self._sequences.get(session_id)
                if sequence is None:
                    cursor = connection.execute(
                        "SELECT COALESCE(MAX(sequence), 0) + 1 FROM commands WHERE session_id = ?",
                        (session_id,),
                    )
                    sequence = cursor.fetchone()[0]
                self._sequences[session_id] = sequence + 1

                cursor = connection.execute(
                    """
                    INSERT INTO commands
                        (session_id, sequence, command, output, prompt, ran_at, duration_ms)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id, sequence, record.command, output,
                        record.prompt, record.started_at or time.time(),
                        record.duration_ms,
                    ),
                )
                connection.commit()
                return int(cursor.lastrowid or -1)
        except sqlite3.Error as exc:
            logger.warning("Could not record command: %s", exc)
            return -1

    # ------------------------------------------------------------------
    # Querying
    # ------------------------------------------------------------------

    def search(
        self, query: str = "", hostname: str = "", since: float | None = None,
        until: float | None = None, limit: int = 100, kind: str = "",
    ) -> list[dict]:
        """
        Search command history.

        Args:
            query:    Free text matched against commands and their output.
            hostname: Restrict to one device.
            since:    Unix timestamp lower bound.
            until:    Unix timestamp upper bound.
            limit:    Maximum hits.

        Returns:
            Matching commands, newest first, each with a snippet of context.
        """
        # Reads share the one connection with the writer, so they take the
        # same lock. check_same_thread=False disables Python's guard; it does
        # not make the connection safe. These run on asyncio worker threads
        # while the terminal loop is committing from its own.
        with self._lock:
            connection = self.connect()
            clauses: list[str] = []
            params: list[Any] = []

            if query and self._fts_enabled:
                base = """
                    SELECT c.id, c.session_id, c.command, c.ran_at,
                           s.hostname, s.label,
                           snippet(commands_fts, 1, '', '', ' … ', 12) AS snippet
                    FROM commands_fts
                    JOIN commands c ON c.id = commands_fts.rowid
                    JOIN sessions s ON s.id = c.session_id
                    WHERE commands_fts MATCH ?
                """
                params.append(_to_fts_query(query))
            elif query:
                base = """
                    SELECT c.id, c.session_id, c.command, c.ran_at,
                           s.hostname, s.label,
                           substr(c.output, 1, 200) AS snippet
                    FROM commands c
                    JOIN sessions s ON s.id = c.session_id
                    WHERE (c.command LIKE ? OR c.output LIKE ?)
                """
                params.extend([f"%{query}%", f"%{query}%"])
            else:
                base = """
                    SELECT c.id, c.session_id, c.command, c.ran_at,
                           s.hostname, s.label,
                           substr(c.output, 1, 200) AS snippet
                    FROM commands c
                    JOIN sessions s ON s.id = c.session_id
                    WHERE 1=1
                """

            if hostname:
                clauses.append("s.hostname = ?")
                params.append(hostname)
            # "collection" for the scheduled show commands (#547), "live"
            # for everything a person typed. Not a connection-type filter in
            # general: the question History is asked is "did I type this or
            # did the scheduler", and ssh-vs-telnet is not that question.
            if kind == "collection":
                clauses.append("s.connection_type = 'collection'")
            elif kind == "live":
                clauses.append("s.connection_type != 'collection'")
            if since is not None:
                clauses.append("c.ran_at >= ?")
                params.append(since)
            if until is not None:
                clauses.append("c.ran_at <= ?")
                params.append(until)

            sql = base + "".join(f" AND {clause}" for clause in clauses)
            sql += " ORDER BY c.ran_at DESC LIMIT ?"
            params.append(min(max(limit, 1), 500))

            try:
                rows = connection.execute(sql, params).fetchall()
            except sqlite3.Error as exc:
                logger.warning("Search failed: %s", exc)
                return []

            # Nothing from the index is not the same as nothing to find
            # (#271). FTS matches whole tokens, so a search for a phrase that
            # sits inside one — "show ip arp" against a command recorded as
            # "arpshow ip arp" — misses entirely, as does anything the
            # tokeniser split differently. A substring pass costs one more
            # query on the miss path only, and it is the answer people
            # expect from a search box.
            #
            # Over the command column only (#459). Scanning `output` as well
            # meant every search-as-you-type miss walked the whole table's
            # output — 700 ms measured over 20,000 × 30 KB — while holding
            # the lock the terminals' own writes wait on. FTS already
            # covers output for the tokenised case; the substring pass is
            # for the mistyped or unbroken command.
            if query and self._fts_enabled and not rows:
                like_sql = """
                    SELECT c.id, c.session_id, c.command, c.ran_at,
                           s.hostname, s.label,
                           substr(c.output, 1, 200) AS snippet
                    FROM commands c
                    JOIN sessions s ON s.id = c.session_id
                    WHERE c.command LIKE ?
                """
                like_params: list[Any] = [f"%{query}%"]
                if hostname:
                    like_sql += " AND s.hostname = ?"
                    like_params.append(hostname)
                if since is not None:
                    like_sql += " AND c.ran_at >= ?"
                    like_params.append(since)
                if until is not None:
                    like_sql += " AND c.ran_at <= ?"
                    like_params.append(until)
                like_sql += " ORDER BY c.ran_at DESC LIMIT ?"
                like_params.append(min(max(limit, 1), 500))
                try:
                    rows = connection.execute(like_sql, like_params).fetchall()
                except sqlite3.Error as exc:
                    logger.warning("Substring search failed: %s", exc)

            return [
                SearchHit(
                    command_id=row["id"], session_id=row["session_id"],
                    hostname=row["hostname"] or "", label=row["label"] or "",
                    command=row["command"], snippet=(row["snippet"] or "").strip(),
                    ran_at=row["ran_at"],
                ).as_dict()
                for row in rows
            ]

    def list_sessions(self, limit: int = 50, hostname: str = "",
                      kind: str = "") -> list[dict]:
        """
        Return recent sessions, newest first, with their command counts.

        ``kind`` is "collection" for the scheduled show commands (#547),
        "live" for everything a person typed, or "" for both.
        """
        # Reads share the one connection with the writer, so they take the
        # same lock. check_same_thread=False disables Python's guard; it does
        # not make the connection safe. These run on asyncio worker threads
        # while the terminal loop is committing from its own.
        with self._lock:
            connection = self.connect()
            sql = """
                SELECT s.*, COUNT(c.id) AS command_count
                FROM sessions s
                LEFT JOIN commands c ON c.session_id = s.id
                WHERE 1=1
            """
            params: list[Any] = []
            if hostname:
                sql += " AND s.hostname = ?"
                params.append(hostname)
            if kind == "collection":
                sql += " AND s.connection_type = 'collection'"
            elif kind == "live":
                sql += " AND s.connection_type != 'collection'"
            sql += " GROUP BY s.id ORDER BY s.started_at DESC LIMIT ?"
            params.append(min(max(limit, 1), 500))

            return [dict(row) for row in connection.execute(sql, params).fetchall()]

    def get_session(self, session_id: str) -> dict | None:
        """Return one session with every command it ran, in order."""
        # Reads share the one connection with the writer, so they take the
        # same lock. check_same_thread=False disables Python's guard; it does
        # not make the connection safe. These run on asyncio worker threads
        # while the terminal loop is committing from its own.
        with self._lock:
            connection = self.connect()
            row = connection.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
            if row is None:
                return None

            commands = connection.execute(
                "SELECT * FROM commands WHERE session_id = ? ORDER BY sequence",
                (session_id,),
            ).fetchall()

            session = dict(row)
            session["commands"] = [dict(c) for c in commands]
            return session

    # -----------------------------------------------------------------
    # Neighbours (#542)
    #
    # The edges CDP and LLDP report, kept so that "what is on the other
    # end of Gi1/0/24" is answerable from the history rather than only in
    # the moment somebody looked.
    # -----------------------------------------------------------------

    # -----------------------------------------------------------------
    # Conversations (#558)
    #
    # The application survives a window close by design — sessions live in
    # the server process, and closing the window hides it. The *reasoning*
    # did not: the chat was an array in the browser, and reloading the page
    # threw away the trail of how a conclusion was reached, at exactly the
    # moment somebody has to write it up.
    #
    # Stored through the same redaction as everything else that leaves a
    # session. This one is easy to argue out of — the text is already in
    # the browser and the model has already seen it — and the argument is
    # wrong: a stored conversation is searched, exported, sent to Jira and
    # pasted into tickets, which is further than a session log ever goes.
    # -----------------------------------------------------------------

    def add_chat_message(self, conversation_id: str, role: str, text: str,
                         session_id: str = "") -> int:
        """
        Store one chat message. Returns the row id, or -1.

        Written synchronously rather than through the writer thread, for
        the same reason `set_notes` is: a message is one small row, and
        "saved" has to mean saved when the next thing that happens might
        be the window closing.
        """
        body = _redacted(text or "")
        if not (conversation_id or "").strip() or not body.strip():
            return -1

        try:
            with self._lock:
                connection = self.connect()
                cursor = connection.execute(
                    """
                    INSERT INTO chat_messages
                        (conversation_id, role, text, session_id, said_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (conversation_id, role if role in ("user", "ai") else "ai",
                     body, session_id, time.time()),
                )
                connection.commit()
                return int(cursor.lastrowid or -1)
        except sqlite3.Error as exc:
            # Never worth interrupting a conversation over.
            logger.warning("Could not store a chat message: %s", exc)
            return -1

    def list_conversations(self, limit: int = 50) -> list[dict]:
        """
        Conversations, newest first, with enough to recognise one.

        The first thing the engineer said is the title. A generated summary
        would be better prose and a worse label: what somebody recognises a
        fortnight later is the words they typed.
        """
        with self._lock:
            rows = self.connect().execute(
                """
                SELECT conversation_id,
                       COUNT(*)                       AS messages,
                       MIN(said_at)                   AS started_at,
                       MAX(said_at)                   AS last_at,
                       MIN(CASE WHEN role = 'user' THEN id END) AS first_user
                FROM chat_messages
                GROUP BY conversation_id
                ORDER BY last_at DESC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()

            out = []
            for row in rows:
                entry = dict(row)
                title = ""
                if entry.get("first_user"):
                    first = self.connect().execute(
                        "SELECT text, session_id FROM chat_messages WHERE id = ?",
                        (entry["first_user"],)).fetchone()
                    if first:
                        title = (first["text"] or "").strip().split("\n")[0][:120]
                        entry["session_id"] = first["session_id"]
                entry["title"] = title or "(no question)"
                entry.pop("first_user", None)
                out.append(entry)
            return out

    def get_conversation(self, conversation_id: str) -> list[dict]:
        """Every message in one conversation, oldest first."""
        with self._lock:
            rows = self.connect().execute(
                """
                SELECT id, role, text, session_id, said_at
                FROM chat_messages
                WHERE conversation_id = ?
                ORDER BY id
                """,
                (conversation_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def search_chat(self, query: str = "", limit: int = 50) -> list[dict]:
        """
        Find a conversation by something said in it.

        Same two-tier shape as the command search: FTS where it is
        available, LIKE where it is not — and a LIKE pass when FTS matched
        nothing, because an index can go stale and "no results" is the one
        answer nobody can tell apart from a broken index.
        """
        text = (query or "").strip()
        if not text:
            return []

        with self._lock:
            connection = self.connect()
            rows = []
            if self._fts_enabled:
                try:
                    rows = connection.execute(
                        """
                        SELECT m.id, m.conversation_id, m.role, m.session_id,
                               m.said_at,
                               snippet(chat_fts, 0, '', '', ' … ', 12) AS snippet
                        FROM chat_fts
                        JOIN chat_messages m ON m.id = chat_fts.rowid
                        WHERE chat_fts MATCH ?
                        ORDER BY m.said_at DESC
                        LIMIT ?
                        """,
                        (_to_fts_query(text), int(limit)),
                    ).fetchall()
                except sqlite3.Error as exc:
                    logger.warning("Chat search failed: %s", exc)
                    rows = []

            if not rows:
                rows = connection.execute(
                    """
                    SELECT id, conversation_id, role, session_id, said_at,
                           substr(text, 1, 200) AS snippet
                    FROM chat_messages
                    WHERE text LIKE ?
                    ORDER BY said_at DESC
                    LIMIT ?
                    """,
                    (f"%{text}%", int(limit)),
                ).fetchall()

            return [dict(r) for r in rows]

    def delete_conversation(self, conversation_id: str) -> int:
        """Remove one conversation. Returns how many messages went."""
        with self._lock:
            connection = self.connect()
            cursor = connection.execute(
                "DELETE FROM chat_messages WHERE conversation_id = ?",
                (conversation_id,))
            connection.commit()
            return int(cursor.rowcount or 0)

    def prune_conversations(self, before: float) -> int:
        """
        Drop conversations whose last message predates ``before``.

        Chat is pruned on the same retention setting as the commands it is
        about — a conversation kept after the session it discusses has been
        swept is a reasoning trail pointing at nothing.
        """
        with self._lock:
            connection = self.connect()
            cursor = connection.execute(
                """
                DELETE FROM chat_messages
                WHERE conversation_id IN (
                    SELECT conversation_id FROM chat_messages
                    GROUP BY conversation_id HAVING MAX(said_at) < ?
                )
                """,
                (float(before),))
            connection.commit()
            return int(cursor.rowcount or 0)

    def record_neighbours(self, hostname: str, neighbours: list[dict],
                          seen_at: float | None = None) -> int:
        """
        Store what one device reports about its ports. Returns edges written.

        Replaces rather than accumulates, per local port. A switch swapped
        for another answers on the same port with a different name, and a
        table that kept both would be a table nobody can read a topology
        out of.

        Neighbours with no local port are skipped: an edge with no near end
        is not an edge, and giving it an empty port would make every such
        neighbour collide on one row.
        """
        host = (hostname or "").strip()
        if not host:
            return 0
        when = float(seen_at if seen_at is not None else time.time())
        rows = []
        for entry in neighbours or []:
            local = str(entry.get("local_port") or "").strip()
            if not local:
                continue
            rows.append((host, local,
                         str(entry.get("name") or ""),
                         str(entry.get("remote_port") or ""),
                         str(entry.get("address") or ""),
                         str(entry.get("platform") or ""),
                         ",".join(entry.get("protocols")
                                  or ([entry["protocol"]] if entry.get("protocol") else [])),
                         when))
        if not rows:
            return 0

        with self._lock:
            connection = self.connect()
            try:
                connection.executemany(
                    """
                    INSERT INTO neighbours
                        (hostname, local_port, remote_host, remote_port,
                         remote_addr, platform, protocol, seen_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(hostname, local_port) DO UPDATE SET
                        remote_host = excluded.remote_host,
                        remote_port = excluded.remote_port,
                        remote_addr = excluded.remote_addr,
                        platform    = excluded.platform,
                        protocol    = excluded.protocol,
                        seen_at     = excluded.seen_at
                    """, rows)
                connection.commit()
            except sqlite3.Error as exc:
                logger.warning("Could not store neighbours of %s: %s", host, exc)
                return 0
        return len(rows)

    def neighbours_of(self, hostname: str) -> list[dict]:
        """Every edge recorded for a device, by port."""
        with self._lock:
            connection = self.connect()
            try:
                rows = connection.execute(
                    "SELECT * FROM neighbours WHERE hostname = ? "
                    "ORDER BY local_port", ((hostname or "").strip(),)).fetchall()
            except sqlite3.Error:                         # pragma: no cover
                return []
            return [dict(row) for row in rows]

    def neighbours_naming(self, hostname: str) -> list[dict]:
        """
        Every device that reports *this* one as its neighbour.

        The other direction, and worth having on its own: a device you
        cannot reach is often still visible because three of its
        neighbours can see it.
        """
        with self._lock:
            connection = self.connect()
            try:
                rows = connection.execute(
                    "SELECT * FROM neighbours WHERE remote_host = ? "
                    "ORDER BY hostname, local_port",
                    ((hostname or "").strip(),)).fetchall()
            except sqlite3.Error:                         # pragma: no cover
                return []
            return [dict(row) for row in rows]

    # -----------------------------------------------------------------
    # Notes (#530)
    #
    # A change window produces a running commentary — "16:02 shut
    # Gi1/0/24, 16:05 confirmed by site" — and it lives in Notepad, where
    # it never meets the transcript it describes. Kept here it is beside
    # the commands it is about, survives a restart, and can be found by
    # searching for what somebody wrote rather than what a device printed.
    # -----------------------------------------------------------------

    #: How much of one is kept. Generous — a change window's commentary is
    #: not short — but bounded, because this is a text box in a browser
    #: and a paste is a paste.
    MAX_NOTES = 64_000

    def set_notes(self, session_id: str, notes: str) -> bool:
        """
        Replace a session's notes. Returns False if there is no such session.

        Written synchronously rather than through the background writer.
        The writer exists so a slow disk cannot stall a terminal read loop;
        a note is a deliberate act whose result somebody is looking at, and
        "saved" has to mean saved.
        """
        text = (notes or "")[: self.MAX_NOTES]
        with self._lock:
            connection = self.connect()
            try:
                cursor = connection.execute(
                    "UPDATE sessions SET notes = ? WHERE id = ?",
                    (text, session_id))
                connection.commit()
            except sqlite3.Error as exc:
                logger.warning("Could not save notes for %s: %s", session_id, exc)
                return False
            return cursor.rowcount > 0

    def get_notes(self, session_id: str) -> str:
        """A session's notes, or "" for one that has none or is not there."""
        with self._lock:
            connection = self.connect()
            try:
                row = connection.execute(
                    "SELECT notes FROM sessions WHERE id = ?",
                    (session_id,)).fetchone()
            except sqlite3.Error:                         # pragma: no cover
                return ""
            return (row["notes"] if row else "") or ""

    def search_notes(self, query: str = "", hostname: str = "",
                     since: float | None = None, until: float | None = None,
                     limit: int = 50) -> list[dict]:
        """
        Sessions whose notes match, newest first.

        Returned separately from the command search rather than merged into
        it. A note is about a session, not about a command, and giving it a
        fabricated command row to travel in would put words in the
        transcript that nobody typed at a device — which is the one thing
        a record of a change window must never do.
        """
        with self._lock:
            connection = self.connect()
            params: list[Any] = []

            if query and self._fts_enabled:
                base = """
                    SELECT s.id, s.hostname, s.label, s.started_at, s.ended_at,
                           snippet(notes_fts, 0, '', '', ' … ', 16) AS snippet
                    FROM notes_fts
                    JOIN sessions s ON s.rowid = notes_fts.rowid
                    WHERE notes_fts MATCH ?
                """
                params.append(_to_fts_query(query))
            elif query:
                base = """
                    SELECT s.id, s.hostname, s.label, s.started_at, s.ended_at,
                           substr(s.notes, 1, 200) AS snippet
                    FROM sessions s
                    WHERE s.notes LIKE ?
                """
                params.append(f"%{query}%")
            else:
                base = """
                    SELECT s.id, s.hostname, s.label, s.started_at, s.ended_at,
                           substr(s.notes, 1, 200) AS snippet
                    FROM sessions s
                    WHERE s.notes <> ''
                """

            clauses: list[str] = []
            if hostname:
                clauses.append("s.hostname = ?")
                params.append(hostname)
            if since is not None:
                clauses.append("s.started_at >= ?")
                params.append(since)
            if until is not None:
                clauses.append("s.started_at <= ?")
                params.append(until)

            sql = base + "".join(f" AND {clause}" for clause in clauses)
            sql += " ORDER BY s.started_at DESC LIMIT ?"
            params.append(min(max(limit, 1), 200))

            try:
                rows = connection.execute(sql, params).fetchall()
            except sqlite3.Error as exc:
                logger.warning("Note search failed: %s", exc)
                return []

            # The FTS index can go stale where a note was written by a
            # version that predates the triggers, so a miss falls back to a
            # substring pass — the same arrangement the command search
            # already uses, and for the same reason: nothing from the index
            # is not the same as nothing to find.
            if not rows and query and self._fts_enabled:
                try:
                    rows = connection.execute(
                        """
                        SELECT s.id, s.hostname, s.label, s.started_at,
                               s.ended_at, substr(s.notes, 1, 200) AS snippet
                        FROM sessions s
                        WHERE s.notes LIKE ?
                        ORDER BY s.started_at DESC LIMIT ?
                        """,
                        (f"%{query}%", min(max(limit, 1), 200))).fetchall()
                except sqlite3.Error:                     # pragma: no cover
                    return []

            return [dict(row) for row in rows]

    def recent_commands(self, hostname: str = "", query: str = "",
                        limit: int = 50) -> list[dict]:
        """
        Distinct commands run on a device, newest first (#522).

        The recall list. Every serious terminal has per-host history and
        ShellMate had the data with no way to reach it: the show commands you
        ran on this switch last month are the ones you want again.

        Deduplicated, because the point is a list of *commands* rather than a
        list of occurrences — `show ip int brief` run forty times is one line
        with a count beside it, not forty identical rows.

        Args:
            hostname: Restrict to one device. Empty means every device, which
                      is what a session whose hostname is not yet known gets.
            query:    Substring of the command. Matched here rather than in
                      the browser so the limit applies to what matched.
            limit:    Most commands returned.

        Returns:
            ``{command, ran_at, times, hostname}`` — ran_at being the most
            recent run, which is what the ordering is on.
        """
        # Reads share the one connection with the writer, so they take the
        # same lock. check_same_thread=False disables Python's guard; it does
        # not make the connection safe. These run on asyncio worker threads
        # while the terminal loop is committing from its own.
        with self._lock:
            connection = self.connect()
            # Over `command` only — never `output`. Scanning output on a
            # type-ahead is what #459 measured at 700 ms while holding the
            # lock every live session's writes wait on.
            sql = """
                SELECT c.command                  AS command,
                       MAX(c.ran_at)              AS ran_at,
                       COUNT(*)                   AS times,
                       MAX(s.hostname)            AS hostname
                FROM commands c
                JOIN sessions s ON s.id = c.session_id
                WHERE c.command != ''
            """
            params: list[Any] = []
            if hostname:
                sql += " AND s.hostname = ?"
                params.append(hostname)
            if query:
                sql += " AND c.command LIKE ?"
                params.append(f"%{query}%")
            sql += " GROUP BY c.command ORDER BY ran_at DESC LIMIT ?"
            params.append(min(max(limit, 1), 500))

            try:
                rows = connection.execute(sql, params).fetchall()
            except sqlite3.Error as exc:
                # Recall is a convenience. A database that cannot answer must
                # not be an error anybody sees at a prompt.
                logger.warning("Command recall failed: %s", exc)
                return []

            return [
                {
                    "command":  row["command"],
                    "ran_at":   row["ran_at"],
                    "times":    row["times"],
                    "hostname": row["hostname"] or "",
                }
                for row in rows
            ]

    def known_hostnames(self) -> list[str]:
        """Every device seen, for the history filter."""
        # Reads share the one connection with the writer, so they take the
        # same lock. check_same_thread=False disables Python's guard; it does
        # not make the connection safe. These run on asyncio worker threads
        # while the terminal loop is committing from its own.
        with self._lock:
            connection = self.connect()
            rows = connection.execute(
                "SELECT DISTINCT hostname FROM sessions WHERE hostname != '' ORDER BY hostname"
            ).fetchall()
            return [row["hostname"] for row in rows]

    def clear_history(self, hostname: str = "", before: float | None = None,
                      include_snapshots: bool = True) -> dict:
        """
        Remove recorded history, optionally for one device or up to a date.

        Retention answers "discard anything older than N days" and recording
        answers "stop from now on". Neither answers "get rid of what you have
        about *that* device", which is the question that actually comes up: a
        customer engagement ends, a lab is torn down, or a session captured
        something that should not have been kept.

        Configuration snapshots go by default and are counted separately. They
        are the most sensitive thing in the database — a running config
        carries hashes, keys and community strings — so leaving them behind
        while claiming to have cleared a device's history would be misleading.

        A pinned baseline is **not** exempt here. It is exempt from *ageing
        out*, because being old is what makes it a baseline; it is not exempt
        from somebody explicitly asking for a device's history to go.

        Returns what was removed, so the interface can say rather than guess.
        """
        clauses, params = [], []
        if hostname:
            clauses.append("hostname = ?")
            params.append(hostname)

        with self._lock:
            connection = self.connect()

            # Commands are joined to sessions for the hostname, so the set of
            # sessions is worked out first.
            session_sql = "SELECT id FROM sessions"
            if clauses:
                session_sql += " WHERE " + " AND ".join(clauses)
            session_ids = [r[0] for r in connection.execute(session_sql, params).fetchall()]

            removed_commands = 0
            if session_ids:
                marks = ",".join("?" for _ in session_ids)
                self._sequences.clear()
                command_sql = f"DELETE FROM commands WHERE session_id IN ({marks})"
                command_params = list(session_ids)
                if before is not None:
                    command_sql += " AND ran_at < ?"
                    command_params.append(before)
                removed_commands = connection.execute(
                    command_sql, command_params).rowcount or 0

            # Sessions go only once nothing is left pointing at them, so
            # clearing "everything before Friday" does not take a session that
            # still holds later commands.
            #
            # The date bound is repeated here rather than relying on the
            # session being empty. A session that recorded nothing — connected,
            # nothing typed — is empty from the first second, and without this
            # a date-scoped clear would take one opened a minute ago.
            session_clauses = ["id NOT IN (SELECT DISTINCT session_id FROM commands)"]
            session_params: list = []
            if hostname:
                session_clauses.append("hostname = ?")
                session_params.append(hostname)
            if before is not None:
                session_clauses.append("started_at < ?")
                session_params.append(before)

            removed_sessions = connection.execute(
                "DELETE FROM sessions WHERE " + " AND ".join(session_clauses),
                session_params,
            ).rowcount or 0

            removed_snapshots = 0
            if include_snapshots:
                snapshot_sql = "DELETE FROM config_snapshots"
                snapshot_params: list = []
                conditions = []
                if hostname:
                    conditions.append("hostname = ?")
                    snapshot_params.append(hostname)
                if before is not None:
                    conditions.append("captured_at < ?")
                    snapshot_params.append(before)
                if conditions:
                    snapshot_sql += " WHERE " + " AND ".join(conditions)
                removed_snapshots = connection.execute(
                    snapshot_sql, snapshot_params).rowcount or 0

                # A baseline pointing at a snapshot that no longer exists
                # would leave drift comparing against nothing.
                connection.execute(
                    """
                    DELETE FROM config_baselines
                     WHERE snapshot_id NOT IN (SELECT id FROM config_snapshots)
                    """)

            connection.commit()

        logger.info("Cleared history%s: %d command(s), %d session(s), %d snapshot(s)",
                    f" for {hostname}" if hostname else "",
                    removed_commands, removed_sessions, removed_snapshots)
        return {"commands": removed_commands, "sessions": removed_sessions,
                "snapshots": removed_snapshots}

    def delete_session(self, session_id: str) -> bool:
        """Delete a session and its commands."""
        with self._lock:
            connection = self.connect()
            connection.execute("DELETE FROM commands WHERE session_id = ?", (session_id,))
            self._sequences.pop(session_id, None)
            cursor = connection.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            connection.commit()
            return cursor.rowcount > 0

    def stats(self) -> dict:
        """Summary counts for the history panel header."""
        # Reads share the one connection with the writer, so they take the
        # same lock. check_same_thread=False disables Python's guard; it does
        # not make the connection safe. These run on asyncio worker threads
        # while the terminal loop is committing from its own.
        with self._lock:
            connection = self.connect()
            row = connection.execute(
                """
                SELECT (SELECT COUNT(*) FROM sessions)         AS sessions,
                       (SELECT COUNT(*) FROM commands)         AS commands,
                       (SELECT COUNT(*) FROM config_snapshots) AS snapshots,
                       (SELECT COUNT(DISTINCT hostname) FROM sessions WHERE hostname != '') AS devices
                """
            ).fetchone()
            result = dict(row)
            result["search"] = "fts5" if self._fts_enabled else "like"
            return result

    # ------------------------------------------------------------------
    # Config snapshots
    # ------------------------------------------------------------------

    def add_snapshot(self, hostname: str, content: str, session_id: str = "") -> dict:
        """
        Store a configuration snapshot, skipping identical consecutive ones.

        Connecting to a device twice in an hour should not create two
        identical rows; the hash check keeps the history to actual changes.
        """
        import hashlib

        content = _redacted(content)
        digest = hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()

        # The whole check-and-insert under one lock. Read outside it, two
        # captures of the same unchanged configuration could both decide they
        # were the first and store a duplicate row — which is exactly what the
        # hash check exists to prevent.
        with self._lock:
            previous = self.latest_snapshot(hostname)
            if previous and previous["sha256"] == digest:
                return {"stored": False, "unchanged": True, "snapshot": previous}

            connection = self.connect()
            cursor = connection.execute(
                """
                INSERT INTO config_snapshots
                    (hostname, session_id, captured_at, content, sha256, line_count)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (hostname, session_id, time.time(), content, digest,
                 content.count("\n") + 1),
            )
            connection.commit()
            snapshot_id = int(cursor.lastrowid or -1)

        return {
            "stored": True, "unchanged": False,
            "snapshot": {"id": snapshot_id, "hostname": hostname, "sha256": digest},
            "previous": previous,
        }

    def latest_snapshot(self, hostname: str, before: float | None = None) -> dict | None:
        """Return the most recent snapshot for a device."""
        # Reads share the one connection with the writer, so they take the
        # same lock. check_same_thread=False disables Python's guard; it does
        # not make the connection safe. These run on asyncio worker threads
        # while the terminal loop is committing from its own.
        with self._lock:
            connection = self.connect()
            sql = "SELECT * FROM config_snapshots WHERE hostname = ?"
            params: list[Any] = [hostname]
            if before is not None:
                sql += " AND captured_at < ?"
                params.append(before)
            # Tie-break on id. Two snapshots captured in the same second share a
            # timestamp, and ordering by it alone leaves them in an arbitrary
            # order — which silently reverses a diff.
            sql += " ORDER BY captured_at DESC, id DESC LIMIT 1"

            row = connection.execute(sql, params).fetchone()
            return dict(row) if row else None

    def list_snapshots(self, hostname: str, limit: int = 50) -> list[dict]:
        """
        Return a device's snapshot history without the config bodies.

        Bodies are excluded deliberately — a list of twenty configurations is
        megabytes of JSON the UI has no use for until one is opened.
        """
        # Reads share the one connection with the writer, so they take the
        # same lock. check_same_thread=False disables Python's guard; it does
        # not make the connection safe. These run on asyncio worker threads
        # while the terminal loop is committing from its own.
        with self._lock:
            connection = self.connect()
            rows = connection.execute(
                """
                SELECT id, hostname, session_id, captured_at, sha256, line_count
                FROM config_snapshots WHERE hostname = ?
                ORDER BY captured_at DESC, id DESC LIMIT ?
                """,
                (hostname, min(max(limit, 1), 200)),
            ).fetchall()
            return [dict(row) for row in rows]

    def get_snapshot(self, snapshot_id: int) -> dict | None:
        """Return one snapshot including its content."""
        # Reads share the one connection with the writer, so they take the
        # same lock. check_same_thread=False disables Python's guard; it does
        # not make the connection safe. These run on asyncio worker threads
        # while the terminal loop is committing from its own.
        with self._lock:
            connection = self.connect()
            row = connection.execute(
                "SELECT * FROM config_snapshots WHERE id = ?", (snapshot_id,)
            ).fetchone()
            return dict(row) if row else None


def _to_fts_query(text: str) -> str:
    """
    Turn user input into a safe FTS5 query.

    FTS5 has its own syntax where bare punctuation is a syntax error, so a
    search for ``10.1.1.1`` or ``Gi0/1`` would otherwise raise rather than
    match. Each word is quoted and the terms ANDed, which is what someone
    typing several words expects.

    The last word is a prefix (#271). The panel searches as you type, so
    every word is a partial word until the instant it is finished: without
    this, typing "interface" reported "nothing matches" through i, in, int,
    inte... and only found anything on the final keystroke — which reads as
    a search that does not work. Only the last term, because the earlier
    words in a multi-word query have been finished by the space after them.
    """
    words = [word for word in text.replace('"', " ").split() if word]
    if not words:
        return '""'
    terms = [f'"{word}"' for word in words[:-1]]
    terms.append(f'"{words[-1]}"*')
    return " AND ".join(terms)


# Process-wide instance — one database file, one owner.
store = SessionStore()
