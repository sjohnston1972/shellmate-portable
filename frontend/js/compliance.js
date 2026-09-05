/**
 * compliance.js — Did the standard land everywhere? (#543)
 *
 * A group, a golden snippet, and a table saying which devices are missing
 * which lines. It reads the snapshots the nightly backups already stored,
 * so it needs no login and answers at once.
 *
 * **Three states get three treatments, and that is the point.** A device
 * nobody has captured is not "not compliant" — it may be perfectly
 * configured, and sending somebody to fix it is the wrong instruction. Nor
 * is it compliant, which would report a device no one has looked at as
 * verified. It gets its own row style and its own count.
 *
 * **The caveat is printed, not remembered.** Lines are compared as a set
 * with indentation stripped, so a line under the wrong parent counts as
 * present. That is right for the flat blocks this is for and wrong
 * otherwise, and a check that overstates what it verified is worse than no
 * check. The server sends the sentence; this prints it above the table
 * rather than holding its own copy that could drift from the matching.
 */
(function () {
  'use strict';

  let overlay, current = null;

  function el(tag, attrs, children) {
    const node = document.createElement(tag);
    Object.entries(attrs || {}).forEach(([k, v]) => {
      if (k === 'text') node.textContent = v;
      else if (k === 'class') node.className = v;
      else if (k === 'onclick') node.addEventListener('click', v);
      else node.setAttribute(k, v);
    });
    (Array.isArray(children) ? children : children ? [children] : [])
      .forEach(c => node.appendChild(typeof c === 'string'
        ? document.createTextNode(c) : c));
    return node;
  }

  function close() {
    if (overlay) overlay.classList.add('hidden');
  }

  function init() {
    overlay = document.getElementById('compliance-overlay');
    if (!overlay) return;
    document.getElementById('compliance-close')
      .addEventListener('click', close);
    overlay.addEventListener('click', (e) => {
      if (e.target === overlay) close();
    });
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && !overlay.classList.contains('hidden')) close();
    });
  }

  /**
   * Ask which snippet to check against, then run it.
   *
   * The library is fetched first so the picker lists real snippets rather
   * than asking somebody to remember an id. A snippet that writes is
   * offered like any other — this never sends anything, and refusing to
   * *compare* against a block because applying it would change something
   * would be a rule about the wrong verb.
   */
  async function open(group) {
    if (!group || !group.key) return;

    let library = [];
    try {
      const res = await fetch('/api/snippets');
      const data = await res.json();
      library = Array.isArray(data) ? data : (data.snippets || []);
    } catch (_) { library = []; }

    if (!library.length) {
      if (window.shellmateAlerts) window.shellmateAlerts.notify({
        global: true, severity: 'warning', icon: 'error',
        title: 'Nothing to check against',
        body: 'Save a snippet holding the lines every device should have, '
            + 'then check the group against it.' });
      return;
    }

    const options = library.map(s => ({
      value: s.id,
      label: s.platform ? `${s.name} (${s.platform})` : s.name,
    }));

    const answer = await window.shellmateDialog.form({
      title: `Check ${group.name} against a snippet`,
      body: 'Every device in the group is compared with the lines in the '
          + 'snippet, using the configuration ShellMate already has stored. '
          + 'Nothing is sent and nothing is logged into.',
      fields: [
        { name: 'snippet', label: 'Every device should have', type: 'select',
          options },
        { name: 'forbidden', label: 'And should not have (optional)',
          type: 'select',
          options: [{ value: '', label: 'Nothing in particular' }, ...options] },
        // Off by default. Somebody asking a one-off question about a site
        // has not asked for a nightly one, and a check that starts
        // recurring because it was run once is a surprise.
        { name: 'every_night', type: 'checkbox', value: false,
          label: 'Check again after every scheduled backup, and report it '
               + 'in the morning digest' },
      ],
      confirmLabel: 'Check',
    });
    if (!answer || !answer.snippet) return;

    await run(group, answer.snippet, answer.forbidden || '',
              !!answer.every_night);
  }

  async function run(group, snippetId, forbiddenId, everyNight) {
    let report;
    try {
      const res = await fetch('/api/groups/' + encodeURIComponent(group.key)
                              + '/compliance', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ snippet_id: snippetId,
                               must_not_have_id: forbiddenId || '',
                               every_night: !!everyNight }),
      });
      report = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(report.detail || `HTTP ${res.status}`);
    } catch (e) {
      if (window.shellmateAlerts) window.shellmateAlerts.notify({
        global: true, severity: 'warning', icon: 'error',
        title: 'The check could not be run', body: String(e.message || e) });
      return;
    }
    show(group, report);
  }

  function show(group, report) {
    if (!overlay) return;
    current = report;

    document.getElementById('compliance-title').textContent =
      `${group.name} — compliance`;
    document.getElementById('compliance-summary').textContent =
      report.summary || '';

    // Printed, not remembered. A caveat the panel owns is one a forwarded
    // or exported result loses.
    const limits = document.getElementById('compliance-limits');
    limits.textContent = report.limits || '';
    limits.classList.toggle('hidden', !report.limits);

    const body = document.getElementById('compliance-body');
    body.innerHTML = '';
    (report.devices || []).forEach(row => body.appendChild(deviceRow(row)));

    overlay.classList.remove('hidden');
  }

  const STATE_LABEL = {
    compliant: 'Has every line',
    missing: 'Missing lines',
    'never-captured': 'Never captured',
    'no-device-name': 'No device name',
    'no-snippet': 'No block for this platform',
    error: 'Could not be checked',
  };

  function deviceRow(row) {
    const card = el('div', { class: `compliance-row compliance-${row.state}` });

    const head = el('div', { class: 'compliance-head' }, [
      el('span', { class: 'compliance-name', text: row.name }),
      el('span', { class: `compliance-state compliance-state-${row.state}`,
                   text: STATE_LABEL[row.state] || row.state }),
    ]);

    // The age of the evidence sits on the verdict, not beside it: a row
    // that says "has every line" without saying when it was last looked at
    // invites exactly the wrong conclusion.
    const when = row.age_days === null || row.age_days === undefined
      ? (row.state === 'never-captured' ? 'no capture stored' : '')
      : row.age_days < 1 ? 'captured today'
      : `captured ${Math.round(row.age_days)} day`
        + `${Math.round(row.age_days) === 1 ? '' : 's'} ago`;
    if (when) {
      head.appendChild(el('span', {
        class: row.stale ? 'compliance-age compliance-age-stale'
                         : 'compliance-age',
        text: row.stale ? `${when} — this verdict is that old too` : when,
      }));
    }
    card.appendChild(head);

    if (row.missing && row.missing.length) {
      card.appendChild(lineList('Missing', row.missing, 'compliance-missing'));
    }
    if (row.unexpected && row.unexpected.length) {
      card.appendChild(lineList('Should not be there', row.unexpected,
                                'compliance-unexpected'));
    }
    if (row.state === 'never-captured') {
      card.appendChild(el('p', { class: 'compliance-note',
        text: 'Nothing is claimed about this device either way. Back the '
            + 'group up, or open it once, and it will be answerable.' }));
    }
    if (row.state === 'no-snippet') {
      card.appendChild(el('p', { class: 'compliance-note',
        text: `No block was given for ${row.platform || 'this platform'}, so `
            + 'it was not checked. Checking it against another platform’s '
            + 'block would report every line missing.' }));
    }
    if (row.why) {
      card.appendChild(el('p', { class: 'compliance-note', text: row.why }));
    }

    // Only where there is something to fix and somewhere to fix it.
    if (row.state === 'missing' && row.missing && row.missing.length) {
      card.appendChild(el('button', {
        type: 'button', class: 'btn-secondary compliance-fix',
        title: 'Open this device and load the missing lines into the '
             + 'configuration editor, with a preview before anything is sent',
        onclick: () => openAndFix(row),
      }, [icon('tune'), 'Open and fix']));
    }
    return card;
  }

  function icon(name) {
    return el('span', { class: 'material-symbols-outlined', text: name });
  }

  function lineList(label, lines, cls) {
    return el('div', { class: `compliance-lines ${cls}` }, [
      el('span', { class: 'compliance-lines-label', text: label }),
      el('ul', {}, lines.map(line => el('li', {}, el('code', { text: line })))),
    ]);
  }

  /**
   * Open the device and load the missing lines into the push editor.
   *
   * Loaded, never sent. config_push shows its own preview and its own
   * dangerous-command guardrail, and this hands it text exactly as if
   * somebody had typed it — a compliance report is evidence, not
   * permission.
   */
  async function openAndFix(row) {
    const text = (row.missing || []).join('\n');
    const tab = await findOrOpen(row);
    if (!tab) {
      if (window.shellmateAlerts) window.shellmateAlerts.notify({
        global: true, severity: 'warning', icon: 'error',
        title: `Could not open ${row.name}`,
        body: 'Open a session to it, then use Apply configuration… from the '
            + 'tab menu. The missing lines are listed above.' });
      return;
    }
    close();
    if (window.shellmateConfigPush) window.shellmateConfigPush.open(tab, text);
  }

  /** An open tab for this device, or a new one from its saved profile. */
  async function findOrOpen(row) {
    const tabs = (typeof window.getOpenTabs === 'function')
      ? window.getOpenTabs() : [];
    const match = tabs.find(t =>
      String(t.hostname || '').toLowerCase() === String(row.hostname).toLowerCase()
      || String(t.label || '').toLowerCase() === String(row.name).toLowerCase());
    if (match) {
      // switchToTabBySessionId, not activateTab — the latter does not
      // exist. tabs.js exposes the session-id form for exactly this.
      if (typeof window.switchToTabBySessionId === 'function') {
        window.switchToTabBySessionId(match.sessionId);
      }
      return match;
    }

    if (typeof window.connectProfile !== 'function') return null;
    try {
      const res = await fetch('/api/profiles');
      const data = await res.json();
      const profiles = Array.isArray(data) ? data : (data.profiles || []);
      const profile = profiles.find(p => p.name === row.name
        || String(p.hostname || '').toLowerCase()
           === String(row.hostname).toLowerCase());
      if (!profile) return null;
      await window.connectProfile(profile);
    } catch (_) {
      return null;
    }

    // The tab appears asynchronously; give it a moment rather than racing.
    for (let attempt = 0; attempt < 20; attempt++) {
      await new Promise(r => setTimeout(r, 150));
      const tabsNow = (typeof window.getOpenTabs === 'function')
        ? window.getOpenTabs() : [];
      const found = tabsNow.find(t =>
        String(t.hostname || '').toLowerCase()
          === String(row.hostname).toLowerCase()
        || String(t.label || '').toLowerCase() === String(row.name).toLowerCase());
      if (found) return found;
    }
    return null;
  }

  document.addEventListener('DOMContentLoaded', init);

  window.shellmateCompliance = { open, run, show, close };
})();
