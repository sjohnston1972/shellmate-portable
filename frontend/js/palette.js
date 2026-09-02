/**
 * palette.js — Find a tab by name (#410).
 *
 * With a few dozen sessions open the tab strip overflows, and the only way
 * to reach a tab was to scroll the strip or remember its number. Ctrl+P
 * opens a small box over the terminal: type part of a name, hostname,
 * address or group, and Enter switches to the best match. Up/Down move the
 * highlight; Escape closes.
 *
 * It reads the tab list through `window.listTabs()` rather than reaching
 * into tabs.js, so it knows exactly what the strip knows and nothing more.
 */
(function () {
  'use strict';

  let box = null;
  let input = null;
  let list = null;
  let matches = [];
  let highlighted = 0;

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
      else if (e.key === 'Enter') { e.preventDefault(); choose(highlighted); }
    });
    // A click on the page behind it closes it, the way a menu closes.
    document.addEventListener('mousedown', (e) => {
      if (box && !box.classList.contains('hidden') && !box.contains(e.target)) close();
    });
  }

  function open() {
    if (!box) build();
    const tabs = typeof window.listTabs === 'function' ? window.listTabs() : [];
    if (!tabs.length) {
      if (window.shellmateAlerts) {
        window.shellmateAlerts.notify({ severity: 'info', icon: 'tab',
          title: 'No tabs open', body: 'Ctrl+T opens a connection.' });
      }
      return;
    }
    box.classList.remove('hidden');
    input.value = '';
    render('');
    input.focus();
  }

  function close() {
    if (!box) return;
    box.classList.add('hidden');
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

  function choose(i) {
    const match = matches[i];
    if (!match) return;
    close();
    if (typeof window.switchToTabBySessionId === 'function') {
      window.switchToTabBySessionId(match.tab.sessionId);
    }
  }

  window.openTabPalette = toggle;
})();
