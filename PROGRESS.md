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
