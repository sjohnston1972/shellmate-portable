"""
xai_client.py — Streaming xAI (Grok) client for ShellMate.
xAI exposes an OpenAI-compatible REST API, so this uses the loop shared
with the other OpenAI-shaped providers (openai_compat.py).
"""
import logging
from collections.abc import AsyncIterator

from backend.config import XAI_API_KEY, XAI_MODEL
from backend.settings_store import get_effective
from backend.ai import openai_compat

logger = logging.getLogger(__name__)

XAI_API_URL = "https://api.x.ai/v1/chat/completions"

#: xAI publishes no list of models that refuse sampling parameters, so
#: nothing is assumed: a model that answers one with a 400 is retried
#: without it and remembered for the rest of the run (#497).
PROVIDER = openai_compat.Provider(name="xai", label="xAI", url=XAI_API_URL)


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
    Stream a Grok response token by token via xAI's OpenAI-compatible API.
    Yields text chunks as they arrive.
    """
    api_key = get_effective("xai_api_key", XAI_API_KEY)
    if not api_key:
        raise ValueError("xAI API key is not set. Configure it in Settings or .env.")

    async for item in openai_compat.stream(
        PROVIDER, api_key, user_message, context_block, model or XAI_MODEL,
        system_prompt=system_prompt, history=history, tools=tools,
        prior=prior, attachment=attachment,
    ):
        yield item
