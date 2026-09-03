"""
release_notes.py — The release body and checksum for a tagged build.

CI runs this after the executable is built (#443, #445):

    python tools/release_notes.py --version 1.0.1 --exe dist/ShellMate-Portable.exe

It writes two files beside the executable:

- ``ShellMate-Portable.exe.sha256`` — the checksum the in-app updater
  verifies a download against before anything is executed.
- ``release-notes.md`` — the matching section of the manual's What's new
  page, which becomes the GitHub release body. The updater shows this text
  in its modal, so the notes a person reads before updating are the same
  notes the manual carries afterwards: one source, no drift.

A version with no section in the page produces a short placeholder rather
than an empty release.
"""

import argparse
import hashlib
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAGE = ROOT / "frontend" / "docs" / "whats-new.md"


def section_for(version: str, text: str) -> str:
    """The body under ``## <version>`` up to the next ``## `` heading."""
    version = version.lstrip("vV")
    pattern = re.compile(rf"^## {re.escape(version)}\s*$", re.M)
    match = pattern.search(text)
    if not match:
        return ""
    rest = text[match.end():]
    nxt = re.search(r"^## ", rest, re.M)
    body = rest[: nxt.start()] if nxt else rest
    return body.strip()


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    parser.add_argument("--exe", required=True)
    parser.add_argument("--out", default="")
    parser.add_argument("--unsigned", action="store_true",
                        help="say in the notes that the build carries no signature (#518)")
    args = parser.parse_args(argv)

    exe = Path(args.exe)
    if not exe.exists():
        print(f"no executable at {exe}", file=sys.stderr)
        return 1
    out_dir = Path(args.out) if args.out else exe.parent

    digest = sha256_of(exe)
    (out_dir / (exe.name + ".sha256")).write_text(f"{digest}  {exe.name}\n", encoding="utf-8")

    page = PAGE.read_text(encoding="utf-8") if PAGE.exists() else ""
    body = section_for(args.version, page)
    if not body:
        body = f"ShellMate {args.version.lstrip('vV')}. See the What's new page in the bundled manual."
    notes = f"{body}\n\n---\n`{exe.name}` SHA-256: `{digest}`\n"
    if args.unsigned:
        notes += ("\nThis build is not code-signed (#518). Windows will show an unknown "
                  "publisher; check the SHA-256 above against the file you downloaded.\n")
    (out_dir / "release-notes.md").write_text(notes, encoding="utf-8")
    print(f"sha256 {digest}")
    print(f"notes  {len(body)} chars")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
