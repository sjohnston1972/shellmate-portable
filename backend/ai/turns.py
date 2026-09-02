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
    - Only the last ``max_turns`` exchanges are kept (a turn is a user
      message and the reply to it). Defaults to the ``ai.history_turns``
      setting; zero means no memory at all.
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

    # Keep whole turns from the end.
    limit = max_turns * 2
    if len(merged) > limit:
        merged = merged[-limit:]
        while merged and merged[0]["role"] != "user":
            merged.pop(0)
    return merged


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
