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

  /** Shorthand for a Stockton value. */
  const A = (key, fallback) =>
    (window.shellmateAdvanced ? window.shellmateAdvanced(key, fallback) : fallback);

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

    const delay = window.shellmateAdvanced
      ? window.shellmateAdvanced('capture.start_delay', START_DELAY_MS)
      : START_DELAY_MS;
    setTimeout(() => runCheck(id, sessionData), delay);
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

      // Say what happened to the snapshot, whether or not it worked and
      // whether or not a file was also written. Silence used to mean all
      // of "captured fine", "capture switched off", "the device refused"
      // and "this is broken" — which is why a working capture was
      // reported as a missing feature.
      confirmCapture(report, sessionData);

      // Nothing further to compare. The notice above has already said why,
      // which is the half that used to be missing — this return is about the
      // drift banner, not about staying quiet altogether.
      if (!report.available) return;

      if (!report.summary) return;
      if (setting('diff_on_connect', true) === false) return;

      showBanner(sessionId, report, sessionData);
    } catch (e) {
      /* best-effort by design */
    }
  }

  /**
   * Say what happened to the configuration snapshot.
   *
   * This used to be gated on `archive.written` — whether an optional *file*
   * was produced — and `save_config_files` defaults to false. So the capture
   * that feeds drift, history and the whole diff feature happened silently
   * every time, and the one line that would have confirmed it returned on its
   * first statement.
   *
   * The snapshot and the file are two features with two settings, so they are
   * two sentences rather than one condition.
   */
  function confirmCapture(report, sessionData) {
    if (!window.shellmateAlerts || !window.shellmateAlerts.notify) return;

    const capture = report.capture;
    if (!capture) return;

    const device = report.hostname || sessionData.display_label || 'device';

    if (!capture.captured) {
      // "No configuration captured", with the reason, is the difference
      // between a feature that is switched off and one that is broken.
      window.shellmateAlerts.notify({
        severity: 'warning',
        icon: 'error',
        title: `No configuration captured — ${device}`,
        body: capture.reason || 'The device did not provide one.',
        sessionId: sessionData.session_id,
      });
      return;
    }

    queueCaptureNotice(device, capture, report, sessionData);
  }

  /**
   * Captures waiting to be summarised.
   *
   * Opening a whole tag (#102) opens forty sessions at once, and forty toasts
   * is not a confirmation, it is an interruption. One line covers them.
   */
  let pendingCaptures = [];
  let captureTimer = null;

  function queueCaptureNotice(device, capture, report, sessionData) {
    pendingCaptures.push({ device, capture, report, sessionData });
    clearTimeout(captureTimer);
    captureTimer = setTimeout(flushCaptureNotices, 1200);
  }

  function flushCaptureNotices() {
    const batch = pendingCaptures;
    pendingCaptures = [];
    if (!batch.length) return;

    if (batch.length > 1) {
      const total = batch.reduce((n, b) => n + (b.capture.lines || 0), 0);
      window.shellmateAlerts.notify({
        title: `${batch.length} configurations captured`,
        body: `${total.toLocaleString()} lines in total, stored for comparison.`,
      });
      return;
    }

    const { device, capture, report, sessionData } = batch[0];
    const archive = report.archive || {};
    const parts = [`${(capture.lines || 0).toLocaleString()} lines, stored`];

    if (capture.unchanged) parts.push('unchanged since the last one');
    if (archive.written && archive.path) {
      parts.push('saved as ' + archive.path.split(/[\\/]/).pop()
                 + (archive.redacted ? ' with secrets masked' : ''));
    }

    window.shellmateAlerts.notify({
      title: `Configuration captured — ${device}`,
      body: parts.join(' · '),
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
      setTimeout(() => banner.remove(), A('files.drift_banner_seconds', 8) * 1000);
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

    renderBaselineLine(report);

    const body = document.getElementById('diff-body');
    body.innerHTML = '';
    hunksOf(report.diff).forEach(hunk => body.appendChild(renderHunk(hunk)));

    // The history is a second round trip, so it fills in behind the diff
    // rather than delaying it.
    renderHistory(report.hostname || sessionData.display_label || '');

    overlay.classList.remove('hidden');
  }


  // -------------------------------------------------------------------------
  // Baselines and comparing any two snapshots
  //
  // "Since your last visit" is an accident of when you happened to log in,
  // and looking at a device consumes it: connect, see four lines changed,
  // reconnect an hour later to investigate, and you are told nothing has
  // changed — the evidence is one row further back with nothing to reach it
  // with. Everything needed was already in the database and in the API; only
  // the last-visit comparison was wired to anything a user could click.
  // -------------------------------------------------------------------------

  /**
   * The extra line under the summary, when a baseline is pinned.
   *
   * Shown *alongside* the last-visit number rather than instead of it. They
   * answer different questions and the honest thing is to give both.
   */
  function renderBaselineLine(report) {
    const existing = document.getElementById('diff-baseline-line');
    if (existing) existing.remove();
    if (!report.baseline) return;

    const line = document.createElement('p');
    line.id = 'diff-baseline-line';
    line.className = 'settings-section-hint';
    line.textContent = report.baseline.summary
      + (report.baseline.note ? ` (${report.baseline.note})` : '');

    const summary = document.getElementById('diff-summary');
    if (summary && summary.parentNode) {
      summary.parentNode.insertBefore(line, summary.nextSibling);
    }
  }

  /**
   * The snapshot history for a device, with a way to pin one and to compare
   * any two.
   *
   * The endpoints for all of this already existed —
   * `/api/configs/{hostname}` and `/api/configs/diff/{old}/{new}` — and
   * nothing called them with anything but the newest pair.
   */
  async function renderHistory(hostname) {
    const host = document.getElementById('diff-history');
    if (!host || !hostname) return;
    host.innerHTML = '';

    let snapshots = [];
    let baseline = null;
    try {
      const [listRes, baseRes] = await Promise.all([
        fetch(`/api/configs/${encodeURIComponent(hostname)}?limit=40`),
        fetch(`/api/configs/baseline/${encodeURIComponent(hostname)}`),
      ]);
      snapshots = listRes.ok ? await listRes.json() : [];
      baseline = baseRes.ok ? await baseRes.json() : null;
    } catch (_) { return; }

    if (snapshots.length < 2) return;

    const heading = document.createElement('h4');
    heading.className = 'settings-subsection-title';
    heading.textContent = 'Compare any two';
    host.appendChild(heading);

    const hint = document.createElement('p');
    hint.className = 'settings-section-hint';
    hint.textContent = 'Pick two and compare, or pin one as the baseline this '
      + 'device is measured against from now on.';
    host.appendChild(hint);

    const list = document.createElement('div');
    list.className = 'diff-history-list';

    snapshots.forEach((snap) => {
      const row = document.createElement('label');
      row.className = 'diff-history-row';

      const tick = document.createElement('input');
      tick.type = 'checkbox';
      tick.dataset.id = String(snap.id);

      const when = document.createElement('span');
      when.className = 'diff-history-when';
      when.textContent = new Date(snap.captured_at * 1000).toLocaleString();

      const size = document.createElement('span');
      size.className = 'diff-history-size';
      size.textContent = `${snap.line_count} lines`;

      row.append(tick, when, size);

      if (baseline && Number(baseline.snapshot_id) === Number(snap.id)) {
        const badge = document.createElement('span');
        badge.className = 'credential-badge credential-encrypted';
        badge.textContent = 'baseline';
        row.appendChild(badge);
      }

      const pin = document.createElement('button');
      pin.type = 'button';
      pin.className = 'btn-secondary btn-tiny';
      pin.textContent = 'Pin as baseline';
      pin.addEventListener('click', async (e) => {
        e.preventDefault();
        await fetch('/api/configs/baseline', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ hostname, snapshot_id: snap.id }),
        });
        renderHistory(hostname);
      });
      row.appendChild(pin);

      list.appendChild(row);
    });

    host.appendChild(list);

    const actions = document.createElement('div');
    actions.className = 'setting-row';

    const compare = document.createElement('button');
    compare.type = 'button';
    compare.className = 'btn-primary';
    compare.textContent = 'Compare selected';
    compare.addEventListener('click', async () => {
      const picked = [...list.querySelectorAll('input:checked')]
        .map(i => Number(i.dataset.id)).sort((a, b) => a - b);
      if (picked.length !== 2) {
        note.textContent = 'Pick exactly two.';
        return;
      }
      note.textContent = '';
      const res = await fetch(`/api/configs/diff/${picked[0]}/${picked[1]}`);
      if (!res.ok) { note.textContent = 'Could not compare those.'; return; }
      const comparison = await res.json();

      document.getElementById('diff-summary').textContent =
        `${comparison.added} line${comparison.added === 1 ? '' : 's'} added, `
        + `${comparison.removed} removed, between the two you picked.`;
      const body = document.getElementById('diff-body');
      body.innerHTML = '';
      hunksOf(comparison.diff).forEach(h => body.appendChild(renderHunk(h)));
    });

    const clear = document.createElement('button');
    clear.type = 'button';
    clear.className = 'btn-secondary';
    clear.textContent = 'Unpin baseline';
    clear.addEventListener('click', async () => {
      await fetch(`/api/configs/baseline/${encodeURIComponent(hostname)}`,
                  { method: 'DELETE' });
      renderHistory(hostname);
    });

    const note = document.createElement('span');
    note.className = 'setting-value';

    actions.append(compare, clear, note);
    host.appendChild(actions);
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
