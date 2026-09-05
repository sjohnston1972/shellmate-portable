"""
config.py — Configuration loader for ShellMate.

Reads settings from the .env file (via python-dotenv) and exposes them
as module-level constants used throughout the backend.  Defaults are
applied when a variable is absent or empty.
"""

import os


def _env(name: str, default: str) -> str:
    """
    Read an env var, treating *empty* the same as absent (#335).

    `.env.example` ships optional keys as blank lines (`ANTHROPIC_API_KEY=`),
    and dotenv loads a blank as "". `os.getenv(name, default)` returns that
    "" — so a blank numeric key crashed `int()` at import time, before
    logging or the failure message box exist. The docstring above always
    promised absent-or-empty; this makes it true.
    """
    value = os.getenv(name, "")
    return value if value.strip() else default


def _env_int(name: str, default: int) -> int:
    """A numeric env var that falls back rather than crashing the import."""
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


# Server binding
HOST: str = _env("SHELLMATE_HOST", "127.0.0.1")
PORT: int = _env_int("SHELLMATE_PORT", 8765)

# Claude API — accept either ANTHROPIC_API_KEY or CLAUDE_API_KEY
ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY") or os.getenv("CLAUDE_API_KEY", "")

# Ollama — Ollama itself exports OLLAMA_HOST as a bare address (e.g. "0.0.0.0")
# so we normalise it to a full URL here.
_ollama_host_raw: str = _env("OLLAMA_HOST", "http://localhost:11434")
if not _ollama_host_raw.startswith(("http://", "https://")):
    _ollama_host_raw = f"http://{_ollama_host_raw}"
# 0.0.0.0 means Ollama is listening on all interfaces; it is not an address
# you can *connect* to (outright refused on Windows), so dial localhost.
# Matched with or without a port (#339) — `0.0.0.0:11434` is the exact form
# Ollama exports, and the old exact-string comparison let it straight past.
if _ollama_host_raw.startswith(("http://0.0.0.0", "https://0.0.0.0")):
    _port = _ollama_host_raw.rpartition(":")[2]
    _ollama_host_raw = ("http://localhost:" + _port) if _port.isdigit() \
                       else "http://localhost:11434"
OLLAMA_HOST: str = _ollama_host_raw
OLLAMA_MODEL: str = _env("OLLAMA_MODEL", "qwen2.5:7b")

# xAI (Grok) — OpenAI-compatible API
XAI_API_KEY: str  = os.getenv("XAI_API_KEY", "")
XAI_MODEL: str    = os.getenv("XAI_MODEL", "grok-3")

# OpenAI
OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL: str   = os.getenv("OPENAI_MODEL", "gpt-4o")

# DeepSeek
DEEPSEEK_API_KEY: str = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_MODEL: str   = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

# Default AI backend ("claude", "ollama", or "xai")
DEFAULT_AI_BACKEND: str = os.getenv("DEFAULT_AI_BACKEND", "claude")

# Serial / console defaults (Windows COM port)
DEFAULT_SERIAL_PORT: str = _env("DEFAULT_SERIAL_PORT", "COM3")
DEFAULT_BAUD_RATE: int = _env_int("DEFAULT_BAUD_RATE", 9600)

# Jira is deliberately not read here (#540). Constants in this module bind
# when it imports, so configuring Jira meant editing .env and restarting —
# which for a portable build closes every live session, and was the single
# biggest reason the feature went unused. `jira_client.settings()` resolves
# Settings, then the vault, then JIRA_URL / JIRA_USER_EMAIL /
# JIRA_API_TOKEN / JIRA_PROJECT_KEY, at the moment a call is made.

# Chroma vector DB (optional). When set, ShellMate queries this collection for
# design-guideline context to inject into AI prompts. Empty = disabled.
CHROMA_URL: str        = os.getenv("CHROMA_URL", "")
CHROMA_COLLECTION: str = os.getenv("CHROMA_COLLECTION", "design_guidelines")
