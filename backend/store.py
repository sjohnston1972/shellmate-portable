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
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Any

from backend import paths

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

# Cap on stored output per command. A "show tech-support" runs to megabytes,
# and storing it whole would bloat the database for no practical gain.
MAX_OUTPUT_CHARS = 256 * 1024


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
        self._lock = threading.Lock()
        self._fts_enabled = False

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
                ended_at        REAL
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

            CREATE TABLE IF NOT EXISTS meta (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )

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
        """Record the start of a session."""
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
        """Mark a session finished."""
        with self._lock:
            connection = self.connect()
            connection.execute(
                "UPDATE sessions SET ended_at = ? WHERE id = ?", (time.time(), session_id),
            )
            connection.commit()

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

    def add_command(self, session_id: str, record: Any) -> int:
        """
        Store one command and its output.

        Returns:
            The new row id, or -1 if the write failed. History is valuable but
            never worth breaking a live session over, so failures are logged
            and swallowed.
        """
        output = record.output or ""
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
                cursor = connection.execute(
                    "SELECT COALESCE(MAX(sequence), 0) + 1 FROM commands WHERE session_id = ?",
                    (session_id,),
                )
                sequence = cursor.fetchone()[0]

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
        until: float | None = None, limit: int = 100,
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

        return [
            SearchHit(
                command_id=row["id"], session_id=row["session_id"],
                hostname=row["hostname"] or "", label=row["label"] or "",
                command=row["command"], snippet=(row["snippet"] or "").strip(),
                ran_at=row["ran_at"],
            ).as_dict()
            for row in rows
        ]

    def list_sessions(self, limit: int = 50, hostname: str = "") -> list[dict]:
        """Return recent sessions, newest first, with their command counts."""
        connection = self.connect()
        sql = """
            SELECT s.*, COUNT(c.id) AS command_count
            FROM sessions s
            LEFT JOIN commands c ON c.session_id = s.id
        """
        params: list[Any] = []
        if hostname:
            sql += " WHERE s.hostname = ?"
            params.append(hostname)
        sql += " GROUP BY s.id ORDER BY s.started_at DESC LIMIT ?"
        params.append(min(max(limit, 1), 500))

        return [dict(row) for row in connection.execute(sql, params).fetchall()]

    def get_session(self, session_id: str) -> dict | None:
        """Return one session with every command it ran, in order."""
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

    def known_hostnames(self) -> list[str]:
        """Every device seen, for the history filter."""
        connection = self.connect()
        rows = connection.execute(
            "SELECT DISTINCT hostname FROM sessions WHERE hostname != '' ORDER BY hostname"
        ).fetchall()
        return [row["hostname"] for row in rows]

    def delete_session(self, session_id: str) -> bool:
        """Delete a session and its commands."""
        with self._lock:
            connection = self.connect()
            connection.execute("DELETE FROM commands WHERE session_id = ?", (session_id,))
            cursor = connection.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            connection.commit()
            return cursor.rowcount > 0

    def stats(self) -> dict:
        """Summary counts for the history panel header."""
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

        digest = hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()
        previous = self.latest_snapshot(hostname)

        if previous and previous["sha256"] == digest:
            return {"stored": False, "unchanged": True, "snapshot": previous}

        with self._lock:
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
    """
    words = [word for word in text.replace('"', " ").split() if word]
    if not words:
        return '""'
    return " AND ".join(f'"{word}"' for word in words)


# Process-wide instance — one database file, one owner.
store = SessionStore()
