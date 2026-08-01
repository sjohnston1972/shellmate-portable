"""
prompt_store.py — The system prompts, as data rather than code.

The two personas are the single largest influence on what the assistant says,
and until now they were constants in ``prompts.py``: invisible in the
application and unchangeable without a rebuild.  That is the wrong shape for
the one thing an experienced engineer would most want to adjust — "stop
explaining, I know what BGP is", or "always mention the change-control
reference", or a house style for how commands are proposed.

Follows ``platforms.json`` exactly, because that pattern is already proven
here: the built-ins are written to ``prompts.json`` in the data directory on
first run, the file is read back in preference to them, and deleting it
restores the defaults.  Editable in Settings or in a text editor, kept in
version control, carried between machines.

Two rules keep it safe:

**A broken file must not break the assistant.**  Unreadable JSON, a missing
mode, a body that is not a string — each falls back to the built-in for that
persona with a log line.  An assistant that will not answer is worse than one
answering with the shipped prompt.

**The command-suggestion rules are not editable.**  They are referenced by a
marker (see ``prompts.RULES_MARKER``) and substituted at render time.  Delete
the marker and they are appended at the end instead — because losing them
entirely would silently stop ``[SUGGEST_CMD]`` blocks rendering as clickable
commands, with no error anywhere to explain it.
"""

import json
import logging

from backend import paths
from backend.ai import prompts

logger = logging.getLogger(__name__)

_cache: dict[str, str] | None = None


def prompts_path():
    """Where the editable copy of the prompts lives."""
    return paths.data_dir() / "prompts.json"


def load(refresh: bool = False) -> dict[str, str]:
    """
    Return the persona body for every mode.

    Writes the built-ins to disk on first run so there is something to edit,
    then prefers whatever is on disk.
    """
    global _cache
    if _cache is not None and not refresh:
        return _cache

    bodies = dict(prompts.DEFAULT_BODIES)

    path = prompts_path()
    if not path.exists():
        _write(bodies)
    else:
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
            for mode, body in (stored.get("prompts") or {}).items():
                if mode in bodies and isinstance(body, str) and body.strip():
                    bodies[mode] = body
                elif mode in bodies:
                    logger.warning(
                        "Ignoring an unusable '%s' prompt in %s; using the built-in",
                        mode, path.name,
                    )
        except (OSError, json.JSONDecodeError, TypeError, AttributeError) as exc:
            logger.warning("Ignoring unreadable %s (%s); using the built-in prompts",
                           path.name, exc)

    _cache = bodies
    return bodies


def body(mode: str | None) -> str:
    """The editable persona body for a mode. Unknown modes fall back to tshoot."""
    key = (mode or "").lower()
    bodies = load()
    return bodies.get(key) or bodies["tshoot"]


def rendered(mode: str | None) -> str:
    """
    The full system prompt: the body with the command rules substituted in.

    When suggestions are switched off the rules are replaced by an instruction
    not to offer any, rather than simply omitted — a model given no format to
    use will invent one, and the blocks would come back as literal tags.
    """
    from backend.advanced import get as advanced

    text = prompts.render(body(mode))

    if not advanced("ai.suggest_commands"):
        text = text.replace(prompts._COMMAND_FORMAT_RULES, _NO_SUGGESTIONS)
    elif not advanced("ai.confirm_dangerous"):
        text = text.replace(
            "- Flag potentially dangerous commands (reload, write erase, "
            "shutdown, no shutdown, clear) with a ⚠️ warning.",
            "- Flag potentially dangerous commands with a ⚠️ warning.")

    return text


#: What replaces the command-suggestion rules when suggestions are off. An
#: instruction, not an absence: a model told nothing about format will invent
#: one, and the tags would arrive as literal text in the reply.
_NO_SUGGESTIONS = (
    "- Do NOT propose commands as clickable blocks and do NOT use any "
    "[SUGGEST_CMD] tags. Describe what to run in prose and let the engineer "
    "type it themselves."
)


def save(mode: str, text: str) -> dict[str, str]:
    """
    Persist an edited persona.

    Raises:
        ValueError: Unknown mode, or an empty body — a blank system prompt is
            not an edit anyone means to make, and would leave the assistant
            with no instructions at all.
    """
    key = (mode or "").lower()
    if key not in prompts.DEFAULT_BODIES:
        raise ValueError(f"'{mode}' is not a prompt ShellMate knows about.")
    if not (text or "").strip():
        raise ValueError("A prompt cannot be empty. Use Reset to restore the default.")

    bodies = dict(load())
    bodies[key] = text
    _write(bodies)

    global _cache
    _cache = bodies
    return bodies


def reset(mode: str | None = None) -> dict[str, str]:
    """Restore one persona, or all of them, to the shipped text."""
    bodies = dict(load())
    if mode is None:
        bodies = dict(prompts.DEFAULT_BODIES)
    else:
        key = (mode or "").lower()
        if key not in prompts.DEFAULT_BODIES:
            raise ValueError(f"'{mode}' is not a prompt ShellMate knows about.")
        bodies[key] = prompts.DEFAULT_BODIES[key]

    _write(bodies)

    global _cache
    _cache = bodies
    return bodies


def state() -> dict:
    """
    Everything the settings panel needs.

    ``modified`` is what lets the interface mark a prompt somebody changed six
    months ago as visibly not the shipped one.
    """
    bodies = load()
    return {
        "path": str(prompts_path()),
        "marker": prompts.RULES_MARKER,
        "command_rules": prompts._COMMAND_FORMAT_RULES,
        "prompts": {
            mode: {
                "body":       bodies[mode],
                "default":    prompts.DEFAULT_BODIES[mode],
                "modified":   bodies[mode] != prompts.DEFAULT_BODIES[mode],
                "has_marker": prompts.RULES_MARKER in bodies[mode],
            }
            for mode in prompts.MODES
        },
    }


def _write(bodies: dict[str, str]) -> None:
    """Write the full set to disk, so the file stays a complete record."""
    document = {
        "_comment": (
            "System prompts for ShellMate's assistant. Edit freely — this file "
            "is read in preference to the built-in defaults. Delete it, or a "
            "single entry, to restore them. Keep the {command_rules} marker "
            "somewhere in each prompt: it is replaced with the rules that make "
            "suggested commands clickable. Without it they are appended at the "
            "end instead."
        ),
        "prompts": bodies,
    }
    path = prompts_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document, indent=2), encoding="utf-8")
    except OSError as exc:
        # Not fatal: an unwritable data folder should cost the edit, not the
        # assistant.
        logger.warning("Could not write %s: %s", path.name, exc)
