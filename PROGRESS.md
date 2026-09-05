# Progress — Teardown

**Goal:** wire `destroy.yml` behind a plan of its own and a typed
confirmation, and prove Azure's and AWS's destroy against the test
resources Steven has said to use — the mirror of the convergence test.

## 2026-09-06 03:00 — run opened

Steven's decision: the test resources are the ones to prove destroy
against. The runner has been asked for a destroy plan (dry run), the
destroy outcome shape, the proof for Azure and AWS, and the words for what
destroy does not touch. Steps 2 and 3 proceed against the proposed
contract meanwhile.

## 2026-09-06 03:20 — step 2 done: destroy.yml is the fifth deployment file

`FILES` and `PLAYBOOKS` gain `destroy.yml`; `fetch_kit` snapshots it
beside plan and apply (a kit without one is still a kit); `files_for` and
`publish` carry it when present, and never commit an empty destroy under
a name that promises the opposite. The destroy that fits an apply is the
one snapshotted beside it, not whatever the kit says later.

Tests: deployments 59, publish 34, api 29.

## 2026-09-06 04:10 — step 3 done: the destroy plan and the gated destroy, over the API

`POST /destroy/plan` runs destroy.yml with `dry_run: true`; `POST /destroy`
with `dry_run: false` and the deployment's name typed back, checked
server-side because the API is scriptable. Refused by name: no destroy
playbook, no manage prefix (an empty one matches everything), no destroy
plan, a plan not read, a plan older than the definition, the wrong name.
After a destroy, `site_ids` go only for the sites the runner says are
gone — a failed or skipped site keeps its id, which is how somebody finds
what is still there.

The runner has now run all three destroys against the real test
resources. AWS failed first: subnets must go before the VPC, and its
reporting listed what was *attempted* as removed — a failed teardown
would have rendered as a clean one. Both fixed; the mirror test (an apply
plan afterwards showing every site as `create`) passes for all three, and
the estate is back to baseline. The sharpest finding: a site row deleted
from the data set is orphaned — destroy cannot see it. That becomes a
warning on upload and a forget-offers-destroy flow in steps 4 and 5.

Tests: deployments 69, api 35, publish 34.
