"""
openai_client.py — Streaming OpenAI client for ShellMate.
Uses the standard OpenAI chat/completions SSE format.
"""
import json
import logging
from collections.abc import AsyncIterator

import httpx

from backend.advanced import get as advanced

from backend.config import OPENAI_API_KEY, OPENAI_MODEL
from backend.settings_store import get_effective
from backend.ai.prompts import SYSTEM_PROMPT

logger = logging.getLogger(__name__)

OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"


async def stream_response(
    user_message: str,
    context_block: str,
    model: str | None = None,
    system_prompt: str | None = None,
) -> AsyncIterator[str]:
    """
    Stream an OpenAI response token by token.
    Yields text chunks as they arrive.
    """
    api_key = get_effective("openai_api_key", OPENAI_API_KEY)
    if not api_key:
        raise ValueError("OpenAI API key is not set. Configure it in Settings or .env.")

    full_user_message = (
        f"{context_block}\n\n=== ENGINEER'S QUESTION ===\n{user_message}"
    )

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type":  "application/json",
    }

    payload = {
        "model":      model or OPENAI_MODEL,
        "stream":     True,
        "max_tokens": advanced("ai.max_tokens"),
        "temperature": advanced("ai.temperature"),
        "messages": [
            {"role": "system", "content": system_prompt or SYSTEM_PROMPT},
            {"role": "user",   "content": full_user_message},
        ],
    }

    async with httpx.AsyncClient(timeout=advanced("ai.request_timeout")) as client:
        async with client.stream(
            "POST", OPENAI_API_URL, headers=headers, json=payload
        ) as resp:
            if resp.status_code != 200:
                body = await resp.aread()
                raise ValueError(
                    f"OpenAI API error {resp.status_code}: {body.decode()[:400]}"
                )

            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data = line[6:]
                if data == "[DONE]":
                    break
                try:
                    event = json.loads(data)
                    chunk = (
                        event.get("choices", [{}])[0]
                        .get("delta", {})
                        .get("content", "")
                    )
                    if chunk:
                        yield chunk
                except json.JSONDecodeError:
                    continue
