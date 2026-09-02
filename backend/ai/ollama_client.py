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
from backend.ai import turns
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


async def stream_response(
    user_message: str,
    context_block: str,
    model: str | None = None,
    system_prompt: str | None = None,
    history: list[dict] | None = None,
) -> AsyncIterator:
    """
    Stream an Ollama response token by token.
    Yields text chunks as they arrive.
    Raises if Ollama is unreachable or returns an error.
    """
    full_user_message = (
        f"{context_block}\n\n=== ENGINEER'S QUESTION ===\n{user_message}"
    )

    host = get_effective("ollama_host", OLLAMA_HOST)
    url = f"{host.rstrip('/')}/api/chat"
    payload = {
        "model": model or OLLAMA_MODEL,
        "stream": True,
        "messages": turns.openai_messages(
            system_prompt or SYSTEM_PROMPT, history, full_user_message),
        # The same settings the cloud clients honour, in Ollama's spelling.
        # Without these, Temperature and Maximum response length in Stockton
        # applied to every provider except the local one (#417).
        "options": _options(),
        "keep_alive": f"{int(advanced('ai.ollama_keep_alive'))}m",
    }

    async with httpx.AsyncClient(timeout=advanced("ai.request_timeout")) as client:
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
                    chunk = event.get("message", {}).get("content", "")
                    if chunk:
                        yield chunk
                    if event.get("done"):
                        # Ollama's counts: what it read and what it wrote.
                        if "prompt_eval_count" in event or "eval_count" in event:
                            yield {"usage": {
                                "provider": "ollama",
                                "input":    event.get("prompt_eval_count", 0),
                                "output":   event.get("eval_count", 0),
                                "cache_read": 0,
                            }}
                        break
                except json.JSONDecodeError:
                    continue
