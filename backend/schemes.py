"""
schemes.py — Terminal colour schemes as data.

The seven built-in schemes were a hardcoded object in ``settings.js``, so a
house scheme — or one matching the rest of somebody's tooling — meant editing
the source and rebuilding. They live in ``schemes.json`` in the data folder
now, following ``platforms.json`` and ``snippets.json``: written on first run
and read back in preference to the built-ins, so a new scheme is a text edit
rather than a rebuild, and it travels with the data folder.

Two things this file is careful about:

**A user scheme is never rejected for being ugly.** ``test_contrast.py``
measures the interface's own tokens and has nothing to say here — these are
terminal colours, and somebody who wants an unreadable one is entitled to it.
What is offered instead is a *warning* at the point of editing, because the
failure mode is a terminal that cannot be read with no explanation of why.

**A missing or malformed scheme falls back rather than failing.** A terminal
that will not open because a colour is spelled wrong is a far worse outcome
than one drawn in the default scheme, so every path here degrades.
"""

import json
import logging
import re
from typing import Any

from backend import paths

logger = logging.getLogger(__name__)

HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")

# Every key xterm.js reads. A scheme missing any of them inherits from the
# default rather than being refused — a half-specified scheme is a reasonable
# thing to write by hand.
KEYS = (
    "background", "foreground", "cursor", "cursorAccent", "selectionBackground",
    "black", "red", "green", "yellow", "blue", "magenta", "cyan", "white",
    "brightBlack", "brightRed", "brightGreen", "brightYellow", "brightBlue",
    "brightMagenta", "brightCyan", "brightWhite",
)

BUILT_IN: dict[str, dict[str, Any]] = {
    "deep_space": {
        "label": "Deep Space (Default)",
        "theme": {
            "background": "#0E0E0E", "foreground": "#E5E2E1",
            "cursor": "#C3C0FF", "cursorAccent": "#0E0E0E",
            "black": "#2A2A2A", "red": "#FFB4AB", "green": "#B7C8E1",
            "yellow": "#F9E2AF", "blue": "#C3C0FF", "magenta": "#CBA6F7",
            "cyan": "#89DCEB", "white": "#E5E2E1",
            "brightBlack": "#353535", "brightRed": "#FFB4AB",
            "brightGreen": "#B7C8E1", "brightYellow": "#F9E2AF",
            "brightBlue": "#C3C0FF", "brightMagenta": "#CBA6F7",
            "brightCyan": "#89DCEB", "brightWhite": "#FFFFFF",
        },
    },
    # #569. The terminal half of the high-contrast interface theme: those
    # are two settings on purpose, because the terminal keeps its own colours
    # whatever the application around it does. Pure white on pure black, and
    # every ANSI colour bright enough to clear 7:1 on it — a scheme where the
    # text is readable and half the colours are not is not a high-contrast
    # scheme, it is a dark one with a white foreground.
    "high_contrast": {
        "label": "High Contrast",
        "theme": {
            "background": "#000000", "foreground": "#FFFFFF",
            "cursor": "#FFFFFF", "cursorAccent": "#000000",
            "selectionBackground": "#4D4D4D",
            # The normal set is already the bright one. A "dim" red at 4:1
            # on black is exactly what somebody choosing this cannot read,
            # and there is nothing to be gained by keeping it dim.
            # ANSI black as a *foreground* on a black background is
            # unreadable by definition, so every scheme remaps it. Here it
            # has to clear 7:1 like everything else, which puts it in the
            # greys — and bright black then has to move up with it, or the
            # two become the same colour and a device drawing a box loses
            # its distinction between them.
            "black": "#9A9A9A", "red": "#FF8A80", "green": "#5AFF8F",
            "yellow": "#FFD000", "blue": "#66D9FF", "magenta": "#F0A6FF",
            "cyan": "#7DF9FF", "white": "#FFFFFF",
            "brightBlack": "#C6C6C6", "brightRed": "#FFB3AC",
            "brightGreen": "#9BFFBC", "brightYellow": "#FFE566",
            "brightBlue": "#A6E9FF", "brightMagenta": "#F7C8FF",
            "brightCyan": "#B3FCFF", "brightWhite": "#FFFFFF",
        },
    },
    "solarized_dark": {
        "label": "Solarized Dark",
        "theme": {
            "background": "#002B36", "foreground": "#839496",
            "cursor": "#839496", "cursorAccent": "#002B36",
            "black": "#073642", "red": "#DC322F", "green": "#859900",
            "yellow": "#B58900", "blue": "#268BD2", "magenta": "#D33682",
            "cyan": "#2AA198", "white": "#EEE8D5",
            "brightBlack": "#002B36", "brightRed": "#CB4B16",
            "brightGreen": "#586E75", "brightYellow": "#657B83",
            "brightBlue": "#839496", "brightMagenta": "#6C71C4",
            "brightCyan": "#93A1A1", "brightWhite": "#FDF6E3",
        },
    },
    "nord": {
        "label": "Nord",
        "theme": {
            "background": "#2E3440", "foreground": "#D8DEE9",
            "cursor": "#D8DEE9", "cursorAccent": "#2E3440",
            "black": "#3B4252", "red": "#BF616A", "green": "#A3BE8C",
            "yellow": "#EBCB8B", "blue": "#81A1C1", "magenta": "#B48EAD",
            "cyan": "#88C0D0", "white": "#E5E9F0",
            "brightBlack": "#4C566A", "brightRed": "#BF616A",
            "brightGreen": "#A3BE8C", "brightYellow": "#EBCB8B",
            "brightBlue": "#81A1C1", "brightMagenta": "#B48EAD",
            "brightCyan": "#8FBCBB", "brightWhite": "#ECEFF4",
        },
    },
    "one_dark": {
        "label": "One Dark",
        "theme": {
            "background": "#282C34", "foreground": "#ABB2BF",
            "cursor": "#528BFF", "cursorAccent": "#282C34",
            "black": "#3F4451", "red": "#E06C75", "green": "#98C379",
            "yellow": "#E5C07B", "blue": "#61AFEF", "magenta": "#C678DD",
            "cyan": "#56B6C2", "white": "#ABB2BF",
            "brightBlack": "#4F5666", "brightRed": "#BE5046",
            "brightGreen": "#98C379", "brightYellow": "#E5C07B",
            "brightBlue": "#61AFEF", "brightMagenta": "#C678DD",
            "brightCyan": "#56B6C2", "brightWhite": "#FFFFFF",
        },
    },
    "gruvbox": {
        "label": "Gruvbox Dark",
        "theme": {
            "background": "#282828", "foreground": "#EBDBB2",
            "cursor": "#EBDBB2", "cursorAccent": "#282828",
            "black": "#282828", "red": "#CC241D", "green": "#98971A",
            "yellow": "#D79921", "blue": "#458588", "magenta": "#B16286",
            "cyan": "#689D6A", "white": "#A89984",
            "brightBlack": "#928374", "brightRed": "#FB4934",
            "brightGreen": "#B8BB26", "brightYellow": "#FABD2F",
            "brightBlue": "#83A598", "brightMagenta": "#D3869B",
            "brightCyan": "#8EC07C", "brightWhite": "#EBDBB2",
        },
    },
    "dracula": {
        "label": "Dracula",
        "theme": {
            "background": "#282A36", "foreground": "#F8F8F2",
            "cursor": "#F8F8F2", "cursorAccent": "#282A36",
            "black": "#21222C", "red": "#FF5555", "green": "#50FA7B",
            "yellow": "#F1FA8C", "blue": "#BD93F9", "magenta": "#FF79C6",
            "cyan": "#8BE9FD", "white": "#F8F8F2",
            "brightBlack": "#6272A4", "brightRed": "#FF6E6E",
            "brightGreen": "#69FF94", "brightYellow": "#FFFFA5",
            "brightBlue": "#D6ACFF", "brightMagenta": "#FF92DF",
            "brightCyan": "#A4FFFF", "brightWhite": "#FFFFFF",
        },
    },
    "monokai": {
        "label": "Monokai",
        "theme": {
            "background": "#272822", "foreground": "#F8F8F2",
            "cursor": "#F8F8F2", "cursorAccent": "#272822",
            "black": "#272822", "red": "#F92672", "green": "#A6E22E",
            "yellow": "#F4BF75", "blue": "#66D9E8", "magenta": "#AE81FF",
            "cyan": "#A1EFE4", "white": "#F8F8F2",
            "brightBlack": "#75715E", "brightRed": "#F92672",
            "brightGreen": "#A6E22E", "brightYellow": "#F4BF75",
            "brightBlue": "#66D9E8", "brightMagenta": "#AE81FF",
            "brightCyan": "#A1EFE4", "brightWhite": "#F9F8F5",
        },
    },
}

DEFAULT = "deep_space"


def _file():
    return paths.data_dir() / "schemes.json"


def _load_custom() -> dict[str, dict]:
    path = _file()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        # A malformed file loses the custom schemes, not the terminal.
        logger.warning("schemes.json could not be read; using the built-ins")
        return {}


def _save_custom(schemes: dict) -> None:
    path = _file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(schemes, indent=2), encoding="utf-8")


def clean_theme(theme: dict, base: str = DEFAULT) -> dict:
    """
    Keep the colours that are real hex, inherit the rest.

    Deliberately forgiving. Somebody pasting a scheme from elsewhere will
    have keys ShellMate does not use and may be missing ones it does, and
    refusing the whole thing over a stray key helps nobody.
    """
    fallback = BUILT_IN.get(base, BUILT_IN[DEFAULT])["theme"]
    out = {}
    for key in KEYS:
        value = str((theme or {}).get(key, "")).strip()
        out[key] = value if HEX.match(value) else fallback.get(key, "#000000")
    return out


def all_schemes() -> dict[str, dict]:
    """Built-ins, then custom — a custom scheme may replace a built-in name."""
    out = {name: {"label": s["label"], "theme": dict(s["theme"]), "builtin": True}
           for name, s in BUILT_IN.items()}
    for name, scheme in _load_custom().items():
        out[name] = {
            "label": scheme.get("label") or name,
            "theme": clean_theme(scheme.get("theme", {})),
            "builtin": False,
        }
    return out


def save_scheme(name: str, label: str, theme: dict) -> dict:
    """Create or replace a custom scheme."""
    key = "_".join(str(name).split()).strip().lower()
    if not key:
        raise ValueError("A scheme needs a name.")

    schemes = _load_custom()
    schemes[key] = {"label": (label or name).strip(), "theme": clean_theme(theme)}
    _save_custom(schemes)
    logger.info("Saved colour scheme %s", key)
    return {"name": key, **schemes[key], "builtin": False}


def delete_scheme(name: str) -> bool:
    """
    Remove a custom scheme. A built-in cannot be deleted, only shadowed.
    """
    schemes = _load_custom()
    if name not in schemes:
        return False
    del schemes[name]
    _save_custom(schemes)
    return True


def contrast(theme: dict) -> float:
    """
    The foreground/background contrast ratio, for the editor's warning.

    Reported, never enforced. These are terminal colours and somebody who
    wants a low-contrast scheme may have it — but a scheme that cannot be
    read is worth saying out loud, because the alternative is discovering it
    on a device at two in the morning.
    """
    def channel(value: float) -> float:
        value /= 255.0
        return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4

    def luminance(colour: str) -> float:
        if not HEX.match(colour or ""):
            return 0.0
        r, g, b = (int(colour[i:i + 2], 16) for i in (1, 3, 5))
        return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)

    light, dark = sorted((luminance(theme.get("foreground", "")),
                          luminance(theme.get("background", ""))), reverse=True)
    return round((light + 0.05) / (dark + 0.05), 2)
