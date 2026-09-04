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
    # `.device-note` and `.drift-banner` were here. Both were floating
    # elements of their own, and both are alert toasts now: they sat in the
    # same bottom corner at different z-indexes, different sizes and different
    # shapes, and the device note rendered on top of the drift prompt's own
    # button. One stack, one format — so there is one surface to measure.
    (".alert-toast",  "the notification stack",                 "--on-surface"),
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
        # --overlay-solid belongs here too. The rule being enforced is that a
        # floating surface takes its background from a theme token, and the
        # pair --overlay / --overlay-solid is what the project documents for
        # exactly this — the second for surfaces that must not be translucent.
        # Omitting it made the test reject a correct fix and demand a
        # translucent background for a notification, which is the opposite of
        # what it is protecting.
        check(
            f"{selector} background is themed",
            background in ("var(--overlay)", "var(--overlay-solid)", "var(--modal-bg)"),
            f"expected an overlay token, found {background!r} — a literal here "
            f"is what made the light theme unreadable in #46",
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


#: Status text and icons, which is the half of this that was missing.
#:
#: The background check below catches a floating surface that hardcodes its
#: colour. It says nothing about a *foreground* written as a literal, and that
#: is how `color: #f9e2af` reached the discovery panel's information icon: a
#: dark-theme amber, chosen against near-black, invisible on near-white. Three
#: more like it were in the credential badges and the save confirmation.
#:
#: Each entry is (rule, what it is, text token, the tint it sits on). The tint
#: matters — a status badge is coloured text on a coloured background, and
#: measuring against the plain panel would flatter it.
STATUS = [
    (".discovery-notice .material-symbols-outlined",
     "the information icon on the scan warning", "--warn", "--warn-tint"),
    (".discovery-notice", "the scan warning itself", "--on-surface", "--warn-tint"),
    (".credential-plain", 'the "plain text" credential badge', "--warn", "--warn-tint"),
    (".credential-encrypted", 'the "encrypted" credential badge', "--ok", "--ok-tint"),
    (".form-error.form-note", "the connection-saved confirmation", "--ok", "--ok-tint"),
    (".discovery-badge", "the identified-platform badge", "--primary", "--primary-tint"),
]


def test_status_colours_read_in_both_themes() -> None:
    """
    Amber and green, measured rather than eyeballed.

    --error existed as a token and --warn/--ok did not, which is exactly why
    error text was the one status colour that already worked in both themes
    and the others did not.
    """
    text = CSS.read_text(encoding="utf-8")
    parsed = rules(text)

    dark = tokens(parsed, ":root")
    light = dict(dark)
    light.update(tokens(parsed, '[data-theme="light"]'))

    for theme_name, table in (("dark", dark), ("light", light)):
        page = parse_colour(resolve(table["--bedrock"], table))[:3]
        panel = over(resolve(table["--overlay"], table), page)

        for selector, label, token, tint in STATUS:
            missing = [name for name in (token, tint) if name not in table]
            if missing:
                check(f"{theme_name}: {selector} — {label}", False,
                      f"undefined token(s): {', '.join(missing)} — an "
                      f"undefined var() falls through to whatever follows it")
                continue

            background = over(resolve(table[tint], table), panel)
            colour = resolve(table[token], table)
            ratio = contrast(over(colour, background), background)
            check(
                f"{theme_name}: {selector} — {label}",
                ratio >= AA,
                f"{ratio:.2f}:1 on its own tint, below the {AA}:1 AA ratio "
                f"({token} = {colour})",
            )


#: The severity colours on the tab hover card (#583), and the card's own
#: background — which is `--panel`, not the `--overlay` every other floating
#: surface uses, so measuring it with the rest would measure the wrong thing.
#:
#: (rule, what it is, the token its text uses).
TAB_TIP = [
    (".tab-tip-alert-info", "a pending action still far off", "--on-surface-dim"),
    (".tab-tip-alert-warning", "a pending action inside the flash window", "--warn"),
    (".tab-tip-alert-critical", "a pending action in its last minute", "--error"),
    (".tab-tip-alert-source", "the command that started it", "--on-surface-dim"),
]


def test_the_hover_card_severities_read_in_both_themes() -> None:
    """
    "Reload in 4:12" has to be legible on the card, not merely coloured.

    The card is the third place a pending action is shown, after the status
    bar and the tab badge, and the first on `--panel`. A colour that clears
    AA on the overlay is not thereby cleared here.
    """
    print("\n-- The pending row on the tab hover card --")
    parsed = rules(CSS.read_text(encoding="utf-8"))

    dark = tokens(parsed, ":root")
    light = dict(dark)
    light.update(tokens(parsed, '[data-theme="light"]'))

    for theme_name, table in (("dark", dark), ("light", light)):
        card = parse_colour(resolve(table["--panel"], table))[:3]
        for selector, label, token in TAB_TIP:
            if token not in table:
                check(f"{theme_name}: {selector} — {label}", False,
                      f"undefined token {token} — an undefined var() falls "
                      f"through to whatever follows it")
                continue
            colour = resolve(table[token], table)
            ratio = contrast(over(colour, card), card)
            check(
                f"{theme_name}: {selector} — {label}",
                ratio >= AA,
                f"{ratio:.2f}:1 on the hover card, below the {AA}:1 AA ratio "
                f"({token} = {colour})",
            )


def test_no_status_colour_is_written_as_a_literal() -> None:
    """
    The rule that should have stopped this, applied to foregrounds.

    A literal hex is legitimate in plenty of places — #fff on a filled button,
    the ANSI palette, the terminal preview — so this checks the rules that are
    *about* status rather than every colour in the file.
    """
    # Comments stripped first: this file explains why these values are tokens
    # now, and a check that counts the explanation as a violation is a check
    # that punishes writing one down.
    text = re.sub(r"/\*.*?\*/", "", CSS.read_text(encoding="utf-8"), flags=re.S)

    # The dark-theme values that used to be pasted around, and the light-theme
    # ones, so a fix in the wrong direction is caught too.
    literals = ("#f9e2af", "#a6e3a1", "#6b4400", "#0f5c26")
    lowered = text.lower()

    for literal in literals:
        occurrences = lowered.count(literal)
        # One each: the token definitions themselves.
        check(f"{literal} appears only where the token is defined",
              occurrences <= 1,
              f"{occurrences} occurrences — a status colour pasted into a rule "
              f"cannot follow the theme")

    check("there is no var(--success), which was never defined",
          "--success" not in lowered,
          "a var() with no definition falls through to its fallback, so the "
          "fallback was always what rendered")


def test_the_application_icon() -> None:
    """
    The tray icon, measured against the taskbar it sits on.

    Same principle as everything above and the same failure mode: the icon was
    drawn in the interface's button indigo, which is 2.59:1 on a dark taskbar —
    present, and hard to pick out. Nothing raises over an icon nobody can see.

    Dark is what is optimised for, deliberately. Windows does not report the
    taskbar theme in any way worth relying on, and a tray icon is one bitmap,
    so one of the two backgrounds has to be chosen rather than adapted to.
    """
    try:
        from backend import branding
    except Exception as exc:                     # pragma: no cover
        check("the icon module imports", False, str(exc))
        return

    accent = branding.ACCENT[:3]
    # A dark Windows taskbar. Not #000: it is a dark grey with transparency
    # over the wallpaper, and #202020 is the usual result.
    ratio = contrast(accent, (32, 32, 32))
    check("the icon reads on a dark taskbar", ratio >= AA,
          f"{ratio:.2f}:1 — the mark is there but hard to pick out")

    check("the mark fills its square", branding.FILL >= 0.95,
          f"FILL is {branding.FILL}, leaving a margin that is mostly wasted "
          f"at 16 pixels")
    check("without being cropped", branding.FILL <= 1.0,
          f"FILL of {branding.FILL} scales the mark past the canvas, which "
          f"clips line art visibly")

    # And the rendered image really is the colour claimed, rather than the
    # constant having been changed in one place and not the other.
    try:
        image = branding.app_image(64)
        opaque = [p for p in image.getdata() if p[3] > 200]
        check("the rendered icon uses that colour",
              opaque and max(abs(a - b) for a, b in zip(opaque[len(opaque) // 2][:3], accent)) <= 12,
              f"rendered {opaque[len(opaque) // 2] if opaque else None}, expected {accent}")
        box = image.getbbox()
        check("and occupies the full width of its canvas",
              box and (box[2] - box[0]) >= 62, f"bounding box {box}")
    except Exception as exc:
        check("the icon renders", False, str(exc))


def main() -> int:
    print("\n" + "=" * 52)
    print("  Colour contrast")
    print("=" * 52)

    for test in (test_no_hardcoded_overlay_backgrounds, test_overlay_text_meets_aa,
                 test_status_colours_read_in_both_themes,
                 test_the_hover_card_severities_read_in_both_themes,
                 test_no_status_colour_is_written_as_a_literal,
                 test_the_application_icon):
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
