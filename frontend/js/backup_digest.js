/**
 * backup_digest.js — What the overnight backups found (#539).
 *
 * Scheduled backups (#408) were built and then reported into a log file.
 * Their most valuable output — "somebody changed core-2 overnight",
 * "Glasgow has failed three nights running", "nothing ran at all because
 * the laptop was shut" — went where nobody looks.
 *
 * Two rules, and both are about restraint:
 *
 * **It says nothing most mornings.** A clean run where nothing changed is
 * the normal night. Something that announces it every day is something
 * people dismiss unread, and then the morning it matters looks exactly
 * like all the others. The backend decides what is worth saying; this
 * shows only what it was given.
 *
 * **A run that did not happen is its own thing.** "It failed" sends
 * somebody to the device; "it never ran" sends them to the machine
 * ShellMate runs on. Collapsing the two wastes a morning.
 *
 * It offers rather than seizes, the same way drift does: a notice with a
 * button, never a window over the terminal. Somebody arriving at a device
 * mid-incident is not there to read last night's backup report.
 */

(function () {
  'use strict';

  /** Let the dashboard settle first; this is never the urgent thing. */
  const START_DELAY_MS = 3000;

  document.addEventListener('DOMContentLoaded', () => {
    setTimeout(announce, START_DELAY_MS);
  });

  async function _fetch(all) {
    try {
      const response = await fetch(`/api/backups/digest${all ? '?all=true' : ''}`);
      if (!response.ok) return null;
      return await response.json();
    } catch (e) {
      // A digest that cannot be read is not worth an error. It is a
      // report about last night, and the terminal is the thing that has
      // to work.
      return null;
    }
  }

  /** The notice, if there is anything to say. */
  async function announce() {
    const report = await _fetch(false);
    if (!report || !report.anything) return;
    if (!window.shellmateAlerts || !window.shellmateAlerts.notify) return;

    window.shellmateAlerts.notify({
      severity: report.failed || report.missed ? 'warning' : 'info',
      icon: report.failed || report.missed ? 'error' : 'archive',
      title: _line(report),
      body: _body(report),
      action: { label: 'Show me', onClick: () => open(report) },
    });
  }

  /**
   * The sentence, built here as well as in the backend.
   *
   * Deliberately: the backend's is for a log and a webhook, this one is
   * for a toast that has already been given a severity and an icon. Two
   * short sentences that agree is a smaller cost than one that has to
   * carry both jobs.
   */
  function _line(report) {
    const parts = [];
    if (report.changed) parts.push(`${report.changed} changed`);
    if (report.failed) parts.push(`${report.failed} failed`);
    if (report.missed) {
      parts.push(`${report.missed} run${report.missed === 1 ? '' : 's'} missed`);
    }
    const where = report.groups.length === 1
      ? report.groups[0].name
      : `${report.groups.length} groups`;
    return `Scheduled backups, ${where}: ${parts.join(', ')}.`;
  }

  function _body(report) {
    if (report.missed && !report.changed && !report.failed) {
      return 'ShellMate was not running when these were due. A gap in a '
           + 'backup history looks exactly like a quiet week.';
    }
    const names = report.groups.flatMap(g => g.changed).slice(0, 3);
    return names.length ? names.join(', ') : '';
  }

  /** The panel. Everything the digest holds, with the diffs behind it. */
  async function open(report) {
    const found = report || await _fetch(true);
    if (!found) return;

    const body = document.createElement('div');
    body.className = 'digest-body';

    if (!found.anything) {
      const quiet = document.createElement('p');
      quiet.className = 'digest-quiet';
      quiet.textContent = 'Nothing to report. Every scheduled run has '
                        + 'happened and nothing has changed since you last '
                        + 'looked.';
      body.appendChild(quiet);
    }

    found.groups.forEach(group => body.appendChild(_groupBlock(group)));

    await window.shellmateDialog.alert({
      title: 'Scheduled backups',
      content: body,
      confirmLabel: 'Done',
    });
    // Marked read on closing rather than on opening: a panel dismissed by
    // Escape three seconds after it appeared was not read, and the point
    // of the marker is that the next morning starts clean.
    try {
      await fetch('/api/backups/digest/seen', { method: 'POST' });
    } catch (e) { /* it will simply ask again */ }
  }

  function _groupBlock(group) {
    const block = document.createElement('section');
    block.className = 'digest-group';

    const head = document.createElement('h4');
    head.className = 'digest-group-title';
    head.textContent = group.name;
    const when = document.createElement('span');
    when.className = 'digest-when';
    when.textContent = _ago(group.at);
    head.appendChild(when);
    block.appendChild(head);

    if (group.missed) {
      // First, and worded as its own kind of problem. A missed run is not
      // a device that misbehaved; it is a night ShellMate was not there.
      const missed = document.createElement('p');
      missed.className = 'digest-missed';
      missed.textContent =
        `${group.missed} scheduled run${group.missed === 1 ? '' : 's'} did not `
        + 'happen — ShellMate was not running when they were due.';
      block.appendChild(missed);
    }

    if (group.changed.length) {
      block.appendChild(_list('Changed', group.changed.map(name => ({
        name,
        action: { label: 'Show changes', run: () => _showChanges(name) },
      })), 'digest-changed'));
    }

    if (group.failed.length) {
      block.appendChild(_list('Failed', group.failed.map(entry => ({
        name: entry.name, detail: entry.why,
      })), 'digest-failed'));
    }

    if (group.skipped && group.skipped.length) {
      // Last and quietest: a serial console has no address to back up and
      // never will, so this is context rather than news.
      block.appendChild(_list('Not attempted', group.skipped.map(entry => ({
        name: entry.name, detail: entry.why,
      })), 'digest-skipped'));
    }

    return block;
  }

  function _list(title, rows, cls) {
    const wrap = document.createElement('div');
    wrap.className = `digest-list ${cls}`;

    const label = document.createElement('p');
    label.className = 'digest-list-title';
    label.textContent = title;
    wrap.appendChild(label);

    rows.forEach(row => {
      const line = document.createElement('div');
      line.className = 'digest-row';

      const name = document.createElement('span');
      name.className = 'digest-device';
      // textContent: a device name is whatever somebody typed into a
      // profile, and is no more trustworthy as markup than device output.
      name.textContent = row.name;
      line.appendChild(name);

      if (row.detail) {
        const detail = document.createElement('span');
        detail.className = 'digest-detail';
        detail.textContent = row.detail;
        line.appendChild(detail);
      }

      if (row.action) {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'btn-tertiary';
        button.textContent = row.action.label;
        button.addEventListener('click', row.action.run);
        line.appendChild(button);
      }

      wrap.appendChild(line);
    });
    return wrap;
  }

  /**
   * The two most recent snapshots for a device, diffed.
   *
   * The digest knows the device changed but not against what, so the
   * snapshots are fetched here. Two are needed: one is a first capture,
   * which is a device newly backed up rather than one that changed.
   */
  async function _showChanges(hostname) {
    let snapshots = [];
    try {
      snapshots = await (await fetch(
        `/api/configs/${encodeURIComponent(hostname)}?limit=2`)).json();
    } catch (e) {
      snapshots = [];
    }
    if (snapshots.length < 2) {
      window.shellmateDialog.alert({
        title: hostname,
        body: snapshots.length
          ? 'This is the first configuration stored for this device, so '
            + 'there is nothing to compare it against yet.'
          : 'No stored configuration for this device.',
      });
      return;
    }

    try {
      const diff = await (await fetch(
        `/api/configs/diff/${snapshots[1].id}/${snapshots[0].id}`)).json();
      if (typeof window.showConfigDiff === 'function') {
        window.showConfigDiff({ ...diff, hostname, changed: true }, null);
      }
    } catch (e) {
      window.shellmateDialog.alert({
        title: hostname,
        body: `The comparison could not be built: ${e.message || e}`,
      });
    }
  }

  function _ago(at) {
    if (!at) return '';
    const delta = Math.max(0, Date.now() / 1000 - at);
    if (delta < 3600) return `${Math.round(delta / 60)} min ago`;
    if (delta < 86400) return `${Math.round(delta / 3600)} h ago`;
    return `${Math.round(delta / 86400)} d ago`;
  }

  window.openBackupDigest = () => open(null);
})();
