"""
test_ansible.py — Driving the Ansible runner (#585).

The container is not here, so it is played by a mock transport answering
in the shapes its own source produces: FastAPI's `{"detail": ...}` for a
refusal, `{id, playbook, status, inventory}` on a launch, and
ansible-runner's own numbered events. What is tested is that ShellMate
reads those correctly and says something useful when the answer is no.

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
EVENTS = [
    {"counter": 1, "uuid": "a", "event": "playbook_on_start", "event_data": {}},
    {"counter": 2, "uuid": "b", "event": "playbook_on_task_start",
     "event_data": {"task": "Gather facts", "play": "all"}},
    {"counter": 3, "uuid": "c", "event": "runner_on_ok",
     "event_data": {"task": "Gather facts", "host": "10.1.0.1", "res": {"changed": False}}},
    {"counter": 4, "uuid": "d", "event": "playbook_on_task_start",
     "event_data": {"task": "Push config", "play": "all"}},
    {"counter": 5, "uuid": "e", "event": "runner_on_ok",
     "event_data": {"task": "Push config", "host": "10.1.0.1", "res": {"changed": True}}},
    {"counter": 6, "uuid": "f", "event": "runner_on_failed",
     "event_data": {"task": "Push config", "host": "10.1.0.2"}, "stdout": "auth failed"},
]

#: What the launch body carried, so a test can assert on what was sent.
SENT: dict = {}


def service(handler=None):
    """A transport playing the runner. `handler` overrides one route."""
    def route(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if handler is not None:
            answer = handler(request)
            if answer is not None:
                return answer
        if path == "/health":
            return httpx.Response(200, json={"status": "ok", "ansible_core": "2.21.3",
                                             "ansible_runner": "2.4.3"})
        if path == "/api/v1/playbooks" and request.method == "GET":
            return httpx.Response(200, json={"playbooks": [
                {"name": "site.yml", "size": 120, "modified": "2026-09-04T10:00:00+00:00"},
                {"name": "net/backup.yml", "size": 80, "modified": "2026-09-03T10:00:00+00:00"}]})
        if path.startswith("/api/v1/playbooks/") and request.method == "GET":
            return httpx.Response(200, text="- hosts: all\n  tasks: []\n")
        if path.startswith("/api/v1/playbooks/") and request.method == "POST":
            import json as _json
            SENT.clear()
            SENT.update(_json.loads(request.content or b"{}"))
            return httpx.Response(202, json={
                "id": "abc123", "playbook": path.split("/playbooks/", 1)[1],
                "status": "starting", "inventory": "/runner/artifacts/abc123/inventory",
                "stdout_url": "/api/v1/jobs/abc123/stdout",
                "job_url": "/api/v1/jobs/abc123"})
        if path == "/api/v1/jobs" and request.method == "GET":
            return httpx.Response(200, json={"jobs": [
                {"id": "abc123", "status": "running", "rc": None, "playbook": "site.yml",
                 "started": "2026-09-04T10:00:00+00:00", "source": "memory"},
                {"id": "old999", "status": "successful", "rc": 0, "playbook": "ping.yml",
                 "started": "2026-09-03T09:00:00+00:00", "source": "artifacts"}]})
        if path.endswith("/events"):
            return httpx.Response(200, json={"events": EVENTS})
        if path.endswith("/stdout"):
            return httpx.Response(200, text="PLAY [all] ***\nok: [10.1.0.1]\n")
        if path.startswith("/api/v1/jobs/") and request.method == "GET":
            return httpx.Response(200, json={"id": "abc123", "status": "running",
                                             "rc": None, "playbook": "site.yml",
                                             "started": "2026-09-04T10:00:00+00:00",
                                             "source": "memory", "cancelled": False})
        if path.startswith("/api/v1/jobs/") and request.method == "DELETE":
            return httpx.Response(200, json={"id": "abc123", "cancelled": True})
        if path == "/api/v1/galaxy/install":
            # The name travels as a query parameter, deliberately. Sent as a
            # JSON body it was accepted and ignored, and the runner
            # installed its default file while answering 200 — the same
            # silent drop that hid envvars. "Module not found three tasks
            # in" is what the install exists to prevent, so a request for
            # the wrong file must not look like success.
            asked = request.url.params.get("requirements", "")
            if asked and asked != "requirements.yml":
                if asked != "site-requirements.yml":
                    return httpx.Response(404, json={
                        "detail": f"{asked} not found in the project directory."})
            return httpx.Response(200, json={"rc": 0, "stdout": f"installed {asked}"})
        return httpx.Response(404, json={"detail": "not found"})
    return httpx.MockTransport(route)


class Patched(httpx.Client):
    transport = None

    def __init__(self, *a, **kw):
        kw.pop("cert", None)
        kw.pop("verify", None)
        kw["transport"] = Patched.transport
        super().__init__(*a, **kw)


def with_runner(fn, handler=None, token: str = ""):
    """Run `fn` with a configured runner and the service standing in."""
    from backend.settings_store import update_settings
    update_settings({"ansible": {"runner_url": "http://runner.test:8081",
                                 "token": token, "client_cert": "", "client_key": ""}})
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
    us({"ansible": {"client_cert": "/nope/a.crt", "client_key": ""}})
    check("half a certificate pair is refused",
          any("both halves" in m for m in ansible.config().missing()),
          str(ansible.config().missing()))


def test_playbooks_and_runs() -> None:
    print("\n-- Playbooks and runs --")
    books = with_runner(ansible.list_playbooks)
    check("the runner's playbooks are listed with their size",
          [b["name"] for b in books] == ["net/backup.yml", "site.yml"], str(books))
    check("  a playbook in a subdirectory keeps its path",
          books[0]["name"] == "net/backup.yml" and books[0]["bytes"] == 80, str(books[0]))
    check("one can be read, read-only",
          "hosts: all" in with_runner(lambda: ansible.read_remote_playbook("site.yml")))

    started = with_runner(lambda: ansible.start(
        "site.yml", limit=["10.1.0.1"], check=True, tags="config",
        inventory_content="[all]\n10.1.0.1\n"))
    check("a run starts and is named", started["id"] == "abc123", str(started))
    check("  and says which inventory it used",
          "abc123/inventory" in started["inventory"], str(started))
    check("check mode is sent as the body field the service takes",
          SENT.get("check") is True and "limit" in SENT and SENT["tags"] == "config", str(SENT))
    check("an inventory generated here is sent as content, never as a name",
          SENT.get("inventory_content", "").startswith("[all]") and "inventory" not in SENT,
          str(sorted(SENT)))

    check("a path in a playbook name is refused",
          _raises(lambda: ansible.start("../etc/passwd"), ansible.AnsibleError))
    check("an absolute playbook name is refused",
          _raises(lambda: ansible.start("/etc/passwd"), ansible.AnsibleError))
    check("both kinds of inventory at once is refused here, not at the runner",
          _raises(lambda: ansible.start("site.yml", inventory="a", inventory_content="b"),
                  ansible.AnsibleError))

    state = with_runner(lambda: ansible.status("abc123"))
    check("a running job says it is running",
          state["running"] and not state["finished"], str(state))

    history = with_runner(ansible.jobs)
    check("past runs are listed too, not only live ones",
          len(history) == 2 and any(j["source"] == "artifacts" for j in history), str(history))

    got = with_runner(lambda: ansible.events("abc123"))
    check("events come back in counter order",
          [e["counter"] for e in got["events"]] == [1, 2, 3, 4, 5, 6],
          str([e["counter"] for e in got["events"]]))
    check("  and the last counter comes back for the next poll",
          got["last"] == 6, str(got["last"]))
    later = with_runner(lambda: ansible.events("abc123", since=3))
    check("`since` returns only what came after",
          [e["counter"] for e in later["events"]] == [4, 5, 6],
          str([e["counter"] for e in later["events"]]))
    check("a task's host and name are lifted out of event_data",
          later["events"][1]["host"] == "10.1.0.1"
          and later["events"][1]["task"] == "Push config", str(later["events"][1]))

    counts = ansible.summarise(got["events"])
    check("the summary counts what the play did",
          counts["tasks"] == 2 and counts["ok"] == 1
          and counts["changed"] == 1 and counts["failed"] == 1, str(counts))
    check("  a changed result is not counted as ok as well",
          counts["ok"] + counts["changed"] == 2, str(counts))

    check("the whole run can be read as text",
          "PLAY [all]" in with_runner(lambda: ansible.stdout("abc123")))

    stopped = with_runner(lambda: ansible.cancel("abc123"))
    check("a run can be stopped", stopped["cancelled"], str(stopped))

    def gone(request):
        if request.method == "DELETE":
            return httpx.Response(404, json={
                "detail": "job 'abc123' is not running in this process"})
        return None
    late = with_runner(lambda: ansible.cancel("abc123"), handler=gone)
    check("stopping a run it can no longer stop is not an error",
          late["cancelled"] is False and "no longer" in late["detail"], str(late))

    # Found against the real container: the runner answers a cancel for an
    # already-finished job with success, so passing that through claimed to
    # have stopped a run that failed a minute earlier. Stop must never say
    # it stopped something that was not running.
    def finished(request):
        if request.url.path == "/api/v1/jobs/abc123":
            return httpx.Response(200, json={
                "id": "abc123", "status": "failed", "rc": 1,
                "playbook": "p.yml", "started": "2026-09-04T10:00:00+00:00"})
        if request.method == "DELETE":
            return httpx.Response(200, json={"id": "abc123", "cancelled": True})
        return None

    done = with_runner(lambda: ansible.cancel("abc123"), handler=finished)
    check("stopping a run that already finished says so",
          done["cancelled"] is False and "already finished" in done["detail"],
          str(done))
    check("and names the state it finished in",
          "failed" in done["detail"], str(done))

    check("galaxy requirements can be installed",
          with_runner(ansible.install_requirements).get("rc") == 0)
    check("the file asked for is the file installed",
          "site-requirements.yml" in with_runner(
              lambda: ansible.install_requirements("site-requirements.yml")
          ).get("stdout", ""),
          "the name has to travel as a query parameter; as a body it is dropped")

    try:
        with_runner(lambda: ansible.install_requirements("nope.yml"))
        missing = "it returned instead of raising"
    except ansible.AnsibleError as exc:
        missing = str(exc)
    check("a requirements file that is not there fails rather than "
          "quietly installing the default",
          "nope.yml" in missing, missing)


def test_the_transport_is_reported_even_when_refused() -> None:
    """
    Whether the connection is encrypted and checked is not an auth question.

    A runner that answers and then refuses the token is a state somebody can
    sit in for an afternoon while they hunt for the value. Reporting the TLS
    facts only on the success path left the header unable to say "connected
    but unverified" in exactly that window — which is when it is most worth
    knowing, because turning verification off is the wrong thing to try next
    and it is the thing people try.
    """
    print("\n-- The transport, reported whether or not we got in --")

    def refuses(request):
        if request.url.path == "/health":
            return httpx.Response(200, json={"ansible_core": "2.21.3"})
        return httpx.Response(401, json={"detail": "no"})

    from backend.settings_store import update_settings
    update_settings({"ansible": {"runner_url": "https://runner.test:8081",
                                 "token": "", "ca_cert": "", "client_cert": "",
                                 "client_key": "", "verify_tls": True}})
    real, Patched.transport = httpx.Client, service(refuses)
    httpx.Client = Patched
    try:
        state = ansible.ping()
    finally:
        httpx.Client = real

    check("a refused runner still reports it was reached",
          state["reachable"] is True and state["authenticated"] is False,
          str(state))
    check("and that the connection was encrypted",
          state.get("encrypted") is True, str(state))
    check("and that the certificate was checked",
          state.get("verified") is True, str(state))
    check("and points at the token rather than the network",
          "token" in state.get("detail", "").lower(), str(state))


def test_the_token_travels_when_there_is_one() -> None:
    print("\n-- Auth, when the deployment has any --")
    seen = {}

    def note(request):
        seen["auth"] = request.headers.get("authorization", "")
        return None
    with_runner(ansible.list_playbooks, handler=note)
    check("no token, no header", not seen.get("auth"), str(seen))
    with_runner(ansible.list_playbooks, handler=note, token="s3cret")
    check("a configured token is sent as a bearer",
          seen.get("auth") == "Bearer s3cret", str(seen))

    def refused(request):
        return httpx.Response(401, json={"detail": "Not authenticated"})
    try:
        with_runner(ansible.list_playbooks, handler=refused, token="wrong")
        check("a refusal points at the setting", False, "no exception")
    except ansible.AnsibleError as exc:
        check("a refusal points at the setting",
              "token" in str(exc) and "Settings" in str(exc), str(exc))


def test_the_runners_words_reach_the_user() -> None:
    print("\n-- When the runner refuses --")

    def missing(request):
        if request.method == "POST":
            return httpx.Response(404, json={
                "detail": "playbook 'nope.yml' not found under /runner/project"})
        return None
    try:
        with_runner(lambda: ansible.start("nope.yml"), handler=missing)
        check("its own message is what is raised", False, "no exception")
    except ansible.AnsibleError as exc:
        check("its own message is what is raised",
              "not found under /runner/project" in str(exc), str(exc))
        check("  with the code it gave", exc.code == 404, str(exc.code))

    def invalid(request):
        return httpx.Response(422, json={"detail": [
            {"loc": ["body", "forks"], "msg": "Input should be a valid integer"}]})
    try:
        with_runner(lambda: ansible.start("site.yml"), handler=invalid)
        check("a validation refusal names the field", False, "no exception")
    except ansible.AnsibleError as exc:
        check("a validation refusal names the field",
              "forks" in str(exc) and "integer" in str(exc), str(exc))

    def refuse(request):
        raise httpx.ConnectError("no route to host")
    try:
        with_runner(ansible.list_playbooks, handler=refuse)
        check("an unreachable runner says where it looked", False, "no exception")
    except ansible.AnsibleError as exc:
        check("an unreachable runner says where it looked",
              "runner.test:8081" in str(exc), str(exc))


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

    text = ansible.inventory_as_ini(inv)
    check("it renders as INI, with the group as a section",
          "[site_004_core_switches]" in text, text[:200])
    check("  the host line carries its variables",
          "ansible_network_os=cisco.ios.ios" in text, text[:300])
    check("  and says it was sent with this run alone",
          "Generated by ShellMate" in text and "untouched" in text, text[:120])


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
                 test_the_token_travels_when_there_is_one,
        test_the_transport_is_reported_even_when_refused,
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
