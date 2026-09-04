"""
ansible.py — Driving Ansible from ShellMate, through ansible-runner-service (#585).

ShellMate does not run Ansible. It talks to an **ansible-runner-service**
container over its REST API and shows what is happening: which playbooks
exist, what a run is doing task by task, what it changed, and what it said
when it failed. The container does the work; this is the window onto it.

What the service actually offers, read from its source rather than assumed:

    GET    /api/v1/playbooks                    the names it holds
    POST   /api/v1/playbooks/<name>             start one; JSON body is the
                                                extra vars, ?limit=a,b and
                                                ?check are the only filters
    POST   /api/v1/playbooks/<name>/tags/<tags> start one, tags only
    GET    /api/v1/playbooks/<play_uuid>        what that run is doing
    DELETE /api/v1/playbooks/<play_uuid>        ask it to stop
    GET    /api/v1/jobs/<play_uuid>/events      every event so far
    GET    /api/v1/jobs/<play_uuid>/events/<id> one task's output
    GET    /api/v1/groups, /api/v1/hosts        the inventory it holds
    POST   /api/v1/hosts/<host>/groups/<group>  put a host in a group

Every reply is the same envelope — ``{"status", "msg", "data"}`` — with the
status mapped onto the HTTP code, so ``OK`` is 200 and ``STARTED`` is 202.
:func:`_unwrap` is the one place that knows this.

Three consequences of the service's design that shape everything here:

- **It authenticates with client certificates, not a token.** Mutual TLS is
  the only mechanism it has. So the settings are a certificate and a key,
  and :func:`configured` is false until both exist on disk.
- **There is no endpoint that uploads a playbook.** Playbooks live in the
  container's project directory and the API only lists and runs them. A
  library edited in ShellMate therefore has to *reach* the container some
  other way, and ShellMate already knows how to copy a file to a host it
  has an SSH session with. :func:`playbook_transfer_plan` says exactly
  that, rather than pretending an upload API exists.
- **Its inventory is its own.** "Use ShellMate's estate" means pushing
  hosts and groups into the service's inventory through the endpoints
  above — :func:`inventory_from_estate` shapes them, and nothing is sent
  until somebody asks for it.
"""

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from backend import jsonfile, paths

logger = logging.getLogger(__name__)

#: Where a library of playbooks written in ShellMate is kept. The service
#: cannot be sent one over its API, so these are authored here and copied
#: across — see :func:`playbook_transfer_plan`.
def library_dir() -> Path:
    return paths.data_dir() / "playbooks"


#: What a run is called at each stage, in the service's own words. Anything
#: outside this set is passed through rather than guessed at.
RUNNING_STATES = ("starting", "started", "running")
FINISHED_STATES = ("successful", "failed", "canceled", "cancelled", "timeout")


class AnsibleError(RuntimeError):
    """The service refused, or could not be reached. Carries its own words."""

    def __init__(self, message: str, status: str = "", code: int = 0):
        super().__init__(message)
        self.status = status
        self.code = code


class NotConfigured(AnsibleError):
    """No runner has been set up yet. Not a failure — a thing not done."""


@dataclass
class RunnerConfig:
    """Where the runner is and how to prove who we are."""

    url: str = ""
    #: Client certificate and its key: mutual TLS is the service's only
    #: authentication. Paths, not contents — the private key stays a file
    #: with its own permissions rather than a string in settings.json.
    client_cert: str = ""
    client_key: str = ""
    #: A CA bundle to verify the service's own certificate with. Empty and
    #: `verify_tls` false means a self-signed development certificate is
    #: accepted, which is what the service ships with.
    ca_cert: str = ""
    verify_tls: bool = True
    timeout: float = 30.0

    @property
    def ready(self) -> bool:
        return bool(self.url and self.client_cert and self.client_key)

    def missing(self) -> list[str]:
        """What is still needed, in words fit for the panel."""
        gaps = []
        if not self.url:
            gaps.append("the runner's address")
        if not self.client_cert:
            gaps.append("a client certificate")
        if not self.client_key:
            gaps.append("the certificate's key")
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
    return RunnerConfig(
        url=str(block.get("runner_url") or "").rstrip("/"),
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
    """An httpx client carrying the certificate the service demands."""
    import httpx

    verify: Any = cfg.ca_cert if cfg.ca_cert else cfg.verify_tls
    return httpx.Client(timeout=cfg.timeout, verify=verify,
                        cert=(cfg.client_cert, cfg.client_key),
                        headers={"User-Agent": "ShellMate-ansible"})


def _unwrap(response, expect: tuple[int, ...] = (200, 202)) -> Any:
    """
    The service's envelope, or an error carrying what it said.

    Every reply is ``{"status", "msg", "data"}``. A failure is far more
    useful with the service's own message in it — "playbook file not found"
    beats "the runner answered 404" — so that is what reaches the user.
    """
    try:
        body = response.json()
    except ValueError:
        body = {}
    status = str(body.get("status") or "")
    message = str(body.get("msg") or "").strip()
    if response.status_code not in expect:
        raise AnsibleError(
            message or f"The runner answered {response.status_code}.",
            status=status, code=response.status_code)
    return body.get("data", {})


def _call(method: str, path: str, *, params: dict | None = None,
          json_body: Any = None, expect: tuple[int, ...] = (200, 202)) -> Any:
    cfg = config()
    if not cfg.ready:
        raise NotConfigured("No Ansible runner is set up yet. "
                            "Add one under Settings → Ansible.")
    import httpx

    try:
        with _client(cfg) as client:
            response = client.request(method, f"{cfg.url}{path}",
                                      params=params or None, json=json_body)
    except httpx.HTTPError as exc:
        raise AnsibleError(
            f"Could not reach the runner at {cfg.url} "
            f"({exc.__class__.__name__}).") from exc
    return _unwrap(response, expect)


# ---------------------------------------------------------------------------
# Playbooks and runs
# ---------------------------------------------------------------------------
def ping() -> dict:
    """Whether the runner answers, and what it holds. Never raises."""
    cfg = config()
    if not cfg.ready:
        return {"reachable": False, "configured": False, "detail": ", ".join(cfg.missing())}
    try:
        names = list_playbooks()
    except AnsibleError as exc:
        return {"reachable": False, "configured": True, "detail": str(exc)}
    return {"reachable": True, "configured": True, "playbooks": len(names),
            "detail": f"{len(names)} playbook(s) available"}


def list_playbooks() -> list[str]:
    """The playbooks the runner holds, by name."""
    data = _call("GET", "/api/v1/playbooks")
    names = data.get("playbooks") if isinstance(data, dict) else data
    return sorted(str(n) for n in (names or []))


def start(playbook: str, *, extra_vars: dict | None = None,
          limit: list[str] | str = "", check: bool = False,
          tags: str = "") -> dict:
    """
    Start a run. Returns ``{"play_uuid", "status"}``.

    ``limit`` is the service's only host filter and it validates the names
    against its own inventory, so a limit naming a host the runner has
    never heard of is refused there rather than here — with its message.
    ``check`` is Ansible's dry run, which is the safe way to see what a
    playbook would do to an estate before it does it.
    """
    if not playbook or "/" in playbook or "\\" in playbook:
        raise AnsibleError("That is not a playbook name.")
    params: dict[str, str] = {}
    hosts = limit if isinstance(limit, str) else ",".join(limit)
    if hosts:
        params["limit"] = hosts
    if check:
        params["check"] = "True"
    path = f"/api/v1/playbooks/{playbook}"
    if tags:
        path += f"/tags/{tags}"
    data = _call("POST", path, params=params, json_body=extra_vars or {},
                 expect=(200, 202))
    uuid = (data or {}).get("play_uuid", "")
    if not uuid:
        raise AnsibleError("The runner started nothing it could name.")
    logger.info("Ansible run %s started from %s", uuid, playbook)
    return {"play_uuid": uuid, "status": "starting"}


def status(play_uuid: str) -> dict:
    """What that run is doing now."""
    data = _call("GET", f"/api/v1/playbooks/{play_uuid}")
    state = str((data or {}).get("status") or "").lower()
    return {"play_uuid": play_uuid, "status": state,
            "running": state in RUNNING_STATES,
            "finished": state in FINISHED_STATES,
            "raw": data}


def cancel(play_uuid: str) -> dict:
    """
    Ask the runner to stop a run.

    The service answers 404 for a uuid it knows but is no longer running,
    which is a normal outcome rather than an error — a run that finished
    while somebody reached for Stop.
    """
    try:
        _call("DELETE", f"/api/v1/playbooks/{play_uuid}", expect=(200,))
    except AnsibleError as exc:
        if exc.code == 404:
            return {"cancelled": False, "detail": "That run had already finished."}
        raise
    return {"cancelled": True, "detail": "Cancellation requested."}


def events(play_uuid: str, since: str = "") -> dict:
    """
    Every event of a run, newest last, shaped for the panel.

    The service returns them keyed by event id in its own order; the
    counter that prefixes each id is what actually orders them, so that is
    what is sorted on. ``since`` returns only what came after that id,
    which is what makes polling cheap while a long play runs.
    """
    data = _call("GET", f"/api/v1/jobs/{play_uuid}/events") or {}
    raw = data.get("events") or {}
    out = []
    for key, value in raw.items():
        entry = dict(value or {})
        entry["event_id"] = key
        entry["counter"] = _counter_of(key)
        out.append(entry)
    out.sort(key=lambda e: e["counter"])
    if since:
        mark = _counter_of(since)
        out = [e for e in out if e["counter"] > mark]
    return {"events": out, "total": int(data.get("total_events") or len(raw))}


def event(play_uuid: str, event_id: str) -> dict:
    """One task's own output, which is where a failure explains itself."""
    return _call("GET", f"/api/v1/jobs/{play_uuid}/events/{event_id}") or {}


_COUNTER_RE = re.compile(r"^(\d+)")


def _counter_of(event_id: str) -> int:
    """The number the service prefixes each event id with, or 0."""
    found = _COUNTER_RE.match(str(event_id or ""))
    return int(found.group(1)) if found else 0


def summarise(event_list: list[dict]) -> dict:
    """
    A run in one line: tasks seen, and what happened to hosts.

    Built from the event names Ansible itself emits, so it says what the
    play did rather than what ShellMate guessed. Anything unrecognised is
    counted as "other" instead of being forced into a bucket.
    """
    counts = {"tasks": 0, "ok": 0, "changed": 0, "failed": 0,
              "unreachable": 0, "skipped": 0, "other": 0}
    for entry in event_list or []:
        name = str(entry.get("event") or "")
        if name == "playbook_on_task_start":
            counts["tasks"] += 1
        elif name == "runner_on_ok":
            counts["ok"] += 1
        elif name == "runner_on_changed":
            counts["changed"] += 1
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
# The inventory the runner holds
# ---------------------------------------------------------------------------
def list_groups() -> list[str]:
    data = _call("GET", "/api/v1/groups")
    names = data.get("groups") if isinstance(data, dict) else data
    return sorted(str(n) for n in (names or []))


def list_hosts(group: str = "") -> list[str]:
    path = f"/api/v1/groups/{group}" if group else "/api/v1/hosts"
    data = _call("GET", path)
    names = data.get("members") if isinstance(data, dict) else data
    if isinstance(data, dict) and names is None:
        names = data.get("hosts")
    return sorted(str(n) for n in (names or []))


#: An Ansible group name is not a ShellMate group name. Ours nest with "/"
#: and may hold spaces ("site-004/core switches"); Ansible's may not.
_UNSAFE = re.compile(r"[^A-Za-z0-9_]+")


def ansible_group_name(key: str) -> str:
    """``site-004/core switches`` → ``site_004_core_switches``."""
    cleaned = _UNSAFE.sub("_", str(key or "")).strip("_").lower()
    if not cleaned:
        return ""
    return cleaned if cleaned[0].isalpha() or cleaned[0] == "_" else f"g_{cleaned}"


def inventory_from_estate(group: str = "") -> dict:
    """
    ShellMate's own connections and groups, shaped as an Ansible inventory.

    Hosts are addresses, because that is what Ansible connects to and what
    a saved connection actually holds; the connection's name travels as a
    host variable so a report can say "core-1" rather than an address.
    A serial connection has no address to reach over the network and is
    left out with its reason — silently dropping it would leave somebody
    hunting for a device that never ran.

    Nothing is sent anywhere by this function. It only says what would be.
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
            skipped.append({"name": name, "why": f"{kind} connections have no address to reach"})
            continue
        address = (profile.get("hostname") or "").strip()
        if not address:
            skipped.append({"name": name, "why": "no address"})
            continue
        hostvars.setdefault(address, {
            "shellmate_name": name,
            "ansible_host": address,
            "ansible_port": int(profile.get("port") or 22),
        })
        if profile.get("username"):
            hostvars[address]["ansible_user"] = profile["username"]
        # The platform ShellMate identified picks Ansible's connection
        # plugin: a network device answers to network_cli, not to the
        # default ssh connection with a shell on the far end.
        platform = (profile.get("platform") or profile.get("last_seen_platform") or "").lower()
        network_os = ANSIBLE_NETWORK_OS.get(platform)
        if network_os:
            hostvars[address]["ansible_network_os"] = network_os
            hostvars[address]["ansible_connection"] = "ansible.netcommon.network_cli"
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


#: ShellMate's platform ids to Ansible's `ansible_network_os`. Only where
#: the mapping is unambiguous; a platform absent here gets no network_os
#: and Ansible's own default connection, which is the honest answer.
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


def push_inventory(inventory: dict) -> dict:
    """
    Put an estate's hosts and groups into the runner's own inventory.

    The service has no bulk endpoint: a host joins a group one call at a
    time. Failures are collected rather than raised, because half an
    inventory pushed and no report of what failed is worse than a slow
    answer that says which three hosts did not go.
    """
    added: list[str] = []
    failed: list[dict] = []
    for group, hosts in (inventory.get("groups") or {}).items():
        try:
            _call("POST", f"/api/v1/groups/{group}", expect=(200, 202))
        except AnsibleError as exc:
            if exc.code not in (409,):                    # already there is fine
                failed.append({"target": group, "why": str(exc)})
                continue
        for host in hosts:
            try:
                _call("POST", f"/api/v1/hosts/{host}/groups/{group}", expect=(200, 202))
                added.append(f"{host} → {group}")
            except AnsibleError as exc:
                failed.append({"target": f"{host} → {group}", "why": str(exc)})
    return {"added": len(added), "failed": failed, "detail": added[:20]}


# ---------------------------------------------------------------------------
# The playbook library, and the gap the service leaves
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

    The service's API lists and runs playbooks; it has no endpoint that
    accepts one. So a library edited in ShellMate has to reach the
    container's project directory another way, and ShellMate already knows
    how to write a file to a host it has an SSH session with (see
    `connections/sftp.py`). This returns what to do rather than failing at
    the point somebody presses Run and the runner says "not found".
    """
    project = ""
    try:
        from backend.settings_store import peek
        project = str((peek("ansible") or {}).get("project_dir") or "")
    except Exception:                                     # pragma: no cover
        pass
    project = project or "/usr/share/ansible-runner-service/project"
    return {
        "name": _library_path(name).name,
        "project_dir": project,
        "target": f"{project.rstrip('/')}/{_library_path(name).name}",
        "why": ("The runner service has no API for uploading a playbook, so "
                "ShellMate copies it over an SSH session to the machine "
                "running the container."),
    }
