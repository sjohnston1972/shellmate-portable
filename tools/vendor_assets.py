"""
vendor_assets.py — Download ShellMate's third-party frontend assets locally.

ShellMate is meant to run on an air-gapped or heavily filtered network, where
loading xterm.js and web fonts from a CDN either stalls for the connect
timeout or fails outright.  This script pulls those assets into
``frontend/vendor/`` so the shipped executable is entirely self-contained.

Run from the repository root:

    python tools/vendor_assets.py

Re-runnable: existing files are overwritten, so this is also how you upgrade
a pinned version.  Nothing here runs at application runtime — the output is
committed to the repository.
"""

import io
import re
import string
import sys
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
VENDOR_DIR = REPO_ROOT / "frontend" / "vendor"
FONTS_DIR = VENDOR_DIR / "fonts"
FRONTEND_DIR = REPO_ROOT / "frontend"

# Google Fonts serves different formats depending on the User-Agent. A modern
# Chrome UA gets woff2, which every browser we care about supports and which is
# roughly half the size of the woff fallback.
CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Pinned to the versions frontend/index.html already referenced, so vendoring
# is not silently also an upgrade.
SCRIPTS: dict[str, str] = {
    "xterm.css":                  "https://cdn.jsdelivr.net/npm/xterm@5.3.0/css/xterm.css",
    "xterm.js":                   "https://cdn.jsdelivr.net/npm/xterm@5.3.0/lib/xterm.js",
    "xterm-addon-fit.js":         "https://cdn.jsdelivr.net/npm/xterm-addon-fit@0.8.0/lib/xterm-addon-fit.js",
    "xterm-addon-web-links.js":   "https://cdn.jsdelivr.net/npm/xterm-addon-web-links@0.9.0/lib/xterm-addon-web-links.js",
    # Searching the buffer in front of you. History search answers a different
    # question — what did I change last Tuesday — out of SQLite, and cannot
    # reach output still in the live scrollback.
    "xterm-addon-search.js":      "https://cdn.jsdelivr.net/npm/xterm-addon-search@0.13.0/lib/xterm-addon-search.js",
}

# The two Google Fonts stylesheets index.html requested. Weight sets are copied
# verbatim from the original URLs so nothing changes visually.
FONT_STYLESHEETS: dict[str, str] = {
    "fonts.css": (
        "https://fonts.googleapis.com/css2"
        "?family=Space+Grotesk:wght@400;500;600;700;900"
        "&family=Inter:wght@400;500;600"
        "&family=JetBrains+Mono:wght@400;500"
        "&family=Fira+Code:wght@400;500"
        "&family=Source+Code+Pro:wght@400;500"
        "&family=Roboto+Mono:wght@400;500"
        "&family=Ubuntu+Mono:wght@400;700"
        "&family=Inconsolata:wght@400;500"
        "&display=swap"
    ),
    "material-symbols.css": (
        "https://fonts.googleapis.com/css2"
        "?family=Material+Symbols+Outlined:opsz,wght,FILL,GRAD@20..48,100..700,0..1,-50..200"
    ),
}


def fetch(url: str, as_text: bool = False) -> bytes | str:
    """Retrieve *url* with a browser User-Agent."""
    request = urllib.request.Request(url, headers={"User-Agent": CHROME_UA})
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = response.read()
    return payload.decode("utf-8") if as_text else payload


def vendor_scripts() -> None:
    """Download the xterm.js library, its addons, and its stylesheet."""
    VENDOR_DIR.mkdir(parents=True, exist_ok=True)
    for filename, url in SCRIPTS.items():
        data = fetch(url)
        (VENDOR_DIR / filename).write_bytes(data)
        print(f"  {filename:<28} {len(data):>8,} bytes")


def vendor_font_stylesheet(filename: str, url: str) -> list[Path]:
    """
    Download a Google Fonts stylesheet and every font file it references,
    rewriting the URLs to point at the local copies.

    Google returns one @font-face block per (weight, unicode subset) pair, each
    pointing at a versioned file on fonts.gstatic.com. We give each a stable
    local name derived from its remote path so re-running produces identical
    output rather than churning filenames.
    """
    FONTS_DIR.mkdir(parents=True, exist_ok=True)
    css = fetch(url, as_text=True)

    font_urls = sorted(set(re.findall(r"url\((https://fonts\.gstatic\.com/[^)]+)\)", css)))
    written: list[Path] = []
    total = 0

    for font_url in font_urls:
        # e.g. .../s/inter/v20/UcC73Fw.woff2 -> inter-v20-UcC73Fw.woff2
        parts = font_url.split("/s/")[-1].split("/")
        local_name = "-".join(parts).split("?")[0]

        data = fetch(font_url)
        local_path = FONTS_DIR / local_name
        local_path.write_bytes(data)
        written.append(local_path)
        total += len(data)

        css = css.replace(font_url, f"fonts/{local_name}")

    (VENDOR_DIR / filename).write_text(css, encoding="utf-8")
    print(f"  {filename:<28} {len(font_urls):>3} font files, {total:>9,} bytes")
    return written


def _frontend_sources() -> list[Path]:
    return [FRONTEND_DIR / "index.html", *(FRONTEND_DIR / "js").glob("*.js")]


# Glyph names in the font that name characters rather than icons. Every one is
# also a plausible English word, so the token scan below would otherwise pick
# them up out of prose.
NON_ICON_GLYPHS = {
    "space", "underscore", "period", "comma", "colon", "semicolon", "hyphen",
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight",
    "nine", "notdef",
}


def used_icon_names(available: set[str] | None = None) -> set[str]:
    """
    Return every Material Symbols ligature name the frontend might use.

    Icons usually appear as the name in the element body, e.g.
    ``<span class="material-symbols-outlined">settings</span>``, which the
    font renders through a ligature. But plenty are assigned from JavaScript
    instead — ``icon.textContent = 'dark_mode'``, or picked by a ternary —
    and a scan that only reads markup misses those. That is not a
    hypothetical: subsetting on the markup alone dropped ``dark_mode`` and
    ``cable``, and the theme toggle rendered the words "dark_mode" on screen.

    So when the full font is available, every identifier-shaped token in the
    frontend is intersected with the font's own glyph names. That
    over-includes a little — "search" and "close" are ordinary words as well
    as icons — but a few unnecessary glyphs cost a kilobyte each, whereas a
    missing one is a visible defect. Erring towards including is the right
    way round.

    Args:
        available: Glyph names present in the full font. When omitted, falls
            back to the markup-only scan.
    """
    names: set[str] = set()
    markup = re.compile(r"material-symbols-outlined[^>]*>\s*([a-z0-9_]+)\s*<")
    token = re.compile(r"\b([a-z][a-z0-9_]{2,30})\b")

    for source in _frontend_sources():
        if not source.exists():
            continue
        text = source.read_text(encoding="utf-8")
        names.update(markup.findall(text))
        if available:
            names.update(t for t in token.findall(text) if t in available)

    # The font names its ordinary characters too, in the usual PostScript way,
    # and several of those names are also ordinary English words that turn up
    # in comments. "space" is the one that bites: it is a real glyph name, so
    # the intersection above accepts it, but it is a character rather than a
    # ligature and the shaping check then reports it as broken. Dropping them
    # here keeps that check meaningful instead of teaching it to ignore things.
    return names - NON_ICON_GLYPHS


def subset_icon_font(font_path: Path, icon_names: set[str]) -> None:
    """
    Reduce the Material Symbols font to only the icons ShellMate uses.

    The full variable font is about 4 MB — the single largest asset in the
    build, and under --onefile it is re-extracted to a temp directory on every
    launch.  ShellMate uses roughly two dozen icons, so subsetting removes
    around 99% of it.

    Two settings matter, and getting either wrong fails *silently* — the
    icons simply render as their own names in plain text:

    ``layout_features = ["*"]``
        Current Material Symbols does not use classic ligature lookups.  The
        icon-name substitution lives in contextual rules under ``rclt``.
        Listing specific features by name (``liga``, ``calt`` ...) drops it.
        Keep everything; the font is tiny after subsetting regardless.

    ``layout_closure = False``
        With closure on, fontTools expands the requested set through the
        substitution rules.  Since spelling the icon names needs the whole
        alphabet, closure then reaches every icon spellable from those
        letters — which is essentially the entire font.  Naming the wanted
        glyphs directly and suppressing closure is the difference between a
        45% saving and a 99% one.

    Skipped with a warning if fonttools is unavailable, so the script still
    works without the extra dependency.
    """
    try:
        from fontTools import subset
        from fontTools.ttLib import TTFont
    except ImportError:
        print("    ! fonttools not installed - keeping the full icon font")
        print("      (pip install 'fonttools[woff2]' brotli to shrink it)")
        return

    original_size = font_path.stat().st_size

    font = TTFont(str(font_path))
    available = set(font.getGlyphOrder())

    # Recompute against the real glyph list: names assigned from JavaScript
    # are only discoverable by intersecting source tokens with it.
    icon_names = used_icon_names(available)

    # Glyph names match icon names exactly. The ".fill" variants back the FILL
    # axis, so keep them too or filled icons fall back to the outlined shape.
    wanted = [name for name in sorted(icon_names) if name in available]
    wanted += [f"{name}.fill" for name in sorted(icon_names) if f"{name}.fill" in available]

    missing = sorted(set(icon_names) - available)
    if missing:
        print(f"    ! not in this font, skipped: {', '.join(missing)}")

    options = subset.Options()
    options.layout_features = ["*"]
    options.layout_closure = False
    options.notdef_outline = True

    subsetter = subset.Subsetter(options=options)
    # The letters are the substitution *inputs*; the icon glyphs are the
    # outputs, named explicitly because closure is off.
    subsetter.populate(
        text=string.ascii_lowercase + string.digits + "_ ",
        glyphs=wanted,
    )
    subsetter.subset(font)

    buffer = io.BytesIO()
    font.flavor = "woff2"
    font.save(buffer)
    font.close()

    font_path.write_bytes(buffer.getvalue())
    new_size = font_path.stat().st_size
    saved = 100 * (1 - new_size / original_size)
    print(
        f"    subset to {len(icon_names)} icons: "
        f"{original_size:,} -> {new_size:,} bytes ({saved:.1f}% smaller)"
    )

    verify_icons_shape(font_path, icon_names)


def verify_icons_shape(font_path: Path, icon_names: set[str]) -> None:
    """
    Confirm every icon name still shapes to a single glyph after subsetting.

    A broken subset does not raise — the icons just quietly render as the
    literal words "settings", "search" and so on, which is the kind of thing
    that reaches a user before it reaches a developer.  HarfBuzz is the same
    shaping engine the browser uses, so if it produces one glyph per icon
    name here, the page will too.
    """
    try:
        import uharfbuzz as hb
        from fontTools.ttLib import TTFont
    except ImportError:
        print("    ? uharfbuzz not installed - skipping icon render check")
        return

    # HarfBuzz needs the uncompressed tables, not the woff2 wrapper.
    font = TTFont(str(font_path))
    raw = io.BytesIO()
    font.flavor = None
    font.save(raw)
    font.close()

    hb_font = hb.Font(hb.Face(raw.getvalue()))

    broken: list[str] = []
    for name in sorted(icon_names):
        buffer = hb.Buffer()
        buffer.add_str(name)
        buffer.guess_segment_properties()
        hb.shape(hb_font, buffer)
        if len(buffer.glyph_infos) != 1:
            broken.append(name)

    if broken:
        print(f"    !! {len(broken)} icons would render as text: {', '.join(broken)}")
        raise SystemExit("Icon subsetting broke the font - refusing to ship it.")

    print(f"    verified: all {len(icon_names)} icons shape to a single glyph")


def main() -> int:
    print(f"Vendoring into {VENDOR_DIR}")

    print("\nxterm.js and addons:")
    vendor_scripts()

    print("\nFonts:")
    for filename, url in FONT_STYLESHEETS.items():
        written = vendor_font_stylesheet(filename, url)

        # The icon font is the one worth shrinking; the text fonts are already
        # split into small per-subset files by Google.
        if filename == "material-symbols.css" and written:
            # The real set is resolved inside subset_icon_font, once the
            # font's glyph names are known.
            subset_icon_font(written[0], used_icon_names())

    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
