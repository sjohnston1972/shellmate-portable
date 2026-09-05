# Progress — the install, and the estate's loose ends

**Goal:** finish the six things that can be finished without a decision from
Steven — #585 (close with a correction), #568, #564, #539, #563, #547 — and
leave the executable current at the end.

Succeeds the AI-cluster run, archived in `docs/runs/2026-09-05-ai-cluster/`.

## 2026-09-05 16:57 — run opened, executable rebuilt

`dist/ShellMate-Portable.exe` at `72ba109`, 36,959,529 bytes. It now carries
#529 and #561 and the redaction fix, which the previous build did not.

107 of 107 test files pass on a clean run.

## 2026-09-05 17:10 — #585 closed with a correction, #568 done

**#585** closed. Not work — the Ansible integration shipped — but not
closed silently either: its body stated two things as fact, read from the
service's own source, that are no longer true. It says the runner uses
mutual TLS *not* a token (it is an optional bearer token, with client
certificates supported as well), and that there is no endpoint to upload a
playbook (#605 added `PUT /api/v1/playbooks/{name}`, probed through the
runner's own OpenAPI document rather than assumed). An issue closed with a
wrong premise standing is a wrong premise somebody quotes back in six
months.

**#568** — crash and startup-failure reports through the relay.

`backend/crash.py`, with the hook installed from `run.py` before anything
that can fault. Threads included, which matters more here than usual: the
session read loops, the scheduler and the store writer all run on them, and
an exception on one of those disappeared into a log line at best.

Three things it deliberately does not do. It never sends anything — the
file is written automatically, sending is a decision taken with the whole
text on screen, and `feedback.report_crashes` governs whether you are
*asked*. It never includes the scrollback: the log says what ShellMate did,
the buffer says what the device said, and only the first is diagnostic. And
it never raises — it runs inside an exception handler, and a crash reporter
that crashes replaces a diagnosable fault with an undiagnosable one.

Redaction happens before the file is written rather than before it is sent,
because a file on disk can be copied out by hand. Verified with a password
in the exception text.

The relay grew a `crash` kind, labelled `crash` as well as `bug` — a report
that arrives with a traceback and no reproduction wants different triage.

`python test_crash.py` — 41 passed. Guards: feedback 21, advanced 384,
startup 150, support 78, outbound 117, diagnostics 64.

## 2026-09-05 17:22 — #564 done

The first-run card and the portable chip.

`update.js announceIfNew` has had a branch meaning exactly "fresh install"
since the What's New modal was written, and it only recorded the version.
Two decisions were being taken silently on that same run.

The one worth the issue is **where saved passwords live**. It was decided
implicitly by whichever write happened first, and the consequence — a vault
that will not open on another machine — appears weeks later with nothing to
connect it back to a decision nobody made. The card states the trade-off in
full, including that a lost master password is not recoverable, and that
only the saved passwords go with it.

The chip has three states rather than two. "Portable" and "From source" are
both intentional; a read-only application folder is neither, and somebody
carrying a stick that will turn out to be empty should be told so before
they carry it. That case names the folder the data actually went to and what
to do about it.

Two integration traps avoided by reading rather than assuming: the AI toggle
goes through `toggleAiPanel` — which re-reads settings afterwards, because
settings.js keeps its own copy for the form and a stale one there undoes the
change on the next Save — and it does nothing when the value already
matches, since that function toggles rather than sets. I had first written
both the wrong way.

`python test_firstrun.py` — 29 passed. Guards: contrast 103, icons 4,
tooltips 8, accessibility 20, settings 49, vault 96, startup 150.

## 2026-09-05 17:40 — #539 finished

The webhook half. The in-app digest shipped earlier; this is the other end
of the same issue, and the reason it stayed open.

`backend/backup_webhook.py`, fired from `run_now` after the compliance
re-check — after, because compliance findings are attached to the group
afterwards and a message sent earlier would disagree with the panel for
exactly the runs where the disagreement mattered. It reads the digest rather
than being handed the run's result, so the numbers it posts are the numbers
on the screen.

Silence is kept as the feature: a clean night sends nothing at all, and the
Send-it-now button says so in those words rather than reporting a failure —
a test that called silence a failure would have somebody chasing a webhook
that works.

The URL is a credential that looks like a location, so it is diverted into
the vault like the Ansible token, masked for the panel, and never written to
settings.json. Verified by reading the file back.

Diffs are off by default and redacted and capped when on, and they are read
from stored snapshots: `drift_report` opens a channel to the device, and by
the time this runs the session it would have used has been closed.

**A real bug the test caught.** `url()` had `from backend import vault` and
then `vault.get(...)` — the module, not the instance. The AttributeError was
swallowed by the locked-vault guard, so the webhook would have silently
never fired. Found only because the test posts to an actual HTTP server and
asserts something arrived.

**One departure from the issue.** It asks for a ShellMate link in the body.
There is nothing honest to put there by default — ShellMate binds to
loopback on a port chosen at startup, so a localhost link in a team channel
works for one person and reads as broken to everyone else. It is
`backups.webhook_link`, empty unless a deployment really is reachable at a
fixed address.

`python test_backup_webhook.py` — 40 passed, against a live receiver.
Guards: settings 49, scheduler 45, advanced 392, support 78, security 58,
backup_digest 23, vault 96, compliance 30.

## 2026-09-05 18:05 — the first-run card blocked the UI tests, and would have blocked a user

The full suite hung on `test_phase2.py` (normally 23s). The first-run card
from #564 was a full-screen overlay, and on a fresh temp data folder — which
is what every UI test starts with — it opened and intercepted every click
until dismissed. Playwright reported it in those words: `firstrun-overlay
intercepts pointer events`.

That contradicts the card's own stated rule, "nothing here blocks reaching a
device". A scrim is exactly that. It now lives in the home screen's column,
after the heading, with no backdrop: there when you come back to the
dashboard, out of the way the moment a tab opens. The UI tests found it
before a user did, which is what they are for.

`test_phase2.py` — 97 passed again. `test_firstrun.py` — 30.

## 2026-09-05 18:30 — #563 done

Export, import, and moving the data folder. `backend/setup_bundle.py`,
routes under `/api/setup/`, a Backup and transfer section in Settings, and
one override check in `paths.data_dir()`.

The rule everything follows from: nothing secret is in a bundle. Tested by
putting a secret in every place one could be — provider key, Ansible token,
webhook URL, the vault, the plaintext credential file — and reading the zip
back. The credential sets travel as names and usernames so a colleague's
profiles point at something meaningful; the passwords are theirs to fill in.

Import previews first, always, with counts and how many you already have.
Merge is the default for lists and incoming loses ties, because somebody
importing a colleague's setup has their own corrections in these files.
Profiles merge on `identity()`, or #73's duplicates come straight back.
Refused while sessions are open, mirroring `updater.blockers`.

The move copies, points, and never deletes — the original stays exactly
where it was, and the response says so every time. The override is
`data-dir.txt` beside the exe or `SHELLMATE_DATA_DIR`, resolved in
`paths.data_dir()` and nowhere else; a pointer to an unusable folder is
ignored with a warning rather than refusing to start.

`python test_setup_bundle.py` — 54 passed, a real round trip on a seeded
folder. Guards: startup 153, settings 49, vault 96, security 58, support 78,
profiles 198, diagnostics 64, updater 32, env_example 8, tooltips, contrast,
accessibility.

## 2026-09-05 19:20 — #547 done

Scheduled show collection. `backend/collection.py`, a `collect` step in
`run_group` after the capture on the same login, a `kind` filter through
`store.search` and `list_sessions`, three routes, the schedule dialog's
"Also collect" checkboxes, and Compare-with-the-previous-run in History.

Read-only, checked twice: a snippet marked `writes`, or with a command on
the platform's dangerous list, is listed with the reason rather than left
out, refused by `scheduler.normalise` on the way in, and refused again at
the moment it would run — the group file is one people are told they may
edit. A scheduled overnight job is the worst place for a command that
changes something.

One synthetic History session per device per run, `connection_type =
"collection"`. Compared by command text, not position: a snippet edited
between runs shifts positions. Bounded per command by
`history.max_output_chars` and per device by the new
`history.collection_keep` (30 — a month of nights), and the sweep is
exercised at its floor in the test.

A collection that fails does not fail the backup; `collected` and
`collect_failed` are reported beside `ok` and `failed`, not folded in.

The full suite before this showed one flake: `test_ansible_env_keys.py`
failed 3 of 23 under load — an environment created and not yet listed —
and passed 23/23 alone. A file this batch did not touch.

`python test_collection.py` — 48 passed, against a fake device on a fake
second channel that speaks `_read_until_idle`'s protocol. Guards:
scheduler 45, history 63, history_range 7, store_writer 11, advanced 395,
groups 94, snippets 23, tooltips 8, contrast 103, backup_digest 23,
backup_webhook 40.

## 2026-09-05 20:05 — the one flake, fixed rather than waved away

`test_ansible_env_keys.py` failed under the full suite twice this evening
and passed alone both times — different assertions each time, which is what
a race looks like, not a bug. The dialog closes the instant it is accepted;
the save is a fetch that lands afterwards; the list re-renders after that.
A fixed 200ms covered it on an idle machine and did not under a suite that
is now seven files longer.

The five post-accept sleeps are now `_settle()`: wait for the list to show
(or stop showing) the expected text. 23/23 run concurrently with two other
Playwright suites as load. Test-only; the executable at `654942c` carries
every non-test change in the batch.

Final suite: 111 of 112 before the fix, the one being this file.
