"""
ansible.py — Driving Ansible from ShellMate, through a runner service (#585).

ShellMate does not run Ansible. It talks to a container that does and shows
what is happening: which playbooks exist, what a run is doing task by task,
what it changed, and what it said when it failed. The container does the
work; this is the window onto it.

The service is a small FastAPI wrapper over the ``ansible_runner`` library
(github.com/sjohnston1972/ansible). ShellMate first targeted Red Hat's
`ansible-runner-service`, which turned out to be archived since 2022 and
pinned to Flask 1.x — so the container was built fresh on a current base
and this module was rewritten to match it. What it offers:

    GET    /health                        versions and where its data lives
    GET    /api/v1/playbooks              what is in the project directory
    GET    /api/v1/playbooks/<name>       that playbook's text
    POST   /api/v1/playbooks/<name>       run it; typed JSON body
    GET    /api/v1/jobs                   every run it knows of
    GET    /api/v1/jobs/<id>              one run's state
    GET    /api/v1/jobs/<id>/events       ansible-runner's own events
    GET    /api/v1/jobs/<id>/stdout       the run as it would look in a shell
    DELETE /api/v1/jobs/<id>              cancel a run this process started
    POST   /api/v1/galaxy/install         install requirements.yml

Three things about it shape everything here:

- **The inventory travels with the run.** The service takes
  ``inventory_content`` inline and writes it to a per-job file, so
  ShellMate generates an inventory from its own connections and groups and
  sends it with the job. Nothing of ShellMate's accumulates on the
  container, and there is no second inventory to keep in step. A path may
  be given instead, for an inventory the container already holds.
- **There is no endpoint that uploads a playbook.** Playbooks live in the
  project directory, which is a bind mount from the container's host — so
  a library written here reaches it by being copied to that host over an
  SSH session ShellMate already has. :func:`playbook_transfer_plan` says
  so rather than pretending an upload exists.
- **Authentication is the deployment's choice.** The service may run with
  no auth on a private bridge, or behind a bearer token. ShellMate sends
  the token when one is configured and works without one, so neither
  choice needs a change here.
"""

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backend import paths

logger = logging.getLogger(__name__)


def library_dir() -> Path:
    """Playbooks written in ShellMate. Copied to the runner, never uploaded."""
    return paths.data_dir() / "playbooks"


#: ansible-runner's own vocabulary for where a run has got to.
RUNNING_STATES = ("starting", "running")
FINISHED_STATES = ("successful", "failed", "timeout", "canceled", "cancelled", "unknown")


class AnsibleError(RuntimeError):
    """The runner refused, or could not be reached. Carries its own words."""

    def __init__(self, message: str, code: int = 0):
        super().__init__(message)
        self.code = code


class NotConfigured(AnsibleError):
    """No runner has been set up yet. Not a failure — a thing not done."""


@dataclass
class RunnerConfig:
    """Where the runner is, and how to prove who we are if it asks."""

    url: str = ""
    #: Sent as ``Authorization: Bearer`` when set. A service on a private
    #: bridge may have no auth at all; one reachable from a management
    #: network should, and ShellMate is ready for either.
    token: str = ""
    #: Client certificate and key, for a deployment that puts mutual TLS in
    #: front. Optional; both are needed or neither is used.
    client_cert: str = ""
    client_key: str = ""
    ca_cert: str = ""
    verify_tls: bool = True
    timeout: float = 30.0

    @property
    def ready(self) -> bool:
        return bool(self.url)

    def missing(self) -> list[str]:
        """What is still needed, in words fit for the panel."""
        gaps = []
        if not self.url:
            gaps.append("the runner's address")
        if bool(self.client_cert) != bool(self.client_key):
            gaps.append("both halves of the client certificate, or neither")
        for label, value in (("client certificate", self.client_cert),
                             ("certificate key", self.client_key),
                             ("CA certificate", self.ca_cert)):
            if value and not Path(value).exists():
                gaps.append(f"the {label} at {value} (no such file)")
        return gaps


def config() -> RunnerConfig:
    """The runner settings as they stand."""
    try:
        from backend.settings_store import peek
        block = peek("ansible") or {}
    except Exception:                                     # pragma: no cover
        block = {}
    # A token is a secret, so it lives in the vault rather than in
    # settings.json; the block's own field is only ever the one just typed,
    # on its way there. The environment is the last resort, for a container
    # and an app started by the same script.
    token = str(block.get("token") or "")
    if not token:
        try:
            from backend.vault import vault
            token = vault.get("ansible_token", "") or ""
        except Exception:                                 # pragma: no cover
            token = ""
    if not token:
        import os as _os
        token = _os.environ.get("ANSIBLE_RUNNER_TOKEN", "") or ""
    return RunnerConfig(
        url=str(block.get("runner_url") or "").rstrip("/"),
        token=token,
        client_cert=str(block.get("client_cert") or ""),
        client_key=str(block.get("client_key") or ""),
        ca_cert=str(block.get("ca_cert") or ""),
        verify_tls=bool(block.get("verify_tls", True)),
        timeout=float(block.get("timeout") or 30.0),
    )


def configured() -> bool:
    return config().ready


# ---------------------------------------------------------------------------
# Talking to it
# ---------------------------------------------------------------------------
def _client(cfg: RunnerConfig):
    import httpx

    headers = {"User-Agent": "ShellMate-ansible"}
    if cfg.token:
        headers["Authorization"] = f"Bearer {cfg.token}"
    kwargs: dict[str, Any] = {"timeout": cfg.timeout, "headers": headers}
    if cfg.url.startswith("https://"):
        kwargs["verify"] = cfg.ca_cert if cfg.ca_cert else cfg.verify_tls
        if cfg.client_cert and cfg.client_key:
            kwargs["cert"] = (cfg.client_cert, cfg.client_key)
    return httpx.Client(**kwargs)


def _detail(response) -> str:
    """
    The service's own message for a refusal.

    FastAPI puts it in ``detail``; that is the sentence worth showing —
    "playbook 'site.yml' not found under /runner/project" tells somebody
    what to do, where "the runner answered 404" does not.
    """
    try:
        body = response.json()
    except ValueError:
        return ""
    if isinstance(body, dict):
        detail = body.get("detail")
        if isinstance(detail, list) and detail:           # pydantic validation
            first = detail[0]
            if isinstance(first, dict):
                where = ".".join(str(p) for p in (first.get("loc") or [])[1:])
                return f"{where}: {first.get('msg', '')}".strip(": ")
        if detail:
            return str(detail)
    return ""


def _call(method: str, path: str, *, json_body: Any = None,
          params: dict | None = None, text: bool = False) -> Any:
    cfg = config()
    if not cfg.ready:
        raise NotConfigured("No Ansible runner is set up yet. "
                            "Add one under Settings → Ansible.")
    import httpx

    try:
        with _client(cfg) as client:
            response = client.request(method, f"{cfg.url}{path}",
                                      json=json_body, params=params or None)
    except httpx.HTTPError as exc:
        raise AnsibleError(
            f"Could not reach the runner at {cfg.url} "
            f"({exc.__class__.__name__}).") from exc
    if response.status_code >= 400:
        message = _detail(response) or f"The runner answered {response.status_code}."
        if response.status_code in (401, 403):
            message = (f"The runner refused ShellMate ({response.status_code}). "
                       "Check the token under Settings → Ansible.")
        raise AnsibleError(message, code=response.status_code)
    if text:
        return response.text
    try:
        return response.json()
    except ValueError:
        return {}


# ---------------------------------------------------------------------------
# The runner, its playbooks and its runs
# ---------------------------------------------------------------------------
def ping() -> dict:
    """Whether the runner answers, and what it is. Never raises."""
    cfg = config()
    if not cfg.ready:
        return {"reachable": False, "configured": False,
                "detail": ", ".join(cfg.missing())}
    gaps = cfg.missing()
    if gaps:
        return {"reachable": False, "configured": True, "detail": ", ".join(gaps)}
    # /health needs no token by design, so it separates two failures that
    # look alike from the outside: a runner that cannot be reached, and one
    # that can be reached but will not talk to us.
    try:
        health = _call("GET", "/health") or {}
    except AnsibleError as exc:
        return {"reachable": False, "configured": True, "detail": str(exc)}
    try:
        books = list_playbooks()
    except AnsibleError as exc:
        if exc.code in (401, 403):
            return {"reachable": True, "configured": True, "authenticated": False,
                    "ansible_core": health.get("ansible_core", ""),
                    "detail": ("The runner is there but will not accept ShellMate: "
                               "check the token under Settings → Ansible.")}
        return {"reachable": False, "configured": True, "detail": str(exc)}
    core = health.get("ansible_core", "")
    return {
        "reachable": True, "configured": True,
        "playbooks": len(books),
        "ansible_core": core,
        "ansible_runner": health.get("ansible_runner", ""),
        "authenticated": bool(cfg.token),
        "detail": (f"{len(books)} playbook(s), ansible-core {core}" if core
                   else f"{len(books)} playbook(s)"),
    }


def list_playbooks() -> list[dict]:
    """What is in the runner's project directory: name, size, when changed."""
    data = _call("GET", "/api/v1/playbooks") or {}
    out = []
    for entry in data.get("playbooks") or []:
        if isinstance(entry, dict):
            out.append({"name": str(entry.get("name") or ""),
                        "bytes": int(entry.get("size") or 0),
                        "modified": entry.get("modified") or ""})
        else:                                             # a bare name
            out.append({"name": str(entry), "bytes": 0, "modified": ""})
    return sorted(out, key=lambda p: p["name"])


def read_remote_playbook(name: str) -> str:
    """A playbook on the runner, as text. Read-only: it is theirs, not ours."""
    return _call("GET", f"/api/v1/playbooks/{name}", text=True) or ""


def start(playbook: str, *, extra_vars: dict | None = None,
          limit: list[str] | str = "", check: bool = False, tags: str = "",
          skip_tags: str = "", inventory: str = "", inventory_content: str = "",
          verbosity: int = 0, forks: int | None = None) -> dict:
    """
    Start a run. Returns ``{"id", "playbook", "status", "inventory"}``.

    ``inventory_content`` is how ShellMate's own estate reaches a run: the
    service writes it to a file for that job alone and never touches the
    inventory it holds. Only one of the two inventory arguments may be
    given, which the service enforces and so does this.

    ``check`` is Ansible's dry run. It reports what a play *would* change
    and changes nothing, which is the honest way to try one against an
    estate for the first time.
    """
    if not playbook or playbook.startswith("/") or ".." in playbook:
        raise AnsibleError("That is not a playbook name.")
    if inventory and inventory_content:
        raise AnsibleError("Give an inventory to use or one to send, not both.")
    hosts = limit if isinstance(limit, str) else ",".join(limit)
    body: dict[str, Any] = {"check": bool(check), "extravars": extra_vars or {}}
    if hosts:
        body["limit"] = hosts
    if tags:
        body["tags"] = tags
    if skip_tags:
        body["skip_tags"] = skip_tags
    if verbosity:
        body["verbosity"] = int(verbosity)
    if forks:
        body["forks"] = int(forks)
    # Never a name ShellMate made up: ansible-runner treats an unresolvable
    # inventory string as inline content, so a constructed name could be
    # written over the runner's own inventory. Content is sent as content.
    if inventory_content:
        body["inventory_content"] = inventory_content
    elif inventory:
        body["inventory"] = inventory

    data = _call("POST", f"/api/v1/playbooks/{playbook}", json_body=body) or {}
    ident = str(data.get("id") or "")
    if not ident:
        raise AnsibleError("The runner started nothing it could name.")
    logger.info("Ansible run %s started from %s%s", ident, playbook,
                " (check mode)" if check else "")
    return {"id": ident, "playbook": data.get("playbook") or playbook,
            "status": data.get("status") or "starting",
            "inventory": data.get("inventory") or ""}


def jobs() -> list[dict]:
    """Every run the runner knows of, newest first — including past ones."""
    data = _call("GET", "/api/v1/jobs") or {}
    return list(data.get("jobs") or [])


def status(job_id: str) -> dict:
    """What one run is doing now."""
    data = _call("GET", f"/api/v1/jobs/{job_id}") or {}
    state = str(data.get("status") or "").lower()
    return {**data, "id": data.get("id") or job_id, "status": state,
            "running": state in RUNNING_STATES,
            "finished": state in FINISHED_STATES}


def cancel(job_id: str) -> dict:
    """
    Ask the runner to stop a run.

    The service can only cancel a run its own process started, and answers
    404 otherwise — a run that finished, or one from before a restart.
    That is an outcome, not an error.
    """
    try:
        data = _call("DELETE", f"/api/v1/jobs/{job_id}") or {}
    except AnsibleError as exc:
        if exc.code == 404:
            return {"cancelled": False,
                    "detail": "That run is no longer one the runner can stop."}
        raise
    return {"cancelled": bool(data.get("cancelled", True)),
            "detail": "Cancellation requested."}


def events(job_id: str, since: int = 0) -> dict:
    """
    A run's events, oldest first, and only those after ``since``.

    ansible-runner numbers its own events, so the counter orders them and
    makes polling cheap: the panel asks for what it has not seen rather
    than re-reading the whole play every second.
    """
    data = _call("GET", f"/api/v1/jobs/{job_id}/events") or {}
    out = []
    for raw in data.get("events") or []:
        if not isinstance(raw, dict):
            continue
        counter = int(raw.get("counter") or 0)
        if counter <= since:
            continue
        payload = raw.get("event_data") or {}
        out.append({
            "counter": counter,
            "uuid": raw.get("uuid") or "",
            "event": raw.get("event") or "",
            "task": payload.get("task") or "",
            "host": payload.get("host") or "",
            "play": payload.get("play") or "",
            "changed": bool((payload.get("res") or {}).get("changed")),
            "stdout": raw.get("stdout") or "",
        })
    out.sort(key=lambda e: e["counter"])
    return {"events": out, "last": out[-1]["counter"] if out else since}


def stdout(job_id: str) -> str:
    """The whole run as it would have looked in a shell."""
    return _call("GET", f"/api/v1/jobs/{job_id}/stdout", text=True) or ""


def install_requirements(requirements: str = "requirements.yml") -> dict:
    """Install the runner's galaxy requirements: roles and collections."""
    return _call("POST", "/api/v1/galaxy/install",
                 params={"requirements": requirements}) or {}


def summarise(event_list: list[dict]) -> dict:
    """
    A run in one line: tasks seen, and what happened to hosts.

    Built from the event names Ansible itself emits, so it says what the
    play did rather than what ShellMate guessed. Anything unrecognised is
    counted as "other" rather than forced into a bucket.
    """
    counts = {"tasks": 0, "ok": 0, "changed": 0, "failed": 0,
              "unreachable": 0, "skipped": 0, "other": 0}
    for entry in event_list or []:
        name = str(entry.get("event") or "")
        if name == "playbook_on_task_start":
            counts["tasks"] += 1
        elif name == "runner_on_ok":
            counts["changed" if entry.get("changed") else "ok"] += 1
        elif name in ("runner_on_failed", "runner_on_async_failed"):
            counts["failed"] += 1
        elif name == "runner_on_unreachable":
            counts["unreachable"] += 1
        elif name == "runner_on_skipped":
            counts["skipped"] += 1
        elif name.startswith("runner_on_"):
            counts["other"] += 1
    return counts


# ---------------------------------------------------------------------------
# The estate as an inventory
# ---------------------------------------------------------------------------
#: An Ansible group name is not a ShellMate group name. Ours nest with "/"
#: and may hold spaces ("site-004/core switches"); Ansible's may not.
_UNSAFE = re.compile(r"[^A-Za-z0-9_]+")


def ansible_group_name(key: str) -> str:
    """``site-004/core switches`` → ``site_004_core_switches``."""
    cleaned = _UNSAFE.sub("_", str(key or "")).strip("_").lower()
    if not cleaned:
        return ""
    return cleaned if cleaned[0].isalpha() or cleaned[0] == "_" else f"g_{cleaned}"


#: ShellMate's platform ids to Ansible's `ansible_network_os`. Only where
#: the mapping is unambiguous; a platform absent here gets none, and
#: Ansible's own default connection, which is the honest answer.
ANSIBLE_NETWORK_OS = {
    "ios": "cisco.ios.ios",
    "iosxr": "cisco.iosxr.iosxr",
    "nxos": "cisco.nxos.nxos",
    "asa": "cisco.asa.asa",
    "junos": "junipernetworks.junos.junos",
    "arista": "arista.eos.eos",
    "panos": "paloaltonetworks.panos.panos",
    "aoscx": "arubanetworks.aoscx.aoscx",
    "huawei": "community.network.ce",
    "routeros": "community.routeros.routeros",
    "fortios": "fortinet.fortios.fortios",
}


def inventory_from_estate(group: str = "") -> dict:
    """
    ShellMate's own connections and groups, shaped as an Ansible inventory.

    Hosts are addresses, because that is what Ansible dials and what a
    saved connection actually holds; the connection's name travels as a
    variable so a report can say "core-1" rather than an address. A serial
    connection has nothing to reach over the network and is left out with
    its reason — silently dropping it would leave somebody hunting for a
    device that never ran.

    Nothing is sent anywhere by this. It only says what would be.
    """
    from backend import profiles as profiles_module

    wanted = (group or "").strip().lower()
    prefix = f"{wanted}/"
    groups: dict[str, list[str]] = {}
    hostvars: dict[str, dict] = {}
    skipped: list[dict] = []

    for profile in profiles_module.get_profiles():
        kind = (profile.get("connection_type") or "ssh").lower()
        tags = profiles_module.normalise_tags(profile.get("tags"))
        if wanted and not (wanted in tags or any(t.startswith(prefix) for t in tags)):
            continue
        name = profile.get("name") or profile.get("hostname") or ""
        if kind != "ssh":
            skipped.append({"name": name,
                            "why": f"{kind} connections have no address to reach"})
            continue
        address = (profile.get("hostname") or "").strip()
        if not address:
            skipped.append({"name": name, "why": "no address"})
            continue
        entry = hostvars.setdefault(address, {
            "shellmate_name": name,
            "ansible_host": address,
            "ansible_port": int(profile.get("port") or 22),
        })
        if profile.get("username"):
            entry["ansible_user"] = profile["username"]
        platform = (profile.get("platform") or profile.get("last_seen_platform") or "").lower()
        network_os = ANSIBLE_NETWORK_OS.get(platform)
        if network_os:
            entry["ansible_network_os"] = network_os
            entry["ansible_connection"] = "ansible.netcommon.network_cli"
        for tag in tags or ["ungrouped"]:
            key = ansible_group_name(tag)
            if key:
                groups.setdefault(key, [])
                if address not in groups[key]:
                    groups[key].append(address)

    return {
        "groups": {k: sorted(v) for k, v in sorted(groups.items())},
        "hostvars": hostvars,
        "hosts": sorted(hostvars),
        "skipped": skipped,
    }


def inventory_as_ini(inventory: dict) -> str:
    """
    An inventory as the text the runner is sent.

    INI rather than YAML because host variables sit on the host's own line,
    so what is sent reads the way an engineer would have written it by
    hand — which matters when a run goes wrong and somebody wants to know
    what it was actually pointed at.
    """
    lines = ["# Generated by ShellMate from its own saved connections.",
             "# Sent with this run only; the runner's own inventory is untouched.",
             ""]
    hostvars = inventory.get("hostvars") or {}
    for group, hosts in (inventory.get("groups") or {}).items():
        lines.append(f"[{group}]")
        for host in hosts:
            pairs = " ".join(f"{k}={_ini_value(v)}"
                             for k, v in sorted((hostvars.get(host) or {}).items())
                             if not (k == "ansible_host" and v == host))
            lines.append(f"{host} {pairs}".rstrip())
        lines.append("")
    return "\n".join(lines)


def _ini_value(value: Any) -> str:
    text = str(value)
    return f"'{text}'" if " " in text else text


# ---------------------------------------------------------------------------
# ShellMate's own playbook library, and the gap the service leaves
# ---------------------------------------------------------------------------
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")


def _library_path(name: str) -> Path:
    if not _NAME_RE.match(name or "") or ".." in name:
        raise AnsibleError("A playbook name may hold letters, digits, dots, "
                           "dashes and underscores.")
    if not name.endswith((".yml", ".yaml")):
        name += ".yml"
    return library_dir() / name


def library() -> list[dict]:
    """What is in ShellMate's own playbook library."""
    folder = library_dir()
    out = []
    if not folder.exists():
        return out
    for path in sorted(folder.glob("*.y*ml")):
        try:
            stat = path.stat()
        except OSError:
            continue
        out.append({"name": path.name, "bytes": stat.st_size,
                    "modified": stat.st_mtime})
    return out


def read_playbook(name: str) -> str:
    path = _library_path(name)
    if not path.exists():
        raise AnsibleError(f"No playbook called {path.name} here.")
    return path.read_text(encoding="utf-8")


def save_playbook(name: str, text: str) -> dict:
    """
    Write one into the library, after checking it parses as YAML.

    Refusing unparseable YAML here is worth it: the alternative is finding
    out from the runner, three steps later, with a message about line 14 of
    a file the user cannot see from there.
    """
    path = _library_path(name)
    body = (text or "").replace("\r\n", "\n")
    try:
        import yaml
        parsed = yaml.safe_load(body)
    except Exception as exc:
        raise AnsibleError(f"That is not valid YAML: {exc}") from exc
    if parsed is not None and not isinstance(parsed, list):
        raise AnsibleError("A playbook is a list of plays; this is a "
                           f"{type(parsed).__name__}.")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(body, encoding="utf-8")
    tmp.replace(path)
    logger.info("Playbook saved: %s (%d bytes)", path.name, len(body))
    return {"name": path.name, "bytes": len(body.encode("utf-8"))}


def delete_playbook(name: str) -> bool:
    path = _library_path(name)
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False


def playbook_transfer_plan(name: str) -> dict:
    """
    How a playbook written here reaches the runner.

    The service lists and runs playbooks; it has no endpoint that accepts
    one. Its project directory is a bind mount from the container's host,
    so the file goes to a path on *that host* over an SSH session ShellMate
    already has — which is why this names a host path rather than a path
    inside the container.
    """
    project = ""
    try:
        from backend.settings_store import peek
        project = str((peek("ansible") or {}).get("project_dir") or "")
    except Exception:                                     # pragma: no cover
        pass
    project = project or "/runner/project"
    return {
        "name": _library_path(name).name,
        "project_dir": project,
        "target": f"{project.rstrip('/')}/{_library_path(name).name}",
        "why": ("The runner has no API for uploading a playbook, so ShellMate "
                "copies it over an SSH session to the machine hosting the "
                "container. The path is the one on that host, which the "
                "container has mounted as its project directory."),
    }
