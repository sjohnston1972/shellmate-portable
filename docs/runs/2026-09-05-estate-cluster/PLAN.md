# Run plan — the estate cluster

Three issues: #544 (change record), #543 (compliance check against a golden
snippet), #547 (scheduled show collection).

Ordered by what each needs from the one before, and with the largest last.
#544 uses `report.change_report`, which the previous run built and which
already distinguishes "the configuration is identical" from "no snapshot
was captured to compare against". #543 needs a `check()` factored out of
`config_push.preview`. #547 is marked *large* on the issue and is the one
that stops if this run runs out of night — so it is last, and #544 and #543
are complete before it starts.

Every step: verify, commit, push, append a timestamped entry to PROGRESS.md.

---

## #544 — the change record

### 1. Where a change lives

A change must survive the session it started in — the issue's own stated
risk. A device reloading mid-change is the ordinary case, not the edge one,
and a record keyed by session id evaporates exactly then. Keyed on hostname,
persisted, with `started_at`, `before_id` and the note.

**Done:** `python test_change.py` passes, including a case that starts a
change, drops the session, and still ends the change afterwards.

### 2. Start and end over the API

`POST /api/sessions/{id}/change/start` captures, pins the baseline with the
note, marks the start. `/change/end` captures, diffs against the start,
gathers the commands typed in the window from history, and reports any
reload still pending.

**Done:** `test_change.py` drives both through TestClient; ending a change
that was never started is a 404, not an empty record.

### 3. The record on screen

Opens the existing diff window — the end report is a dict `drift.js
showDiff` already understands, plus a `commands` list — with Send to Jira,
Export report and Propose the way back.

**Done:** the three actions are wired, `test_icons.py` and
`test_contrast.py` pass.

### 4. A change across a group

"Start a change on Glasgow/core" captures every member through the
scheduler's headless harness, so a change spanning eight switches is one
record.

**Done:** a test starts and ends a group change against fake sessions and
gets one record per device.

---

## #543 — the compliance check

### 5. `check()` out of `preview()`

`config_push.preview` already classifies every line as add, present or
remove against the latest snapshot without sending anything. That
classification is the whole check; it is currently welded to one device and
one editor.

**Done:** `test_config_push.py` still passes, and a new test calls `check()`
directly with a snapshot and a snippet.

### 6. The group question

`POST /api/groups/{key}/compliance {snippet_id}` over `profiles_tagged`,
reading `store.latest_snapshot` per device. A device with no snapshot is
reported as never captured — which is a different answer from compliant,
and the one that must not be silently rounded to it.

**Done:** a test covers a group with a compliant device, a non-compliant
one, and one that has never been captured, and asserts the three read
differently.

### 7. The table, and its limit stated

Device, missing lines, unexpected lines, snapshot age. "Open and fix" opens
the session and the push editor prefilled with the missing lines.

The limit goes on the screen, not only in a comment: line-set matching
ignores section context, so `description uplink` under the wrong interface
counts as present. A check that overstates what it verified is worse than
no check.

**Done:** the panel renders, says what it cannot see, and `test_icons.py`
passes.

### 8. Into the digest

The result stores on the group like `backup_last` and routes with the
nightly backup digest.

**Done:** the digest carries the compliance result; `test_scheduler.py` and
the digest test pass.

---

## #547 — scheduled show collection

### 9. What is eligible

Only snippets not marked `writes`, checked against the dangerous list the
way `config_push._dangerous` does. A scheduled job that can type is a
different feature with a different blast radius.

**Done:** a test proves a `writes` snippet and a snippet containing a
dangerous command are both refused, by two separate routes.

### 10. Collecting on the timer

`scheduler.run_group` gains a `collect(session, snippet)` step after
`capture`, on the second channel. Outputs are stored as commands under a
synthetic session with `connection_type = "collection"`.

**Done:** a test runs a group collection against a fake harness and finds
the output in the store, attributed to the right device.

### 11. Asking for it

The schedule dialog gains "also collect": one or more read-only snippets.

**Done:** the control saves and reloads; `test_icons.py` passes.

### 12. Finding it again

A Collections filter in History, and "compare with previous run" diffing
the same command on the same device across two runs.

**Done:** a test stores two runs and asserts the diff names only what
changed between them.

### 13. Bounds

`history.max_output_chars`, retention, and a per-group collection age all
apply. Unbounded growth is the stated risk and it is a real one: a nightly
`show interfaces status` across two hundred devices is a lot of rows.

**Done:** a test proves the age bound prunes, and the existing retention
test still passes.

---

## 14. Close the run

Regenerate the tree (`python tools/claude_tree.py --write`), run the whole
suite, archive, write DONE.

**Done:** `python tools/run_tests.py` reports every file passing.

---

## Not in this run, and why

- **The executable is still not rebuilt.** It cannot be while ShellMate is
  running. It is the first line of DONE for the third time; it needs a
  minute of Steven's, not a step here.
- **#543's anchored mode** — matching a parent line plus its children — is
  named on the issue as a follow-on. Step 7 states the limit instead.
- **#546 (NetBox) and #548 (group push)** are in the same cluster and are
  not in this batch. Three issues, one of them large, is already the size
  of a night.
- **If the run runs out**, it stops inside #547 and says which step. #544
  and #543 are ordered first so they are whole either way.
