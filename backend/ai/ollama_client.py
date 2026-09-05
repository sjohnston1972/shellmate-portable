"""
ollama_client.py — Streaming Ollama API client for ShellMate.
Connects to a local Ollama instance and streams responses.
"""
import json
import logging
from collections.abc import AsyncIterator

import httpx

from backend.advanced import get as advanced

from backend.config import OLLAMA_HOST, OLLAMA_MODEL
from backend.settings_store import get_effective
from backend.ai import http, toolloop
from backend.ai import tools as tool_registry, turns
from backend.ai.prompts import SYSTEM_PROMPT

logger = logging.getLogger(__name__)


def _options() -> dict:
    """
    Ollama's per-request model options.

    `num_predict` is Ollama's name for the reply ceiling and `num_ctx` the
    window the model reads. The window defaults to 2048 in Ollama itself,
    which a terminal buffer overruns silently — the prompt is truncated from
    the front and the model answers about output it never saw.
    """
    options = {
        "temperature": advanced("ai.temperature"),
        "num_predict": advanced("ai.max_tokens"),
    }
    num_ctx = int(advanced("ai.ollama_num_ctx"))
    if num_ctx > 0:
        options["num_ctx"] = num_ctx
    return options


def _usage(event: dict, num_ctx: int) -> dict:
    """
    Ollama's done event, read as the usage dict every provider returns (#555).

    The first four keys are the contract the chat panel and the router share
    across providers and must not move. The two additions are what only a
    local model can tell you:

    - **tokens_per_second**, from `eval_duration` — nanoseconds spent
      generating — which Ollama has always sent and this client always threw
      away. It is the one number that answers "is this model too big for
      this machine?", and there is nowhere else to get it: a local run has
      no bill and no dashboard.
    - **context_full**, when the prompt reached the window. Ollama does not
      refuse an over-long prompt; it truncates it from the front and answers
      confidently about output it never read. Silence here is the bug.

    *num_ctx* is what this request actually asked for, so a window changed
    in Stockton mid-session cannot be reported against the old figure.
    A zero means the request left Ollama's own default in force — the limit
    is then whatever the model was built with, which the response does not
    say, so nothing is claimed rather than a number invented.
    """
    output = int(event.get("eval_count", 0) or 0)
    prompt = int(event.get("prompt_eval_count", 0) or 0)
    usage = {
        "provider": "ollama",
        "input":    prompt,
        "output":   output,
        "cache_read": 0,
    }
    # Guarded rather than assumed: a cached prompt, a cancelled generation or
    # an older Ollama can all leave the duration absent or zero, and dividing
    # by it would replace a missing figure with a crash.
    elapsed = int(event.get("eval_duration", 0) or 0)
    if elapsed > 0:
        usage["tokens_per_second"] = round(output / (elapsed / 1_000_000_000), 1)
    if num_ctx > 0 and prompt >= num_ctx:
        usage["context_full"] = True
        usage["context_limit"] = num_ctx
    return usage


def _arguments_as_text(arguments) -> str:
    """
    Ollama returns tool arguments as an object; the collector expects the
    JSON string every other provider sends. Normalised here rather than
    branching in the collector, so there is one accumulation to be right.
    """
    if isinstance(arguments, str):
        return arguments
    try:
        return json.dumps(arguments or {})
    except (TypeError, ValueError):                       # pragma: no cover
        return "{}"


async def stream_response(
    user_message: str,
    context_block: str,
    model: str | None = None,
    system_prompt: str | None = None,
    history: list[dict] | None = None,
    tools: list[dict] | None = None,
    prior: list[dict] | None = None,
    attachment: str = "",
) -> AsyncIterator:
    """
    Stream an Ollama response token by token.
    Yields text chunks as they arrive.
    Raises if Ollama is unreachable or returns an error.
    """
    full_user_message = turns.user_content(context_block, user_message,
                                          attachment)

    host = get_effective("ollama_host", OLLAMA_HOST)
    url = f"{host.rstrip('/')}/api/chat"
    messages = turns.openai_messages(
        system_prompt or SYSTEM_PROMPT, history, full_user_message)
    if prior:
        messages = messages[:-1] + list(prior) + messages[-1:]
    payload = {
        "model": model or OLLAMA_MODEL,
        "stream": True,
        "messages": messages,
        # The same settings the cloud clients honour, in Ollama's spelling.
        # Without these, Temperature and Maximum response length in Stockton
        # applied to every provider except the local one (#417).
        "options": _options(),
        "keep_alive": f"{int(advanced('ai.ollama_keep_alive'))}m",
    }
    # Ollama's tool support is per model and per build, so this is only
    # sent where something has already established that it works (#560).
    # A model that ignores it emits a suggestion tag instead, which is the
    # path every model still has.
    if tools:
        payload["tools"] = tools
    collector = toolloop.OpenAICollector()

    client = http.shared(__name__.rsplit(".", 1)[-1], advanced("ai.request_timeout"))   # reused (#503)
    if True:
        async with client.stream("POST", url, json=payload) as resp:
            if resp.status_code != 200:
                body = await resp.aread()
                raise ValueError(
                    f"Ollama error {resp.status_code}: {body.decode()}"
                )

            async for line in resp.aiter_lines():
                if not line:
                    continue
                try:
                    event = json.loads(line)
                    # Ollama reports a failure after the 200 — the model
                    # could not load, the runner died — as an error line
                    # with no message and no done; read as text it was an
                    # empty reply and a clean finish (#500).
                    if event.get("error"):
                        raise ValueError(f"Ollama error: {str(event['error'])[:400]}")
                    message = event.get("message") or {}
                    chunk = message.get("content", "")
                    if chunk:
                        yield chunk
                    # Ollama sends whole tool calls rather than fragments,
                    # but through the same collector so the shapes cannot
                    # drift apart.
                    if message.get("tool_calls"):
                        collector.delta([
                            {"index": i, "id": call.get("id") or f"ollama-{i}",
                             "function": {
                                 "name": (call.get("function") or {}).get("name", ""),
                                 "arguments": _arguments_as_text(
                                     (call.get("function") or {}).get("arguments")),
                             }}
                            for i, call in enumerate(message["tool_calls"])])
                    if event.get("done"):
                        calls = collector.calls()
                        if calls:
                            tool_registry.remember_support(
                                "ollama", payload["model"])
                            yield {"tool_calls": calls}
                        # Ollama's counts: what it read, what it wrote, how
                        # fast, and whether it read all of what it was sent.
                        if "prompt_eval_count" in event or "eval_count" in event:
                            yield {"usage": _usage(
                                event, int(payload["options"].get("num_ctx", 0)))}
                        break
                except json.JSONDecodeError:
                    continue
