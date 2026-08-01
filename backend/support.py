"""
support.py — Building a diagnostic bundle worth reading.

The support link used to open a `mailto:` carrying two facts and a request
that the user go and find `shellmate.log` themselves. Almost nobody did, so
what arrived was "it didn't work" and the first reply was always the same four
questions.

This gathers what those questions ask for, and lets the user see all of it
before any of it leaves. That second part is the point: the manual already
tells people to read the log before sending it, and an instruction nobody
follows is worse than a preview nobody can avoid.

Three rules shape what may go in.

**Nothing sensitive, ever.**  No API keys, no passwords, no vault contents.
Settings come through ``get_settings_for_ui()``, which masks; the plaintext
credentials file is excluded by name and by test.  A bundle that leaked a
credential would be worse than no bundle, because it is *designed* to be
emailed.

**Device data is opt-in.**  Sections that describe ShellMate default on;
sections that contain anything about the estate it is pointed at default off,
and are redacted when included.

**Never fatal.**  A section that cannot be collected reports why, in place,
and the rest of the bundle is still produced.  Somebody assembling a support
request is already having a bad day.
"""

import io
import json
import logging
import platform
import sys
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from backend import paths
from backend.session import outbound

logger = logging.getLogger(__name__)

# How much of the application log to include. It is rewritten each launch, so
# this is normally the whole thing; the cap exists for a session that has been
# up for a fortnight.
LOG_TAIL_BYTES = 512 * 1024

# Lines of terminal output per session, when the user asks for it.
SCROLLBACK_LINES = 300


@dataclass
class Section:
    """One thing that can go in the bundle."""

    id: str
    label: str
    #: What it contains, in the user's terms. Shown beside the checkbox.
    summary: str
    #: Off by default means "this describes your estate, not ShellMate".
    default_on: bool
    #: True when it can carry device data, which the interface says out loud.
    device_data: bool
    filename: str
    collect: Callable[[dict], str]


# ---------------------------------------------------------------------------
# Collectors
#
# Each returns text. Raising is allowed — the caller turns it into a note in
# the bundle rather than a failure.
# ---------------------------------------------------------------------------


def _about(_ctx: dict) -> str:
    from backend import __init__ as _  # noqa: F401  (package marker)

    lines = [
        "ShellMate Portable — diagnostic bundle",
        f"Collected:      {time.strftime('%Y-%m-%d %H:%M:%S %Z')}",
        "",
        f"Frozen build:   {'yes' if paths.is_frozen() else 'no (running from source)'}",
        f"Executable:     {sys.executable}",
        f"Application at: {paths.app_dir()}",
        f"Data folder:    {paths.data_dir()}",
        f"Portable data:  {'no — fell back to per-user storage' if paths.data_dir_is_fallback() else 'yes'}",
        "",
        f"Python:         {sys.version.split()[0]}",
        f"Platform:       {platform.platform()}",
        f"Machine:        {platform.machine()}",
    ]
    return "\n".join(lines)


def _versions(_ctx: dict) -> str:
    """What the third-party libraries actually are, which pins most bug reports."""
    from importlib.metadata import PackageNotFoundError, version

    wanted = ["fastapi", "uvicorn", "paramiko", "cryptography", "pyserial",
              "httpx", "pywebview", "pystray", "pillow", "websockets"]
    lines = []
    for name in wanted:
        try:
            lines.append(f"{name:16} {version(name)}")
        except PackageNotFoundError:
            lines.append(f"{name:16} not installed")
    return "\n".join(lines)


def _log(_ctx: dict) -> str:
    path = paths.data_dir() / "shellmate.log"
    if not path.exists():
        return "(no log file — this run has not written one yet)"
    data = path.read_bytes()
    trimmed = data[-LOG_TAIL_BYTES:]
    prefix = "" if len(trimmed) == len(data) else \
        f"(truncated to the last {LOG_TAIL_BYTES // 1024} KB)\n\n"
    return prefix + trimmed.decode("utf-8", errors="replace")


def _settings(_ctx: dict) -> str:
    """
    Settings as the interface sees them.

    Through ``get_settings_for_ui`` rather than reading settings.json, because
    that is the function that masks secrets. Reading the file directly would
    work today and leak the day somebody adds a field.
    """
    from backend.settings_store import get_settings_for_ui

    return json.dumps(get_settings_for_ui(), indent=2)


def _platforms(_ctx: dict) -> str:
    from backend import platforms as platforms_module

    return json.dumps(
        {k: p.as_dict() for k, p in platforms_module.load_profiles().items()}, indent=2)


def _prompts(_ctx: dict) -> str:
    from backend.ai import prompt_store

    state = prompt_store.state()
    return json.dumps({
        mode: {"modified": entry["modified"],
               "has_marker": entry["has_marker"],
               "body": entry["body"]}
        for mode, entry in state["prompts"].items()
    }, indent=2)


def _snippets(_ctx: dict) -> str:
    from backend import snippets

    return json.dumps([s.as_dict() for s in snippets.load_snippets()], indent=2)


def _sessions(ctx: dict) -> str:
    """
    What is open, and what ShellMate thinks each device is.

    Hostnames are included: this section is already marked as device data, and
    a session list without names cannot be matched to anything the person
    reporting the problem says.
    """
    manager = ctx.get("session_manager")
    if manager is None:
        return "(no session manager available)"

    rows = []
    for session in manager.get_all_sessions():
        fingerprint = session.get("fingerprint") or {}
        rows.append({
            "label":           session.get("display_label", ""),
            "hostname":        session.get("hostname", ""),
            "connection_type": session.get("connection_type", ""),
            "connected_at":    session.get("connected_at", ""),
            "is_connected":    session.get("is_connected"),
            "platform":        fingerprint.get("platform", ""),
            "confidence":      fingerprint.get("confidence"),
            "identified_from": fingerprint.get("source", ""),
            "paging_sent":     fingerprint.get("paging_command", ""),
            "paging_skipped":  fingerprint.get("paging_skipped", ""),
        })
    return json.dumps(rows, indent=2) if rows else "(no sessions open)"


def _scrollback(ctx: dict) -> str:
    """
    Recent terminal output, redacted.

    Through the outbound helper, exactly like anything else that leaves the
    machine — a bundle emailed to support is the definitive case for it.
    """
    manager = ctx.get("session_manager")
    if manager is None:
        return "(no session manager available)"

    parts = []
    for session in manager._sessions.values():        # noqa: SLF001 — internal by design
        label = session.get("display_label") or session.get("hostname") or "session"
        parts.append(f"===== {label} =====")
        parts.append(outbound.session_text(session, SCROLLBACK_LINES) or "(no output)")
        parts.append("")
    return "\n".join(parts) if parts else "(no sessions open)"


def _providers(_ctx: dict) -> str:
    """Which AI providers are configured, and never with what."""
    from backend.settings_store import SECRET_FIELDS, get_settings

    providers = (get_settings().get("providers") or {})
    lines = []
    for field in sorted(providers):
        if field in SECRET_FIELDS:
            from backend.settings_store import get_effective
            lines.append(f"{field:22} {'set' if get_effective(field) else 'not set'}")
        else:
            lines.append(f"{field:22} {providers[field] or '(unset)'}")
    return "\n".join(lines)


SECTIONS: list[Section] = [
    Section("about", "About this installation",
            "Version, whether it is a frozen build, and where its data lives.",
            True, False, "about.txt", _about),
    Section("versions", "Library versions",
            "What paramiko, pywebview and the rest actually are here.",
            True, False, "versions.txt", _versions),
    Section("log", "Application log",
            "What ShellMate itself did this run. Not your session contents.",
            True, False, "shellmate.log", _log),
    Section("settings", "Settings",
            "Your preferences. API keys are masked before this is written.",
            True, False, "settings.json", _settings),
    Section("platforms", "Platform definitions",
            "The commands and aliases per device type — the commonest thing to have edited.",
            False, False, "platforms.json", _platforms),
    Section("prompts", "Assistant prompts",
            "The system prompts, and whether you have changed them.",
            False, False, "prompts.json", _prompts),
    Section("snippets", "Command library",
            "The saved broadcast commands.",
            False, False, "snippets.json", _snippets),
    Section("providers", "AI providers",
            "Which are configured. Never the keys themselves.",
            False, False, "providers.txt", _providers),
    Section("sessions", "Open sessions",
            "Device names, transports, and what each was identified as.",
            False, True, "sessions.json", _sessions),
    Section("scrollback", "Recent terminal output",
            "The last few hundred lines from each open session, with credentials masked.",
            False, True, "scrollback.txt", _scrollback),
]

SECTIONS_BY_ID = {s.id: s for s in SECTIONS}


def describe() -> list[dict]:
    """The section list, for the panel."""
    return [
        {"id": s.id, "label": s.label, "summary": s.summary,
         "default_on": s.default_on, "device_data": s.device_data}
        for s in SECTIONS
    ]


def collect(section_ids: list[str], session_manager=None) -> dict[str, str]:
    """
    Gather the requested sections.

    A section that raises becomes a note in its own place rather than taking
    the bundle down — somebody assembling a support request has already hit
    one problem today.
    """
    context = {"session_manager": session_manager}
    out: dict[str, str] = {}

    for section_id in section_ids:
        section = SECTIONS_BY_ID.get(section_id)
        if section is None:
            continue
        try:
            out[section.id] = section.collect(context)
        except Exception as exc:
            logger.warning("Could not collect '%s' for the support bundle: %s",
                           section_id, exc)
            out[section.id] = f"(could not be collected: {exc})"

    return out


def bundle_dir() -> Path:
    """Where bundles are written. Beside the log, which is what they are about."""
    return paths.data_dir() / "support"


def write_bundle(collected: dict[str, str], note: str = "") -> Path:
    """
    Write the gathered sections as one zip.

    A zip rather than a folder because ``mailto:`` cannot attach anything at
    all — the realistic flow is "here is one file, attach it", and one file is
    the only version of that people complete.

    Raises:
        OSError: The data folder could not be written to.
    """
    bundle_dir().mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = bundle_dir() / f"shellmate-support-{stamp}.zip"

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        if note.strip():
            archive.writestr("what-happened.txt", note)
        archive.writestr("contents.txt", _manifest(collected, note))
        for section_id, text in collected.items():
            section = SECTIONS_BY_ID.get(section_id)
            if section is None:
                continue
            archive.writestr(section.filename, text)

    path.write_bytes(buffer.getvalue())
    logger.info("Wrote a support bundle to %s (%s section(s))", path, len(collected))
    return path


def _manifest(collected: dict[str, str], note: str) -> str:
    lines = [
        "ShellMate Portable — what is in this bundle",
        f"Collected {time.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "Everything here was chosen and previewed before it was written.",
        "",
        "ShellMate Portable is owned by Foundry Networks and Services and is",
        "provided as is, without warranty. This bundle contains diagnostic",
        "information from one installation; it carries no ShellMate source or",
        "third-party code, and no saved credentials. See the Legal and licences",
        "page in the manual for the full statement and attributions.",
        "",
    ]
    if note.strip():
        lines.append("what-happened.txt    Your description of the problem")
    for section_id, text in collected.items():
        section = SECTIONS_BY_ID.get(section_id)
        if section is None:
            continue
        marker = "  [device data]" if section.device_data else ""
        lines.append(f"{section.filename:20} {section.label}{marker}"
                     f"  ({len(text):,} bytes)")

    lines += [
        "",
        "Not included, ever: API keys, device passwords, vault contents, or the",
        "plaintext credentials file. Terminal output has credentials masked.",
    ]
    return "\n".join(lines)
