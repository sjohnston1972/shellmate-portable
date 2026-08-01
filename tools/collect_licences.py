"""
collect_licences.py — Gather the licence texts ShellMate has to redistribute.

ShellMate ships as one executable containing roughly thirty Python libraries,
eight font families and xterm.js. Several of those licences require their text
to travel with the thing they cover — the OFL says so explicitly, the LGPL and
MPL both require the recipient be given the licence and told the component is
in there. Naming them in a manual page is not the same as shipping them.

So this writes ``frontend/docs/licences/`` from the packages actually installed
and the fonts actually vendored, and a ``MANIFEST.json`` describing what it
found. ``frontend/`` is bundled whole by build.spec, so anything written here
is inside the executable and reachable offline — which matters, because an
attribution nobody can reach has not really been given.

Run from the repository root, alongside the asset vendoring:

    python tools/collect_licences.py

Two things this deliberately does *not* do:

**Guess.** A component whose licence file cannot be found is recorded as
missing and listed in the output rather than filled in from its metadata
classifier. "Apache Software License" in a classifier is not the Apache
licence text, and a plausible-looking file that is not the real one is worse
than an obvious gap.

**Decide.** It reports what is bundled. Whether the LGPL obligations for a
--onefile binary are met is a question for somebody qualified, and the manual
page says so in those words.
"""

import importlib.metadata as metadata
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
OUTPUT_DIR = REPO_ROOT / "frontend" / "docs" / "licences"
FONTS_DIR = REPO_ROOT / "frontend" / "vendor" / "fonts"

#: Every Python distribution that ends up inside the executable. Written out
#: rather than derived from `pip freeze`, because a development environment
#: holds things the build does not — pytest, playwright, fonttools — and
#: attributing what is not shipped is its own kind of wrong.
BUNDLED_PACKAGES = (
    # Transport and crypto
    "paramiko", "cryptography", "bcrypt", "PyNaCl", "pyserial",
    # Web server
    "fastapi", "starlette", "uvicorn", "pydantic", "pydantic_core",
    "annotated-types", "anyio", "h11", "httptools", "sniffio", "click",
    "websockets", "python-multipart", "typing_extensions",
    # HTTP client
    "httpx", "httpcore", "certifi", "idna",
    # Desktop shell
    "pywebview", "pystray", "Pillow", "proxy_tools", "clr_loader",
    "pythonnet", "cffi", "pycparser", "six",
    # Configuration
    "python-dotenv",
)

#: Where to fetch a licence a wheel did not ship one for.
#:
#: Every licence here requires its text to be redistributed — MIT and BSD say
#: so as plainly as the LGPL does ("the above copyright notice and this
#: permission notice shall be included in all copies"). So a package with no
#: licence file in its wheel is a gap to fill from upstream, not a package to
#: quietly leave out.
UPSTREAM_LICENCES = {
    "pyserial":    "https://raw.githubusercontent.com/pyserial/pyserial/master/LICENSE.txt",
    "proxy_tools": "https://raw.githubusercontent.com/jtushman/proxy_tools/master/LICENSE.txt",
}

#: Where each vendored font family's licence lives in the google/fonts
#: repository. The families come from FONT_STYLESHEETS in vendor_assets.py;
#: the directory prefix encodes the licence, which is why the mapping is
#: written out rather than guessed from the family name.
FONT_LICENCES = {
    "Fira Code":                "https://raw.githubusercontent.com/google/fonts/main/ofl/firacode/OFL.txt",
    "Inconsolata":              "https://raw.githubusercontent.com/google/fonts/main/ofl/inconsolata/OFL.txt",
    "Inter":                    "https://raw.githubusercontent.com/google/fonts/main/ofl/inter/OFL.txt",
    "JetBrains Mono":           "https://raw.githubusercontent.com/google/fonts/main/ofl/jetbrainsmono/OFL.txt",
    # Relicensed from Apache to OFL in 2023, along with the rest of the Roboto
    # family. Which is why these are looked up rather than derived: the licence
    # is not a property of the family name.
    "Roboto Mono":              "https://raw.githubusercontent.com/google/fonts/main/ofl/robotomono/OFL.txt",
    "Source Code Pro":          "https://raw.githubusercontent.com/google/fonts/main/ofl/sourcecodepro/OFL.txt",
    "Space Grotesk":            "https://raw.githubusercontent.com/google/fonts/main/ofl/spacegrotesk/OFL.txt",
    "Ubuntu Mono":              "https://raw.githubusercontent.com/google/fonts/main/ufl/ubuntumono/UFL.txt",
    # Not in google/fonts at all — it lives with the icon set it belongs to.
    "Material Symbols Outlined": "https://raw.githubusercontent.com/google/material-design-icons/master/LICENSE",
}

#: xterm.js and its addons, vendored into frontend/vendor/ by vendor_assets.py.
SCRIPT_LICENCES = {
    "xterm.js 5.3.0": "https://raw.githubusercontent.com/xtermjs/xterm.js/5.3.0/LICENSE",
}

#: Fonts ShellMate modifies. Material Symbols is subsetted at build time, which
#: Apache 2.0 permits with a notice of modification — this is that notice.
#: Nothing under the OFL is subsetted, and nothing under it can be while it
#: keeps a Reserved Font Name.
MODIFIED = {
    "Material Symbols Outlined":
        "Subsetted by tools/vendor_assets.py to the icons ShellMate uses, "
        "reducing the file from about 3.9 MB to 190 KB. No glyph outlines are "
        "altered. Apache 2.0 permits modification with notice; this is that "
        "notice.",
}


def licence_text_for(name: str) -> tuple[str, str, str]:
    """
    Return (version, declared licence, licence text) for an installed package.

    The text is read from whatever the wheel shipped — the metadata's
    ``Licence-File`` entries, then the usual filenames in the dist-info. Some
    distributions embed the whole licence in the ``License:`` metadata field
    instead, which is used as a last resort.
    """
    try:
        dist = metadata.distribution(name)
    except metadata.PackageNotFoundError:
        return "", "", ""

    meta = dist.metadata
    declared = (meta.get("License-Expression") or meta.get("License") or "").strip()

    # A handful of packages put the entire licence in the License: field. That
    # is a licence text, not a name, so treat a long value as the text.
    embedded = declared if declared.count("\n") > 2 else ""
    if embedded:
        declared = _classifier_licence(meta) or "see text"

    if not declared:
        declared = _classifier_licence(meta) or "not declared"

    text = ""
    for candidate in _licence_file_candidates(dist):
        try:
            body = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if body.strip():
            text += ("\n\n" + "-" * 70 + "\n\n" if text else "") + body
    return dist.version, declared, text or embedded


def _classifier_licence(meta) -> str:
    for classifier in meta.get_all("Classifier") or []:
        if classifier.startswith("License ::"):
            return classifier.split("::")[-1].strip()
    return ""


def _licence_file_candidates(dist) -> list[Path]:
    """Every file in a distribution that looks like a licence."""
    base = Path(str(dist.locate_file("")))
    found: list[Path] = []

    # PEP 639 records these explicitly; wheels built before it usually still
    # put the files in the dist-info directory.
    for entry in dist.files or []:
        name = Path(str(entry)).name.upper()
        if name.startswith(("LICENSE", "LICENCE", "COPYING", "NOTICE")):
            path = base / str(entry)
            if path.is_file():
                found.append(path)

    return found


def fetch(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "ShellMate-licences"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8", errors="replace")


def _from_upstream(name: str) -> str:
    """Fetch a licence for a package whose wheel did not carry one."""
    url = UPSTREAM_LICENCES.get(name)
    if not url:
        return ""
    try:
        return f"Retrieved from {url}\n\n{fetch(url)}"
    except (urllib.error.URLError, OSError) as exc:
        print(f"      could not fetch {url}: {exc}")
        return ""


#: The manual page, everything above the generated attribution tables.
#:
#: Written here rather than kept as a hand-edited legal.md so the prose and
#: the tables cannot drift apart — an attribution list that no longer matches
#: what is bundled is worse than none, because it reads as a checked one.
LEGAL_HEADER = """\
# Legal and licences

## Copyright

ShellMate is owned by **Foundry Networks and Services**.

Copyright © 2025–2026 Foundry Networks and Services. All rights reserved.

Contact: **support@foundry-ns.com**

ShellMate itself is proprietary software and is not open source. It is
supplied to you under the terms agreed with Foundry Networks and Services. You
may use it within your organisation; you may not redistribute it, sell it, or
publish modified copies. The third-party components listed below keep their
own licences, which are reproduced in full and are unaffected by this.

## No warranty

**ShellMate is provided "as is", without warranty of any kind, express or
implied**, including but not limited to the warranties of merchantability,
fitness for a particular purpose and non-infringement.

That disclaimer is worth reading rather than skimming, because of what this
tool does:

- It **types commands into live network devices**, including on connect — the
  paging command is sent automatically once a platform is identified.
- It can **broadcast a command to every open session at once**.
- It **suggests commands generated by a language model**, which can be
  confidently wrong, and which has only the text on your screen to go on.
- It **stores credentials**, and offers an option to store them unencrypted.
- It **captures device configurations to disk**, including whatever secrets
  those configurations contain.

**You remain responsible for what is sent to your equipment.** Every command
ShellMate proposes is shown to you before it is sent and requires your
approval. Nothing in this software substitutes for knowing what a command
does on the device in front of you, for change control, or for a maintenance
window. To the maximum extent permitted by law, Foundry Networks and Services
accepts no liability for loss of service, loss of data, loss of profit or any
other damage arising from the use of this software.

## Sending session content to AI providers

When you use the assistant with a cloud provider, **the recent contents of
your terminal are sent to that provider** — Anthropic, OpenAI, xAI or DeepSeek
depending on which you have selected. That is how it can answer questions
about what is on your screen.

What this means in practice:

- Device output leaves your machine and is processed under **that provider's
  terms and privacy policy**, not this one. Review theirs before using the
  assistant on anything you are not free to share.
- ShellMate masks what it recognises as credentials on the way out, but that
  is pattern matching. A secret in a form it does not recognise goes through.
- Running **Ollama locally** sends nothing anywhere. It is the option to
  choose where the content cannot leave the building.
- The assistant can be switched off entirely under **Settings → AI**.

## Third-party components

ShellMate redistributes the components below inside its executable. Each keeps
its own licence; the full text of every one is bundled with the application in
`frontend/docs/licences/` and is reproduced here in the folder next to this
page.

### A note on the LGPL components

**paramiko** and **pystray** are licensed under the LGPL, and are compiled
into a single-file executable. The LGPL permits this in a proprietary product
but attaches conditions to distribution — broadly, that the recipient is told
the libraries are present, is given their licence text, and is able to replace
them with their own build.

Foundry Networks and Services satisfies this by:

- naming both libraries and their versions here, and shipping their licence
  texts with the application;
- using both **unmodified**, at the pinned versions listed below, which are
  available from PyPI and from their upstream repositories;
- offering, on request to support@foundry-ns.com, a **folder build** of
  ShellMate in which these libraries are separate files that can be
  substituted with your own build. `build.spec` produces one with
  `ONEFILE = False`.

*This page is a statement of what is bundled and how, written by the
engineers who bundled it. It is not legal advice, and the LGPL position in
particular should be confirmed by someone qualified before any external
release.*

"""


def write_legal_page(manifest: list[dict]) -> None:
    """Write frontend/docs/legal.md from the components actually collected."""
    lines = [LEGAL_HEADER]

    sections = (
        ("python", "### Python libraries",
         "Bundled into the executable by PyInstaller."),
        ("script", "### Frontend libraries",
         "Vendored into `frontend/vendor/`; no CDN is used at runtime."),
        ("font", "### Fonts",
         "Vendored into `frontend/vendor/fonts/`. The OFL requires its text to "
         "accompany the font files, which is why they are bundled rather than "
         "only named."),
    )

    for kind, heading, blurb in sections:
        rows = [c for c in manifest if c["kind"] == kind]
        if not rows:
            continue
        lines.append(f"{heading}\n\n{blurb}\n")
        lines.append("| Component | Version | Licence |")
        lines.append("|---|---|---|")
        for row in sorted(rows, key=lambda c: c["component"].lower()):
            lines.append(f"| {row['component']} | {row['version'] or '—'} "
                         f"| {row['licence']} |")
        lines.append("")

    modified = [c for c in manifest if c["modified"]]
    if modified:
        lines.append("### Components ShellMate modifies\n")
        for row in modified:
            lines.append(f"**{row['component']}** — {row['modified']}\n")

    lines.append(
        "---\n\n"
        f"*Generated by `tools/collect_licences.py` from the packages actually "
        f"installed and the fonts actually vendored — {len(manifest)} "
        f"components. Re-run it after changing a dependency.*\n")

    (REPO_ROOT / "frontend" / "docs" / "legal.md").write_text(
        "\n".join(lines), encoding="utf-8")
    print(f"  wrote frontend/docs/legal.md ({len(manifest)} components)")


def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    manifest: list[dict] = []
    missing: list[str] = []

    print(f"Collecting licences into {OUTPUT_DIR}\n")

    print("Python packages:")
    for name in BUNDLED_PACKAGES:
        version, declared, text = licence_text_for(name)
        if not version:
            print(f"  ?  {name:22} not installed - skipped")
            continue

        if not text:
            text = _from_upstream(name)

        filename = ""
        if text:
            filename = f"{name.lower()}.txt"
            (OUTPUT_DIR / filename).write_text(
                f"{name} {version}\nDeclared licence: {declared}\n"
                f"{'=' * 70}\n\n{text}\n", encoding="utf-8")
        else:
            missing.append(f"{name} ({declared})")

        manifest.append({
            "component": name, "version": version, "licence": declared,
            "kind": "python", "file": filename,
            "modified": "",
        })
        mark = "ok" if text else "!!"
        print(f"  {mark}  {name:22} {version:12} {declared[:38]}")

    print("\nFonts:")
    for family, url in FONT_LICENCES.items():
        filename = f"font-{family.lower().replace(' ', '-')}.txt"
        try:
            body = fetch(url)
        except (urllib.error.URLError, OSError) as exc:
            print(f"  !!  {family:26} could not fetch ({exc})")
            missing.append(f"{family} (font licence)")
            continue

        header = f"{family}\nSource: {url}\n"
        if family in MODIFIED:
            header += f"\nModification notice:\n{MODIFIED[family]}\n"
        (OUTPUT_DIR / filename).write_text(
            f"{header}{'=' * 70}\n\n{body}\n", encoding="utf-8")

        declared = ("SIL Open Font License 1.1" if url.endswith("OFL.txt")
                    else "Ubuntu Font Licence 1.0" if url.endswith("UFL.txt")
                    else "Apache-2.0")
        if family == "Material Symbols Outlined":
            declared = "Apache-2.0"
        manifest.append({
            "component": family, "version": "", "licence": declared,
            "kind": "font", "file": filename,
            "modified": MODIFIED.get(family, ""),
        })
        print(f"  ok  {family:26} {declared}")

    print("\nFrontend libraries:")
    for label, url in SCRIPT_LICENCES.items():
        filename = f"{label.split()[0].lower().replace('.', '')}.txt"
        try:
            body = fetch(url)
        except (urllib.error.URLError, OSError) as exc:
            print(f"  !!  {label:26} could not fetch ({exc})")
            missing.append(label)
            continue
        (OUTPUT_DIR / filename).write_text(
            f"{label}\nSource: {url}\n{'=' * 70}\n\n{body}\n", encoding="utf-8")
        manifest.append({
            "component": label, "version": "", "licence": "MIT",
            "kind": "script", "file": filename, "modified": "",
        })
        print(f"  ok  {label:26} MIT")

    (OUTPUT_DIR / "MANIFEST.json").write_text(
        json.dumps({"components": manifest}, indent=2), encoding="utf-8")

    write_legal_page(manifest)

    print(f"\n{len(manifest)} components, "
          f"{sum(1 for c in manifest if c['file'])} with licence text.")

    if missing:
        # Named individually rather than counted. Each one is a licence that
        # requires its text to travel with the software, and a count is not
        # something anybody can act on.
        print("\n!! These require their licence text to be redistributed and "
              "none was found:")
        for item in missing:
            print(f"     {item}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
