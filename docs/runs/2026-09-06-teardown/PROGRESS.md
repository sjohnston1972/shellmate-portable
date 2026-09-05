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

## 2026-09-06 04:40 — steps 4 and 5 done: the destroy step, the dialog, the orphan warning

A Destroy plan step and a Destroy step after Apply; destroy off on the
server's `destroy_blocked` word with the reason beside it; a dialog that
wants the deployment's name typed and says, per provider, what destroy
does NOT remove — the runner's observed list, not a reasoned one. The
destroy table renders from `outcome` per row and never from a list of
names, failures first: the runner's first version listed what was
*attempted* as removed, and that is exactly the lie a table would have
laundered.

The sharpest finding became two pieces of UI: a re-upload that drops a
built site names it as orphaned at that moment, and Forget on a deployment
that built anything offers Destroy first rather than only forgetting.

Documented under Ansible → Deployments → Tearing down. Tests: api 36,
ui 41, deployments 69, ansible_view 44, startup 154, publish 34.

## 2026-09-06 05:10 — step 1's done-condition met; close-out begins

The runner shipped `dry_run` on all three destroys as the extra var — one
file that knows the ordering — and proved it: each dry run reported
changed=0 and the Meraki networks were listed afterwards, all still
there. Then it ran all three destroys for real and the mirror test after
each: every site `create` on the apply plan afterwards. Estate back to
baseline. The real dry-run payload (four removals, one skip with its
reason) is in the fixtures and the table's skip-row rendering is asserted.

Full suite: 116 of 116. Tests: ui 43.

## 2026-09-06 05:40 — close-out

Executable rebuilt at `8a928fd`, 37068838 bytes. Full suite 116 of 116.
Archived to `docs/runs/2026-09-06-teardown/`; DONE is the hand-off.
