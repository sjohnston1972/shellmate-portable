"""
test_icons.py — Every icon the interface asks for exists in the font it ships.

Material Symbols is subsetted at build time, from about 3.9 MB to 190 KB, by
matching identifier-shaped tokens in the frontend against the full font's glyph
names. That works until somebody adds an icon and does not re-run
`tools/vendor_assets.py` — at which point the icon renders as its own name in
plain text, in the middle of a button, and **nothing anywhere reports an
error**. It is a visual defect that reaches a user before it reaches a
developer.

The vendoring script does verify its own output, with HarfBuzz, which is the
right check. The problem is *when*: it runs only when assets are re-downloaded,
which needs a network and is not part of anybody's edit-test loop. Adding a
`<span class="material-symbols-outlined">gavel</span>` to a page and running
the test suite gave no hint at all.

So this asks the same question of the font that is committed, using the same
shaping engine the browser will use. No network, no downloads.

    python test_icons.py
"""

import importlib.util
import io
import re
import sys
from pathlib import Path

passed = 0
failed: list[str] = []

ROOT = Path(__file__).parent
FONTS = ROOT / "frontend" / "vendor" / "fonts"


def check(name: str, condition: bool, detail: str = "") -> None:
    global passed
    if condition:
        passed += 1
        print(f"  OK  {name}")
    else:
        failed.append(f"{name}: {detail}")
        print(f"  FAIL {name}\n       {detail}")


def _vendor_module():
    """The vendoring script, imported for its icon-name scanner."""
    spec = importlib.util.spec_from_file_location(
        "vendor_assets", ROOT / "tools" / "vendor_assets.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _icon_font() -> Path | None:
    matches = sorted(FONTS.glob("materialsymbols*.woff2"))
    return matches[0] if matches else None


def _markup_icons() -> set[str]:
    """
    Icon names written directly into markup.

    Only these, deliberately. The scanner in vendor_assets.py also intersects
    every identifier-shaped token with the full font's glyph names, which
    over-includes on purpose — but the full font is not committed, so that set
    cannot be reproduced here. Markup names are the ones that are unambiguously
    icons, and they are where a new one is nearly always added.
    """
    names: set[str] = set()
    pattern = re.compile(r"material-symbols-outlined[^>]*>\s*([a-z0-9_]+)\s*<")
    for source in (list((ROOT / "frontend").rglob("*.html"))
                   + list((ROOT / "frontend").rglob("*.js"))):
        if "vendor" in source.parts:
            continue
        names.update(pattern.findall(source.read_text(encoding="utf-8")))
    return names


def _assigned_icons() -> set[str]:
    """
    Names assigned from JavaScript rather than written into markup.

    `icon.textContent = 'dark_mode'` is invisible to a markup scan, and that is
    not hypothetical: subsetting on markup alone once dropped `dark_mode` and
    `cable`, and the theme toggle rendered the words on screen.
    """
    names: set[str] = set()
    assigned = re.compile(
        r"""(?:textContent|innerHTML)\s*=\s*['"]([a-z][a-z0-9_]{2,30})['"]""")

    # The other shape this takes: a helper that builds a button takes the icon
    # name as an argument, so the literal is at the call site and the element
    # it lands on is three lines away. `button('Show', 'visibility', ...)`.
    # Only searched in files that build icon elements at all, and only the
    # snake_case-looking arguments — a label would be capitalised.
    # The callee has to look like something that builds a button, or ordinary
    # two-string calls turn into false positives — `device`, `true` and `false`
    # all arrived that way, and a test that cries wolf gets muted.
    argument = re.compile(
        r"""\w*(?:button|btn|icon|action)\w*\(\s*['"][^'"]*['"],"""
        r"""\s*['"]([a-z][a-z0-9_]{2,30})['"]""",
        re.IGNORECASE)

    # And the third: a table of pages or menu entries, each naming its icon as
    # a property. docs.js's PAGES is the one that let `gavel` through.
    prop = re.compile(r"""icon:\s*['\"]([a-z][a-z0-9_]{2,30})['\"]""")

    for source in (ROOT / "frontend" / "js").glob("*.js"):
        text = source.read_text(encoding="utf-8")
        # A property table needs no markup of its own: the menu builder or
        # the alert code renders it. update.js and terminal.js both named
        # icons that were not in the font and this gate hid them (#426).
        names.update(prop.findall(text))
        if "material-symbols-outlined" not in text:
            continue

        for name in assigned.findall(text):
            for line in text.splitlines():
                if name in line and ("material-symbols" in line or
                                     "glyph" in line or "icon" in line.lower()):
                    names.add(name)
                    break

        names.update(argument.findall(text))
        names.update(prop.findall(text))

    return names


def _backend_icons() -> set[str]:
    """
    Icon names declared in Python, not in the frontend.

    `groups.ICONS` is the group icon picker's whole vocabulary (#180), fixed
    precisely so nothing outside the font can be stored. Declaring it in the
    backend put all of it outside the subset instead, because the subsetter
    read only `frontend/` — the failure the fixed list existed to prevent,
    relocated one directory. Both this and the subsetter now read it.
    """
    from backend.groups import ICONS
    return set(ICONS)


def test_the_font_is_there() -> None:
    print("\n-- The shipped icon font --")
    font = _icon_font()
    check("the subsetted icon font is committed", font is not None,
          f"nothing matching materialsymbols*.woff2 in {FONTS}")
    if font:
        check("and is a subset rather than the whole family",
              font.stat().st_size < 1_000_000,
              f"{font.stat().st_size:,} bytes — the full font is ~3.9 MB, so "
              f"either subsetting did not run or it shipped everything")


def test_every_icon_the_interface_uses_renders() -> None:
    """The check that would have caught `gavel`."""
    print("\n-- Every icon the frontend asks for --")

    font = _icon_font()
    if font is None:
        check("the font is available to check", False, "no font found")
        return

    try:
        import uharfbuzz as hb
        from fontTools.ttLib import TTFont
    except ImportError:
        print("       uharfbuzz not installed — skipping (pip install "
              "-r requirements-dev.txt)")
        return

    wanted = _markup_icons() | _assigned_icons() | _backend_icons()
    check("icons were found to check", len(wanted) > 20,
          f"only {len(wanted)} — the scan is probably not matching anything")

    # HarfBuzz needs the uncompressed tables, not the woff2 wrapper.
    parsed = TTFont(str(font))
    raw = io.BytesIO()
    parsed.flavor = None
    parsed.save(raw)
    parsed.close()
    shaper = hb.Font(hb.Face(raw.getvalue()))

    broken = []
    for name in sorted(wanted):
        buffer = hb.Buffer()
        buffer.add_str(name)
        buffer.guess_segment_properties()
        hb.shape(shaper, buffer)
        if len(buffer.glyph_infos) != 1:
            broken.append(name)

    check(f"all {len(wanted)} shape to a single glyph", not broken,
          f"these would render as their own names in plain text: "
          f"{', '.join(broken)}. Run: python tools/vendor_assets.py")


def main() -> int:
    print("\n" + "=" * 52)
    print("  Icons")
    print("=" * 52)

    for test in (test_the_font_is_there, test_every_icon_the_interface_uses_renders):
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
