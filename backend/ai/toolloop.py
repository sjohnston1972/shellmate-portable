"""
toolloop.py — Answering what the assistant asked for (#560).

Two halves. **Collecting** a tool call out of a provider's stream, which
arrives in fragments and in two different shapes. And **answering** the
read-only ones, which is done here without asking anybody, because they
reach nothing.

`run_command` is not answered here. It is handed to the browser as a
command block, exactly as a `[SUGGEST_CMD]` tag is today, and the person
approves it. The result comes back on the next request as a tool result,
so the model continues its own exchange rather than being told about it
afterwards in prose.

**The rule this module exists to keep: a read-only tool must never touch
the device.** It is easy to lose. `configs.drift_report` looks exactly
like the right thing to answer `get_drift` with — and it calls
`capture_config`, which opens a second channel to the switch. A model
asking "what changed?" would then be reaching a device that nobody
approved it reaching, at whatever moment it chose. So `get_drift` reads
the two most recent *stored* snapshots and diffs those; it answers about
the archive, and says so when the archive has nothing to say.

The same care applies to the other two. `get_parsed_output` reads the
records the session already collected; `search_history` reads SQLite.
Neither opens anything, and `test_toolloop.py` asserts it by making any
capture attempt fail loudly.
"""

import json
import logging
from typing import Any

from backend.ai import tools as tool_registry

logger = logging.getLogger(__name__)

#: How much of any one tool result is handed back to the model. A tool that
#: returns a whole running configuration spends the context the tool
#: existed to save — and a model given forty thousand tokens of output it
#: did not ask for behaves worse, not better.
MAX_RESULT_CHARS = 6000


class ToolError(Exception):
    """Something the model should be told, rather than a fault to raise."""


# ---------------------------------------------------------------------------
# Collecting a call out of a stream
#
# Anthropic streams a tool_use block: a start event naming it, then the
# arguments as partial JSON deltas. The OpenAI shape streams an array of
# tool_calls whose fragments arrive indexed, with the arguments as partial
# JSON too. Both end up as {"id", "name", "arguments"}; the accumulation is
# different enough to be worth two small classes rather than one branching
# one.
# ---------------------------------------------------------------------------

class AnthropicCollector:
    """Rebuilds tool calls from an Anthropic content-block stream."""

    def __init__(self) -> None:
        self._blocks: dict[int, dict] = {}

    def block_start(self, index: int, block: dict) -> None:
        if block.get("type") != "tool_use":
            return
        self._blocks[index] = {"id": block.get("id", ""),
                               "name": block.get("name", ""), "json": ""}

    def block_delta(self, index: int, delta: dict) -> None:
        if delta.get("type") != "input_json_delta":
            return
        block = self._blocks.get(index)
        if block is not None:
            block["json"] += delta.get("partial_json", "")

    def calls(self) -> list[dict]:
        return [_finish(b) for _, b in sorted(self._blocks.items())]


class OpenAICollector:
    """Rebuilds tool calls from an OpenAI-shaped delta stream."""

    def __init__(self) -> None:
        self._calls: dict[int, dict] = {}

    def delta(self, fragments: list[dict] | None) -> None:
        for fragment in fragments or []:
            index = fragment.get("index", 0)
            call = self._calls.setdefault(
                index, {"id": "", "name": "", "json": ""})
            if fragment.get("id"):
                call["id"] = fragment["id"]
            function = fragment.get("function") or {}
            if function.get("name"):
                call["name"] = function["name"]
            # Appended, never assigned: the arguments arrive in pieces and
            # the last piece is not the arguments.
            call["json"] += function.get("arguments", "") or ""

    def calls(self) -> list[dict]:
        return [_finish(c) for _, c in sorted(self._calls.items())]


def _finish(block: dict) -> dict:
    """
    One collected call, with its arguments parsed.

    Malformed JSON becomes an empty argument set rather than an exception:
    a model that streamed half an argument has made a mistake the model
    should be told about, and a traceback in the chat panel tells the
    engineer about it instead.
    """
    raw = (block.get("json") or "").strip()
    arguments: dict = {}
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                arguments = parsed
        except json.JSONDecodeError:
            logger.info("Tool %s sent arguments that are not JSON: %.120s",
                        block.get("name"), raw)
    return {"id": block.get("id") or "", "name": block.get("name") or "",
            "arguments": arguments}


def partition(calls: list[dict]) -> tuple[list[dict], list[dict]]:
    """
    Split into (answerable here, needs a person).

    An unknown tool name goes in the first list: it is answered with an
    error the model can read and correct, which is better than a silent
    drop that leaves it waiting for a result that never comes.
    """
    here: list[dict] = []
    ask: list[dict] = []
    for call in calls:
        spec = tool_registry.BY_NAME.get(call.get("name", ""))
        if spec is None or spec.read_only:
            here.append(call)
        else:
            ask.append(call)
    return here, ask


# ---------------------------------------------------------------------------
# Answering the read-only ones
# ---------------------------------------------------------------------------

def execute(call: dict, session: dict | None) -> dict:
    """
    Answer one read-only tool call.

    Returns a result in the shape both conversation builders take:
    ``{"id", "content", "is_error"}``. Never raises — a tool that fails
    tells the model why, and a model told why can say so or try something
    else, while an exception here ends the turn with a red banner.
    """
    name = call.get("name", "")
    arguments = call.get("arguments") or {}
    result = {"id": call.get("id", ""), "content": "", "is_error": False}

    spec = tool_registry.BY_NAME.get(name)
    if spec is None:
        result["content"] = (
            f"There is no tool called {name!r}. The tools available are: "
            + ", ".join(sorted(tool_registry.BY_NAME)) + ".")
        result["is_error"] = True
        return result
    if not spec.read_only:                                # pragma: no cover
        result["content"] = "That tool needs the engineer's approval."
        result["is_error"] = True
        return result

    try:
        if name == "get_parsed_output":
            text = _parsed_output(session, str(arguments.get("command", "")))
        elif name == "get_drift":
            text = _drift(session)
        elif name == "search_history":
            text = _search(str(arguments.get("query", "")),
                           str(arguments.get("hostname", "")))
        else:                                             # pragma: no cover
            text = f"{name} is not implemented."
    except ToolError as exc:
        result["content"] = str(exc)
        result["is_error"] = True
        return result
    except Exception as exc:                              # pragma: no cover
        logger.warning("Tool %s failed: %s", name, exc)
        result["content"] = f"That could not be answered: {exc}"
        result["is_error"] = True
        return result

    if len(text) > MAX_RESULT_CHARS:
        kept = text[:MAX_RESULT_CHARS]
        text = (kept + f"\n\n[... {len(text) - MAX_RESULT_CHARS:,} more "
                       "characters not included ...]")
    result["content"] = text or "Nothing was found."
    return result


def _parsed_output(session: dict | None, command: str) -> str:
    """
    The rows already parsed from a command run in this session.

    Reads `recent_records`, which the terminal loop fills as commands
    finish. Nothing is sent: a command that has not been run is not run
    now, it is reported as not run — the model can then ask for it through
    `run_command`, which is the path with a person on it.
    """
    if not session:
        raise ToolError("There is no active session to read output from.")
    records = session.get("recent_records") or []
    if not records:
        raise ToolError("No command output has been recorded in this session "
                        "yet.")
    if not command.strip():
        raise ToolError("Name the command whose output you want.")

    from backend.configs import session_platform
    from backend.session import parsed

    wanted = command.strip().lower()
    matches = [r for r in records
               if wanted in str(getattr(r, "command", "")).strip().lower()]
    if not matches:
        ran = ", ".join(sorted({str(getattr(r, "command", "")).strip()
                                for r in records})[:12])
        raise ToolError(
            f"{command!r} has not been run in this session. What has: {ran}. "
            "Ask to run it with run_command if you need it.")

    tables = parsed.tables_for(session_platform(session), matches[-3:])
    if not tables:
        raise ToolError(
            f"ShellMate has no template that parses {command!r} on this "
            "platform, so there are no rows. The raw output is in the "
            "context block.")
    return "\n\n".join(tables)


def _drift(session: dict | None) -> str:
    """
    What changed between the two most recent *stored* captures.

    Deliberately not `configs.drift_report`, which captures from the device
    first. That would make a read-only tool open a channel to a switch at a
    moment nobody chose — the one thing this whole distinction exists to
    prevent. This answers about the archive, and says so when the archive
    has nothing to say.
    """
    if not session:
        raise ToolError("There is no active session, so there is no device "
                        "to report on.")
    hostname = str(session.get("hostname") or "").strip()
    if not hostname:
        raise ToolError("This session's device has not been identified yet.")

    from backend.configs import diff_snapshots
    from backend.store import store as history

    rows = history.list_snapshots(hostname, limit=2)
    if not rows:
        raise ToolError(
            f"No configuration has ever been captured from {hostname}, so "
            "there is nothing to compare. Nothing was captured just now "
            "either — this reads the archive only.")
    if len(rows) < 2:
        return (f"Only one capture of {hostname} is stored, from "
                f"{rows[0].get('captured_at')}, so there is nothing to "
                "compare it with yet.")

    new = history.get_snapshot(rows[0].get("id"))
    old = history.get_snapshot(rows[1].get("id"))
    if not new or not old:
        raise ToolError("The stored captures could not be read.")

    diff = diff_snapshots(old, new)
    if not (diff.get("diff") or "").strip():
        return (f"The two most recent stored captures of {hostname} are "
                "identical.")
    return (f"Between the two most recent stored captures of {hostname}: "
            f"{diff.get('added', 0)} line(s) added, "
            f"{diff.get('removed', 0)} removed.\n\n{diff['diff']}")


def _search(query: str, hostname: str = "") -> str:
    """Commands recorded across every session. Reads SQLite, nothing else."""
    if not query.strip():
        raise ToolError("Give something to search for.")

    from backend.store import store as history

    hits = history.search(query=query, hostname=hostname, limit=20)
    if not hits:
        where = f" on {hostname}" if hostname else ""
        return f"Nothing matching {query!r} has been recorded{where}."

    lines = []
    for hit in hits:
        row = hit.as_dict() if hasattr(hit, "as_dict") else dict(hit)
        lines.append(
            f"{row.get('label') or row.get('hostname') or '?'}: "
            f"{row.get('command', '')}"
            + (f"  — {row.get('snippet', '')}" if row.get("snippet") else ""))
    return f"{len(hits)} match(es) for {query!r}:\n" + "\n".join(lines)


def answer_all(calls: list[dict], session: dict | None) -> list[dict]:
    """Answer every read-only call in a batch."""
    return [execute(call, session) for call in calls]
