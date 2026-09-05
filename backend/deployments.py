"""
deployments.py — Infrastructure built from a definition, not discovered.

The cloud accounts hold zero hosts. Meraki is managed by calling its API
with ids, not by connecting to devices. So the thing ShellMate needs is not
an inventory of what exists but a *definition* of what should — five
hundred sites, each with an MX, an MS, a VLAN plan, a rule set — and a way
to turn that into a plan the engineer reads and an apply they approve.

A deployment is four files in one folder of the ansible repository:

    deployments/<slug>/sites.yml    the data set, rendered from rows uploaded here
    deployments/<slug>/scheme.yml   the scheme, rendered from a form here
    deployments/<slug>/plan.yml     the runner's read-only plan playbook
    deployments/<slug>/apply.yml    the runner's apply playbook

Three rules, each of which came out of the design conversation with the
runner session and each of which is enforced here rather than trusted:

**Rendering is deterministic.** The same record produces the same bytes,
every time. The commit and the PUT to the runner must carry identical
content, and "has anything changed since the last commit" is a byte
comparison — a YAML dumper that reorders keys would make every save a
change.

**Columns are asked for, never guessed.** The same rule as inventories,
for the same reason: a header called `site` and one called `Network Name`
mean the same thing, and one called `serial` does not. The mapping is
stored with the deployment so a re-upload behaves the same way.

**The git path and the runner path differ, in one place.** The runner's
project directory is `runner/project/` inside the repository, not its
root. `PROJECT_PREFIX` is the only place that fact lives; a caller asks
for `git_paths()` or `runner_paths()` and never assembles either.

Provisioning logic — subnets, loops, claiming serials — stays in Ansible.
ShellMate never computes a VLAN.
"""
from __future__ import annotations

import logging
import re
import time
import uuid
from typing import Any

import yaml

from backend import jsonfile, paths

logger = logging.getLogger(__name__)


class DeploymentError(ValueError):
    """A name, a mapping or a value that cannot be stored as asked."""


#: What a deployment can build. Meraki first because it is the only one
#: with a real target; the other two use the same machinery with a
#: different scheme.
PROVIDERS = ("meraki", "azure", "aws")

#: The columns a site data set may nominate. `name` is the only one that
#: must be there; serials are "not yet" when absent, never an error, because
#: the org has no claimed devices and sites get built before hardware ships.
SITE_FIELDS = ("name", "tags", "mx", "ms")

#: Where the runner's project directory sits inside the repository.
#:
#: The git commit path prepends this; the runner's PUT path never sees it.
#: One constant, and the test proves the same bytes reach both routes
#: under the two paths, because building the two calls symmetrically is
#: exactly the mistake this exists to prevent.
PROJECT_PREFIX = "runner/project/"

#: The folder every deployment lives under, on both sides of that prefix.
FOLDER = "deployments"

#: The four files, in the order they are committed and sent.
FILES = ("sites.yml", "scheme.yml", "plan.yml", "apply.yml")

#: Which of them are playbooks (PUT /playbooks) and which are data
#: (PUT /files). The runner validates the difference on upload — a data
#: file sent to the playbook route is a 422 — so it is stated here rather
#: than discovered at the second file.
PLAYBOOKS = ("plan.yml", "apply.yml")
DATA_FILES = ("sites.yml", "scheme.yml")

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
# A display name, not a path: the slug carries the path rules. So a
# name may be "Glasgow — Phase 2" or "Zürich HB" — anything printable
# that starts with a letter or digit, up to eighty characters.
_NAME_RE = re.compile(r"^[^\W_][^\x00-\x1f]{0,79}$")


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def _file():
    return paths.data_dir() / "ansible" / "deployments.json"


def _now() -> float:
    return time.time()


def deployments() -> list[dict]:
    """Every deployment, sites replaced by a count — the list view."""
    out = []
    for row in jsonfile.read(_file(), [], expect=list):
        summary = {k: v for k, v in row.items() if k not in ("sites", "site_ids")}
        summary["sites"] = len(row.get("sites") or [])
        summary["built"] = len(row.get("site_ids") or {})
        out.append(summary)
    return out


def get(deployment_id: str) -> dict | None:
    for row in jsonfile.read(_file(), [], expect=list):
        if row.get("id") == deployment_id:
            return row
    return None


def _replace(entry: dict) -> dict:
    path = _file()
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


def delete(deployment_id: str) -> bool:
    """
    Forget a deployment record. Nothing in the cloud is touched.

    Said in the docstring because it is the first thing somebody will
    wonder: deleting the definition of 500 networks does not delete 500
    networks. Tearing down is an apply of its own, not a side effect of
    tidying a list.
    """
    path = _file()
    with jsonfile.locked(path):
        rows = jsonfile.read(path, [], expect=list)
        kept = [r for r in rows if r.get("id") != deployment_id]
        if len(kept) == len(rows):
            return False
        jsonfile.write(path, kept)
    return True


# ---------------------------------------------------------------------------
# Names and paths
# ---------------------------------------------------------------------------

def slug_for(name: str) -> str:
    """
    A folder name from a display name, and refused if nothing survives.

    Lowercase, digits and dashes only, because it becomes a path on the
    runner and a path in git, and both are the wrong place for a space or
    a dot-dot. The check is on the result rather than on the input so
    that "Glasgow — Phase 2" becomes `glasgow-phase-2` rather than an
    error about the dash.
    """
    text = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    text = text[:64].rstrip("-")
    if not _SLUG_RE.match(text):
        raise DeploymentError(
            "That name leaves nothing usable once spaces and punctuation are "
            "taken out. Give it a name with at least one letter or digit.")
    return text


def _checked_slug(slug: str) -> str:
    """A slug that arrived from a record or a request, and cannot escape."""
    text = str(slug or "").strip()
    if not _SLUG_RE.match(text) or "/" in text or ".." in text:
        raise DeploymentError(f"{text!r} is not a deployment folder name.")
    return text


def runner_paths(slug: str) -> dict[str, str]:
    """The four paths as the runner sees them — no repository prefix."""
    slug = _checked_slug(slug)
    return {name: f"{FOLDER}/{slug}/{name}" for name in FILES}


def git_paths(slug: str) -> dict[str, str]:
    """The four paths as the repository sees them — under the project dir."""
    return {name: PROJECT_PREFIX + path
            for name, path in runner_paths(slug).items()}


# ---------------------------------------------------------------------------
# The data set
# ---------------------------------------------------------------------------

def sites_from_upload(text: str, mapping: dict,
                      headed: bool | None = None) -> list[dict]:
    """
    Turn an uploaded table into sites, using the mapping the user confirmed.

    `mapping` is ``{field: column header}`` for the fields in
    :data:`SITE_FIELDS`, plus an optional ``extra`` list of further columns
    to carry through verbatim as site variables — a `region` or a
    `timezone` the scheme wants to read per site. Refuses rather than
    guesses when no name column is nominated, and refuses a duplicate
    name, because two sites called `Glasgow` produce one network and a
    plan that says both were created.
    """
    from backend.ansible_inventories import InventoryError, _all_rows, preview

    try:
        read = preview(text, headed=headed)
    except InventoryError as exc:
        raise DeploymentError(str(exc)) from exc
    if read["kind"] != "table":
        raise DeploymentError(
            "A site list needs columns — at least a name per site. A plain "
            "list of names is fine if the first line is a header.")

    name_col = str((mapping or {}).get("name") or "").strip()
    if not name_col:
        raise DeploymentError(
            "Say which column holds the site name. ShellMate will not "
            "guess: a header called 'site' and one called 'Network Name' "
            "mean the same thing, and one called 'serial' does not.")
    headers = read["headers"]
    for field, column in _columns(mapping).items():
        if column and column not in headers:
            raise DeploymentError(
                f"There is no column called {column!r} (nominated for {field}).")

    index = {name: i for i, name in enumerate(headers)}
    sites: list[dict] = []
    seen: set[str] = set()
    for row in _all_rows(text, read):
        def cell(column: str) -> str:
            position = index.get(column)
            if position is None or position >= len(row):
                return ""
            return (row[position] or "").strip()

        name = cell(name_col)
        if not name:
            continue
        if name.lower() in seen:
            raise DeploymentError(
                f"Two rows are called {name!r}. Site names have to be unique "
                "— two sites with one name would build one network and "
                "report both as created.")
        seen.add(name.lower())

        site: dict[str, Any] = {"name": name}
        tags = cell(str(mapping.get("tags") or ""))
        if tags:
            site["tags"] = [t.strip() for t in re.split(r"[,;]", tags) if t.strip()]
        serials = {k: cell(str(mapping.get(k) or "")) for k in ("mx", "ms")}
        serials = {k: v for k, v in serials.items() if v}
        if serials:
            site["serials"] = serials
        for column in (mapping.get("extra") or []):
            value = cell(str(column))
            if value:
                site[_var_name(str(column))] = value
        sites.append(site)

    if not sites:
        raise DeploymentError(
            f"No row had anything in {name_col!r}, so there are no sites. "
            "Check the column, or the file.")
    return sites


def _columns(mapping: dict) -> dict[str, str]:
    return {field: str((mapping or {}).get(field) or "").strip()
            for field in SITE_FIELDS}


def _var_name(column: str) -> str:
    """A column header as an Ansible variable name."""
    text = re.sub(r"[^a-z0-9_]+", "_", column.strip().lower()).strip("_")
    if not text or text[0].isdigit():
        text = f"col_{text}"
    return text


# ---------------------------------------------------------------------------
# The record
# ---------------------------------------------------------------------------

def save(fields: dict) -> dict:
    """
    Create or update a deployment.

    A new record takes its slug from its name once and keeps it: the slug
    is a folder in git and on the runner, and renaming the folder under a
    deployment that has been applied would orphan its history. Renaming
    the display name is fine; the slug is not a display name.
    """
    name = str(fields.get("name") or "").strip()
    if not _NAME_RE.match(name):
        raise DeploymentError(
            "A deployment needs a name of up to eighty characters, starting "
            "with a letter or a digit.")
    provider = str(fields.get("provider") or "").strip().lower()
    if provider not in PROVIDERS:
        raise DeploymentError("The provider is one of: " + ", ".join(PROVIDERS) + ".")

    existing = get(str(fields.get("id") or "")) if fields.get("id") else None
    slug = existing["slug"] if existing else slug_for(name)

    scheme = fields.get("scheme") if isinstance(fields.get("scheme"), dict) else {}
    sites = fields.get("sites") if isinstance(fields.get("sites"), list) else \
        (existing or {}).get("sites") or []
    mapping = fields.get("mapping") if isinstance(fields.get("mapping"), dict) else \
        (existing or {}).get("mapping") or {}

    record = {
        "id":             existing["id"] if existing else str(uuid.uuid4()),
        "slug":           slug,
        "name":           name,
        "provider":       provider,
        "description":    str(fields.get("description") or "").strip(),
        "template_id":    str(fields.get("template_id") or (existing or {}).get("template_id") or ""),
        "environment_id": str(fields.get("environment_id") or "").strip(),
        "scheme":         {str(k): v for k, v in scheme.items()},
        "sites":          [s for s in sites if isinstance(s, dict) and s.get("name")],
        "mapping":        mapping,
        # What the last commit and the last runs were. Never cleared by a
        # save: editing the scheme does not un-happen the apply, and the
        # ids the apply returned are the only record of what was built.
        "last_commit":    (existing or {}).get("last_commit") or None,
        "last_plan":      (existing or {}).get("last_plan") or None,
        "last_apply":     (existing or {}).get("last_apply") or None,
        "site_ids":       (existing or {}).get("site_ids") or {},
        "created":        (existing or {}).get("created") or _now(),
        "updated":        _now(),
    }
    _replace(record)
    logger.info("Saved deployment %s (%s, %d sites)", name, provider,
                len(record["sites"]))
    return record


# ---------------------------------------------------------------------------
# Rendering — the bytes that go to git and to the runner
# ---------------------------------------------------------------------------

def _dump(document: dict) -> str:
    """
    YAML as the runner reads it and git diffs it.

    `sort_keys=True` is the determinism: the same record renders to the
    same bytes whatever order the browser sent the keys in, so the commit
    and the PUT match and "did anything change" is a comparison, not a
    guess. `allow_unicode` keeps a site called `Zürich` as itself rather
    than as an escape somebody has to decode to review a diff.
    """
    return yaml.safe_dump(document, sort_keys=True, allow_unicode=True,
                          default_flow_style=False, width=100)


def render_sites(record: dict) -> str:
    return _dump({"sites": list(record.get("sites") or [])})


def render_scheme(record: dict) -> str:
    document = dict(record.get("scheme") or {})
    document["deployment"] = record.get("slug", "")
    document["provider"] = record.get("provider", "")
    return _dump(document)


def files_for(record: dict, plan_text: str, apply_text: str) -> dict[str, bytes]:
    """
    The four files as bytes, keyed by the runner-side path.

    The playbooks are passed in rather than read from a template here,
    because they are the runner's: ShellMate commits them verbatim and
    never rewrites them. Rendering the scheme is the only substitution.
    """
    slug = record.get("slug", "")
    paths_by_name = runner_paths(slug)
    return {
        paths_by_name["sites.yml"]:  render_sites(record).encode("utf-8"),
        paths_by_name["scheme.yml"]: render_scheme(record).encode("utf-8"),
        paths_by_name["plan.yml"]:   (plan_text or "").encode("utf-8"),
        paths_by_name["apply.yml"]:  (apply_text or "").encode("utf-8"),
    }


def as_git_tree(files: dict[str, bytes]) -> dict[str, bytes]:
    """The same bytes under the repository's paths."""
    return {PROJECT_PREFIX + path: content for path, content in files.items()}


# ---------------------------------------------------------------------------
# What the runs said
# ---------------------------------------------------------------------------

def record_commit(deployment_id: str, sha: str, url: str = "") -> dict | None:
    record = get(deployment_id)
    if record is None:
        return None
    record["last_commit"] = {"sha": sha, "url": url, "at": _now()}
    record["updated"] = _now()
    return _replace(record)


def record_run(deployment_id: str, kind: str, job_id: str,
               result: dict | None = None) -> dict | None:
    """
    Remember a plan or an apply, and what it returned.

    An apply's per-site ids are folded into `site_ids`, keyed by site name,
    because they are the only record of what was built: the second run —
    claiming serials — and every later change needs the network id, and
    looking it up by name is how a renamed network becomes a new one.
    """
    if kind not in ("plan", "apply"):
        raise DeploymentError("A run is a plan or an apply.")
    record = get(deployment_id)
    if record is None:
        return None
    entry = {"job": str(job_id), "at": _now(),
             "result": result if isinstance(result, dict) else None}
    record[f"last_{kind}"] = entry
    if kind == "apply" and isinstance(result, dict):
        body = result.get("apply") if isinstance(result.get("apply"), dict) else result
        for site in body.get("sites") or []:
            if isinstance(site, dict) and site.get("name") and site.get("ids"):
                record.setdefault("site_ids", {})[site["name"]] = site["ids"]
    record["updated"] = _now()
    return _replace(record)


def apply_allowed(record: dict) -> str:
    """
    Why an apply may not start, or "" when it may.

    Meraki has no check mode — none of its network modules declare it — so
    a `--check` run skips every task and reports success. The plan
    playbook is therefore the only preview there is, and this is the gate
    that makes it one: no plan, no apply; a plan whose result was never
    fetched, no apply; a plan older than the definition, no apply.
    """
    plan = record.get("last_plan") or {}
    if not plan.get("job"):
        return "Run a plan first. There is no other preview: Meraki ignores " \
               "check mode, so a plan is the only way to see what an apply " \
               "would do before it does it."
    if not isinstance(plan.get("result"), dict):
        return "The plan has not finished, or its result has not been read " \
               "yet. Apply waits for a plan somebody has looked at."
    if float(plan.get("at") or 0) < float(record.get("updated") or 0) - 1:
        return "The definition changed after the last plan. Plan again, so " \
               "the apply matches what you have read."
    return ""
