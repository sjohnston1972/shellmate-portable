/**
 * palette.js — Find a tab by name (#410), and recall a command (#522).
 *
 * With a few dozen sessions open the tab strip overflows, and the only way
 * to reach a tab was to scroll the strip or remember its number. Ctrl+P
 * opens a small box over the terminal: type part of a name, hostname,
 * address or group, and Enter switches to the best match. Up/Down move the
 * highlight; Escape closes.
 *
 * It reads the tab list through `window.listTabs()` rather than reaching
 * into tabs.js, so it knows exactly what the strip knows and nothing more.
 *
 * Ctrl+R opens the same box in its second mode: the commands already run on
 * *this* device, newest first and deduplicated, read from the session store.
 * Enter puts one at the prompt without running it — a command recalled from
 * last month is a starting point far more often than it is a thing to run
 * verbatim — and Ctrl+Enter runs it.
 *
 * One box rather than two because it is one gesture with one set of keys:
 * type, arrow, Enter, Escape. A second floating list with its own conventions
 * is a second thing to learn for no gain.
 */
(function () {
  'use strict';

  let box = null;
  let input = null;
  let list = null;
  let matches = [];
  let highlighted = 0;

  /** 'tabs' or 'commands'. Decides what is listed and what Enter does. */
  let mode = 'tabs';

  /** In command mode: the session Enter types into, and its recall list. */
  let recallTab = null;
  let recallCommands = [];

  function build() {
    box = document.createElement('div');
    box.id = 'tab-palette';
    box.className = 'hidden';
    box.setAttribute('role', 'dialog');
    box.setAttribute('aria-label', 'Find a tab');

    input = document.createElement('input');
    input.type = 'text';
    input.placeholder = 'Find a tab by name, hostname or group…';
    input.setAttribute('aria-label', 'Find a tab');
    input.autocomplete = 'off';
    input.spellcheck = false;

    list = document.createElement('div');
    list.className = 'tab-palette-list';
    list.setAttribute('role', 'listbox');

    box.append(input, list);
    document.body.appendChild(box);

    input.addEventListener('input', () => render(input.value));
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') { e.preventDefault(); close(); }
      else if (e.key === 'ArrowDown') { e.preventDefault(); move(1); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); move(-1); }
      else if (e.key === 'Enter') {
        e.preventDefault();
        // Ctrl+Enter runs a recalled command instead of only typing it.
        // Deliberately the harder of the two: Enter alone puts the command
        // where you can read it before it reaches a device.
        choose(highlighted, e.ctrlKey || e.metaKey);
      }
    });
    // A click on the page behind it closes it, the way a menu closes.
    document.addEventListener('mousedown', (e) => {
      if (box && !box.classList.contains('hidden') && !box.contains(e.target)) close();
    });
  }

  function open() {
    if (!box) build();
    mode = 'tabs';
    const tabs = typeof window.listTabs === 'function' ? window.listTabs() : [];
    if (!tabs.length) {
      if (window.shellmateAlerts) {
        window.shellmateAlerts.notify({ severity: 'info', icon: 'tab',
          title: 'No tabs open', body: 'Ctrl+T opens a connection.' });
      }
      return;
    }
    box.classList.remove('hidden');
    input.placeholder = 'Find a tab by name, hostname or group…';
    input.setAttribute('aria-label', 'Find a tab');
    input.value = '';
    render('');
    input.focus();
  }

  /**
   * Command recall for the active session (#522).
   *
   * Scoped to the device, not to the session: the list is what has ever been
   * run on this hostname, which is the question anybody pressing Ctrl+R is
   * asking. A tab whose device has not announced a name yet falls back to
   * every device — a shorter list than useless is not an improvement, and the
   * heading says which it is showing.
   */
  async function openRecall() {
    if (!box) build();
    const tab = typeof window.getActiveTab === 'function' ? window.getActiveTab() : null;
    if (!tab) {
      if (window.shellmateAlerts) {
        window.shellmateAlerts.notify({ severity: 'info', icon: 'history',
          title: 'No session to recall into',
          body: 'Ctrl+R recalls the commands already run on the device in front of you.' });
      }
      return;
    }

    mode = 'commands';
    recallTab = tab;
    recallCommands = [];

    const host = tab.hostname || '';
    box.classList.remove('hidden');
    input.placeholder = host
      ? `Commands run on ${host}…`
      : 'Commands run on any device…';
    input.setAttribute('aria-label', 'Recall a command');
    input.value = '';
    list.innerHTML = '<div class="tab-palette-empty">Reading history…</div>';
    input.focus();

    const params = new URLSearchParams({ limit: '300' });
    if (host) params.set('hostname', host);
    try {
      const res = await fetch(`/api/history/commands?${params}`);
      const data = res.ok ? await res.json() : { commands: [] };
      // Opened, closed and reopened while that was in flight: the answer
      // belongs to a box that is no longer showing this list.
      if (mode !== 'commands' || recallTab !== tab) return;
      recallCommands = data.commands || [];
    } catch (_) {
      if (mode !== 'commands' || recallTab !== tab) return;
      recallCommands = [];
    }
    render(input.value);
  }

  function close() {
    if (!box) return;
    box.classList.add('hidden');
    // Back to the default mode, which also tells a recall fetch still in
    // flight that its answer is no longer wanted.
    mode = 'tabs';
    // Back to the device, which is where the next keystroke belongs.
    const tab = typeof window.getActiveTab === 'function' ? window.getActiveTab() : null;
    if (tab && tab.terminalInstance) {
      try { tab.terminalInstance.focus(); } catch (_) { /* not mounted */ }
    }
  }

  function toggle() {
    if (box && !box.classList.contains('hidden')) close();
    else open();
  }

  /**
   * Rank tabs against the query: a label that starts with it first, then
   * one that contains it, then a hostname, address or group that does.
   * An empty query lists everything in strip order.
   */
  function rank(query, tabs) {
    const q = query.trim().toLowerCase();
    if (!q) return tabs.map(t => ({ tab: t, score: 0 }));
    const scored = [];
    tabs.forEach(t => {
      const label = (t.label || '').toLowerCase();
      const rest = [t.hostname, t.address, ...(t.groups || [])]
        .filter(Boolean).map(s => String(s).toLowerCase());
      let score = -1;
      if (label.startsWith(q)) score = 3;
      else if (label.includes(q)) score = 2;
      else if (rest.some(s => s.includes(q))) score = 1;
      if (score >= 0) scored.push({ tab: t, score });
    });
    return scored.sort((a, b) => b.score - a.score);
  }

  function render(query) {
    if (mode === 'commands') { renderCommands(query); return; }

    const tabs = typeof window.listTabs === 'function' ? window.listTabs() : [];
    matches = rank(query, tabs);
    highlighted = 0;
    list.innerHTML = '';

    if (!matches.length) {
      const empty = document.createElement('div');
      empty.className = 'tab-palette-empty';
      empty.textContent = 'No tab matches that.';
      list.appendChild(empty);
      return;
    }

    matches.slice(0, 40).forEach(({ tab }, i) => {
      const row = document.createElement('button');
      row.type = 'button';
      row.className = 'tab-palette-row' + (i === 0 ? ' active' : '');
      row.setAttribute('role', 'option');
      row.dataset.index = String(i);

      const dot = document.createElement('span');
      dot.className = 'tab-palette-dot' + (tab.isConnected ? ' live' : '');

      const name = document.createElement('span');
      name.className = 'tab-palette-name';
      name.textContent = tab.label || tab.hostname || '';

      const detail = document.createElement('span');
      detail.className = 'tab-palette-detail';
      const bits = [];
      if (tab.address && tab.address !== tab.label) bits.push(tab.address);
      if (tab.groups && tab.groups.length) bits.push(tab.groups.join(', '));
      if (!tab.isConnected) bits.push('disconnected');
      detail.textContent = bits.join('  ·  ');

      const number = document.createElement('span');
      number.className = 'tab-palette-number';
      number.textContent = tab.index < 9 ? `Ctrl+${tab.index + 1}` : '';

      row.append(dot, name, detail, number);
      row.addEventListener('click', () => choose(i));
      row.addEventListener('mousemove', () => highlight(i));
      list.appendChild(row);
    });
  }

  /**
   * The recall list, filtered as you type (#522).
   *
   * Filtered here rather than re-queried per keystroke: the whole list for
   * one device is a few hundred short strings, and a query per character over
   * the lock every live session's writes wait on is what the history panel's
   * debounce exists to avoid.
   */
  function renderCommands(query) {
    const q = query.trim().toLowerCase();
    matches = recallCommands
      .filter(entry => !q || String(entry.command || '').toLowerCase().includes(q))
      .map(entry => ({ command: entry }));
    highlighted = 0;
    list.innerHTML = '';

    if (!matches.length) {
      const empty = document.createElement('div');
      empty.className = 'tab-palette-empty';
      empty.textContent = q
        ? 'Nothing recorded matches that.'
        : 'Nothing recorded for this device yet. Run something and it is here next time.';
      list.appendChild(empty);
      return;
    }

    matches.slice(0, 40).forEach(({ command: entry }, i) => {
      const row = document.createElement('button');
      row.type = 'button';
      row.className = 'tab-palette-row' + (i === 0 ? ' active' : '');
      row.setAttribute('role', 'option');
      row.dataset.index = String(i);

      const dot = document.createElement('span');
      dot.className = 'tab-palette-dot';

      const name = document.createElement('span');
      name.className = 'tab-palette-name tab-palette-command';
      // textContent — a recorded command came off a device and is not ours
      // to render as markup.
      name.textContent = entry.command;

      const detail = document.createElement('span');
      detail.className = 'tab-palette-detail';
      const bits = [];
      if (entry.times > 1) bits.push(`${entry.times}×`);
      if (entry.hostname) bits.push(entry.hostname);
      bits.push(_when(entry.ran_at));
      detail.textContent = bits.filter(Boolean).join('  ·  ');

      const hint = document.createElement('span');
      hint.className = 'tab-palette-number';
      hint.textContent = i === 0 ? 'Enter' : '';

      row.append(dot, name, detail, hint);
      row.addEventListener('click', (e) => choose(i, e.ctrlKey || e.metaKey));
      row.addEventListener('mousemove', () => highlight(i));
      list.appendChild(row);
    });
  }

  /** When a command last ran, in the shape the history panel uses. */
  function _when(epochSeconds) {
    if (!epochSeconds) return '';
    const date = new Date(epochSeconds * 1000);
    const ageDays = (Date.now() - date.getTime()) / 86400000;
    if (ageDays < 7) {
      return date.toLocaleString(undefined,
        { weekday: 'short', hour: '2-digit', minute: '2-digit' });
    }
    return date.toLocaleDateString(undefined, { day: 'numeric', month: 'short' });
  }

  function highlight(i) {
    highlighted = i;
    list.querySelectorAll('.tab-palette-row').forEach((row, j) => {
      row.classList.toggle('active', j === i);
    });
    const row = list.querySelector(`.tab-palette-row[data-index="${i}"]`);
    if (row) row.scrollIntoView({ block: 'nearest' });
  }

  function move(delta) {
    const n = Math.min(matches.length, 40);
    if (!n) return;
    highlight((highlighted + delta + n) % n);
  }

  function choose(i, run) {
    const match = matches[i];
    if (!match) return;

    if (mode === 'commands') {
      chooseCommand(match.command, Boolean(run));
      return;
    }

    close();
    if (typeof window.switchToTabBySessionId === 'function') {
      window.switchToTabBySessionId(match.tab.sessionId);
    }
  }

  /**
   * Recall one command into the session the box was opened from (#522).
   *
   * Into *that* session, held since the box opened, rather than whatever is
   * active by the time Enter lands. They are the same tab in practice — but
   * "the active one" is how a recalled command finds the wrong device, and
   * this list is full of commands that changed something.
   */
  function chooseCommand(entry, run) {
    const tab = recallTab;
    close();
    if (!tab || !entry || !entry.command) return;

    const to = run ? window.sendCommandToSession : window.insertIntoSession;
    const sent = typeof to === 'function' && to(tab.sessionId, entry.command);

    if (!sent && window.shellmateAlerts) {
      window.shellmateAlerts.notify({
        severity: 'warning', icon: 'error',
        title: 'Could not reach that session',
        body: 'It may have disconnected since the list was opened.',
      });
    }
  }

  window.openTabPalette   = toggle;
  window.openCommandRecall = openRecall;
})();
