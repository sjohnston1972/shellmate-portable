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

