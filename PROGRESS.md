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

- **2026-09-05 03:05** — Steps 2 and 3 done (#608). The Inventory area is
  where a list gets built: tick hosts in the estate table and save them as
  a named list, or upload a CSV or a plain list of addresses. The generated
  INI block is gone — it answered a question the two tables above it had
  already answered, and `grep -c "Generated inventory" frontend/` is 0. A
  custom inventory is a target in the Run dialog beside a group, the
  runner's own inventory and specific hosts, and an environment can pin one.
  Five worked examples ship with it. 29 checks in
  `test_ansible_custom_inv.py`, 65 in `test_ansible_inventories.py`.

  The examples earned their place the moment they were written. The plain
  list carries its own comment — `# the distribution layer, 12 March` — and
  the comma in that comment decided the delimiter, because comments were
  stripped *after* the delimiter was chosen rather than before. So a plain
  list with a note in it was read as a one-column table and refused for
  having no host column to nominate: a refusal about the mapping, for a
  problem in the comment. Fixed, and the shape is now asserted through the
  same `preview`/`rows_from` a real upload goes through. An example that
  only parses because something special-cased it teaches a shape the parser
  refuses, and the person who followed it cannot tell which was wrong.

  One deliberate widening beyond the plan: `inventory_from_estate` now puts
  `shellmate_platform` in the hostvars. A curated list has to store what
  ShellMate knew, and without it the browser would have had to reverse
  `cisco.ios.ios` back to `ios` — a second copy of a map that already
  exists in the backend, in the place least able to keep up with it.

- **2026-09-05 03:20** — Step 5 done (#586). Not a fix; the cause is still
  unknown. `tools/run_tests.py` now streams each file's output *and* keeps
  it, writing a failing file's to `.test-failures/` and repeating the last
  25 lines in the summary where a CI log will carry them. Asserted in
  `test_runner_evidence.py`: a deliberately failing probe leaves a named
  log carrying its own output, the timestamp, the exit code and the Python
  version; a passing one leaves nothing.

  Worth recording what this run learnt about the intermittent itself. It
  failed here as `test_ansible_env_keys.py`, which passed immediately on
  its own — and the earlier occurrences were `test_sftp.py`. Two different
  files now, on two different machines, which rules out both explanations
  reached for first: it is not one bad test, and it is not parallel load,
  because the runner has always been sequential. I asserted that
  parallel-load explanation twice before being corrected, which is the
  actual lesson: a verdict delivered where its precondition was never
  established. The runner being sequential was checkable in the file the
  whole time.

- **2026-09-05 03:25** — A test of my own from the last run was wrong.
  `test_scheduler.py` read the wall clock and asserted that twelve hours
  on a nightly 02:00 schedule owes no missed run. That is true between
  14:00 and 02:00 and false the rest of the day; it passed every time it
  was run until it was run at half past two in the morning. Pinned to a
  fixed date, chosen away from the March clock change. A test whose answer
  depends on when it is run is not testing the thing it names.

