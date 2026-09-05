# Run: work the open issue list

Steven asked (5 Sep 2026) for the Ansible issues to be finished and then the
rest of the open list worked through, with the run continuing across usage
limits. Session-scoped scheduling cannot do that — cron jobs here die with
the session — so this file and PROGRESS.md are the baton instead. Any
session that opens in this directory reads them and carries on.

Working rules for this run:

- One issue per step. Commit and `git push origin portable:main` as each
  closes, then append a timestamped line to PROGRESS.md.
- `python tools/run_tests.py` must pass before a step is called done.
  `test_sftp.py` is a known intermittent (#586) — rerun it alone before
  treating it as a failure.
- Rebuild the executable (`pyinstaller build.spec --noconfirm`) only after
  bundled files change and only at the end of a step, not mid-work.
- Close the issue with `gh issue close`, one `Closes #N` keyword per issue
  in the commit message — a list after one keyword closes only the first.

---

## 1. Custom inventories: the backend (#608)

Curated lists of devices picked from the estate, and lists uploaded as CSV
or plain text. Stored in the Ansible library beside templates and
environments.

**Done when** `test_ansible_inventories.py` passes, covering: a curated list
survives a round trip; a CSV with a header is parsed into hosts with the
column the user nominated; a plain list of addresses works; a file whose
columns nobody mapped is refused rather than guessed at; and a list with no
platform mapping does not silently claim one.

## 2. Custom inventories: the area (#608)

The Inventory area becomes where they are built. Remove the "Generated
inventory (INI)" block — what matters is which hosts are in and which were
left out, and both are already shown above it. Add a "Custom inventories"
picker to the run dialog.

**Done when** the browser test drives building a curated list, uploading a
CSV, and choosing one in the run dialog; and `grep -c "Generated inventory"
frontend/` returns 0.

## 3. Ship example upload files (#608)

Worked examples for each shape named in the issue — Meraki export, plain
list, headered CSV, `ansible_host` style. Downloadable from the area.

**Done when** each shipped example parses through the same code path as a
user's upload, asserted in the test rather than by eye.

## 4. Create a GitHub repository when saving a playbook (#609)

Its own vaulted token setting, never `settings.json`. Offer both "create
one" and "use this existing one", so a smaller-scoped token works. Private
by default. The playbook only — never the inventory. A failure to reach
GitHub must still save locally.

**Done when** the test covers: the token never appears in `settings.json`;
a create failure still leaves the playbook saved; and the default
visibility is private.

## 5. Make the intermittent test diagnosable (#586)

Not a fix — the cause is still unknown after failures on two machines. Make
`tools/run_tests.py` keep the output of a file that fails, so the next
occurrence carries evidence instead of a summary line.

**Done when** a deliberately failing test file leaves its captured output
where the runner names it.

## 6. Honour ~/.ssh/config, and import its Host stanzas (#527)

**Done when** a `Host` stanza with `HostName`, `User`, `Port` and
`IdentityFile` imports as a profile, and an entry ShellMate cannot express
is reported rather than half-imported.

## 7. Session notes, kept with the history (#530)

**Done when** a note survives a restart, is searchable with the session it
belongs to, and appears in the session's own record rather than in a
separate list.

## 8. Serial and telnet ergonomics (#525)

Change baud mid-session, send a break over telnet, toggle DTR/RTS.

**Done when** each is exercised against the fake serial and telnet devices
in the existing tests, and a control that the transport cannot support is
disabled with a reason rather than hidden.

## 9. Route scheduled-backup findings somewhere (#539)

An in-app digest of what the overnight backups found, including the runs
that did not happen (#612 now records those).

**Done when** the digest names failures and missed runs, and says nothing
at all when there is nothing to report.

## 10. Add the neighbours: CDP/LLDP into the group (#542)

**Done when** neighbours parsed from a live session can be saved as
profiles into the session's group, with anything already known matched
rather than duplicated.
