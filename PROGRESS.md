# Progress — Deployments

**Goal:** the framework to build infrastructure from a definition —
sites data set, scheme, provider templates, one git commit, plan then a
gated apply, an outcome table with ids — Meraki first, Azure and AWS the
same way. Designed jointly with the runner session; its side runs in
parallel.

## 2026-09-05 20:40 — run opened

Contract agreed with `ansible-08` and recorded in PLAN.md. Runner side
already done: `/files/{path}`, stdout paging, `/jobs/{id}/result`. Runner
is building the Meraki plan/apply skeletons against `deploy-test-*`.

Assumption stated rather than decided: ShellMate is the sole git writer.

## 2026-09-05 21:05 — step 1 done, and the data-set half of step 2

`backend/deployments.py`: the record, deterministic rendering, the two
path sets under one `PROJECT_PREFIX`, the site data set from an upload
with the columns asked (reusing `ansible_inventories.preview`), and the
gate — `apply_allowed()` refuses without a plan, without a fetched plan
result, or with a plan older than the definition.

Two things decided in the code rather than left to the UI: a duplicate
site name is refused by name (two sites called Glasgow build one network
and report both as created), and deleting a deployment deletes the record
and nothing in the cloud — tearing down is an apply of its own.

`python test_deployments.py` — 42 passed.

## 2026-09-05 21:35 — step 4 done: one commit, two PUTs

`ansible_git.commit_tree()` — the Trees API, one commit for all four files,
the branch moved without force so a commit that landed meanwhile is a
refusal rather than an overwrite. `ansible.upload_file()` for the two data
files on the runner's new `/files/{path}` route (a data file on the
playbook route is a 422 — decided by file name here, not discovered at the
second file). `deployments.publish()` orders them: commit, then send. A
configured GitHub that refuses sends nothing; GitHub not configured at all
is a state, said in the result, and the runner's copy is the only copy.

The runner's Meraki plan and apply are built and proven against the real
org (runner `f5531d4`); real result payloads are in
`test_fixtures/deploy_results.json` and the renderer is written against
them. Three `deploy-test-*` networks now exist in Steven's org.

`python test_deployments_publish.py` — 24 passed. Guards: ansible 79,
ansible_github 29, ansible_library 83, deployments 42.

## 2026-09-05 22:10 — step 5 done: plan, then a gated apply

Routes under `/api/deployments`: save, sites (preview, then store with the
mapping named), publish, plan, apply, result. `ansible.result()` reads
what a run published with set_stats. Keys live on the deployment by name
and become values only when a run starts; an unreadable one stops the run
by name.

Three gates, in the order somebody hits them: not published → "publish
first" (the missing-playbook failure it prevents is several screens from
the cause); no plan → the check-mode sentence; plan not read → "has not
finished". Fetching the plan's result is what opens the gate — read, not
merely produced — and an apply carries the plan's job id it was approved
against. Ids per site land on the record from the apply's result.

The API test drives the whole flow against the runner's real payloads
from `test_fixtures/`. `python test_deployments_api.py` — 24 passed.
Guards: deployments 42, publish 24, ansible 79, startup 154, library 83.
