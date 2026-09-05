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

## 2026-09-05 22:50 — step 6 done: the Deployments area

`frontend/js/ansible_deployments.js`, a tab beside Environments. A list of
cards; a detail view laid out as the five steps in the order they happen
— sites, scheme, publish, plan, apply — with the plan and outcome tables
under them, rendered from the runner's real payloads.

The interface keeps the backend's rules rather than its own: Apply is off
on the server's `apply_blocked` word, with the reason written beside it,
and `disabled` is only ever set when true (el() sets any attribute it is
given, and disabled="false" is a disabled button — caught by reading el()
rather than assuming). Conflicts then creates in a plan; failures then
creates in an outcome. Network ids on plan rows too, so an unchanged or
conflicting site names the network it matched before any apply exists.

The scheme editor is generic JSON until the runner's kit declares its
fields — a form invented here for fields the playbook does not read would
be a form that lies. Step 3 waits on the runner's plan.yml/apply.yml and
its scheme field list.

`python test_deployments_ui.py` — 24 passed, including one pass in a
browser. `test_ansible_view.py` gained the tab: 44 passed.

## 2026-09-05 23:10 — the decision is taken

Steven: ShellMate is the sole git writer; the runner's tree stays a mirror
and is not committed there. The deploy-test networks stay. The runner has
been told both. Steps 3 and 7 still wait on the runner's playbook texts
and its Azure/AWS skeletons — asked for again, since its reply never
arrived here.

## 2026-09-06 00:05 — step 3 done: the kit, and the scheme as a form

The runner owns provider knowledge. A new deployment fetches its plan and
apply from `deployments/_kit/<provider>/` on the runner and snapshots them
on the record — so the deployment commits the exact texts it was applied
with, and a later kit change never silently rewrites a deployment already
built. Fetching again is a deliberate step. No copy lives in the exe.

The Meraki scheme is a form for the four keys the kit reads, with the
runner's meaning and default beside each — `manage_prefix` required, with
its reason as the hint: only sites whose name starts with it are
configured beyond creation, which is what stops a deployment reaching into
a network it did not create. Other providers keep the JSON editor until
their kit declares fields. Site uploads gained `third_octet` (validated
0–255, by site) and `timezone` as nominated columns.

Publish now reports what the runner said changed, and names any file that
replaced a copy on the host that differed from the commit.

The runner reports plan and apply now agree — plan → apply → re-plan
converges to all-unchanged — and reads MERAKI_DASHBOARD_API_KEY, the
collection's own name, as asked. Its Meraki and Azure kits are live.

Tests: deployments 52, publish 27, api 28, ui 24, ansible_view 44.

## 2026-09-06 00:40 — the kits made durable; the tables follow the runner's rows

Nothing commits the runner's working tree — that is the decision — so a
kit the runner session writes exists only on a bind mount until ShellMate
commits it. `deployments.commit_kit(provider)` fetches the kit's files from
the runner and commits them under `runner/project/deployments/_kit/` in
one revision; no PUT goes back, because the bytes came from there. No
GitHub is an error here, not a state: durability is the whole point.

Plan rows gained `managed` (a site outside `manage_prefix` reads
"planned, not touched" on the row — a fact, not an inference from a
blank); apply rows gained `vlans` and the `updated` outcome. The fixtures
carry the runner's convergence test: plan {1 create, 3 update, 1
unchanged} → apply {1 created, 3 updated, 1 skipped, 0 failed} → re-plan
{0, 0, 5, 0}. That is what "unchanged" means now.

The area's browser check waited 400ms for a fetch; it now waits for the
render. Tests: publish 33, ui 28, deployments 52, api 28.

## 2026-09-06 01:20 — step 7, the Azure half; kits are five files; scope is per provider

The runner's Azure kit is ready and converges the same way Meraki does —
and answering my questions turned up the Meraki VLAN bug again in Azure
(`subnets[0]` only; a plan that compared resource-group existence only).
Fixed on its side, proved by a plan that immediately reported the missing
voice subnet on both existing sites, then applied, then re-planned clean.
Twice is a pattern: every scheme key the form exposes must be acted on by
apply for every element and compared by plan, or the form lies. That rule
has gone to the runner for AWS before it is written.

On this side: the scheme form is now a spec per provider (Meraki and
Azure, JSON for anything without a spec); a kit is five files and all five
are committed, fetched fresh at the moment of committing; and scope is
per provider — Meraki's org id is a playbook variable, Azure's
subscription is an environment variable the collection reads and would
silently ignore as a var. Encoded as a table rather than assumed
symmetric, so AWS can say which it is.

Tests: deployments 54, publish 33, api 28, ui 30.

## 2026-09-06 02:00 — step 7 complete: AWS

The runner's AWS kit passed the convergence test with a site and two
elements added — a third subnet and a second SG rule — and the plan named
every element down to the subnet CIDR and the port. Three kits, five
files each, all converging.

One AWS gotcha now lives in the form: AWS forbids security-group names
beginning `sg-`, and `amazon.aws.ec2_security_group` swallows the reason
— the runner found the cause only by calling boto3 directly. The form
refuses that prefix with the real reason. `aws_region` is an extra var
overriding the scheme's region: three providers, three different answers
to "where does scope go", all encoded, none assumed.

`destroy.yml` exists in every kit and is wired to nothing yet; Azure's
and AWS's are unproven because proving them deletes what Steven said to
leave. Tests: deployments 55, ui 32.
