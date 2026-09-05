"""
logsearch.py — Finding a line across every session log (#576).

The Logs panel listed files by name and opened one at a time. That works
while you remember which session it was in, and stops working at exactly
the point somebody needs it: "we changed something on a Tuesday and I do
not remember which switch."

Two filters, because they answer different questions and people arrive
with either. **When** narrows by the file's date. **What** searches inside
the files, with the case, regex and whole-word switches the history search
already has — a search that only matches whole files would send somebody
back to reading one at a time, which is where they started.

Three properties this has to hold, and each is a real failure that a
happy-path implementation gets wrong:

**It has to be bounded.** A log folder nobody has pruned holds gigabytes.
An unbounded search holds the request open reading all of it, and the panel
looks hung. So there is a byte bound per file and a hit bound per file,
both from Stockton, and both *announced in the result* — a search that
quietly stopped early is worse than one that took longer, because the
answer "no matches" is then indistinguishable from "I did not look".

**A bad pattern is a message, not a 500.** People type regular expressions
into a box while composing them, so a half-written one is the normal state
of the field rather than an error.

**A file that cannot be read is skipped, not fatal.** A log being written
to right now, or held open by something else, must not take the whole
search with it.
"""

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# Read in chunks rather than line by line off a huge file, and never hold
# more than this in memory at once regardless of how long a "line" is — a
# log with no newlines in it (a device streaming a progress bar) would
# otherwise be one enormous string.
CHUNK = 256 * 1024

# A single matching line is shown in a list, so a running configuration on
# one line has to be cut somewhere.
MAX_LINE = 400


class SearchError(ValueError):
    """The query itself is wrong — reported to the user, not logged as a fault."""


@dataclass
class FileHits:
    """What one log file contributed."""

    filename: str
    size_bytes: int
    modified: str
    hits: int = 0
    matches: list[dict] = field(default_factory=list)
    #: The byte bound stopped the read before the end of this file.
    truncated: bool = False
    #: The hit bound stopped lines being collected; ``hits`` still counts on.
    capped: bool = False

    def as_dict(self) -> dict:
        return {
            "filename": self.filename,
            "size_bytes": self.size_bytes,
            "modified": self.modified,
            "hits": self.hits,
            "matches": self.matches,
            "truncated": self.truncated,
            "capped": self.capped,
        }


def _bounds() -> tuple[int, int]:
    from backend.advanced import get as advanced

    return int(advanced("logs.search_max_bytes")), int(advanced("logs.search_max_hits"))


def build_pattern(query: str, regex: bool = False, case: bool = False,
                  whole_word: bool = False) -> re.Pattern:
    """
    Turn what somebody typed into a pattern.

    Raises:
        SearchError: The query is empty, or is an invalid regular expression.
    """
    text = query or ""
    if not text.strip():
        raise SearchError("Type something to search for.")
    body = text if regex else re.escape(text)
    if whole_word:
        # Word boundaries around an escaped literal are safe. Around a
        # user's own regex they are not always what they expect, but they
        # are what "whole word" means, and the alternative — silently
        # ignoring the switch in regex mode — is worse than surprising.
        body = r"\b(?:" + body + r")\b"

    flags = 0 if case else re.IGNORECASE
    try:
        return re.compile(body, flags)
    except re.error as exc:
        raise SearchError(f"That is not a valid pattern: {exc}") from exc


def _parse_day(value: str, end: bool = False) -> float | None:
    """
    A YYYY-MM-DD from the picker, as a timestamp.

    ``end`` takes the *end* of that day, so "until 5 September" includes
    the fifth. Off by one here means a search that silently omits the day
    somebody actually asked about, which is the day they are looking for.
    """
    text = (value or "").strip()
    if not text:
        return None
    try:
        day = date.fromisoformat(text)
    except ValueError as exc:
        raise SearchError(f"{text!r} is not a date (use YYYY-MM-DD).") from exc
    moment = datetime.combine(
        day, datetime.max.time() if end else datetime.min.time())
    return moment.timestamp()


def _scan(path: Path, pattern: re.Pattern, max_bytes: int,
          max_hits: int) -> tuple[int, list[dict], bool, bool]:
    """
    Search one file, bounded.

    Returns:
        ``(hits, matches, truncated, capped)``.
    """
    hits = 0
    matches: list[dict] = []
    truncated = False
    capped = False
    read = 0
    line_no = 0
    carry = ""

    with path.open("r", encoding="utf-8", errors="replace") as handle:
        while True:
            if read >= max_bytes:
                truncated = True
                break
            chunk = handle.read(min(CHUNK, max_bytes - read))
            if not chunk:
                break
            read += len(chunk)
            carry += chunk
            lines = carry.split("\n")
            # The last piece may be half a line; it waits for the next chunk.
            carry = lines.pop()
            for line in lines:
                line_no += 1
                if not pattern.search(line):
                    continue
                hits += 1
                if len(matches) < max_hits:
                    text = line.rstrip("\r")
                    matches.append({
                        "line": line_no,
                        "text": (text[:MAX_LINE] + " …"
                                 if len(text) > MAX_LINE else text),
                    })
                else:
                    capped = True

        # Whatever was left over after the final chunk is a real line too,
        # unless the read stopped at the bound mid-line — in which case it
        # is half of one and matching on it would report a hit that is not
        # in the file.
        if carry and not truncated:
            line_no += 1
            if pattern.search(carry):
                hits += 1
                if len(matches) < max_hits:
                    text = carry.rstrip("\r")
                    matches.append({
                        "line": line_no,
                        "text": (text[:MAX_LINE] + " …"
                                 if len(text) > MAX_LINE else text),
                    })
                else:
                    capped = True

    return hits, matches, truncated, capped


def search(directory: Path, query: str, since: str = "", until: str = "",
           regex: bool = False, case: bool = False,
           whole_word: bool = False) -> dict:
    """
    Search every log file in ``directory``.

    Args:
        directory:  The session-log folder.
        query:      What to look for, or "" to filter by date alone.
        since:      YYYY-MM-DD, or "" for no lower bound.
        until:      YYYY-MM-DD, or "" for no upper bound.
        regex:      Treat the query as a regular expression.
        case:       Match case.
        whole_word: Match whole words only.

    Returns:
        ``{"files": [...], "searched": n, "skipped": n, "hits": n,
           "truncated": bool}``.

    Raises:
        SearchError: The query or a date is malformed.
    """
    # An empty query is not an error, it is the absence of a text filter:
    # "show me the logs from that Tuesday" is a whole question on its own.
    # Substituting a pattern that matches everything would report every
    # line of every file as a hit, which is a number nobody asked for and
    # would read as a result.
    pattern = (build_pattern(query, regex, case, whole_word)
               if (query or "").strip() else None)
    after = _parse_day(since)
    before = _parse_day(until, end=True)
    if after is not None and before is not None and after > before:
        raise SearchError("The start of the range is after its end.")

    max_bytes, max_hits = _bounds()
    results: list[FileHits] = []
    searched = 0
    skipped = 0

    if not directory.exists():
        return {"files": [], "searched": 0, "skipped": 0, "hits": 0,
                "truncated": False}

    for path in sorted(directory.glob("*.log")):
        try:
            stat = path.stat()
        except OSError:
            # Deleted between the glob and the stat. Not worth a failure.
            skipped += 1
            continue

        if after is not None and stat.st_mtime < after:
            continue
        if before is not None and stat.st_mtime > before:
            continue

        searched += 1
        if pattern is None:
            results.append(FileHits(
                filename=path.name,
                size_bytes=stat.st_size,
                modified=datetime.fromtimestamp(stat.st_mtime).isoformat(),
            ))
            continue

        try:
            hits, matches, truncated, capped = _scan(
                path, pattern, max_bytes, max_hits)
        except OSError as exc:
            # Being written to, locked, or on a share that went away. One
            # unreadable file must not take the search with it.
            logger.info("Skipped %s while searching logs: %s", path.name, exc)
            skipped += 1
            continue

        if hits:
            results.append(FileHits(
                filename=path.name,
                size_bytes=stat.st_size,
                modified=datetime.fromtimestamp(stat.st_mtime).isoformat(),
                hits=hits, matches=matches,
                truncated=truncated, capped=capped,
            ))

    # Most hits first: the file that mentions the thing forty times is
    # almost always the session somebody is looking for, and sorting by
    # date would bury it under one that mentions it once. With no query
    # there are no hits to rank by, so newest first — which is what the
    # plain listing does, and this is a filtered listing.
    if pattern is None:
        results.sort(key=lambda item: item.modified, reverse=True)
    else:
        results.sort(key=lambda item: (-item.hits, item.filename))

    return {
        "files": [item.as_dict() for item in results],
        "searched": searched,
        "skipped": skipped,
        "hits": sum(item.hits for item in results),
        "truncated": any(item.truncated for item in results),
    }
