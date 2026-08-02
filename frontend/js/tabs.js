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

    // The Sessions rail link had no handler at all — permanently marked
    // active and doing nothing. It is the way back to the dashboard now.
    const sessionsLink = document.getElementById('sidebar-link-sessions');
    if (sessionsLink) {
      sessionsLink.addEventListener('click', (e) => {
        e.preventDefault();
        if (dashboardVisible() && tabs.length) hideDashboard();
        else showDashboard();
      });
    }
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
      if (window.shellmateLayout) window.shellmateLayout.clear();
      welcomeScreen.classList.remove('hidden');
      if (typeof window.renderWelcomeProfiles === 'function') window.renderWelcomeProfiles();
    });

    // Right-click the brand for a quicker route than the welcome screen.
    document.getElementById('tab-bar-brand')
      .addEventListener('contextmenu', _showBrandContextMenu);

    // Keyboard shortcuts
    document.addEventListener('keydown', handleKeyboard);

    // Mark the tabs that are on screen alongside the active one.
    window.addEventListener('shellmate:layout-rendered', (e) => {
      const shown = new Set((e.detail && e.detail.visible) || []);
      tabs.forEach(tab => {
        tab.tabEl.classList.toggle(
          'tiled', shown.has(tab.sessionId) && !tab.tabEl.classList.contains('active'));
      });
    });

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
      // When this tab was opened. The array order answers the same question
      // until somebody drags or sorts, at which point it no longer does.
      openedAt:         Date.now(),
      // Transport backing this tab. The file browser needs it to explain why
      // a serial or telnet tab cannot transfer files.
      connectionType:   sessionData.connection_type || 'ssh',
      hostname:         hostname || '',
      // What was dialled, never rewritten. `hostname` above is replaced with
      // whatever the device calls itself the moment it says so, which is
      // precisely when the address stops being visible anywhere — so the menu
      // that offers to copy it needs its own copy.
      address:          sessionData.address || hostname || '',
      // Kept so a reconnect can tell twenty saved connections on 127.0.0.1
      // apart. Hostname alone does not.
      port:             sessionData.port || 0,
      username:         sessionData.username || '',
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
    // Placed where it belongs rather than always at the end. Sorting only on
    // the settings change would mean the order was right until the next tab
    // opened, which is worse than not sorting at all.
    _learnTag(tabObj);
    sortTabs();
    _rememberOpenTabs();

    // The session clock. connected_at is stamped by the backend, so the count
    // is from when the device answered rather than from when this ran.
    if (window.shellmateUptime) {
      window.shellmateUptime.start(session_id, sessionData.connected_at);
    }

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

    // Clicking into a terminal focuses it. Under a tiled layout that is the
    // gesture people expect from every other tiling window manager, and
    // without it typing would go to whichever pane was focused last.
    const containerEl = document.getElementById(termData.containerId);
    if (containerEl) {
      containerEl.addEventListener('mousedown', () => {
        const idx = tabs.findIndex(t => t.sessionId === session_id);
        if (idx !== -1 && idx !== activeTabIndex) switchToTab(idx);
      });
    }

    // Drag to reorder
    _bindDrag(tabEl, session_id);

    // A new tab takes you to it, so the dashboard steps aside.
    welcomeScreen.classList.add('hidden');
    if (terminalsContainer) terminalsContainer.classList.remove('behind-dashboard');
    _markSessionsLink(false);

    // Switch to the new tab.
    //
    // Looked up rather than reusing newIndex: a sort runs between the two, so
    // the position this tab was pushed to is not necessarily the one it now
    // occupies. Using the stale index would open a tab and activate a
    // different one — most likely under any ordering but 'opened'.
    const index = tabs.findIndex(t => t.sessionId === session_id);
    switchToTab(index === -1 ? newIndex : index);
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

    // Selecting a tab is the way back from the dashboard.
    welcomeScreen.classList.add('hidden');
    if (terminalsContainer) terminalsContainer.classList.remove('behind-dashboard');
    _markSessionsLink(false);

    activeTabIndex = index;

    // Which terminals are on screen is the layout's business, not this
    // module's — under a tiled layout several are visible at once and the tab
    // being switched to may already be one of them. The strip still marks
    // exactly one tab active, because exactly one has the keyboard.
    tabs.forEach((tab, i) => {
      tab.tabEl.classList.toggle('active', i === index);
      const container = document.getElementById(tab.containerId);
      if (container) container.classList.toggle('tab-current', i === index);
    });

    if (window.shellmateLayout) {
      window.shellmateLayout.focus(tabs[index].sessionId);
    } else {
      tabs.forEach((tab, i) => {
        const container = document.getElementById(tab.containerId);
        if (container) container.classList.toggle('active', i === index);
      });
    }

    // Notify chat.js (and anything else) that the active tab changed
    window.dispatchEvent(new CustomEvent('mate:tab-switched', { detail: tabs[index] }));

    // Let xterm.js recalculate dimensions after becoming visible
    refitTerminals(window.shellmateLayout
      ? window.shellmateLayout.visible()
      : [tabs[index].sessionId]);

    // The terminal only receives keystrokes when it has the focus, and a tab
    // switched to from the strip, a shortcut or a pane click should be ready
    // to type into without a further click into the terminal itself.
    const active = tabs[index];
    if (active && active.terminalInstance) {
      requestAnimationFrame(() => { try { active.terminalInstance.focus(); } catch (_) {} });
    }

    updateStatusBar();
  }

  /** sessionId → was that terminal following the tail when the refit was asked for. */
  const _pendingRefit = new Map();
  let _refitScheduled = false;

  /**
   * Re-measure the given terminals and tell each device its new size.
   *
   * A terminal that is resized without the far end being told keeps sending
   * output wrapped for the old width, which on a device paging through a
   * configuration produces a screen of ragged half-lines.
   *
   * Calls made in the same tick are collapsed into one. Switching tab under a
   * tiled layout used to ask twice — once from the layout re-rendering, once
   * from the switch itself — and xterm adjusts the scroll position on every
   * resize, so the second one moved the viewport a second time and left the
   * newest output above the fold.
   */
  function refitTerminals(sessionIds) {
    // Whether a terminal was following the tail has to be read now, not in the
    // callback. Between the two, the browser lays out the new pane geometry
    // and clamps the scroll container to its new height, which moves the
    // viewport — so by the time the fit runs, a terminal that was pinned to
    // the bottom no longer looks like it was. First observation wins, since
    // the earliest one is the only one taken before any of that has happened.
    (sessionIds || []).forEach(id => {
      if (_pendingRefit.has(id)) return;
      const tab = tabs.find(t => t.sessionId === id);
      let atBottom = true;
      try {
        const buf = tab.terminalInstance.buffer.active;
        atBottom = buf.viewportY >= buf.baseY;
      } catch (_) { /* not ready; treat as following */ }
      _pendingRefit.set(id, atBottom);
    });

    if (_refitScheduled) return;
    _refitScheduled = true;

    requestAnimationFrame(() => {
      _refitScheduled = false;
      const wanted = new Map(_pendingRefit);
      _pendingRefit.clear();
      wanted.forEach((atBottom, id) => {
        const tab = tabs.find(t => t.sessionId === id);
        if (tab) _fitOne(tab, 0, atBottom);
      });
    });
  }

  /**
   * Fit one terminal, retrying briefly while it has no measurable size.
   *
   * xterm measures its character cell on its first render. Asked before that
   * has happened — which is the case on the frame a terminal is created —
   * proposeDimensions() returns nothing and fit() quietly does nothing at all.
   * The terminal then stays at the 80x24 default for the life of the session
   * while its pane is far wider, and the device wraps its output to 80 columns
   * in a window with room for a hundred. It fails silently, which is why it
   * went unnoticed; hence the retry rather than a single attempt.
   */
  function _fitOne(tab, attempt, wasAtBottom) {
    if (!tab.fitAddon || !tab.terminalInstance) return;

    let dims;
    try { dims = tab.fitAddon.proposeDimensions(); } catch (_) { dims = null; }

    if (!dims || !dims.cols || !dims.rows) {
      if (attempt < 6) setTimeout(() => _fitOne(tab, attempt + 1, wasAtBottom), 40);
      return;
    }

    const term = tab.terminalInstance;
    if (dims.cols === term.cols && dims.rows === term.rows) return;

    try {
      // Resizing reflows the buffer and moves the viewport with it, which
      // after growing a pane can strand the newest output below the fold —
      // the session looks stuck. Someone who had scrolled up to read something
      // stays where they were; only a terminal already following the tail
      // keeps following it.
      tab.fitAddon.fit();

      // Not immediately, and not once: xterm re-syncs the viewport after the
      // reflow settles, and a scroll issued before that is overwritten by it.
      // Re-asserting over a few frames costs nothing and is not sensitive to
      // exactly which frame the reflow lands on.
      if (wasAtBottom) {
        [0, 1, 2].forEach(n => setTimeout(() => {
          try {
            const b = term.buffer.active;
            if (b.viewportY < b.baseY) term.scrollToBottom();
          } catch (_) {}
        }, n * 60));
      }

      if (tab.websocket && tab.websocket.readyState === WebSocket.OPEN) {
        tab.websocket.send(JSON.stringify({
          type: 'resize', cols: term.cols, rows: term.rows,
        }));
      }
    } catch (_) { /* terminal disposed mid-flight */ }
  }

  /**
   * Close the tab at `index`.
   *
   * Sends DELETE /api/sessions/{id}, closes the WebSocket, disposes the
   * xterm.js terminal, removes the DOM element, and switches to an adjacent
   * tab (or shows the welcome screen if none remain).
   *
   * Async because the confirmation is ShellMate's own dialog rather than the
   * browser's blocking one. Every caller fires and forgets, which is what
   * they did before — the difference is that the rest of the interface keeps
   * running while the question is on screen.
   *
   * @param {number} index
   */
  async function closeTab(index, options) {
    if (index < 0 || index >= tabs.length) return;

    const tab = tabs[index];
    // Whatever happens next, this drop is ours. Set before the confirmation
    // rather than after: the socket can close while the dialog is open.
    tab.closingDeliberately = true;
    _stopRetrying(tab, '');

    // Closing a tab tears down the session on the other end of it. A
    // disconnected one has nothing left to lose and closes without a word —
    // a confirmation that always appears is one people learn to click through.
    const settings = (window.shellmateSettings || {}).interface || {};
    const ask = settings.confirm_close_tab !== false;
    if (ask && tab.isConnected && !(options && options.force)) {
      const ok = await window.shellmateDialog.confirm({
        title: `Close ${tab.label}?`,
        body: 'The session is still connected and will be disconnected. ' +
              'Anything already scheduled on the device — a pending reload — ' +
              'carries on regardless.',
        confirmLabel: 'Close tab',
      });
      if (!ok) return;
    }

    const { sessionId, websocket, terminalInstance, containerId, tabEl } = tab;

    // The terminal, its addons, its socket and its resize listener go too.
    // Without this every terminal ever opened stayed reachable, and — worse —
    // applySettingsToAll() would later call terminal.options on a disposed
    // instance, throw, and stop applying settings to every tab after it.
    if (window.forgetTerminal) window.forgetTerminal(sessionId);

    // The clock goes with the tab, so a closed session stops costing a tick.
    if (window.shellmateUptime) window.shellmateUptime.forget(sessionId);

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
    _rememberOpenTabs();

    // Free the pane it occupied before anything is asked to re-render, so a
    // tiled layout pulls in a waiting session rather than leaving a hole.
    if (window.shellmateLayout) window.shellmateLayout.forget(sessionId);

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
      // Decide whether to start retrying. Everything about *whether* lives in
      // there — this is simply the one place that hears about every drop,
      // however it happened.
      _maybeAutoReconnect(tab);
    } else {
      _stopRetrying(tab, '');
      tab.labelEl.title = '';
    }
    // Freeze the clock rather than let it run on: a tab counting up on a dead
    // session states something untrue.
    if (window.shellmateUptime) {
      isConnected ? window.shellmateUptime.restart(sessionId)
                  : window.shellmateUptime.stop(sessionId);
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
  // Remembering, and restoring, what was open
  //
  // Every launch started empty. That is right for a tool you dip into and
  // wrong for one somebody has open all day across the same twelve devices.
  //
  // The objection in the issue is the real one and is not dismissed here:
  // reopening tabs means connecting to devices nobody asked to connect to,
  // which is the same reason auto-reconnect is off by default. So this is
  // opt-in, it only restores connections whose credentials the *server*
  // already holds, and it says which ones it could not — rather than quietly
  // restoring nine of twelve and leaving somebody to notice.
  // -------------------------------------------------------------------------

  /**
   * Record the open tabs, so a restart can offer them back.
   *
   * Written on every change rather than at quit: the process can be killed,
   * the machine can lose power, and a list that is only correct after a clean
   * shutdown is a list that fails exactly when it would have been most
   * welcome. prefs.set debounces, so a burst of tab changes is one write.
   *
   * Session ids are deliberately not stored. They do not survive a restart,
   * and storing them would invite code that looks them up and silently finds
   * nothing.
   */
  function _rememberOpenTabs() {
    if (!window.shellmatePrefs) return;
    window.shellmatePrefs.set('open_tabs', tabs.map(t => ({
      label:           t.label,
      hostname:        t.hostname || '',
      port:            t.port || 0,
      username:        t.username || '',
      connection_type: t.connectionType || 'ssh',
    })));
  }

  /**
   * Reopen what was open, if that was asked for.
   *
   * Runs once, after settings have arrived. Each entry is matched to a saved
   * connection the same way Duplicate and auto-reconnect do — the server fills
   * the credentials in from the profile, because scrub_secrets() clears them
   * from the session the moment it connects and the browser has never had
   * them.
   */
  async function _restoreTabs() {
    const prefs = (window.shellmateSettings || {}).interface || {};
    if (prefs.restore_tabs !== true) return;

    const remembered = prefs.open_tabs || [];
    if (!remembered.length) return;

    const unsaved = [];
    const noPassword = [];
    let restored = 0;

    for (const entry of remembered) {
      const profile = await _exactProfileFor(entry);

      // Without saved credentials there is nothing to connect with, and
      // twelve password prompts on startup is not a feature. Named instead —
      // and the two reasons are kept apart, because they need different
      // things done about them: one wants the connection saved, the other
      // wants a password added to a connection that already exists.
      if (!profile) {
        unsaved.push(entry.label || entry.hostname || 'a session');
        continue;
      }
      if (!profile.has_saved_credentials) {
        noPassword.push(entry.label || entry.hostname || 'a session');
        continue;
      }

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
            display_label:   profile.name || entry.label || '',
            profile_id:      profile.id,
          }),
        });
        if (!res.ok) throw new Error(String(res.status));
        createTab(await res.json());
        restored += 1;
      } catch (_) {
        noPassword.push(entry.label || entry.hostname || 'a session');
      }
    }

    if (!window.shellmateAlerts) return;

    // Counted, and the failures named. "Restored 9 of 12" with no list is
    // worse than saying nothing: it tells somebody three devices are missing
    // without telling them which three.
    const problems = [];
    if (unsaved.length) {
      problems.push(`Not saved as a connection: ${unsaved.join(', ')}.`);
    }
    if (noPassword.length) {
      problems.push(`No saved password: ${noPassword.join(', ')}.`);
    }

    if (problems.length) {
      window.shellmateAlerts.notify({
        severity: 'warning',
        icon:     'warning',
        title:    `Restored ${restored} of ${remembered.length} sessions`,
        body:     problems.join(' ')
                  + ' Save the connection with its credentials and it comes'
                  + ' back next time.',
      });
    } else if (restored) {
      window.shellmateAlerts.notify({
        title: `Restored ${restored} session${restored === 1 ? '' : 's'}`,
        body:  'Reopened from where you left off.',
      });
    }
  }

  /**
   * What the New Tab button opens.
   *
   * The welcome screen is a poor answer for somebody who works on one device
   * all day, and a good one for somebody who does not — hence a choice rather
   * than a change. 'last' repeats whatever was opened most recently, which is
   * the common case without needing anything chosen in advance.
   */
  async function openNewTabTarget() {
    const prefs = (window.shellmateSettings || {}).interface || {};
    const mode = prefs.new_tab_opens || 'welcome';

    let profile = null;
    if (mode === 'profile' && prefs.new_tab_profile) {
      profile = await _profileById(prefs.new_tab_profile);
    } else if (mode === 'last') {
      const last = (prefs.open_tabs || [])[prefs.open_tabs.length - 1];
      if (last) {
        profile = await _profileFor({
          label:          last.label,
          hostname:       last.hostname,
          port:           last.port,
          username:       last.username,
          connectionType: last.connection_type,
        });
      }
    }

    // The dialog either way, prefilled or empty. Connecting outright on a
    // button called "New tab" would be a surprise, and the setting is about
    // saving the typing rather than skipping the decision — a saved
    // connection is one keypress away once its fields are filled in.
    if (typeof window.showConnectionDialog === 'function') {
      window.showConnectionDialog(profile || undefined);
      return;
    }
  }

  /**
   * Find the saved connection a remembered tab *is*, not the one it is most
   * like.
   *
   * `_profileFor` scores loosely on purpose: it backs Reconnect and Duplicate,
   * where somebody has asked for this device and a near match is a helpful
   * guess they can see and correct. Restore is neither asked for nor watched —
   * it runs at startup, unattended — so the same scoring will happily connect
   * to a different device on the same address, which is how a change lands in
   * the wrong place. Found in testing, with two devices on 127.0.0.1
   * distinguished only by port.
   *
   * So: address, port and transport must all agree, and the username too when
   * one was recorded. Anything less is reported as unrestorable, which is a
   * far better outcome than a confident connection to the wrong box.
   */
  async function _exactProfileFor(entry) {
    let profiles = [];
    try {
      const res = await fetch('/api/profiles');
      profiles = res.ok ? await res.json() : [];
    } catch (_) {
      return null;
    }
    if (!Array.isArray(profiles)) profiles = profiles.profiles || [];

    const type = entry.connection_type || 'ssh';
    return profiles.find(p =>
      (p.connection_type || 'ssh') === type
      && (p.hostname || '') === (entry.hostname || '')
      && Number(p.port || 0) === Number(entry.port || 0)
      && (!entry.username || (p.username || '') === entry.username)
    ) || null;
  }

  async function _profileById(id) {
    try {
      const res = await fetch('/api/profiles');
      if (!res.ok) return null;
      const data = await res.json();
      return (data.profiles || []).find(p => p.id === id) || null;
    } catch (_) {
      return null;
    }
  }

  // -------------------------------------------------------------------------
  // The dashboard as a place you can go back to
  //
  // It used to be purely an empty state: shown when the last tab closed,
  // hidden the moment one opened, with no way to reach it in between. Groups
  // change that — you cannot dive in and out of a group you can only see when
  // nothing is connected.
  //
  // Terminals are hidden, never destroyed. That is the Phase 1 rule and it is
  // the whole reason this is safe: a session mid-reload keeps running, keeps
  // receiving, and is exactly where it was when you come back.
  // -------------------------------------------------------------------------

  /** Show the dashboard over the terminals, leaving every session alone. */
  function showDashboard() {
    if (!welcomeScreen) return;
    welcomeScreen.classList.remove('hidden');
    if (terminalsContainer) terminalsContainer.classList.add('behind-dashboard');
    _markSessionsLink(true);
    if (typeof window.renderWelcomeProfiles === 'function') {
      window.renderWelcomeProfiles();
    }
  }

  /** Go back to the terminals. */
  function hideDashboard() {
    if (!welcomeScreen) return;
    // Only when there is something to go back to. With no tabs open the
    // dashboard is the empty state again and hiding it would leave a void.
    if (!tabs.length) return;
    welcomeScreen.classList.add('hidden');
    if (terminalsContainer) terminalsContainer.classList.remove('behind-dashboard');
    _markSessionsLink(false);
  }

  /** Whether the dashboard is currently in front. */
  function dashboardVisible() {
    return Boolean(welcomeScreen) && !welcomeScreen.classList.contains('hidden');
  }

  function _markSessionsLink(active) {
    const link = document.getElementById('sidebar-link-sessions');
    if (link) link.classList.toggle('active', active);
  }

  // -------------------------------------------------------------------------
  // Tab ordering
  //
  // Tabs have always been manual: appended in the order they were opened and
  // moved by dragging. That is the right default and stays the default —
  // people put tabs where they want them and expect them to stay there.
  //
  // Twenty tabs across three estates is where it stops working, and it is
  // exactly where the grouping already recorded in the profiles would earn
  // its keep. Tags exist now, and the tab strip was the one place they were
  // not reflected.
  // -------------------------------------------------------------------------

  /** The current mode. 'manual' means this module does nothing at all. */
  let _order = 'manual';

  /** Tag lookup by session, filled lazily — a profile match costs a fetch. */
  const _tagCache = new Map();

  /**
   * Reorder the tab strip.
   *
   * Sorts the `tabs` array as well as the DOM, deliberately. Ctrl+1..9,
   * closeTab's "activate the neighbour" and the drag handler all index into
   * that array, so sorting only the DOM would leave Ctrl+3 selecting whatever
   * used to be third — which looks like a bug in the shortcut rather than a
   * consequence of the sort.
   */
  function sortTabs() {
    if (_order === 'manual' || tabs.length < 2) return;

    const active = getActiveTab();

    const key = {
      name:   t => (t.label || '').toLowerCase(),
      device: t => (t.hostname || t.label || '').toLowerCase(),
      // Opened order is what manual mode starts as, so this is only distinct
      // once tabs have been dragged — which is precisely when somebody wants
      // it back.
      opened: t => t.openedAt || 0,
      tag:    t => _tagCache.get(t.sessionId) || '￿',
    }[_order];
    if (!key) return;

    const sorted = tabs.slice().sort((a, b) => {
      const ka = key(a);
      const kb = key(b);
      if (ka < kb) return -1;
      if (ka > kb) return 1;
      // Tabs with no tag, or the same one, keep the order they were opened
      // in rather than an arbitrary one that changes on every re-sort.
      return (a.openedAt || 0) - (b.openedAt || 0);
    });

    tabs.length = 0;
    sorted.forEach(t => { tabs.push(t); tabList.appendChild(t.tabEl); });

    // The array indices just changed underneath the active-tab index.
    if (active) {
      const index = tabs.findIndex(t => t.sessionId === active.sessionId);
      if (index !== -1) activeTabIndex = index;
    }
  }

  /**
   * Learn a tab's tag, then re-sort.
   *
   * Only when grouping by tag, because it costs a profile lookup per tab and
   * nothing else needs the answer. The first tag is used: a device in both
   * "core" and "site-3" has to sit somewhere, and picking the first recorded
   * one is at least stable.
   */
  async function _learnTag(tab) {
    if (_order !== 'tag' || _tagCache.has(tab.sessionId)) return;
    try {
      const profile = await _profileFor(tab);
      const tag = ((profile || {}).tags || [])[0] || '';
      _tagCache.set(tab.sessionId, tag.toLowerCase());
    } catch (_) {
      _tagCache.set(tab.sessionId, '');
    }
    sortTabs();
  }

  /** Set the mode and apply it. Called by prefs.js on load and on save. */
  function setTabOrder(mode) {
    const allowed = ['manual', 'name', 'device', 'opened', 'tag'];
    _order = allowed.includes(mode) ? mode : 'manual';
    if (_order === 'tag') tabs.forEach(_learnTag);
    sortTabs();

    // Re-evaluated on every change, not only when a tab is built. _bindDrag
    // runs once per tab at creation, so tabs that already existed when the
    // setting changed kept whatever they were given then — leaving them
    // draggable in a sorted strip, where a drop is silently undone by the
    // next sort.
    const draggable = tabOrderIsManual() ? 'true' : 'false';
    tabs.forEach(t => t.tabEl.setAttribute('draggable', draggable));
  }

  /** Whether dragging should be offered. Sorted tabs cannot be rearranged. */
  function tabOrderIsManual() {
    return _order === 'manual';
  }

  // -------------------------------------------------------------------------
  // Drag-to-reorder
  // -------------------------------------------------------------------------

  function _bindDrag(tabEl, sessionId) {
    // Only offered while the order is manual. Dropping a tab somewhere the
    // next sort immediately undoes reads as the drag having failed, and there
    // is no way to tell from the strip that a sort is what moved it back.
    tabEl.setAttribute('draggable', tabOrderIsManual() ? 'true' : 'false');

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
  /**
   * Escape text bound for innerHTML.
   *
   * The address comes from the connection dialog, so it is user input on its
   * way into markup. Everything else in this menu is a literal.
   */
  function _escapeHtml(value) {
    const box = document.createElement('div');
    box.textContent = value == null ? '' : String(value);
    return box.innerHTML;
  }

  function _showTabContextMenu(e, sessionId) {
    e.preventDefault();
    _hideTabContextMenu();
    _ctxSessionId = sessionId;

    const tab = tabs.find(t => t.sessionId === sessionId);
    const disconnected = tab && !tab.isConnected;

    // Shown on the entry itself. "Copy address" tells you the action; it does
    // not tell you *which* address you are about to copy, and on a tab named
    // after the device that is the one thing you cannot see anywhere else.
    // Escaped: it reaches innerHTML, and it originates from the connection
    // dialog rather than from us.
    const address = tab ? (tab.address || tab.hostname || '') : '';
    const addressLabel = _escapeHtml(
      address && tab && tab.port && tab.port !== 22 && tab.connectionType === 'ssh'
        ? `${address}:${tab.port}`
        : address);

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
      <!-- The address is what you need somewhere else: a ticket, a chat, a
           firewall rule. The tab shows the device's *name* once it announces
           itself, which is exactly when the address stops being on screen. -->
      <button data-action="copy-address">
        <span class="material-symbols-outlined">lan</span>
        Copy address
        ${addressLabel ? `<span class="ctx-value">${addressLabel}</span>` : ''}
      </button>
      <div class="ctx-sep"></div>
      <!-- An ad-hoc connection could not be kept: the only way was to retype
           the whole thing into the dialog. -->
      <button data-action="save-connection">
        <span class="material-symbols-outlined">bookmark_add</span>
        Save this connection
      </button>
      <button data-action="duplicate">
        <span class="material-symbols-outlined">tab_duplicate</span>
        Duplicate session
      </button>
    `;

    // Only offered when there is more than one pane to choose between —
    // "move to pane 1" on a single layout is a menu entry that does nothing.
    const panes = window.shellmateLayout ? window.shellmateLayout.panes() : 1;
    if (panes > 1) {
      const sep = document.createElement('div');
      sep.className = 'ctx-sep';
      _ctxMenu.appendChild(sep);

      const heading = document.createElement('div');
      heading.className = 'ctx-heading';
      heading.textContent = 'Move to pane';
      _ctxMenu.appendChild(heading);

      const row = document.createElement('div');
      row.className = 'ctx-pane-row';
      for (let i = 0; i < panes; i++) {
        const btn = document.createElement('button');
        btn.className = 'ctx-pane';
        btn.dataset.action = 'pane';
        btn.dataset.pane = String(i);
        btn.textContent = String(i + 1);
        row.appendChild(btn);
      }
      _ctxMenu.appendChild(row);
    }

    document.body.appendChild(_ctxMenu);

    // Filled in behind the menu, so a right-click never waits on a request.
    _appendQuickBroadcast(_ctxMenu, tab);

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
          case 'copy-address': _copyAddress(tab);      break;
          case 'save-connection': _saveConnection(tab);  break;
          case 'duplicate': _duplicateSession(tab);   break;
          case 'pane':
            window.shellmateLayout.place(Number(btn.dataset.pane), tab.sessionId);
            break;
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
    // Remove by class, not only the tracked reference. The dismissal
    // listeners are registered per menu with { once: true }, so an older
    // menu's handler can fire after a newer one has replaced the reference —
    // removing the new menu and orphaning the old one, which then stays in
    // the DOM for the life of the page.
    document.querySelectorAll('.tab-context-menu').forEach(el => el.remove());
    _ctxMenu = null;
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

  // -------------------------------------------------------------------------
  // Coming back on its own
  //
  // _reconnectSession() was well built and nothing ever called it: a dropped
  // session greyed out and waited to be noticed. The reload case is the point
  // — schedule it, watch the countdown, lose the session, then sit clicking
  // Reconnect until the device answers. That last part is the tool making a
  // person do polling, at the one moment they most want their hands free.
  //
  // Three rules, all of them about not surprising anybody:
  //
  // Only a drop ShellMate did not cause. Never after closeTab(), never after
  // a deliberate disconnect, never for serial — the COM port did not go
  // anywhere and reopening it would fight whatever else took it.
  //
  // Only where credentials resolve without being held.
  // ConnectionParams.scrub_secrets() clears the password the moment
  // authentication succeeds, on purpose, and this must not weaken that. So it
  // works from a saved connection whose password the backend can fetch
  // itself, and nowhere else — and says so when it cannot.
  //
  // Backing off. A device coming back from a reload takes minutes; retrying
  // every second for the first two is pointless and looks like an attack to
  // anything watching the management network.
  // -------------------------------------------------------------------------

  /**
   * Printed when a session comes back on its own.
   *
   * A session that silently reappears leaves you unsure whether the
   * scrollback above the line is from the same boot of the device — which
   * matters a great deal when you are about to reason about what changed.
   */
  const RECONNECTED_NOTE =
    '\r\n\x1b[32m[reconnected — a new session; anything above this line '
    + 'is from before the drop]\x1b[0m\r\n';

  /** Sessions currently being retried: sessionId -> { timer, attempt, cancel }. */
  const _retrying = {};

  function _advanced(key, fallback) {
    return window.shellmateAdvanced ? window.shellmateAdvanced(key, fallback) : fallback;
  }

  /**
   * Decide whether to start retrying, and start if so.
   *
   * Called from updateTabStatus when a session goes down, which is the one
   * place that learns about every drop however it happened.
   */
  async function _maybeAutoReconnect(tab) {
    if (!_advanced('ssh.auto_reconnect', false)) return;
    if (tab.closingDeliberately) return;
    if ((tab.connectionType || 'ssh') === 'serial') return;
    if (_retrying[tab.sessionId]) return;

    // Whether it *can* work is a question about credentials, and the honest
    // answer is available before the first attempt rather than after it.
    const profile = await _profileFor(tab);
    if (!profile || !profile.has_saved_credentials) {
      _sayWhyNot(tab, profile);
      return;
    }

    // A reload is the case worth waiting out, and ShellMate saw it go in.
    let attempts = Number(_advanced('ssh.auto_reconnect_attempts', 10));
    if (_advanced('ssh.auto_reconnect_after_reload', true) && tab.hadPendingReload) {
      attempts *= 3;
    }

    _retrying[tab.sessionId] = { attempt: 0, attempts, timer: null };
    _scheduleRetry(tab, profile);
  }

  function _scheduleRetry(tab, profile) {
    const state = _retrying[tab.sessionId];
    if (!state) return;

    state.attempt += 1;
    if (state.attempt > state.attempts) {
      _stopRetrying(tab, `gave up after ${state.attempts} attempts`);
      return;
    }

    // Doubling, capped at a minute. The cap matters more than the growth:
    // unbounded backoff means the one that would have worked never runs.
    const base = Number(_advanced('ssh.auto_reconnect_backoff', 5)) * 1000;
    const wait = Math.min(base * Math.pow(2, state.attempt - 1), 60000);

    _setRetryLabel(tab, `reconnecting… (${state.attempt}/${state.attempts})`);

    state.timer = setTimeout(async () => {
      if (!_retrying[tab.sessionId]) return;          // cancelled meanwhile
      const ok = await _reconnectSession(tab, { silent: true, profile });
      if (ok) {
        delete _retrying[tab.sessionId];
        return;
      }
      _scheduleRetry(tab, profile);
    }, wait);
  }

  function _stopRetrying(tab, why) {
    const state = _retrying[tab.sessionId];
    if (state && state.timer) clearTimeout(state.timer);
    delete _retrying[tab.sessionId];
    if (why) _setRetryLabel(tab, why);
  }

  /** One click on the tab cancels it — no modal for something this small. */
  function _setRetryLabel(tab, text) {
    if (!tab.labelEl) return;
    tab.labelEl.textContent = `${tab.label} (${text})`;
    tab.labelEl.title = 'Click the tab to stop trying';
  }

  /**
   * The saved connection a tab came from.
   *
   * Scored rather than found. The original matched on hostname *or* label and
   * took the first hit, which on a lab where twenty saved connections share
   * 127.0.0.1 returned whichever happened to be saved first — so reconnecting
   * used the wrong profile's credentials, or none at all, and reported "no
   * saved password" while the right profile sat further down the list.
   *
   * Port and username are what actually distinguish them, and a profile that
   * can supply credentials beats one that cannot when everything else ties.
   */
  async function _profileFor(tab) {
    let profiles = [];
    try {
      const res = await fetch('/api/profiles');
      profiles = res.ok ? await res.json() : [];
    } catch (_) {
      return null;
    }

    const type = tab.connectionType || 'ssh';
    const score = (p) => {
      if ((p.connection_type || 'ssh') !== type) return -1;
      let points = 0;
      if (p.hostname && p.hostname === tab.hostname) points += 4;
      if (tab.port && Number(p.port) === Number(tab.port)) points += 3;
      if (tab.username && p.username === tab.username) points += 2;
      if (p.name && p.name === tab.label) points += 1;
      // Only a tie-breaker: a profile that matches less well is still the
      // wrong device however good its credentials are.
      if (points > 0 && p.has_saved_credentials) points += 0.5;
      return points;
    };

    let best = null;
    let bestScore = 0;
    profiles.forEach(p => {
      const points = score(p);
      if (points > bestScore) { best = p; bestScore = points; }
    });
    return best;
  }

  /**
   * Say why it is not retrying.
   *
   * "Reconnect needs a saved password for this device" is something somebody
   * can act on. A tab that silently does not retry is not.
   */
  function _sayWhyNot(tab, profile) {
    if (!tab.labelEl) return;
    const reason = profile
      ? 'no saved password'
      : 'not a saved connection';
    tab.labelEl.textContent = `${tab.label} (disconnected — ${reason})`;
    tab.labelEl.title =
      'Automatic reconnect needs credentials the server can fetch itself. '
      + 'Save this connection with its password, or reconnect by hand.';
  }

  /**
   * Stand a dropped session back up.
   *
   * @param {object}  tab
   * @param {object} [opts]
   * @param {boolean} [opts.silent]  Do not open the dialog on failure — an
   *   automatic retry that pops a modal is worse than one that waits.
   * @param {object} [opts.profile]  Already looked up, to save a round trip
   *   on every attempt.
   * @returns {Promise<boolean>} whether a session was established.
   */
  async function _reconnectSession(tab, opts) {
    const options = opts || {};
    const index = tabs.findIndex(t => t.sessionId === tab.sessionId);

    let profile = options.profile || null;
    if (!profile) {
      profile = await _profileFor(tab);
    }

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
          // force: the tab being replaced is the dead one the user asked to
          // reconnect. It would not be asked about today — a disconnected tab
          // closes without a word — but saying so keeps this correct if that
          // ever changes, now that closing is asynchronous.
          if (index !== -1) await closeTab(index, { force: true });
          const fresh = createTab(data);
          // Say so in the terminal. A session that silently reappears leaves
          // you unsure whether the scrollback above the line is from the same
          // boot of the device — which matters a great deal when you are
          // about to reason about what changed.
          if (options.silent && fresh && fresh.terminal) {
            fresh.terminal.write(RECONNECTED_NOTE);
          }
          return true;
        }
      } catch (_) { /* fall through to the dialog */ }
    }

    if (options.silent) return false;

    if (typeof window.showConnectionDialog === 'function') {
      window.showConnectionDialog(profile || {
        name:            tab.label,
        hostname:        tab.hostname,
        connection_type: tab.connectionType || 'ssh',
      });
    }
  }

  /**
   * Open a second session to the same device.
   *
   * Two things were wrong with this. It prefilled `s.hostname`, which is
   * overwritten with the *detected* device name once the device announces
   * itself — so duplicating a connection dialled by address put "S3-R1" in
   * the hostname box, which on a management network resolves nowhere. And it
   * passed no profile id, so the saved password could not be resolved
   * server-side and had to be typed again.
   *
   * Where the credentials do resolve it now simply opens the session, which
   * is the same rule as clicking a ready tile: a dialog showing nothing but
   * filled-in fields and a Connect button is a wasted step.
   */
  /**
   * Put the device's address on the clipboard.
   *
   * `address` rather than `hostname`: the latter is overwritten with whatever
   * the device calls itself, so on the tabs where this is most useful it is
   * not an address at all.
   */
  async function _copyAddress(tab) {
    let address = tab.address || tab.hostname || '';
    let port = tab.port;
    try {
      const res = await fetch('/api/sessions');
      if (res.ok) {
        const session = (await res.json())
          .find(s => s.session_id === tab.sessionId);
        if (session) {
          address = session.address || session.hostname || address;
          port = session.port || port;
        }
      }
    } catch (_) { /* the tab's own copy is a reasonable fallback */ }

    if (!address) return;
    const text = (port && port !== 22) ? `${address}:${port}` : address;
    try {
      await navigator.clipboard.writeText(text);
      if (window._showCopyToast) window._showCopyToast(text);
    } catch (_) { /* clipboard refused; nothing useful to say */ }
  }

  /**
   * Keep an ad-hoc connection, credentials included.
   *
   * The credential has to be asked for rather than recovered.
   * `ConnectionParams.scrub_secrets()` clears it the moment authentication
   * succeeds — deliberately, so it cannot be read out of a memory dump or
   * leak into a crash report — so by the time anybody decides the connection
   * is worth keeping, ShellMate genuinely does not have it. The dialog says
   * that, because otherwise being asked for a password you typed two minutes
   * ago looks like a bug.
   */

  // -------------------------------------------------------------------------
  // Quick broadcast
  //
  // A handful of commands get sent dozens of times a day, and reaching them
  // meant opening the Broadcast panel, finding the entry in a 137-line
  // library, choosing targets and sending. These are the same library
  // entries, flagged — one place commands are written down and one editor for
  // them, whichever route you arrive by.
  // -------------------------------------------------------------------------

  /** Cached between right-clicks; the menu should not wait on a fetch. */
  let _quickSnippets = null;

  async function _loadQuick() {
    if (_quickSnippets) return _quickSnippets;
    try {
      const res = await fetch('/api/snippets/quick');
      _quickSnippets = res.ok ? (await res.json()).snippets : [];
    } catch (_) {
      _quickSnippets = [];
    }
    return _quickSnippets;
  }

  // The library can change while the application is open.
  window.addEventListener('shellmate:snippets-changed', () => { _quickSnippets = null; });

  /**
   * Add the quick-broadcast entries to an open tab menu.
   *
   * Appended after the menu is on screen rather than built into it, so a
   * right-click is never waiting on a request.
   */
  async function _appendQuickBroadcast(menu, tab) {
    // Guard against being appended twice to one menu. The fetch is async, so
    // two calls can be in flight against the same element and the entries
    // appear doubled.
    if (menu.dataset.quickAdded) return;
    menu.dataset.quickAdded = '1';

    const snippets = await _loadQuick();
    if (!menu.isConnected) return;          // menu already dismissed

    const sep = document.createElement('div');
    sep.className = 'ctx-sep';
    menu.appendChild(sep);

    const heading = document.createElement('div');
    heading.className = 'ctx-heading';
    heading.textContent = 'Quick broadcast';
    menu.appendChild(heading);

    if (!snippets.length) {
      const empty = document.createElement('div');
      empty.className = 'ctx-empty';
      empty.textContent = 'None marked yet';
      menu.appendChild(empty);
    }

    snippets.forEach(snippet => {
      const row = document.createElement('div');
      row.className = 'ctx-quick-row';

      const label = document.createElement('span');
      label.className = 'ctx-quick-name';
      label.textContent = snippet.name;
      // The commands themselves, because "Save configuration" is not enough
      // to decide by when it writes to the device.
      label.title = snippet.commands.join('\n')
        + (snippet.send_return ? '' : '\n\n(typed in, not run)');

      const here = document.createElement('button');
      here.className = 'ctx-quick-btn';
      here.textContent = 'This tab';
      here.addEventListener('click', (e) => {
        e.stopPropagation();
        _hideTabContextMenu();
        _quickBroadcast(snippet, [tab]);
      });

      const all = document.createElement('button');
      all.className = 'ctx-quick-btn';
      all.textContent = 'All tabs';
      all.addEventListener('click', (e) => {
        e.stopPropagation();
        _hideTabContextMenu();
        _quickBroadcast(snippet, tabs.filter(t => t.isConnected));
      });

      row.append(label, here, all);
      menu.appendChild(row);
    });

    const edit = document.createElement('button');
    edit.className = 'ctx-quick-edit';
    edit.textContent = 'Edit quick broadcast commands…';
    edit.addEventListener('click', () => {
      _hideTabContextMenu();
      // The same signpost pattern the prompt editor needed: a feature found
      // on a menu has to say where it is maintained, or it is discovered and
      // not maintainable.
      // The library lives in the Broadcast panel, which is where snippets
      // are already added, edited and removed. One editor, whichever route
      // you arrive by.
      if (typeof window.openBroadcast === 'function') window.openBroadcast();
      else document.dispatchEvent(new KeyboardEvent('keydown',
        { key: 'B', ctrlKey: true, shiftKey: true, bubbles: true }));
    });
    menu.appendChild(edit);
  }

  /**
   * Send a quick snippet to one tab or to all of them.
   *
   * Routed through /api/broadcast rather than typed into each socket, so it
   * inherits the concurrency cap, the overall timeout and — most of all —
   * the per-device result list. A partial failure has to be visible rather
   * than assumed, and that matters more here than in the panel, because the
   * whole point is not watching each one.
   */
  async function _quickBroadcast(snippet, targets) {
    const connected = targets.filter(t => t.isConnected);
    if (!connected.length) return;

    // Anything that writes gets a confirmation naming the count, and sending
    // to twelve devices is a different decision from sending to one.
    if (snippet.writes) {
      const ok = await window.shellmateDialog.confirm({
        title: connected.length === 1
          ? `Send "${snippet.name}" to ${connected[0].label}?`
          : `Send "${snippet.name}" to ${connected.length} devices?`,
        body: snippet.commands.join('\n'),
        confirmLabel: 'Send',
        danger: true,
      });
      if (!ok) return;
    }

    try {
      const res = await fetch('/api/broadcast', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({
          session_ids: connected.map(t => t.sessionId),
          commands:    snippet.commands,
          wait_ms:     snippet.wait_ms,
          // The broadcast endpoint already had this flag — `execute` decides
          // whether a carriage return follows the command, which is exactly
          // the difference between typing one in and running it.
          execute:     snippet.send_return !== false,
        }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || 'Could not send it.');

      const failed = (data.results || []).filter(r => !r.ok);
      if (window.shellmateAlerts) {
        window.shellmateAlerts.notify({
          severity: failed.length ? 'warning' : 'info',
          icon: failed.length ? 'warning' : 'check_circle',
          title: snippet.send_return === false
            ? `${snippet.name} typed into ${data.sent} of ${connected.length}`
            : `${snippet.name} sent to ${data.sent} of ${connected.length}`,
          body: failed.length
            ? failed.map(r => `${r.label || r.session_id}: ${r.error}`).join('\n')
            : snippet.commands.join('  ·  '),
        });
      }
    } catch (e) {
      if (window.shellmateAlerts) {
        window.shellmateAlerts.notify({
          severity: 'warning', icon: 'error',
          title: 'Quick broadcast failed', body: e.message,
        });
      }
    }
  }

  async function _saveConnection(tab) {
    let session = null;
    try {
      const res = await fetch('/api/sessions');
      if (res.ok) {
        session = (await res.json())
          .find(s => s.session_id === tab.sessionId) || null;
      }
    } catch (_) { /* nothing to save without it */ }
    if (!session) return;

    const address = session.address || session.hostname || '';
    const isSerial = session.connection_type === 'serial';

    const answer = await window.shellmateDialog.form({
      title: `Save ${session.display_label || address}`,
      body:  'Kept on the welcome screen so you can reconnect with one click.',
      note:  isSerial ? '' :
             'ShellMate does not still have the password — it is cleared the '
             + 'moment a connection succeeds, on purpose. Give it again to '
             + 'save it, or leave it blank to save the connection without one.',
      confirmLabel: 'Save',
      fields: [
        { name: 'name', label: 'Name', required: true,
          value: session.display_label || address },
        { name: 'tags', label: 'Tags',
          placeholder: 'glasgow, production',
          hint: 'Optional. Groups the welcome screen and lets you open a set '
                + 'at once.' },
      ].concat(isSerial ? [] : [
        { name: 'password', label: 'Password', type: 'password',
          hint: 'Optional. Leave blank and you will be asked on connect.' },
        { name: 'storage', label: 'Keep it', type: 'select',
          options: [
            { value: 'vault',     label: 'Encrypted in the vault' },
            { value: 'plaintext', label: 'Plain text — readable on disk' },
          ] },
      ]),
    });
    if (!answer) return;

    try {
      const res = await fetch('/api/profiles', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({
          name:            answer.name,
          hostname:        address,
          port:            session.port || 22,
          username:        session.username || '',
          connection_type: session.connection_type || 'ssh',
          serial_port:     isSerial ? address : '',
          tags:            (answer.tags || '').split(',')
                             .map(s => s.trim()).filter(Boolean),
        }),
      });
      if (!res.ok) throw new Error('Could not save it.');
      const profile = await res.json();

      if (answer.password) {
        // PUT with a flat body — the same endpoint the connection dialog
        // uses after a successful connect.
        await fetch(`/api/profiles/${profile.id}/credentials`, {
          method:  'PUT',
          headers: { 'Content-Type': 'application/json' },
          body:    JSON.stringify({
            password: answer.password,
            storage:  answer.storage || 'vault',
          }),
        });
      }

      if (typeof window.renderWelcomeProfiles === 'function') {
        window.renderWelcomeProfiles();
      }
      if (window.shellmateAlerts) {
        window.shellmateAlerts.notify({
          title: profile.already_saved ? 'Connection updated' : 'Connection saved',
          body:  `${answer.name} is on the welcome screen.`,
        });
      }
    } catch (e) {
      if (window.shellmateAlerts) {
        window.shellmateAlerts.notify({
          severity: 'warning', icon: 'error',
          title: 'Could not save the connection', body: e.message,
        });
      }
    }
  }

  async function _duplicateSession(tab) {
    let session = null;
    try {
      const res      = await fetch('/api/sessions');
      const sessions = await res.json();
      session = sessions.find(s => s.session_id === tab.sessionId) || null;
    } catch (err) {
      console.error('Could not get session for duplicate:', err);
    }
    if (!session) return;

    // `address` is what was dialled and is never rewritten; `hostname` is
    // what the device calls itself.
    const address = session.address || session.hostname || '';
    const profile = await _profileFor({
      connectionType: session.connection_type || 'ssh',
      hostname:       address,
      port:           session.port,
      username:       session.username,
      label:          session.display_label,
    });

    if (profile && profile.has_saved_credentials) {
      try {
        const res = await fetch('/api/sessions', {
          method:  'POST',
          headers: { 'Content-Type': 'application/json' },
          body:    JSON.stringify({
            connection_type: profile.connection_type || 'ssh',
            hostname:        profile.hostname || address,
            port:            profile.port || session.port || 22,
            username:        profile.username || session.username || '',
            serial_port:     profile.serial_port || '',
            display_label:   profile.name || session.display_label || '',
            profile_id:      profile.id,
          }),
        });
        if (res.ok) {
          createTab(await res.json());
          return;
        }
      } catch (_) { /* fall through to the dialog */ }
    }

    if (typeof window.showConnectionDialog === 'function') {
      window.showConnectionDialog(profile || {
        name:            session.display_label || '',
        hostname:        address,
        port:            session.port || 22,
        username:        session.username || '',
        connection_type: session.connection_type || 'ssh',
      });
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
  window.setTabOrder      = setTabOrder;
  window.showDashboard    = showDashboard;
  window.hideDashboard    = hideDashboard;
  window.dashboardVisible = dashboardVisible;
  window.openNewTabTarget = openNewTabTarget;
  window.restoreTabs      = _restoreTabs;
  // ShellMate saw the `reload` go in, so when the session drops it knows why
  // and roughly how long the device will be away. That is what justifies
  // waiting minutes rather than giving up after thirty seconds.
  window.addEventListener('shellmate:pending-action', (e) => {
    const detail = e.detail || {};
    const tab = tabs.find(t => t.sessionId === detail.sessionId);
    if (!tab) return;
    const kind = (detail.pending && detail.pending.kind) || '';
    // alerts.py uses RELOAD / COMMIT_CONFIRM.
    if (kind) tab.hadPendingReload = kind.toUpperCase().includes('RELOAD');
  });

  window.updateTabStatus  = updateTabStatus;
  window.updateStatusBar  = updateStatusBar;
  window.refitTerminals   = refitTerminals;

})();
