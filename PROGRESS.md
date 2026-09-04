# Progress

Goal: finish the Ansible issues, then work through the rest of the open
list, committing and pushing as each closes. PLAN.md holds the steps.

Machine: SJLAP. The Ansible runner container is local here, reached on
`https://127.0.0.1:8081`.

---

- **2026-09-05 00:20** — Run opened. Before it: session moved from SJGAMING
  and verified; paramiko 5 startup failure fixed (`DSSKey` removed
  upstream, ShellMate would not import at all); `test_advanced` made
  version-robust; #606, #607, #590, #591, #612 closed; a refused connection
  no longer reported as "certificate not trusted"; a mistyped group no
  longer returns an empty inventory that reads as an empty group. 72 of 72
  test files passing at `a383261`+.
