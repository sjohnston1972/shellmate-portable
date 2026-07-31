"""
session/ansi.py — Turn a raw terminal stream into the text a human sees.

A terminal stream is not text.  It is a sequence of instructions to a display,
and the same visible line can arrive as any number of different byte
sequences.  Storing it raw and searching it later gives false negatives —
``grep`` for ``GigabitEthernet0/1`` misses the line when the device coloured
the interface name, because there is an escape sequence sitting in the middle
of the word.

Three distinct things have to be undone, and network gear does all three:

**Escape sequences.** Colour, cursor movement, window titles.  Invisible on
screen, but very much present in the bytes.

**Backspace.** How a device erases ``--More--`` when you press space: it sends
backspaces, then spaces, then backspaces again.  Left in place, every paged
``show run`` is peppered with ``--More--`` and control characters.

**Carriage return without newline.** How progress indicators and countdown
timers redraw a line in place.  ``Building configuration...\\r`` followed by
``[OK]`` is one line reading ``[OK]``, not two lines.

Applying these is what makes stored transcripts searchable and makes the
context sent to the AI resemble what the engineer is actually looking at.
"""

import re

# CSI sequences: ESC [ ... final-byte. Covers colour (SGR), cursor movement,
# erase-in-line, and the rest of the common repertoire.
_CSI = r"\x1b\[[0-?]*[ -/]*[@-~]"

# OSC sequences: ESC ] ... terminated by BEL or ST. Devices use these to set
# the window title, which some terminal servers do on every login.
_OSC = r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"

# Two-character escapes (ESC 7, ESC 8, ESC =, ESC >, charset selection...)
# and single-shift/DCS-style introducers we simply drop.
_SIMPLE_ESC = r"\x1b[()#][0-9A-Za-z]|\x1b[0-9A-Za-z=><]"

ANSI_RE = re.compile(f"{_OSC}|{_CSI}|{_SIMPLE_ESC}")

# Control characters that carry no meaning once the sequences above are gone.
# \t, \n and \r are deliberately excluded — they are handled or preserved.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def strip_ansi(text: str) -> str:
    """Remove escape sequences, leaving printable text and whitespace."""
    return ANSI_RE.sub("", text)


def apply_backspace(text: str) -> str:
    """
    Resolve backspace characters against the preceding text.

    A backspace at the very start of a line has nothing to delete and is
    simply dropped, which is what a real terminal does.
    """
    if "\b" not in text:
        return text

    out: list[str] = []
    for char in text:
        if char == "\b":
            # Never delete back past a line boundary; a terminal would not.
            if out and out[-1] not in ("\n", "\r"):
                out.pop()
        else:
            out.append(char)
    return "".join(out)


def apply_carriage_returns(line: str) -> str:
    """
    Resolve carriage returns within a single line.

    Text after a CR overwrites the line from column zero, but only as far as
    it reaches — the tail of the previous content survives if the new text is
    shorter. That is genuine terminal behaviour and it matters: a device
    redrawing ``100%`` over ``  0%`` must not leave ``100%%``.
    """
    if "\r" not in line:
        return line

    result = ""
    for index, segment in enumerate(line.split("\r")):
        if index == 0:
            result = segment
        elif len(segment) >= len(result):
            result = segment
        else:
            result = segment + result[len(segment):]
    return result


def clean(text: str) -> str:
    """
    Convert a raw terminal stream into the text a human would see.

    Applied in the order a terminal would: sequences first (so a backspace
    hidden inside one is not misread), then backspace, then per-line carriage
    returns.

    Line endings are normalised to ``\\n``. Trailing whitespace is left alone —
    it is occasionally meaningful in configuration output.
    """
    text = strip_ansi(text)
    text = apply_backspace(text)

    # Normalise CRLF before resolving bare CRs, or every line would look like
    # it were being overwritten by an empty string.
    text = text.replace("\r\n", "\n")

    if "\r" in text:
        text = "\n".join(apply_carriage_returns(line) for line in text.split("\n"))

    return _CONTROL_RE.sub("", text)


# ---------------------------------------------------------------------------
# Paging artefacts
# ---------------------------------------------------------------------------

# Pager prompts left behind when paging was not disabled. Cisco writes
# "--More--", Junos "---(more)---", others vary. By the time output is stored,
# these are noise that breaks up otherwise contiguous configuration.
_PAGER_RE = re.compile(
    r"^[ \t]*(?:--+\s*more\s*--+|---\(more(?:\s+\d+%)?\)---|<--- More --->)[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)


def strip_pager_prompts(text: str) -> str:
    """
    Remove leftover ``--More--`` prompts.

    Best-effort tidying for devices where paging was not turned off. Only
    whole lines are removed, so a genuine occurrence of the word inside real
    output is left alone.
    """
    return _PAGER_RE.sub("", text)
