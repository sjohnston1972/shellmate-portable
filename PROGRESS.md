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

- **2026-09-05 01:05** — Step 1 done. `backend/ansible_inventories.py` with
  curated and uploaded lists, plus the routes. 46 checks in
  `test_ansible_inventories.py`, 73 of 73 test files passing.

  Two things worth carrying forward. The header heuristic first accepted any
  bare word as evidence of a data row, so an `ansible_host` CSV had its
  header read as a device — narrowed to IP addresses and dotted names only,
  which leaves bare-hostname files genuinely ambiguous, so `headed` is
  reported and can be overruled. Same principle as the columns: say what was
  concluded, let the caller correct it, never guess quietly.

  And a column that exists but is empty in every row is refused rather than
  producing an empty inventory — that is the shape of somebody picking the
  wrong column, and it is the same "confident nothing" defect found in the
  estate inventory and in the runner's API this week.
