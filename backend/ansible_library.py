"""
ansible_library.py — What ShellMate keeps for Ansible, beside the runner (#586).

The runner holds playbooks and runs them. Everything else an automation
needs in order to be repeatable lives here, in the data folder, so it
travels with the executable and is editable without a container:

- **Templates.** A parameterised task or play — "shut an interface", "set
  NTP servers" — with named holes and a description of each. Filling one
  in produces a playbook, which is what makes the same change repeatable
  by somebody who did not write it.
- **Environments.** A named set of variables, a default inventory choice
  and the run options that go with them: staging with its own jump host,
  production with `check` forced on until somebody deliberately turns it
  off. The point is that "run it against production" is one choice rather
  than six fields typed the same way every time.
- **Repositories.** Where a set of playbooks came from. The runner has no
  git of its own, so this records the remote, the branch and the last
  known revision, and says how the files get across.

Each is a JSON file written through :mod:`backend.jsonfile`, so a
half-written file cannot lose the lot and two writers cannot interleave.
Names are checked rather than trusted: these end up as filenames and as
Ansible identifiers, and both have opinions about what a name may contain.
"""

import logging
import re
import time
import uuid
from typing import Any

from backend import jsonfile, paths

logger = logging.getLogger(__name__)


def _file(name: str):
    return paths.data_dir() / "ansible" / f"{name}.json"


class LibraryError(ValueError):
    """A name, a field or a value that cannot be stored as asked."""


#: A name that is safe as a filename, as a JSON key and on a screen.
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,79}$")

#: An Ansible variable name. Ansible will accept more, but a variable that
#: is not a valid Python identifier cannot be used in a template without
#: quoting gymnastics, and a template exists to spare somebody that.
_VAR_RE = re.compile(r"^[a-z_][a-z0-9_]{0,63}$")


def _checked_name(name: str, what: str = "name") -> str:
    text = (name or "").strip()
    if not _NAME_RE.match(text):
        raise LibraryError(
            f"A {what} may hold letters, digits, spaces, dots, dashes and "
            "underscores, and must start with a letter or a digit.")
    return text


def _now() -> float:
    return time.time()


def _load(kind: str) -> list[dict]:
    return jsonfile.read(_file(kind), [], expect=list)


def _save(kind: str, rows: list[dict]) -> None:
    jsonfile.write(_file(kind), rows)


def _replace(kind: str, entry: dict) -> dict:
    """Insert or update by id, under the file's lock."""
    path = _file(kind)
    with jsonfile.locked(path):
        rows = jsonfile.read(path, [], expect=list)
        for index, row in enumerate(rows):
            if row.get("id") == entry["id"]:
                rows[index] = entry
                break
        else:
            rows.append(entry)
        jsonfile.write(path, rows)
    return entry


def _remove(kind: str, entry_id: str) -> bool:
    path = _file(kind)
    with jsonfile.locked(path):
        rows = jsonfile.read(path, [], expect=list)
        kept = [r for r in rows if r.get("id") != entry_id]
        if len(kept) == len(rows):
            return False
        jsonfile.write(path, kept)
    return True


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------
def templates() -> list[dict]:
    return sorted(_load("templates"), key=lambda t: (t.get("name") or "").lower())


def save_template(fields: dict) -> dict:
    """
    Store a parameterised playbook.

    Whether it writes to a device is read off the body rather than
    accepted from the caller — see below. A template with no recognisable
    task in it counts as writing, because "ShellMate could not tell" and
    "it is safe" are not the same claim.

    ``body`` is YAML with ``{{ variable }}`` holes; ``variables`` describes
    each hole so the form that fills it can be drawn — a label, a default,
    whether it is required, and optionally the values it may take. A hole
    the body uses but the variables do not describe is refused: the form
    would not ask for it, and the run would fail on an undefined variable
    somewhere in the middle of a play.
    """
    name = _checked_name(fields.get("name", ""), "template name")
    body = str(fields.get("body") or "").replace("\r\n", "\n")
    if not body.strip():
        raise LibraryError("A template needs a body.")

    variables = []
    seen: set[str] = set()
    for raw in fields.get("variables") or []:
        key = str((raw or {}).get("name") or "").strip()
        if not _VAR_RE.match(key):
            raise LibraryError(
                f"'{key}' is not a usable variable name: lower case, digits "
                "and underscores, starting with a letter or underscore.")
        if key in seen:
            raise LibraryError(f"'{key}' is described twice.")
        seen.add(key)
        variables.append({
            "name": key,
            "label": str(raw.get("label") or key.replace("_", " ")).strip(),
            "help": str(raw.get("help") or "").strip(),
            "default": raw.get("default", ""),
            "required": bool(raw.get("required", True)),
            "choices": [str(c) for c in (raw.get("choices") or []) if str(c).strip()],
        })

    missing = sorted(placeholders(body) - seen)
    if missing:
        raise LibraryError(
            "The body uses " + ", ".join(missing) + " but nothing describes "
            + ("it" if len(missing) == 1 else "them")
            + ", so the form could not ask for a value.")

    entry = {
        "id": str(fields.get("id") or uuid.uuid4()),
        "name": name,
        "description": str(fields.get("description") or "").strip(),
        "body": body,
        "variables": variables,
        "platform": str(fields.get("platform") or "").strip(),
        "updated": _now(),
    }

    # Whether a template changes a device is read off its body, not taken
    # from a checkbox. It was a checkbox, and a badge saying "read only"
    # sitting next to badges that are actually verified is worse than no
    # badge: it is one honest mistake away from making something look safer
    # than it is. The scan is conservative — a module it does not recognise
    # counts as a write — so the badge can be wrong in the direction that
    # makes somebody look, and not in the direction that stops them.
    from backend.ansible_builder import inspect as read_back

    found = read_back(body)
    entry["writes"] = bool(found["writes"]) or not found["tasks"]
    entry["modules"] = [t["module"] for t in found["tasks"]]
    entry["unknown_modules"] = found["unknown_modules"]
    logger.info("Ansible template saved: %s (%d variable(s))", name, len(variables))
    return _replace("templates", entry)


def delete_template(entry_id: str) -> bool:
    return _remove("templates", entry_id)


#: `{{ name }}`, `{{name}}`, and the filtered form `{{ name | default(x) }}`.
_PLACEHOLDER_RE = re.compile(r"{{\s*([a-z_][a-z0-9_]*)\s*(?:\|[^}]*)?}}")


def placeholders(body: str) -> set[str]:
    """Every variable a template body asks for."""
    return set(_PLACEHOLDER_RE.findall(body or ""))


def render_template(template: dict, values: dict) -> str:
    """
    Fill a template in, and refuse rather than guess.

    A missing required value stops here with its name. Substitution is
    literal and deliberately not Jinja: this runs in ShellMate, where a
    template is data somebody typed, and handing that to a template engine
    with access to Python would be a remote-code path from a text box.
    Ansible still evaluates its own Jinja when the play runs.
    """
    body = template.get("body") or ""
    described = {v["name"]: v for v in template.get("variables") or []}
    filled: dict[str, str] = {}
    missing: list[str] = []
    for name, spec in described.items():
        raw = values.get(name, spec.get("default", ""))
        text = "" if raw is None else str(raw)
        if not text.strip() and spec.get("required", True):
            missing.append(spec.get("label") or name)
            continue
        choices = spec.get("choices") or []
        if choices and text and text not in choices:
            raise LibraryError(f"{spec.get('label') or name} must be one of: "
                               + ", ".join(choices))
        filled[name] = text
    if missing:
        raise LibraryError("Still needed: " + ", ".join(missing) + ".")

    def substitute(match: re.Match) -> str:
        return filled.get(match.group(1), match.group(0))

    return _PLACEHOLDER_RE.sub(substitute, body)


# ---------------------------------------------------------------------------
# Environments
# ---------------------------------------------------------------------------
def environments() -> list[dict]:
    return sorted(_load("environments"), key=lambda e: (e.get("name") or "").lower())


def save_environment(fields: dict) -> dict:
    """
    Store a named set of run options.

    ``force_check`` is the one worth having: an environment can insist that
    every run against it is a dry run until somebody deliberately says
    otherwise. "Production, and I mean it" should take a second decision,
    not the same click as staging.
    """
    name = _checked_name(fields.get("name", ""), "environment name")
    variables = {}
    for key, value in (fields.get("variables") or {}).items():
        key = str(key).strip()
        if not _VAR_RE.match(key):
            raise LibraryError(f"'{key}' is not a usable variable name.")
        variables[key] = value

    source = str(fields.get("inventory_source") or "estate")
    if source not in ("estate", "runner"):
        raise LibraryError("An inventory comes from the estate or from the runner.")

    entry = {
        "id": str(fields.get("id") or uuid.uuid4()),
        "name": name,
        "description": str(fields.get("description") or "").strip(),
        "variables": variables,
        "inventory_source": source,
        "group": str(fields.get("group") or "").strip(),
        "inventory_path": str(fields.get("inventory_path") or "").strip(),
        "limit": str(fields.get("limit") or "").strip(),
        "force_check": bool(fields.get("force_check", False)),
        "forks": int(fields.get("forks") or 0) or None,
        "verbosity": max(0, min(4, int(fields.get("verbosity") or 0))),
        "updated": _now(),
    }
    return _replace("environments", entry)


def delete_environment(entry_id: str) -> bool:
    return _remove("environments", entry_id)


def environment(entry_id: str) -> dict | None:
    return next((e for e in _load("environments") if e.get("id") == entry_id), None)


# ---------------------------------------------------------------------------
# Repositories
# ---------------------------------------------------------------------------
def repositories() -> list[dict]:
    return sorted(_load("repositories"), key=lambda r: (r.get("name") or "").lower())


def save_repository(fields: dict) -> dict:
    """
    Record where a set of playbooks comes from.

    ShellMate does not clone anything: the runner has no git API, and a
    portable executable carrying a git implementation to drive a container
    it cannot reach directly would be the wrong shape. What this holds is
    the remote, the branch and what was last seen there — enough to say
    "the runner is three commits behind" once somebody tells it, and
    enough to put in a change record.
    """
    name = _checked_name(fields.get("name", ""), "repository name")
    url = str(fields.get("url") or "").strip()
    if not url:
        raise LibraryError("A repository needs a URL.")
    if not re.match(r"^(https?://|git@|ssh://)", url):
        raise LibraryError("A repository URL should be https, ssh or git@.")

    entry_id = str(fields.get("id") or uuid.uuid4())
    revision = str(fields.get("revision") or "").strip()
    checked = float(fields.get("checked") or 0) or None
    if checked is None and revision:
        # No caller passes `checked` directly — it is set by note_revision(),
        # a deliberate second action. So an edit that leaves the revision
        # exactly as it was (the ordinary case: fixing a URL, not reporting a
        # new commit) must not silently erase when that revision was last
        # actually checked, or noting one would be undone by the next
        # unrelated correction.
        existing = next((r for r in _load("repositories") if r.get("id") == entry_id), None)
        if existing and existing.get("revision") == revision:
            checked = existing.get("checked")

    entry = {
        "id": entry_id,
        "name": name,
        "url": url,
        "branch": str(fields.get("branch") or "main").strip(),
        "path": str(fields.get("path") or "").strip(),
        "revision": revision,
        "checked": checked,
        "notes": str(fields.get("notes") or "").strip(),
        "updated": _now(),
    }
    return _replace("repositories", entry)


def delete_repository(entry_id: str) -> bool:
    return _remove("repositories", entry_id)


def note_revision(entry_id: str, revision: str) -> dict | None:
    """What was last seen on the runner for this repository."""
    for row in _load("repositories"):
        if row.get("id") == entry_id:
            row["revision"] = str(revision or "").strip()
            row["checked"] = _now()
            return _replace("repositories", row)
    return None


# ---------------------------------------------------------------------------
# The dashboard's numbers
# ---------------------------------------------------------------------------
def counts() -> dict:
    """What the library holds, for the dashboard. Cheap and never raises."""
    try:
        return {"templates": len(_load("templates")),
                "environments": len(_load("environments")),
                "repositories": len(_load("repositories"))}
    except Exception:                                     # pragma: no cover
        return {"templates": 0, "environments": 0, "repositories": 0}
