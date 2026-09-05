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

- **2026-09-05 04:40** — Step 4 done (#609). A playbook can now be committed
  to GitHub on every save. `backend/ansible_git.py`, its routes, the
  Settings section and the editor's note. 29 checks in
  `test_ansible_github.py`, against a stand-in GitHub that records what was
  *sent* — because for the visibility question that is the only thing that
  proves anything: a response saying "private" says nothing if the request
  asked for public.

  Deliberately no `GITHUB_TOKEN` environment fallback. The obvious
  convenience is the trap: that name exists in a great many development
  environments, this repository's own included, and picking it up would
  commit a user's estate under a developer's identity with nothing on
  screen saying so. Vault only.

  Two buttons rather than one with a fallback, because creating a
  repository and pushing to one need very different tokens; somebody who
  only wants the smaller permission should be able to hand over only the
  smaller token and have it work.

  And a real bug found on the way, in the code this copied. The Ansible
  runner token had no mask guard on the backend — the frontend filters the
  "••••••••" placeholder, but the API is scriptable and settings.json is a
  file people are told to edit, so a POST echoing back what a GET returned
  would have stored eight bullets as the runner token. The providers path
  had that guard; this path never did. Both section secrets now go through
  one helper that has it.

- **2026-09-05 05:35** — Step 6 done (#527). `backend/ssh_config.py`, its
  routes, an import door on the home view and a fill in the connection
  dialog. 34 checks in `test_ssh_config.py`.

  One deliberate departure from the issue's sketch, which put the fill
  inside `ssh_handler.connect()`. Nothing above the transport has a channel
  to report on at that point, so a fill applied there would be invisible at
  exactly the moment it changed where the session was going — and "the
  address you typed is not the address this dialled" is not a thing to
  discover from a device answering wrongly. In the dialog it is visible by
  construction, named field by field, and can be typed over before anything
  is sent.

  A paramiko 5.0.0 bug on the way: `SSHConfig.get_hostnames()` raises
  `KeyError: 'host'` on any file containing a `Match` block, because it
  walks every parsed entry expecting a key a Match entry does not have.
  Match blocks are ordinary, so that is not an edge case — it is "the
  listing fails for exactly the people with the most in their config". The
  `Host` lines are read from the file directly now; `lookup()` is
  unaffected and still supplies every value.

  And a test of mine was wrong before paramiko was: the sample config had
  `Host *` at the top and then asserted the specific stanza won. OpenSSH
  keeps the *first* value it obtains, which is why real files put the
  defaults block last. paramiko was right and the assertion was not.

- **2026-09-05 05:45** — A wrong turn worth recording, because the
  temptation was to ship it quietly.

  With the failing output now kept (#586), the intermittent showed up twice
  at the identical line — `test_ansible_templates.py:146`, waiting for a
  refusal banner. Reading the area turned up a plausible cause: the example
  seeding is two round trips and re-renders the area when it lands, with no
  guard against the editor being open by then, so a click on its way to the
  Create button would land on a node that had left the document.

  It is not the cause. Reverting the guard and running the file four times
  did not reproduce the failure; slowing the seed response to a second to
  widen the window did not either — it perturbed a different check instead.
  So the diagnosis is unsupported, and saying otherwise would have made the
  next person believe #586 was solved.

  The guard is kept, because a re-render over an open form is wrong on its
  own terms, and the comment says exactly that rather than claiming a cause.
  The speculative assertion that went with it is gone: it passed with the
  bug restored, so it asserted nothing, and a check that cannot fail is
  worse than no check.

  What is now known about the intermittent: the location, and that it is not
  this. That is still more than the name it used to arrive as.

- **2026-09-05 06:50** — Step 7 done (#530). Notes on the session:
  Ctrl+Shift+N or the tab menu, a Timestamp button, no Save button because
  a note that needs saving is a note that gets lost. Kept on the sessions
  row, indexed by FTS with an update trigger, shown in the session's own
  replay and as marked note hits in History — never mixed into the
  transcript, because a sentence in a record of a change window that
  nobody typed at a device is the one thing it must not contain. 30 checks
  in `test_notes.py`, including that a note handed to the prompt builder on
  the session summary does not travel in the prompt.

  The schema needed a real migration. `CREATE TABLE IF NOT EXISTS` does
  nothing at all to a table that already exists, so a column added to the
  definition appears only for people installing fresh — everybody else
  meets it as an OperationalError at the point of use, with their history
  apparently broken. `_add_missing_columns` is additive, idempotent, and
  checked against the table rather than a version number, so a database
  restored from a backup ends up right too.

- **2026-09-05 07:05** — **The intermittent (#586) has a cause, proved.**

  `test_sftp.py` stands up five fake SSH servers, each with a freshly
  generated host key, each on an ephemeral port — and the OS recycles
  ephemeral ports. When a later server lands on a port an earlier one
  used, ShellMate compares the new key against the one it remembered and
  refuses: "the host key for 127.0.0.1 has changed". Nothing was wrong
  with the code under test. The fixture was two different machines
  claiming to be one host.

  That explains every property the failure had. About one run in three,
  because a collision needs the port pool to have wrapped. Never when the
  file ran alone, because forty other files are what cycles it. And
  order-dependent, which is what made it look like a race.

  Proved rather than argued: storing a key for a port, then verifying a
  different key for the same port, reproduces the exact error message from
  the kept log; forgetting it first accepts. The fixture now forgets the
  key for the port it just took — through `forget_host`, which already
  existed for the Keys panel.

  Which is the other lesson. I wrote a second function to do it, and only
  noticed the duplicate because a passing test in the same file was called
  "and Forget removes it". A second implementation of something this
  project already had would have drifted, silently, in the direction of
  whichever one got maintained.

  Two things this does not claim. The templates and env-keys occurrences
  are not explained by it — they use no SSH — so either there is a second
  cause or those were something else. And it fixes a fixture, not the
  application: the behaviour it was tripping over is correct and stays.

