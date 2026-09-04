"""
pipeline.py — The chokepoint every keystroke passes through on its way out.

Until now the WebSocket handler forwarded raw bytes straight to the device.
That leaves nowhere for anything that needs to *understand* what is being sent:
expanding an alias, warning before ``write erase`` on a production core,
pacing a pasted configuration.

The pipeline assembles keystrokes into candidate command lines and runs each
through an ordered chain before it reaches the device:

    keystrokes -> line assembly -> alias expansion -> guardrails -> device

Alias expansion and the dangerous-command guardrail both run here. Paste
pacing is split, because the two kinds of pacing answer different questions.
Chunking a paste by *bytes* is done in the frontend (`terminal.js`), before
the bytes reach the socket, which is the only place a stream can be slowed
down. Pacing it by *lines* is :class:`PasteBatch`, below: a line at a time,
waiting for the device to come back to its prompt — which only this side can
see — with every line going through the pipeline like any other.

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
import time
from dataclasses import dataclass, field

from backend.platforms import GENERIC, matches_dangerous, resolve_alias

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

    # The full command lines completed by this batch of keystrokes, after any
    # expansion. Published rather than merely used here because other things
    # need to know what was actually sent — the alert tracker has to see
    # "reload in 10" the moment Enter is pressed, and this is the one place
    # where a line is known to be complete.
    completed_commands: list[str] = field(default_factory=list, init=False)

    #: Destructive commands waiting for an answer, oldest first. Nothing has
    #: been sent for any of them. A queue rather than one slot (#343): a
    #: pasted batch can hold two commands, and one slot kept only the last —
    #: the first vanished with no prompt and no record. And a pending hold
    #: survives unrelated keystrokes: it is cleared by release() or drop(),
    #: never by the next call to process().
    held_commands: list[str] = field(default_factory=list, init=False)

    #: What the *latest* process() call held, for the caller to prompt on.
    newly_held: list[str] = field(default_factory=list, init=False)

    @property
    def held_command(self) -> str:
        """The oldest command still waiting for an answer, or ""."""
        return self.held_commands[0] if self.held_commands else ""

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
        self.completed_commands = []
        self.newly_held = []

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
        """Handle Enter: expand an alias, then check what is about to be sent."""
        line = self._line
        self._line = ""

        if line.strip():
            self.completed_commands.append(line.strip())

        expanded = self._expand(line)
        if expanded is not None:
            line = expanded

        # The guardrail runs on what will actually reach the device, so an
        # alias for `reload in 10` is caught exactly as typing it out would be.
        held = self._guard(line)
        if held is not None:
            return held

        if expanded is not None:
            # Ctrl-U clears whatever the device has echoed onto its input
            # line, so the expansion replaces it rather than appending to it.
            return CTRL_U + expanded + terminator
        return terminator

    def _guard(self, line: str) -> str | None:
        """
        Hold a destructive command until somebody confirms it.

        The risk was inverted before this existed. A `write erase` *suggested
        by the assistant* got a confirmation; the same `write erase` typed by
        the engineer went straight to the device. The second is how the
        accident actually happens — `reload` typed from muscle memory into
        whichever tab happens to be focused is the canonical mistake, and
        ShellMate already knows which tab is which device and which commands
        are destructive on it.

        Returns CTRL_U to swallow the line, or None to let it through.

        **Deliberately partial, and the interface says so.** This works on the
        assembled line, and the assembler gives up on ESC because arrow keys
        and history recall move the cursor invisibly — so a recalled `reload`
        arrives with nothing to match against. Failing open with a visible
        tell beats pretending to total coverage: a guardrail believed to catch
        everything is worse than a known-partial one.
        """
        from backend.advanced import get as advanced

        command = line.strip()
        if not command or not advanced("terminal.confirm_dangerous"):
            return None

        # Below the confidence gate there is no platform list to consult. The
        # generic profile carries the commands that are destructive on
        # anything, which is the honest thing to check against.
        platform = self.platform
        if not platform:
            if advanced("terminal.confirm_dangerous_scope") == "identified-only":
                return None
            platform = GENERIC

        if not matches_dangerous(platform, command):
            return None

        self.held_commands.append(command)
        self.newly_held.append(command)
        logger.info("Holding %r for confirmation", command)
        # Clear what the device echoed. Nothing is sent until the answer
        # comes back, so an unanswered prompt leaves the device untouched.
        return CTRL_U

    def _take(self, command: str | None) -> str:
        """Remove and return a pending hold — the named one, or the oldest."""
        if not self.held_commands:
            return ""
        if command:
            if command not in self.held_commands:
                return ""
            self.held_commands.remove(command)
            return command
        return self.held_commands.pop(0)

    def release(self, command: str | None = None) -> str:
        """A held command plus a carriage return, and forget it.

        Named, so two prompts answered out of order each act on their own
        command (#343); unnamed, the oldest — the order they were prompted.
        """
        taken = self._take(command)
        return (taken + CR) if taken else ""

    def drop(self, command: str | None = None) -> str:
        """Forget a held command. Returns what was dropped, for the log."""
        return self._take(command)

    def _expand(self, line: str) -> str | None:
        """The alias expansion for a line, or None if there is not one."""
        if not self.expand_aliases or not self.platform:
            return None
        if not line.strip() or len(line) > MAX_ALIAS_LINE:
            return None

        expansion = resolve_alias(self.platform, line)
        if not expansion:
            return None

        # What reaches the device is the expansion, so that is what anything
        # downstream should be told about — an alias for `reload in 10` has to
        # raise the same alert as typing it out.
        if self.completed_commands:
            self.completed_commands[-1] = expansion

        self.last_expansion = (line.strip(), expansion)
        logger.info("Expanded alias %r to %r", line.strip(), expansion)
        return expansion

    def reset(self) -> None:
        """Forget the partial line, e.g. after a reconnect."""
        self._line = ""
        self.last_expansion = None
        self.completed_commands = []
        self.held_commands = []
        self.newly_held = []

    @property
    def current_line(self) -> str:
        """What the user appears to have typed so far."""
        return self._line


# ---------------------------------------------------------------------------
# A pasted block, sent a line at a time
# ---------------------------------------------------------------------------

#: Below this, a line-paced paste is indistinguishable from an unpaced one:
#: every line falls due on the same tick and the whole block arrives at once,
#: which is the single outcome pacing exists to prevent. The byte-chunked path
#: in the browser floors its delay for the same reason.
MIN_LINE_DELAY = 0.01


@dataclass
class PasteBatch:
    """
    A pasted block, sent one line at a time, at a pace the device can take.

    Byte chunking — in the browser, before the bytes reach the socket — is the
    right knob for a serial input buffer that drops characters. It is the
    wrong one for sixty lines of ACL: what each line there needs is not a gap
    in milliseconds but *the device back at its prompt*, and only this side
    can see that.

    Owns the decision of what to send next and nothing else. The session's
    read loop owns the sending, because that is where the pipeline and the
    socket are — the same division as
    :class:`~backend.onboard.OnConnectScript`, and for the same reason.

    Two modes, one machine:

    - **wait_for_prompt** — a line goes out only when the device is idle at a
      bare prompt with nothing half-typed, and never before the device has
      said *something* since the last line. Without that second half the batch
      races its own output: ``idle_at_prompt`` is recomputed only when output
      arrives, so for the moment after a line is sent it still describes the
      prompt before it, and the whole block would go out at once into a device
      that had answered none of it.
    - **timed** — a line every ``delay`` seconds, for the places where there
      is no prompt worth waiting for: a boot loader, a terminal server, a
      device part-way through a dialogue.

    A stall is a *result*, not an error. The line it stopped at is the one
    thing the person whose session this is needs afterwards, so it is carried
    in the summary rather than left to be inferred from a count.
    """

    lines: list[str]
    wait_for_prompt: bool = True
    #: Seconds between lines in timed mode.
    delay: float = 0.2
    #: Seconds to wait for a prompt before giving up on the rest.
    timeout: float = 10.0

    sent: int = field(default=0, init=False)
    _index: int = field(default=0, init=False)
    _done: bool = field(default=False, init=False)
    _reason: str = field(default="", init=False)
    _stalled_at: int = field(default=0, init=False)

    #: Whether the device has said anything since the last line went out.
    #: True to begin with, because the first line is sent at a prompt the
    #: device printed itself.
    _saw_output: bool = field(default=True, init=False)
    _last_progress: float = field(default_factory=time.monotonic, init=False)

    @property
    def done(self) -> bool:
        return self._done

    @property
    def total(self) -> int:
        return len(self.lines)

    @property
    def remaining(self) -> int:
        return max(0, len(self.lines) - self._index)

    def finish(self, reason: str = "") -> None:
        """Stop, with a reason the interface can state."""
        if not self._done:
            self._done = True
            self._reason = reason
            if reason and self.remaining:
                self._stalled_at = self.sent

    def abort(self, reason: str) -> None:
        """Stop because of something outside the batch — a keystroke."""
        self.finish(reason)

    def observe(self, text: str) -> None:
        """The device said something. See ``_saw_output``."""
        if text:
            self._saw_output = True

    def hold(self, now: float | None = None) -> None:
        """
        Pause the clock while somebody is being asked a question.

        A line held at the guardrail is waiting on a human, and a human is
        slower than any device timeout. Without this the batch would time out
        behind its own confirmation dialog and report a stall that never
        happened.
        """
        self._last_progress = time.monotonic() if now is None else now

    def wait_for_device(self, now: float | None = None) -> None:
        """Something else was typed into the session; wait for the answer."""
        self._saw_output = False
        self.hold(now)

    def resume(self, now: float | None = None) -> None:
        """Nothing reached the device after all — carry on without waiting."""
        self._saw_output = True
        self.hold(now)

    def next_line(self, at_prompt: bool, now: float | None = None) -> str | None:
        """
        The next line to send, or None if it is not time.

        Args:
            at_prompt: The device is idle at a bare prompt with nothing
                half-typed. The caller resolves that, because it is the only
                thing holding both the transcript and the pipeline.
        """
        if self._done:
            return None
        clock = time.monotonic() if now is None else now

        if self._index >= len(self.lines):
            self.finish("")
            return None

        if self.wait_for_prompt:
            if not (at_prompt and self._saw_output):
                if clock - self._last_progress > self.timeout:
                    self.finish("no-prompt")
                return None
        elif clock - self._last_progress < max(self.delay, MIN_LINE_DELAY):
            return None

        line = self.lines[self._index]
        self._index += 1
        self.sent += 1
        self._last_progress = clock
        self._saw_output = False
        return line

    def summary(self) -> dict:
        """
        What went out, what did not, and where it stopped.

        ``stalled_at`` is the number of the line that was sent and never
        answered — "line 12 sent, no prompt seen" — rather than a bare count,
        because the next thing anybody does is go and look at line 12.
        """
        return {
            "sent":       self.sent,
            "total":      len(self.lines),
            "remaining":  self.remaining,
            "stalled_at": self._stalled_at,
            "reason":     self._reason,
        }
