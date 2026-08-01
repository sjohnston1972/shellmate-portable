"""
claude_client.py — Streaming Claude API client for ShellMate.
Uses httpx to stream responses token by token.
"""
import json
import logging
from collections.abc import AsyncIterator

import httpx

from backend.advanced import get as advanced

from backend.config import ANTHROPIC_API_KEY
from backend.settings_store import get_effective
from backend.ai.prompts import SYSTEM_PROMPT

logger = logging.getLogger(__name__)

CLAUDE_API_URL = "https://api.anthropic.com/v1/messages"
MODEL = "claude-sonnet-4-6"


async def stream_response(
    user_message: str,
    context_block: str,
    model: str | None = None,
    system_prompt: str | None = None,
) -> AsyncIterator[str]:
    """
    Stream a Claude API response token by token.
    Yields text chunks as they arrive.
    Raises on API or auth errors.
    """
    api_key = get_effective("anthropic_api_key", ANTHROPIC_API_KEY)
    if not api_key:
        raise ValueError("Anthropic API key is not set. Configure it in Settings or .env.")

    full_user_message = (
        f"{context_block}\n\n=== ENGINEER'S QUESTION ===\n{user_message}"
    )

    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }

    payload = {
        "model": model or MODEL,
        "max_tokens": advanced("ai.max_tokens"),
        "temperature": advanced("ai.temperature"),
        "system": system_prompt or SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": full_user_message}],
        "stream": True,
    }

    async with httpx.AsyncClient(timeout=advanced("ai.request_timeout")) as client:
        async with client.stream(
            "POST", CLAUDE_API_URL, headers=headers, json=payload
        ) as resp:
            if resp.status_code != 200:
                body = await resp.aread()
                raise ValueError(
                    f"Claude API error {resp.status_code}: {body.decode()}"
                )

            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data = line[6:]
                if data == "[DONE]":
                    break
                try:
                    event = json.loads(data)
                    if event.get("type") == "content_block_delta":
                        delta = event.get("delta", {})
                        if delta.get("type") == "text_delta":
                            yield delta.get("text", "")
                except json.JSONDecodeError:
                    continue
