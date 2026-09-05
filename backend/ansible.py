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
    """
    The runner refused, or could not be reached. Carries its own words.

    ``kind`` separates failures that need different actions. A certificate
    that does not verify and a container that is not running both arrive
    here as a failed connection, and reporting both as "unreachable" sends
    somebody to the firewall for a problem in a file on their own disk.
    """

    def __init__(self, message: str, code: int = 0, kind: str = ""):
        super().__init__(message)
        self.code = code
        self.kind = kind


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


def _certificate_problem(exc: BaseException) -> str:
    """
    The certificate's own complaint, if that is what went wrong.

    httpx wraps a TLS failure in a ConnectError, so the useful sentence is
    two or three ``__cause__`` links down. Walking for it is worth the code
    because of what happens otherwise: a certificate that does not verify
    is reported as "could not reach the runner", which is true and useless.
    It sends somebody to the firewall for a problem in a file on their own
    disk, and the switch they reach for next is the one that turns
    verification off.
    """
    import ssl

    seen = 0
    while exc is not None and seen < 6:
        if isinstance(exc, ssl.SSLError):
            return " ".join(str(a) for a in exc.args if isinstance(a, str)) or str(exc)
        text = str(exc)
        if "CERTIFICATE_VERIFY_FAILED" in text or "certificate verify failed" in text:
            return text
        exc = exc.__cause__ or exc.__context__
        seen += 1
    return ""


def _transport_error(exc: Exception, cfg: "RunnerConfig") -> "AnsibleError":
    """Why the call did not happen, said as the thing that needs fixing."""
    trouble = _certificate_problem(exc)
    if trouble:
        fix = ("Give ShellMate the runner's CA certificate under Settings → "
               "Ansible." if not cfg.ca_cert else
               f"The CA file given is {cfg.ca_cert}.")
        return AnsibleError(
            f"The runner's certificate was not accepted: {trouble}. {fix}",
            code=0, kind="certificate")
    return AnsibleError(
        f"Could not reach the runner at {cfg.url} "
        f"({exc.__class__.__name__}).", kind="unreachable")


def _call(method: str, path: str, *, json_body: Any = None,
          params: dict | None = None, text: bool = False,
          body: bytes | None = None) -> Any:
    cfg = config()
    if not cfg.ready:
        raise NotConfigured("No Ansible runner is set up yet. "
                            "Add one under Settings → Ansible.")
    import httpx

    try:
        with _client(cfg) as client:
            # A raw body for the playbook upload, which takes the YAML
            # itself rather than a field containing it.
            response = client.request(
                method, f"{cfg.url}{path}",
                content=body if body is not None else None,
                json=json_body if body is None else None,
                params=params or None,
                headers={"Content-Type": "text/plain; charset=utf-8"}
                if body is not None else None)
    except httpx.HTTPError as exc:
        raise _transport_error(exc, cfg) from exc
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
        return {"reachable": False, "configured": False, "url": cfg.url,
                "detail": ", ".join(cfg.missing())}
    gaps = cfg.missing()
    if gaps:
        return {"reachable": False, "configured": True, "url": cfg.url,
                "detail": ", ".join(gaps)}
    # /health needs no token by design, so it separates two failures that
    # look alike from the outside: a runner that cannot be reached, and one
    # that can be reached but will not talk to us.
    # Whether the connection is encrypted, and whether anything checked the
    # certificate, are facts about the transport — true or false regardless
    # of whether the token was then accepted. Computing them only on the
    # success path left the header unable to say "unverified" for a runner
    # that answered and refused us, which is a state somebody can easily be
    # in for an afternoon.
    transport = {
        "encrypted": cfg.url.startswith("https://"),
        "verified": bool(cfg.url.startswith("https://")
                         and (cfg.ca_cert or cfg.verify_tls)),
    }
    try:
        health = _call("GET", "/health") or {}
    except AnsibleError as exc:
        return {"reachable": False, "configured": True, "url": cfg.url,
                "kind": getattr(exc, "kind", ""), "detail": str(exc), **transport}
    try:
        books = list_playbooks()
    except AnsibleError as exc:
        if exc.code in (401, 403):
            return {"reachable": True, "configured": True, "authenticated": False,
                    "url": cfg.url, **transport,
                    "ansible_core": health.get("ansible_core", ""),
                    "detail": ("The runner is there but will not accept ShellMate: "
                               "check the token under Settings → Ansible.")}
        return {"reachable": False, "configured": True, "url": cfg.url,
                "kind": getattr(exc, "kind", ""), "detail": str(exc), **transport}
    core = health.get("ansible_core", "")
    return {
        "reachable": True, "configured": True, "url": cfg.url, **transport,
        "playbooks": len(books),
        "ansible_core": core,
        "ansible_runner": health.get("ansible_runner", ""),
        "authenticated": True,   # the listing came back, so we were let in
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
          verbosity: int = 0, forks: int | None = None,
          envvars: dict | None = None) -> dict:
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
    # Credentials a collection expects in the environment rather than as a
    # play variable (#586). Sent only when a run asked for them, so the
    # ordinary run carries no secret at all.
    if envvars:
        body["envvars"] = {str(k): str(v) for k, v in envvars.items()}
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


def jobs(limit: int = 100, offset: int = 0, pipeline: str = "",
         status_filter: str = "") -> dict:
    """
    A window onto the runs the runner knows of, newest first.

    **Not "every run".** The endpoint used to return all of them and now
    pages, defaulting to 100 and capping at 500 — 402 runs took it 8.5
    seconds and 88 KB, which is about seventeen days of an hourly pipeline.
    The old signature returned a bare list and its docstring claimed
    completeness, so a caller had no way to know it was seeing a window.

    Two bounds, not one, and they are different: ``total`` is how many the
    runner is holding *now*, and the runner prunes artifacts (500 by
    default, roughly three weeks of an hourly pipeline). So even
    ``total`` is not the history — it is what has not been pruned. Anything
    presenting this as a complete record is wrong twice over.

    Returns:
        ``{"jobs": [...], "total": n, "count": n, "offset": n, "limit": n}``.
        ``total`` is absent on a runner predating pagination, in which case
        the list itself is everything it had.
    """
    params = {"limit": max(1, min(int(limit), 500)), "offset": max(0, int(offset))}
    if pipeline:
        params["pipeline"] = pipeline
    if status_filter:
        params["status"] = status_filter
    data = _call("GET", "/api/v1/jobs", params=params) or {}
    rows = list(data.get("jobs") or [])
    return {
        "jobs": rows,
        # An older runner sends no total. Reporting len(rows) as the total
        # would be a claim we cannot support; None says "not stated", and
        # the interface can say so rather than inventing a number.
        "total": data.get("total"),
        "count": data.get("count", len(rows)),
        "offset": data.get("offset", params["offset"]),
        "limit": data.get("limit", params["limit"]),
    }


def status(job_id: str) -> dict:
    """What one run is doing now."""
    data = _call("GET", f"/api/v1/jobs/{job_id}") or {}
    state = str(data.get("status") or "").lower()
    return {**data, "id": data.get("id") or job_id, "status": state,
            "running": state in RUNNING_STATES,
            "finished": state in FINISHED_STATES}


def cancel(job_id: str) -> dict:
    """
    Ask the runner to stop a run, and say what actually happened.

    The state is read first, because the runner answers a cancel for an
    already-finished job with success. Passing that through made ShellMate
    report "Cancellation requested" for a run that had failed a minute
    earlier — claiming to have stopped something that was never running,
    which is the one thing Stop must not do. Found against the real
    container, not the mock.

    A 404 is the other shape of the same outcome: the service can only
    cancel a run its own process started, so a run from before a restart is
    gone rather than stoppable. Both are outcomes, not errors.
    """
    already = ""
    try:
        current = status(job_id)
        if current.get("status") in FINISHED_STATES:
            already = current.get("status", "")
    except AnsibleError:
        # If the state cannot be read, ask anyway and report what comes
        # back — refusing to try because the check failed would be worse.
        already = ""

    if already:
        return {"cancelled": False, "status": already,
                "detail": f"That run had already finished ({already}); "
                          "nothing was stopped."}

    try:
        data = _call("DELETE", f"/api/v1/jobs/{job_id}") or {}
    except AnsibleError as exc:
        if exc.code == 404:
            return {"cancelled": False,
                    "detail": "That run is no longer one the runner can stop."}
        raise

    # It can still have finished between the two calls. Cheap to re-read,
    # and the alternative is a message that is wrong exactly when a run
    # ends at the moment somebody reaches for Stop.
    landed = ""
    try:
        landed = status(job_id).get("status", "")
    except AnsibleError:
        landed = ""
    if landed in FINISHED_STATES and landed not in ("canceled", "cancelled"):
        return {"cancelled": False, "status": landed,
                "detail": f"The run finished ({landed}) before it could be "
                          "stopped."}
    return {"cancelled": bool(data.get("cancelled", True)),
            "status": landed, "detail": "Cancellation requested."}


def events(job_id: str, since: int = 0) -> dict:
    """
    A run's events, oldest first, and only those after ``since``.

    ansible-runner numbers its own events, so the counter orders them and
    makes polling cheap: the panel asks for what it has not seen rather
    than re-reading the whole play every second.

    ``since`` is sent to the runner *and* applied here. That is not
    belt-and-braces for its own sake — the runner only learned to honour
    the parameter in e35fe6b, and an older one ignores it and returns the
    whole run. Filtering here as well means one code path against both.

    The local sort is load-bearing for the same reason. That release also
    fixed the runner sorting events by filename, which for a run of more
    than nine events returned 1, 10, 11 ... 2, 20. Ordering by the counter
    ourselves is what meant that never reached anybody here.
    """
    data = _call("GET", f"/api/v1/jobs/{job_id}/events",
                 params={"since": int(since)} if since else None) or {}
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


def result(job_id: str) -> dict:
    """
    What a run published with ``set_stats``, or nothing (Deployments).

    ``{"has_result": bool, "result": dict | None}``. The runner sends
    ``has_result: false`` rather than an empty object when a playbook
    published nothing — ansible-runner writes ``{}`` in that case, and an
    empty object read as "the plan found nothing to do" is a different
    sentence from "the playbook did not say".
    """
    data = _call("GET", f"/api/v1/jobs/{job_id}/result") or {}
    return {"has_result": bool(data.get("has_result")),
            "result": data.get("result") if data.get("has_result") else None}


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


def _group_exists(key: str) -> bool:
    """
    Whether anything answers to this group name.

    A group is real if it was made — it is in the group store — or if any
    connection is tagged with it or with something beneath it. Both count,
    because the estate has always allowed a tag to be a group nobody
    formally created.
    """
    from backend import groups as groups_module
    from backend import profiles as profiles_module

    prefix = f"{key}/"
    try:
        for stored in groups_module.list_groups():
            name = (stored.get("key") or "").lower()
            if name == key or name.startswith(prefix):
                return True
    except Exception:                                     # pragma: no cover
        pass
    try:
        for profile in profiles_module._load():
            for tag in profiles_module.normalise_tags(profile.get("tags")):
                tag = tag.lower()
                if tag == key or tag.startswith(prefix):
                    return True
    except Exception:                                     # pragma: no cover
        pass
    return False


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

    # Whether that group exists at all, asked before anything is counted.
    #
    # Without this, a mistyped name returned a perfectly well-formed empty
    # inventory — 0 hosts, 0 groups, 0 skipped — which is byte for byte what
    # a real group full of serial consoles returns. The preview then said
    # "nothing would run" and the run dialog said "no connection in that
    # group has an address to reach", both sending somebody to look at
    # their devices when the answer was a typo. A verdict about a group is
    # not deliverable until the group is known to exist.
    known = True
    if wanted:
        known = _group_exists(wanted)

    groups: dict[str, list[str]] = {}
    original: dict[str, str] = {}
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
            # The connection this came from, so something dragged out of the
            # group tree — which carries profile ids, not addresses — can be
            # resolved to a host without a second round trip (#601).
            "shellmate_id": profile.get("id", ""),
            "ansible_host": address,
            "ansible_port": int(profile.get("port") or 22),
        })
        if profile.get("username"):
            entry["ansible_user"] = profile["username"]
        platform = (profile.get("platform") or profile.get("last_seen_platform") or "").lower()
        network_os = ANSIBLE_NETWORK_OS.get(platform)
        if platform:
            # ShellMate's own id, beside the Ansible name it maps to. A
            # curated list built from this table has to store what
            # ShellMate knew, not the mapping's output — reversing
            # `cisco.ios.ios` back to `ios` in the browser would be a
            # second copy of a map that already exists here (#608).
            entry["shellmate_platform"] = platform
        if network_os:
            entry["ansible_network_os"] = network_os
            entry["ansible_connection"] = "ansible.netcommon.network_cli"
        for tag in tags or ["ungrouped"]:
            key = ansible_group_name(tag)
            if key:
                groups.setdefault(key, [])
                # What ShellMate calls it, kept beside what Ansible will.
                # An interface that can only show `site_1_routers` is asking
                # somebody to recognise their own estate through a mangling
                # ShellMate performed (#601).
                original.setdefault(key, tag)
                if address not in groups[key]:
                    groups[key].append(address)

    # A site is a group too (#601). `site-1/routers` and `site-1/switches`
    # hold the hosts; nothing is tagged `site-1` itself, so without this the
    # site is not a group Ansible knows and cannot be targeted — which is
    # exactly what somebody dragging a whole site out of the tree means to
    # do. Ansible's own answer is a group of groups, so that is what this
    # emits rather than copying the hosts upward: copying would duplicate
    # every host under every ancestor and make the inventory lie about how
    # many there are.
    children: dict[str, list[str]] = {}
    for tag, key in sorted(original.items(), key=lambda pair: pair[1]):
        parts = key.split("/")
        for depth in range(1, len(parts)):
            parent = ansible_group_name("/".join(parts[:depth]))
            if not parent or parent == tag:
                continue
            children.setdefault(parent, [])
            if tag not in children[parent]:
                children[parent].append(tag)
            original.setdefault(parent, "/".join(parts[:depth]))

    return {
        "groups": {k: sorted(v) for k, v in sorted(groups.items())},
        # Parent -> the groups beneath it, as Ansible's [parent:children].
        "children": {k: sorted(v) for k, v in sorted(children.items())},
        # Ansible's name to ShellMate's. Show the second, send the first.
        "group_names": {k: original.get(k, k)
                        for k in sorted(set(groups) | set(children))},
        "hostvars": hostvars,
        "hosts": sorted(hostvars),
        "skipped": skipped,
        # False only when a group was named and nothing answers to it. The
        # difference between "this group is empty" and "there is no such
        # group" is the difference between checking your devices and
        # checking your spelling.
        "group_known": known,
        "group": group or "",
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
    # Groups of groups, so a whole site is targetable without every
    # host being listed twice.
    for parent, kids in (inventory.get("children") or {}).items():
        lines.append(f"[{parent}:children]")
        lines.extend(kids)
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


def upload_playbook(name: str, text: str, overwrite: bool = False) -> dict:
    """
    Put a playbook on the runner over its own API (#605).

    The runner grew this endpoint after the integration was built around
    its absence, so the SSH copy remains as a fallback for a container that
    predates it — but this is the route now. It needs no shell access to
    the container's host and no knowledge of the host path, which is what
    made the old one fail in a way that was about SSH rather than Ansible.

    Returns the runner's own answer, which says how many plays it parsed.
    That is a cheap sanity check worth surfacing: "2 plays" means the file
    arrived as a playbook rather than as text that happens to be there.
    """
    if not name or name.startswith("/") or ".." in name:
        raise AnsibleError("That is not a playbook name.")
    data = _call("PUT", f"/api/v1/playbooks/{name}",
                 params={"overwrite": "true" if overwrite else "false"},
                 body=text.encode("utf-8"))
    logger.info("Uploaded %s to the runner (%s)", name,
                (data or {}).get("path", "?"))
    return data or {}


def upload_file(path: str, text: str, overwrite: bool = True) -> dict:
    """
    Put a data file on the runner — a site list, a scheme (Deployments).

    Its own route, `/api/v1/files/{path}`, because the playbook route
    validates that the body is a list of plays and refuses a dict with a
    422. The runner parses the file on upload, so a broken `sites.yml`
    fails here, in front of the person who uploaded it, rather than when
    a plan runs. Never executed by the runner.

    `overwrite` defaults on, unlike a playbook: ShellMate is the writer of
    record for these files — they are rendered from the deployment and
    committed to git first — so a stale copy on the runner is the thing
    being corrected, not a thing to protect.
    """
    clean = str(path or "").strip().lstrip("/")
    if not clean or ".." in clean.split("/"):
        raise AnsibleError("That is not a file path on the runner.")
    data = _call("PUT", f"/api/v1/files/{clean}",
                 params={"overwrite": "true" if overwrite else "false"},
                 body=text.encode("utf-8"))
    logger.info("Uploaded %s to the runner", clean)
    return data or {}


def read_file(path: str) -> str:
    """A data file's text from the runner — a kit's scheme, a deployment's sites."""
    clean = str(path or "").strip().lstrip("/")
    if not clean or ".." in clean.split("/"):
        raise AnsibleError("That is not a file path on the runner.")
    return _call("GET", f"/api/v1/files/{clean}", text=True) or ""


def supports_files() -> bool:
    """Whether this runner has the data-file route. Probed, like upload."""
    try:
        spec = _call("GET", "/openapi.json") or {}
        return "/api/v1/files/{path}" in (spec.get("paths") or {})
    except AnsibleError:
        return False


def supports_upload() -> bool:
    """
    Whether this runner accepts a playbook over its API.

    Asked rather than assumed: a container built before the endpoint
    existed answers 404 or 405, and offering an upload that cannot work is
    worse than offering the copy that can.
    """
    try:
        spec = _call("GET", "/openapi.json") or {}
        node = (spec.get("paths") or {}).get("/api/v1/playbooks/{name}") or {}
        return "put" in node
    except AnsibleError:
        return False


def playbook_transfer_plan(name: str) -> dict:
    """
    How a playbook written here reaches the runner.

    The fallback route, for a runner that predates the upload endpoint
    (#605). Its project directory is a bind mount from the container's
    host, so the file goes to a path on *that host* over an SSH session
    ShellMate already has — which is why this names a host path rather
    than a path inside the container.
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
        "why": ("This runner does not accept playbooks over its API, so "
                "ShellMate copies the file over an SSH session to the machine "
                "hosting the container. The path is the one on that host, "
                "which the container has mounted as its project directory."),
    }
