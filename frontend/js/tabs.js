/**
 * tabs.js — Tab bar management for ShellMate.
 *
 * Maintains an array of tab objects (one per session), handles creating,
 * switching and closing tabs, and updates the status bar.  The actual
 * xterm.js initialisation is delegated to terminal.js via initTerminal().
 *
 * Tab object structure:
 *   { sessionId, label, terminalInstance, fitAddon, websocket,
 *     isConnected, containerId }
 */

(function () {
  'use strict';

  // -------------------------------------------------------------------------
  // State
  // -------------------------------------------------------------------------

  /**
   * Ctrl-U: kill the current input line. Recognised by IOS, NX-OS, Junos
   * and readline shells alike. Written as an escape rather than a literal
   * control byte so the source file stays plain text.
   */
  const KILL_LINE = '\x15';

  /** @type {Array<Object>} All open tabs */
  const tabs = [];

  /** @type {number} Index of the currently visible tab (-1 = none) */
  let activeTabIndex = -1;

  /** @type {string|null} sessionId of the tab currently being dragged */
  let _dragSrcId = null;

  // -------------------------------------------------------------------------
  // DOM references
  // -------------------------------------------------------------------------

  let tabList, welcomeScreen, terminalsContainer;

  document.addEventListener('DOMContentLoaded', () => {
    tabList            = document.getElementById('tab-list');
    welcomeScreen      = document.getElementById('welcome-screen');
    terminalsContainer = document.getElementById('terminals-container');

    // Brand click → show welcome/home screen
    document.getElementById('tab-bar-brand').addEventListener('click', () => {
      // Hide all terminal containers so the welcome screen shows through
      tabs.forEach(tab => {
        const c = document.getElementById(tab.containerId);
        if (c) c.classList.remove('active');
      });
      tabs.forEach(tab => tab.tabEl.classList.remove('active'));
      activeTabIndex = -1;
      welcomeScreen.classList.remove('hidden');
      if (typeof window.renderWelcomeProfiles === 'function') window.renderWelcomeProfiles();
    });

    // Right-click the brand for a quicker route than the welcome screen.
    document.getElementById('tab-bar-brand')
      .addEventListener('contextmenu', _showBrandContextMenu);

    // Keyboard shortcuts
    document.addEventListener('keydown', handleKeyboard);

    // Initial status bar
    updateStatusBar();
  });

  // -------------------------------------------------------------------------
  // Public API
  // -------------------------------------------------------------------------

  /**
   * Create a new tab for an established session.
   *
   * Called by connections.js after a successful POST /api/sessions.
   *
   * @param {Object} sessionData - Session metadata returned by the backend.
   *   Must include: session_id, display_label, hostname, connection_type,
   *   connected_at, is_connected.
   */
  function createTab(sessionData) {
    const { session_id, display_label, hostname } = sessionData;
    const label = display_label || hostname || session_id.slice(0, 8);

    // Build the tab DOM element
    const tabEl = document.createElement('div');
    tabEl.className = 'tab';
    tabEl.dataset.sessionId = session_id;

    const dot = document.createElement('span');
    dot.className = 'tab-dot';

    const labelEl = document.createElement('span');
    labelEl.className = 'tab-label';
    labelEl.textContent = label;
    labelEl.title = label;

    const closeBtn = document.createElement('button');
    closeBtn.className = 'tab-close';
    closeBtn.textContent = 'x';
    closeBtn.title = 'Close tab (Ctrl+W)';

    tabEl.appendChild(dot);
    tabEl.appendChild(labelEl);
    tabEl.appendChild(closeBtn);
    tabList.appendChild(tabEl);

    // Initialise terminal and WebSocket (defined in terminal.js)
    const termData = window.initTerminal(session_id);

    const tabObj = {
      sessionId:        session_id,
      label,
      // Transport backing this tab. The file browser needs it to explain why
      // a serial or telnet tab cannot transfer files.
      connectionType:   sessionData.connection_type || 'ssh',
      hostname:         hostname || '',
      terminalInstance: termData.terminal,
      fitAddon:         termData.fitAddon,
      websocket:        termData.websocket,
      getBufferLines:   termData.getBufferLines,
      getContextChars:  termData.getContextChars,
      isConnected:      true,
      containerId:      termData.containerId,
      tabEl,
      labelEl,
    };

    tabs.push(tabObj);
    const newIndex = tabs.length - 1;

    // Snapshot the config and report what has changed since the last visit.
    // Best-effort and silent when the device cannot support it.
    if (typeof window.checkDrift === 'function') window.checkDrift(sessionData);

    // Wire up click events — always look up current index (drag may have moved it)
    tabEl.addEventListener('click', (e) => {
      if (e.target === closeBtn) return;
      const idx = tabs.findIndex(t => t.sessionId === session_id);
      if (idx !== -1) switchToTab(idx);
    });

    closeBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      const idx = tabs.findIndex(t => t.sessionId === session_id);
      if (idx !== -1) closeTab(idx);
    });

    // Right-click: show context menu
    tabEl.addEventListener('contextmenu', (e) => {
      _showTabContextMenu(e, session_id);
    });

    // Drag to reorder
    _bindDrag(tabEl, session_id);

    // Hide the welcome screen now that we have a tab
    welcomeScreen.classList.add('hidden');

    // Switch to the new tab
    switchToTab(newIndex);
  }

  /**
   * Switch the visible terminal to the tab at `index`.
   *
   * Hides all other terminal containers and marks the tab as active.
   *
   * @param {number} index
   */
  function switchToTab(index) {
    if (index < 0 || index >= tabs.length) return;

    // Hide welcome screen when switching to a real tab
    welcomeScreen.classList.add('hidden');

    // Deactivate all tabs and hide all terminals
    tabs.forEach((tab, i) => {
      tab.tabEl.classList.toggle('active', i === index);
      const container = document.getElementById(tab.containerId);
      if (container) {
        container.classList.toggle('active', i === index);
      }
    });

    activeTabIndex = index;

    // Notify chat.js (and anything else) that the active tab changed
    window.dispatchEvent(new CustomEvent('mate:tab-switched', { detail: tabs[index] }));

    // Let xterm.js recalculate dimensions after becoming visible
    const activeTab = tabs[index];
    if (activeTab && activeTab.fitAddon) {
      requestAnimationFrame(() => {
        try {
          activeTab.fitAddon.fit();
        } catch (_) { /* ignore if terminal not ready */ }
      });
    }

    updateStatusBar();
  }

  /**
   * Close the tab at `index`.
   *
   * Sends DELETE /api/sessions/{id}, closes the WebSocket, disposes the
   * xterm.js terminal, removes the DOM element, and switches to an adjacent
   * tab (or shows the welcome screen if none remain).
   *
   * @param {number} index
   */
  function closeTab(index) {
    if (index < 0 || index >= tabs.length) return;

    const tab = tabs[index];
    const { sessionId, websocket, terminalInstance, containerId, tabEl } = tab;

    // Tell the backend to tear down the session
    fetch(`/api/sessions/${sessionId}`, { method: 'DELETE' }).catch(() => {
      // Best-effort — don't block UI on network error
    });

    // Close WebSocket
    try { websocket.close(); } catch (_) {}

    // Dispose terminal instance
    try { terminalInstance.dispose(); } catch (_) {}

    // Remove terminal container from DOM
    const container = document.getElementById(containerId);
    if (container) container.remove();

    // Remove tab DOM element
    tabEl.remove();

    // Remove from array
    tabs.splice(index, 1);

    // Decide what to show next
    if (tabs.length === 0) {
      activeTabIndex = -1;
      welcomeScreen.classList.remove('hidden');
    } else {
      // Switch to the tab to the left, or the first one
      const nextIndex = Math.min(index, tabs.length - 1);
      switchToTab(nextIndex);
    }

    updateStatusBar();
  }

  /**
   * Return the currently active tab object, or null.
   * @returns {Object|null}
   */
  function getActiveTab() {
    if (activeTabIndex < 0 || activeTabIndex >= tabs.length) return null;
    return tabs[activeTabIndex];
  }

  /**
   * Update a tab's label text.
   * @param {string} sessionId
   * @param {string} label
   */
  function updateTabLabel(sessionId, label) {
    const tab = tabs.find(t => t.sessionId === sessionId);
    if (!tab) return;
    tab.label = label;
    tab.labelEl.textContent = label;
    tab.labelEl.title = label;
    updateStatusBar();
  }

  /**
   * Mark a tab as connected or disconnected.
   * @param {string} sessionId
   * @param {boolean} isConnected
   */
  function updateTabStatus(sessionId, isConnected) {
    const tab = tabs.find(t => t.sessionId === sessionId);
    if (!tab) return;
    tab.isConnected = isConnected;
    tab.tabEl.classList.toggle('disconnected', !isConnected);
    if (!isConnected) {
      tab.labelEl.textContent = tab.label + ' (disconnected)';
    }
    updateStatusBar();
  }

  /**
   * Refresh the status bar with information about the active session.
   */
  function updateStatusBar() {
    const connEl   = document.getElementById('status-connection');
    const bufferEl = document.getElementById('status-buffer');
    const tabsEl   = document.getElementById('status-tabs');

    tabsEl.textContent = `Tabs: ${tabs.length}`;

    const active = getActiveTab();
    if (!active) {
      connEl.textContent  = 'No active session';
      bufferEl.textContent = 'Buffer: 0L';
      return;
    }

    const stateText = active.isConnected ? 'Connected' : 'Disconnected';
    connEl.textContent = `SSH: ${active.label} | ${stateText}`;

    const lines = active.getBufferLines ? active.getBufferLines() : 0;
    bufferEl.textContent = `Buffer: ${lines.toLocaleString()}L`;

    if (typeof window.updateContextStatus === 'function') window.updateContextStatus();
  }

  // -------------------------------------------------------------------------
  // Keyboard shortcuts
  // -------------------------------------------------------------------------

  function handleKeyboard(e) {
    // Ctrl+T — new tab
    if (e.ctrlKey && e.key === 't') {
      e.preventDefault();
      if (typeof window.showConnectionDialog === 'function') {
        window.showConnectionDialog();
      }
      return;
    }

    // Ctrl+W — close active tab
    if (e.ctrlKey && e.key === 'w') {
      // Only intercept when a terminal is active (not when a form has focus)
      if (document.activeElement && document.activeElement.tagName === 'INPUT') return;
      e.preventDefault();
      if (activeTabIndex >= 0) closeTab(activeTabIndex);
      return;
    }

    // Ctrl+1 through Ctrl+9 — switch to tab N
    if (e.ctrlKey && e.key >= '1' && e.key <= '9') {
      const targetIndex = parseInt(e.key, 10) - 1;
      if (targetIndex < tabs.length) {
        e.preventDefault();
        switchToTab(targetIndex);
      }
    }
  }

  // -------------------------------------------------------------------------
  // Drag-to-reorder
  // -------------------------------------------------------------------------

  function _bindDrag(tabEl, sessionId) {
    tabEl.setAttribute('draggable', 'true');

    tabEl.addEventListener('dragstart', (e) => {
      _dragSrcId = sessionId;
      e.dataTransfer.effectAllowed = 'move';
      e.dataTransfer.setData('text/plain', sessionId);
      // Slight delay so the ghost image renders before we dim the element
      requestAnimationFrame(() => tabEl.classList.add('dragging'));
    });

    tabEl.addEventListener('dragend', () => {
      tabEl.classList.remove('dragging');
      tabList.querySelectorAll('.tab').forEach(t => t.classList.remove('drag-over'));
      _dragSrcId = null;
    });

    tabEl.addEventListener('dragover', (e) => {
      e.preventDefault();
      e.dataTransfer.dropEffect = 'move';
      if (!_dragSrcId || _dragSrcId === sessionId) return;
      tabList.querySelectorAll('.tab').forEach(t => t.classList.remove('drag-over'));
      tabEl.classList.add('drag-over');
    });

    tabEl.addEventListener('dragleave', (e) => {
      // Only remove if leaving to something outside this tab
      if (!tabEl.contains(e.relatedTarget)) {
        tabEl.classList.remove('drag-over');
      }
    });

    tabEl.addEventListener('drop', (e) => {
      e.preventDefault();
      tabEl.classList.remove('drag-over');
      if (!_dragSrcId || _dragSrcId === sessionId) return;

      const srcIdx = tabs.findIndex(t => t.sessionId === _dragSrcId);
      const dstIdx = tabs.findIndex(t => t.sessionId === sessionId);
      if (srcIdx === -1 || dstIdx === -1) return;

      // Reorder the tabs array
      const [moved] = tabs.splice(srcIdx, 1);
      tabs.splice(dstIdx, 0, moved);

      // Reorder the DOM to match
      if (dstIdx > srcIdx) {
        tabList.insertBefore(moved.tabEl, tabEl.nextSibling);
      } else {
        tabList.insertBefore(moved.tabEl, tabEl);
      }

      // Keep activeTabIndex correct
      const activeSession = getActiveTab();
      if (activeSession) {
        activeTabIndex = tabs.findIndex(t => t.sessionId === activeSession.sessionId);
      }
    });
  }

  // -------------------------------------------------------------------------
  // Tab right-click context menu
  // -------------------------------------------------------------------------

  let _ctxMenu      = null;
  let _ctxSessionId = null;

  /**
   * Show the context menu near the cursor for a given session.
   */
  function _showTabContextMenu(e, sessionId) {
    e.preventDefault();
    _hideTabContextMenu();
    _ctxSessionId = sessionId;

    const tab = tabs.find(t => t.sessionId === sessionId);
    const disconnected = tab && !tab.isConnected;

    _ctxMenu = document.createElement('div');
    _ctxMenu.className = 'tab-context-menu';
    _ctxMenu.innerHTML = `
      ${disconnected ? `
      <button data-action="reconnect">
        <span class="material-symbols-outlined">add_circle</span>
        Reconnect
      </button>
      <div class="ctx-sep"></div>` : ''}
      <button data-action="clear">
        <span class="material-symbols-outlined">backspace</span>
        Clear console
      </button>
      <button data-action="copy">
        <span class="material-symbols-outlined">content_copy</span>
        Copy history
      </button>
      <div class="ctx-sep"></div>
      <button data-action="duplicate">
        <span class="material-symbols-outlined">tab_duplicate</span>
        Duplicate session
      </button>
    `;

    document.body.appendChild(_ctxMenu);

    // Position near cursor, clamped to viewport
    const x = Math.min(e.clientX, window.innerWidth  - 200);
    const y = Math.min(e.clientY, window.innerHeight - 160);
    _ctxMenu.style.left = `${x}px`;
    _ctxMenu.style.top  = `${y}px`;

    _ctxMenu.addEventListener('click', (ev) => {
      const btn = ev.target.closest('[data-action]');
      if (!btn) return;
      const tab = tabs.find(t => t.sessionId === _ctxSessionId);
      if (tab) {
        switch (btn.dataset.action) {
          case 'reconnect': _reconnectSession(tab);   break;
          case 'clear':     _clearConsole(tab);       break;
          case 'copy':      _copyHistory(tab);        break;
          case 'duplicate': _duplicateSession(tab);   break;
        }
      }
      _hideTabContextMenu();
    });

    // Dismiss on outside click or Escape
    setTimeout(() => {
      document.addEventListener('click',   _hideTabContextMenu, { once: true });
      document.addEventListener('keydown', (ev) => {
        if (ev.key === 'Escape') _hideTabContextMenu();
      }, { once: true });
    }, 0);
  }

  function _hideTabContextMenu() {
    if (_ctxMenu) { _ctxMenu.remove(); _ctxMenu = null; }
  }

  /**
   * Clear the console: both the display and whatever is half-typed.
   *
   * terminal.clear() only wipes what ShellMate has drawn. Anything already
   * typed at the prompt lives on the *device's* input line, so it survives
   * and reappears on the fresh screen. Ctrl-U is the kill-line on IOS, NX-OS,
   * Junos and readline shells alike, so send that too and let the device
   * redraw an empty prompt.
   */
  function _clearConsole(tab) {
    try { tab.terminalInstance.clear(); } catch (_) {}
    try {
      if (tab.isConnected && tab.websocket && tab.websocket.readyState === WebSocket.OPEN) {
        tab.websocket.send(JSON.stringify({ type: 'input', data: KILL_LINE }));
      }
    } catch (_) {}
  }

  /** Copy all lines from the terminal buffer to clipboard. */
  function _copyHistory(tab) {
    try {
      const buf   = tab.terminalInstance.buffer.active;
      const lines = [];
      for (let i = 0; i < buf.length; i++) {
        const line = buf.getLine(i);
        if (line) lines.push(line.translateToString(true));
      }
      // Trim trailing blank lines
      while (lines.length && !lines[lines.length - 1].trim()) lines.pop();
      navigator.clipboard.writeText(lines.join('\n')).then(() => {
        window._showCopyToast && window._showCopyToast();
      }).catch(() => {});
    } catch (err) {
      console.error('Could not copy terminal history:', err);
    }
  }

  /** Open the connection dialog pre-filled with this session's details. */
  /**
   * Context menu on the brand: new session, or straight to a saved one.
   *
   * The saved connections are listed inline rather than behind a submenu —
   * reconnecting to a device you use daily should be one gesture, not three.
   */
  async function _showBrandContextMenu(e) {
    e.preventDefault();
    _hideTabContextMenu();

    let profiles = [];
    try {
      const res = await fetch('/api/profiles');
      if (res.ok) profiles = await res.json();
    } catch (_) { /* the New session option still works */ }

    _ctxMenu = document.createElement('div');
    _ctxMenu.className = 'tab-context-menu';

    const newBtn = document.createElement('button');
    newBtn.dataset.action = 'new';
    newBtn.innerHTML =
      '<span class="material-symbols-outlined">add_circle</span> New session';
    _ctxMenu.appendChild(newBtn);

    if (profiles.length) {
      const sep = document.createElement('div');
      sep.className = 'ctx-sep';
      _ctxMenu.appendChild(sep);

      const heading = document.createElement('div');
      heading.className = 'ctx-heading';
      heading.textContent = 'Saved connections';
      _ctxMenu.appendChild(heading);

      profiles.slice(0, 12).forEach(p => {
        const btn = document.createElement('button');
        btn.dataset.action = 'open';
        btn.dataset.profileId = p.id;
        const icon = document.createElement('span');
        icon.className = 'material-symbols-outlined';
        icon.textContent = p.connection_type === 'serial' ? 'cable' : 'terminal';
        const label = document.createElement('span');
        // textContent — a profile name is user input.
        label.textContent = p.name || p.hostname || p.serial_port || 'unnamed';
        btn.appendChild(icon);
        btn.appendChild(label);
        _ctxMenu.appendChild(btn);
      });
    }

    document.body.appendChild(_ctxMenu);
    _ctxMenu.style.left = `${Math.min(e.clientX, window.innerWidth - 240)}px`;
    _ctxMenu.style.top  = `${Math.min(e.clientY, window.innerHeight - 60 - _ctxMenu.offsetHeight)}px`;

    _ctxMenu.addEventListener('click', (ev) => {
      const btn = ev.target.closest('[data-action]');
      if (!btn) return;
      if (btn.dataset.action === 'new') {
        if (typeof window.showConnectionDialog === 'function') window.showConnectionDialog();
      } else if (btn.dataset.action === 'open') {
        const profile = profiles.find(p => p.id === btn.dataset.profileId);
        if (profile && typeof window.showConnectionDialog === 'function') {
          window.showConnectionDialog(profile);
        }
      }
      _hideTabContextMenu();
    });

    setTimeout(() => {
      document.addEventListener('click', _hideTabContextMenu, { once: true });
      document.addEventListener('keydown', (ev) => {
        if (ev.key === 'Escape') _hideTabContextMenu();
      }, { once: true });
    }, 0);
  }

  /**
   * Reconnect a dropped session, reusing the details already on the tab.
   *
   * Tries silently first: if the device has a saved profile with credentials
   * in the vault, the backend can fill them in and the session comes straight
   * back. Only when that is not possible does the dialog appear, pre-filled,
   * so the user types a password rather than everything.
   *
   * The old tab is closed only once the new one is up — a failed reconnect
   * must not also lose the buffer you were reading.
   */
  async function _reconnectSession(tab) {
    const index = tabs.findIndex(t => t.sessionId === tab.sessionId);

    let profile = null;
    try {
      const res = await fetch('/api/profiles');
      const profiles = res.ok ? await res.json() : [];
      profile = profiles.find(p =>
        (p.connection_type || 'ssh') === (tab.connectionType || 'ssh') &&
        (p.hostname === tab.hostname || p.name === tab.label)) || null;
    } catch (_) { /* fall through to the dialog */ }

    if (profile && profile.has_saved_credentials) {
      try {
        const res = await fetch('/api/sessions', {
          method:  'POST',
          headers: { 'Content-Type': 'application/json' },
          body:    JSON.stringify({
            connection_type: profile.connection_type || 'ssh',
            hostname:        profile.hostname || '',
            port:            profile.port || 22,
            username:        profile.username || '',
            serial_port:     profile.serial_port || '',
            baud_rate:       profile.baud_rate || 9600,
            display_label:   profile.name || '',
            profile_id:      profile.id,
          }),
        });
        if (res.ok) {
          const data = await res.json();
          if (index !== -1) closeTab(index);
          createTab(data);
          return;
        }
      } catch (_) { /* fall through to the dialog */ }
    }

    if (typeof window.showConnectionDialog === 'function') {
      window.showConnectionDialog(profile || {
        name:            tab.label,
        hostname:        tab.hostname,
        connection_type: tab.connectionType || 'ssh',
      });
    }
  }

  async function _duplicateSession(tab) {
    try {
      const res      = await fetch('/api/sessions');
      const sessions = await res.json();
      const s        = sessions.find(s => s.session_id === tab.sessionId);
      if (s && typeof window.showConnectionDialog === 'function') {
        window.showConnectionDialog({
          label:           s.display_label || '',
          hostname:        s.hostname      || '',
          port:            s.port          || 22,
          username:        s.username      || '',
          connection_type: s.connection_type || 'ssh',
        });
      }
    } catch (err) {
      console.error('Could not get session for duplicate:', err);
    }
  }

  // -------------------------------------------------------------------------
  // Expose to global scope
  // -------------------------------------------------------------------------

  /** Return an array of all open session IDs in current tab order. */
  window.getOpenSessionIds = () => tabs.map(t => t.sessionId);

  /**
   * Open tabs as plain data, for the chat context picker.
   *
   * Deliberately not the tab objects themselves: those carry live xterm and
   * WebSocket handles that nothing outside this module should be reaching
   * into.
   */
  window.getOpenTabs = () => tabs.map(t => ({
    sessionId:      t.sessionId,
    label:          t.label,
    hostname:       t.hostname,
    connectionType: t.connectionType,
    isConnected:    t.isConnected,
  }));

  /** Return the tab object at 1-based tab number, or null. */
  window.getTabByNumber = (n) => tabs[n - 1] || null;

  window.createTab        = createTab;
  window.switchToTab      = switchToTab;
  window.switchToTabBySessionId = (sessionId) => {
    const idx = tabs.findIndex(t => t.sessionId === sessionId);
    if (idx !== -1) switchToTab(idx);
  };
  window.closeTab         = closeTab;
  window.getActiveTab     = getActiveTab;
  window.updateTabLabel   = updateTabLabel;
  window.updateTabStatus  = updateTabStatus;
  window.updateStatusBar  = updateStatusBar;

})();
