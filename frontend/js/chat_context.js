/**
 * chat_context.js — Choose which sessions the assistant can see.
 *
 * The capability already existed as `/context all` and `/context 2`, typed
 * into the chat box. Nobody discovers that, so in practice the assistant only
 * ever saw the active tab — which is the least useful of the three when the
 * question is "why can A reach C but B cannot".
 *
 * This exposes the same thing as a picker: tick the sessions to include. The
 * default stays "whatever tab I am looking at", because that is right most of
 * the time and should not need a decision.
 */
(function () {
  'use strict';

  /**
   * Session ids the user has explicitly ticked.
   * Empty means "follow the active tab", which is the default.
   */
  let selected = new Set();

  let button, menu, label;

  document.addEventListener('DOMContentLoaded', () => {
    button = document.getElementById('chat-tabs-btn');
    menu   = document.getElementById('chat-tabs-menu');
    label  = document.getElementById('chat-tabs-label');
    if (!button || !menu) return;

    button.addEventListener('click', (e) => {
      e.stopPropagation();
      menu.classList.contains('hidden') ? open() : close();
    });

    document.addEventListener('click', (e) => {
      if (!menu.classList.contains('hidden') && !menu.contains(e.target) && e.target !== button) {
        close();
      }
    });

    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') close();
    });

    // A closed tab must not linger in the selection, or the assistant would
    // be asked about a session that no longer exists.
    window.addEventListener('mate:tab-switched', prune);
    setInterval(prune, 4000);
  });

  function openTabs() {
    return (typeof window.getOpenTabs === 'function') ? window.getOpenTabs() : [];
  }

  function prune() {
    const live = new Set(openTabs().map(t => t.sessionId));
    let changed = false;
    selected.forEach(id => {
      if (!live.has(id)) { selected.delete(id); changed = true; }
    });
    if (changed) render();
  }

  function open() {
    const tabs = openTabs();
    menu.innerHTML = '';

    if (!tabs.length) {
      const empty = document.createElement('div');
      empty.className = 'chat-tabs-empty';
      empty.textContent = 'No sessions open.';
      menu.appendChild(empty);
      menu.classList.remove('hidden');
      return;
    }

    const followRow = document.createElement('label');
    followRow.className = 'chat-tabs-row chat-tabs-follow';
    const followBox = document.createElement('input');
    followBox.type = 'radio';
    followBox.name = 'chat-context-mode';
    followBox.checked = selected.size === 0;
    followBox.addEventListener('change', () => {
      if (followBox.checked) { selected.clear(); render(); open(); }
    });
    const followText = document.createElement('span');
    followText.textContent = 'Follow the active tab';
    followRow.appendChild(followBox);
    followRow.appendChild(followText);
    menu.appendChild(followRow);

    const pickRow = document.createElement('label');
    pickRow.className = 'chat-tabs-row chat-tabs-follow';
    const pickBox = document.createElement('input');
    pickBox.type = 'radio';
    pickBox.name = 'chat-context-mode';
    pickBox.checked = selected.size > 0;
    pickBox.addEventListener('change', () => {
      if (pickBox.checked && selected.size === 0) {
        const active = typeof window.getActiveTab === 'function' ? window.getActiveTab() : null;
        if (active) selected.add(active.sessionId);
        render();
        open();
      }
    });
    const pickText = document.createElement('span');
    pickText.textContent = 'Choose sessions';
    pickRow.appendChild(pickBox);
    pickRow.appendChild(pickText);
    menu.appendChild(pickRow);

    const sep = document.createElement('div');
    sep.className = 'chat-tabs-sep';
    menu.appendChild(sep);

    tabs.forEach(tab => {
      const row = document.createElement('label');
      row.className = 'chat-tabs-row';

      const box = document.createElement('input');
      box.type = 'checkbox';
      box.checked = selected.has(tab.sessionId);
      box.addEventListener('change', () => {
        box.checked ? selected.add(tab.sessionId) : selected.delete(tab.sessionId);
        render();
      });

      const name = document.createElement('span');
      // textContent — a tab label comes from the device.
      name.textContent = tab.label || tab.hostname || tab.sessionId.slice(0, 8);

      const kind = document.createElement('span');
      kind.className = 'chat-tabs-kind';
      kind.textContent = (tab.connectionType || 'ssh').toUpperCase();

      row.appendChild(box);
      row.appendChild(name);
      row.appendChild(kind);
      menu.appendChild(row);
    });

    menu.classList.remove('hidden');
  }

  function close() {
    if (menu) menu.classList.add('hidden');
  }

  function render() {
    if (!label) return;
    if (selected.size === 0) {
      label.textContent = 'Active tab';
      button.classList.remove('chat-pill-active');
    } else {
      label.textContent = selected.size === 1 ? '1 session' : `${selected.size} sessions`;
      button.classList.add('chat-pill-active');
    }
    // The context indicator and token meter count the selection; a tick that
    // moved neither looked like the assistant never received it (#213).
    if (typeof window.updateContextStatus === 'function') window.updateContextStatus();
  }

  /**
   * What chat.js should send with the next message.
   *
   * Returns null when the user has not chosen, so the caller keeps its
   * existing "active tab" behaviour rather than this having to know about it.
   */
  window.getChatContextSelection = () => (selected.size ? [...selected] : null);
})();
