/**
 * broadcast.js — Send one command to several devices at once.
 *
 * Compose-and-send rather than keystroke mirroring. Mirroring what you type
 * into every open tab is the usual implementation and it is the wrong one for
 * this: a stray keypress reaches the whole fleet, and you never see the
 * finished command before it lands. Here the command is written once, the
 * targets are listed by name, and the result of each is reported separately —
 * so a device that was disconnected shows up as a failure rather than being
 * quietly skipped.
 */
(function () {
  'use strict';

  let overlay, input, targets, results;

  document.addEventListener('DOMContentLoaded', () => {
    overlay = document.getElementById('broadcast-overlay');
    input   = document.getElementById('broadcast-command');
    targets = document.getElementById('broadcast-targets');
    results = document.getElementById('broadcast-results');
    if (!overlay) return;

    document.getElementById('sidebar-link-broadcast')
      .addEventListener('click', (e) => { e.preventDefault(); open(); });
    document.getElementById('broadcast-close').addEventListener('click', close);
    document.getElementById('broadcast-send').addEventListener('click', send);
    document.getElementById('broadcast-all').addEventListener('click', () => setAll(true));
    document.getElementById('broadcast-none').addEventListener('click', () => setAll(false));

    overlay.addEventListener('click', (e) => { if (e.target === overlay) close(); });
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && !overlay.classList.contains('hidden')) close();
      // Ctrl+Shift+B is the usual shortcut for this in terminal tools.
      if (e.ctrlKey && e.shiftKey && e.key.toLowerCase() === 'b') {
        e.preventDefault();
        overlay.classList.contains('hidden') ? open() : close();
      }
    });

    input.addEventListener('keydown', (e) => {
      // Enter sends; Shift+Enter is left alone so a multi-line paste survives.
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
    });
  });

  function openTabs() {
    return (typeof window.getOpenTabs === 'function') ? window.getOpenTabs() : [];
  }

  function open() {
    renderTargets();
    results.innerHTML = '';
    overlay.classList.remove('hidden');
    setTimeout(() => input.focus(), 50);
  }

  function close() {
    overlay.classList.add('hidden');
  }

  function renderTargets() {
    const tabs = openTabs();
    targets.innerHTML = '';

    if (!tabs.length) {
      const empty = document.createElement('div');
      empty.className = 'broadcast-empty';
      empty.textContent = 'No sessions open.';
      targets.appendChild(empty);
      return;
    }

    tabs.forEach(tab => {
      const row = document.createElement('label');
      row.className = 'broadcast-target' + (tab.isConnected ? '' : ' broadcast-target-down');

      const box = document.createElement('input');
      box.type = 'checkbox';
      box.dataset.sessionId = tab.sessionId;
      // A disconnected session cannot receive anything, so it starts
      // unticked rather than silently failing when Send is pressed.
      box.checked = tab.isConnected;
      box.disabled = !tab.isConnected;

      const name = document.createElement('span');
      name.textContent = tab.label || tab.hostname || tab.sessionId.slice(0, 8);

      const kind = document.createElement('span');
      kind.className = 'broadcast-kind';
      kind.textContent = tab.isConnected
        ? (tab.connectionType || 'ssh').toUpperCase()
        : 'DISCONNECTED';

      row.append(box, name, kind);
      targets.appendChild(row);
    });
  }

  function setAll(state) {
    targets.querySelectorAll('input[type=checkbox]:not(:disabled)')
      .forEach(box => { box.checked = state; });
  }

  function selected() {
    return [...targets.querySelectorAll('input[type=checkbox]:checked')]
      .map(box => box.dataset.sessionId);
  }

  async function send() {
    const command = input.value.trim();
    const ids = selected();

    results.innerHTML = '';
    if (!command) { report('error', 'Type a command to send.'); return; }
    if (!ids.length) { report('error', 'Select at least one session.'); return; }

    // Sending the same command to many devices at once is exactly the sort of
    // thing that is regretted afterwards, so name them and ask. The same names
    // are reused in the results, so what is confirmed and what is reported
    // cannot disagree — the server knows the target address, not the label the
    // user gave the tab.
    const names = {};
    [...targets.querySelectorAll('input[type=checkbox]:checked')].forEach(b => {
      names[b.dataset.sessionId] = b.parentElement.querySelector('span').textContent;
    });
    if (!window.confirm(
      `Send this to ${ids.length} device${ids.length === 1 ? '' : 's'}?\n\n` +
      `  ${command}\n\n` + ids.map(id => `  · ${names[id]}`).join('\n'))) {
      return;
    }

    const button = document.getElementById('broadcast-send');
    button.disabled = true;

    try {
      const res = await fetch('/api/broadcast', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ session_ids: ids, command, execute: true }),
      });
      const data = await res.json();
      if (!res.ok) { report('error', data.detail || 'Broadcast failed.'); return; }

      data.results.forEach(r => {
        report(r.ok ? 'ok' : 'error',
               `${names[r.session_id] || r.label}: ${r.ok ? `sent "${r.sent}"` : r.error}`);
      });
      report('muted', `${data.sent} of ${data.total} sent.`);
      input.value = '';
    } catch (e) {
      report('error', `Could not reach the server: ${e.message}`);
    } finally {
      button.disabled = false;
    }
  }

  function report(kind, text) {
    const row = document.createElement('div');
    row.className = `broadcast-result broadcast-${kind}`;
    const icon = document.createElement('span');
    icon.className = 'material-symbols-outlined';
    icon.textContent = kind === 'ok' ? 'check_circle' : kind === 'error' ? 'close' : 'list_alt';
    const label = document.createElement('span');
    // textContent — device labels and error text are not ours to trust.
    label.textContent = text;
    row.append(icon, label);
    results.appendChild(row);
  }

  window.openBroadcast = open;
})();
