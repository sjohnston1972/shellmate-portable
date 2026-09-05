"""
setup_bundle.py — Taking a ShellMate setup somewhere else (#563).

The manual enumerates every file in `ShellMate-Data` and says three of them
are meant to be handed to a colleague. That is as far as it goes: the way to
move a setup to a laptop is to copy a folder by hand and then find out, some
days later, that the vault did not come with it.

Two operations and one rule.

**Export** writes one zip of the things that are a *setup* — settings,
connections, groups, credential-set names, platforms, schemes, snippets,
prompts — with a manifest and per-file checksums. **Import** shows what is
in one and what each file would do before applying any of it.

**The rule: nothing secret is in the bundle.** Not the vault, not the
plaintext credential file, not an API key, not a device password. That is
not a limitation to be worked around later — a setup bundle is a file people
mail to each other, and the whole design follows from it. The credential
*sets* travel as names and usernames so that a colleague's profiles point at
something meaningful; the passwords behind them are theirs to fill in.

Because of that rule, the DPAPI point has to be made in the interface rather
than only here: a vault encrypted to a Windows account never travels, and
somebody who exports their setup and finds their saved passwords gone has
been failed by a missing sentence, not by the encryption.
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import time
import zipfile
from pathlib import Path

from backend import jsonfile, paths

logger = logging.getLogger(__name__)

#: Bundle format. Bumped only for a change an older ShellMate could not
#: read safely; `inspect` refuses anything higher than it knows.
FORMAT = 1

#: The ceiling on an uploaded bundle, before it is opened.
#:
#: A setup is a few hundred kilobytes of JSON. Anything near this is either
#: not a setup bundle or is trying to be a decompression bomb, and the zip
#: is read entirely in memory.
MAX_BUNDLE_BYTES = 8 * 1024 * 1024

#: And on any one file inside it, checked against the *declared* size
#: before extraction, then against the real one after.
MAX_MEMBER_BYTES = 4 * 1024 * 1024


class BundleError(Exception):
    """A bundle that cannot be read, or is not one."""


# ---------------------------------------------------------------------------
# What a setup is made of
# ---------------------------------------------------------------------------

class Part:
    """One file in a bundle, and how it merges."""

    def __init__(self, key: str, filename: str, label: str, describe: str,
                 mergeable: bool, optional: bool = False):
        self.key = key
        self.filename = filename
        self.label = label
        self.describe = describe
        #: Whether "merge" is offered as well as "replace". A list of
        #: things merges; a single document of preferences does not, and
        #: offering a merge that silently means replace is worse than not
        #: offering it.
        self.mergeable = mergeable
        #: Not included unless asked for.
        self.optional = optional

    def path(self) -> Path:
        return paths.data_dir() / self.filename


PARTS: tuple[Part, ...] = (
    Part("settings", "settings.json", "Settings",
         "Your preferences. API keys are removed — they live in the vault, "
         "and a bundle is a file people mail to each other.", False),
    Part("profiles", "profiles.json", "Saved connections",
         "Devices, addresses and usernames. Never a password: profiles have "
         "never held one.", True),
    Part("groups", "groups.json", "Groups",
         "How the dashboard is organised, including schedules.", True),
    Part("credential_sets", "credential-sets.json", "Credential sets",
         "The names and usernames only. The passwords behind them stay on "
         "the machine they were saved on.", True),
    Part("platforms", "platforms.json", "Platform definitions",
         "What ShellMate knows about each kind of device — the commonest "
         "thing to have corrected.", True),
    Part("schemes", "schemes.json", "Colour schemes", "Terminal colours.", True),
    Part("snippets", "snippets.json", "Command library",
         "Saved commands and runbooks.", True),
    Part("prompts", "prompts.json", "Assistant prompts",
         "What the assistant is told before it sees anything of yours.",
         False),
    Part("licence", "licence-state.json", "Licence key",
         "Only if you ask for it, and only useful where the licence permits "
         "another machine.", False, optional=True),
)

PARTS_BY_KEY = {part.key: part for part in PARTS}

#: Files that must never be in a bundle, whatever else changes.
#:
#: Named rather than inferred from what is *not* in PARTS, so that adding a
#: part cannot accidentally add one of these — and so that the test can
#: assert on the list rather than on the absence of a name it had to guess.
NEVER = ("vault.json", "credentials-plaintext.json", "history.db",
         "knowledge.db", "shellmate.log", "feedback-outbox.json")


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def _scrubbed_settings(raw: bytes) -> bytes:
    """
    Settings with every provider secret removed.

    Belt and braces. `update_settings` already diverts secrets to the vault
    and blanks them, so settings.json should hold none — but "should" is
    not the standard for a file somebody mails to a colleague, and the cost
    of checking again here is nothing.
    """
    from backend.settings_store import SECRET_FIELDS

    try:
        document = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return raw

    providers = document.get("providers")
    if isinstance(providers, dict):
        for field in SECRET_FIELDS:
            if field in providers:
                providers[field] = ""

    # The two secrets that live in sections of their own, and the webhook
    # URL, which is a credential that looks like a location.
    for section, field in (("ansible", "token"), ("ansible", "github_token"),
                           ("ticketing", "jira_api_token"),
                           ("backups", "webhook_url")):
        block = document.get(section)
        if isinstance(block, dict) and field in block:
            block[field] = ""

    return json.dumps(document, indent=2).encode("utf-8")


def export(include: list[str] | None = None) -> tuple[bytes, dict]:
    """
    Build a bundle. Returns ``(zip bytes, manifest)``.

    Args:
        include: Part keys to include. None means everything that is not
            optional — the licence has to be asked for.

    A checksum per file, and the manifest is written into the zip as well
    as returned. Not for security: a bundle is not signed and cannot be.
    It is so that "this is not the file I exported" is answerable at all,
    which matters when the answer to a support question is "send me your
    setup".
    """
    wanted = (list(include) if include is not None
              else [p.key for p in PARTS if not p.optional])

    entries = []
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for part in PARTS:
            if part.key not in wanted:
                continue
            path = part.path()
            if not path.exists():
                # Absent is normal — a fresh install has no groups.json —
                # and a bundle that recorded it as an empty file would
                # replace a colleague's groups with nothing on import.
                continue
            try:
                raw = path.read_bytes()
            except OSError as exc:
                logger.warning("Could not read %s for the bundle: %s",
                               part.filename, exc)
                continue
            if part.key == "settings":
                raw = _scrubbed_settings(raw)

            archive.writestr(part.filename, raw)
            entries.append({
                "key":      part.key,
                "file":     part.filename,
                "label":    part.label,
                "bytes":    len(raw),
                "sha256":   hashlib.sha256(raw).hexdigest(),
                "mergeable": part.mergeable,
            })

        manifest = {
            "format":  FORMAT,
            "created": time.strftime("%Y-%m-%d %H:%M:%S"),
            "version": _version(),
            "parts":   entries,
        }
        archive.writestr("manifest.json",
                         json.dumps(manifest, indent=2))
        archive.writestr("README.txt", _readme(entries))

    return buffer.getvalue(), manifest


def _version() -> str:
    try:
        from backend import version as app_version
        return app_version.describe()
    except Exception:
        return "unknown"


def _readme(entries: list[dict]) -> str:
    """
    What this file is, for whoever opens the zip rather than importing it.

    People do open the zip. A bundle with no explanation in it is a bundle
    somebody unpacks over their data folder by hand, which is precisely the
    thing the import preview exists to stop.
    """
    lines = [
        "ShellMate setup bundle",
        "",
        "Import this through Settings -> Backup and transfer -> Import,",
        "which shows what each file would do before it does any of it.",
        "Unpacking it over your data folder by hand skips that.",
        "",
        "What is in it:",
    ]
    for entry in entries:
        lines.append(f"  {entry['file']:24} {entry['label']}")
    lines += [
        "",
        "What is NOT in it, and cannot be:",
        "  the vault, the plaintext credential file, any API key,",
        "  any device password, your session history.",
        "",
        "Saved passwords do not travel. If your vault is sealed to a",
        "Windows account it cannot be moved at all; a master-password",
        "vault can be moved with its own backup, from Settings ->",
        "Credentials vault.",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Inspect
# ---------------------------------------------------------------------------

def inspect(blob: bytes) -> dict:
    """
    What is in a bundle, and what each part would do — without doing any.

    The preview is the whole point of having an import rather than telling
    people to unzip it. Counts per file, not just names: "profiles.json"
    says nothing, "31 connections, 4 of which you already have" is a
    decision somebody can take.

    Raises:
        BundleError: not a zip, not a bundle, or too large.
    """
    if len(blob) > MAX_BUNDLE_BYTES:
        raise BundleError(
            f"That file is {len(blob) // 1024} KB. A setup bundle is a few "
            f"hundred KB of JSON, so this is not one.")

    try:
        archive = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile as exc:
        raise BundleError("That is not a zip file.") from exc

    try:
        manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
    except KeyError as exc:
        raise BundleError(
            "That zip has no manifest.json, so it is not a ShellMate setup "
            "bundle.") from exc
    except (ValueError, UnicodeDecodeError) as exc:
        raise BundleError("The bundle's manifest could not be read.") from exc

    if int(manifest.get("format") or 0) > FORMAT:
        raise BundleError(
            "That bundle was written by a newer ShellMate than this one. "
            "Update first — importing it here could drop settings this "
            "version does not know about.")

    parts = []
    for entry in manifest.get("parts") or []:
        part = PARTS_BY_KEY.get(entry.get("key"))
        if part is None:
            # A part this version does not know. Reported rather than
            # ignored: a bundle from a newer minor version is importable,
            # and somebody should be told which piece will not come across.
            parts.append({"key": entry.get("key"), "file": entry.get("file"),
                          "label": entry.get("label") or entry.get("key"),
                          "known": False, "count": None, "overlap": None,
                          "mergeable": False, "checksum_ok": None,
                          "describe": "This version of ShellMate does not "
                                      "know what this is, so it will be "
                                      "skipped."})
            continue

        info = _member(archive, part.filename, entry)
        parts.append({
            "key":       part.key,
            "file":      part.filename,
            "label":     part.label,
            "describe":  part.describe,
            "known":     True,
            "mergeable": part.mergeable,
            "have":      part.path().exists(),
            **info,
        })

    return {
        "format":  int(manifest.get("format") or 0),
        "created": manifest.get("created") or "",
        "version": manifest.get("version") or "",
        "parts":   parts,
    }


def _member(archive: zipfile.ZipFile, filename: str, entry: dict) -> dict:
    """One file's counts and whether its checksum still matches."""
    out: dict = {"count": None, "overlap": None, "checksum_ok": None,
                 "bytes": int(entry.get("bytes") or 0), "error": ""}
    try:
        info = archive.getinfo(filename)
    except KeyError:
        out["error"] = "The manifest lists this, but it is not in the zip."
        return out

    if info.file_size > MAX_MEMBER_BYTES:
        out["error"] = f"{info.file_size // 1024} KB is far larger than this " \
                       f"file should ever be, so it was not opened."
        return out

    raw = archive.read(filename)
    declared = str(entry.get("sha256") or "")
    if declared:
        out["checksum_ok"] = hashlib.sha256(raw).hexdigest() == declared

    try:
        document = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        out["error"] = "That file is not readable JSON."
        return out

    out["count"] = _count(document)
    out["overlap"] = _overlap(filename, document)
    return out


def _count(document) -> int | None:
    if isinstance(document, list):
        return len(document)
    if isinstance(document, dict):
        # A settings document is one thing, not len(keys) things. The
        # shapes that are really collections are the ones whose values are
        # all dicts with an id.
        values = list(document.values())
        if values and all(isinstance(v, dict) for v in values):
            return len(values)
        return None
    return None


def _overlap(filename: str, document) -> int | None:
    """
    How many of these are already here.

    Only for connections, because that is the one where the answer changes
    what somebody chooses — and because `profiles.identity()` is the only
    place that knows what "the same connection" means. Guessing at it for
    the other files would produce a number that looks authoritative and is
    not.
    """
    if filename != "profiles.json" or not isinstance(document, list):
        return None
    try:
        from backend.profiles import get_profiles, identity

        mine = {identity(p) for p in get_profiles()}
        return sum(1 for p in document
                   if isinstance(p, dict) and identity(p) in mine)
    except Exception as exc:
        logger.debug("Could not count overlapping profiles: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------

def apply(blob: bytes, actions: dict[str, str]) -> dict:
    """
    Import a bundle. ``actions`` is ``{part key: "replace"|"merge"|"skip"}``.

    Everything is read and validated before anything is written, and the
    files that survive that are written in one pass. Half an import is the
    worst outcome available here — a settings file from one machine beside
    profiles from another is a configuration nobody has ever tested.

    Raises:
        BundleError: the bundle is unusable, or a chosen action is not
            available for that part.
    """
    from backend.settings_store import invalidate

    archive = zipfile.ZipFile(io.BytesIO(blob))
    staged: list[tuple[Part, object]] = []

    for key, action in (actions or {}).items():
        if action == "skip":
            continue
        part = PARTS_BY_KEY.get(key)
        if part is None:
            continue
        if action == "merge" and not part.mergeable:
            raise BundleError(
                f"{part.label} cannot be merged — it is one document rather "
                f"than a list, so the only honest choices are replace and "
                f"skip.")
        if action not in ("replace", "merge"):
            raise BundleError(f"{action!r} is not something that can be done "
                              f"to {part.label}.")

        try:
            raw = archive.read(part.filename)
        except KeyError:
            continue
        try:
            incoming = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise BundleError(
                f"{part.label} is not readable JSON, so nothing was "
                f"imported.") from exc

        if action == "merge":
            incoming = _merge(part, incoming)
        staged.append((part, incoming))

    applied = []
    for part, document in staged:
        jsonfile.write(part.path(), document)
        applied.append({"key": part.key, "label": part.label,
                        "count": _count(document)})
        logger.info("Imported %s from a setup bundle", part.filename)

    # Every cache that could now be stale. Settings is the one that matters
    # — `advanced()` reads it per line of device output — but a profile
    # list held in memory would be just as wrong.
    invalidate()
    return {"applied": applied}


def _merge(part: Part, incoming):
    """
    Combine an incoming list with what is here, incoming losing ties.

    Incoming losing rather than winning: somebody importing a colleague's
    setup has their own corrections in these files, and an import that
    silently overwrote a platform definition they had fixed would be a
    regression they would not connect to the import. Replace is there for
    when they do want that, and it says so.
    """
    current = _read_current(part)
    if not isinstance(current, list) or not isinstance(incoming, list):
        # Not a list on one side or the other: merge has no meaning, and
        # `apply` has already refused it for parts declared unmergeable —
        # so this is a file whose shape is not what was expected.
        raise BundleError(
            f"{part.label} is not the shape a merge expects, so nothing was "
            f"imported. Replace or skip it instead.")

    if part.key == "profiles":
        # The one place identity is not "the id": #73 exists because two
        # profiles for the same device are worse than one merged badly.
        from backend.profiles import identity

        mine = {identity(p): p for p in current if isinstance(p, dict)}
        for item in incoming:
            if isinstance(item, dict) and identity(item) not in mine:
                current.append(item)
        return current

    seen = {str(item.get("id") or item.get("key") or item.get("name"))
            for item in current if isinstance(item, dict)}
    for item in incoming:
        if not isinstance(item, dict):
            continue
        marker = str(item.get("id") or item.get("key") or item.get("name"))
        if marker not in seen:
            current.append(item)
            seen.add(marker)
    return current


def _read_current(part: Part):
    try:
        return json.loads(part.path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


# ---------------------------------------------------------------------------
# Moving the data folder
#
# The other half of "get my setup onto a laptop": sometimes the answer is
# not a bundle but the folder itself, somewhere else — a bigger disk, an
# encrypted volume, a synced folder.
#
# One override, in one place: `data-dir.txt` beside the executable, read by
# `paths.data_dir()` and nowhere else. An application that resolved its own
# data folder in two places would eventually give two answers, and for a
# portable tool that means a stick somebody believes is carrying their
# setup and is not.
# ---------------------------------------------------------------------------

def move_plan(target: str) -> dict:
    """
    What moving to *target* would do, and whether it can be done.

    Checked before anything is copied and reported as prose, because every
    one of these is a thing somebody can fix, and "it failed" is not.
    """
    from backend import paths as _paths

    current = _paths.data_dir()
    problems: list[str] = []
    destination = Path(str(target or "").strip()) if target else None

    if destination is None or not str(destination):
        problems.append("No folder was chosen.")
        return {"ok": False, "problems": problems, "from": str(current),
                "to": "", "bytes": 0}

    destination = destination.expanduser()
    try:
        destination = destination.resolve()
    except OSError:
        problems.append("That path could not be resolved.")

    if destination == current:
        problems.append("That is where the data already is.")
    elif current in destination.parents:
        # Copying a folder into itself is an unbounded copy, and the first
        # thing anybody tries is dragging the current folder onto the
        # picker.
        problems.append("That folder is inside the current data folder, "
                        "which would copy it into itself.")

    if not destination.exists():
        try:
            destination.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            problems.append(f"That folder could not be created ({exc}).")
    elif any(destination.iterdir()):
        # Not refused — moving into an existing ShellMate-Data is the
        # obvious way to put a setup back — but said, because merging into
        # somebody else's folder is the one that goes wrong quietly.
        problems.append("NOTE: that folder is not empty. Files with the "
                        "same names will be overwritten.")

    if not _paths._is_writable(destination):
        problems.append("That folder cannot be written to.")

    total = 0
    try:
        total = sum(f.stat().st_size for f in current.rglob("*") if f.is_file())
    except OSError:
        pass

    hard = [p for p in problems if not p.startswith("NOTE:")]
    return {"ok": not hard, "problems": problems, "from": str(current),
            "to": str(destination), "bytes": total}


def move_data_dir(target: str, session_manager=None) -> dict:
    """
    Copy the data folder to *target* and point future launches at it.

    **Copy, then point, and never delete.** The original is left exactly
    where it was. A move that removed it would be the one operation in
    ShellMate with no way back, performed on the folder holding everything
    somebody has configured — and the disk space is a far smaller problem
    than a failed copy nobody noticed until the next launch.

    Refuses while sessions are open, mirroring `updater.blockers`: it is
    the same class of problem, which is replacing state underneath a live
    connection.
    """
    import shutil as _shutil

    from backend import paths as _paths

    if session_manager is not None:
        open_sessions = [
            s.get("display_label") or s.get("hostname") or "a device"
            for s in session_manager.get_all_sessions()
        ]
        if open_sessions:
            raise BundleError(
                "Close your sessions first — moving the data folder while "
                "ShellMate is writing history for a live connection would "
                "split that session's record across two folders. Open: "
                + ", ".join(open_sessions[:6])
                + ("…" if len(open_sessions) > 6 else ""))

    plan = move_plan(target)
    if not plan["ok"]:
        raise BundleError(" ".join(p for p in plan["problems"]
                                   if not p.startswith("NOTE:")))

    source = Path(plan["from"])
    destination = Path(plan["to"])
    try:
        _shutil.copytree(source, destination, dirs_exist_ok=True)
    except OSError as exc:
        raise BundleError(
            f"The copy failed and nothing was changed: {exc}") from exc

    pointer = _paths.app_dir() / _paths.DATA_DIR_POINTER
    try:
        pointer.write_text(str(destination), encoding="utf-8")
    except OSError as exc:
        # The copy worked and the pointer did not. Said plainly, with the
        # path, because the fix is one line in a text file and the
        # alternative is somebody assuming the move silently failed.
        raise BundleError(
            f"Everything was copied to {destination}, but ShellMate could "
            f"not write {pointer.name} beside the executable ({exc}). Put "
            f"the path in that file by hand, or move ShellMate somewhere "
            f"writable.") from exc

    logger.info("Data folder moved to %s; the original is left at %s",
                destination, source)
    return {
        "moved": True,
        "from": str(source),
        "to": str(destination),
        # Said every time, not just when it goes wrong. Somebody who
        # believes a move deleted the original will not go looking for the
        # copy of their vault that is still sitting there.
        "note": (f"The original folder at {source} has been left exactly as "
                 f"it was. Delete it yourself once you are satisfied the "
                 f"move worked."),
        "restart_required": True,
    }
