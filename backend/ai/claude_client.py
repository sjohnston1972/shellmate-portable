"""
claude_client.py — Streaming Claude API client for ShellMate.
Uses httpx to stream responses token by token.
"""
import json
import re
import logging
from collections.abc import AsyncIterator

import httpx

from backend.advanced import get as advanced

from backend.config import ANTHROPIC_API_KEY
from backend.settings_store import get_effective
from backend.ai import http, turns
from backend.ai.prompts import SYSTEM_PROMPT

logger = logging.getLogger(__name__)

CLAUDE_API_URL = "https://api.anthropic.com/v1/messages"
#: The last-resort fallback when no explicit model is passed and nothing has
#: ever been discovered. A model id the API no longer serves comes back as a
#: 404, so a hardcoded id going stale is a matter of when — which is why
#: _fallback_model() prefers what the provider actually offered last time.
MODEL = "claude-sonnet-5"


def _fallback_model() -> str:
    """
    The model to use when the caller did not name one.

    Resolved from the last successful discovery rather than the constant,
    because the constant is written once and retired on Anthropic's schedule,
    not ours (#230). Only with no discovery at all does the constant apply.
    """
    try:
        from backend.ai.providers import load_cached
        cached = (load_cached().get("anthropic") or {}).get("models") or []
        if cached:
            return cached[0]["id"]
    except Exception:
        pass
    return MODEL



#: Models that answered a `temperature` with a 400 this run. Learned, so the
#: second request to such a model does not pay the round trip again.
_NO_SAMPLING: set[str] = set()

#: Families that have removed sampling parameters. Anything else is sent the
#: temperature, and the reactive check above catches a family this misses.
_NO_SAMPLING_FAMILIES = re.compile(
    r"^claude-(fable|mythos|opus-5|opus-4-[789]|sonnet-5|sonnet-4-[789])")


def _accepts_sampling(model: str) -> bool:
    """Whether `temperature` may be sent to this model at all."""
    if model in _NO_SAMPLING:
        return False
    return _NO_SAMPLING_FAMILIES.match(model or "") is None


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

    error_type = ""
    try:
        error_type = str(((_json.loads(body.decode("utf-8", errors="replace"))
                           .get("error") or {}).get("type", "")))
    except Exception:
        pass
    # Only a genuine not-found is a retired model. Any 400 whose message
    # happened to contain the word "model" — "`temperature` is deprecated
    # for this model" — used to be reported as one, which sent people
    # picking through a model list that was fine (#433).
    if status == 404 or error_type == "not_found_error":
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
    history: list[dict] | None = None,
) -> AsyncIterator:
    """
    Stream a Claude API response token by token.
    Yields text chunks as they arrive.
    Raises on API or auth errors.
    """
    api_key = get_effective("anthropic_api_key", ANTHROPIC_API_KEY)
    if not api_key:
        raise ValueError("Anthropic API key is not set. Configure it in Settings or .env.")

    full_user_message = turns.user_content(context_block, user_message)

    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }

    # The system prompt and the earlier turns are the stable prefix, marked
    # cacheable (#416); the context block and the question come last.
    system_blocks, messages = turns.anthropic_request(
        system_prompt or SYSTEM_PROMPT, history, full_user_message)
    payload = {
        "model": model or _fallback_model(),
        "max_tokens": advanced("ai.max_tokens"),
        "system": system_blocks,
        "messages": messages,
        "stream": True,
    }
    usage: dict = {}
    # Sampling parameters were removed from the Claude 5 family and from
    # Opus 4.7 onwards: sending `temperature` is a 400, not a warning. It is
    # sent only to models known to accept it, and dropped and retried once
    # if a model turns out not to (#433).
    if _accepts_sampling(payload["model"]):
        payload["temperature"] = advanced("ai.temperature")

    client = http.shared(__name__.rsplit(".", 1)[-1], advanced("ai.request_timeout"))   # reused (#503)
    if True:
        for attempt in (1, 2):
            async with client.stream(
                "POST", CLAUDE_API_URL, headers=headers, json=payload
            ) as resp:
                if resp.status_code != 200:
                    body = await resp.aread()
                    if (attempt == 1 and resp.status_code == 400
                            and "temperature" in payload and b"temperature" in body):
                        _NO_SAMPLING.add(payload["model"])
                        payload.pop("temperature", None)
                        continue
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
                        kind = event.get("type")
                        if kind == "content_block_delta":
                            delta = event.get("delta", {})
                            if delta.get("type") == "text_delta":
                                yield delta.get("text", "")
                        elif kind == "message_start":
                            # Input-side counts arrive first, cache hits included.
                            u = (event.get("message") or {}).get("usage") or {}
                            usage.update({
                                "input":       u.get("input_tokens", 0),
                                "cache_read":  u.get("cache_read_input_tokens", 0),
                                "cache_write": u.get("cache_creation_input_tokens", 0),
                            })
                        elif kind == "message_delta":
                            u = event.get("usage") or {}
                            if "output_tokens" in u:
                                usage["output"] = u["output_tokens"]
                        elif kind == "error":
                            # An overloaded_error mid-stream arrives as an
                            # event on the 200, not as a status (#500).
                            err = event.get("error") or {}
                            raise ValueError(
                                f"Claude error: {err.get('message') or err.get('type') or 'unknown'}")
                    except json.JSONDecodeError:
                        continue
                break
    if usage:
        usage["provider"] = "anthropic"
        yield {"usage": usage}
