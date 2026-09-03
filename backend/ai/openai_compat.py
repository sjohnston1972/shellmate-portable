"""
openai_compat.py — The one streaming loop for the OpenAI-shaped providers.

OpenAI, xAI and DeepSeek speak the same chat/completions dialect: the same
message list, the same SSE framing, the same usage object at the end. The
three clients were three copies of the same loop, and a copy each is how
the OpenAI client came to send `max_tokens` and `temperature` to reasoning
models that reject both (#497) while the Claude client had already learned
not to (#433). One loop, one lesson.

What differs per provider is data — the URL, the label in an error, and
which model families have removed the sampling parameters — and that is
what :class:`Provider` carries. The lesson itself is learned at run time
too: a model that answers a parameter with a 400 is retried once without
it and remembered, so a family this file does not know about costs one
round trip, not every request.
"""
import json
import logging
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx

from backend.advanced import get as advanced
from backend.ai import turns
from backend.ai.prompts import SYSTEM_PROMPT

logger = logging.getLogger(__name__)

#: A pattern that matches nothing, for a provider with no known families.
NONE = re.compile(r"(?!)")


@dataclass(frozen=True)
class Provider:
    """What one OpenAI-shaped provider needs the loop to know."""

    name: str                  #: the usage dict's ``provider`` and the learned-set key
    label: str                 #: how an error names it: "OpenAI API error 400"
    url: str                   #: the chat/completions endpoint
    #: Model families that have removed the sampling parameters. Sent no
    #: temperature; the reactive check below catches a family this misses.
    reasoning: re.Pattern = NONE
    #: Whether the reasoning families want ``max_completion_tokens`` in
    #: place of ``max_tokens`` (OpenAI's o-series and GPT-5 do).
    completion_tokens: bool = False


#: Models that answered a `temperature` with a 400 this run, as
#: "provider:model". Learned, so the second request does not pay again.
_NO_SAMPLING: set[str] = set()

#: Models that answered `max_tokens` with a 400 this run and want
#: `max_completion_tokens` instead.
_COMPLETION_TOKENS: set[str] = set()


def _key(provider: Provider, model: str) -> str:
    return f"{provider.name}:{model}"


def accepts_sampling(provider: Provider, model: str) -> bool:
    """Whether `temperature` may be sent to this model at all."""
    if _key(provider, model) in _NO_SAMPLING:
        return False
    return provider.reasoning.match(model or "") is None


def length_parameter(provider: Provider, model: str) -> str:
    """The name of the reply-length parameter this model accepts."""
    if _key(provider, model) in _COMPLETION_TOKENS:
        return "max_completion_tokens"
    if provider.completion_tokens and provider.reasoning.match(model or ""):
        return "max_completion_tokens"
    return "max_tokens"


def build_payload(provider: Provider, model: str, messages: list[dict]) -> dict:
    """The request body, with only the parameters this model is known to take."""
    payload = {
        "model": model,
        "stream": True,
        length_parameter(provider, model): advanced("ai.max_tokens"),
        "messages": messages,
        # The final chunk then carries the token counts (#416).
        "stream_options": {"include_usage": True},
    }
    if accepts_sampling(provider, model):
        payload["temperature"] = advanced("ai.temperature")
    return payload


def learn_from_refusal(provider: Provider, payload: dict, body: bytes) -> bool:
    """
    Read a 400, drop the parameter it names, and say whether to retry.

    Providers refuse one parameter at a time, so a model that rejects both
    takes two retries; the caller bounds that. Anything the body does not
    name is not this function's to guess at.
    """
    text = body.decode("utf-8", errors="replace")
    key = _key(provider, payload.get("model", ""))
    if "temperature" in payload and "temperature" in text:
        _NO_SAMPLING.add(key)
        payload.pop("temperature", None)
        return True
    if "max_tokens" in payload and "max_tokens" in text:
        _COMPLETION_TOKENS.add(key)
        payload["max_completion_tokens"] = payload.pop("max_tokens")
        return True
    return False


def _error_message(error) -> str:
    """The message out of an error object, whichever shape it takes."""
    if isinstance(error, dict):
        return str(error.get("message") or error.get("type") or error)[:400]
    return str(error)[:400]


def normalise_usage(u: dict) -> dict:
    """
    The usage object in ShellMate's one shape: ``input`` is the uncached
    prompt, ``cache_read`` what was served from cache, ``output`` the reply.

    OpenAI-shaped providers count the cached portion *inside*
    ``prompt_tokens``, where Anthropic's ``input_tokens`` excludes it. The
    meter adds ``input`` and ``cache_read`` back together, so reporting
    ``prompt_tokens`` as the input counted the cached part twice and
    overstated the context by up to the whole prompt (#499). OpenAI
    reports cached tokens in a sub-object; DeepSeek at the top level.
    """
    details = u.get("prompt_tokens_details") or {}
    prompt = int(u.get("prompt_tokens") or 0)
    cached = int(details.get("cached_tokens", u.get("prompt_cache_hit_tokens", 0)) or 0)
    return {
        "input":      max(0, prompt - cached),
        "output":     int(u.get("completion_tokens") or 0),
        "cache_read": cached,
    }


async def stream(
    provider: Provider,
    api_key: str,
    user_message: str,
    context_block: str,
    model: str,
    system_prompt: str | None = None,
    history: list[dict] | None = None,
) -> AsyncIterator:
    """
    Stream one chat completion, yielding text chunks and then the usage.

    Raises ValueError with the provider's own message on any status other
    than 200, after at most two retries that each drop a parameter the
    model refused.
    """
    full_user_message = (
        f"{context_block}\n\n=== ENGINEER'S QUESTION ===\n{user_message}"
    )
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type":  "application/json",
    }
    payload = build_payload(provider, model, turns.openai_messages(
        system_prompt or SYSTEM_PROMPT, history, full_user_message))
    usage: dict = {}

    async with httpx.AsyncClient(timeout=advanced("ai.request_timeout")) as client:
        for attempt in (1, 2, 3):
            async with client.stream(
                "POST", provider.url, headers=headers, json=payload
            ) as resp:
                if resp.status_code != 200:
                    body = await resp.aread()
                    if (attempt < 3 and resp.status_code == 400
                            and learn_from_refusal(provider, payload, body)):
                        continue
                    raise ValueError(
                        f"{provider.label} API error {resp.status_code}: "
                        f"{body.decode('utf-8', errors='replace')[:400]}"
                    )

                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data = line[6:]
                    if data == "[DONE]":
                        break
                    try:
                        event = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    # A failure after the 200 — a rate limit hit mid-stream,
                    # a model that fell over — arrives as an error event
                    # with no choices. Read as text it was an empty reply
                    # and a clean "done" (#500).
                    if event.get("error"):
                        raise ValueError(
                            f"{provider.label} error: {_error_message(event['error'])}")
                    if event.get("usage"):
                        usage.update(normalise_usage(event["usage"]))
                    choices = event.get("choices") or [{}]
                    chunk = (choices[0].get("delta") or {}).get("content", "")
                    if chunk:
                        yield chunk
                break
    if usage:
        usage["provider"] = provider.name
        yield {"usage": usage}
