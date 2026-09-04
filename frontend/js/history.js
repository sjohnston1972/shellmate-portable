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
    rangeSelect.addEventListener('change', _onRangeChange);
    _syncRangeControls();
    ['history-from', 'history-from-time', 'history-to', 'history-to-time'].forEach((id) => {
      const el = document.getElementById(id);
      if (el) el.addEventListener('change', runSearch);
    });
    const clearDates = document.getElementById('history-dates-clear');
    if (clearDates) clearDates.addEventListener('click', () => {
      ['history-from', 'history-from-time', 'history-to', 'history-to-time'].forEach((id) => {
        const el = document.getElementById(id);
        if (el) el.value = '';
      });
      runSearch();
    });

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

  function _onRangeChange() {
    _syncRangeControls();
    runSearch();
  }

  function rangeToSince(days) {
    if (!days || days === 'between') return null;
    return (Date.now() / 1000) - (Number(days) * 86400);
  }

  /**
   * The explicit range, in epoch seconds (#575).
   *
   * A date with no time means the whole of that day in local time: the
   * "from" starts at midnight, the "to" ends a moment before the next one,
   * which is what someone typing one date for both means.
   */
  function explicitRange() {
    if (rangeSelect.value !== 'between') return {};
    const el = (id) => document.getElementById(id);
    const from = el('history-from'), fromTime = el('history-from-time');
    const to = el('history-to'), toTime = el('history-to-time');
    const out = {};
    if (from && from.value) {
      const stamp = new Date(`${from.value}T${(fromTime && fromTime.value) || '00:00'}`);
      if (!Number.isNaN(stamp.getTime())) out.since = stamp.getTime() / 1000;
    }
    if (to && to.value) {
      const hasTime = !!(toTime && toTime.value);
      const stamp = new Date(`${to.value}T${hasTime ? toTime.value : '00:00'}`);
      if (!Number.isNaN(stamp.getTime())) {
        out.until = stamp.getTime() / 1000 + (hasTime ? 60 : 86400);
      }
    }
    return out;
  }

  /** Show or hide the two date fields, and remember nothing else about them. */
  function _syncRangeControls() {
    const row = document.getElementById('history-dates');
    if (!row) return;
    row.classList.toggle('hidden', rangeSelect.value !== 'between');
  }

  async function runSearch() {
    const params = new URLSearchParams();
    if (queryInput.value.trim()) params.set('q', queryInput.value.trim());
    if (deviceSelect.value) params.set('hostname', deviceSelect.value);

    const explicit = explicitRange();
    const since = explicit.since !== undefined ? explicit.since : rangeToSince(rangeSelect.value);
    if (since) params.set('since', String(since));
    if (explicit.until !== undefined) params.set('until', String(explicit.until));

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
      // And, when this device is open in front of you, the two things
      // actually wanted from a search result (#522).
      _recallButtons(head, hit);
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
   * The live tab for a recorded device, if one is open (#522).
   *
   * Matched on hostname, never on "whichever tab is active". Sending a
   * command from a search result into the wrong device is the one failure
   * this feature could produce, and "the tab in front of you" is exactly how
   * that happens — the result you are reading is about last Tuesday's session
   * on a switch you may not even be connected to now.
   *
   * A tab's `hostname` is rewritten with whatever the device calls itself, so
   * it is the field a recorded hostname matches; `address` is the fallback
   * for a session that never announced a name.
   */
  function _liveTabFor(hostname) {
    const wanted = String(hostname || '').trim().toLowerCase();
    if (!wanted) return null;
    const tabs = typeof window.getOpenTabs === 'function' ? window.getOpenTabs() : [];
    return tabs.find(t => t.isConnected && (
      String(t.hostname || '').toLowerCase() === wanted
      || String(t.address || '').toLowerCase() === wanted)) || null;
  }

  /**
   * "Send to <tab>" and "Put at the prompt", on a result whose device is open.
   *
   * Absent rather than disabled when nothing matches: a greyed-out Send on
   * every row of a search across an estate is noise, and the answer to "why
   * is it grey" is a paragraph.
   *
   * The button names the tab it will reach, because that is the only thing
   * standing between a recalled `reload` and the wrong switch.
   */
  function _recallButtons(head, hit) {
    const tab = _liveTabFor(hit.hostname || hit.label);
    if (!tab) return;
    const name = tab.label || tab.hostname || 'this tab';

    const insert = document.createElement('button');
    insert.type = 'button';
    insert.className = 'history-copy';
    insert.title = `Put it at the prompt on ${name}, without running it`;
    insert.innerHTML = '<span class="material-symbols-outlined">content_paste</span>';
    insert.addEventListener('click', (e) => {
      e.stopPropagation();
      _recall(tab, hit.command, false);
    });

    const send = document.createElement('button');
    send.type = 'button';
    send.className = 'history-copy history-send';
    send.title = `Run it on ${name}`;
    send.innerHTML = '<span class="material-symbols-outlined">send</span>';
    send.addEventListener('click', (e) => {
      e.stopPropagation();
      _recall(tab, hit.command, true);
    });

    head.appendChild(insert);
    head.appendChild(send);
  }

  /**
   * Put a recalled command into a session, and say what happened.
   *
   * Running one switches to its tab first. A command sent to a device you
   * cannot see is the thing ShellMate refuses to do everywhere else, and the
   * output is the reason anybody pressed the button.
   */
  function _recall(tab, command, run) {
    const to = run ? window.sendCommandToSession : window.insertIntoSession;
    const sent = typeof to === 'function' && to(tab.sessionId, command);
    const name = tab.label || tab.hostname || 'the tab';

    if (sent && typeof window.switchToTabBySessionId === 'function') {
      closeHistory();
      window.switchToTabBySessionId(tab.sessionId);
    }

    if (!window.shellmateAlerts) return;
    window.shellmateAlerts.notify({
      severity: sent ? 'info' : 'warning',
      icon:     sent ? (run ? 'send' : 'content_paste') : 'error',
      title:    sent ? (run ? `Sent to ${name}` : `At the prompt on ${name}`)
                     : `Could not reach ${name}`,
      body:     command,
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
    const pause = document.getElementById('replay-pause');
    const speed = document.getElementById('replay-speed');
    const seek = document.getElementById('replay-seek');
    if (!host || !play) return;
    _stopPlayer();
    host.classList.add('hidden');
    const progress = document.getElementById('replay-progress');
    if (progress) progress.classList.add('hidden');
    play.onclick = () => _startPlayer(session, host, Number(speed.value) || 4);
    stop.onclick = _stopPlayer;
    if (pause) pause.onclick = _togglePause;
    // Dragging the bar jumps: the commands up to that point are written at
    // once and playing continues from there (#573).
    if (seek) seek.oninput = () => { if (_player) _player.seekTo = Number(seek.value); };
    // Speed can change mid-playback; the loop reads it each wait.
    if (speed) speed.onchange = () => { if (_player) _player.speed = Number(speed.value) || 4; };
    play.disabled = !session.commands || !session.commands.length;
  }

  /** Pause or resume, from the button or the Space key (#573). */
  function _togglePause() {
    if (!_player) return;
    _player.paused = !_player.paused;
    const pause = document.getElementById('replay-pause');
    if (pause) {
      pause.innerHTML = _player.paused
        ? '<span class="material-symbols-outlined">replay</span> Resume'
        : '<span class="material-symbols-outlined">pending</span> Pause';
    }
  }

  function _formatClock(seconds) {
    const s = Math.max(0, Math.round(seconds));
    const m = Math.floor(s / 60);
    return `${m}:${String(s % 60).padStart(2, '0')}`;
  }

  /**
   * How long the recording runs: the gaps between commands, capped the same
   * way the player caps them, plus how long each command took.
   */
  function _recordingSpan(commands) {
    const marks = [];
    let total = 0;
    let previousEnd = null;
    for (const entry of commands) {
      const ranAt = Number(entry.ran_at) || 0;
      const took = (Number(entry.duration_ms) || 0) / 1000;
      if (previousEnd !== null && ranAt) {
        total += Math.min(Math.max(0, ranAt - previousEnd), 60);
      }
      marks.push(total);                      // when this command starts
      total += took;
      previousEnd = ranAt + took;
    }
    return { marks, total };
  }

  function _stopPlayer() {
    if (_player) {
      _player.cancelled = true;
      try { _player.term.dispose(); } catch (_) { /* already gone */ }
      _player = null;
    }
    const stop = document.getElementById('replay-stop');
    if (stop) stop.disabled = true;
    const pause = document.getElementById('replay-pause');
    if (pause) {
      pause.disabled = true;
      pause.innerHTML = '<span class="material-symbols-outlined">pending</span> Pause';
    }
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
    const commands = session.commands || [];
    const player = { term, cancelled: false, paused: false, speed, seekTo: null };
    _player = player;
    document.getElementById('replay-stop').disabled = false;
    const pause = document.getElementById('replay-pause');
    if (pause) pause.disabled = false;

    // The bar, the step and the clock (#573).
    const { marks, total } = _recordingSpan(commands);
    const progress = document.getElementById('replay-progress');
    const seek = document.getElementById('replay-seek');
    const stepEl = document.getElementById('replay-step');
    const clockEl = document.getElementById('replay-clock');
    if (progress) progress.classList.remove('hidden');
    if (seek) { seek.max = String(Math.max(0, commands.length - 1)); seek.value = '0'; }

    const show = (index, elapsed) => {
      const entry = commands[index];
      if (seek && document.activeElement !== seek) seek.value = String(index);
      if (stepEl) {
        stepEl.textContent = entry
          ? `${index + 1} of ${commands.length} · ${entry.command || ''}`
          : `${commands.length} of ${commands.length}`;
      }
      if (clockEl) clockEl.textContent = `${_formatClock(elapsed)} / ${_formatClock(total)}`;
    };

    // One wait, honouring pause, a speed change and a seek — all of which
    // can arrive while a slice is on screen.
    const wait = async (ms) => {
      const step = 60;
      let left = ms;
      while (left > 0) {
        if (player.cancelled || player.seekTo !== null) return;
        if (player.paused) { await new Promise(r => setTimeout(r, step)); continue; }
        const slice = Math.min(step, left / (player.speed || 1));
        await new Promise(r => setTimeout(r, Math.max(0, slice)));
        left -= slice * (player.speed || 1);
      }
    };

    let index = 0;
    let previousEnd = null;
    while (index < commands.length) {
      if (player.cancelled) return;

      // A drag: write everything up to the chosen command at once, then
      // carry on playing from it.
      if (player.seekTo !== null) {
        const target = Math.min(Math.max(0, player.seekTo), commands.length - 1);
        player.seekTo = null;
        term.reset();
        for (let i = 0; i < target; i++) {
          const past = commands[i];
          term.write((past.prompt || '') + past.command + '\r\n');
          const text = past.output || '';
          term.write(text.endsWith('\n') ? text : text + '\r\n');
        }
        index = target;
        previousEnd = null;
        show(index, marks[index] || 0);
        continue;
      }

      const entry = commands[index];
      const ranAt = Number(entry.ran_at) || 0;
      const took = Number(entry.duration_ms) || 0;
      if (previousEnd !== null && ranAt) {
        // The pause between the previous command finishing and this one.
        await wait(Math.min(Math.max(0, (ranAt - previousEnd) * 1000), 60000));
        if (player.cancelled) return;
        if (player.seekTo !== null) continue;
      }
      show(index, marks[index] || 0);
      term.write((entry.prompt || '') + entry.command + '\r\n');
      // The output over the time it originally took, in a few slices so a
      // long command visibly streams rather than lands at once.
      const output = entry.output || '';
      const slices = Math.min(20, Math.max(1, Math.ceil(output.length / 400)));
      const size = Math.ceil(output.length / slices);
      let jumped = false;
      for (let i = 0; i < slices; i++) {
        if (player.cancelled) return;
        term.write(output.slice(i * size, (i + 1) * size));
        await wait(took / slices);
        if (player.seekTo !== null) { jumped = true; break; }
      }
      if (jumped) continue;
      if (output && !output.endsWith('\n')) term.write('\r\n');
      previousEnd = ranAt + took / 1000;
      index += 1;
    }
    show(commands.length, total);
    term.write('\r\n\x1b[2m— end of recording —\x1b[0m\r\n');
    document.getElementById('replay-stop').disabled = true;
    if (pause) pause.disabled = true;
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

  // Space pauses, the arrows step a command at a time — but not while
  // somebody is typing in the search box (#573).
  document.addEventListener('keydown', (e) => {
    if (!_player || replayOverlay.classList.contains('hidden')) return;
    const tag = (e.target && e.target.tagName) || '';
    if (tag === 'INPUT' && e.target.type !== 'range') return;
    if (tag === 'TEXTAREA' || tag === 'SELECT') return;
    const seek = document.getElementById('replay-seek');
    const at = seek ? Number(seek.value) : 0;
    if (e.key === ' ') { e.preventDefault(); _togglePause(); }
    else if (e.key === 'ArrowRight') { e.preventDefault(); _player.seekTo = at + 1; }
    else if (e.key === 'ArrowLeft') { e.preventDefault(); _player.seekTo = Math.max(0, at - 1); }
  });

  window.openHistory = openHistory;
})();
