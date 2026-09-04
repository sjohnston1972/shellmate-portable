"""
diagnostics.py — Is this install healthy? (#562)

The support bundle answers *what happened*, after the fact. Nothing answered
*is this copy working properly* before somebody has a reason to ask.

Every failure worth reporting was already being computed somewhere and then
only logged: the window-frame ladder in `desktop.py`, the data-folder fallback
in `paths.py`, the vault's backend, FTS5 versus LIKE in `store.py`, the port
walk in `server.py`, a failed update swap in `updater.py`, the feedback
outbox. On a copy running from a stick, two of those degrade silently and are
exactly what gets reported later in other words: a fallback data folder
arrives as "my settings vanished", and the browser rung of the frame ladder as
"it opened in Chrome".

Three rules, and the third is the one that constrains the design:

**A check never raises.** Every one is wrapped, and a check that cannot run
says so as its own result. A diagnostics panel that fails to render is worse
than no diagnostics panel.

**A check never changes anything.** They read state; they do not tidy the
data folder, unlock the vault or reapply an update. `updater.tidy_after_launch()`
already did its work at startup and this reports its verdict rather than
running it again.

**Network probes happen per click, never on open.** ShellMate promises to work
air-gapped, and a Settings panel that reaches out to GitHub the moment it is
opened breaks that promise without anybody choosing to. The probes are a
separate argument and the panel asks for them explicitly.
"""

import logging
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from backend import paths

logger = logging.getLogger(__name__)

#: How long a network probe may take. Short on purpose: an air-gapped machine
#: must reach "could not be reached" quickly rather than stalling the panel.
PROBE_TIMEOUT = 6.0

OK, WARN, FAIL = "ok", "warn", "fail"


@dataclass(frozen=True)
class Check:
    """One thing that can be true or not about this installation."""

    id: str
    label: str
    #: Returns (status, detail, fix). ``fix`` is what to do about it, or "".
    run: Callable[[], tuple[str, str, str]]
    #: True when it goes over the network, so it only runs when asked.
    probe: bool = False


# ---------------------------------------------------------------------------
# The checks
# ---------------------------------------------------------------------------


def _frame() -> tuple[str, str, str]:
    """Which rung of the window ladder took it, and the runtime behind it."""
    from backend import desktop

    frame = desktop.frame_in_use()
    version = _webview2_version()
    if frame == "native window":
        return OK, f"Native window{f' — WebView2 {version}' if version else ''}.", ""
    if frame == "not started":
        return OK, ("No application window in this process — ShellMate is being "
                    "used through a browser, or was started with --no-window."), ""
    return WARN, f"Running in a {frame}.", (
        "The native window needs the WebView2 runtime. Installing Microsoft "
        "Edge WebView2 gives ShellMate its own window, taskbar entry and tray "
        "icon; everything works without it.")


def _webview2_version() -> str:
    """The installed WebView2 runtime version, or "" when there is none."""
    if not sys.platform.startswith("win"):
        return ""
    try:
        import winreg
    except ImportError:
        return ""
    key = (r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients"
           r"\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}")
    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for path in (key, key.replace(r"\WOW6432Node", "")):
            try:
                with winreg.OpenKey(root, path) as handle:
                    return str(winreg.QueryValueEx(handle, "pv")[0])
            except OSError:
                continue
    return ""


def _data_folder() -> tuple[str, str, str]:
    """Where settings actually live, whether it is writable, and how full."""
    folder = paths.data_dir()
    writable = paths._is_writable(folder)
    free = ""
    try:
        free = f"{shutil.disk_usage(folder).free / (1024 ** 3):.1f} GB free"
    except OSError:
        pass
    detail = f"{folder}{f' — {free}' if free else ''}"

    if not writable:
        return FAIL, detail, (
            "Nothing can be saved. Move ShellMate somewhere you can write to, "
            "or check whether the folder is read-only.")
    if paths.data_dir_is_fallback():
        return WARN, detail + " — fallback", (
            "The folder beside the executable was not writable, so settings, "
            "history and the vault went to your user profile instead. They do "
            "not travel with the executable from here. Move ShellMate to a "
            "writable folder and copy ShellMate-Data across.")
    return OK, detail, ""


def _disk() -> tuple[str, str, str]:
    """Room to write. The history database grows, and a full disk corrupts."""
    try:
        free = shutil.disk_usage(paths.data_dir()).free
    except OSError as exc:
        return WARN, f"The free space could not be read: {exc}", ""
    gb = free / (1024 ** 3)
    if free < 100 * 1024 * 1024:
        return FAIL, f"{gb:.2f} GB free where the data folder is.", (
            "SQLite needs room to write. Free some space before the history "
            "database is asked to grow.")
    if free < 1024 ** 3:
        return WARN, f"{gb:.2f} GB free where the data folder is.", (
            "Getting tight. History, snapshots and session logs all live here.")
    return OK, f"{gb:.1f} GB free where the data folder is.", ""


def _vault() -> tuple[str, str, str]:
    """Whether secrets can actually be sealed and read back on this machine."""
    from backend import vault as vault_module

    status = vault_module.vault.status()
    mode = status.get("mode", "none")

    if mode == "password":
        if status.get("locked"):
            return WARN, "Master-password vault — locked.", (
                "Saved credentials and API keys read as absent until it is "
                "unlocked. Unlock it from Settings → Credentials.")
        return OK, "Master-password vault — unlocked.", ""

    if not vault_module.dpapi_available():
        return (OK if mode == "none" else WARN,
                "Windows DPAPI is unavailable on this platform.",
                "Set a master password under Settings → Credentials to store "
                "secrets encrypted on a machine without DPAPI.")

    try:
        token = b"shellmate-diagnostics"
        ok = vault_module.dpapi_decrypt(vault_module.dpapi_encrypt(token)) == token
    except Exception as exc:
        return FAIL, f"DPAPI would not seal a test value: {exc}", (
            "Secrets cannot be stored on this account. A master password is "
            "the way round it.")
    if not ok:
        return FAIL, "DPAPI returned something other than what it was given.", (
            "Do not rely on the vault on this machine.")
    return OK, ("DPAPI sealed and read back a test value"
                + (f"; {status.get('entry_count') or 0} entries stored."
                   if mode != "none" else " — no vault file yet.")), ""


def _history() -> tuple[str, str, str]:
    """FTS5 or the LIKE fallback, and how much is recorded."""
    from backend.store import store

    stats = store.stats()
    counts = (f"{stats.get('sessions', 0):,} sessions · "
              f"{stats.get('commands', 0):,} commands · "
              f"{stats.get('snapshots', 0):,} snapshots")
    if str(stats.get("search", "")).lower() == "fts5":
        return OK, f"FTS5 full-text search — {counts}.", ""
    return WARN, f"LIKE matching, not FTS5 — {counts}.", (
        "This build of SQLite has no FTS5, so History still finds things, "
        "more slowly and less precisely. Nothing is lost; nothing needs doing.")


def _port() -> tuple[str, str, str]:
    """The port bound against the preferred one — the walk is silent."""
    from backend import server
    from backend.config import HOST, PORT

    bound = server.listening_port
    if not bound:
        return WARN, "The listening port has not been recorded.", ""
    if int(bound) == int(PORT):
        return OK, f"Listening on {HOST}:{bound}, the preferred port.", ""
    return OK, f"Listening on {HOST}:{bound}; {PORT} was taken.", (
        "Nothing is wrong — the port is picked at startup precisely so a busy "
        "one cannot stop ShellMate. It does mean a bookmark to the old port "
        "will not open this copy.")


def _updates() -> tuple[str, str, str]:
    """A leftover previous executable, and what the last swap said."""
    from backend import updater

    attempt = (updater.state().get("last_attempt") or {})
    old = ""
    if paths.is_frozen():
        exe = Path(sys.executable)
        candidate = exe.with_name(exe.stem + ".old.exe")
        if candidate.exists():
            old = str(candidate)

    if attempt and not attempt.get("ok", True):
        return FAIL, f"The last update did not apply: {attempt.get('detail', '')}", (
            "The previous copy was put back, so this one still runs. Try the "
            "update again, or download the release by hand.")
    if old:
        return WARN, f"A previous executable is still beside this one: {old}", (
            "It is the roll-back copy from an update and is removed on the "
            "next clean launch. Delete it by hand if it lingers.")
    if attempt.get("ok"):
        return OK, f"The last update applied: {attempt.get('detail', '')}".strip(), ""
    return OK, "No update has been applied by this copy.", ""


def _outbox() -> tuple[str, str, str]:
    """Feedback that never got out. Silent by design, so it is asked about."""
    from backend import feedback

    try:
        queued = len(feedback._load_outbox())
    except Exception as exc:
        return WARN, f"The outbox could not be read: {exc}", ""
    if not queued:
        return OK, "Nothing waiting to be sent.", ""
    return WARN, f"{queued} report{'' if queued == 1 else 's'} waiting to be sent.", (
        "They are queued because the relay could not be reached, and go on "
        "the next successful send. Nothing about your devices is in them.")


def _env_file() -> tuple[str, str, str]:
    """Whether a .env is in play, because it changes where keys come from."""
    path = paths.env_file()
    if not path.exists():
        return OK, "No .env file — settings and the vault decide everything.", ""
    return OK, f"{path} is present and read at startup.", (
        "Values in it are overridden by the vault and by Settings. A key that "
        "looks stuck is usually one set in both places.")


def _log_level() -> tuple[str, str, str]:
    """The level, and whether the file behind it can actually be written."""
    from backend.advanced import get as advanced

    level = str(advanced("diag.log_level")).upper()
    path = paths.data_dir() / "shellmate.log"
    exists = path.exists()
    size = f"{path.stat().st_size / 1024:.0f} KB" if exists else "not written yet"
    if level == "DEBUG":
        return WARN, f"Logging at DEBUG — {path} ({size}).", (
            "DEBUG is verbose and the log is truncated on each launch anyway. "
            "Put it back to INFO under Stockton → Diagnostics once you have "
            "what you needed.")
    return OK, f"Logging at {level} — {path} ({size}).", ""


def _reachable(url: str, what: str, why: str) -> tuple[str, str, str]:
    """One network probe, with failure reported as a state of the world."""
    try:
        import httpx

        response = httpx.get(url, timeout=PROBE_TIMEOUT,
                             follow_redirects=True,
                             headers={"User-Agent": "ShellMate-Diagnostics"})
        return OK, f"{what} answered ({response.status_code}).", ""
    except Exception as exc:
        return WARN, f"{what} could not be reached: {type(exc).__name__}.", why


def _github() -> tuple[str, str, str]:
    return _reachable(
        "https://api.github.com/", "GitHub",
        "Update checks will report that GitHub could not be reached. On an "
        "air-gapped machine that is the correct answer, not a fault.")


def _licence_service() -> tuple[str, str, str]:
    from backend import licence

    return _reachable(
        licence.SERVICE_URL, "The licence service",
        "An installed key keeps working — it is verified on this machine "
        "without a network. Only renewals and revocations wait for a "
        "connection.")


CHECKS: tuple[Check, ...] = (
    Check("frame",    "Window", _frame),
    Check("data",     "Data folder", _data_folder),
    Check("disk",     "Free space", _disk),
    Check("vault",    "Vault", _vault),
    Check("history",  "History database", _history),
    Check("port",     "Port", _port),
    Check("updates",  "Last update", _updates),
    Check("outbox",   "Feedback outbox", _outbox),
    Check("env",      "Environment file", _env_file),
    Check("log",      "Logging", _log_level),
    Check("github",   "GitHub", _github, probe=True),
    Check("licence",  "Licence service", _licence_service, probe=True),
)

CHECKS_BY_ID = {c.id: c for c in CHECKS}


def run(probes: bool = False) -> list[dict]:
    """
    Run every check and return one row each.

    Args:
        probes: Include the checks that go over the network. Off by default,
            and the panel asks for them per click rather than on open — the
            air-gapped promise is not something a Settings panel gets to
            spend on the user's behalf.

    Returns:
        ``[{"id", "label", "status", "detail", "fix"}]`` — always one row per
        check that ran, including for a check that failed to run.
    """
    rows: list[dict] = []
    for check in CHECKS:
        if check.probe and not probes:
            continue
        try:
            status, detail, fix = check.run()
        except Exception as exc:            # a check is never allowed to raise
            logger.debug("Diagnostic check %s failed: %s", check.id, exc)
            status, detail, fix = (
                WARN, f"This check could not run: {type(exc).__name__}: {exc}", "")
        rows.append({"id": check.id, "label": check.label, "status": status,
                     "detail": detail, "fix": fix})
    return rows


def summarise(rows: list[dict]) -> str:
    """One line for the panel and the top of the bundle's checks.txt."""
    counts = {OK: 0, WARN: 0, FAIL: 0}
    for row in rows:
        counts[row.get("status", WARN)] = counts.get(row.get("status", WARN), 0) + 1
    if counts[FAIL]:
        return (f"{counts[FAIL]} problem{'' if counts[FAIL] == 1 else 's'} found"
                + (f", {counts[WARN]} worth knowing about." if counts[WARN] else "."))
    if counts[WARN]:
        return (f"Nothing broken; {counts[WARN]} thing"
                f"{'' if counts[WARN] == 1 else 's'} worth knowing about.")
    return f"All {counts[OK]} checks passed."


def as_text(probes: bool = False) -> str:
    """The same checks as a section of the support bundle."""
    rows = run(probes=probes)
    lines = ["ShellMate self-checks", summarise(rows), ""]
    for row in rows:
        lines.append(f"[{row['status'].upper():<4}] {row['label']}: {row['detail']}")
        if row["fix"]:
            lines.append(f"         {row['fix']}")
    return "\n".join(lines)


__all__ = ["CHECKS", "Check", "run", "summarise", "as_text"]
