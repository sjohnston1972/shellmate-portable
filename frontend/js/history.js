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
  function closeReplay()  { replayOverlay.classList.add('hidden'); }

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

    try {
      const res = await fetch(`/api/history/search?${params.toString()}`);
      if (!res.ok) { showMessage('Search failed.'); return; }
      renderResults(await res.json());
    } catch (e) {
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
      row.appendChild(head);

      if (hit.snippet) {
        const snippet = document.createElement('div');
        snippet.className = 'history-snippet';
        snippet.textContent = hit.snippet;
        row.appendChild(snippet);
      }

      row.addEventListener('click', () => openReplay(hit.session_id));
      resultsEl.appendChild(row);
    });
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
