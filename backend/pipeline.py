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
