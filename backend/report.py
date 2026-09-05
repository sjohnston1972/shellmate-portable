"""
report.py — A session, a diff or a change as a file somebody else can read.

The only report ShellMate could produce was a Jira ticket, which assumes
that everybody has Jira and that whoever needs the evidence is inside the
same organisation. Neither holds. A CAB pack goes to a change board, a
vendor case goes to a vendor, and a customer report goes to a customer —
and none of those are reachable by creating an issue in a project.

**Two formats, one document.** Markdown and HTML are rendered from the same
block list rather than by turning one into the other. Converting would mean
parsing Markdown that contains arbitrary device output, and device output is
full of angle brackets, ampersands, backticks and lines beginning with a
hash — every one of which is Markdown or HTML syntax. Rendering twice from a
structure that was never text means the escaping happens once, in the
renderer that needs it, and there is no parser to confuse. It also means the
two formats cannot drift into disagreeing about what the report contains.

**PDF is deliberately not generated here.** It needs a rendering engine, and
the browser already has one. The HTML carries print rules and is meant to be
printed to PDF, which also keeps the page count and margins under the
control of the person who has to hand the thing in.

**Everything from a device is redacted on the way in.** This is the #320 and
#463 lesson: a report is far more "handed to someone else" than a log file on
disk ever is, and a running configuration carries password hashes, pre-shared
keys and community strings. The masking is pattern matching and so reduces
exposure rather than guaranteeing absence — but it happens at every entry
point here rather than at some of them.
"""

import html
import logging
import re
import time
from datetime import datetime
from pathlib import Path

from backend.paths import reports_dir
from backend.session.outbound import redact_text

logger = logging.getLogger(__name__)

# How much of one command's output a report carries. A report is read by a
# person; forty thousand lines of `show tech` is not evidence, it is a place
# evidence could be. The cut is announced in the document rather than silent,
# because a truncated report that does not say so is worse than a long one.
MAX_OUTPUT_LINES = 400


# ---------------------------------------------------------------------------
# The block model
#
# A report is a list of these. Both renderers walk the same list, so a
# section added here appears in both formats or in neither — the failure
# mode where one export gained the AI summary and the other did not is not
# reachable from this shape.
# ---------------------------------------------------------------------------

def _heading(level: int, text: str) -> tuple:
    return ("heading", level, text)


def _para(text: str) -> tuple:
    return ("para", text)


def _meta(pairs: list[tuple[str, str]]) -> tuple:
    """A definition list — the who, what and when at the top of a report."""
    return ("meta", [(k, v) for k, v in pairs if v])


def _code(text: str) -> tuple:
    return ("code", text)


def _diff(text: str) -> tuple:
    """A unified diff, which the HTML renderer colours by line prefix."""
    return ("diff", text)


def _rule() -> tuple:
    return ("rule",)


# ---------------------------------------------------------------------------
# Shared pieces
# ---------------------------------------------------------------------------

def _when(stamp: float | None) -> str:
    """A timestamp as a person writes one, or "" when there isn't one."""
    if not stamp:
        return ""
    try:
        return datetime.fromtimestamp(float(stamp)).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, OSError, OverflowError):
        return str(stamp)


def _duration(session: dict) -> str:
    started, ended = session.get("started_at"), session.get("ended_at")
    if not started or not ended:
        return ""
    seconds = max(0, int(float(ended) - float(started)))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60}m"


def _clip(text: str) -> str:
    """
    Cut long output, and say so in the document rather than silently.

    A report that quietly drops the last three hundred lines of the output
    somebody is relying on is not a smaller report, it is a wrong one.
    """
    lines = text.splitlines()
    if len(lines) <= MAX_OUTPUT_LINES:
        return text
    kept = lines[:MAX_OUTPUT_LINES]
    dropped = len(lines) - MAX_OUTPUT_LINES
    kept.append("")
    kept.append(f"[... {dropped:,} more line(s) not included in this report ...]")
    return "\n".join(kept)


def _command_blocks(commands: list[dict]) -> list[tuple]:
    """The command list with its outputs — the body of every session report."""
    blocks: list[tuple] = []
    for index, record in enumerate(commands, start=1):
        command = redact_text((record.get("command") or "").strip())
        output = redact_text(record.get("output") or "")
        ran = _when(record.get("ran_at"))
        took = record.get("duration_ms") or 0

        subtitle = " · ".join(part for part in (ran, f"{took} ms" if took else "") if part)
        blocks.append(_heading(3, f"{index}. {command or '(no command)'}"))
        if subtitle:
            blocks.append(_para(subtitle))
        blocks.append(_code(_clip(output.rstrip()) or "(no output captured)"))
    return blocks


# ---------------------------------------------------------------------------
# The three documents
# ---------------------------------------------------------------------------

def session_report(session: dict, chat: list[dict] | None = None,
                   summary: str = "") -> tuple[str, list[tuple]]:
    """
    One session: what was connected to, what was typed, and what came back.

    Args:
        session: A store record from ``get_session`` — metadata, ``notes``
                 and ``commands``.
        chat:    Optional assistant conversation, ``[{role, text}]``.
        summary: Optional AI summary of the session.

    Returns:
        ``(title, blocks)``.
    """
    label = session.get("label") or session.get("hostname") or "session"
    title = f"Session report — {label}"

    blocks: list[tuple] = [
        _heading(1, title),
        _meta([
            ("Device", session.get("label") or ""),
            ("Host", session.get("hostname") or session.get("target") or ""),
            ("Connection", (session.get("connection_type") or "").upper()),
            ("User", session.get("username") or ""),
            ("Started", _when(session.get("started_at"))),
            ("Ended", _when(session.get("ended_at")) or "still open"),
            ("Duration", _duration(session)),
            ("Commands", str(len(session.get("commands") or []))),
        ]),
    ]

    notes = (session.get("notes") or "").strip()
    if notes:
        blocks += [_heading(2, "Notes"), _para(redact_text(notes))]

    if summary.strip():
        # Marked as the assistant's, not as fact. Somebody reading this in a
        # change record has to be able to tell which sentences a device said
        # and which a model wrote about what it said.
        blocks += [
            _heading(2, "Summary"),
            _para("Written by the ShellMate assistant from the session below."),
            _para(redact_text(summary.strip())),
        ]

    blocks.append(_rule())
    commands = session.get("commands") or []
    blocks.append(_heading(2, "Commands"))
    if commands:
        blocks += _command_blocks(commands)
    else:
        blocks.append(_para("No commands were recorded in this session."))

    if chat:
        blocks += [_rule(), _heading(2, "Assistant conversation")]
        for message in chat:
            text = (message.get("text") or "").strip()
            if not text:
                continue
            who = "You" if message.get("role") == "user" else "Assistant"
            blocks.append(_para(f"**{who}:** {redact_text(text)}"))

    return title, blocks


def diff_report(diff: dict, old: dict, new: dict) -> tuple[str, list[tuple]]:
    """
    What changed between two captured configurations.

    Args:
        diff: The ``diff_snapshots`` result — text plus counts.
        old:  The earlier snapshot row.
        new:  The later snapshot row.
    """
    hostname = new.get("hostname") or old.get("hostname") or "device"
    title = f"Configuration change — {hostname}"

    blocks: list[tuple] = [
        _heading(1, title),
        _meta([
            ("Device", hostname),
            ("From", _when(old.get("captured_at"))),
            ("To", _when(new.get("captured_at"))),
            ("Lines added", str(diff.get("added", 0))),
            ("Lines removed", str(diff.get("removed", 0))),
            ("Lines changed", str(diff.get("changed", 0))),
        ]),
        _rule(),
        _heading(2, "Differences"),
    ]

    text = (diff.get("diff") or "").strip()
    if text:
        blocks.append(_diff(redact_text(text)))
    else:
        blocks.append(_para("The two configurations are identical."))

    return title, blocks


def change_report(session: dict, before: dict | None, after: dict | None,
                  diff: dict | None = None, summary: str = "") -> tuple[str, list[tuple]]:
    """
    A change record: the configuration before, what was typed, and after.

    This is the document a change board asks for, and the reason the three
    parts are one report rather than three is that its whole value is the
    join — a diff with no commands beside it says what moved but not who
    moved it or why, and commands with no diff say what was attempted but
    not what took effect.
    """
    hostname = session.get("hostname") or session.get("label") or "device"
    title = f"Change record — {hostname}"

    blocks: list[tuple] = [
        _heading(1, title),
        _meta([
            ("Device", hostname),
            ("Operator", session.get("username") or ""),
            ("Started", _when(session.get("started_at"))),
            ("Ended", _when(session.get("ended_at")) or "still open"),
            ("Before", _when((before or {}).get("captured_at")) or "not captured"),
            ("After", _when((after or {}).get("captured_at")) or "not captured"),
            ("Lines changed", str((diff or {}).get("changed", 0)) if diff else ""),
        ]),
    ]

    notes = (session.get("notes") or "").strip()
    if notes:
        blocks += [_heading(2, "What this change was for"), _para(redact_text(notes))]

    if summary.strip():
        blocks += [
            _heading(2, "Summary"),
            _para("Written by the ShellMate assistant from the session below."),
            _para(redact_text(summary.strip())),
        ]

    blocks += [_rule(), _heading(2, "What changed")]
    if diff and (diff.get("diff") or "").strip():
        blocks.append(_diff(redact_text(diff["diff"].strip())))
    elif before and after:
        blocks.append(_para("The configuration is identical before and after."))
    else:
        # Stated plainly rather than left as an empty section. "No diff" and
        # "no snapshot to diff against" are different facts, and a change
        # board reading the first when the second is true is being misled.
        blocks.append(_para(
            "No before-and-after comparison is available: a configuration "
            "snapshot was not captured on both sides of this session."))

    blocks += [_rule(), _heading(2, "Commands typed")]
    commands = session.get("commands") or []
    if commands:
        blocks += _command_blocks(commands)
    else:
        blocks.append(_para("No commands were recorded in this session."))

    return title, blocks


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _fence(text: str) -> str:
    """
    A fence long enough to survive the content.

    Device output contains backticks — a Cisco banner, a shell prompt, any
    pasted snippet — and a three-backtick fence around content holding three
    backticks ends the block early, dumping the rest of the output into the
    document as prose. The fence is one longer than the longest run inside.
    """
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)


def to_markdown(blocks: list[tuple]) -> str:
    out: list[str] = []
    for block in blocks:
        kind = block[0]
        if kind == "heading":
            out.append(f"{'#' * block[1]} {block[2]}")
        elif kind == "para":
            out.append(block[1])
        elif kind == "meta":
            for key, value in block[1]:
                out.append(f"- **{key}:** {value}")
        elif kind == "code":
            fence = _fence(block[1])
            out.append(f"{fence}\n{block[1]}\n{fence}")
        elif kind == "diff":
            fence = _fence(block[1])
            out.append(f"{fence}diff\n{block[1]}\n{fence}")
        elif kind == "rule":
            out.append("---")
        out.append("")
    return "\n".join(out).strip() + "\n"


def _diff_html(text: str) -> str:
    """Colour a unified diff by line prefix, escaping every line first."""
    rows: list[str] = []
    for line in text.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            css = "df-file"
        elif line.startswith("@@"):
            css = "df-hunk"
        elif line.startswith("+"):
            css = "df-add"
        elif line.startswith("-"):
            css = "df-del"
        else:
            css = ""
        escaped = html.escape(line) or "&nbsp;"
        rows.append(f'<span class="{css}">{escaped}</span>' if css else f"<span>{escaped}</span>")
    return "\n".join(rows)


# Deliberately plain and light-on-white regardless of the reader's theme:
# this file is printed, e-mailed and pasted into other documents, and a page
# that arrives dark grey burns a toner cartridge and reads badly on paper.
_CSS = """
  :root { color-scheme: light; }
  body { margin: 0; padding: 2.5rem 2rem; background: #fff; color: #16161a;
         font: 15px/1.6 -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
         max-width: 62rem; }
  h1 { font-size: 1.75rem; margin: 0 0 1.25rem; letter-spacing: -0.01em; }
  h2 { font-size: 1.2rem; margin: 2rem 0 0.75rem;
       border-bottom: 1px solid #d8d8de; padding-bottom: 0.35rem; }
  h3 { font-size: 1rem; margin: 1.5rem 0 0.4rem; font-family: ui-monospace,
       "Cascadia Mono", Consolas, monospace; color: #33333c; }
  p { margin: 0 0 0.75rem; }
  hr { border: 0; border-top: 1px solid #d8d8de; margin: 2rem 0; }
  dl.meta { display: grid; grid-template-columns: max-content 1fr;
            gap: 0.3rem 1.25rem; margin: 0 0 1.5rem; }
  dl.meta dt { font-weight: 600; color: #55555f; }
  dl.meta dd { margin: 0; }
  pre { background: #f5f5f7; border: 1px solid #e2e2e8; border-radius: 6px;
        padding: 0.85rem 1rem; overflow-x: auto; white-space: pre;
        font: 12.5px/1.55 ui-monospace, "Cascadia Mono", Consolas, monospace; }
  pre.diff span { display: block; }
  .df-add  { background: #e3f7e6; color: #12551f; }
  .df-del  { background: #fdeaea; color: #7a1220; }
  .df-hunk { color: #5b5bd6; }
  .df-file { color: #55555f; font-weight: 600; }
  footer { margin-top: 3rem; padding-top: 1rem; border-top: 1px solid #d8d8de;
           color: #6b6b76; font-size: 12px; }
  @media print {
    body { padding: 0; max-width: none; font-size: 11pt; }
    h2, h3 { break-after: avoid; }
    pre { break-inside: avoid; border-color: #bbb; }
  }
"""


def to_html(title: str, blocks: list[tuple]) -> str:
    """
    The same document as one self-contained page, for print-to-PDF.

    No external reference of any kind: this is written to a folder, attached
    to a mail, and opened on a machine that may have no network at all. A
    stylesheet fetched from anywhere would render it as unstyled text
    exactly when it matters.
    """
    parts: list[str] = []
    for block in blocks:
        kind = block[0]
        if kind == "heading":
            level = min(6, max(1, block[1]))
            parts.append(f"<h{level}>{html.escape(block[2])}</h{level}>")
        elif kind == "para":
            parts.append(f"<p>{_inline_html(block[1])}</p>")
        elif kind == "meta":
            rows = "".join(
                f"<dt>{html.escape(k)}</dt><dd>{html.escape(v)}</dd>"
                for k, v in block[1]
            )
            parts.append(f'<dl class="meta">{rows}</dl>')
        elif kind == "code":
            parts.append(f"<pre>{html.escape(block[1])}</pre>")
        elif kind == "diff":
            parts.append(f'<pre class="diff">{_diff_html(block[1])}</pre>')
        elif kind == "rule":
            parts.append("<hr>")

    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return (
        "<!doctype html>\n"
        '<html lang="en">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{html.escape(title)}</title>\n"
        f"<style>{_CSS}</style>\n"
        "</head>\n<body>\n"
        + "\n".join(parts)
        + f"\n<footer>Exported from ShellMate Portable on {stamp}. "
          "Passwords and secrets are masked where ShellMate recognised them.</footer>\n"
        "</body>\n</html>\n"
    )


def _inline_html(text: str) -> str:
    """
    Escape a paragraph, then honour the one piece of markup we generate.

    ``**Bold:**`` labels are written by this module and nowhere else, so the
    substitution runs *after* escaping and can only ever match what we put
    there — device output containing asterisks is already inert by then.
    """
    escaped = html.escape(text)
    return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)


# ---------------------------------------------------------------------------
# Writing it out
# ---------------------------------------------------------------------------

def _slug(text: str) -> str:
    """A filename fragment: letters, digits and dashes, nothing else."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", (text or "").strip()).strip("-.")
    return (cleaned or "session")[:48]


def write(title: str, blocks: list[tuple], device: str, fmt: str = "md") -> Path:
    """
    Render and write the report, returning the path.

    Args:
        title:  Document title, used for the HTML ``<title>``.
        blocks: The block list from one of the three builders.
        device: Used in the filename.
        fmt:    "md" or "html".

    Raises:
        ValueError: ``fmt`` is neither "md" nor "html".
        OSError:    The reports folder could not be written to.
    """
    if fmt not in ("md", "html"):
        raise ValueError(f"Unknown report format: {fmt!r}")

    reports_dir().mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = reports_dir() / f"{_slug(device)}-{stamp}.{fmt}"

    text = to_html(title, blocks) if fmt == "html" else to_markdown(blocks)
    path.write_text(text, encoding="utf-8")
    logger.info("Wrote a report to %s (%s bytes)", path, len(text))
    return path
