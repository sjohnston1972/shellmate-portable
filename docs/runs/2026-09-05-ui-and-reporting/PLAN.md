# Run plan — the UI and reporting cluster

Four issues: #540 (export a session, a diff or a change; Jira out of .env),
#574 (export a playback), #576 (log date picker and rich search),
#569 (high-contrast theme and terminal scheme).

Ordered so the shared machinery exists before the two issues that need it.
#540 and #574 both write into `ShellMate-Data/reports/`, both redact through
`outbound.redact_text`, and both hang an "Export…" off the replay header —
so #540 goes first and #574 reuses it rather than growing a second copy.

Every step: commit, push, append a timestamped line to PROGRESS.md.

---

## 1. The reports folder and `backend/report.py`

`paths.reports_dir()` beside the other data locations. `backend/report.py`
with `session_markdown`, `diff_markdown`, `change_markdown` — metadata, the
command list with outputs, the diff, an optional AI summary. Every string
through `outbound.redact_text`; that is the #320 and #463 lesson and it is
not optional.

**Done:** `python test_report.py` passes, and one of its cases asserts a
planted secret does not survive into the Markdown.

## 2. The same three as self-contained HTML

One page, no external references, for print-to-PDF. PDF is deliberately not
generated in-process.

**Done:** `test_report.py` asserts that device output containing `<script>`
and `&` arrives escaped, and that the HTML carries no `http://` or `https://`
reference.

## 3. `POST /api/reports` and its reveal

Writes `ShellMate-Data/reports/<device>-<stamp>.md|.html` and reveals it —
the `support.write_bundle` + `desktop.reveal` pattern, which is already the
shape people know.

**Done:** `test_report.py` drives both routes through TestClient and finds
the file on disk.

## 4. "Export…" where the three things actually are

The replay header, the diff window, and the Jira modal as "Save as a file
instead".

**Done:** all three call sites present, and `python test_icons.py` passes —
any new icon name must be in the committed font subset or it renders as
plain text with no error.

## 5. Jira settings out of `.env`

URL, email, token and project into Settings under a "Ticketing" section,
the token into the vault via `SECRET_FIELDS`. `jira_client` resolves through
`get_effective()`, so `.env` keeps working for anyone already using it.

**Done:** a test asserts the token round-trips through the vault and never
appears in settings.json, and `python test_jira.py` still passes.

## 6. #574 — the playback as a self-contained HTML replay

The vendored xterm.js and the session's commands embedded, the same
controls, redacted.

**Done:** a test asserts the produced file contains the player, the commands,
and no reference to anything off the page.

## 7. #574 — the transcript alternative, and both wired to Export

Markdown or plain text, from the same session record.

**Done:** `test_report.py` covers it; the replay header offers both.

## 8. #576 — `GET /api/logs/search`

`?q=&since=&until=`, case toggle, regex toggle, whole-word. A bounded read
per file, so a log folder nobody has pruned cannot stall the request.

**Done:** a test proves the bound holds against an oversized file, and that
an invalid regex returns a message rather than a 500.

## 9. #576 — the date range picker

Filters the file list by the file's date.

**Done:** the control is in the Logs panel and `test_icons.py` passes.

## 10. #576 — the search box, hit counts, and jumping to matches

Matching files listed with a hit count; the viewer jumps to the matches with
the #520 highlighting treatment.

**Done:** the panel renders hits from the endpoint; a test covers the
endpoint's shape.

## 11. #569 — the high-contrast token set

A fourth value of `interface.theme`: pure black and white surfaces, 2 px
focus rings, no translucency. Honoured automatically under
`prefers-contrast: more` when the theme is system. The `--overlay` surfaces
must be included or the floating panels regress to the bug #429 fixed.

**Done:** `python test_contrast.py` asserts AAA for the new set and its
existing assertions still pass in dark and light.

## 12. #569 — the High Contrast terminal scheme

A built-in scheme measuring at least 7 through `schemes.contrast()`.

**Done:** a test asserts `schemes.contrast()` of the built-in is >= 7.

## 13. Close the run

Regenerate the tree in CLAUDE.md (`python tools/claude_tree.py --write`),
run the whole suite, archive and write DONE.

**Done:** `python tools/run_tests.py` reports every file passing.

---

## Not in this run, and why

- **The executable is not rebuilt.** It cannot be while ShellMate is
  running, and killing it drops live device sessions. It is a note in DONE,
  as it was last run.
- **ServiceNow** as a second ticketing target. #540 says it is the same
  shape and can follow; it is not this batch.
- **The webhook half of #539.** Still waiting on the policy decision about
  whether diff text may leave the machine.
