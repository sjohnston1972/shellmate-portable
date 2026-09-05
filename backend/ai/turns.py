"""
turns.py — A conversation, shaped for each provider.

Every request used to carry exactly one user message (#402): the context
block and the question, nothing before it. "What about the other
interface?" started from nothing, because the model had never seen the
first question. The browser already kept the transcript for the Jira export;
now the recent turns travel with each request and this module turns them
into what each API wants.

Two providers' shapes, one normalisation:

**Anthropic** takes the system prompt separately and wants strictly
alternating user/assistant messages. It also caches a prefix of the request
when asked (#416): the system prompt and the earlier turns are the same from
one request to the next, so a cache breakpoint on the last earlier turn
means each request re-reads the conversation from cache and pays full price
only for the fresh context block and the new question.

**OpenAI-compatible** (OpenAI, xAI, DeepSeek, Ollama) takes the system prompt
as the first message and is relaxed about alternation, but the same
normalised history is used so the model sees the same conversation
whichever provider answers.
"""

from backend.advanced import get as advanced

#: What the browser calls the two sides, mapped to what the APIs call them.
_ROLES = {"user": "user", "ai": "assistant", "assistant": "assistant"}


def normalise(history: list | None, max_turns: int | None = None) -> list[dict]:
    """
    Earlier turns as ``[{"role": "user"|"assistant", "content": str}, ...]``.

    - Unknown roles and empty text are dropped.
    - Consecutive messages from one side are merged: an auto-analysis reply
      follows an ordinary reply with no question between them, and Anthropic
      refuses two assistant turns in a row.
    - A leading assistant turn is dropped, so the conversation starts with
      the user as the APIs require.
    - At most ``max_turns`` exchanges are kept (a turn is a user message
      and the reply to it). Defaults to the ``ai.history_turns`` setting;
      zero means no memory at all.
    - Trimming is done in blocks, not one turn at a time (#498). A cached
      prefix is only worth having if it is the same on the next request,
      and dropping the oldest turn on every request changed it on every
      request: each one paid the cache-write premium on the history and
      read nothing back. So once the conversation is over the limit it is
      cut back to ``max_turns - 4`` and allowed to grow again, which keeps
      the prefix stable for four requests at a time. Below eight turns
      the block is half the limit; at one, there is nothing to trade.
    """
    if max_turns is None:
        max_turns = int(advanced("ai.history_turns"))
    if max_turns <= 0 or not history:
        return []

    merged: list[dict] = []
    for entry in history:
        if not isinstance(entry, dict):
            continue
        role = _ROLES.get(str(entry.get("role", "")).lower())
        text = str(entry.get("text") or entry.get("content") or "").strip()
        if not role or not text:
            continue
        if merged and merged[-1]["role"] == role:
            merged[-1]["content"] += "\n\n" + text
        else:
            merged.append({"role": role, "content": text})

    while merged and merged[0]["role"] != "user":
        merged.pop(0)
    # The last message must be the assistant's — the new question follows.
    # A trailing user turn with no reply (a dropped connection, say) is
    # folded into nothing rather than making two user turns in a row.
    if merged and merged[-1]["role"] == "user":
        merged.pop()

    # Keep whole turns from the end — and when over the limit, fewer than
    # the limit, so the next few requests share a prefix.
    if len(merged) > max_turns * 2:
        keep = max_turns - trim_block(max_turns)
        merged = merged[-(keep * 2):]
        while merged and merged[0]["role"] != "user":
            merged.pop(0)
    return merged


def trim_block(max_turns: int) -> int:
    """How many turns beyond the limit to drop at once: four, or fewer for a small limit."""
    return min(4, max_turns // 2)


#: What an attachment is called in the prompt, per kind (#551). Named
#: rather than lumped under one heading because the model should treat
#: them differently: a selection is what the engineer is pointing at, a
#: record is output already on screen, and a paste came from somewhere
#: else entirely and may be about a device this session is not on.
ATTACHMENT_HEADINGS = {
    "selection": "THE LINES THE ENGINEER IS POINTING AT",
    "record":    "THE OUTPUT OF THE LAST COMMAND THEY RAN",
    "paste":     "TEXT THE ENGINEER PASTED IN (it may be from another device, or from a file — do not assume it is this session)",
}


def attachment_block(kind: str, text: str) -> str:
    """
    One attachment, under its own heading (#551).

    Mid-outage the engineer wants to say "these six lines", not ask a
    question over a two-hundred-line window and hope. The heading
    matters as much as the text: without it the lines are indistinguish-
    able from the terminal output already in the block, and the model
    has no way to know they are what was pointed at.
    """
    body = (text or "").strip()
    if not body:
        return ""
    heading = ATTACHMENT_HEADINGS.get(kind,
                                      ATTACHMENT_HEADINGS["selection"])
    return f"=== {heading} ===\n{body}"


def user_content(context_block: str, user_message: str,
                 attachment: str = "") -> str:
    """
    The user message as the model receives it: the context block, then the
    question under its own heading.

    With no context block it is the message alone. The session summary
    sends its whole task as the message, and a "question" heading over a
    note-writing task was the wrong framing for it (#502).
    """
    parts = [p for p in (context_block or "").strip().split("\n\n") if p]
    if attachment.strip():
        # After the context and before the question: the model reads
        # the session, then what it is being pointed at, then what is
        # being asked about it — which is the order somebody says it in.
        parts.append(attachment.strip())
    if not parts:
        return user_message
    body = "\n\n".join(parts)
    return f"{body}\n\n=== ENGINEER'S QUESTION ===\n{user_message}"


def openai_messages(system: str, history: list | None, user: str) -> list[dict]:
    """The message list for an OpenAI-shaped chat completion."""
    return ([{"role": "system", "content": system}]
            + normalise(history)
            + [{"role": "user", "content": user}])


def anthropic_request(system: str, history: list | None, user: str) -> tuple[list, list]:
    """
    ``(system, messages)`` for the Anthropic Messages API.

    With ``ai.prompt_caching`` on, the system prompt carries a cache
    breakpoint and so does the last earlier turn: everything up to and
    including it is the stable prefix, and the fresh context block after it
    is what changes each time.

    Two things make the prefix worth caching (#498). The system text is the
    persona plus the steady part of the context (the router appends
    :func:`prompts.build_system_preamble`), which takes it past the
    provider's minimum cacheable length; and the history is trimmed in
    blocks by :func:`normalise`, so the breakpoint on the last earlier turn
    sits in the same place for several requests running.
    """
    caching = bool(advanced("ai.prompt_caching"))
    marker = {"type": "ephemeral"}

    system_blocks: list = [{"type": "text", "text": system}]
    if caching:
        system_blocks[0]["cache_control"] = marker

    messages = normalise(history)
    if caching and messages:
        last = messages[-1]
        messages[-1] = {
            "role": last["role"],
            "content": [{"type": "text", "text": last["content"], "cache_control": marker}],
        }
    messages.append({"role": "user", "content": user})
    return system_blocks, messages


# ---------------------------------------------------------------------------
# Tool turns (#560)
#
# A tool exchange is three messages, not one: the assistant's request, the
# result, and then whatever the assistant says having seen it. The two
# provider families disagree about how each of those is shaped, and about
# which role the result belongs to — Anthropic sends it as a *user* message
# containing a tool_result block, while the OpenAI shape gives it a role of
# its own. Both are generated here rather than in the clients, so a change
# to the conversation shape lands in one place and stays symmetrical.
# ---------------------------------------------------------------------------


def anthropic_tool_call(text: str, calls: list[dict]) -> dict:
    """
    The assistant turn that asked for something.

    Anthropic requires the assistant's own text to travel with the tool_use
    blocks in one message. Dropping it loses the model's reasoning for the
    request, which is the half a person reads before approving.
    """
    content: list[dict] = []
    if (text or "").strip():
        content.append({"type": "text", "text": text})
    for call in calls:
        content.append({
            "type": "tool_use",
            "id": call["id"],
            "name": call["name"],
            "input": call.get("arguments") or {},
        })
    return {"role": "assistant", "content": content}


def anthropic_tool_results(results: list[dict]) -> dict:
    """
    The results, as the *user* turn Anthropic expects them to be.

    Every result for one assistant turn goes in a single message: sending
    them one message each produces two user turns in a row, which the API
    refuses.
    """
    return {
        "role": "user",
        "content": [{
            "type": "tool_result",
            "tool_use_id": result["id"],
            "content": str(result.get("content", "")),
            **({"is_error": True} if result.get("is_error") else {}),
        } for result in results],
    }


def openai_tool_call(text: str, calls: list[dict]) -> dict:
    """The assistant turn that asked, in the OpenAI shape."""
    import json as _json

    return {
        "role": "assistant",
        # None rather than "": some implementations reject an empty string
        # alongside tool_calls, and the field is optional when there is no
        # prose to carry.
        "content": (text or None) if (text or "").strip() else None,
        "tool_calls": [{
            "id": call["id"],
            "type": "function",
            "function": {
                "name": call["name"],
                "arguments": _json.dumps(call.get("arguments") or {}),
            },
        } for call in calls],
    }


def openai_tool_results(results: list[dict]) -> list[dict]:
    """
    The results, one message each — the opposite of Anthropic's rule.

    A list rather than a single message, so the caller extends the
    conversation with it either way and the difference stays here.
    """
    return [{
        "role": "tool",
        "tool_call_id": result["id"],
        "content": str(result.get("content", "")),
    } for result in results]


def with_tool_exchange(messages: list[dict], shape: str, text: str,
                       calls: list[dict], results: list[dict]) -> list[dict]:
    """
    Append one complete request-and-answer to a conversation.

    Args:
        messages: The conversation so far, which is extended in place-safe
                  fashion (a new list is returned).
        shape:    "anthropic" or "openai".
        text:     Anything the assistant said alongside its request.
        calls:    ``[{"id", "name", "arguments"}]``.
        results:  ``[{"id", "content", "is_error"}]``.
    """
    out = list(messages)
    if shape == "anthropic":
        out.append(anthropic_tool_call(text, calls))
        out.append(anthropic_tool_results(results))
    else:
        out.append(openai_tool_call(text, calls))
        out.extend(openai_tool_results(results))
    return out
