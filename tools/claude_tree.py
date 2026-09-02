"""
claude_tree.py — The project tree in CLAUDE.md, generated rather than typed.

The structure block in CLAUDE.md drifted until it listed a file that did not
exist and omitted thirty that did (#427). It is generated now: each module's
own header line ("name.py — what it is") is its description, so the tree
cannot describe a module the module does not describe itself. The handful
whose docstrings take another shape are named in FALLBACK below.

    python tools/claude_tree.py            # print the block
    python tools/claude_tree.py --write    # replace the block in CLAUDE.md
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WIDTH = 66

#: Modules whose first docstring line is prose rather than "name — what".
FALLBACK = {
    "providers.py":      "Model discovery per provider, cached to models.json",
    "base.py":           "ConnectionHandler contract and ConnectionParams",
    "manager.py":        "Session lifecycle and the transport registry",
    "serial_handler.py": "Serial console via pyserial",
    "sftp.py":           "File transfer over an existing SSH transport",
    "ssh_handler.py":    "SSH via paramiko: keys, jump host, second channel",
    "telnet_handler.py": "Telnet over a raw socket, with IAC negotiation",
    "ansi.py":           "Undo escape sequences, backspace and bare CR",
    "buffer.py":         "Rolling per-session screen buffer",
    "outbound.py":       "The one door out: redaction before any AI call",
    "redact.py":         "Secret-pattern redaction for logs and prompts",
    "transcript.py":     "Prompt detection and command segmentation",
}

TOP = [
    ("CLAUDE.md", "This file — project spec and instructions"),
    ("README.md", "User and builder documentation"),
    ("requirements.txt", "Runtime dependency floors"),
    ("requirements.lock", "Exact versions a release is built from"),
    ("requirements-dev.txt", "Build, vendoring and test dependencies"),
    ("build.spec", "PyInstaller definition; writes build_info.json"),
    ("run.py", "Entry point — server on a thread, window on main"),
    ("Dockerfile, docker-compose.yml", "Server deployment (token required)"),
    (".github/workflows/ci.yml", "Tests, build, sign, release"),
    ("test_*.py", "Standalone test scripts; tools/run_tests.py runs all"),
]

FRONTEND = [
    ("index.html", "The one page: tab bar, split panes, every panel"),
    ("css/style.css", "All styling; dark and light themes from tokens"),
    ("docs/*.md", "The bundled manual, rendered offline by docs.js"),
    ("vendor/", "xterm.js, addons, fonts — no CDN at runtime"),
]


def describe(path: Path) -> str:
    """The module's own one-line description, cut at a word boundary."""
    text = ""
    try:
        head = path.read_text(encoding="utf-8").splitlines()[:6]
    except OSError:
        head = []
    for line in head:
        m = re.match(r'^\s*(?:"""|\*|/\*\*)?\s*([\w.]+\.(?:py|js))\s+[—-]+\s+(.+?)\s*$', line)
        if m:
            text = m.group(2)
            break
    text = text or FALLBACK.get(path.name, "")
    text = text.rstrip(".")
    if len(text) > WIDTH:
        cut = text[:WIDTH].rsplit(" ", 1)[0]
        text = cut.rstrip(",;:-— ") + "…"
    return text


def _row(prefix: str, name: str, desc: str, pad: int) -> str:
    return f"{prefix}{name:{pad}s}" + (f" # {desc}" if desc else "")


def tree() -> str:
    lines = ["shellmate/"]
    for name, desc in TOP:
        lines.append(_row("├── ", name, desc, 28))

    lines.append("├── tools/")
    for f in sorted((ROOT / "tools").glob("*.py")):
        lines.append(_row("│   ├── ", f.name, describe(f), 24))
    lines.append("├── relay/                       # Cloudflare Worker: in-app feedback → GitHub issues")

    lines.append("├── backend/")
    for f in sorted(p for p in (ROOT / "backend").glob("*.py") if p.name != "__init__.py"):
        lines.append(_row("│   ├── ", f.name, describe(f), 24))
    for sub in ("ai", "connections", "session"):
        lines.append(f"│   ├── {sub}/")
        for f in sorted(p for p in (ROOT / "backend" / sub).glob("*.py") if p.name != "__init__.py"):
            lines.append(_row("│   │   ├── ", f.name, describe(f), 20))

    lines.append("├── frontend/")
    for name, desc in FRONTEND:
        lines.append(_row("│   ├── ", name, desc, 24))
    lines.append("│   └── js/")
    for f in sorted((ROOT / "frontend" / "js").glob("*.js")):
        lines.append(_row("│       ├── ", f.name, describe(f), 20))
    lines.append("└── profiles/examples.json       # Example connection profile")
    return "\n".join(lines)


BLOCK_HEAD = (
    "## Project structure\n\n"
    "Generated from the tree and each module's own header line — regenerate\n"
    "rather than hand-edit (`python tools/claude_tree.py --write`). A module\n"
    "with no description has no header line yet; give it one.\n\n"
)


def write_block() -> None:
    path = ROOT / "CLAUDE.md"
    text = path.read_text(encoding="utf-8")
    m = re.search(r"## Project structure\n\n.*?```\n.*?\n```\n", text, re.S)
    if not m:
        raise SystemExit("CLAUDE.md has no '## Project structure' block to replace")
    text = text[:m.start()] + BLOCK_HEAD + "```\n" + tree() + "\n```\n" + text[m.end():]
    path.write_text(text, encoding="utf-8")


def main(argv: list[str]) -> int:
    if "--write" in argv:
        write_block()
        print("CLAUDE.md project structure regenerated")
    else:
        print(BLOCK_HEAD + "```\n" + tree() + "\n```")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
