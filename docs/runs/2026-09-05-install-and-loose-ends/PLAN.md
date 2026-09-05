# Run plan — the install, and the estate's loose ends

Six items: **#585** (close with a correction), **#568**, **#564**, **#539**,
**#563**, **#547**.

Succeeds the AI-cluster run, archived in `docs/runs/2026-09-05-ai-cluster/`.
All twelve of its issues closed: #560, #553, #551, #554, #556, #557, #559,
#558, #555, #552, #529, #561. No DONE was written for it — it is succeeded
immediately rather than handed over, and a DONE created and deleted in the
same minute is a signal nobody can read. The same precedent as the estate
run before it.

## Why these six

Everything here is finishable **without a decision from Steven**. The four
issues that need one — #518 (a certificate to buy), #449 (a commercial
model), #570 (portal scope), #611/#594 (the runner's CI shape, and which
cloud integrations exist) — stay on the open list untouched rather than
being guessed at overnight.

The theme is the install itself. Four of the six are about what happens
before anyone has connected to anything: the first run, a crash, moving to
another machine. The application has grown a long way past the point where
"copy the folder by hand" is an acceptable answer to "how do I take this to
my laptop".

## How this run commits

One issue, one close, one push. A step that finishes an issue closes it on
GitHub with what shipped and why, then commits and pushes. Steps that are
half of an issue commit and push without closing.

The executable is rebuilt **at the end**, once, carrying everything. It was
rebuilt at the start of this run too, so there is a current artifact either
way if the run stops early.

## Ordering

Largest last, as before. #547 is the only large one and it is the one most
likely to be cut; the five before it stand on their own.

---

## #585 — close the Ansible issue, with a correction

Not work. The integration shipped: eight backend modules, thirteen frontend,
sixteen sections of manual. The issue is still open because nobody closed it.

It must not be closed silently, because **its body states two things as fact
that are no longer true**, and it presents them as read from the service's
own source:

- "It authenticates with **mutual TLS**, not a token." The implementation
  uses an optional bearer token (`ANSIBLE_RUNNER_TOKEN`, vault-backed), with
  client certificates supported *as well* rather than instead.
- "**There is no endpoint that uploads a playbook.**" #605 added
  `PUT /api/v1/playbooks/{name}`. `supports_upload()` probes the runner's
  own OpenAPI document rather than assuming, and the SSH copy remains as the
  fallback for a container that predates it.

An issue closed with a wrong premise standing is a wrong premise somebody
quotes back in six months.

**Done when:** #585 is closed with a comment naming both corrections and
what actually shipped.

---

## #568 — crash and startup-failure reports through the relay

**Size: small.** The relay already exists (#370); nothing here builds one.

1. `backend/crash.py`: an unhandled-exception hook, `threading.excepthook`
   included, writing `crash-<stamp>.json` to the data dir — traceback, the
   About section, the last 50 log lines. Redacted through
   `outbound.redact_text`, capped like `MAX_DESCRIPTION`, and **never the
   scrollback**. A traceback embeds hostnames in its exception text, which
   is exactly why it goes through the same door everything else does.
2. `run.py _fatal` writes the same file *before* the message box. A startup
   failure is the case with no log the user can be asked to read, so it is
   the one that most needs this.
3. On the next launch: a toast, "ShellMate hit a fault last time. Review and
   send?", opening the feedback panel pre-filled with kind `crash` and a
   preview of exactly what will leave. `feedback.report_crashes` defaults to
   never prompting automatically — nothing about a crash justifies sending
   something the user has not read.
4. `kind` widens from bug/feature to bug/feature/crash, relay included.

**Done when:** `python test_crash.py` passes, and a deliberately raised
exception in a thread produces a redacted file with no hostname in it.

---

## #564 — the first-run card and a portable-mode chip

**Size: small.** `update.js announceIfNew` already has the fresh-install
branch; today it only records the version.

One dismissible card, **not a tour**. Four things, because they are the four
the application already knows the answer to and asks nobody:

- theme;
- **where saved passwords live** — Windows account or a master password.
  This is the decision that costs people their vault when they change
  machines, and it is currently taken implicitly on first write;
- whether to turn the AI assistant on (off by default, and the reason is
  already written down in `settings_store.py`);
- "your data lives at …, and travels with the executable" — or the warning
  chip when `using_fallback` is true and it does not.

Plus a permanent status-bar chip, Portable or Local profile, that opens
Diagnostics.

**Done when:** `python test_firstrun.py` passes, the card renders once and
never again, and both themes clear AA (`test_contrast.py`).

---

## #539 — the webhook half

**Size: small.** The in-app digest shipped; this is the other half of the
issue and the reason it is still open.

- A webhook URL in Settings, **vault-backed** — it is a bearer secret in a
  URL — and masked in the support bundle.
- A generic JSON body: device, group, counts, a ShellMate link. Plus one
  card format. Teams' incoming-webhook shape changed under Workflows, so
  generic JSON is the thing that keeps working and the card is the
  convenience.
- **Never the diff text** unless a separate setting says so, and then only
  redacted.
- Fired from the same place the digest is written, so the two cannot report
  different numbers for the same night.

**Done when:** `python test_backup_webhook.py` passes, and a run with
nothing changed sends nothing at all — silence is the feature.

---

## #563 — export and import my setup, and move the data folder

**Size: medium.** The largest of the install group.

1. `backend/setup_bundle.py` — `export`, `inspect`, `apply`.
2. **Export**: one zip of settings (providers blanked), profiles, groups,
   credential sets (names and usernames only, never a secret), platforms,
   schemes, snippets, prompts, optionally the licence key. Manifest with
   per-file checksums.
3. **Import**: a preview table — replace, merge or skip per file, with
   counts — applied atomically with caches invalidated. Profile merges go
   through `identity()` and `find_matching`, or #73's duplicate problem
   comes straight back.
4. **Move data folder**: pick a directory, copy, write a `data-dir.txt`
   pointer beside the exe, restart. One override check in `paths.data_dir()`
   and nowhere else.
5. Import with sessions open **refuses**, mirroring `updater.blockers`. It
   is the same class of problem: replacing state underneath a live
   connection.

The DPAPI point has to be stated in the UI, not just the manual: a vault
encrypted to a Windows account does not travel, and somebody who exports
their setup and finds their credentials gone has been failed by a missing
sentence.

**Done when:** `python test_setup_bundle.py` passes, a round trip on a
seeded data folder reproduces it, and no exported file contains a secret.

---

## #547 — scheduled show collection

**Size: large.** Cut this first if the run runs out.

1. The schedule dialog gains "also collect": one or more read-only snippets.
   **Only snippets not marked `writes`**, checked against the dangerous list
   the way `config_push._dangerous` does — a scheduled overnight job is the
   worst possible place for a command that changes something.
2. `scheduler.run_group` gains a `collect(session, snippet)` step after
   `capture`, on the second channel.
3. Results written through `store.submit` under a synthetic session with
   `connection_type = "collection"`, so History finds
   `show interfaces status` on every access switch from last night.
4. A Collections filter in History, and compare-with-previous: the same
   command on the same device across two runs.
5. Bounded: `history.max_output_chars`, retention, and a per-group
   collection age. Unbounded growth is the stated risk and the one that
   turns this from useful into a disk somebody has to go and clear.

**Done when:** `python test_collection.py` passes, a snippet marked `writes`
cannot be scheduled, and a collection older than the age is pruned.

---

## Not in this run, and why

- **#518, #449, #570, #611, #594** — need a decision from Steven.
- **#526, #546, #566, #541, #548, #571, #572** — available, but this run is
  already six items and the last one is large.
