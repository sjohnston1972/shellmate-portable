"""
branding.py — One source for the application icon.

ShellMate appears in three places on a Windows desktop — the system tray, the
taskbar and the window's own title bar — and until now only the first had an
icon at all.  The other two got whatever the toolkit defaulted to, so the
three did not agree with each other.

Everything is derived here, at runtime and at build time, from a single source
image.  Not a checked-in ``.ico`` beside a checked-in ``.png``: two files
someone has to remember to regenerate together are two files that drift apart,
which is the same failure as the hardcoded colours in ``style.css``.

Three things happen to the source mark:

**It is recoloured** to the interface accent.  The source is a flat black
silhouette with an alpha channel, so this is exact — the alpha is kept and
only the colour replaced.

**It is trimmed.**  The source has a wide transparent margin, roughly a third
of the canvas on each side.  Scaled into a 16-pixel tray slot that margin is
most of the icon, and the mark inside it is unreadable.  Cropping to the
actual glyph and re-padding to a small, deliberate margin makes it visibly
larger at every size without changing anything else.

**It is rendered large.**  128 px rather than 64.  Windows draws the tray at
16, 24 or 32 px whatever it is given, so this buys sharpness on a high-DPI
display rather than a bigger icon — the size that matters is the trimming
above.  For the ``.ico``, every size Windows asks for is embedded, because
letting it downscale a single 256 px image is what produces a smudge in the
taskbar.
"""

import logging
from pathlib import Path

from backend import paths

logger = logging.getLogger(__name__)

# --primary-container from style.css. The accent the interface already uses
# for anything that belongs to ShellMate rather than to the device.
ACCENT = (79, 70, 229, 255)

# What the source mark is scaled to inside its square, leaving a margin so it
# does not touch the edge of the tray slot.
FILL = 0.86

# Every size Windows may ask for, embedded rather than downscaled on demand.
ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)

# The tray takes an image directly, at a size chosen for high-DPI sharpness.
TRAY_SIZE = 128

SOURCE_NAME = "Untitled-removebg-preview.png"
ICON_NAME = "shellmate.ico"


def source_path() -> Path:
    """
    Where the source mark lives.

    Via ``resource_dir()`` so it is found inside the unpacked bundle when
    frozen, and never derived from ``__file__``.
    """
    return paths.frontend_dir() / SOURCE_NAME


def icon_path() -> Path:
    """
    Where the generated multi-size ``.ico`` lives.

    Beside the source mark, in the bundled frontend, because that is what gets
    packaged.  ``build.spec`` writes it before Analysis runs, so the file the
    executable's icon is taken from is the same one that ships inside it.
    """
    return paths.frontend_dir() / ICON_NAME


def app_image(size: int = TRAY_SIZE):
    """
    Return the application icon as a PIL image, in the accent colour.

    Args:
        size: Width and height in pixels.

    Returns:
        An RGBA image. Falls back to a drawn placeholder if the source mark is
        missing or unreadable — a missing logo must never stop the application
        starting, since the icon is the least important thing it does.
    """
    from PIL import Image

    try:
        source = Image.open(source_path()).convert("RGBA")
    except Exception as exc:
        logger.info("Could not load the application mark (%s); drawing a fallback", exc)
        return _fallback(size)

    try:
        # Trim the transparent margin — most of the source canvas is empty,
        # and at 16 px that margin is the icon.
        box = source.getbbox()
        if box:
            source = source.crop(box)

        # Recolour: keep the alpha, replace everything else. Exact for a flat
        # silhouette, which is what the source is.
        tint = Image.new("RGBA", source.size, ACCENT)
        tint.putalpha(source.getchannel("A"))

        # Fit inside the square without distorting it, then centre.
        target = max(1, int(size * FILL))
        scale = min(target / tint.width, target / tint.height)
        tint = tint.resize(
            (max(1, round(tint.width * scale)), max(1, round(tint.height * scale))),
            Image.LANCZOS,
        )

        canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        canvas.paste(tint, ((size - tint.width) // 2, (size - tint.height) // 2), tint)
        return canvas
    except Exception as exc:
        logger.info("Could not render the application icon (%s); drawing a fallback", exc)
        return _fallback(size)


def write_ico(destination: Path | None = None) -> Path | None:
    """
    Write the multi-size ``.ico`` used by the executable, taskbar and window.

    Called from ``build.spec`` so the icon is regenerated on every build from
    the same source the tray uses, rather than being a second artefact someone
    has to remember to update.

    Returns:
        The path written, or None if it could not be written — a build should
        not fail over an icon.
    """
    destination = destination or icon_path()
    try:
        largest = app_image(max(ICO_SIZES))
        destination.parent.mkdir(parents=True, exist_ok=True)
        largest.save(destination, format="ICO",
                     sizes=[(size, size) for size in ICO_SIZES])
        logger.info("Wrote %s", destination)
        return destination
    except Exception as exc:
        logger.warning("Could not write the application icon: %s", exc)
        return None


def _fallback(size: int):
    """A plain accent-coloured tile, so there is always something to show."""
    from PIL import Image, ImageDraw

    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    inset = max(1, size // 16)
    draw.rounded_rectangle(
        (inset, inset, size - inset, size - inset),
        radius=max(2, size // 5), fill=ACCENT,
    )
    draw.text((size // 3, size // 3), ">_", fill=(255, 255, 255, 255))
    return image
