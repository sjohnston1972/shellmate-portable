"""
test_contrast.py — Measure the interface's colour contrast instead of eyeballing it.

Written after a bug (#46) where the "identified Cisco IOS" note rendered dark
grey on near-black in the light theme: the panel colour was hardcoded while the
text colour followed the theme, so the two drifted apart and nothing caught it.
Three rules had the defect, three siblings had already been patched by hand, and
looking at the dark theme — where it is fine — proved nothing.

Two things are checked, and the first is the one that keeps the second honest:

1. **No floating surface hardcodes its background.**  Every dialog, panel and
   note must take its background from ``--overlay``, so a light theme cannot be
   half-applied.

2. **The resolved pairs clear WCAG AA.**  Colours are composited exactly as the
   browser would — translucent text over a translucent panel over the page — and
   the ratio is computed, in both themes.

Checking by eye is also unreliable here for a subtler reason: the theme switch
is animated, so a reading taken mid-transition measures a blend of the two.

    python test_contrast.py
"""

import re
import sys
from pathlib import Path

CSS = Path(__file__).with_name("frontend") / "css" / "style.css"

# WCAG AA for normal-size text. Everything measured here is body copy.
AA = 4.5

passed = 0
failed: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    global passed
    if condition:
        passed += 1
        print(f"  OK  {name}")
    else:
        failed.append(f"{name}: {detail}")
        print(f"  FAIL {name}\n       {detail}")


# ---------------------------------------------------------------------------
# A very small slice of CSS parsing — enough to resolve custom properties
# ---------------------------------------------------------------------------

RULE_RE = re.compile(r"([^{}]+)\{([^{}]*)\}", re.DOTALL)


def rules(text: str) -> list[tuple[str, dict[str, str]]]:
    """Return (selector, declarations) for every rule in the stylesheet."""
    # Comments first: this stylesheet explains itself above nearly every rule,
    # and a comment left in place lands in the selector of the rule after it.
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    out = []
    for selector, body in RULE_RE.findall(text):
        selector = " ".join(selector.split())
        decls = {}
        for part in body.split(";"):
            if ":" not in part:
                continue
            prop, _, value = part.partition(":")
            decls[prop.strip()] = value.strip()
        out.append((selector, decls))
    return out


def declarations_for(parsed, selector: str) -> dict[str, str]:
    """Merge every rule whose selector list contains *selector*."""
    merged: dict[str, str] = {}
    for sel, decls in parsed:
        if selector in [s.strip() for s in sel.split(",")]:
            merged.update(decls)
    return merged


def tokens(parsed, selector: str) -> dict[str, str]:
    return {k: v for k, v in declarations_for(parsed, selector).items()
            if k.startswith("--")}


def resolve(value: str, table: dict[str, str], depth: int = 0) -> str:
    """Expand var(--x) references until a literal colour is left."""
    while value.startswith("var(") and depth < 10:
        name = value[4:].split(")")[0].strip()
        value = table.get(name, "")
        depth += 1
    return value


# ---------------------------------------------------------------------------
# Colour maths
# ---------------------------------------------------------------------------


def parse_colour(value: str) -> tuple[float, float, float, float]:
    """Return (r, g, b, alpha) with channels 0-255, for #rgb/#rrggbb/rgba()."""
    value = value.strip()
    if value.startswith("#"):
        hexed = value[1:]
        if len(hexed) == 3:
            hexed = "".join(c * 2 for c in hexed)
        return (int(hexed[0:2], 16), int(hexed[2:4], 16), int(hexed[4:6], 16), 1.0)

    match = re.match(r"rgba?\(([^)]+)\)", value)
    if not match:
        raise ValueError(f"cannot parse colour {value!r}")
    parts = [p.strip() for p in match.group(1).replace("/", ",").split(",")]
    r, g, b = (float(p) for p in parts[:3])
    alpha = float(parts[3]) if len(parts) > 3 else 1.0
    return (r, g, b, alpha)


def over(top: str, bottom: tuple[float, float, float]) -> tuple[float, float, float]:
    """Composite a possibly-translucent colour over an opaque one."""
    r, g, b, alpha = parse_colour(top)
    return tuple(alpha * c + (1 - alpha) * base for c, base in zip((r, g, b), bottom))


def luminance(rgb: tuple[float, float, float]) -> float:
    def channel(value: float) -> float:
        srgb = value / 255
        return srgb / 12.92 if srgb <= 0.03928 else ((srgb + 0.055) / 1.055) ** 2.4
    r, g, b = (channel(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(fg: tuple[float, float, float], bg: tuple[float, float, float]) -> float:
    light, dark = sorted((luminance(fg), luminance(bg)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


# ---------------------------------------------------------------------------
# What is measured
# ---------------------------------------------------------------------------

# (rule, what it is, the token its text uses). The background of each comes
# from the rule itself, so a regression to a literal is caught by test 1.
SURFACES = [
    (".device-note",  'the "identified Cisco IOS" note',        "--on-surface-dim"),
    (".drift-banner", "the config-drift banner shown on connect", "--on-surface-dim"),
    ("#vault-dialog", "the master-password prompt",             "--on-surface-dim"),
    ("#modal-dialog", "the connection dialog",                  "--on-surface"),
    (".side-panel",   "Settings, History and the other panels", "--on-surface"),
    ("#paste-dialog", "the multi-line paste warning",           "--on-surface"),
    (".sm-dialog",    "confirm, prompt and alert",              "--on-surface"),
    (".tip-bubble",   "the tooltips on settings rows",          "--on-surface-dim"),
]

# Text on an overlay that is not the default colour. The warning line in a
# destructive dialog is the one people are meant to read before answering, so
# it has to clear the ratio in its own right rather than on the strength of
# being red.
ACCENTED = [
    (".sm-dialog-body", "the explanation in a dialog", "--on-surface-dim"),
    (".sm-dialog-note", "the warning line in a destructive dialog", "--error"),
]


def test_no_hardcoded_overlay_backgrounds() -> None:
    """A floating surface must take its background from the theme."""
    text = CSS.read_text(encoding="utf-8")
    parsed = rules(text)

    for selector, _label, _token in SURFACES:
        decls = declarations_for(parsed, selector)
        background = decls.get("background", "")
        check(
            f"{selector} background is themed",
            background in ("var(--overlay)", "var(--modal-bg)"),
            f"expected var(--overlay), found {background!r} — a literal here is "
            f"what made the light theme unreadable in #46",
        )

    # The specific literal that caused it, anywhere outside the token itself.
    strays = [
        (sel, decls["background"])
        for sel, decls in parsed
        if "background" in decls and re.match(r"rgba\(\s*32\s*,\s*32\s*,\s*32", decls["background"])
    ]
    check(
        "no rule hardcodes the old panel colour",
        not strays,
        f"still hardcoded in: {', '.join(s for s, _ in strays)}",
    )


def test_overlay_text_meets_aa() -> None:
    """Every floating surface reads at AA in both themes."""
    text = CSS.read_text(encoding="utf-8")
    parsed = rules(text)

    dark = tokens(parsed, ":root")
    light = dict(dark)
    light.update(tokens(parsed, '[data-theme="light"]'))

    for theme_name, table in (("dark", dark), ("light", light)):
        # The page behind the overlay. --bedrock is the terminal, which is the
        # extreme of the two and so the worst case for a translucent panel.
        page = parse_colour(resolve(table["--bedrock"], table))[:3]
        panel = over(resolve(table["--overlay"], table), page)

        for selector, label, token in SURFACES + ACCENTED:
            colour = resolve(f"var({token})", table)
            ratio = contrast(over(colour, panel), panel)
            check(
                f"{theme_name}: {selector} — {label}",
                ratio >= AA,
                f"{ratio:.2f}:1 against the panel, below the {AA}:1 AA ratio "
                f"({token} = {colour})",
            )


def main() -> int:
    print("\n" + "=" * 52)
    print("  Colour contrast")
    print("=" * 52)

    for test in (test_no_hardcoded_overlay_backgrounds, test_overlay_text_meets_aa):
        try:
            test()
        except Exception as exc:
            failed.append(f"{test.__name__}: raised {type(exc).__name__}: {exc}")
            print(f"  FAIL {test.__name__} raised {type(exc).__name__}: {exc}")

    print("\n" + "=" * 52)
    print(f"  {passed} passed  |  {len(failed)} failed")
    print("=" * 52)
    if failed:
        print("\nFAILURES:")
        for item in failed:
            print(f"  {item}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
