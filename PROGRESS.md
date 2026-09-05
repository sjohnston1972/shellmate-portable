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
