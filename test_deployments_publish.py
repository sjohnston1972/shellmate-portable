"""
test_deployments_publish.py — One commit, two PUTs.

A deployment's four files reach two places: the repository, in one commit
under `runner/project/`, and the runner, under `deployments/` with the
playbooks and the data files on different routes. Four properties:

**Commit first, then send.** A PUT without its commit is bytes on the
runner no revision describes. A configured GitHub that refuses means
nothing is sent; GitHub not configured at all is a state, not an error,
and is said in the result.

**The same bytes under two paths.** `PROJECT_PREFIX` is prepended for git
and never seen by the runner, and this proves it rather than trusting the
two call sites to agree.

**Playbooks and data on their own routes.** The runner refuses a data file
on the playbook route with a 422; the split is decided by file name here.

**One commit via the Trees API, and the branch is never forced.** A commit
that landed on the branch meanwhile is a refusal, not an overwrite.

    python test_deployments_publish.py
"""

import base64
import json
import shutil
import sys
import tempfile
from pathlib import Path

from backend import paths

_TEMP = Path(tempfile.mkdtemp(prefix="shellmate-publish-"))
paths._data_dir_cache = _TEMP

from backend import ansible_git, deployments as d               # noqa: E402

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


PLAN = "- hosts: localhost\n  vars_files: [sites.yml, scheme.yml]\n  tasks: []\n"
APPLY = "- hosts: localhost\n  vars_files: [sites.yml, scheme.yml]\n  tasks: [{debug: {msg: hi}}]\n"


class FakeGit:
    def __init__(self, configured=True, refuse=False):
        self.configured = configured
        self.refuse = refuse
        self.trees: list[dict] = []
        self.messages: list[str] = []

    def config(self):
        return {"has_token": self.configured, "repo": "ansible" if self.configured else "",
                "owner": "sjohnston1972"}

    def commit_tree(self, tree, message):
        if self.refuse:
            raise ansible_git.GitError("GitHub refused: no permission", "refused")
        self.trees.append(dict(tree))
        self.messages.append(message)
        return {"sha": "abc1234", "url": "https://github.com/x/y/commit/abc1234",
                "files": sorted(tree), "branch": "main"}


class FakeRunner:
    def __init__(self, changed=True, overwrote=False):
        self.calls: list[tuple[str, str, str]] = []
        self.changed, self.overwrote = changed, overwrote

    def upload_playbook(self, name, text, overwrite=False):
        self.calls.append(("playbook", name, text))
        return {"plays": 1, "changed": self.changed, "overwrote": self.overwrote}

    def upload_file(self, path, text, overwrite=True):
        self.calls.append(("file", path, text))
        return {"parsed": True, "changed": self.changed, "overwrote": self.overwrote}


def make() -> dict:
    return d.save({"name": "Glasgow", "provider": "meraki",
                   "sites": [{"name": "g-001"}, {"name": "g-002"}],
                   "scheme": {"base_prefix": "10.10.0.0/16"}})


# ---------------------------------------------------------------------------

def test_commit_then_send() -> None:
    print("\n-- Commit first, then send --")
    rec = make()
    git, runner = FakeGit(), FakeRunner()

    out = d.publish(rec["id"], PLAN, APPLY, git=git, runner=runner, destroy_text=APPLY)
    check("one commit was made", len(git.trees) == 1 and out["commit"]["sha"] == "abc1234")
    check("with all five files", len(git.trees[0]) == 5, str(sorted(git.trees[0])))
    check("under the project prefix",
          all(p.startswith("runner/project/deployments/glasgow/") for p in git.trees[0]),
          str(sorted(git.trees[0])))
    check("five files were sent to the runner", len(runner.calls) == 5)
    check("destroy went to the playbook route",
          any(r == "playbook" and p.endswith("/destroy.yml") for r, p, _ in runner.calls))
    check("and the record remembers the commit",
          d.get(rec["id"])["last_commit"]["sha"] == "abc1234")
    check("the message names the deployment and the site count",
          "glasgow" in git.messages[0] and "2 sites" in git.messages[0], git.messages[0])


def test_same_bytes_two_paths() -> None:
    print("\n-- The same bytes under two paths --")
    rec = make()
    git, runner = FakeGit(), FakeRunner()
    d.publish(rec["id"], PLAN, APPLY, git=git, runner=runner)

    tree = git.trees[0]
    sent = {path: text for _, path, text in runner.calls}
    check("runner paths have no prefix",
          all(p.startswith("deployments/glasgow/") for p in sent), str(sorted(sent)))
    check("every runner path has its git twin with identical bytes",
          all(tree[d.PROJECT_PREFIX + p] == t.encode("utf-8") for p, t in sent.items()))
    check("the plan went verbatim", sent["deployments/glasgow/plan.yml"] == PLAN)
    check("sites.yml is the rendered data set",
          "g-001" in sent["deployments/glasgow/sites.yml"])


def test_routes_by_file_name() -> None:
    print("\n-- Playbooks and data on their own routes --")
    rec = make()
    runner = FakeRunner()
    d.publish(rec["id"], PLAN, APPLY, git=FakeGit(), runner=runner, destroy_text=APPLY)

    routes = {path.rsplit("/", 1)[1]: route for route, path, _ in runner.calls}
    check("plan and apply go to the playbook route",
          routes["plan.yml"] == "playbook" and routes["apply.yml"] == "playbook", str(routes))
    check("sites and scheme go to the file route",
          routes["sites.yml"] == "file" and routes["scheme.yml"] == "file", str(routes),)
    check("decided by name here, not discovered at the second file",
          set(d.PLAYBOOKS) == {"plan.yml", "apply.yml", "destroy.yml"}
          and routes["destroy.yml"] == "playbook")


def test_when_git_is_not_there() -> None:
    print("\n-- GitHub absent versus GitHub refusing --")
    rec = make()

    runner = FakeRunner()
    out = d.publish(rec["id"], PLAN, APPLY, git=FakeGit(configured=False), runner=runner)
    check("not configured: sent anyway, and said",
          out["commit"] is None and len(runner.calls) == 4
          and "not set up" in out["skipped_git"], str(out))
    check("and the record stays uncommitted", d.get(rec["id"])["last_commit"] is None)

    runner = FakeRunner()
    try:
        d.publish(rec["id"], PLAN, APPLY, git=FakeGit(refuse=True), runner=runner)
        raised = False
    except ansible_git.GitError:
        raised = True
    check("configured but refusing: an error", raised)
    check("and nothing was sent — a PUT without its commit is a drift",
          runner.calls == [], str(runner.calls))

    try:
        d.publish(rec["id"], "", APPLY, git=FakeGit(), runner=FakeRunner())
        raised = False
    except d.DeploymentError as exc:
        raised = "both its plan and its apply" in str(exc)
    check("a missing playbook is refused before anything moves", raised)


def test_what_the_runner_said_changed() -> None:
    """
    With ShellMate the sole writer, "overwrote something that differed" is
    the one case worth a sentence: a copy on the host had been edited by
    hand, and the commit won.
    """
    print("\n-- What changed on the runner --")
    rec = make()
    out = d.publish(rec["id"], PLAN, APPLY, git=FakeGit(), runner=FakeRunner(changed=False, overwrote=True))
    check("same bytes again: nothing changed, nothing replaced",
          out["changed"] == [] and out["replaced"] == [], str(out))
    out = d.publish(rec["id"], PLAN, APPLY, git=FakeGit(), runner=FakeRunner(changed=True, overwrote=False))
    check("first write: changed, not replaced",
          len(out["changed"]) == 4 and out["replaced"] == [], str(out))
    out = d.publish(rec["id"], PLAN, APPLY, git=FakeGit(), runner=FakeRunner(changed=True, overwrote=True))
    check("different bytes over an existing copy: replaced, and named",
          len(out["replaced"]) == 4 and all(p.startswith("deployments/") for p in out["replaced"]),
          str(out))


def test_committing_a_kit() -> None:
    """
    Nothing commits the runner's tree, so a kit exists only on its disk
    until ShellMate commits it. The bytes come from the runner; no PUT
    goes back.
    """
    print("\n-- The kit made durable --")

    class Runner:
        def __init__(self, have): self.have = have; self.puts = 0
        def read_playbook(self, p): return self.have.get(p, "")
        def read_file(self, p):
            if p not in self.have: raise RuntimeError("404")
            return self.have[p]
        def upload_playbook(self, *a, **k): self.puts += 1
        def upload_file(self, *a, **k): self.puts += 1

    git = FakeGit()
    runner = Runner({"deployments/_kit/meraki/plan.yml": "- hosts: localhost\n",
                     "deployments/_kit/meraki/apply.yml": "- hosts: localhost\n  tasks: []\n",
                     "deployments/_kit/meraki/destroy.yml": "- hosts: localhost\n  tasks: []\n",
                     "deployments/_kit/meraki/scheme.yml": "manage_prefix: deploy-\n",
                     "deployments/_kit/meraki/sites.yml": "# sites: [{name}]\n"})
    out = d.commit_kit("meraki", git=git, runner=runner)
    check("all five kit files committed under the kit path",
          sorted(git.trees[0]) == sorted("runner/project/deployments/_kit/meraki/" + n
                                         for n in d.KIT_FILES),
          str(sorted(git.trees[0])))
    check("nothing was sent back to the runner", runner.puts == 0)
    check("the message names the provider", "meraki" in git.messages[-1])

    thin = Runner({"deployments/_kit/azure/plan.yml": "- hosts: localhost\n",
                   "deployments/_kit/azure/apply.yml": "- hosts: localhost\n"})
    out = d.commit_kit("azure", git=git, runner=thin)
    check("a kit without a scheme.yml still commits its two playbooks",
          len(out["files"]) == 2, str(out))

    try:
        d.commit_kit("aws", git=git, runner=Runner({})); raised = ""
    except d.DeploymentError as exc:
        raised = str(exc)
    check("a missing kit is refused by provider", "aws kit" in raised, raised)
    try:
        d.commit_kit("meraki", git=FakeGit(configured=False), runner=runner); raised = ""
    except d.DeploymentError as exc:
        raised = str(exc)
    check("and no GitHub is an error here, not a state — durability is the point",
          "not durable" in raised, raised)


def test_the_trees_api_sequence() -> None:
    """
    commit_tree against a scripted GitHub: the six requests in order, and
    the branch moved without force.
    """
    print("\n-- One commit via the Trees API --")
    calls: list[tuple[str, str, dict]] = []

    class Resp:
        def __init__(self, status, body):
            self.status_code, self._body = status, body

        def json(self):
            return self._body

        @property
        def text(self):
            return json.dumps(self._body)

    def fake_call(method, path, auth, **kwargs):
        calls.append((method, path, kwargs.get("json") or {}))
        if path.endswith("/repos/o/r"):
            return Resp(200, {"default_branch": "main"})
        if "/git/ref/heads/main" in path:
            return Resp(200, {"object": {"sha": "HEAD0000"}})
        if "/git/commits/HEAD0000" in path:
            return Resp(200, {"tree": {"sha": "TREE0000"}})
        if path.endswith("/git/blobs"):
            return Resp(201, {"sha": "BLOB" + str(len(calls))})
        if path.endswith("/git/trees"):
            return Resp(201, {"sha": "NEWTREE"})
        if path.endswith("/git/commits"):
            return Resp(201, {"sha": "NEWCOMMIT1234", "html_url": "https://x/c"})
        if "/git/refs/heads/main" in path:
            return Resp(200, {})
        return Resp(404, {"message": "nope"})

    real_call, real_token, real_config = ansible_git._call, ansible_git.token, ansible_git.config
    ansible_git._call = fake_call
    ansible_git.token = lambda: "tok"
    ansible_git.config = lambda: {"has_token": True, "owner": "o", "repo": "r",
                                  "enabled": True, "visibility": "private"}
    try:
        out = ansible_git.commit_tree({"runner/project/deployments/g/a.yml": b"a",
                                       "runner/project/deployments/g/b.yml": b"b"},
                                      "Deployment g")
    finally:
        ansible_git._call, ansible_git.token, ansible_git.config = real_call, real_token, real_config

    methods = [(m, p.rsplit("/", 1)[-1] if "/git/" not in p else p.split("/git/")[1].split("/")[0])
               for m, p, _ in calls]
    check("head, base tree, two blobs, tree, commit, ref — in that order",
          [m for m, _ in methods] == ["GET", "GET", "GET", "POST", "POST", "POST", "POST", "PATCH"],
          str(methods))
    check("the tree is built on the base tree",
          any(k.get("base_tree") == "TREE0000" for _, _, k in calls))
    check("the commit's parent is the head that was read",
          any(k.get("parents") == ["HEAD0000"] for _, _, k in calls))
    check("blobs are sent base64",
          any(k.get("encoding") == "base64" and base64.b64decode(k["content"]) == b"a"
              for _, _, k in calls))
    check("the branch is moved without force",
          any(k.get("force") is False for _, _, k in calls),
          "a commit that landed meanwhile must be a refusal, not an overwrite")
    check("the result carries the short sha and the files",
          out["sha"] == "NEWCOMM" and len(out["files"]) == 2, str(out))


def main() -> int:
    print("=" * 52)
    print("  Deployments — publish")
    print("=" * 52)
    for test in (test_commit_then_send, test_same_bytes_two_paths, test_routes_by_file_name,
                 test_when_git_is_not_there, test_what_the_runner_said_changed,
                 test_committing_a_kit, test_the_trees_api_sequence):
        try:
            test()
        except Exception as exc:
            failed.append(f"{test.__name__}: raised {type(exc).__name__}: {exc}")
            print(f"  FAIL {test.__name__} raised {type(exc).__name__}: {exc}")
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
