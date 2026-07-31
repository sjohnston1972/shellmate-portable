"""
settings_store.py — Application settings persistence for ShellMate.
Settings are stored in settings.json in the portable data directory
(see backend/paths.py).

Also provides effective-config helpers — settings.json overrides .env values
for API keys, model URLs, and the Chroma DB URL.
"""
import json
import logging
from pathlib import Path

from backend import config as env_config
from backend import paths
from backend.vault import VaultError, vault

logger = logging.getLogger(__name__)

DEFAULT_SETTINGS: dict = {
    "terminal": {
        "font_family": "JetBrains Mono, Fira Code, Consolas, monospace",
        "font_size": 14,
        "line_height": 1.2,
        "cursor_style": "block",
        "cursor_blink": True,
        "scrollback_lines": 5000,
        "right_click_paste": True,
        "copy_on_select": False,
        # Expand short aliases ("ints") into the right command for whatever
        # platform the tab is connected to.
        "expand_aliases": True,
        # Send the platform's paging-off command on connect, so nobody types
        # "terminal length 0" a hundred times a week.
        "auto_paging_off": True,
    },
    # Regex rules that colour terminal output. Applied to plain text only, so
    # colour a device sends itself is never disturbed.
    "highlight": {
        "enabled": True,
        "rules": [
            {"pattern": r"\b(down|err-disabled|failed|failure|denied|unreachable)\b",
             "colour": "red", "ignore_case": True},
            {"pattern": r"\b(error|errors|CRC|drop|drops|discard|discards)\b",
             "colour": "orange", "ignore_case": True},
            {"pattern": r"\b(up|connected|established|active|success|ok)\b",
             "colour": "green", "ignore_case": True},
            {"pattern": r"\b(warning|notice|shutdown|disabled)\b",
             "colour": "yellow", "ignore_case": True},
        ],
    },
    "logging": {
        "enabled": False,
        "directory": "logs",
        # Mask credentials in written logs. On by default: a session log is
        # meant to be handed to someone else, and devices echo.
        "redact_secrets": True,
    },
    "appearance": {
        "color_scheme": "deep_space",
    },
    # Defaults for a serial console, so the same values are not re-entered on
    # every connection. Cisco console is 9600 8-N-1 with no flow control.
    "serial": {
        "baud_rate": 9600,
        "data_bits": 8,
        "parity": "N",
        "stop_bits": 1,
        "flow_control": "none",
    },
    # User-overridable API keys / endpoints. Empty string means "fall back
    # to whatever .env provides". Keys persisted here override .env.
    "providers": {
        "anthropic_api_key": "",
        "openai_api_key": "",
        "xai_api_key": "",
        "deepseek_api_key": "",
        "ollama_host": "",
        "chroma_url": "",
        "chroma_collection": "design_guidelines",
    },
    "ai": {
        # "learn" | "tshoot" — controls which system-prompt persona is used.
        "mode": "tshoot",
        # The assistant is optional. On a locked-down network it may not be
        # reachable at all, and the terminal has to stand on its own.
        "panel_enabled": True,
    },
}

# Which env-var name backs each provider field, for the "preconfigured by env"
# indicator the settings UI shows.
ENV_BACKED_FIELDS: dict = {
    "anthropic_api_key": "ANTHROPIC_API_KEY",
    "openai_api_key":    "OPENAI_API_KEY",
    "xai_api_key":       "XAI_API_KEY",
    "deepseek_api_key":  "DEEPSEEK_API_KEY",
    "ollama_host":       "OLLAMA_HOST",
    "chroma_url":        "CHROMA_URL",
    "chroma_collection": "CHROMA_COLLECTION",
}

SECRET_FIELDS = {
    "anthropic_api_key", "openai_api_key", "xai_api_key", "deepseek_api_key",
}


def get_settings() -> dict:
    """Return raw stored settings deep-merged over the defaults."""
    settings_file = paths.settings_file()
    if not settings_file.exists():
        return _deep_merge(DEFAULT_SETTINGS, {})
    try:
        stored = json.loads(settings_file.read_text(encoding="utf-8"))
        return _deep_merge(DEFAULT_SETTINGS, stored)
    except Exception:
        return _deep_merge(DEFAULT_SETTINGS, {})


def get_settings_for_ui() -> dict:
    """
    Return settings shaped for the frontend:
      - secret API keys are masked (replaced by 8 dots) but a flag tells the UI
        whether a value is actually set
      - includes an "env_preconfigured" map listing which provider fields have
        an env var backing them so the UI can render the placeholder text
    """
    s = get_settings()
    providers = dict(s.get("providers", {}))
    out_providers: dict = {}
    has_value: dict = {}
    for k, v in providers.items():
        # A secret's real home is the vault, so "is it set?" has to be asked
        # there. settings.json is only consulted as a fallback for a key that
        # predates the vault and has not been migrated yet.
        stored = vault.get(k) if k in SECRET_FIELDS else ""
        effective = stored or v

        if k in SECRET_FIELDS and effective:
            out_providers[k] = "•" * 8
        else:
            out_providers[k] = v or ""
        has_value[k] = bool(effective)

    env_preconfigured: dict = {}
    for field, env_name in ENV_BACKED_FIELDS.items():
        env_preconfigured[field] = bool(getattr(env_config, env_name, "") if hasattr(env_config, env_name) else "")
        # CHROMA_URL/CHROMA_COLLECTION read directly from os.getenv since they
        # weren't in config.py originally
        if field in ("chroma_url", "chroma_collection"):
            import os
            env_preconfigured[field] = bool(os.getenv(env_name, ""))

    s_out = dict(s)
    s_out["providers"] = out_providers
    s_out["providers_has_value"] = has_value
    s_out["env_preconfigured"] = env_preconfigured
    return s_out


def update_settings(partial: dict) -> dict:
    """
    Persist a partial settings update.

    Secrets are diverted into the encrypted vault and never reach
    settings.json. Everything else is merged and written as before.

    A secret arriving as the masked placeholder ("••••••••") means the user
    did not touch that field, so it is dropped rather than saved — otherwise
    opening the settings panel and pressing Save would overwrite every stored
    key with a row of dots.
    """
    incoming = dict(partial)
    secrets = _extract_secrets(incoming)

    if secrets:
        try:
            vault.set_many(secrets)
        except VaultError as exc:
            raise VaultError(f"Could not save to the vault: {exc}") from exc

    current = get_settings()
    merged = _deep_merge(current, incoming)

    # Belt and braces: even if a secret slipped through the extraction above,
    # it must not be written to disk in the clear.
    for field in SECRET_FIELDS:
        if field in merged.get("providers", {}):
            merged["providers"][field] = ""

    settings_file = paths.settings_file()
    settings_file.parent.mkdir(parents=True, exist_ok=True)
    settings_file.write_text(json.dumps(merged, indent=2), encoding="utf-8")
    return get_settings_for_ui()


def log_directory() -> Path:
    """
    Resolve the configured session-log directory to an absolute path.

    An absolute path in settings is honoured as-is, so a user can log straight
    to a network share.  A relative one resolves against the portable data
    directory — never the working directory, which for a double-clicked
    executable is wherever Explorer happened to leave us.
    """
    configured = (get_settings().get("logging", {}) or {}).get("directory", "logs")
    candidate = Path(configured)
    return candidate if candidate.is_absolute() else paths.data_dir() / candidate


def get_effective(field: str, env_fallback: str = "") -> str:
    """
    Return the active value for a provider field.

    Order of precedence:

    1. The encrypted vault — where every secret now lives.
    2. settings.json — non-secret fields (Ollama host, Chroma URL) only, plus
       any plaintext secret left behind by a version predating the vault and
       not yet migrated.
    3. The matching .env variable.

    Every AI client resolves its credentials through here, so this one function
    is what makes the vault apply everywhere.
    """
    if field in SECRET_FIELDS:
        stored = vault.get(field)
        if stored:
            return stored

    settings = get_settings()
    value = (settings.get("providers", {}) or {}).get(field, "") or ""
    return value or env_fallback


# ---------------------------------------------------------------------------
# Migration off plaintext storage
# ---------------------------------------------------------------------------


def migrate_plaintext_secrets() -> list[str]:
    """
    Move any plaintext API keys out of settings.json and into the vault.

    Versions before the vault wrote provider keys straight into settings.json.
    Leaving them there would mean the vault protects new keys while the old
    ones stay readable next to it, which is worse than either option alone.

    Skipped silently when the vault is locked — the user gets a prompt first,
    and migration runs on the next write instead.

    Returns:
        Names of the fields moved, for the startup log. Never their values.
    """
    settings = get_settings()
    providers = settings.get("providers", {}) or {}

    plaintext = {
        field: providers.get(field, "")
        for field in SECRET_FIELDS
        if providers.get(field)
    }
    if not plaintext:
        return []

    try:
        vault.set_many(plaintext)
    except VaultError as exc:
        logger.warning("Could not migrate plaintext keys into the vault: %s", exc)
        return []

    # Only blank them in settings.json once the vault write has succeeded, so a
    # failure here can never destroy the only copy.
    cleared = {field: "" for field in plaintext}
    current = get_settings()
    current.setdefault("providers", {}).update(cleared)
    settings_file = paths.settings_file()
    settings_file.parent.mkdir(parents=True, exist_ok=True)
    settings_file.write_text(json.dumps(current, indent=2), encoding="utf-8")

    return sorted(plaintext)


def _extract_secrets(partial: dict) -> dict[str, str]:
    """
    Pull secret provider fields out of *partial*, mutating it in place.

    Returns the secrets the user actually changed, ready for the vault.
    Values that are the masked placeholder are treated as "unchanged" and
    excluded from both the return value and the settings written to disk.
    """
    providers = partial.get("providers")
    if not isinstance(providers, dict):
        return {}

    secrets: dict[str, str] = {}
    for field in list(providers):
        if field not in SECRET_FIELDS:
            continue
        value = providers.pop(field)
        if not isinstance(value, str):
            continue
        # Unchanged: the UI echoed back the mask it was given.
        if value and set(value) <= {"•"}:
            continue
        secrets[field] = value

    return secrets


def _deep_merge(base: dict, override: dict) -> dict:
    result = dict(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result
