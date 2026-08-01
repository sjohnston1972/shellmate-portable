/**
 * drift.js — "This device has changed since you were last here. Want to see?"
 *
 * Every SSH connection quietly snapshots the device's running configuration
 * and compares it against the previous visit. Logging in becomes a free drift
 * check: nobody has to remember to diff anything, and a change made by someone
 * else last week is visible the moment you arrive rather than when it breaks.
 *
 * Runs on a second SSH channel, so it never disturbs the session the user is
 * typing into. Plenty of devices will not cooperate — serial and telnet cannot
 * multiplex, some switches allow only one session, and the command varies by
 * platform — so this is strictly best-effort and stays silent when it cannot
 * run. An unavailable drift check is a missing nicety, not an error worth
 * interrupting anyone for.
 *
 * On presentation, two decisions worth stating (#42):
 *
 * **One announcement, not two.** The obvious way to "offer a diff rather than
 * interrupt" is to add a prompt — and then a banner and a prompt both announce
 * the same change, which is worse than either alone. So the banner *is* the
 * prompt: it asks a question and carries the button that answers it.
 *
 * **A prompt waits; it does not seize.** Nothing opens over the terminal by
 * itself. Someone arriving at a device mid-incident is not there to read a
 * diff, and a window they have to close first is a tax on every login.
 */
(function () {
  'use strict';

  // Give the session a moment to settle: a device still printing its login
  // banner will not answer a config request cleanly.
  const START_DELAY_MS = 2500;

  /** Sessions already checked, so switching tabs does not re-run it. */
  const checked = new Set();

  /** The report currently open in the diff window, for the copy buttons. */
  let openReport = null;

  /**
   * Kick off a drift check for a newly connected session.
   * Called by tabs.js once a tab is live.
   */
  function checkSession(sessionData) {
    if (!sessionData || sessionData.connection_type !== 'ssh') return;
    const id = sessionData.session_id;
    if (!id || checked.has(id)) return;
    checked.add(id);

    setTimeout(() => runCheck(id, sessionData), START_DELAY_MS);
  }

  function setting(name, fallback) {
    const logging = (window.shellmateSettings || {}).logging || {};
    return logging[name] === undefined ? fallback : logging[name];
  }

  async function runCheck(sessionId, sessionData) {
    try {
      const res = await fetch(`/api/sessions/${sessionId}/drift`);
      if (!res.ok) return;
      const report = await res.json();

      // Not available on this device: stay quiet rather than explaining
      // ourselves unprompted every time someone opens a serial console.
      if (!report.available) return;

      // The capture itself is meant to be invisible, so the one thing said
      // about it is that a copy was written where the user asked for it.
      confirmArchive(report, sessionData);

      if (!report.summary) return;
      if (setting('diff_on_connect', true) === false) return;

      showBanner(sessionId, report, sessionData);
    } catch (e) {
      /* best-effort by design */
    }
  }

  /** The small confirmation that a capture reached the archive. */
  function confirmArchive(report, sessionData) {
    const archive = report.archive || {};
    if (!archive.written) return;
    if (!window.shellmateAlerts || !window.shellmateAlerts.notify) return;

    const name = archive.path.split(/[\\/]/).pop();
    window.shellmateAlerts.notify({
      title: `Configuration saved — ${report.hostname || sessionData.display_label || 'device'}`,
      body: `${archive.lines.toLocaleString()} lines to ${name}` +
            (archive.redacted ? ', secrets masked' : ''),
      sessionId: sessionData.session_id,
    });
  }

  // -------------------------------------------------------------------------
  // The prompt
  // -------------------------------------------------------------------------

  function showBanner(sessionId, report, sessionData) {
    const host = document.getElementById('terminals-container');
    if (!host) return;

    // Replace any previous banner for this session rather than stacking them.
    const existing = document.querySelector(`.drift-banner[data-session="${sessionId}"]`);
    if (existing) existing.remove();

    const banner = document.createElement('div');
    banner.className = 'drift-banner' + (report.changed ? ' drift-banner-changed' : '');
    banner.dataset.session = sessionId;

    const icon = document.createElement('span');
    icon.className = 'material-symbols-outlined drift-icon';
    icon.textContent = report.changed ? 'difference' : 'check_circle';

    const text = document.createElement('span');
    text.className = 'drift-text';

    if (report.changed) {
      // Phrased as the question it is. "4 lines have changed" states a fact
      // and leaves the reader to work out that something can be done about
      // it; this says what is on offer.
      const lead = document.createElement('strong');
      lead.className = 'drift-lead';
      lead.textContent =
        `${report.hostname || sessionData.display_label || 'This device'} has changed ` +
        `since you last logged in.`;
      const detail = document.createElement('span');
      detail.className = 'drift-detail';
      detail.textContent =
        ` ${report.changed} line${report.changed === 1 ? '' : 's'} ` +
        `(${report.added} added, ${report.removed} removed)` +
        (report.days_since ? `, ${report.days_since} day${report.days_since === 1 ? '' : 's'} ago` : '') +
        '. Would you like to see the difference?';
      text.append(lead, detail);
    } else {
      text.textContent = report.summary;
    }

    banner.appendChild(icon);
    banner.appendChild(text);

    if (report.diff) {
      const view = document.createElement('button');
      view.className = 'drift-action';
      view.textContent = 'Show me';
      view.addEventListener('click', () => showDiff(report, sessionData));
      banner.appendChild(view);
    }

    const dismiss = document.createElement('button');
    dismiss.className = 'drift-dismiss';
    dismiss.setAttribute('aria-label', 'Dismiss');
    dismiss.innerHTML = '<span class="material-symbols-outlined">close</span>';
    dismiss.addEventListener('click', () => banner.remove());
    banner.appendChild(dismiss);

    host.appendChild(banner);

    // An unchanged config is reassurance, not news — let it fade. A changed
    // one stays until acknowledged.
    if (!report.changed) {
      setTimeout(() => banner.remove(), 8000);
    }
  }

  // -------------------------------------------------------------------------
  // The diff window
  // -------------------------------------------------------------------------

  function showDiff(report, sessionData) {
    const overlay = document.getElementById('diff-overlay');
    if (!overlay) return;

    openReport = report;

    document.getElementById('diff-title').textContent =
      `${report.hostname || sessionData.display_label || 'Device'} — configuration changes`;

    document.getElementById('diff-summary').textContent =
      `${report.added} line${report.added === 1 ? '' : 's'} added, ` +
      `${report.removed} removed, since ${report.days_since ?? 0} day` +
      `${report.days_since === 1 ? '' : 's'} ago.`;

    const body = document.getElementById('diff-body');
    body.innerHTML = '';
    hunksOf(report.diff).forEach(hunk => body.appendChild(renderHunk(hunk)));

    overlay.classList.remove('hidden');
  }

  /**
   * Split a unified diff into its hunks.
   *
   * A configuration diff is rarely one change: a VLAN added here, an ACL line
   * removed there. Rendered as one long block they run together, and there is
   * no way to take one of them anywhere. Split, each becomes a thing that can
   * be read on its own and copied on its own.
   */
  function hunksOf(diff) {
    const hunks = [];
    let current = null;

    (diff || '').split('\n').forEach(line => {
      // The +++/--- file headers name the two snapshots; the summary above
      // already says all of that in prose.
      if (line.startsWith('+++') || line.startsWith('---')) return;

      if (line.startsWith('@@')) {
        current = { header: line, lines: [] };
        hunks.push(current);
        return;
      }
      if (!current) {
        current = { header: '', lines: [] };
        hunks.push(current);
      }
      current.lines.push(line);
    });

    return hunks.filter(h => h.lines.length || h.header);
  }

  function renderHunk(hunk) {
    const block = document.createElement('div');
    block.className = 'diff-hunk-block';

    const head = document.createElement('div');
    head.className = 'diff-hunk-head';

    const where = document.createElement('span');
    where.className = 'diff-hunk-where';
    where.textContent = describeHunk(hunk);
    head.appendChild(where);

    const added = hunk.lines.filter(l => l.startsWith('+')).map(l => l.slice(1));
    if (added.length) {
      // The lines without their "+" — what you would actually paste into a
      // device, which is the reason anyone copies a config hunk.
      head.appendChild(copyButton(
        'Copy added', added.join('\n'),
        `${added.length} added line${added.length === 1 ? '' : 's'}, without the + markers`));
    }
    head.appendChild(copyButton(
      'Copy hunk', [hunk.header, ...hunk.lines].filter(Boolean).join('\n'),
      'The block exactly as shown, markers and context included'));

    block.appendChild(head);

    const lines = document.createElement('div');
    lines.className = 'diff-hunk-lines';

    // Rendered line by line so additions and removals can be coloured. Using
    // textContent per line keeps device output out of the HTML parser.
    hunk.lines.forEach(line => {
      const row = document.createElement('div');
      row.className = 'diff-line';
      if (line.startsWith('+')) row.className += ' diff-add';
      else if (line.startsWith('-')) row.className += ' diff-remove';
      row.textContent = line;
      lines.appendChild(row);
    });

    block.appendChild(lines);
    return block;
  }

  /** "Around line 412" reads better than "@@ -412,7 +412,9 @@". */
  function describeHunk(hunk) {
    const match = /^@@\s*-(\d+)/.exec(hunk.header || '');
    return match ? `Around line ${match[1]}` : 'Change';
  }

  function copyButton(label, text, title) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'diff-copy';
    button.textContent = label;
    button.title = title || '';
    button.addEventListener('click', () => copy(text));
    return button;
  }

  async function copy(text) {
    try {
      await navigator.clipboard.writeText(text);
    } catch (e) {
      // Clipboard access can be refused; a textarea and execCommand still
      // works, and a copy button that silently does nothing is worse than an
      // old API.
      const scratch = document.createElement('textarea');
      scratch.value = text;
      scratch.style.position = 'fixed';
      scratch.style.opacity = '0';
      document.body.appendChild(scratch);
      scratch.select();
      try { document.execCommand('copy'); } catch (_) { /* nothing left to try */ }
      scratch.remove();
    }
    if (typeof window._showCopyToast === 'function') window._showCopyToast();
  }

  document.addEventListener('DOMContentLoaded', () => {
    const close = document.getElementById('diff-close');
    const overlay = document.getElementById('diff-overlay');
    if (close) close.addEventListener('click', () => overlay.classList.add('hidden'));
    if (overlay) {
      overlay.addEventListener('click', (e) => {
        if (e.target === overlay) overlay.classList.add('hidden');
      });
    }

    const copyAll = document.getElementById('diff-copy-all');
    if (copyAll) {
      copyAll.addEventListener('click', () => {
        if (openReport && openReport.diff) copy(openReport.diff);
      });
    }

    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && overlay && !overlay.classList.contains('hidden')) {
        overlay.classList.add('hidden');
      }
    });
  });

  window.checkDrift = checkSession;
  window.showConfigDiff = showDiff;
})();
