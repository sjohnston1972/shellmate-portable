"""
test_ansible.py — Driving ansible-runner-service (#585).

The service is not here, so it is played by a mock transport that answers
in the shapes its own source produces: the `{"status", "msg", "data"}`
envelope, `play_uuid` on a start, events keyed by a counter-prefixed id.
What is tested is that ShellMate reads those shapes correctly and says
something useful when the answer is a refusal.

Run: python test_ansible.py
"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import httpx  # noqa: E402

from backend import paths  # noqa: E402

_TEMP = Path(tempfile.mkdtemp(prefix="shellmate-ansible-"))
paths._data_dir_cache = _TEMP
assert paths.data_dir() == _TEMP, "test isolation failed — refusing to run"

from backend import ansible  # noqa: E402
from backend import profiles as profiles_module  # noqa: E402

passed = 0
failed: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    global passed
    if condition:
        passed += 1
        print(f"  OK  {name}")
    else:
        failed.append(f"{name}: {detail}")
        print(f"  FAIL {name}\n       {detail}")


# ---------------------------------------------------------------------------
# A stand-in for the service, answering the way its source does
# ---------------------------------------------------------------------------
EVENTS = {
    "1-abc": {"event": "playbook_on_start", "task": ""},
    "2-def": {"event": "playbook_on_task_start", "task": "Gather facts"},
    "3-ghi": {"event": "runner_on_ok", "task": "Gather facts"},
    "4-jkl": {"event": "playbook_on_task_start", "task": "Push config"},
    "5-mno": {"event": "runner_on_changed", "task": "Push config"},
    "6-pqr": {"event": "runner_on_failed", "task": "Push config"},
}


def service(handler=None):
    """A transport playing the runner. `handler` overrides one route."""
    def route(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if handler is not None:
            answer = handler(request)
            if answer is not None:
                return answer
        if path == "/api/v1/playbooks" and request.method == "GET":
            return httpx.Response(200, json={"status": "OK", "msg": "", "data": {
                "playbooks": ["site.yml", "backup.yml"]}})
        if path.startswith("/api/v1/playbooks/") and request.method == "POST":
            return httpx.Response(202, json={"status": "STARTED", "msg": "starting",
                                             "data": {"play_uuid": "run-1"}})
        if path.startswith("/api/v1/playbooks/") and request.method == "GET":
            return httpx.Response(200, json={"status": "OK", "msg": "",
                                             "data": {"status": "running"}})
        if path.startswith("/api/v1/playbooks/") and request.method == "DELETE":
            return httpx.Response(200, json={"status": "OK", "msg": "Cancel request issued",
                                             "data": {}})
        if path.endswith("/events"):
            return httpx.Response(200, json={"status": "OK", "msg": "", "data": {
                "events": EVENTS, "total_events": len(EVENTS)}})
        if "/events/" in path:
            return httpx.Response(200, json={"status": "OK", "msg": "", "data": {
                "task": "Push config", "stdout": "config applied"}})
        if path == "/api/v1/groups":
            return httpx.Response(200, json={"status": "OK", "msg": "",
                                             "data": {"groups": ["core", "edge"]}})
        if path == "/api/v1/hosts":
            return httpx.Response(200, json={"status": "OK", "msg": "",
                                             "data": {"hosts": ["10.0.0.1"]}})
        if path.startswith("/api/v1/hosts/") or path.startswith("/api/v1/groups/"):
            return httpx.Response(200, json={"status": "OK", "msg": "", "data": {}})
        return httpx.Response(404, json={"status": "NOTFOUND", "msg": "no such thing",
                                         "data": {}})
    return httpx.MockTransport(route)


class Patched(httpx.Client):
    transport = None

    def __init__(self, *a, **kw):
        kw.pop("cert", None)                 # no certificate to present here
        kw["transport"] = Patched.transport
        kw.pop("verify", None)
        super().__init__(*a, **kw)


def with_runner(fn, handler=None):
    """Run `fn` with a configured runner and the service standing in."""
    from backend.settings_store import update_settings
    cert = _TEMP / "client.crt"
    key = _TEMP / "client.key"
    cert.write_text("x", encoding="utf-8")
    key.write_text("x", encoding="utf-8")
    update_settings({"ansible": {"runner_url": "https://runner.test:5001",
                                 "client_cert": str(cert), "client_key": str(key),
                                 "verify_tls": False}})
    real, Patched.transport = httpx.Client, service(handler)
    httpx.Client = Patched
    try:
        return fn()
    finally:
        httpx.Client = real


# ---------------------------------------------------------------------------
def test_not_configured() -> None:
    print("\n-- Before a runner is set up --")
    from backend.settings_store import update_settings
    update_settings({"ansible": {"runner_url": "", "client_cert": "", "client_key": ""}})
    check("it is not configured", not ansible.configured())
    state = ansible.ping()
    check("ping says so rather than failing",
          state["configured"] is False and state["reachable"] is False, str(state))
    check("  and names what is missing", "address" in state["detail"], state["detail"])
    try:
        ansible.list_playbooks()
        check("asking anyway raises NotConfigured", False, "no exception")
    except ansible.NotConfigured as exc:
        check("asking anyway raises NotConfigured", "Settings" in str(exc), str(exc))

    from backend.settings_store import update_settings as us
    us({"ansible": {"runner_url": "https://x:5001", "client_cert": "/nope/a.crt",
                    "client_key": "/nope/a.key"}})
    check("a certificate that is not there is named",
          any("no such file" in m for m in ansible.config().missing()),
          str(ansible.config().missing()))


def test_playbooks_and_runs() -> None:
    print("\n-- Playbooks and runs --")
    names = with_runner(ansible.list_playbooks)
    check("the runner's playbooks are listed", names == ["backup.yml", "site.yml"], str(names))

    started = with_runner(lambda: ansible.start("site.yml", limit=["10.0.0.1"], check=True))
    check("a run starts and is named", started["play_uuid"] == "run-1", str(started))

    check("a path in a playbook name is refused",
          _raises(lambda: ansible.start("../etc/passwd"), ansible.AnsibleError))

    state = with_runner(lambda: ansible.status("run-1"))
    check("a running job says it is running",
          state["running"] and not state["finished"], str(state))

    got = with_runner(lambda: ansible.events("run-1"))
    check("events come back in counter order",
          [e["counter"] for e in got["events"]] == [1, 2, 3, 4, 5, 6],
          str([e["counter"] for e in got["events"]]))
    later = with_runner(lambda: ansible.events("run-1", since="3-ghi"))
    check("  and `since` returns only what came after",
          [e["counter"] for e in later["events"]] == [4, 5, 6],
          str([e["counter"] for e in later["events"]]))

    counts = ansible.summarise(got["events"])
    check("the summary counts what the play did",
          counts["tasks"] == 2 and counts["ok"] == 1
          and counts["changed"] == 1 and counts["failed"] == 1, str(counts))

    one = with_runner(lambda: ansible.event("run-1", "6-pqr"))
    check("one task's own output can be read", one.get("stdout") == "config applied", str(one))

    stopped = with_runner(lambda: ansible.cancel("run-1"))
    check("a run can be stopped", stopped["cancelled"], str(stopped))

    def gone(request):
        if request.method == "DELETE":
            return httpx.Response(404, json={"status": "NOT ACTIVE",
                                             "msg": "playbook with uuid run-1 is not active",
                                             "data": {}})
        return None
    late = with_runner(lambda: ansible.cancel("run-1"), handler=gone)
    check("stopping a finished run is not an error",
          late["cancelled"] is False and "already finished" in late["detail"], str(late))


def test_the_runners_words_reach_the_user() -> None:
    print("\n-- When the runner refuses --")

    def missing(request):
        if request.method == "POST":
            return httpx.Response(404, json={"status": "NOTFOUND",
                                             "msg": "playbook file not found", "data": {}})
        return None
    try:
        with_runner(lambda: ansible.start("nope.yml"), handler=missing)
        check("its own message is what is raised", False, "no exception")
    except ansible.AnsibleError as exc:
        check("its own message is what is raised", "playbook file not found" in str(exc), str(exc))
        check("  with the status it gave", exc.status == "NOTFOUND" and exc.code == 404,
              f"{exc.status} {exc.code}")

    def refuse(request):
        raise httpx.ConnectError("no route to host")
    try:
        with_runner(ansible.list_playbooks, handler=refuse)
        check("an unreachable runner says where it looked", False, "no exception")
    except ansible.AnsibleError as exc:
        check("an unreachable runner says where it looked",
              "runner.test:5001" in str(exc), str(exc))


def test_inventory_from_the_estate() -> None:
    print("\n-- The estate as an inventory --")
    profiles_module.save_profile({"name": "core-1", "hostname": "10.1.0.1", "port": 22,
                                  "username": "eng", "connection_type": "ssh",
                                  "platform": "ios", "tags": ["site-004/core switches"]})
    profiles_module.save_profile({"name": "edge-1", "hostname": "10.1.0.2", "port": 2222,
                                  "username": "eng", "connection_type": "ssh",
                                  "tags": ["site-004/edge"]})
    profiles_module.save_profile({"name": "console-1", "serial_port": "COM3",
                                  "connection_type": "serial", "tags": ["site-004/edge"]})

    inv = ansible.inventory_from_estate()
    check("a group name is made safe for Ansible",
          "site_004_core_switches" in inv["groups"], str(list(inv["groups"])))
    check("hosts are addresses, which is what Ansible dials",
          inv["groups"]["site_004_core_switches"] == ["10.1.0.1"], str(inv["groups"]))
    check("the connection's name travels as a variable",
          inv["hostvars"]["10.1.0.1"]["shellmate_name"] == "core-1",
          str(inv["hostvars"]["10.1.0.1"]))
    check("a non-default port travels too",
          inv["hostvars"]["10.1.0.2"]["ansible_port"] == 2222, str(inv["hostvars"]["10.1.0.2"]))
    check("an identified platform picks the network connection",
          inv["hostvars"]["10.1.0.1"]["ansible_network_os"] == "cisco.ios.ios"
          and "network_cli" in inv["hostvars"]["10.1.0.1"]["ansible_connection"],
          str(inv["hostvars"]["10.1.0.1"]))
    check("  and an unidentified one is left alone",
          "ansible_network_os" not in inv["hostvars"]["10.1.0.2"],
          str(inv["hostvars"]["10.1.0.2"]))
    check("a serial connection is left out, with its reason",
          any(s["name"] == "console-1" and "address" in s["why"] for s in inv["skipped"]),
          str(inv["skipped"]))

    one = ansible.inventory_from_estate("site-004/edge")
    check("one group can be asked for on its own",
          one["hosts"] == ["10.1.0.2"], str(one["hosts"]))

    pushed = with_runner(lambda: ansible.push_inventory(inv))
    check("pushing reports what went", pushed["added"] >= 2 and not pushed["failed"], str(pushed))


def test_the_library() -> None:
    print("\n-- ShellMate's own playbook library --")
    saved = ansible.save_playbook("upgrade", "- hosts: all\n  tasks: []\n")
    check("a name gains its extension", saved["name"] == "upgrade.yml", str(saved))
    check("it is listed", any(p["name"] == "upgrade.yml" for p in ansible.library()))
    check("it reads back", "hosts: all" in ansible.read_playbook("upgrade.yml"))

    check("YAML that does not parse is refused",
          _raises(lambda: ansible.save_playbook("bad", "- hosts: [oops"), ansible.AnsibleError))
    check("something that is not a list of plays is refused",
          _raises(lambda: ansible.save_playbook("bad", "hosts: all"), ansible.AnsibleError))
    check("a name that climbs out of the folder is refused",
          _raises(lambda: ansible.save_playbook("../evil", "- hosts: all"), ansible.AnsibleError))

    plan = ansible.playbook_transfer_plan("upgrade.yml")
    check("the transfer says where it must land",
          plan["target"].endswith("/project/upgrade.yml"), str(plan))
    check("  and why it is a copy rather than an upload",
          "no API for uploading" in plan["why"], plan["why"])
    check("it can be deleted", ansible.delete_playbook("upgrade.yml")
          and not ansible.library())


def _raises(fn, kind) -> bool:
    try:
        fn()
    except kind:
        return True
    except Exception:
        return False
    return False


def main() -> int:
    print("=" * 52)
    print("  Ansible")
    print("=" * 52)
    for test in (test_not_configured, test_playbooks_and_runs,
                 test_the_runners_words_reach_the_user,
                 test_inventory_from_the_estate, test_the_library):
        try:
            test()
        except Exception as exc:
            failed.append(f"{test.__name__}: {exc!r}")
            print(f"  FAIL {test.__name__}: {exc!r}")
    print("\n" + "=" * 52)
    print(f"  {passed} passed  |  {len(failed)} failed")
    print("=" * 52)
    if failed:
        print("FAILURES:")
        for f in failed:
            print(" -", f)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
