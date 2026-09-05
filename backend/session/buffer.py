"""
session/buffer.py — Per-session terminal I/O buffer for ShellMate.

Each active session gets one SessionBuffer instance.  It keeps a rolling
window of terminal output lines (up to max_lines) using a deque, which
gives O(1) append and automatic eviction of old lines.  A separate raw
list stores the original byte strings for any future full-fidelity replay.
"""

import time
from collections import deque
from typing import Deque


class SessionBuffer:
    """Stores all terminal output for a single session."""

    def __init__(self, session_id: str, max_lines: int = 5000) -> None:
        """
        Args:
            session_id: The UUID string that identifies the owning session.
            max_lines:  Maximum number of lines to keep in memory before
                        old lines are evicted from the front of the deque.
        """
        self.session_id: str = session_id
        self.max_lines: int = max_lines

        # Rolling line buffer — oldest lines fall off the left end
        self._lines: Deque[str] = deque(maxlen=max_lines)

        # When each of those lines arrived, in step with it (#553).
        # The assistant is told where its visible window starts, and
        # "I cannot see that" is only checkable if there is a time to
        # check it against. A parallel deque rather than tuples in the
        # one above, so every existing reader of _lines is untouched.
        self._times: Deque[float] = deque(maxlen=max_lines)

        # Lines evicted since the session opened. The count matters more
        # than the lines: "400 older lines are not visible" is the
        # difference between an answer about the session and an answer
        # about the last screenful of it.
        self._dropped: int = 0

        # Accumulates the current incomplete line until we see a newline
        self._pending: str = ""

        # There is deliberately no raw-bytes copy here (#345). One existed,
        # unbounded and with no reader — a session left on `terminal monitor`
        # overnight held every byte in memory for a replay feature nothing
        # implements. The deque above is the bound; full-fidelity recording
        # is the session log's job.

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def write(self, data: str) -> None:
        """
        Append terminal output to the buffer.

        Splits on newline characters so that each logical line is stored
        as a separate entry in the deque.  The final fragment (no trailing
        newline) is held in _pending and prepended to the next write.

        Args:
            data: Raw string data received from the terminal channel.
        """
        if not data:
            return

        # Combine any leftover partial line with the new data
        combined = self._pending + data

        # Split on newlines — the last element may be an incomplete line
        parts = combined.split("\n")

        # All parts except the last are complete lines
        now = time.time()
        for line in parts[:-1]:
            # Strip carriage returns that come from CR+LF sequences
            if len(self._lines) == self._lines.maxlen:
                self._dropped += 1
            self._lines.append(line.rstrip("\r"))
            self._times.append(now)

        # The last part is either empty (data ended with \n) or an
        # incomplete line that we hold until the next write
        self._pending = parts[-1]

    def get_lines(self, n: int = 200) -> list[str]:
        """
        Return the last *n* lines stored in the buffer.

        Includes the pending fragment — the text received since the last
        newline — as a final line when there is one.

        That fragment is almost always the device's prompt, or a command the
        user is part-way through typing, because a prompt is written without a
        trailing newline while it waits for input.  Omitting it means the
        buffer never contains the one line that says which device you are on
        and what mode it is in, which is exactly the context the AI needs and
        what hostname detection reads.

        Args:
            n: Number of lines to return.  Clamped to what is available.
               Zero or less returns nothing.

        Returns:
            List of strings, oldest first.
        """
        # Zero has to mean none, and slicing does not say so: ``lines[-0:]``
        # is ``lines[0:]``, the whole buffer. ``ai.context_lines`` is
        # documented as "zero sends none" and is the setting chosen precisely
        # to keep device output away from a cloud provider (#494).
        if n <= 0:
            return []
        lines = list(self._lines)
        if self._pending:
            lines.append(self._pending)
        return lines[-n:] if n < len(lines) else lines

    def get_text(self, n: int = 200) -> str:
        """
        Return the last *n* lines as a single newline-joined string.

        Args:
            n: Number of lines to include.

        Returns:
            Multi-line string.
        """
        return "\n".join(self.get_lines(n))

    def horizon(self, n: int = 200) -> dict:
        """
        Where the window the assistant can see begins (#553).

        The model is told to say when it cannot see enough, and was
        never told where "enough" ends. Without this, "I cannot see
        that" is an assertion nobody can check; with it, it is a claim
        about a stated line and a stated time.

        Args:
            n: How many lines are being sent.

        Returns:
            ``{"visible", "hidden", "since", "total"}`` — how many
            lines are in the window, how many are not, when the oldest
            visible one arrived, and how many the session has had
            altogether. ``since`` is 0 when nothing has been written.
        """
        stored = len(self._lines) + (1 if self._pending else 0)
        visible = min(max(0, n), stored)
        # Both halves of what is missing: lines still in the buffer
        # but outside the window, and lines the buffer has evicted.
        # Reporting only one of them understates it, and the whole
        # point is that the number is honest.
        hidden = (stored - visible) + self._dropped
        since = 0.0
        if visible and self._times:
            index = max(0, len(self._times) - visible)
            since = self._times[min(index, len(self._times) - 1)]
        return {"visible": visible, "hidden": hidden,
                "since": since, "total": stored + self._dropped}

    def clear(self) -> None:
        """Discard all stored data and reset the pending line fragment."""
        self._lines.clear()
        self._times.clear()
        self._dropped = 0
        self._pending = ""

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def line_count(self) -> int:
        """
        Number of lines currently stored, counting the pending fragment.

        Matches what get_lines() returns, so the line count shown in the
        status bar agrees with the content the AI is given.
        """
        return len(self._lines) + (1 if self._pending else 0)
