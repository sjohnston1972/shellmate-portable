"""
ai/providers.py — Check a provider works, and find out what it can run.

Adding an API key previously gave no feedback at all: you typed it in, saved,
and found out whether it was right the next time you asked a question and got
an error in the chat pane.  Equally, the model list was hardcoded, so pulling
a new model into Ollama or gaining access to a new Claude model meant editing
the HTML.

Both are answered by asking the provider itself.  Every one of these exposes a
models endpoint, and listing models doubles as the connection test — it needs
a valid key, costs nothing, and returns something useful when it succeeds.

Errors are translated rather than passed through. "401" tells a network
engineer nothing; "the key was rejected" tells them what to do.
"""

import asyncio
import logging
from dataclasses import dataclass

import httpx

from backend.settings_store import get_effective

logger = logging.getLogger(__name__)

TIMEOUT = 12.0

# A provider that answers nothing in this long is unreachable for our purposes,
# whatever the underlying cause.
CONNECT_TIMEOUT = 8.0


@dataclass
class ProviderResult:
    """Outcome of testing a provider."""

    ok: bool
    message: str
    models: list[dict]

    def as_dict(self) -> dict:
        return {"ok": self.ok, "message": self.message, "models": self.models}


# ---------------------------------------------------------------------------
# Provider definitions
# ---------------------------------------------------------------------------

PROVIDERS: dict[str, dict] = {
    "anthropic": {
        "label": "Anthropic",
        "key_field": "anthropic_api_key",
        "env": "ANTHROPIC_API_KEY",
        "models_url": "https://api.anthropic.com/v1/models",
        "headers": lambda key: {"x-api-key": key, "anthropic-version": "2023-06-01"},
    },
    "openai": {
        "label": "OpenAI",
        "key_field": "openai_api_key",
        "env": "OPENAI_API_KEY",
        "models_url": "https://api.openai.com/v1/models",
        "headers": lambda key: {"Authorization": f"Bearer {key}"},
    },
    "xai": {
        "label": "xAI",
        "key_field": "xai_api_key",
        "env": "XAI_API_KEY",
        "models_url": "https://api.x.ai/v1/models",
        "headers": lambda key: {"Authorization": f"Bearer {key}"},
    },
    "deepseek": {
        "label": "DeepSeek",
        "key_field": "deepseek_api_key",
        "env": "DEEPSEEK_API_KEY",
        "models_url": "https://api.deepseek.com/models",
        "headers": lambda key: {"Authorization": f"Bearer {key}"},
    },
    "ollama": {
        "label": "Ollama",
        "key_field": "",                     # local, no key
        "env": "",
        "models_url": "",                    # built from the configured host
        "headers": lambda key: {},
    },
}


def _api_key(provider: str) -> str:
    """Resolve the key from the vault, settings, then .env."""
    spec = PROVIDERS.get(provider) or {}
    field = spec.get("key_field")
    if not field:
        return ""

    from backend import config as env_config

    return get_effective(field, getattr(env_config, spec.get("env", ""), "") or "")


def _ollama_url() -> str:
    from backend.config import OLLAMA_HOST

    host = get_effective("ollama_host", OLLAMA_HOST) or "http://localhost:11434"
    return f"{host.rstrip('/')}/api/tags"


def _explain(provider: str, status: int, body: str) -> str:
    """
    Turn an HTTP status into something worth reading.

    The status alone is not actionable, and the provider's own error body is
    usually a wall of JSON.
    """
    label = PROVIDERS.get(provider, {}).get("label", provider)

    if status in (401, 403):
        return f"{label} rejected the API key. Check it has been copied in full."
    if status == 404:
        return f"{label} did not recognise the request. The endpoint may have moved."
    if status == 429:
        return f"{label} is rate limiting. The key works, but you are over quota."
    if 500 <= status < 600:
        return f"{label} is having server trouble ({status}). Nothing wrong at this end."
    snippet = (body or "").strip().replace("\n", " ")[:160]
    return f"{label} returned {status}. {snippet}".strip()


def _normalise(provider: str, payload: dict) -> list[dict]:
    """
    Reduce each provider's model list to {id, label}.

    They all disagree about the shape: Anthropic and OpenAI wrap a list in
    "data", Ollama uses "models" with a "name". Normalising here keeps that
    mess out of the frontend.
    """
    models: list[dict] = []

    if provider == "ollama":
        for entry in payload.get("models", []) or []:
            name = entry.get("name")
            if not name:
                continue
            size = (entry.get("details") or {}).get("parameter_size", "")
            models.append({"id": name, "label": f"{name} {size}".strip()})
        return models

    for entry in payload.get("data", []) or []:
        model_id = entry.get("id")
        if not model_id:
            continue
        models.append({"id": model_id, "label": entry.get("display_name") or model_id})

    # OpenAI in particular returns everything including embeddings and audio
    # models, which are no use in a chat panel.
    if provider == "openai":
        models = [
            m for m in models
            if m["id"].startswith(("gpt-", "o1", "o3", "o4", "chatgpt"))
            and not any(x in m["id"] for x in ("embedding", "audio", "realtime",
                                               "transcribe", "tts", "image", "moderation"))
        ]

    models.sort(key=lambda m: m["id"])
    return models


async def check(provider: str) -> ProviderResult:
    """
    Test a provider and return the models it offers.

    Never raises: a failure is a result the UI shows, not an exception.
    """
    spec = PROVIDERS.get(provider)
    if spec is None:
        return ProviderResult(False, f"Unknown provider '{provider}'.", [])

    if provider == "ollama":
        url, headers = _ollama_url(), {}
    else:
        key = _api_key(provider)
        if not key:
            return ProviderResult(
                False, f"No API key set for {spec['label']}.", [],
            )
        url, headers = spec["models_url"], spec["headers"](key)

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(TIMEOUT, connect=CONNECT_TIMEOUT)
        ) as client:
            response = await client.get(url, headers=headers)
    except httpx.ConnectError:
        if provider == "ollama":
            return ProviderResult(
                False,
                "Could not reach Ollama. Is it running, and is the host correct?",
                [],
            )
        return ProviderResult(
            False,
            f"Could not reach {spec['label']}. Check your internet connection "
            f"or proxy — this machine may not be allowed out.",
            [],
        )
    except httpx.TimeoutException:
        return ProviderResult(False, f"{spec['label']} timed out after {TIMEOUT:.0f}s.", [])
    except Exception as exc:
        return ProviderResult(False, f"Could not reach {spec['label']}: {exc}", [])

    if response.status_code != 200:
        return ProviderResult(False, _explain(provider, response.status_code, response.text), [])

    try:
        models = _normalise(provider, response.json())
    except Exception as exc:
        return ProviderResult(False, f"{spec['label']} returned something unreadable: {exc}", [])

    if not models:
        return ProviderResult(
            True, f"Connected to {spec['label']}, but it offers no usable models.", [],
        )

    plural = "model" if len(models) == 1 else "models"
    return ProviderResult(True, f"Connected. {len(models)} {plural} available.", models)


async def check_all() -> dict[str, dict]:
    """
    Test every configured provider at once.

    Concurrently, because doing them in sequence would take as long as the
    slowest one multiplied by however many are set up.
    """
    names = [
        name for name in PROVIDERS
        if name == "ollama" or _api_key(name)
    ]
    results = await asyncio.gather(*(check(name) for name in names), return_exceptions=True)

    out: dict[str, dict] = {}
    for name, result in zip(names, results):
        if isinstance(result, Exception):
            out[name] = ProviderResult(False, str(result), []).as_dict()
        else:
            out[name] = result.as_dict()
    return out
