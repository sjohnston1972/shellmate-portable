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

    switch01#                     IOS / IOS-XE / NX-OS / Aruba AOS-CX
    switch01(config-if)#          IOS configuration modes
    ASA-FW/pri/act#               ASA failover context
    RP/0/RSP0/CPU0:edge-xr#       IOS-XR, node id and all
    FGT-01 (global) #             FortiOS, with the current object named
    neteng@srx-edge>              Junos operational
    neteng@srx-edge#              Junos configuration
    admin@PA-VM(active)>          PAN-OS
    <core-sw1>                    Huawei VRP user view
    [core-sw1-GigabitEthernet0/0/1]  Huawei VRP system view
    [admin@MikroTik] >            MikroTik RouterOS
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
        # MikroTik RouterOS: [admin@MikroTik] > and, inside a menu,
        # [admin@MikroTik] /ip address> — the path may carry a space.
        (?:
            \[[A-Za-z0-9._-]+@[A-Za-z0-9._-]{1,63}\]
            [ \t]*(?:/[A-Za-z0-9 ._-]{0,60})?[ \t]*
        )
      | # Huawei VRP, which closes its own prompt rather than ending in a
        # sigil: <core-sw1> is the user view, [core-sw1] the system view,
        # and [core-sw1-GigabitEthernet0/0/1] an interface within it. The
        # ~ and * are VRP's uncommitted-change markers.
        (?P<vrp>
            \[[~*]?[A-Za-z0-9._/-]{1,80}\]
          | <[A-Za-z0-9._-]{1,63}>
        )
      | # Everything else: a hostname with the shapes vendors hang off it.
        (?:[A-Za-z0-9._-]+@)?        # optional user@ (Junos, PAN-OS, Linux)
        [A-Za-z0-9._-]{1,63}         # hostname
        (?:/[A-Za-z0-9._-]+)*        # ASA failover context: /pri/act
                                     # IOS-XR node id: RP/0/RSP0/CPU0
        (?:[ \t]*\([^)\n]{0,40}\))?  # mode: (config), (config-if), (active)
                                     # FortiOS puts a space first: (global)
        (?::[^\s#>$%]{0,40})?        # Linux path, or the IOS-XR hostname
    )
    [ \t]*
    # The prompt character itself — but the bracketed forms above have
    # already consumed their own closing bracket, and requiring a sigil
    # after one would reject every Huawei prompt there is.
    (?P<sigil>(?(vrp)|[#>$%]))
    (?(vrp)|[ \t]?)                  # single space devices put after it
    """,
    re.VERBOSE,
)

# Junos prints this above the prompt while in configuration mode. It looks
# nothing like a prompt but must not be mistaken for command output either.
EDIT_BANNER_RE = re.compile(r"^\[edit[^\]]*\]$")

# Lines that are never a prompt, however much they resemble one.
#
# The bracketed vendor forms above cost something here. `[HUAWEI]` and `[OK]`
# are the same shape, and `[OK]` is what IOS prints after `write memory`;
# `<core-sw1>` and `<html>` are the same shape too. Nothing in the text
# separates them — only what the word means — so the words that are never a
# hostname are listed. The trade is that a device named `ok` or `html` is not
# recognised from its prompt, which is the right way round: a missed prompt
# merges two records, a false one files configuration under the wrong command.
_NOT_A_PROMPT = re.compile(
    r"""
    ^\s*(?:
        [-=*_]{3,}                   # rule / separator lines
      | \d+[:.]                      # numbered output
      | \[\d+\]                      # a shell job number
      | \[edit\b                     # a Junos configuration banner
      | \[(?:ok|fail|failed|error|err|warn|warning|info|debug|notice
            |crit|critical|alert|emerg|done|pass|passed|yes|no|y/n
            |confirm|abort|aborted|skipped|none)\]
      | <\s*[/!?]                    # a closing or declaring markup tag
      | <(?:html|head|body|title|div|span|table|thead|tbody|tr|td|th
           |ul|ol|li|br|hr|pre|code|script|style|meta|link|form|input
           |img|xml|rpc|rpc-reply|data|configuration|nc|soap)\s*/?>
    )
    """,
    re.VERBOSE | re.IGNORECASE,
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


#: Huawei VRP names the current view in the prompt, after the hostname:
#: ``[core-sw1-GigabitEthernet0/0/1]``, ``[core-sw1-ospf-1]``. A hostname may
#: contain hyphens too, so the only way to find where it ends is to recognise
#: the view. Best effort, and only for a tab label — an unrecognised view
#: leaves the whole string, which is ugly rather than wrong.
_VRP_VIEWS = (
    "GigabitEthernet", "XGigabitEthernet", "Ethernet", "Eth-Trunk", "GE",
    "25GE", "40GE", "100GE", "Vlanif", "LoopBack", "NULL", "Tunnel", "Pos",
    "Serial", "MEth", "Virtual-Template", "Dialer", "Bridge-Domain",
    "aaa", "acl", "bfd", "bgp", "dhcp", "hwtacacs", "ike", "ipsec", "isis",
    "lacp", "mpls", "nqa", "ospf", "ospfv3", "pki", "policy", "radius",
    "rip", "ripng", "route-policy", "snmp", "stp", "ui-", "user-interface",
    "vlan", "vpn-instance", "vrrp", "wlan",
)


def _strip_vrp_view(name: str) -> str:
    """``core-sw1-GigabitEthernet0/0/1`` -> ``core-sw1``."""
    for index, character in enumerate(name):
        if character != "-":
            continue
        rest = name[index + 1:]
        if any(rest.startswith(view) for view in _VRP_VIEWS):
            return name[:index] or name
    return name


def hostname_from_prompt(prompt: str) -> str:
    """
    The device name inside one prompt, whatever shape it arrived in.

    Separate from :func:`detect_hostname` because the shapes disagree about
    where the name is: Junos puts it after an ``@``, Linux before a colon,
    and IOS-XR after one — ``RP/0/RSP0/CPU0:edge-xr#`` is the node the
    session landed on, then the hostname. Splitting that on the *first*
    colon, as the Linux case wants, names every XR tab "RP".
    """
    text = (prompt or "").strip()
    if not text:
        return ""

    # Huawei VRP user view: <core-sw1>
    if text.startswith("<") and text.endswith(">"):
        return _strip_vrp_view(text[1:-1])

    # A bracketed prompt: Huawei's system view, or RouterOS with its menu
    # path trailing after the bracket.
    if text.startswith("["):
        inner = text[1:].split("]", 1)[0].lstrip("~*")
        if "@" in inner:                            # [admin@MikroTik] >
            return inner.split("@")[-1]
        return _strip_vrp_view(inner)

    if text[-1] in "#>$%":
        text = text[:-1]                            # drop the sigil
    text = text.split("@")[-1]                      # user@host -> host
    head = text.split(":")[0]
    if ":" in text and "/" in head:
        text = text.rsplit(":", 1)[-1]              # RP/0/RSP0/CPU0:host
    else:
        text = head                                 # host:~ -> host
    text = re.sub(r"\(.*\)$", "", text).strip()     # host(config) -> host
    return text.split("/")[0]                       # host/pri/act -> host


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
        name = hostname_from_prompt(found[0])
        if len(name) >= 2 and not name.isdigit():
            hostname = name
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
    # True only while the device sits at a bare prompt with nothing typed
    # after it; cleared by any other output (#474).
    idle_at_prompt: bool = field(default=False, init=False)

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
        # Whether the device is sitting at a bare prompt right now (#474):
        # true only when the unterminated tail is a prompt and nothing
        # after it. `last_prompt` is never cleared, so it means "has ever
        # seen a prompt" — and the paging command was typed into a line
        # the user had started.
        self.idle_at_prompt = False
        if self._partial:
            found = match_prompt(self._partial)
            if found:
                self.last_prompt = found[0]
                self.idle_at_prompt = not (found[1] if len(found) > 1 else "").strip()
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
