"""
openai_client.py — Streaming OpenAI client for ShellMate.
Uses the standard OpenAI chat/completions SSE format, through the loop
shared with the other OpenAI-shaped providers (openai_compat.py).
"""
import logging
import re
from collections.abc import AsyncIterator

from backend.config import OPENAI_API_KEY, OPENAI_MODEL
from backend.settings_store import get_effective
from backend.ai import openai_compat

logger = logging.getLogger(__name__)

OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"

#: The o-series and GPT-5 are reasoning models: they return 400 for
#: `max_tokens` (they want `max_completion_tokens`) and for any temperature
#: but the default (#497). The picker offers them on purpose.
PROVIDER = openai_compat.Provider(
    name="openai", label="OpenAI", url=OPENAI_API_URL,
    reasoning=re.compile(r"^(?:o[1-9]|gpt-5)"),
    completion_tokens=True,
)


async def stream_response(
    user_message: str,
    context_block: str,
    model: str | None = None,
    system_prompt: str | None = None,
    history: list[dict] | None = None,
    tools: list[dict] | None = None,
    prior: list[dict] | None = None,
) -> AsyncIterator:
    """
    Stream an OpenAI response token by token.
    Yields text chunks as they arrive.
    """
    api_key = get_effective("openai_api_key", OPENAI_API_KEY)
    if not api_key:
        raise ValueError("OpenAI API key is not set. Configure it in Settings or .env.")

    async for item in openai_compat.stream(
        PROVIDER, api_key, user_message, context_block, model or OPENAI_MODEL,
        system_prompt=system_prompt, history=history, tools=tools, prior=prior,
    ):
        yield item
