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


# Cursor movement and erase within a line. These have to be *applied*, not
# stripped: a device redrawing its input line moves the cursor back and
# retypes over what is there, so discarding the movement concatenates the two
# versions instead of overwriting.
_CUB = re.compile(r"\x1b\[(\d*)D")     # cursor back
_CUF = re.compile(r"\x1b\[(\d*)C")     # cursor forward
_EL = re.compile(r"\x1b\[(\d?)K")      # erase in line
_CHA = re.compile(r"\x1b\[(\d*)G")     # cursor to column

# Anything that moves the cursor or erases within the current line.
_LINE_EDIT_RE = re.compile(r"\x1b\[\d*[DCGK]")


def apply_line_edits(text: str) -> str:
    """
    Replay cursor movement and erase-in-line against a single line.

    Devices redraw their input line rather than reprinting it: tab completion,
    ``?`` help, and arrow-key history all emit "move the cursor back N, then
    write". Stripping those escapes — as simply removing all sequences does —
    turns ``Tunnel7`` + back-7 + ``Tunnel7`` into ``Tunnel7Tunnel7``.

    That is not hypothetical. A real session recorded a command as
    ``Tunnel7Tunnel7Tunnel7S3-R2#Tunnel7Tunnel7Tunnel7``.

    This models just enough of a terminal to be correct for one line: a
    character buffer and a cursor. Writing overwrites at the cursor rather
    than appending, which is what a terminal actually does.
    """
    if not _LINE_EDIT_RE.search(text):
        return text

    buffer: list[str] = []
    cursor = 0
    index = 0
    length = len(text)

    def write(char: str) -> None:
        nonlocal cursor
        if cursor < len(buffer):
            buffer[cursor] = char        # overwrite, as a terminal does
        else:
            buffer.extend(" " * (cursor - len(buffer)))
            buffer.append(char)
        cursor += 1

    while index < length:
        char = text[index]

        if char == "\x1b" and index + 1 < length and text[index + 1] == "[":
            match = _LINE_EDIT_RE.match(text, index)
            if match:
                sequence = match.group(0)
                digits = sequence[2:-1]
                final = sequence[-1]
                count = int(digits) if digits.isdigit() else (0 if final == "K" else 1)

                if final == "D":
                    cursor = max(0, cursor - max(1, count))
                elif final == "C":
                    cursor = cursor + max(1, count)
                elif final == "G":
                    cursor = max(0, count - 1)       # columns are 1-based
                elif final == "K":
                    if count == 0:                   # to end of line
                        del buffer[cursor:]
                    elif count == 1:                 # to start of line
                        for i in range(min(cursor, len(buffer))):
                            buffer[i] = " "
                    else:                            # whole line
                        # Erases the content but leaves the cursor where it
                        # is, per ECMA-48. Devices that mean "start again"
                        # follow this with a CR or ESC[G, and honouring that
                        # rather than assuming it keeps us faithful.
                        buffer[:] = [" "] * len(buffer)
                index = match.end()
                continue

        if char == "\b":
            cursor = max(0, cursor - 1)
        else:
            write(char)
        index += 1

    return "".join(buffer)


def clean(text: str) -> str:
    """
    Convert a raw terminal stream into the text a human would see.

    Order matters, and mirrors what a terminal does:

    1. Replay cursor movement and erase-in-line, *before* anything is
       stripped — those sequences carry meaning that is lost once removed.
    2. Drop the remaining sequences, which are purely presentational.
    3. Resolve backspace and carriage returns.

    Line endings are normalised to ``\\n``. Trailing whitespace is left alone —
    it is occasionally meaningful in configuration output.
    """
    # Normalise CRLF first so lines can be handled individually below.
    text = text.replace("\r\n", "\n")

    if _LINE_EDIT_RE.search(text):
        text = "\n".join(apply_line_edits(line) for line in text.split("\n"))

    text = strip_ansi(text)
    text = apply_backspace(text)

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
