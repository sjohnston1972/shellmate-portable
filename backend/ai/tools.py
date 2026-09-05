"""
tools.py — What the assistant may ask ShellMate to do (#560).

Every provider has been driven text-only: the model is told to wrap a
suggestion in `[SUGGEST_CMD]` tags, three regexes repair the malformed ones,
and the result comes back on the *next* turn as prose — "The user just
ran…". That works, and it costs a turn per step, forces the whole context
to be pre-loaded in case the model wants it, and makes a multi-step
investigation a sequence of separate requests that each start over.

Native tool use replaces the tags where a model supports them. The model
asks, ShellMate answers, and the same turn continues.

**The approval gate does not move.** This is the rule the whole feature is
built around. `run_command` produces exactly what a `[SUGGEST_CMD]` tag
produces today — a command block a person clicks — and nothing reaches a
device until they do. The guardrail in `pipeline.py` still sees the line, a
dangerous command is still held, and `investigate_max_steps` still bounds
the loop. A tool call is a *request to be allowed*, not permission.

**Read-only tools need no approval, and must never touch the device.**
`get_parsed_output`, `get_drift` and `search_history` are answered from
what ShellMate already holds. That is what makes them safe to answer
without asking, and it is also the property most easily lost: a
"read-only" tool that opens a channel to go and look is a device
interaction nobody approved. The registry marks it, the executor enforces
it, and the tests assert it.

**Tags remain the fallback.** Ollama's tool support varies by model and by
build, and a model that ignores `tools` and emits a tag must still work.
Support is probed, never assumed, and a provider that does not answer is
treated as not supporting them.
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ToolSpec:
    """One thing the assistant may ask for."""

    name: str
    description: str
    #: JSON Schema `properties`, and which of them are required.
    properties: dict[str, dict]
    required: tuple[str, ...] = ()
    #: True when answering it cannot reach a device. Read-only tools are
    #: answered without asking; anything else raises a command block.
    read_only: bool = True

    def schema(self) -> dict:
        return {
            "type": "object",
            "properties": dict(self.properties),
            "required": list(self.required),
        }


#: The vocabulary. Deliberately small: each of these replaces something the
#: router currently pre-loads into every request whether the model wants it
#: or not, and a tool nobody calls is context spent on a menu.
TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="run_command",
        description=(
            "Ask to run one command on the device the engineer is looking at. "
            "This does NOT run it: it puts the command in front of the "
            "engineer, who approves or rejects it. Use it exactly as you "
            "would suggest a command, one at a time, and explain why first."
        ),
        properties={
            "command": {
                "type": "string",
                "description": "The command, exactly as it would be typed.",
            },
            "why": {
                "type": "string",
                "description": "One short line on what this will show.",
            },
        },
        required=("command",),
        read_only=False,
    ),
    ToolSpec(
        name="get_parsed_output",
        description=(
            "Return the rows ShellMate parsed from a command already run in "
            "this session, as structured data rather than screen text. Use "
            "for interface tables, neighbour lists and version output "
            "instead of re-reading the raw buffer. Answers only from what "
            "has already been run; it does not go to the device."
        ),
        properties={
            "command": {
                "type": "string",
                "description": "The command whose output you want, e.g. "
                               "'show ip interface brief'.",
            },
        },
        required=("command",),
    ),
    ToolSpec(
        name="get_drift",
        description=(
            "What changed in this device's configuration since the last "
            "stored capture, as a unified diff. Answers from ShellMate's "
            "own archive; it does not capture from the device."
        ),
        properties={},
    ),
    ToolSpec(
        name="search_history",
        description=(
            "Search every command ShellMate has recorded, across all "
            "sessions and devices, for text in the command or its output. "
            "Use to answer 'when did we last…' and 'has this been seen "
            "elsewhere'."
        ),
        properties={
            "query": {"type": "string", "description": "Free text to match."},
            "hostname": {
                "type": "string",
                "description": "Optional: restrict to one device.",
            },
        },
        required=("query",),
    ),
)

BY_NAME: dict[str, ToolSpec] = {tool.name: tool for tool in TOOLS}


def read_only_names() -> set[str]:
    return {tool.name for tool in TOOLS if tool.read_only}


def enabled() -> bool:
    """Whether native tool use is on at all."""
    from backend.advanced import get as advanced

    try:
        return bool(advanced("ai.native_tools"))
    except Exception:                                     # pragma: no cover
        return False


# ---------------------------------------------------------------------------
# The two payload shapes
#
# Anthropic and the OpenAI-shaped providers describe tools differently and
# name the pieces differently. Both are generated from the one registry
# above rather than written out twice — a tool added to one and not the
# other would be a model that can ask for something on Claude and not on
# xAI, with nothing saying why.
# ---------------------------------------------------------------------------

def for_anthropic(specs: tuple[ToolSpec, ...] = TOOLS) -> list[dict]:
    """`tools` for the Anthropic Messages API."""
    return [{"name": t.name, "description": t.description,
             "input_schema": t.schema()} for t in specs]


def for_openai(specs: tuple[ToolSpec, ...] = TOOLS) -> list[dict]:
    """`tools` for an OpenAI-shaped chat completion."""
    return [{"type": "function",
             "function": {"name": t.name, "description": t.description,
                          "parameters": t.schema()}} for t in specs]


# ---------------------------------------------------------------------------
# Which models can be asked
# ---------------------------------------------------------------------------

#: Models that answered a `tools` payload with a refusal this run, as
#: "backend:model". Learned rather than declared: a list of which builds of
#: which local models support tools would be wrong within a month, and
#: wrong in the direction of sending something that fails.
_NO_TOOLS: set[str] = set()

#: Backends whose hosted models all support tools. Ollama is deliberately
#: absent — support there is per model and per build.
_NATIVE = {"claude", "openai", "xai", "deepseek"}


def supports(backend: str, model: str) -> bool:
    """
    Whether to send `tools` to this backend and model.

    A refusal seen once this run is remembered, so the second request does
    not pay for it again — and the fallback to tags is silent to the user,
    because a model that cannot use tools is not an error, it is a model.
    """
    if not enabled():
        return False
    if f"{backend}:{model}" in _NO_TOOLS:
        return False
    if backend in _NATIVE:
        return True
    # Ollama and anything else: only where the model's own metadata says
    # so. Probed by the caller, which has the client; unknown means no.
    return f"{backend}:{model}" in _KNOWN_GOOD


#: Ollama models observed to accept a tools payload this run.
_KNOWN_GOOD: set[str] = set()


def remember_refusal(backend: str, model: str) -> None:
    """Record that this model will not take a tools payload."""
    key = f"{backend}:{model}"
    if key not in _NO_TOOLS:
        _NO_TOOLS.add(key)
        _KNOWN_GOOD.discard(key)
        logger.info("%s does not accept tool definitions; falling back to "
                    "suggestion tags for it", key)


def remember_support(backend: str, model: str) -> None:
    """Record that this model does take a tools payload."""
    key = f"{backend}:{model}"
    _KNOWN_GOOD.add(key)
    _NO_TOOLS.discard(key)


def looks_like_a_tools_refusal(body: bytes) -> bool:
    """
    Whether a 400 is about the tools payload rather than anything else.

    Matched on the word rather than on a provider's exact wording: the
    wordings differ and change, and the cost of a false positive is one
    silent fallback to tags, while the cost of a false negative is a
    request that fails on every retry.
    """
    text = (body or b"").decode("utf-8", errors="replace").lower()
    return "tool" in text and ("support" in text or "unknown" in text
                               or "not allowed" in text or "invalid" in text)
