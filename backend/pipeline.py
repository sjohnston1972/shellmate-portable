"""
pipeline.py — The chokepoint every keystroke passes through on its way out.

Until now the WebSocket handler forwarded raw bytes straight to the device.
That leaves nowhere for anything that needs to *understand* what is being sent:
expanding an alias, warning before ``write erase`` on a production core,
pacing a pasted configuration.

The pipeline assembles keystrokes into candidate command lines and runs each
through an ordered chain before it reaches the device:

    keystrokes -> line assembly -> alias expansion -> [guardrails] -> [throttle] -> device

Only alias expansion exists today; the guardrail and throttle stages are Phase
6 and slot into the same chain.

Two things make this less trivial than it looks.

**The user is mid-line.**  Keystrokes arrive one at a time and the device has
already echoed them, so a substitution at Enter has to clear what the user
typed. Ctrl-U (kill line) does that on IOS, NX-OS, Junos and readline shells
alike, which is far safer than counting backspaces and hoping the echo matched.

**Editing keys exist.**  Backspace, Ctrl-U and Ctrl-C all change the line
without a newline. Ignoring them means the assembled line drifts out of step
with what is actually on the device's input line, and a substitution built on
a stale line would send something the user never typed.
"""

import logging
from dataclasses import dataclass, field

from backend.platforms import resolve_alias

logger = logging.getLogger(__name__)

# Control characters we have to interpret rather than merely pass along.
CR = "\r"
LF = "\n"
BACKSPACE = "\b"
DELETE = "\x7f"
CTRL_C = "\x03"
CTRL_U = "\x15"          # kill line — also what we send to undo an alias
CTRL_W = "\x17"          # delete previous word
ESC = "\x1b"

# A line longer than this is a paste, not typing. Alias expansion is for
# things people type.
MAX_ALIAS_LINE = 64


@dataclass
class OutboundPipeline:
    """
    Per-session outbound processing.

    Not thread-safe: one WebSocket reader owns one pipeline, which is the only
    thing that writes to it.
    """

    # What the user has typed since the last Enter. Mirrors the device's
    # current input line as closely as we can track it.
    _line: str = field(default="", init=False)

    # Set by the session once the device has been identified.
    platform: str = field(default="", init=False)

    # Whether alias expansion is switched on for this session.
    expand_aliases: bool = field(default=True, init=False)

    # Commands the pipeline rewrote, for the UI to report honestly.
    last_expansion: tuple[str, str] | None = field(default=None, init=False)

    def process(self, data: str) -> str:
        """
        Transform outbound keystrokes.

        Args:
            data: Exactly what the browser sent.

        Returns:
            What should actually reach the device — usually *data* unchanged.
        """
        if not data:
            return data

        out: list[str] = []
        self.last_expansion = None

        for char in data:
            if char in (CR, LF):
                out.append(self._on_enter(char))
            elif char in (BACKSPACE, DELETE):
                self._line = self._line[:-1]
                out.append(char)
            elif char == CTRL_U:
                self._line = ""
                out.append(char)
            elif char == CTRL_W:
                self._line = self._line.rstrip().rsplit(" ", 1)[0] if " " in self._line.strip() else ""
                out.append(char)
            elif char == CTRL_C:
                self._line = ""
                out.append(char)
            elif char == ESC:
                # Arrow keys and history recall move the cursor in ways we
                # cannot track from this side. Rather than guess, drop the
                # assembled line so no substitution is made from a line we can
                # no longer vouch for.
                self._line = ""
                out.append(char)
            elif char.isprintable():
                self._line += char
                out.append(char)
            else:
                out.append(char)

        return "".join(out)

    def _on_enter(self, terminator: str) -> str:
        """Handle Enter: expand an alias if the line is exactly one."""
        line = self._line
        self._line = ""

        if not self.expand_aliases or not self.platform:
            return terminator
        if not line.strip() or len(line) > MAX_ALIAS_LINE:
            return terminator

        expansion = resolve_alias(self.platform, line)
        if not expansion:
            return terminator

        self.last_expansion = (line.strip(), expansion)
        logger.info("Expanded alias %r to %r", line.strip(), expansion)

        # Ctrl-U clears whatever the device has echoed onto its input line, so
        # the expansion replaces it rather than being appended to it.
        return CTRL_U + expansion + terminator

    def reset(self) -> None:
        """Forget the partial line, e.g. after a reconnect."""
        self._line = ""
        self.last_expansion = None

    @property
    def current_line(self) -> str:
        """What the user appears to have typed so far."""
        return self._line
