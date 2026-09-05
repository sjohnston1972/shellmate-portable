"""
deepseek_client.py — Streaming DeepSeek client for ShellMate.
DeepSeek exposes an OpenAI-compatible API at api.deepseek.com, so this
uses the loop shared with the other OpenAI-shaped providers
(openai_compat.py).
"""
import logging
import re
from collections.abc import AsyncIterator

from backend.config import DEEPSEEK_API_KEY, DEEPSEEK_MODEL
from backend.settings_store import get_effective
from backend.ai import openai_compat

logger = logging.getLogger(__name__)

DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"

#: deepseek-reasoner documents temperature as having no effect, so it is
#: not sent; anything else that refuses a parameter is learned (#497).
PROVIDER = openai_compat.Provider(
    name="deepseek", label="DeepSeek", url=DEEPSEEK_API_URL,
    reasoning=re.compile(r"^deepseek-reasoner"),
)


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
    Stream a DeepSeek response token by token.
    Yields text chunks as they arrive.
    """
    api_key = get_effective("deepseek_api_key", DEEPSEEK_API_KEY)
    if not api_key:
        raise ValueError("DeepSeek API key is not set. Configure it in Settings or .env.")

    async for item in openai_compat.stream(
        PROVIDER, api_key, user_message, context_block, model or DEEPSEEK_MODEL,
        system_prompt=system_prompt, history=history, tools=tools,
        prior=prior, attachment=attachment,
    ):
        yield item
