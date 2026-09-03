/**
 * history.js — Search across every session ever recorded.
 *
 * Session logs used to be flat text in a folder, which makes "what did I
 * change on the Glasgow core last Tuesday" a grep exercise. Every session is
 * now recorded as structured commands and output, so the same question is a
 * search with a device filter and a date range.
 *
 * Two views: a results list, and a replay showing one session command by
 * command in the order it happened.
 */
(function () {
  'use strict';

  let overlay, replayOverlay, queryInput, deviceSelect, rangeSelect;
  let resultsEl, statsEl;

  /**
   * Debounce handle for search-as-you-type.
   *
   * Searching on every keystroke would fire a query per character; a short
   * pause is the difference between one search for "shutdown" and eight.
   */
  let searchTimer = null;
  const SEARCH_DELAY_MS = 250;

  /**
   * Which search is the current one (#488).
   *
   * The debounce stops a query per keystroke; it does not order the replies.
   * A slow search for "sh" could land after the fast one for "shutdown on
   * core-1" and replace its results with its own, so every search takes a
   * number and a reply that is not the latest is dropped.
   */
  let searchSeq = 0;

  document.addEventListener('DOMContentLoaded', () => {
    overlay       = document.getElementById('history-overlay');
    replayOverlay = document.getElementById('replay-overlay');
    queryInput    = document.getElementById('history-query');
    deviceSelect  = document.getElementById('history-device');
    rangeSelect   = document.getElementById('history-range');
    resultsEl     = document.getElementById('history-results');
    statsEl       = document.getElementById('history-stats');

    document.getElementById('sidebar-link-history')
      .addEventListener('click', (e) => { e.preventDefault(); openHistory(); });

    document.getElementById('history-close').addEventListener('click', closeHistory);
    document.getElementById('history-clear').addEventListener('click', clearHistory);
    document.getElementById('replay-close').addEventListener('click', closeReplay);

    queryInput.addEventListener('input', () => {
      clearTimeout(searchTimer);
      searchTimer = setTimeout(runSearch, SEARCH_DELAY_MS);
    });
    deviceSelect.addEventListener('change', runSearch);
    rangeSelect.addEventListener('change', runSearch);

    overlay.addEventListener('click', (e) => { if (e.target === overlay) closeHistory(); });
    replayOverlay.addEventListener('click', (e) => { if (e.target === replayOverlay) closeReplay(); });

    document.addEventListener('keydown', (e) => {
      if (e.key !== 'Escape') return;
      // Close the topmost layer first, so Escape does not dismiss both.
      if (!replayOverlay.classList.contains('hidden')) closeReplay();
      else if (!overlay.classList.contains('hidden')) closeHistory();
    });
  });

  // -------------------------------------------------------------------------
  // Panel
  // -------------------------------------------------------------------------

  async function openHistory() {
    overlay.classList.remove('hidden');
    await Promise.all([loadDevices(), loadStats()]);
    await runSearch();
    setTimeout(() => queryInput.focus(), 50);
  }

  function closeHistory() { overlay.classList.add('hidden'); }
  function closeReplay()  { _stopPlayer(); replayOverlay.classList.add('hidden'); }


  // -------------------------------------------------------------------------
  // Clearing
  // -------------------------------------------------------------------------

  /**
   * Clear history, scoped by whatever the panel is already filtered to.
   *
   * Retention answers "discard anything older than N days" and recording
   * answers "stop from now on". Neither answers "get rid of what you have
   * about *that* device", which is the question that actually comes up when
   * an engagement ends or a lab is torn down.
   *
   * The two filters are the scope rather than a second, differently shaped
   * question — choosing a device and then being offered only all-or-nothing
   * is how the wrong thing gets deleted.
   */
  async function clearHistory() {
    const hostname = deviceSelect.value;
    const days     = Number(rangeSelect.value) || 0;

    // Said in full, because it cannot be undone and the two filters compose:
    // "Cisco-3560 older than 7 days" is easy to read as either half alone.
    const scope = [
      hostname ? `for ${hostname}` : 'for every device',
      days ? `older than ${days} day${days === 1 ? '' : 's'}` : '',
    ].filter(Boolean).join(', ');

    const answer = await window.shellmateDialog.form({
      title: 'Clear session history',
      body:  `This removes recorded commands and output ${scope}. It cannot be undone.`,
      note:  days
        // The inversion is worth stating outright: the panel shows the last N
        // days and this removes everything older, so what is on screen stays.
        ? 'The range filter selects what is kept — everything currently listed '
          + 'survives, and everything older goes.'
        : 'No date filter is set, so this covers all of it.',
      confirmLabel: 'Clear',
      danger: true,
      fields: [
        { name: 'snapshots', label: 'Also delete configuration snapshots',
          type: 'checkbox', value: true,
          // The most sensitive thing in the database: a running config
          // carries hashes, keys and community strings. Clearing a device's
          // history while quietly keeping its configs would be misleading.
          hint: 'Snapshots hold full configurations, including secrets. '
                + 'Baselines pointing at deleted snapshots are removed too.' },
      ],
    });
    if (!answer) return;

    const params = new URLSearchParams({
      snapshots: answer.snapshots ? 'true' : 'false',
    });
    if (hostname) params.set('hostname', hostname);
    if (days)     params.set('days', String(days));

    try {
      const res  = await fetch(`/api/history?${params}`, { method: 'DELETE' });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || 'Could not clear it.');

      // Counted rather than "done", because a scope that matched nothing and
      // a scope that took a thousand records look identical otherwise.
      const parts = [
        `${data.commands} command${data.commands === 1 ? '' : 's'}`,
        `${data.sessions} session${data.sessions === 1 ? '' : 's'}`,
      ];
      if (answer.snapshots) {
        parts.push(`${data.snapshots} snapshot${data.snapshots === 1 ? '' : 's'}`);
      }
      if (window.shellmateAlerts) {
        window.shellmateAlerts.notify({
          severity: 'info', icon: 'delete_sweep',
          title: 'History cleared',
          body:  parts.join(', ') + ' removed.',
        });
      }
    } catch (e) {
      if (window.shellmateAlerts) {
        window.shellmateAlerts.notify({
          severity: 'warning', icon: 'error',
          title: 'Could not clear history', body: e.message,
        });
      }
      return;
    }

    // The device list is rebuilt too: a device with nothing left should not
    // stay in the filter, and it is currently the selected one.
    deviceSelect.value = '';
    await loadDevices();
    await loadStats();
    await runSearch();
  }

  async function loadDevices() {
    try {
      const res = await fetch('/api/history/devices');
      const devices = res.ok ? await res.json() : [];
      const current = deviceSelect.value;

      deviceSelect.innerHTML = '<option value="">All devices</option>';
      devices.forEach(name => {
        const opt = document.createElement('option');
        opt.value = name;
        opt.textContent = name;
        deviceSelect.appendChild(opt);
      });
      deviceSelect.value = current;
    } catch (e) { /* filter just stays on "all" */ }
  }

  async function loadStats() {
    try {
      const res = await fetch('/api/history/stats');
      if (!res.ok) return;
      const s = await res.json();
      statsEl.textContent =
        `${s.commands.toLocaleString()} commands across ${s.sessions.toLocaleString()} ` +
        `sessions on ${s.devices} device${s.devices === 1 ? '' : 's'}.`;
      // Worth surfacing: without FTS5 the search is a plain substring match,
      // which behaves differently enough that the user should know.
      if (s.search !== 'fts5') {
        statsEl.textContent += ' Full-text search unavailable — using simple matching.';
      }
    } catch (e) { /* header is decoration */ }
  }

  // -------------------------------------------------------------------------
  // Search
  // -------------------------------------------------------------------------

  function rangeToSince(days) {
    if (!days) return null;
    return (Date.now() / 1000) - (Number(days) * 86400);
  }

  async function runSearch() {
    const params = new URLSearchParams();
    if (queryInput.value.trim()) params.set('q', queryInput.value.trim());
    if (deviceSelect.value) params.set('hostname', deviceSelect.value);

    const since = rangeToSince(rangeSelect.value);
    if (since) params.set('since', String(since));

    resultsEl.innerHTML = '<div class="history-loading">Searching…</div>';

    const seq = ++searchSeq;
    try {
      const res = await fetch(`/api/history/search?${params.toString()}`);
      // Superseded while in flight: a newer search owns the results pane.
      if (seq !== searchSeq) return;
      if (!res.ok) { showMessage('Search failed.'); return; }
      const data = await res.json();
      if (seq !== searchSeq) return;
      renderResults(data);
    } catch (e) {
      if (seq !== searchSeq) return;
      showMessage('Could not reach the server.');
    }
  }

  function renderResults(data) {
    resultsEl.innerHTML = '';

    if (!data.results.length) {
      showMessage(data.query
        ? `Nothing matches "${data.query}".`
        : 'No sessions recorded yet. Connect to a device and your history builds itself.');
      return;
    }

    data.results.forEach(hit => {
      const row = document.createElement('div');
      row.className = 'history-row';

      const head = document.createElement('div');
      head.className = 'history-row-head';

      const cmd = document.createElement('span');
      cmd.className = 'history-command';
      // textContent throughout — command text and device output are untrusted
      // and must never be parsed as markup.
      cmd.textContent = hit.command;

      const meta = document.createElement('span');
      meta.className = 'history-meta';
      meta.textContent = `${hit.hostname || hit.label || 'unknown'} · ${formatWhen(hit.ran_at)}`;

      head.appendChild(cmd);
      head.appendChild(meta);
      // Copying the command is the commonest thing anybody wants from a
      // search result — it is why they went looking (#273). stopPropagation,
      // or the row's own click would open the replay behind the copy.
      head.appendChild(_copyButton('Copy the command', hit.command));
      row.appendChild(head);

      if (hit.snippet) {
        const snippet = document.createElement('div');
        snippet.className = 'history-snippet';
        snippet.textContent = hit.snippet;
        row.appendChild(snippet);
        snippet.appendChild(_copyButton('Copy this output', hit.snippet));
      }

      row.addEventListener('click', () => openReplay(hit.session_id));
      resultsEl.appendChild(row);
    });
  }

  /**
   * A copy button for one piece of a result (#273).
   *
   * The clipboard API first, with the textarea route behind it for the
   * webview builds that refuse it, and the shared toast so a copy that
   * worked says so — the same shape drift.js uses for diff hunks.
   */
  function _copyButton(title, text) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'history-copy';
    button.title = title;
    button.innerHTML = '<span class="material-symbols-outlined">content_copy</span>';
    button.addEventListener('click', async (e) => {
      e.stopPropagation();
      try {
        await navigator.clipboard.writeText(text);
      } catch (_) {
        const area = document.createElement('textarea');
        area.value = text;
        document.body.appendChild(area);
        area.select();
        try { document.execCommand('copy'); } catch (_) { /* give up quietly */ }
        area.remove();
      }
      if (typeof window._showCopyToast === 'function') window._showCopyToast();
    });
    return button;
  }

  function showMessage(text) {
    resultsEl.innerHTML = '';
    const box = document.createElement('div');
    box.className = 'history-empty';
    box.textContent = text;
    resultsEl.appendChild(box);
  }

  // -------------------------------------------------------------------------
  // Replay
  // -------------------------------------------------------------------------

  // Timed playback (#415): the recorded commands, replayed into a terminal
  // with the gaps between them and the time each took, at a chosen speed.
  // The records already carry when each ran and how long it took; nothing
  // new is stored.
  let _player = null;

  function _bindPlayer(session) {
    const host = document.getElementById('replay-player');
    const play = document.getElementById('replay-play');
    const stop = document.getElementById('replay-stop');
    const speed = document.getElementById('replay-speed');
    if (!host || !play) return;
    _stopPlayer();
    host.classList.add('hidden');
    play.onclick = () => _startPlayer(session, host, Number(speed.value) || 4);
    stop.onclick = _stopPlayer;
    play.disabled = !session.commands || !session.commands.length;
  }

  function _stopPlayer() {
    if (_player) {
      _player.cancelled = true;
      try { _player.term.dispose(); } catch (_) { /* already gone */ }
      _player = null;
    }
    const stop = document.getElementById('replay-stop');
    if (stop) stop.disabled = true;
  }

  async function _startPlayer(session, host, speed) {
    _stopPlayer();
    host.classList.remove('hidden');
    host.innerHTML = '';
    const settings = (window.shellmateSettings || {}).terminal || {};
    const term = new window.Terminal({
      fontFamily: settings.font_family || "'JetBrains Mono', monospace",
      fontSize: Number(settings.font_size) || 13,
      scrollback: 5000,
      disableStdin: true,
      convertEol: true,
      theme: { background: '#1e1e2e', foreground: '#cdd6f4' },
    });
    const fit = window.FitAddon ? new window.FitAddon.FitAddon() : null;
    if (fit) term.loadAddon(fit);
    term.open(host);
    if (fit) { try { fit.fit(); } catch (_) { /* hidden */ } }
    const player = { term, cancelled: false };
    _player = player;
    document.getElementById('replay-stop').disabled = false;

    const wait = (ms) => new Promise(r => setTimeout(r, Math.max(0, ms / speed)));
    let previousEnd = null;
    for (const entry of session.commands) {
      if (player.cancelled) return;
      const ranAt = Number(entry.ran_at) || 0;
      const took = Number(entry.duration_ms) || 0;
      if (previousEnd !== null && ranAt) {
        // The pause between the previous command finishing and this one.
        await wait(Math.min(Math.max(0, (ranAt - previousEnd) * 1000), 60000));
      }
      if (player.cancelled) return;
      term.write((entry.prompt || '') + entry.command + '\r\n');
      // The output over the time it originally took, in a few slices so a
      // long command visibly streams rather than lands at once.
      const output = entry.output || '';
      const slices = Math.min(20, Math.max(1, Math.ceil(output.length / 400)));
      const size = Math.ceil(output.length / slices);
      for (let i = 0; i < slices; i++) {
        if (player.cancelled) return;
        term.write(output.slice(i * size, (i + 1) * size));
        await wait(took / slices);
      }
      if (output && !output.endsWith('\n')) term.write('\r\n');
      previousEnd = ranAt + took / 1000;
    }
    term.write('\r\n\x1b[2m— end of recording —\x1b[0m\r\n');
    document.getElementById('replay-stop').disabled = true;
  }

  async function openReplay(sessionId) {
    replayOverlay.classList.remove('hidden');
    const list = document.getElementById('replay-commands');
    list.innerHTML = '<div class="history-loading">Loading…</div>';

    try {
      const res = await fetch(`/api/history/sessions/${sessionId}`);
      if (!res.ok) { list.innerHTML = '<div class="history-empty">Session not found.</div>'; return; }
      renderReplay(await res.json());
    } catch (e) {
      list.innerHTML = '<div class="history-empty">Could not load the session.</div>';
    }
  }

  function renderReplay(session) {
    document.getElementById('replay-title').textContent =
      session.hostname || session.label || 'Session';
    _bindPlayer(session);

    const meta = document.getElementById('replay-meta');
    const started = formatWhen(session.started_at);
    const duration = session.ended_at
      ? formatDuration(session.ended_at - session.started_at)
      : 'still open';
    meta.textContent =
      `${session.connection_type.toUpperCase()} · ${session.target || ''} · ` +
      `${started} · ${duration} · ${session.commands.length} commands`;

    const list = document.getElementById('replay-commands');
    list.innerHTML = '';

    if (!session.commands.length) {
      list.innerHTML = '<div class="history-empty">No commands were recorded in this session.</div>';
      return;
    }

    session.commands.forEach(entry => {
      const block = document.createElement('div');
      block.className = 'replay-entry';

      const head = document.createElement('div');
      head.className = 'replay-command';

      const prompt = document.createElement('span');
      prompt.className = 'replay-prompt';
      prompt.textContent = entry.prompt || '';

      const text = document.createElement('span');
      text.textContent = entry.command;

      const timing = document.createElement('span');
      timing.className = 'replay-timing';
      timing.textContent = entry.duration_ms > 1000
        ? `${(entry.duration_ms / 1000).toFixed(1)}s`
        : `${entry.duration_ms}ms`;

      head.appendChild(prompt);
      head.appendChild(text);
      head.appendChild(timing);
      block.appendChild(head);

      if (entry.output) {
        const output = document.createElement('pre');
        output.className = 'replay-output';
        output.textContent = entry.output;
        block.appendChild(output);
      }

      list.appendChild(block);
    });
  }

  // -------------------------------------------------------------------------
  // Helpers
  // -------------------------------------------------------------------------

  function formatWhen(epochSeconds) {
    if (!epochSeconds) return '';
    const date = new Date(epochSeconds * 1000);
    const ageDays = (Date.now() - date.getTime()) / 86400000;
    // Within a week the weekday is what people actually remember — "last
    // Tuesday" is how the question gets asked in the first place.
    if (ageDays < 7) {
      return date.toLocaleString(undefined,
        { weekday: 'short', hour: '2-digit', minute: '2-digit' });
    }
    return date.toLocaleString(undefined,
      { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
  }

  function formatDuration(seconds) {
    if (!seconds || seconds < 0) return '';
    if (seconds < 60) return `${Math.round(seconds)}s`;
    if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
    return `${(seconds / 3600).toFixed(1)}h`;
  }

  window.openHistory = openHistory;
})();
