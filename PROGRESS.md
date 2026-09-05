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
