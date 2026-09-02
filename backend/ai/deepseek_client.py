"""
deepseek_client.py — Streaming DeepSeek client for ShellMate.
DeepSeek exposes an OpenAI-compatible API at api.deepseek.com.
"""
import json
import logging
from collections.abc import AsyncIterator

import httpx

from backend.advanced import get as advanced

from backend.config import DEEPSEEK_API_KEY, DEEPSEEK_MODEL
from backend.settings_store import get_effective
from backend.ai import turns
from backend.ai.prompts import SYSTEM_PROMPT

logger = logging.getLogger(__name__)

DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"


async def stream_response(
    user_message: str,
    context_block: str,
    model: str | None = None,
    system_prompt: str | None = None,
    history: list[dict] | None = None,
) -> AsyncIterator:
    """
    Stream a DeepSeek response token by token.
    Yields text chunks as they arrive.
    """
    api_key = get_effective("deepseek_api_key", DEEPSEEK_API_KEY)
    if not api_key:
        raise ValueError("DeepSeek API key is not set. Configure it in Settings or .env.")

    full_user_message = (
        f"{context_block}\n\n=== ENGINEER'S QUESTION ===\n{user_message}"
    )

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type":  "application/json",
    }

    payload = {
        "model":      model or DEEPSEEK_MODEL,
        "stream":     True,
        "max_tokens": advanced("ai.max_tokens"),
        "temperature": advanced("ai.temperature"),
        "messages": turns.openai_messages(
            system_prompt or SYSTEM_PROMPT, history, full_user_message),
        # The final chunk then carries the token counts (#416).
        "stream_options": {"include_usage": True},
    }
    usage: dict = {}

    async with httpx.AsyncClient(timeout=advanced("ai.request_timeout")) as client:
        async with client.stream(
            "POST", DEEPSEEK_API_URL, headers=headers, json=payload
        ) as resp:
            if resp.status_code != 200:
                body = await resp.aread()
                raise ValueError(
                    f"DeepSeek API error {resp.status_code}: {body.decode()[:400]}"
                )

            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data = line[6:]
                if data == "[DONE]":
                    break
                try:
                    event = json.loads(data)
                    if event.get("usage"):
                        u = event["usage"]
                        details = u.get("prompt_tokens_details") or {}
                        usage.update({
                            "input":      u.get("prompt_tokens", 0),
                            "output":     u.get("completion_tokens", 0),
                            # OpenAI reports cached prompt tokens in a
                            # sub-object; DeepSeek at the top level.
                            "cache_read": details.get("cached_tokens",
                                                      u.get("prompt_cache_hit_tokens", 0)),
                        })
                    choices = event.get("choices") or [{}]
                    chunk = (choices[0].get("delta") or {}).get("content", "")
                    if chunk:
                        yield chunk
                except json.JSONDecodeError:
                    continue
    if usage:
        usage["provider"] = "deepseek"
        yield {"usage": usage}
