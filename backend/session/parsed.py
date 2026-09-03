"""
parsed.py — Show output as rows, when a template exists for it (#404).

The model reads raw text. `show ip interface brief` on a 48-port switch is
fifty lines of columns the model has to re-parse on every question, and a
misaligned column is a wrong answer about which port is down. ntc-templates
has a TextFSM template for most of the show commands anyone types on the
platforms ShellMate knows, and parsing is local and instant.

What this does *not* do is replace the raw output. Rows go alongside it: the
template may be stale for a release, may cover only the first half of a
long command, or may not exist at all — and when it does not, this returns
None and nothing changes. An optional dependency: without ntc-templates
installed, every call returns None.

Platform ids are ShellMate's (`platforms.py`); the map below translates them
to ntc-templates' names. A platform not in the map has no templates.
"""

import logging

logger = logging.getLogger(__name__)

#: ShellMate platform id → ntc-templates platform name.
NTC_PLATFORMS = {
    "ios":    "cisco_ios",
    "nxos":   "cisco_nxos",
    "asa":    "cisco_asa",
    "junos":  "juniper_junos",
    "panos":  "paloalto_panos",
    "arista": "arista_eos",
    "linux":  "linux",
}

#: Rows beyond this are summarised as a count — a full routing table is not
#: something to hand a model row by row.
MAX_ROWS = 60

try:                                            # pragma: no cover - import guard
    from ntc_templates.parse import parse_output as _parse_output
    AVAILABLE = True
except Exception:                               # ImportError, or a broken install
    _parse_output = None
    AVAILABLE = False


def available() -> bool:
    return AVAILABLE


def parse(platform_id: str, command: str, output: str) -> list[dict] | None:
    """
    Rows for ``command``'s ``output`` on ``platform_id``, or None.

    None means "no template, or it did not match" — never an error. A
    command with a template that yields no rows (an empty ARP table, say) is
    a real answer and comes back as ``[]``.
    """
    if not AVAILABLE or not command or not output:
        return None
    platform = NTC_PLATFORMS.get((platform_id or "").lower())
    if not platform:
        return None
    try:
        rows = _parse_output(platform=platform, command=command.strip(), data=output)
    except Exception as exc:
        # ParsingException for "no template"; anything else is a template
        # that did not match this release's output. Both mean "no rows".
        logger.debug("No parse for %r on %s: %s", command, platform, exc)
        return None
    if not isinstance(rows, list):
        return None
    return [dict(r) for r in rows if isinstance(r, dict)]


def render(command: str, rows: list[dict], max_rows: int = MAX_ROWS) -> str:
    """
    A compact fixed-width table for the prompt.

    Columns are the template's field names; empty columns are dropped so a
    template with thirty optional fields does not produce thirty blanks per
    row. Long tables are cut with a note, because the raw output is there
    too and the model can ask for the rest.
    """
    if not rows:
        return f"--- Parsed: {command} — 0 rows ---"
    columns = [c for c in rows[0].keys()
               if any(str(r.get(c, "")).strip() for r in rows)]
    shown = rows[:max_rows]
    widths = {c: max(len(c), *(len(str(r.get(c, ""))) for r in shown)) for c in columns}
    lines = [f"--- Parsed: {command} — {len(rows)} row{'s' if len(rows) != 1 else ''} ---",
             "  " + "  ".join(c.ljust(widths[c]) for c in columns)]
    for r in shown:
        lines.append("  " + "  ".join(str(r.get(c, "")).ljust(widths[c]) for c in columns))
    if len(rows) > max_rows:
        lines.append(f"  … {len(rows) - max_rows} more rows not shown")
    return "\n".join(lines)


def table_for(platform_id: str, record) -> str | None:
    """
    The rendered table for one record, or None when it has no template.

    The output is parsed *after* redaction, through the same door as the
    raw text (#496). A template whose fields carry a secret — `show snmp
    community` has a column for the community string — would otherwise
    hand the model in a clean table exactly what redaction had masked out
    of the text beside it.

    The result is kept on the record, keyed by the platform and the
    redaction switch, because a chat message renders up to twelve records
    whose output has not changed since the last question, and a TextFSM
    parse is not free.
    """
    from backend.session import outbound

    key = ((platform_id or "").lower(), outbound.redaction_enabled())
    cached = getattr(record, "_parsed_table", None)
    if cached is not None and cached[0] == key:
        return cached[1]

    command = getattr(record, "command", "") or ""
    output = outbound.redact_text(getattr(record, "output", "") or "")
    rows = parse(platform_id, command, output)
    table = render(command, rows) if rows is not None else None
    try:
        record._parsed_table = (key, table)
    except Exception:                       # a record type that refuses attributes
        pass
    return table


def tables_for(platform_id: str, records, limit: int = 3) -> list[str]:
    """
    Rendered tables for the most recent records that parse, newest last.

    ``records`` are :class:`CommandRecord`-like objects (``.command`` and
    ``.output``). At most ``limit`` tables, so a session full of `show`
    commands does not turn every question into a data dump.
    """
    out: list[str] = []
    for record in reversed(list(records or [])):
        if len(out) >= limit:
            break
        table = table_for(platform_id, record)
        if table is not None:
            out.append(table)
    out.reverse()
    return out
