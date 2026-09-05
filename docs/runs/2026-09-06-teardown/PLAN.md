# Run plan — Teardown: destroy behind a plan, proven against the test resources

Steven's decision, 2026-09-06: the test resources are the ones to prove
destroy against — the four `deploy-test-*` Meraki networks, `rg-azure-
test-001/002` with their vnets, subnets and NSGs, and the three AWS VPCs
with nine subnets. `destroy.yml` exists in every kit and is wired to
nothing; Meraki's has been exercised, Azure's and AWS's have not, and AWS's
ordering (security groups before the VPC) is reasoned rather than tested.

Succeeds the Deployments run, archived in `docs/runs/2026-09-06-deployments/`.
Built jointly with the runner session (`ansible-08`) as before: it owns the
playbooks and proves them; ShellMate owns the gate and the tables.

## The rules this run keeps

**A teardown deserves a plan of its own.** Destroy is offered only after a
read-only destroy plan has run and its result has been fetched and shown —
the same gate as apply, for the same reason, with more at stake.

**A typed confirmation, not a click.** Deleting five hundred networks is
not a button. The confirmation requires typing the deployment's name, and
the dialog says in words exactly what will and will not be removed.

**Only what the deployment built.** Destroy removes a site only if it is
both in `sites.yml` and matches `manage_prefix`, and refuses to run at all
with an empty prefix. Anything outside the prefix is listed as skipped,
with the reason, never silently omitted.

**Forgetting is not destroying.** "Forget a deployment" offers destroy
first as a choice; it never does it as a side effect.

## The contract (proposed to the runner; confirmed when its payloads land)

    destroy plan:  extra var dry_run=true (or destroy-plan.yml — runner's call)
    result:  {destroy: {provider, deployment, dry_run, counts: {remove, skip, failed},
                        sites: [{name, action|outcome, reason, ids, elements: [str]}],
                        truncated}}

`elements` names what goes per site in order — Meraki: the network;
Azure: NSG, subnets, vnet, resource group; AWS: SG rules, SGs, subnets,
VPC — the same "name every element" standard the apply plan meets.

The proof for each provider is the mirror of the convergence test:
destroy plan → destroy → a re-plan of the *apply* kit showing every site
as `create`. That is what "nothing is left" means.

---

## 1. Runner: prove Azure and AWS destroy against the test resources

Asked for at the start of the run. Blocks steps 4 and 5; steps 2 and 3
proceed against the proposed contract and are corrected when payloads
arrive.

**Done when:** the runner has sent real destroy-plan and destroy payloads
for all three providers, and the re-plan after each shows every site as
`create`.

## 2. `destroy.yml` becomes the fifth deployment file

`FILES` gains `destroy.yml`; `fetch_kit` snapshots it beside plan and
apply; `publish` commits and sends five. A deployment already published
with four is republished to gain the fifth — the record says so.

**Done when:** `test_deployments*` pass with five files under both paths.

## 3. The destroy plan and the gated destroy, over the API

`POST /api/deployments/{id}/destroy/plan` and `POST .../destroy`, with
`GET .../result?kind=destroy_plan|destroy`. Destroy refuses without a
destroy plan whose result has been read, with a plan older than the
definition, or when `manage_prefix` is empty — each refusal naming its
reason. The confirmation token (the deployment's name, typed) is checked
server-side too: the API is scriptable.

After a destroy, `site_ids` is cleared for every site reported removed —
and only those.

**Done when:** `test_deployments_api.py` drives plan → destroy plan →
destroy against the runner's real payloads and the ids are gone for the
removed sites and kept for the skipped ones.

## 4. The destroy step in the detail view

A step after Apply, off until a destroy plan has been read, then a dialog
that requires the deployment's name typed and states in words what will
and will not be removed (from the runner's answer to that question). The
destroy-plan table lists what goes per site with its elements; the
outcome table lists failures first.

**Done when:** `test_deployments_ui.py` passes and the browser pass shows
the step off with its reason.

## 5. Forget offers destroy first

"Forget a deployment" becomes a choice: forget the record only (default,
as now), or run the destroy flow first. Never the latter silently.

**Done when:** the dialog offers both and the test asserts the default.

## 6. Close out

Manual section, full suite, rebuild, archive, DONE. The test resources
will be gone at the end of this run — that is the point of it.

## Not in this run

- Claiming real serials — no devices exist.
- Scheduled applies — #611, blocked on an always-on host.
- The team-and-identity cluster (#566, #526, #541) — the next batch.
