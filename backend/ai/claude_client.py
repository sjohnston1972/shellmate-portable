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
#: The fallback whenever no explicit model is passed — anything not coming
#: from the chat picker. A model id the API no longer serves comes back as a
#: 404, so this being current matters more than it looks.
MODEL = "claude-sonnet-5"



def _explain_api_error(status: int, body: bytes, model: str) -> str:
    """
    Turn an API failure into something a network engineer can act on.

    The raw JSON went straight into a chat bubble, so a retired model id
    surfaced as a wall of provider JSON with the actionable part — that the
    model does not exist — buried in the middle of it. The model id is
    hardcoded as a fallback for anything not coming from the chat picker, so
    it going stale is a matter of when.
    """
    import json as _json

    detail = ""
    try:
        parsed = _json.loads(body.decode("utf-8", errors="replace"))
        detail = (parsed.get("error") or {}).get("message", "")
    except Exception:
        detail = body.decode("utf-8", errors="replace")[:300]

    if status == 404 or "model" in detail.lower():
        return (f"Claude does not recognise the model '{model}'. It has "
                f"probably been retired — pick another in the model list "
                f"beside the chat box.")
    if status == 401:
        return ("Claude rejected the API key. Check it under Settings → AI "
                "Providers.")
    if status == 429:
        return "Claude is rate-limiting this key. Wait a moment and try again."
    if status >= 500:
        return f"Claude returned a server error ({status}). Not something at this end."

    return f"Claude API error {status}: {detail or 'no detail given'}"


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
                raise ValueError(_explain_api_error(resp.status_code, body,
                                                    payload.get("model", "")))

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
