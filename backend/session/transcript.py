"""
session/transcript.py — Turn a terminal stream into commands and their output.

Until now the backend had no concept of "a command".  A session was a rolling
window of bytes, which is enough to paint a screen and nothing else.  You
cannot ask "what did I change on the Glasgow core last Tuesday", diff a
config against last week's, or build a change record out of an undifferentiated
blob of text.

This module reconstructs the structure by watching for the device's prompt.
Everything between one prompt and the next is one command and its output.

Doing that reliably rests on recognising a prompt across vendors, which is
where the previous Cisco-only pattern fell down.  The regex below covers:

    switch01#                     IOS / IOS-XE / NX-OS
    switch01(config-if)#          IOS configuration modes
    ASA-FW/pri/act#               ASA failover context
    neteng@srx-edge>              Junos operational
    neteng@srx-edge#              Junos configuration
    admin@PA-VM(active)>          PAN-OS
    [edit interfaces]             Junos edit banner (not itself a prompt)
    neteng@jump:~$                Linux jump hosts

The parser is deliberately conservative.  A missed prompt costs one merged
record; a *false* prompt would slice real output in half and attribute
configuration lines to the wrong command, which is far worse when the result
is being used as evidence of what changed.
"""

import logging
import re
import time
from dataclasses import dataclass, field

from backend.session.ansi import clean, strip_pager_prompts

# ---------------------------------------------------------------------------
# Prompt recognition
# ---------------------------------------------------------------------------

PROMPT_RE = re.compile(
    r"""
    ^[ \t]*                          # leading space some devices emit
    (?P<prompt>
        (?:[A-Za-z0-9._-]+@)?        # optional user@ (Junos, PAN-OS, Linux)
        [A-Za-z0-9._-]{1,63}         # hostname
        (?:/[A-Za-z0-9._-]+)*        # ASA failover context: /pri/act
        (?:\([^)\n]{0,40}\))?        # mode: (config), (config-if), (active)
        (?::[^\s#>$%]{0,40})?        # Linux path after a colon
    )
    [ \t]*
    (?P<sigil>[#>$%])                # the prompt character itself
    [ \t]?                           # single space devices put after it
    """,
    re.VERBOSE,
)

# Junos prints this above the prompt while in configuration mode. It looks
# nothing like a prompt but must not be mistaken for command output either.
EDIT_BANNER_RE = re.compile(r"^\[edit[^\]]*\]$")

# Lines that are never a prompt, however much they resemble one.
_NOT_A_PROMPT = re.compile(
    r"""
    ^\s*(?:
        [-=*_]{3,}                   # rule / separator lines
      | \d+[:.]                      # numbered output
    )
    """,
    re.VERBOSE,
)


logger = logging.getLogger(__name__)


def _active_prompt_re():
    """
    The prompt pattern in force, honouring the Stockton override.

    One pattern drives the whole transcript layer — which text is a command,
    and where its output ends. An invalid expression falls back to the built-in
    with a log line rather than leaving every session unparseable, which is why
    this is compiled here rather than at import.
    """
    global _override_source, _override_re, _override_seen
    # Once per settings change, not once per line (#458): the version moves
    # only when settings.json has been re-read.
    try:
        from backend.settings_store import settings_version
        version = settings_version()
    except Exception:
        return PROMPT_RE
    if version == _override_seen:
        return _override_re or PROMPT_RE
    _override_seen = version
    try:
        from backend.advanced import get as advanced
        source = str(advanced("history.prompt_pattern") or "")
    except Exception:
        return PROMPT_RE

    if not source:
        return PROMPT_RE
    if source != _override_source:
        _override_source = source
        _override_re = _compile_override(source)
    return _override_re or PROMPT_RE


#: What match_prompt() reads off a match. A pattern without both of these
#: compiles perfectly well and then raises IndexError on the first line of
#: output — taking the transcript layer, the history and the drift check with
#: it. Checked here so a bad pattern is rejected rather than armed.
REQUIRED_GROUPS = ("prompt", "sigil")


def _compile_override(source: str):
    """Compile a user pattern, or return None with a reason in the log."""
    try:
        compiled = re.compile(source)
    except re.error as exc:
        logger.warning("Ignoring an invalid prompt pattern (%s); using the "
                       "built-in", exc)
        return None

    missing = [g for g in REQUIRED_GROUPS if g not in compiled.groupindex]
    if missing:
        logger.warning(
            "Ignoring a prompt pattern with no (?P<%s>...) group; using the "
            "built-in. A pattern needs %s.",
            ">, (?P<".join(missing),
            " and ".join(f"(?P<{g}>...)" for g in REQUIRED_GROUPS),
        )
        return None

    return compiled


_override_seen: int = -1          # settings_version() the override was read at
_override_source: str = ""
_override_re = None


def match_prompt(line: str) -> tuple[str, str] | None:
    """
    Test whether *line* begins with a device prompt.

    Args:
        line: A single cleaned line, without its newline.

    Returns:
        ``(prompt, remainder)`` when it does — the remainder being whatever
        the user typed after it — or None.
    """
    if not line or len(line) > 512:
        # A prompt is short. Anything long is output that happens to contain
        # a '#', which is most of a Cisco configuration file.
        return None
    if _NOT_A_PROMPT.match(line):
        return None

    match = _active_prompt_re().match(line)
    if not match:
        return None

    remainder = line[match.end():]

    # A '#' mid-sentence is a comment or a hash, not a prompt. Require that
    # what follows looks like a command, or nothing at all.
    if remainder and not re.match(r"^[A-Za-z0-9./_-]", remainder):
        return None

    return match.group("prompt") + match.group("sigil"), remainder


def detect_hostname(text: str) -> str | None:
    """
    Return the device hostname parsed from the most recent prompt.

    Used for the tab label. Prefers the last prompt in the text, since early
    output may still be the terminal server rather than the device itself.
    """
    hostname = None
    for line in clean(text).splitlines():
        found = match_prompt(line)
        if not found:
            continue
        prompt = found[0][:-1]                      # drop the sigil
        prompt = prompt.split("@")[-1]              # user@host -> host
        prompt = prompt.split(":")[0]               # host:~ -> host
        prompt = re.sub(r"\(.*\)$", "", prompt)     # host(config) -> host
        prompt = prompt.split("/")[0]               # host/pri/act -> host
        if len(prompt) >= 2 and not prompt.isdigit():
            hostname = prompt
    return hostname


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass
class CommandRecord:
    """One command and everything the device said in reply."""

    command: str
    output: str = ""
    prompt: str = ""
    started_at: float = 0.0
    duration_ms: int = 0

    def as_dict(self) -> dict:
        return {
            "command":     self.command,
            "output":      self.output,
            "prompt":      self.prompt,
            "started_at":  self.started_at,
            "duration_ms": self.duration_ms,
        }


# ---------------------------------------------------------------------------
# The parser
# ---------------------------------------------------------------------------


@dataclass
class TranscriptParser:
    """
    Incremental parser turning a terminal stream into CommandRecords.

    Fed the same data as the terminal, in whatever chunk sizes arrive. State
    is carried between calls because a prompt, a command, or a line of output
    can be split across TCP segments.

    Completed records are returned from :meth:`feed`; the command currently
    running is available from :attr:`pending` until its next prompt arrives.
    """

    # Text received but not yet terminated by a newline, cleaned — what the
    # prompt matcher and the rest of this class read.
    _partial: str = field(default="", init=False)

    # The same tail, exactly as it arrived (#272).
    #
    # Cleaning has to see the whole unterminated line at once, because an
    # erase only means anything against what precedes it. A device answers
    # Ctrl-U with backspaces, spaces and backspaces again — and when that
    # arrives in its own chunk, cleaning the chunk alone finds nothing to
    # delete and discards the erase. The alias the user typed then stayed in
    # front of the expansion ShellMate sent, and the pair were recorded as
    # one command: "arpshow ip arp". Typo correction and arrow-key redraws
    # were corrupted the same way, for the same reason.
    _raw: str = field(default="", init=False)

    # The command awaiting its next prompt.
    _current: CommandRecord | None = field(default=None, init=False)
    _output_lines: list[str] = field(default_factory=list, init=False)

    # Last prompt seen, so a record knows which device and mode it ran in.
    last_prompt: str = field(default="", init=False)

    def feed(self, chunk: str) -> list[CommandRecord]:
        """
        Consume a chunk of raw terminal output.

        Returns:
            Records completed by this chunk. Usually empty — a record is only
            finished when the device returns to a prompt.
        """
        if not chunk:
            return []

        # Accumulate raw, and clean whole lines together with the tail they
        # belong to — never a chunk in isolation (#272).
        self._raw += chunk
        head, newline, tail = self._raw.rpartition("\n")
        self._raw = tail

        # A stream with no newline at all must not grow the tail without
        # bound (#346): an IOS `copy` printing `!` per block, an xmodem
        # transfer, CR-only progress redraws. Left alone, the tail grows for
        # the transfer's duration *and* clean() re-runs over all of it on
        # every chunk — O(n²) on the per-output hot path. A prompt is at most
        # 512 characters by match_prompt's own rule, so keeping the last 8 KB
        # loses nothing the parser could have used.
        if len(self._raw) > 32768:
            self._raw = self._raw[-8192:]

        lines: list[str] = []
        if newline:
            # The trailing empty element after the final newline is not a line.
            lines = clean(head + newline).split("\n")
            lines.pop()

        # Re-cleaned in full each time, so an erase arriving later still finds
        # the characters it is meant to remove. From self._raw, not the local
        # tail — the cap above may just have trimmed it.
        self._partial = clean(self._raw)

        if not lines and not self._partial:
            return []

        completed: list[CommandRecord] = []
        for line in lines:
            record = self._consume_line(line)
            if record is not None:
                completed.append(record)

        # A prompt arrives without a trailing newline — the device has printed
        # it and is waiting for input — so it never reaches _consume_line and
        # has to be recognised from the unterminated tail.
        #
        # This has to happen whether or not a command is running. A freshly
        # connected device sits at a prompt having run nothing, and treating
        # that as "no prompt seen yet" leaves everything downstream — device
        # fingerprinting, alias expansion, config capture — waiting for a
        # newline that only arrives once the user types something.
        if self._partial:
            found = match_prompt(self._partial)
            if found:
                self.last_prompt = found[0]
                if self._current is not None:
                    finished = self._finish_current()
                    if finished is not None:
                        completed.append(finished)

        return completed

    def _consume_line(self, line: str) -> CommandRecord | None:
        """Process one complete line. Returns a record if it closed one."""
        found = match_prompt(line)

        if found is None:
            # Junos prints [edit] above its prompt; it belongs to neither the
            # command nor its output.
            if EDIT_BANNER_RE.match(line.strip()):
                return None
            if self._current is not None:
                self._output_lines.append(line)
            return None

        prompt, remainder = found
        self.last_prompt = prompt

        # Reaching a prompt closes whatever was running.
        finished = self._finish_current()

        command = remainder.strip()
        if command:
            self._current = CommandRecord(
                command=command, prompt=prompt, started_at=time.time(),
            )
            self._output_lines = []
        else:
            # A bare prompt: the device is idle, nothing new started.
            self._current = None
            self._output_lines = []

        return finished

    def _finish_current(self) -> CommandRecord | None:
        """Close the running command and return it, if there was one."""
        if self._current is None:
            return None

        record = self._current
        record.output = strip_pager_prompts("\n".join(self._output_lines)).strip("\n")
        record.duration_ms = max(0, int((time.time() - record.started_at) * 1000))

        self._current = None
        self._output_lines = []
        return record

    def flush(self) -> CommandRecord | None:
        """
        Close the in-flight command at end of session.

        A session usually ends mid-command — the user closes the tab while
        output is still arriving, or the device drops the connection. Without
        this, that last command is lost, and it is often the interesting one.
        """
        if self._partial and self._current is not None:
            self._output_lines.append(self._partial)
        self._partial = ""
        self._raw = ""
        return self._finish_current()

    @property
    def pending(self) -> CommandRecord | None:
        """The command currently running, if any."""
        return self._current
