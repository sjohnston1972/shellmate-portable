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
   * What the diff window is currently showing, for the Explain button (#549).
   *
   * A pair of snapshot ids when two were picked from the history (or when a
   * push named its before and after); null when the window is showing this
   * session's connect-time drift, which the server already holds and can
   * reach from the session id alone. Only ever identifiers — the diff itself
   * is read, capped and masked server-side.
   */
  let openPair = null;
  let openSession = null;
  let openLabel = '';

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
      const live  = batch.filter(b => b.capture.via === 'live session').length;
      window.shellmateAlerts.notify({
        title: `${batch.length} configurations captured`,
        body: `${total.toLocaleString()} lines in total, stored for comparison.`
              // Still said when forty tabs open at once. This is somebody's
              // only notice that a command was run in sessions they are
              // sitting in, so it survives the batching that exists to stop
              // forty toasts.
              + (live ? ` ${live} ran in your own session${live === 1 ? '' : 's'}.`
                      : ''),
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

    // A capture over the live channel is deliberately hidden while it runs —
    // withheld from the screen, the buffer, the transcript and the log — so
    // this line is the whole of how anyone learns a command was typed into
    // the session they are sitting in. Hidden while it happens and stated
    // afterwards is the honest shape; genuinely invisible is not.
    const live = capture.via === 'live session';
    if (live) parts.push('run in this session, as the device refused a second channel');

    window.shellmateAlerts.notify({
      title: `Configuration captured — ${device}`,
      body: parts.join(' · '),
      sessionId: sessionData.session_id,
      // Where it went (#275). Only when a file was actually written — the
      // database copy has no folder to open, and an action that leads
      // nowhere is worse than none.
      action: (archive.written && archive.path)
        ? { label: 'Open the folder',
            onClick: () => fetch('/api/configs/archive/reveal', { method: 'POST' })
              .catch(() => { /* the folder not opening is not worth an error */ }) }
        : { label: 'Capture history',
            onClick: () => showDiff(report, sessionData) },
    });
  }

  // -------------------------------------------------------------------------
  // The prompt
  // -------------------------------------------------------------------------

  /**
   * Ask whether the changes are worth looking at.
   *
   * This was a full-width bar of its own, pinned bottom-left-to-right at
   * z-index 40 — while the alert toasts sat bottom-right at 60 and the device
   * notes bottom-right at 45. Three floating elements, three geometries, three
   * sizes, none aware of the others, and the two at bottom:16px occupied the
   * same corner. The device note landed squarely on this banner's "Show me"
   * button, so the one notification carrying a question was also the one most
   * likely to be buried.
   *
   * It is a toast now, in the same stack as everything else: one column, one
   * format, one set of dismissal rules, and things queue instead of colliding.
   * Sticky when there is something to answer, timed when it is only
   * reassurance.
   */
  function showBanner(sessionId, report, sessionData) {
    if (!window.shellmateAlerts || !window.shellmateAlerts.notify) return;

    const device = report.hostname || sessionData.display_label || 'This device';

    if (!report.changed) {
      // Nothing changed is reassurance, not news. It goes in the stack like
      // everything else and lets itself out — but it carries the way in to
      // the snapshots (#276). The diff panel holds every past capture and
      // the pinned baseline, and it was reachable only from a *change*: on a
      // device that never drifts, the history was unreachable entirely.
      window.shellmateAlerts.notify({
        icon:      'check_circle',
        title:     `${device} is unchanged`,
        body:      report.summary || '',
        sessionId,
        action:    { label: 'Capture history',
                     onClick: () => showDiff(report, sessionData) },
      });
      return;
    }

    const detail =
      `${report.changed} line${report.changed === 1 ? '' : 's'} ` +
      `(${report.added} added, ${report.removed} removed)` +
      (report.days_since
        ? `, ${report.days_since} day${report.days_since === 1 ? '' : 's'} ago`
        : '') + '.';

    window.shellmateAlerts.notify({
      severity: 'warning',
      icon:     'difference',
      // Phrased as the question it is. "4 lines have changed" states a fact
      // and leaves the reader to work out that something can be done about it.
      title:    `${device} has changed since you last logged in`,
      body:     detail,
      sessionId,
      // Stays until answered or dismissed — see the sticky note in alerts.js.
      sticky:   true,
      action:   report.diff ? { label: 'Show me',
                                onClick: () => showDiff(report, sessionData) }
                            : null,
    });
  }

  /**
   * The change-record block (#544).
   *
   * Only drawn when the report carries a `change`, so an ordinary drift
   * view is untouched. What goes here is what a change has and a drift
   * report does not: why it was made, how long it took, what was typed,
   * and whether the two ends are actually comparable.
   *
   * That last one is the reason this block exists at all. A drift report
   * always has two captures; a change record may have one or none — the
   * device may have reloaded, which is frequently the change itself. "No
   * difference" and "we could not look" render identically in a diff, and
   * a change board reading the first when the second is true is being told
   * the work had no effect.
   */
  /**
   * The one line at the top of a change record.
   *
   * Three outcomes, and they are three different sentences rather than one
   * sentence with a zero in it. "0 lines added, 0 removed" over a change
   * that could not be measured is the exact misreading this feature exists
   * to prevent.
   */
  function changeSummary(report) {
    if (!report.comparable) {
      return 'This change could not be measured — see below. Nothing here '
           + 'says the configuration did or did not change.';
    }
    if (!report.changed) {
      return 'The configuration is identical before and after this change.';
    }
    return `${report.added} line${report.added === 1 ? '' : 's'} added, `
         + `${report.removed} removed, over ${formatWindow(report.window_seconds)}.`;
  }

  function renderChangeBlock(report) {
    const host = document.getElementById('diff-change');
    if (!host) return;
    host.innerHTML = '';

    const change = report && report.change;
    if (!change) { host.classList.add('hidden'); return; }
    host.classList.remove('hidden');

    const facts = document.createElement('dl');
    facts.className = 'change-facts';
    const add = (label, value) => {
      if (!value) return;
      const dt = document.createElement('dt');
      dt.textContent = label;
      const dd = document.createElement('dd');
      dd.textContent = value;
      facts.append(dt, dd);
    };
    add('What for', change.note);
    add('Ticket', change.ticket);
    add('Operator', change.operator);
    add('Started', change.started_at
      ? new Date(change.started_at * 1000).toLocaleString() : '');
    add('Window', formatWindow(report.window_seconds));
    host.appendChild(facts);

    // Said before the hunks, not after them: somebody scrolling a diff and
    // finding the caveat underneath has already drawn a conclusion.
    if (!report.comparable) {
      const warn = document.createElement('div');
      warn.className = 'change-incomparable';
      warn.textContent = report.old_id
        ? 'The configuration could not be captured at the end of this change, '
          + 'so there is nothing to compare the start against. This is not the '
          + 'same as nothing having changed.'
        : 'No configuration was captured at the start of this change, so '
          + 'nothing below is a comparison.';
      if (report.capture_error) {
        const why = document.createElement('div');
        why.className = 'change-incomparable-why';
        why.textContent = report.capture_error;
        warn.appendChild(why);
      }
      host.appendChild(warn);
    }

    // Anything still hanging over the device. A record that omits the
    // reload describes a state the device is about to leave.
    if (report.pending) {
      const pending = document.createElement('div');
      pending.className = 'change-pending';
      const left = report.pending.seconds_left;
      pending.textContent = `A ${report.pending.kind || 'pending action'} is `
        + 'still outstanding on this device'
        + (typeof left === 'number' && left > 0
            ? `, in about ${Math.round(left / 60)} minute`
              + `${Math.round(left / 60) === 1 ? '' : 's'}.`
            : '.');
      host.appendChild(pending);
    }

    renderChangeCommands(host, report.commands || []);
  }

  function formatWindow(seconds) {
    if (typeof seconds !== 'number' || seconds < 0) return '';
    if (seconds < 60) return `${Math.round(seconds)} seconds`;
    if (seconds < 3600) return `${Math.round(seconds / 60)} minutes`;
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.round((seconds % 3600) / 60);
    return `${hours}h ${minutes}m`;
  }

  /**
   * What was typed in the window.
   *
   * Collapsed by default and counted on the summary line: on a long change
   * this is two hundred lines, and the diff is what somebody opened the
   * record to see. Empty is stated rather than left blank — "nothing was
   * typed" is a real and interesting answer about a configuration that
   * nevertheless changed.
   */
  function renderChangeCommands(host, commands) {
    const box = document.createElement('details');
    box.className = 'change-commands';

    const summary = document.createElement('summary');
    summary.textContent = commands.length
      ? `${commands.length} command${commands.length === 1 ? '' : 's'} typed `
        + 'during the change'
      : 'No commands were recorded during the change';
    box.appendChild(summary);

    if (!commands.length) {
      const none = document.createElement('p');
      none.className = 'change-commands-none';
      none.textContent = 'Either nothing was typed on this device in the '
        + 'window, or session recording is switched off. If the '
        + 'configuration changed anyway, it changed from somewhere else.';
      box.appendChild(none);
      host.appendChild(box);
      return;
    }

    const list = document.createElement('ol');
    list.className = 'change-command-list';
    commands.forEach((entry) => {
      const row = document.createElement('li');
      // textContent: this is what a device echoed, not markup.
      const cmd = document.createElement('code');
      cmd.textContent = entry.command || '';
      row.appendChild(cmd);
      if (entry.ran_at) {
        const when = document.createElement('span');
        when.className = 'change-command-when';
        when.textContent = new Date(entry.ran_at * 1000).toLocaleTimeString();
        row.appendChild(when);
      }
      list.appendChild(row);
    });
    box.appendChild(list);
    host.appendChild(box);
  }

  // -------------------------------------------------------------------------
  // The diff window
  // -------------------------------------------------------------------------

  function showDiff(report, sessionData) {
    const overlay = document.getElementById('diff-overlay');
    if (!overlay) return;

    openReport = report;
    // A report that names its two snapshots — a push's before and after —
    // can be explained precisely; otherwise Explain means "this session's
    // drift", which the server resolves from the session id.
    openPair = (report.old_id && report.new_id)
      ? { old: report.old_id, new: report.new_id } : null;
    openSession = (sessionData && sessionData.session_id) || null;
    openLabel = report.hostname || (sessionData && sessionData.display_label) || '';

    const name = report.hostname || sessionData.display_label || 'Device';
    // Opened from an unchanged device too now (#276), where "configuration
    // changes" and "0 lines added" would be a strange way to describe what
    // is on screen — which is the capture history.
    // A change record is not a drift report and should not be titled like
    // one: "since your last visit" is the wrong frame for a window somebody
    // opened deliberately twenty minutes ago.
    document.getElementById('diff-title').textContent = report.change
      ? `${name} — change record`
      : report.changed
        ? `${name} — configuration changes`
        : `${name} — configuration history`;

    document.getElementById('diff-summary').textContent =
      report.change ? changeSummary(report)
      : report.changed
        ? `${report.added} line${report.added === 1 ? '' : 's'} added, `
          + `${report.removed} removed, since ${report.days_since ?? 0} day`
          + `${report.days_since === 1 ? '' : 's'} ago.`
        : 'Nothing has changed since the last capture. Every stored capture is '
          + 'below — pick any two to compare.';

    // #544: a change record carries a note, a window, the commands and
    // whether the two ends are comparable at all. Drawn before the
    // hunks, because somebody who scrolls a diff and finds the caveat
    // underneath has already drawn a conclusion.
    renderChangeBlock(report);

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
      // Explain now means the pair on screen, not the connect-time drift.
      openPair = { old: picked[0], new: picked[1] };

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

    // Explain (#549). The window stays open — the answer arrives in the chat
    // pane beside it, which is where the diff is still on screen to read
    // against. Nothing about the diff is sent from here: the button hands
    // over two snapshot ids, or none at all, and the server does the rest.
    const explain = document.getElementById('diff-explain');
    if (explain) {
      explain.addEventListener('click', () => {
        if (typeof window.shellmateAskAboutDiff !== 'function') return;
        window.shellmateAskAboutDiff({
          oldId:     openPair ? openPair.old : null,
          newId:     openPair ? openPair.new : null,
          sessionId: openSession,
          label:     openLabel,
        });
      });
    }

    const copyAll = document.getElementById('diff-copy-all');
    if (copyAll) {
      copyAll.addEventListener('click', () => {
        if (openReport && openReport.diff) copy(openReport.diff);
      });
    }

    // Export (#540). Copy hands over the diff text; this hands over a
    // document with the device, the two timestamps and the counts around
    // it, which is what a change record has to carry to be one.
    const exportBtn = document.getElementById('diff-export');
    if (exportBtn) {
      exportBtn.addEventListener('click', () => {
        if (!window.shellmateReport) return;
        if (openPair) {
          window.shellmateReport.diff(exportBtn, openPair.old, openPair.new);
        } else if (openSession) {
          // No pair to compare: the change record says so in as many words
          // rather than rendering an empty section, which would read as
          // "nothing changed" when the truth is "nothing was captured".
          window.shellmateReport.change(exportBtn, openSession, null, null);
        }
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
