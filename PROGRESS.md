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
