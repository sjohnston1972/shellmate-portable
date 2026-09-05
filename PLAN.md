# Run plan — Deployments: build infrastructure from a definition

Steven's example: a playbook that deploys 500 new Meraki networks in an org
— an MX and an MS in each, VLANs and subnets per site, firewall rules, port
profiles — then the same framework for Azure and AWS, all git-controlled,
driven from ShellMate through the runner.

Designed jointly with the runner session (`ansible-08`) on 2026-09-05. Every
API fact below was verified by that session against its container and the
collections, not read from an issue. Its side is being built in parallel:
stdout paging and `PUT /api/v1/files/{path}` are done (runner `beda2fb`);
the Meraki plan and apply skeletons are in progress against a
`deploy-test-*` prefix.

Supersedes #594's framing. Nothing here is an inventory: the cloud accounts
hold zero hosts, and Meraki is managed by calling its API with ids, not by
connecting to devices. The runner's census stands — 0 EC2, 0 Azure VMs,
0 Meraki devices — so the first real run creates networks and configuration
and claims serials later against the same data set.

## The one decision, taken as an assumption until Steven says otherwise

**ShellMate is the sole git writer.** It commits a deployment's four files
to `sjohnston1972/ansible` in one commit (Trees API) and PUTs the same bytes
to the runner, whose working tree is a mirror. The runner does not commit
its tree — nothing does today. Reason: one writer, the token stays where it
already is, and no git credentials on the container, which is the principle
Steven already applied when ruling out cloning. If he prefers the runner to
commit, step 4 loses its GitHub half and keeps the PUT.

## The contract (both sides build to this)

    deployments/<slug>/sites.yml    the data set: sites: [{name, tags, serials: {mx, ms}, ...}]
    deployments/<slug>/scheme.yml   base prefix, VLAN plan, rule sets, port profiles
    deployments/<slug>/plan.yml     read-only; publishes {plan: ...} via set_stats
    deployments/<slug>/apply.yml    serial-per-site block/rescue; publishes {apply: ...}

Playbooks and data files go to different routes (`/playbooks/{name}` vs
`/files/{path}`) and the git path has a `runner/project/` prefix the PUT
path never sees. One constant, one test that the same bytes reach both.

    plan:  {provider, deployment, counts: {create, update, unchanged, conflict},
            sites: [{name, action, detail, changes: [str]}], truncated: bool}
    apply: {provider, deployment, plan_job, counts: {created, updated, skipped, failed},
            sites: [{name, outcome, reason, ids: {network_id, ...}}], truncated: bool}

**Meraki has no check mode** — 0 of 309 network modules — so `--check`
silently skips every task and reports success. Preview is the plan
playbook and nothing else, and Apply is a hard gate behind a rendered,
acknowledged plan.

## How this run commits

One step, one commit, one push. Steps that complete a user-visible piece
say so in PROGRESS.md with the test that proves it. Nothing here closes an
issue until the last step; #594 is retitled then, not before.

---

## 1. `backend/deployments.py` — the object, on disk

A deployment record: `{id, slug, name, provider, description, scheme,
sites, mapping, environment_id, last_commit, last_plan_job,
last_apply_job, site_ids, created, updated}`. `sites` is a list of dicts
keyed by column name; `scheme` is a dict of the holes the provider's
template declares. Stored in `deployments.json` through `jsonfile`.

Rendering `sites.yml` and `scheme.yml` from the record is deterministic —
same record, same bytes — because the commit and the PUT must carry
identical content, and because "did anything change" is a byte comparison.

**Done when:** `python test_deployments.py` passes: round trip, stable
rendering, and a slug that cannot escape `deployments/`.

## 2. The site data set: upload with the columns asked, never guessed

Reuse `ansible_inventories`' CSV parsing and column-mapping rule. A data
set is uploaded, parsed into rows, and the caller nominates which columns
hold `name`, `tags` and the optional `mx`/`ms` serials; the mapping is
stored with the deployment so a re-upload behaves the same way.

**Done when:** a 500-row CSV round-trips into `sites` with the mapping
preserved, and an unmapped required column is refused with its name.

## 3. Provider templates with scheme holes

The Meraki plan and apply playbooks from the runner, wrapped as ShellMate
templates whose `{{ holes }}` are `scheme.yml`'s fields. The template
machinery exists (`ansible_library.save_template`, `render_template`);
what is new is that a deployment's template renders `scheme.yml`, not the
playbook — the playbooks read the scheme with `vars_files` and are
committed verbatim.

Blocked on the runner's skeletons for the *body*; the wrapper and the
scheme form can be built against the field list now.

**Done when:** rendering a scheme from the form produces YAML the
runner's `PUT /files` accepts (it parses on upload).

## 4. One commit, two PUTs

`ansible_git.commit_tree(files, message)` — the Trees API: one commit
carrying all four files under `runner/project/deployments/<slug>/`.
Then `ansible.upload_playbook` for the two playbooks and a new
`ansible.upload_file` for the two data files. `PROJECT_PREFIX =
"runner/project/"` is the one place the asymmetry lives.

Order: commit first, then PUT. A PUT that lands without its commit is a
drift; a commit without its PUT is merely not yet deployed, and the
deployment says so.

**Done when:** a test proves the four files reach both routes under their
two paths with identical bytes, and a failure in either half is reported
by name rather than as "failed".

## 5. Plan, then a gated apply

`POST /api/deployments/{id}/plan` starts `plan.yml` with credentials from
the environment's keys as envvars and the scope as extra_vars, records
the job id, and the UI polls it the way runs already do. When the job
ends, `GET /jobs/{id}/result` is read and rendered: counts as the
headline, the site table beneath, conflicts first.

`POST /api/deployments/{id}/apply` **refuses** without a `plan_job` whose
result has been fetched and acknowledged, and passes that job id through
so the outcome table can be shown against what was promised. The result
renderer is the one piece written against the runner's real output rather
than the contract text.

**Done when:** apply without a plan is a 409 naming the reason; a plan
result renders from a captured real payload; ids per site are stored on
the deployment.

## 6. The Deployments area in the Ansible view

A new `av-tab` beside Environments: list, create (provider -> template ->
data set -> scheme -> environment), the plan/apply flow, the outcome
table, and the git state (last commit, whether the runner matches it).

**Done when:** `python test_deployments_ui.py` passes and the UI tests
that exercise the Ansible view still pass.

## 7. Azure and AWS

The same wrapper over the runner's Azure (RG + VNet + subnets + NSG) and
AWS (VPC + subnets + SG) skeletons once they exist. No new machinery;
different scheme fields.

## 8. Close out

Retitle #594 to what it now is, close it against this run, rebuild the
executable, archive, DONE.

## Explicitly not in this run

- Scheduled applies — **#611**, blocked on an always-on host. A 45-minute
  apply that must be watched is fine from a desktop; one that fires at
  3 a.m. from a laptop lid is not.
- Inventory hosts from `aws_ec2`/`azure_rm` — a separate issue if VMs ever
  appear.
- Claiming real MX/MS serials end to end — the org has none; the steps are
  written and unproven until hardware exists, and the plan says so.
