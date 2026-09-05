"""
test_deployments_api.py — Plan, then a gated apply, over the API.

The whole flow through the routes, against a fake runner and a fake
GitHub: save a deployment, upload sites with the columns named, publish,
try to apply (refused), plan, fetch the plan's result, apply, fetch the
apply's result and see the ids land on the record.

The result payloads are the runner's real ones, captured against the org
(`test_fixtures/deploy_results.json`), not the contract text.

    python test_deployments_api.py
"""

import json
import shutil
import sys
import tempfile
from pathlib import Path

from backend import paths

_TEMP = Path(tempfile.mkdtemp(prefix="shellmate-deploy-api-"))
paths._data_dir_cache = _TEMP

from fastapi.testclient import TestClient                       # noqa: E402

from backend import ansible as runner                           # noqa: E402
from backend import ansible_git as git                          # noqa: E402
from backend import ansible_keys as key_store                   # noqa: E402
from backend import app as app_module                           # noqa: E402

client = TestClient(app_module.app, base_url="http://127.0.0.1")
FIX = json.loads((Path(__file__).parent / "test_fixtures" / "deploy_results.json")
                 .read_text(encoding="utf-8"))

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


# --- the fakes ---------------------------------------------------------------

class Runner:
    """What the runner would do, recorded."""

    def __init__(self):
        self.started: list[dict] = []
        self.uploaded: list[tuple[str, str]] = []
        self.finished: dict[str, bool] = {}
        self.results: dict[str, dict] = {}
        self.counter = 0

    def start(self, playbook, **kwargs):
        self.counter += 1
        job = f"job-{self.counter}"
        self.started.append({"playbook": playbook, "job": job, **kwargs})
        self.finished[job] = False
        return {"id": job, "playbook": playbook, "status": "pending", "inventory": ""}

    def status(self, job):
        done = self.finished.get(job, False)
        return {"id": job, "status": "successful" if done else "running",
                "running": not done, "finished": done}

    def result(self, job):
        payload = self.results.get(job)
        return {"has_result": payload is not None, "result": payload}

    def upload_playbook(self, name, text, overwrite=False):
        self.uploaded.append(("playbook", name)); return {"plays": 1}

    def read_playbook(self, path):
        return "- hosts: localhost\n  tasks: []\n" if "_kit/meraki/" in path else ""

    def upload_file(self, path, text, overwrite=True):
        self.uploaded.append(("file", path)); return {"parsed": True}


fake = Runner()
runner.start, runner.status, runner.result = fake.start, fake.status, fake.result
runner.upload_playbook, runner.upload_file = fake.upload_playbook, fake.upload_file
runner.read_playbook = fake.read_playbook
git.config = lambda: {"has_token": True, "owner": "o", "repo": "r",
                      "enabled": True, "visibility": "private"}
git.commit_tree = lambda tree, message: {"sha": "c0ffee1", "url": "", "files": sorted(tree),
                                          "branch": "main"}
key_store.resolve = lambda names: (
    {"MERAKI_DASHBOARD_API_KEY": "sekrit-value-9"} if "meraki_key" in names else {}, {},
    [n for n in names if n != "meraki_key"])

CSV = "Network Name,Tags\ndeploy-test-001,retail\ndeploy-test-002,retail\nAPI Navigator DEV,\n"
PLAN = "- hosts: localhost\n  vars_files: [sites.yml, scheme.yml]\n  tasks: []\n"


# ---------------------------------------------------------------------------

def test_the_flow() -> None:
    print("\n-- Save, upload, publish --")
    res = client.post("/api/deployments", json={
        "name": "Deploy test", "provider": "meraki",
        "scheme": {"product_types": ["appliance"], "vlans": [{"id": 10, "name": "data"}]},
        "scope": {"meraki_org_id": "923103"}, "keys": ["meraki_key"],
        "plan_text": PLAN, "apply_text": PLAN})
    check("saved", res.status_code == 200, res.text[:200])
    dep = res.json()

    res = client.post(f"/api/deployments/{dep['id']}/sites",
                      json={"text": CSV, "preview": True})
    check("a preview returns headers and asks nothing",
          res.status_code == 200 and res.json()["headers"] == ["Network Name", "Tags"],
          res.text[:200])

    res = client.post(f"/api/deployments/{dep['id']}/sites",
                      json={"text": CSV, "mapping": {"name": "Network Name", "tags": "Tags"}})
    check("sites stored with the mapping named",
          res.status_code == 200 and res.json()["sites"] == 3, res.text[:200])

    res = client.post(f"/api/deployments/{dep['id']}/apply")
    check("apply before publish is refused, and says publish",
          res.status_code == 409 and "Publish" in res.json()["detail"], res.text[:200])

    res = client.post(f"/api/deployments/{dep['id']}/publish", json={})
    check("publish commits then sends", res.status_code == 200
          and res.json()["commit"]["sha"] == "c0ffee1" and len(fake.uploaded) == 4,
          res.text[:300])
    check("two playbooks, two files", sorted(r for r, _ in fake.uploaded) ==
          ["file", "file", "playbook", "playbook"], str(fake.uploaded))

    print("\n-- No plan, no apply --")
    res = client.post(f"/api/deployments/{dep['id']}/apply")
    check("apply without a plan is a 409 that names check mode",
          res.status_code == 409 and "check mode" in res.json()["detail"], res.text[:200])

    res = client.post(f"/api/deployments/{dep['id']}/plan")
    check("a plan starts", res.status_code == 200 and res.json()["kind"] == "plan", res.text[:200])
    started = fake.started[-1]
    check("it runs the deployment's own plan.yml",
          started["playbook"] == "deployments/deploy-test/plan.yml", str(started))
    check("with the scope and the deployment as extra_vars",
          started["extra_vars"]["meraki_org_id"] == "923103"
          and started["extra_vars"]["deployment"] == "deploy-test", str(started["extra_vars"]))
    check("and the key as an env var, never stored on the record",
          started["envvars"] == {"MERAKI_DASHBOARD_API_KEY": "sekrit-value-9"}
          and "sekrit-value-9" not in json.dumps(client.get(f"/api/deployments/{dep['id']}").json()))
    check("a plan_job is not sent on a plan", "plan_job" not in started["extra_vars"])

    res = client.get(f"/api/deployments/{dep['id']}/result?kind=plan")
    check("a result before the job finishes says so",
          res.status_code == 200 and res.json()["finished"] is False, res.text[:200])
    res = client.post(f"/api/deployments/{dep['id']}/apply")
    check("and apply is still refused — the plan has not been read",
          res.status_code == 409 and "has not finished" in res.json()["detail"], res.text[:200])

    print("\n-- The plan is read, and the gate opens --")
    fake.finished[started["job"]] = True
    fake.results[started["job"]] = FIX["plan"]
    res = client.get(f"/api/deployments/{dep['id']}/result?kind=plan")
    body = res.json()
    check("the real plan payload comes back",
          body["has_result"] and body["result"]["plan"]["counts"]["create"] == 2, res.text[:300])
    check("and the record now says apply may go",
          client.get(f"/api/deployments/{dep['id']}").json()["apply_blocked"] == "")

    res = client.post(f"/api/deployments/{dep['id']}/apply")
    check("apply starts", res.status_code == 200 and res.json()["kind"] == "apply", res.text[:200])
    applied = fake.started[-1]
    check("and carries the plan it was approved against",
          applied["extra_vars"]["plan_job"] == started["job"], str(applied["extra_vars"]))

    fake.finished[applied["job"]] = True
    fake.results[applied["job"]] = FIX["apply"]
    res = client.get(f"/api/deployments/{dep['id']}/result?kind=apply")
    check("the real apply payload comes back",
          res.json()["result"]["apply"]["counts"]["created"] == 2, res.text[:300])
    record = client.get(f"/api/deployments/{dep['id']}").json()
    check("the network ids landed on the record, per site",
          record["site_ids"]["deploy-test-001"]["network_id"] == "N_706502191543928065",
          str(record.get("site_ids")))
    check("the list view counts what was built",
          next(x for x in client.get("/api/deployments").json()["deployments"]
               if x["id"] == dep["id"])["built"] == 3)

    print("\n-- The kit --")
    res = client.post("/api/deployments", json={"name": "Kit via API", "provider": "meraki"})
    kit = res.json()
    check("a new deployment has no playbooks until the kit is fetched",
          not client.get(f"/api/deployments/{kit['id']}").json()["plan_text"])
    res = client.post(f"/api/deployments/{kit['id']}/kit")
    check("fetching the kit snapshots all three playbooks",
          res.status_code == 200 and len(res.json()["fetched"]) == 3, res.text[:200])
    check("including destroy, which is wired to nothing yet",
          client.get(f"/api/deployments/{kit['id']}").json()["destroy_text"].startswith("- hosts"))
    check("and publish now has something to send",
          client.get(f"/api/deployments/{kit['id']}").json()["plan_text"].startswith("- hosts"))
    res = client.post("/api/deployments", json={"name": "No kit", "provider": "azure"})
    res = client.post(f"/api/deployments/{res.json()['id']}/kit")
    check("a provider with no kit on the runner is a 400 that says so",
          res.status_code == 400 and "no azure kit" in res.json()["detail"], res.text[:200])

    print("\n-- Refusals by name --")
    res = client.post("/api/deployments", json={
        "name": "Bad key", "provider": "meraki", "keys": ["nope"], "plan_text": PLAN,
        "apply_text": PLAN, "scope": {}})
    bad = res.json()
    client.post(f"/api/deployments/{bad['id']}/publish", json={})
    res = client.post(f"/api/deployments/{bad['id']}/plan")
    check("an unreadable key stops the run by name",
          res.status_code == 400 and "nope" in res.json()["detail"], res.text[:200])
    res = client.post(f"/api/deployments/{dep['id']}/sites",
                      json={"text": CSV, "mapping": {"name": "Site"}})
    check("a column that is not there is named",
          res.status_code == 400 and "'Site'" in res.json()["detail"], res.text[:200])
    res = client.delete(f"/api/deployments/{dep['id']}")
    check("deleting forgets the record and touches nothing",
          res.json()["deleted"] is True and len(fake.started) == 2)


def main() -> int:
    print("=" * 52)
    print("  Deployments — the API")
    print("=" * 52)
    try:
        test_the_flow()
    except Exception as exc:
        failed.append(f"test_the_flow: raised {type(exc).__name__}: {exc}")
        print(f"  FAIL test_the_flow raised {type(exc).__name__}: {exc}")
    shutil.rmtree(_TEMP, ignore_errors=True)
    print("\n" + "=" * 52)
    print(f"  {passed} passed  |  {len(failed)} failed")
    print("=" * 52)
    if failed:
        print("\nFAILURES:")
        for item in failed:
            print(f"  {item}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
