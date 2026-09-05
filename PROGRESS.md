# Progress — the estate cluster

Goal: close #544 (change record), #543 (compliance check) and #547
(scheduled show collection).

Fourteen steps in PLAN.md, ordered so the two medium issues are complete
before the large one starts. Each step verifies, commits, pushes, and adds
an entry here.

## 2026-09-05 09:48 — step 1 done (#544)

`backend/change.py`. A change is keyed on the hostname and persisted to
`changes.json`, which is the entire design constraint and the issue's own
stated risk: a reload is frequently *the change being made*, and a reload
is exactly what destroys the session. A record on the session dict would
evaporate at the moment it became most valuable.

Three tests attack that directly — one ends a change with no session
anywhere in sight, one reloads the module to stand in for ShellMate
restarting, and one proves the file is the record rather than a cache.

Four decisions the tests pinned down:

- **Case-folded hostname matching.** A device answering `Core-SW-01#` on
  one login and `core-sw-01#` on the next is one device; prompt case
  follows the configured hostname, which people change. The record keeps
  the spelling it was given — matching loosely is not the same as
  rewriting what the device said.
- **One change per device**, refused with what is already open rather than
  queued. The answer to "somebody else is changing this switch" is a
  person talking to a person.
- **No baseline is a fact, not a refusal.** A device that will not give up
  its configuration is exactly the one somebody wants a record of working
  on, so the window opens and carries the reason.
- **`abandon` is not `end`.** One produces evidence and one says there is
  none to produce; a change opened on the wrong device should not leave a
  diff implying somebody did something there.

`commands_in_window` is written but not yet exercised — it needs the
history store, and step 2 drives it through the API.

`python test_change.py` — 38 passed, 0 failed.

## 2026-09-05 09:52 — step 2 done (#544)

`POST /api/sessions/{id}/change/start`, `/end` and `/abandon`, plus
`GET /api/changes`. `prune_stale` is called at startup rather than being
declared and never read.

The end record is deliberately the shape `drift.js showDiff` already
renders — hostname, diff, counts, the two snapshot ids — plus the commands
and any pending reload. Inventing a second shape would let the change
record and the drift view come to present the same diff differently.

The test that earns its place covers **the two kinds of nothing**, in all
four combinations, because both captures fail independently:

- both ends captured, nothing moved → comparable, zero changes
- captured at the start, device gone at the end → **not** comparable, and
  the reason carried. This is the ordinary shape of a change that reloaded
  the device, and reporting it as "no difference" would tell a change board
  the work had no effect
- nothing captured at the start → the window still opens, carrying why
- neither → not comparable, and said so

Three smaller decisions: a second change on a device is a **409** rather
than a 400, because the request is well formed and the state is the
problem; the window is closed **last**, after the record is assembled, so
a failure in between cannot lose the baseline id, which is the half that
cannot be recovered; and a session with no device name is refused, because
a change keyed on nothing could never be found from anywhere else.

Also: a broken alert tracker does not take the record with it. Tested.

`python test_change_api.py` — 33 passed, 0 failed. test_change 38.

## 2026-09-05 10:09 — step 3 done (#544)

The record is drawn by the **diff window**, not a panel of its own. A
change record is mostly a diff, and there is already a window that renders
one with hunks, a capture history, Explain, Copy all, Export and "Propose
the way back". A second window would be a second implementation of all of
that, and the two would come to disagree about the same hunks. So drift.js
grows one block for the parts a change has and drift does not: the note,
the ticket, the window, the commands, and anything still pending.

`config_push.offerRestore` is now exported so the way back is offered from
the baseline *this change* pinned, rather than from whatever the last push
left. One button, one implementation.

Three menu entries — Start, End, Abandon — gated on a cache of which
devices have a window open, because the menu is built synchronously and
the answer lives on the server. When the cache is behind, the server
refuses with a message naming what is open, which is why the 409 and 404
on those routes are worded as they are.

The browser test's centre of gravity is the case the whole feature exists
for: **a change that could not be measured must not read as a change that
did nothing.** A diff renders "0 lines added, 0 removed" for two identical
captures *and* for a device that went away before the second one, and
those are opposite facts. It asserts the warning appears, the summary
refuses to claim anything about the configuration, and the words "0 lines
added" do not appear at all. Three separate records are rendered —
measured, identical, unmeasurable — and an ordinary drift view is checked
to be untouched.

Two things caught: `shellmate:tab-activated` was an event I invented and
nothing dispatches (the real one is `shellmate:sessions-changed`, on
`window`), and `play_circle` is outside the committed font subset.

`python test_change_ui.py` — 20 passed, 0 failed. test_icons 4,
test_contrast 103.

## 2026-09-05 10:14 — step 4 done, and #544 is complete

A change across a group, through the scheduler's own four injected
callables — so a device with no session open is connected to headlessly
exactly as a nightly backup does, and the two paths cannot come to
disagree about which devices they can reach.

**One record per device, never one merged diff.** Eight switches' hunks in
a single diff loses which line belonged to which device, and that is the
first thing anybody reading a change record needs.

The interesting part is the taxonomy of not-quite-working, because a group
run has four outcomes and folding any two together loses something:

- **started** — bracketed, with or without a baseline
- **skipped** — a reason ShellMate can state up front: a serial console, no
  saved credentials, no device name to key on, or *a window somebody
  already opened by hand*. That last one is never overwritten: their
  baseline is evidence and taking a second would spend it.
- **failed** — a device that should have answered and did not. This is the
  one somebody has to go and look at, which is why it is not a skip.
- **unmeasurable** — closed, but with only one end captured. Counted
  separately from "changed" in the toast, because folding it in is exactly
  the misreading the whole feature exists to stop.

Two of my test expectations were wrong, not the code. A profile with a
name but no hostname is legitimately keyed on the name — the same fallback
the single-session path uses — and an unreachable device *should* be
reported as failed. Both corrected, and the unreachable case is now its
own test asserting no window is left claiming a baseline it never took.

`python test_change_api.py` — 67 passed, 0 failed. test_change 38,
test_change_ui 20. test_groups, test_scheduler and test_group_clone still
pass against the changed app.py and groups.js.

**#544 is complete.** Steps 5–8 are #543 next.

## 2026-09-05 10:19 — steps 5 and 6 done (#543)

`config_push.check(snapshot_text, lines, platform)` factored out of
`preview`. The classification *is* both features; only where the
configuration comes from differs — a live session for one device, a stored
snapshot for two hundred — and two copies of it would drift. `preview` now
delegates and test_config_push still passes unchanged.

`backend/compliance.py` and `POST /api/groups/{key}/compliance`. Four
things the tests pinned down, each of which sends somebody to the wrong
place if got wrong:

- **Three states, not two.** `compliant`, `missing`, `never-captured`.
  Folding the third into "not compliant" sends an engineer to fix a device
  that may be fine; folding it into "compliant" reports a device nobody
  has looked at as verified. An empty snapshot counts as never-captured
  for the same reason.
- **The age of the evidence is part of the verdict.** Compliant against a
  six-week-old capture is a statement about six weeks ago, so every row
  carries `age_days` and a `stale` flag.
- **A mixed group gets a block per platform.** Running the IOS AAA lines
  against a firewall reports every line missing, which reads as a badly
  misconfigured device when the truth is the check was asked the wrong
  question. A platform with no block gets `no-snippet`, not a verdict.
- **The limit travels with the result.** Section context is ignored, so
  `description uplink` under the wrong interface counts as present. The
  caveat is a field on the report rather than something the panel
  remembers — a caveat the panel owns is one a forwarded result loses.

"Unexpected lines" needed no new parameter: a must-not-have block is the
same call, and anything coming back `present` is a line that should not be
there.

`python test_compliance.py` — 30 passed. test_config_push 26, unchanged.

## 2026-09-05 10:23 — step 7 done (#543)

The compliance panel, the group menu entry, and "Open and fix".

The browser test exists because "the verdict is right in the JSON" and
"the verdict was communicated" are different claims. Its centre is
**never-captured looking like neither of its neighbours** — painted green
it reports a device nobody has looked at as verified, painted amber it
sends an engineer to fix a device that may be perfectly configured. The
test reads the computed border colour and asserts it differs from both.

Two more properties it holds:

- The caveat sits **above** the table, checked with
  `compareDocumentPosition` rather than by looking. A limit found after
  the verdicts is a limit read after a conclusion has been drawn.
- The age of the evidence is on the verdict line, and a 45-day-old
  "has every line" says "this verdict is that old too".

"Open and fix" loads the missing lines into the push editor rather than
sending them — config_push keeps its own preview and its dangerous-command
guardrail, and a compliance report is evidence, not permission.

Two mistakes, both mine and both the same shape as earlier ones:
`window.activateTab` does not exist (it is `switchToTabBySessionId`), and
my grep for it matched only my own file. And an assertion failed on
casing because `inner_text` returns *rendered* text — a CSS
`text-transform: uppercase` is invisible in the source and not to the test.

`python test_compliance_ui.py` — 18 passed. test_compliance 30,
test_contrast 103, test_icons 4.

## 2026-09-05 10:33 — step 8 done, and #543 is complete

Compliance findings now reach the morning digest, and the check repeats
itself after every scheduled backup for any group that asked for it —
which is the moment its evidence is freshest, and the difference between a
standard verified every night and one verified the afternoon somebody
happened to click.

Four rules the tests hold:

- **Only the two states somebody acts on** reach the digest: devices
  missing lines, and devices there is no capture to check. "46 compliant"
  is not news, and a digest that leads with it is one people skim — at
  which point the morning something *is* missing looks like every other.
- **Named separately from the backup's own numbers.** "The capture failed"
  and "the capture worked and the standard is not there" are two different
  mornings' work.
- **A stale result is not tonight's news.** A finding from a click three
  weeks ago is only attached if it ran with this backup; the devices may
  have been fixed since, and date-stamping old news as new is worse than
  saying nothing.
- **A deleted snippet is said out loud.** A check that quietly stops
  running reports compliance by omission, which is the worst available way
  for this to fail.

**The bug worth recording is one I walked straight into.** `update_group`
handles the keys it knows and silently drops the rest, and `list_groups`
builds the public view field by field — so `compliance_last` was written,
accepted, and never persisted, and then not readable either. That is
exactly the silently-dropped-field shape I have been calling out since the
Ansible build request, and it cost two rounds of "why is this None". Both
places now name the two keys, with a comment saying why they have to.

`python test_scheduler.py` — 45 passed (was 30). test_groups,
test_compliance, test_group_clone, test_config_push and test_change_api
all still pass.

**#543 is complete.** Steps 9–13 are #547, the large one.

