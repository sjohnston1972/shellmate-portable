/**
 * drift.js — "You were last here 12 days ago, 4 lines have changed."
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
 */
(function () {
  'use strict';

  // Give the session a moment to settle: a device still printing its login
  // banner will not answer a config request cleanly.
  const START_DELAY_MS = 2500;

  /** Sessions already checked, so switching tabs does not re-run it. */
  const checked = new Set();

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

  async function runCheck(sessionId, sessionData) {
    try {
      const res = await fetch(`/api/sessions/${sessionId}/drift`);
      if (!res.ok) return;
      const report = await res.json();

      // Not available on this device: stay quiet rather than explaining
      // ourselves unprompted every time someone opens a serial console.
      if (!report.available || !report.summary) return;

      showBanner(sessionId, report, sessionData);
    } catch (e) {
      /* best-effort by design */
    }
  }

  // -------------------------------------------------------------------------
  // Banner
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
    icon.textContent = report.changed ? 'summarize' : 'check_circle';

    const text = document.createElement('span');
    text.className = 'drift-text';
    text.textContent = report.summary;

    banner.appendChild(icon);
    banner.appendChild(text);

    if (report.diff) {
      const view = document.createElement('button');
      view.className = 'drift-action';
      view.textContent = 'View diff';
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

  function showDiff(report, sessionData) {
    const overlay = document.getElementById('diff-overlay');
    if (!overlay) return;

    document.getElementById('diff-title').textContent =
      `${report.hostname || sessionData.display_label || 'Device'} — configuration changes`;

    document.getElementById('diff-summary').textContent =
      `${report.added} line${report.added === 1 ? '' : 's'} added, ` +
      `${report.removed} removed, since ${report.days_since ?? 0} day` +
      `${report.days_since === 1 ? '' : 's'} ago.`;

    const body = document.getElementById('diff-body');
    body.innerHTML = '';

    // Rendered line by line so additions and removals can be coloured. Using
    // textContent per line keeps device output out of the HTML parser.
    report.diff.split('\n').forEach(line => {
      const row = document.createElement('div');
      row.className = 'diff-line';
      if (line.startsWith('+++') || line.startsWith('---')) row.className += ' diff-file';
      else if (line.startsWith('@@')) row.className += ' diff-hunk';
      else if (line.startsWith('+')) row.className += ' diff-add';
      else if (line.startsWith('-')) row.className += ' diff-remove';
      row.textContent = line;
      body.appendChild(row);
    });

    overlay.classList.remove('hidden');
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
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && overlay && !overlay.classList.contains('hidden')) {
        overlay.classList.add('hidden');
      }
    });
  });

  window.checkDrift = checkSession;
})();
