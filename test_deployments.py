"""
test_deployments.py — Infrastructure built from a definition, on disk.

The object behind the Deployments area: a site data set, a scheme, and the
four files they become. Three rules, each enforced rather than trusted:

**Rendering is deterministic.** The same record produces the same bytes,
because the git commit and the PUT to the runner must match and "did
anything change" is a byte comparison.

**Columns are asked for, never guessed.** The same rule as inventories:
the name column is nominated, a duplicate name is refused by name.

**The git path and the runner path differ in exactly one place.**
`PROJECT_PREFIX` is prepended for the repository and never seen by the
runner; the same bytes travel under both paths.

And the gate: no plan, no apply. Meraki has no check mode, so the plan
playbook is the only preview there is.

    python test_deployments.py
"""

import shutil
import sys
import tempfile
from pathlib import Path

from backend import paths

_TEMP = Path(tempfile.mkdtemp(prefix="shellmate-deploy-"))
paths._data_dir_cache = _TEMP

import yaml                                                    # noqa: E402

from backend import deployments as d                           # noqa: E402

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


def _raises(fn, exc_type=d.DeploymentError) -> str:
    try:
        fn()
    except exc_type as exc:
        return str(exc)
    except Exception:
        return ""
    return ""


CSV = "\n".join([
    "Network Name,Region,Tags,MX Serial,MS Serial,Timezone,Octet",
    "Glasgow Central,uk-west,retail;flagship,Q2XX-AAAA-0001,Q2YY-BBBB-0001,Europe/London,10",
    "Edinburgh Waverley,uk-east,retail,,,Europe/London,11",
    "Zürich HB,ch,retail;flagship,,,Europe/Zurich,",
])
MAPPING = {"name": "Network Name", "tags": "Tags", "mx": "MX Serial",
           "ms": "MS Serial", "timezone": "Timezone", "third_octet": "Octet",
           "extra": ["Region"]}


# ---------------------------------------------------------------------------

def test_the_data_set() -> None:
    print("\n-- Columns asked, never guessed --")

    sites = d.sites_from_upload(CSV, MAPPING)
    check("three sites", len(sites) == 3, str(sites))
    check("the name column is the one nominated",
          sites[0]["name"] == "Glasgow Central")
    check("tags split on ; or ,", sites[0]["tags"] == ["retail", "flagship"])
    check("serials land under serials, both halves",
          sites[0]["serials"] == {"mx": "Q2XX-AAAA-0001", "ms": "Q2YY-BBBB-0001"})
    check("a site with no serials has no serials key — 'not yet', not an error",
          "serials" not in sites[1], str(sites[1]))
    check("extra columns pass through as variable names",
          sites[0]["region"] == "uk-west", str(sites[0]))
    check("timezone is its own nominated column",
          sites[0]["timezone"] == "Europe/London" and "timezone" not in sites[2].get("tags", []),
          str(sites[0]))
    check("the third octet is a number, and blank is absent",
          sites[0]["third_octet"] == 10 and "third_octet" not in sites[2], str(sites))
    bad = CSV.replace(",Europe/London,10", ",Europe/London,300", 1)
    why = _raises(lambda: d.sites_from_upload(bad, MAPPING))
    check("an octet outside 0-255 is refused by site and value",
          "Glasgow Central" in why and "300" in why, why)
    check("unicode survives", sites[2]["name"] == "Zürich HB")

    why = _raises(lambda: d.sites_from_upload(CSV, {}))
    check("no name column nominated is refused, and says why",
          "will not guess" in why, why)
    why = _raises(lambda: d.sites_from_upload(CSV, {"name": "Site"}))
    check("a column that is not there is named",
          "'Site'" in why, why)

    dup = CSV + "\nglasgow central,uk-west,,,,Europe/London"
    why = _raises(lambda: d.sites_from_upload(dup, MAPPING))
    check("a duplicate name is refused by name, case-insensitively",
          "glasgow central" in why.lower() and "one network" in why, why)

    why = _raises(lambda: d.sites_from_upload("a\nb\nc", {"name": "x"}))
    check("a plain list is refused — sites need columns",
          "needs columns" in why, why)


def test_the_record() -> None:
    print("\n-- The record --")

    rec = d.save({"name": "Glasgow — Phase 2", "provider": "meraki",
                  "sites": d.sites_from_upload(CSV, MAPPING), "mapping": MAPPING,
                  "scheme": {"base_prefix": "10.10.0.0/16", "vlans": [10, 20, 30]}})
    check("a slug from the name", rec["slug"] == "glasgow-phase-2", rec["slug"])
    check("listed with a count, not the rows",
          d.deployments()[0]["sites"] == 3 and "site_ids" not in d.deployments()[0])

    again = d.save({"id": rec["id"], "name": "Glasgow Phase Two", "provider": "meraki",
                    "scheme": rec["scheme"]})
    check("renaming keeps the slug — it is a folder in git and on the runner",
          again["slug"] == "glasgow-phase-2", again["slug"])
    check("and keeps the sites when none were sent",
          len(again["sites"]) == 3)

    check("an unknown provider is refused",
          "one of" in _raises(lambda: d.save({"name": "x", "provider": "gcp"})))
    check("a name that slugs to nothing is refused",
          "nothing usable" in _raises(lambda: d.slug_for("—— ——")))
    check("deleting forgets the record and nothing else",
          d.delete(rec["id"]) is True and d.get(rec["id"]) is None)


def test_rendering_is_deterministic() -> None:
    print("\n-- Same record, same bytes --")

    rec = d.save({"name": "Det", "provider": "meraki",
                  "sites": [{"name": "b", "tags": ["y", "x"]}, {"name": "a"}],
                  "scheme": {"zeta": 1, "alpha": {"k": "v", "a": 2}}})
    one = d.render_scheme(rec)
    two = d.render_scheme(d.get(rec["id"]))
    check("scheme renders identically twice", one == two)
    check("keys are sorted, so key order in the request cannot change the bytes",
          one.index("alpha") < one.index("provider") < one.index("zeta"), one)
    check("the scheme names its deployment and provider",
          yaml.safe_load(one)["deployment"] == "det"
          and yaml.safe_load(one)["provider"] == "meraki")
    sites = d.render_sites(rec)
    check("sites render under a top-level key the playbooks read",
          yaml.safe_load(sites)["sites"][0]["name"] == "b")
    check("and site order is preserved — it is the order they were uploaded",
          yaml.safe_load(sites)["sites"][1]["name"] == "a")
    check("unicode is written as itself",
          "Zürich" in d.render_sites({"slug": "z", "sites": [{"name": "Zürich"}]}))


def test_two_paths_one_prefix() -> None:
    print("\n-- One prefix, two paths --")

    runner = d.runner_paths("glasgow-phase-2")
    git = d.git_paths("glasgow-phase-2")
    check("the runner sees deployments/<slug>/…",
          runner["plan.yml"] == "deployments/glasgow-phase-2/plan.yml", str(runner))
    check("git sees the project prefix in front",
          git["plan.yml"] == "runner/project/deployments/glasgow-phase-2/plan.yml", str(git))
    check("and that prefix lives in exactly one constant",
          d.PROJECT_PREFIX == "runner/project/"
          and all(g == d.PROJECT_PREFIX + r for g, r in zip(git.values(), runner.values())))

    rec = {"slug": "glasgow-phase-2", "provider": "meraki",
           "sites": [{"name": "a"}], "scheme": {"x": 1}}
    files = d.files_for(rec, "- hosts: localhost\n", "- hosts: localhost\n  tasks: []\n")
    tree = d.as_git_tree(files)
    check("four files each side", len(files) == 4 and len(tree) == 4)
    check("the same bytes under both paths",
          all(tree[d.PROJECT_PREFIX + p] == b for p, b in files.items()))
    check("playbooks and data files are told apart, as the runner does",
          set(d.PLAYBOOKS) | set(d.DATA_FILES) == set(d.FILES))

    for bad in ("../x", "a/b", "A", "", "x..y"):
        check(f"{bad!r} cannot be a folder", bool(_raises(lambda: d.runner_paths(bad))))


def test_the_kit() -> None:
    """
    The runner owns provider knowledge; the deployment snapshots it.
    """
    print("\n-- The kit --")
    check("a kit lives under deployments/_kit/<provider>/",
          d.kit_paths("meraki")["plan.yml"] == "deployments/_kit/meraki/plan.yml")
    check("and an unknown provider has none",
          bool(_raises(lambda: d.kit_paths("gcp"))))

    class Runner:
        def __init__(self, texts): self.texts = texts; self.asked = []
        def read_playbook(self, path):
            self.asked.append(path); return self.texts.get(path, "")

    rec = d.save({"name": "Kit", "provider": "meraki", "scheme": {}})
    full = Runner({"deployments/_kit/meraki/plan.yml": "- hosts: localhost\n",
                   "deployments/_kit/meraki/apply.yml": "- hosts: localhost\n  tasks: []\n"})
    out = d.fetch_kit(rec["id"], runner=full)
    after = d.get(rec["id"])
    check("both playbooks are fetched from the kit",
          sorted(full.asked) == sorted(d.kit_paths("meraki").values()), str(full.asked))
    check("and snapshotted on the record",
          after["plan_text"].startswith("- hosts") and after["apply_text"].startswith("- hosts"))
    check("with when", bool(after["kit_fetched"]) and out["plan_bytes"] > 0)
    check("a save keeps the snapshot",
          d.save({"id": rec["id"], "name": "Kit", "provider": "meraki",
                  "scheme": {"x": 1}})["plan_text"].startswith("- hosts"),
          "a later kit change must not silently rewrite a deployment already built")

    why = _raises(lambda: d.fetch_kit(rec["id"], runner=Runner({})))
    check("a runner with no kit says so, and who owns kits",
          "no meraki kit" in why and "runner session" in why, why)


def test_scope_per_provider() -> None:
    """
    Meraki's org id is a playbook variable; Azure's subscription is an
    environment variable the collection reads. Sending the latter as a var
    would be silently ignored.
    """
    print("\n-- Scope is per provider --")
    m = d.save({"name": "M", "provider": "meraki", "scope": {"meraki_org_id": "923103"}})
    check("Meraki: the org id is an extra var",
          d.run_vars(m, "plan")["meraki_org_id"] == "923103" and d.run_env(m) == {})
    a = d.save({"name": "A", "provider": "azure", "scope": {"azure_subscription_id": "394c"}})
    check("Azure: the subscription is an env var, and not a var",
          d.run_env(a) == {"AZURE_SUBSCRIPTION_ID": "394c"}
          and "azure_subscription_id" not in d.run_vars(a, "plan"),
          str((d.run_env(a), d.run_vars(a, "plan"))))
    w = d.save({"name": "W", "provider": "aws", "scope": {"aws_region": "eu-west-2"}})
    check("AWS: the region is an extra var, overriding the scheme's",
          d.run_vars(w, "plan")["aws_region"] == "eu-west-2" and d.run_env(w) == {},
          "three providers, three different answers — encoded, not assumed")


def test_the_gate() -> None:
    """
    Meraki has no check mode, so the plan is the only preview.
    """
    print("\n-- No plan, no apply --")

    rec = d.save({"name": "Gate", "provider": "meraki", "scheme": {}})
    check("apply is refused with no plan, and says why",
          "check mode" in d.apply_allowed(rec), d.apply_allowed(rec))

    d.record_run(rec["id"], "plan", "job-1", None)
    check("a plan whose result has not been read is not enough",
          "has not finished" in d.apply_allowed(d.get(rec["id"])))

    d.record_run(rec["id"], "plan", "job-1",
                 {"plan": {"counts": {"create": 1}, "sites": [], "truncated": False}})
    check("a plan with a result opens the gate",
          d.apply_allowed(d.get(rec["id"])) == "", d.apply_allowed(d.get(rec["id"])))

    import time
    time.sleep(1.1)
    d.save({"id": rec["id"], "name": "Gate", "provider": "meraki", "scheme": {"changed": 1}})
    check("editing the definition closes it again",
          "changed after" in d.apply_allowed(d.get(rec["id"])))

    d.record_run(rec["id"], "apply", "job-2", {"apply": {
        "plan_job": "job-1", "counts": {"created": 1},
        "sites": [{"name": "a", "outcome": "created",
                   "ids": {"network_id": "N_1"}}], "truncated": False}})
    check("an apply's ids are kept per site — the only record of what was built",
          d.get(rec["id"])["site_ids"] == {"a": {"network_id": "N_1"}},
          str(d.get(rec["id"])["site_ids"]))
    check("and the list view counts what was built",
          next(x for x in d.deployments() if x["id"] == rec["id"])["built"] == 1)
    check("a run is a plan or an apply, nothing else",
          bool(_raises(lambda: d.record_run(rec["id"], "destroy", "j"))))


def main() -> int:
    print("=" * 52)
    print("  Deployments")
    print("=" * 52)
    for test in (test_the_data_set, test_the_record, test_rendering_is_deterministic,
                 test_two_paths_one_prefix, test_the_kit, test_scope_per_provider,
                 test_the_gate):
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
