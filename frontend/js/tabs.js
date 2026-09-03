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

    // Brand click → the default home view (#232)
    document.getElementById('tab-bar-brand').addEventListener('click', goHome);

    // The logo is the first thing people try as "home", and it did nothing.
    const sidebarLogo = document.getElementById('sidebar-logo');
    if (sidebarLogo) sidebarLogo.addEventListener('click', goHome);

    function goHome() {
      // Hide all terminal containers so the welcome screen shows through
      tabs.forEach(tab => {
        const c = document.getElementById(tab.containerId);
        if (c) c.classList.remove('active');
      });
      tabs.forEach(tab => tab.tabEl.classList.remove('active'));
      activeTabIndex = -1;
      if (window.shellmateLayout) window.shellmateLayout.clear();
      // Home means the ShellMate hero, not the last group's dashboard — the
      // tooltip said "Home" while the click preserved the selection (#232).
      if (window.shellmateGroups && window.shellmateGroups.clear) {
        window.shellmateGroups.clear();
      }
      // Through showDashboard(), not a bare un-hide: it also puts the
      // terminals behind the dashboard — without that, a tiled layout's
      // focus ring paints straight through the welcome screen (#212) — and
      // tells the alert stack to hide (#168).
      showDashboard();
      // With no active tab the bar must say so — left alone it went on
      // claiming "Connected" with a ticking clock from the dashboard.
      updateStatusBar();
    }

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
  /** How many entries the dashboard's recently-used list keeps (#268). */
  const RECENT_MAX = 8;

  /**
   * Remember a connection as recently used (#268).
   *
   * Here rather than at each call site because this is the single funnel
   * every session passes through — the dialog, a tile, a duplicate, a
   * reconnect, a restored tab. Identity is the profile when there is one and
   * the address otherwise, so an ad-hoc connection is still remembered and a
   * saved one never appears twice.
   */
  function _rememberRecent(sessionData) {
    if (!window.shellmatePrefs) return;
    const entry = {
      profile_id:      sessionData.profile_id || '',
      label:           sessionData.display_label || sessionData.hostname || '',
      hostname:        sessionData.address || sessionData.hostname || '',
      port:            sessionData.port || 0,
      connection_type: sessionData.connection_type || 'ssh',
    };
    const identity = (r) => r.profile_id
      || `${r.connection_type}|${r.hostname}|${r.port}`;
    const kept = (window.shellmatePrefs.get('recent_connections', []) || [])
      .filter(r => r && identity(r) !== identity(entry));
    kept.unshift(entry);
    window.shellmatePrefs.set('recent_connections', kept.slice(0, RECENT_MAX));
    // No redraw here (#483). createTab fires `shellmate:sessions-changed` a
    // moment later, which is the one redraw the dashboard gets per opening —
    // and it hides the dashboard anyway, so this one was a full tree rebuild
    // that nobody saw.
  }

  function createTab(sessionData) {
    const { session_id, display_label, hostname } = sessionData;
    const label = display_label || hostname || session_id.slice(0, 8);

    _rememberRecent(sessionData);

    // Build the tab DOM element
    const tabEl = document.createElement('div');
    tabEl.className = 'tab';
    tabEl.dataset.sessionId = session_id;
    // A tab in a tablist (#428): reachable by keyboard, announced as one.
    tabEl.setAttribute('role', 'tab');
    tabEl.setAttribute('aria-selected', 'false');
    tabEl.tabIndex = -1;
    tabEl.addEventListener('keydown', (e) => {
      const index = tabs.findIndex(t => t.tabEl === tabEl);
      if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') {
        e.preventDefault();
        const next = (index + (e.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length;
        switchToTab(next);
        tabs[next].tabEl.focus();
      } else if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        switchToTab(index);
      } else if (e.key === 'Delete') {
        e.preventDefault();
        closeTab(index);
      }
    });

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
    closeBtn.setAttribute('aria-label', `Close ${label}`);
    closeBtn.tabIndex = -1;

    tabEl.appendChild(dot);
    tabEl.appendChild(labelEl);
    tabEl.appendChild(closeBtn);
    tabList.appendChild(tabEl);

    // Initialise terminal and WebSocket (defined in terminal.js)
    const termData = window.initTerminal(session_id);

    const tabObj = {
      sessionId:        session_id,
      // The saved connection this session was opened from (#187).
      //
      // The identity everything else keys on. Before this, a tab was matched
      // back to a connection by address and port, which is not an identity —
      // five thousand devices behind one address all matched, so one session
      // lit every indicator, tabs took the wrong group's name, and clicking
      // any device switched to the tab already open instead of connecting.
      // Empty for a session opened straight from the dialog, which genuinely
      // has no profile.
      profileId:        sessionData.profile_id || '',
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
      // Read through to terminal.js each time rather than copied once: the
      // socket is replaced when it drops and is reopened (#481), and a copy
      // taken here would be the dead one for the rest of the session.
      get websocket()   { return termData.websocket; },
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
    _paintGroups();
    // The dashboard shows how many of each group are open (#146), and it is
    // somewhere you return to with sessions running — so a count that only
    // updated when the dashboard was rebuilt would be wrong most of the time.
    window.dispatchEvent(new CustomEvent('shellmate:sessions-changed'));
    // A device marked red stays red on reconnect — that is the point of
    // marking it.
    _restoreScheme(tabObj);

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
    window.dispatchEvent(new CustomEvent('shellmate:dashboard-changed'));

    activeTabIndex = index;
    _paintBrand();
    _revealTab(tabs[index]);

    // Which terminals are on screen is the layout's business, not this
    // module's — under a tiled layout several are visible at once and the tab
    // being switched to may already be one of them. The strip still marks
    // exactly one tab active, because exactly one has the keyboard.
    tabs.forEach((tab, i) => {
      tab.tabEl.classList.toggle('active', i === index);
      tab.tabEl.setAttribute('aria-selected', i === index ? 'true' : 'false');
      tab.tabEl.tabIndex = i === index ? 0 : -1;
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
    return _closeTabObject(tabs[index], options);
  }

  /**
   * Close a tab held by identity rather than by position (#482).
   *
   * An index is only good until the array changes, and everything in here
   * awaits. Callers that hold a tab across an await — a reconnect, "close
   * all", a group disconnect — go through this so that a tab spliced out
   * meanwhile shifts nothing: the one that closes is the one that was asked
   * for, or nothing at all if it has already gone.
   */
  async function _closeTabObject(tab, options) {
    if (!tab || !tabs.includes(tab)) return;

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

    // Found by identity, and only now (#313): the confirm above is an await,
    // and the array can mutate under it — auto-reconnect replaces tabs,
    // sortTabs() reorders them. Splicing a captured index removed the wrong
    // tab: a live entry vanished from the array while the closed one
    // lingered, pointing at a disposed terminal.
    const index = tabs.indexOf(tab);
    if (index === -1) return;   // something else already closed it

    const { sessionId, websocket, terminalInstance, containerId, tabEl } = tab;
    const wasConnected = tab.isConnected;

    // The terminal, its addons, its socket and its resize listener go too.
    // Without this every terminal ever opened stayed reachable, and — worse —
    // applySettingsToAll() would later call terminal.options on a disposed
    // instance, throw, and stop applying settings to every tab after it.
    if (window.forgetTerminal) window.forgetTerminal(sessionId);

    // The clock goes with the tab, so a closed session stops costing a tick.
    if (window.shellmateUptime) window.shellmateUptime.forget(sessionId);

    // Hang up first, then tear down — two requests, in order (#214). DELETE
    // does disconnect as part of destroying the session, but it also flushes
    // history and clears the buffer, and it is sent best-effort: when it
    // fails, the device side stayed logged in with no tab left pointing at
    // it. The explicit disconnect makes the hang-up its own request, so the
    // connection is closed on the device even if the teardown after it is
    // lost. Both are idempotent, so the overlap costs nothing.
    (async () => {
      if (wasConnected) {
        try {
          await fetch(`/api/sessions/${sessionId}/disconnect`, { method: 'POST' });
        } catch (_) { /* the DELETE below disconnects too */ }
      }
      try {
        await fetch(`/api/sessions/${sessionId}`, { method: 'DELETE' });
      } catch (_) { /* best-effort — don't block UI on network error */ }
    })();

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
    window.dispatchEvent(new CustomEvent('shellmate:sessions-changed'));

    // Free the pane it occupied before anything is asked to re-render, so a
    // tiled layout pulls in a waiting session rather than leaving a hole.
    if (window.shellmateLayout) window.shellmateLayout.forget(sessionId);

    // Decide what to show next
    if (tabs.length === 0) {
      activeTabIndex = -1;
      // The full dashboard path, for the same reason as the brand click:
      // un-hiding the welcome screen without putting the terminals behind it
      // is how a leftover focus ring ends up painted over it (#212).
      showDashboard();
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
  function updateTabLabel(sessionId, label, opts) {
    const tab = tabs.find(t => t.sessionId === sessionId);
    if (!tab) return;
    // A detected hostname is remembered but does not overwrite a name the
    // user typed (#409); it becomes the label again if the name is cleared.
    if (!(opts && opts.custom)) {
      tab.autoLabel = label;
      if (tab.customLabel) {
        tab.labelEl.title = `${tab.label}\n(${label})`;
        return;
      }
    }
    tab.label = label;
    tab.labelEl.textContent = label;
    tab.labelEl.title = label;
    updateStatusBar();
  }

  /** Give a tab a name of your own, or clear it to get the detected one back. */
  async function _renameTab(tab) {
    const answer = await window.shellmateDialog.prompt({
      title: 'Rename this tab',
      body:  'Leave it blank to go back to the name the device gives itself.',
      value: tab.customLabel ? tab.label : '',
      placeholder: tab.autoLabel || tab.label,
      confirmLabel: 'Rename',
    });
    if (answer === null || answer === undefined) return;
    const name = String(answer).trim();
    if (name) {
      tab.customLabel = true;
      updateTabLabel(tab.sessionId, name, { custom: true });
      if (tab.autoLabel && tab.autoLabel !== name) {
        tab.labelEl.title = `${name}\n(${tab.autoLabel})`;
      }
    } else {
      tab.customLabel = false;
      updateTabLabel(tab.sessionId, tab.autoLabel || tab.label);
    }
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

    // The tree reads live state from this event, and it was dispatched only
    // when a tab opened or closed — never when one dropped. So a dead session
    // left its device, and its group, showing green until something else
    // happened to force a reload. This is the one place that hears about
    // every drop, however it happened (#203).
    window.dispatchEvent(new CustomEvent('shellmate:sessions-changed'));
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
    // The nested text span, not #status-connection itself: the clock beside
    // it (#227) is uptime.js's, and writing the parent would erase it.
    const connEl   = document.getElementById('status-connection-text');
    const bufferEl = document.getElementById('status-buffer');
    const tabsEl   = document.getElementById('status-tabs');

    tabsEl.textContent = `Tabs: ${tabs.length}`;

    const active = getActiveTab();
    if (!active) {
      connEl.textContent  = 'No active session';
      bufferEl.textContent = 'Buffer: 0L';
      if (window.shellmateUptime) window.shellmateUptime.render();
      return;
    }

    const stateText = active.isConnected ? 'Connected' : 'Disconnected';
    connEl.textContent = `SSH: ${active.label} | ${stateText}`;

    const lines = active.getBufferLines ? active.getBufferLines() : 0;
    bufferEl.textContent = `Buffer: ${lines.toLocaleString()}L`;

    if (typeof window.updateContextStatus === 'function') window.updateContextStatus();
    if (window.shellmateUptime) window.shellmateUptime.render();
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
      // Only intercept when a terminal is active — not when any text entry
      // has focus (#314). The chat box and the broadcast command box are
      // textareas, and Ctrl+W there is delete-word muscle memory from
      // readline; answering it by closing the active tab destroyed a
      // disconnected tab's scrollback with no confirmation at all.
      const el = document.activeElement;
      if (el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA'
                 || el.isContentEditable)) return;
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
      return;
    }

    // The rest of the set (#413). Each one is also listed in isAppShortcut()
    // so that a terminal with focus lets it through, and in the cheat-sheet.
    const shortcut = SHORTCUTS.find(s => s.match(e));
    if (shortcut) {
      e.preventDefault();
      shortcut.run();
    }
  }

  /** Move to the next or previous tab, wrapping at the ends. */
  function _stepTab(delta) {
    if (!tabs.length) return;
    const index = activeTabIndex < 0 ? 0
      : (activeTabIndex + delta + tabs.length) % tabs.length;
    switchToTab(index);
  }

  function _focusTerminal() {
    const tab = getActiveTab();
    if (tab && tab.terminalInstance) {
      try { tab.terminalInstance.focus(); } catch (_) { /* not mounted yet */ }
    }
  }

  function _focusChat() {
    const input = document.getElementById('chat-input');
    if (input && input.offsetParent !== null) input.focus();
  }

  function _call(name) {
    return () => { if (typeof window[name] === 'function') window[name](); };
  }

  /**
   * The application's keyboard shortcuts, in one table (#413).
   *
   * The table is the single source for three things: the document-level
   * handler, the list a focused terminal lets through, and the cheat-sheet
   * on Ctrl+/. Ctrl+T, Ctrl+W and Ctrl+1-9 stay in handleKeyboard() above
   * because they need the tab index and a guard the table cannot express.
   *
   * Chosen to avoid what the browser reserves: Ctrl+L, Ctrl+N, Ctrl+Shift+T
   * and Ctrl+Shift+N cannot be intercepted in Chromium, and Ctrl+Tab only
   * in the desktop window — hence Ctrl+PageUp/PageDown as well.
   */
  const SHORTCUTS = [
    { keys: 'Ctrl+Tab', what: 'Next tab',
      match: e => e.ctrlKey && !e.shiftKey && e.key === 'Tab', run: () => _stepTab(1) },
    { keys: 'Ctrl+Shift+Tab', what: 'Previous tab',
      match: e => e.ctrlKey && e.shiftKey && e.key === 'Tab', run: () => _stepTab(-1) },
    { keys: 'Ctrl+PageDown', what: 'Next tab',
      match: e => e.ctrlKey && e.key === 'PageDown', run: () => _stepTab(1) },
    { keys: 'Ctrl+PageUp', what: 'Previous tab',
      match: e => e.ctrlKey && e.key === 'PageUp', run: () => _stepTab(-1) },
    { keys: 'Ctrl+P', what: 'Find a tab by name',
      match: e => e.ctrlKey && !e.shiftKey && e.key.toLowerCase() === 'p',
      run: _call('openTabPalette') },
    { keys: 'Ctrl+`', what: 'Focus the terminal',
      match: e => e.ctrlKey && e.key === '`', run: _focusTerminal },
    { keys: 'Ctrl+Shift+A', what: 'Focus the assistant',
      match: e => e.ctrlKey && e.shiftKey && e.key.toLowerCase() === 'a', run: _focusChat },
    { keys: 'Ctrl+,', what: 'Settings',
      match: e => e.ctrlKey && e.key === ',', run: _call('openSettings') },
    { keys: 'Ctrl+H', what: 'Session history',
      match: e => e.ctrlKey && !e.shiftKey && e.key.toLowerCase() === 'h', run: _call('openHistory') },
    { keys: 'F1', what: 'The manual',
      match: e => e.key === 'F1', run: _call('openDocs') },
    { keys: 'Ctrl+/', what: 'This list',
      match: e => e.ctrlKey && e.key === '/', run: () => _showShortcuts() },
  ];

  /** Shortcuts owned by the page rather than the device; terminal.js asks. */
  function isAppShortcut(e) {
    if (e.ctrlKey && !e.altKey && (e.key === 't' || e.key === 'w')) return true;
    if (e.ctrlKey && !e.altKey && e.key >= '1' && e.key <= '9') return true;
    return SHORTCUTS.some(s => s.match(e));
  }

  function _showShortcuts() {
    const fixed = [
      { keys: 'Ctrl+T', what: 'New connection' },
      { keys: 'Ctrl+W', what: 'Close the current tab' },
      { keys: 'Ctrl+1 … Ctrl+9', what: 'Switch to that tab' },
      { keys: 'Ctrl+Alt+1 … Ctrl+Alt+9', what: 'Choose a split layout' },
      { keys: 'Ctrl+F', what: 'Find in the terminal' },
      { keys: 'Ctrl+Shift+B', what: 'Broadcast a command' },
      { keys: 'Ctrl+Shift+C / Ctrl+Shift+V', what: 'Copy / paste' },
      { keys: 'Shift+right-click', what: 'The terminal menu' },
    ];
    window.shellmateDialog.alert({
      title: 'Keyboard shortcuts',
      list: [...fixed, ...SHORTCUTS.filter(s => s.keys !== 'Ctrl+PageDown' && s.keys !== 'Ctrl+PageUp')]
        .map(s => ({ text: s.keys, detail: s.what, mono: true })),
      confirmLabel: 'Close',
    });
  }

  window.isAppShortcut = isAppShortcut;



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
      // The guard has to cover the .length read too (#311): with no tab list
      // ever recorded this was a TypeError, and the New Tab button did
      // nothing at all.
      const recorded = prefs.open_tabs || [];
      const last = recorded[recorded.length - 1];
      if (last) {
        profile = await _profileForQuiet({
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
      // A bare array (#310) — `data.profiles` was always undefined here, so
      // "New tab opens: chosen profile" never found its profile and every
      // New Tab click silently fell through to an empty dialog.
      let profiles = await res.json();
      if (!Array.isArray(profiles)) profiles = profiles.profiles || [];
      return profiles.find(p => p.id === id) || null;
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
    // The alert stack hides with it (#168) and has no other way to know.
    window.dispatchEvent(new CustomEvent('shellmate:dashboard-changed'));
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
    window.dispatchEvent(new CustomEvent('shellmate:dashboard-changed'));
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

  /**
   * Does a tab's group fall within the selected one (#191)?
   *
   * A prefix test, not equality. Selecting a site used to hide every tab in
   * it, because their group is `site-001/core switches` and the selection is
   * `site-001` — so the parent showed nothing, which is the opposite of what
   * the tree does, where a parent aggregates everything beneath it.
   *
   * The separator matters: `site-1` must not swallow `site-10`.
   */
  function _within(group, selected) {
    if (!selected) return true;
    return group === selected || group.startsWith(`${selected}/`);
  }

  /** The current mode. 'manual' means this module does nothing at all. */
  let _order = 'manual';

  /** Tag lookup by session, filled lazily — a profile match costs a fetch. */
  const _tagCache = new Map();

  /** Failed lookups per session, so a retry is bounded rather than forever. */
  const _tagRetries = new Map();

  /**
   * The profile list, one fetch shared by everyone who asks (#215).
   *
   * Five tabs opening together used to fire five fetches of the whole list —
   * heaviest exactly when the backend is busiest establishing sessions, which
   * is when one of them is most likely to lose. Cached for a few seconds:
   * long enough to cover a burst of openings, short enough that an edit to a
   * profile still shows up. A failure is never cached.
   */
  let _profilesShared = null;   // { at, promise }
  function _allProfiles() {
    const now = Date.now();
    if (_profilesShared && now - _profilesShared.at < 5000) {
      return _profilesShared.promise;
    }
    const promise = fetch('/api/profiles').then(res => {
      if (!res.ok) throw new Error(`profiles returned ${res.status}`);
      return res.json();
    });
    promise.catch(() => {
      if (_profilesShared && _profilesShared.promise === promise) {
        _profilesShared = null;
      }
    });
    _profilesShared = { at: now, promise };
    return promise;
  }

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
    _paintGroups();

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
    // Learned whenever the strip is grouped *or* sorted by group. Gated on
    // the sort order alone, a tab opened while merely showing groups had no
    // group to show.
    const wanted = _order === 'tag' || _groupStripe();
    if (!wanted || _tagCache.has(tab.sessionId)) return;
    // The tab may have closed between a failure and its retry.
    if (!tabs.some(t => t.sessionId === tab.sessionId)) return;
    try {
      const profile = await _profileFor(tab);
      const tag = ((profile || {}).tags || [])[0] || '';
      _tagCache.set(tab.sessionId, tag.toLowerCase());
    } catch (_) {
      // Left unlearned rather than cached as "no group". Writing '' here
      // turned a lost fetch into the answer for the life of the session —
      // one tab kept its group colour while its four siblings, opened in the
      // same burst, silently lost theirs (#215). Retry a few times, backing
      // off, then stay quiet until something asks again.
      const tries = (_tagRetries.get(tab.sessionId) || 0) + 1;
      _tagRetries.set(tab.sessionId, tries);
      if (tries <= 3) setTimeout(() => _learnTag(tab), 2000 * tries);
      return;
    }
    _tagRetries.delete(tab.sessionId);
    sortTabs();
    _paintGroups();
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
  // Showing the grouping, not just sorting by it (#140)
  //
  // Tab ordering could already group tabs together; nothing said *why* they
  // were adjacent. Twelve tabs across three estates read as twelve things.
  //
  // A coloured spine and a label on the first tab of each run, rather than
  // header elements inserted into the strip. That is deliberate: sortTabs()
  // keeps the `tabs` array and the DOM in step, and Ctrl+1..9, closeTab's
  // neighbour selection and the drag handler all index into that array. A
  // header is not a tab and must never become index 0.
  // -------------------------------------------------------------------------

  // The status-bar group chip that lived here (#149) was removed (#224) —
  // the group is on the tree and the tab strip already. The brand and window
  // title still follow the active tab's site via _paintBrand(), which the old
  // repaint carried along and its call sites now invoke directly.

  /**
   * The site on the brand, and on the window title (#190).
   *
   * A tab is too narrow for `site-001/core switches`, so it carries the
   * subgroup alone — which leaves nothing anywhere saying *which site*. The
   * top-level group goes here instead, where there is room for it and where
   * it is true of the whole view rather than of one tab.
   *
   * Taken from the selection when there is one, and otherwise from the active
   * tab, so it still reads correctly for somebody who never touches the tree.
   */
  function _paintBrand() {
    const brand = document.getElementById('tab-bar-brand');

    const selected = window.shellmateGroups ? window.shellmateGroups.active() : '';
    const tab = getActiveTab();
    const key = selected || (tab ? (_tagCache.get(tab.sessionId) || '') : '');
    const site = key ? key.split('/')[0] : '';

    if (brand) {
      // The brand is the brand (#216). The suffix made it read as a
      // navigation element and change width with every selection — the
      // selected group is already on the tree and the dashboard heading.
      brand.textContent = 'ShellMate Portable';
      brand.title = site ? `Home — showing ${site}` : 'Home / Quick connect';
    }
    // The native window's own title bar keeps the site: it is the only place
    // it shows when the window is not focused.
    document.title = site ? `ShellMate Portable — ${site}` : 'ShellMate Portable';
  }

  /**
   * Scroll a tab into view, and mark whether the strip overflows (#206).
   *
   * The strip is `overflow-x: auto` with the scrollbar hidden, so a tab past
   * the right edge was simply invisible — and selecting one did not bring it
   * back, because nothing ever set scrollLeft. With fourteen tabs the last
   * sat 378px beyond the container while the strip gave no sign there was
   * anything there.
   *
   * `nearest` rather than `center`: shifting the whole strip every time the
   * tab beside the current one is picked is its own kind of disorienting.
   */
  function _revealTab(tab) {
    const list = document.getElementById('tab-list');
    if (!list) return;
    if (tab && tab.tabEl) {
      try {
        tab.tabEl.scrollIntoView({ block: 'nearest', inline: 'nearest' });
      } catch (_) {
        // Older engines take no options; the default is still better than
        // leaving the selected tab off screen.
        try { tab.tabEl.scrollIntoView(); } catch (_) {}
      }
    }
    _markOverflow();
  }

  function _markOverflow() {
    const list = document.getElementById('tab-list');
    if (!list) return;
    list.classList.toggle('tabs-overflowing',
                          list.scrollWidth > list.clientWidth + 1);
  }

  /** Whether to show grouping at all. */
  function _groupStripe() {
    const prefs = (window.shellmateSettings || {}).interface || {};
    return prefs.tab_groups !== false;
  }

  /** Group colours, so the strip and the dashboard agree. */
  let _groupColours = null;

  async function _loadGroupColours() {
    // Already invalidated on 'shellmate:groups-changed' below, so a cache hit
    // here is correct rather than merely cheap — and it takes two of the
    // three /api/groups fetches a single click was making off the wire (#188).
    if (_groupColours) return _groupColours;
    try {
      const res = await fetch('/api/groups');
      const data = res.ok ? await res.json() : { groups: [] };
      _groupColours = {};
      (data.groups || []).forEach(g => { _groupColours[g.key] = g.colour; });
    } catch (_) {
      _groupColours = {};
    }
    return _groupColours;
  }

  window.addEventListener('shellmate:groups-changed', () => {
    _groupColours = null;
    // The learned group per session goes too (#169). It was written once and
    // never invalidated, so moving an open device to another group left its
    // tab showing — and being grouped under — the one it had left, for the
    // life of the session. The membership had changed; only the tab had not
    // heard.
    _tagCache.clear();
    tabs.forEach(_learnTag);
    _paintGroups();
  });

  /**
   * Mark each run of same-group tabs.
   *
   * Only where tabs are actually adjacent. Showing a group label on scattered
   * tabs would claim a grouping the strip is not showing — with a manual
   * order, the honest thing is the colour alone.
   */
  async function _paintGroups() {
    const show = _groupStripe();
    const colours = show ? await _loadGroupColours() : {};
    // Whole-tab colour (#434) is a class on the strip, so the CSS decides
    // and the per-tab painting below stays exactly as it was.
    const fill = show && ((window.shellmateSettings || {}).interface || {}).tab_fill === true;
    if (tabList) tabList.classList.toggle('tab-fill', fill);

    tabs.forEach((tab, index) => {
      const el = tab.tabEl;
      el.classList.remove('tab-group-start', 'tab-group-member',
                          'tab-outside-group');
      el.removeAttribute('data-group');
      [...el.classList].filter(c => c.startsWith('group-'))
        .forEach(c => el.classList.remove(c));
      const label = el.querySelector('.tab-group-label');
      if (label) label.remove();
      if (!show) return;

      const group = _tagCache.get(tab.sessionId) || '';

      // Selecting a group on the dashboard narrows the strip to it (#165).
      //
      // Hiding a live session is a stronger thing than filtering a list of
      // shortcuts, so the array is never filtered — only the DOM. Ctrl+1..9,
      // closeTab's neighbour selection and the drag handler all index into
      // that array, and #140 was careful to keep the two in step: a keystroke
      // must not silently change meaning because of a selection made on
      // another screen, and a device must not become unreachable by the route
      // somebody's fingers already know.
      // Selecting a group emphasises its tabs; it never hides one (#195,
      // #197).
      //
      // #165 hid everything outside the selection, and #191 widened that to
      // parent groups. Both were wrong in the same way: a live session that
      // cannot be seen is one somebody forgets is running, on a device that
      // may be mid-reload. The problem hiding solved — forty tabs is
      // unreadable — is real, and the answer to it is emphasis.
      const selected = window.shellmateGroups
        ? window.shellmateGroups.active() : '';
      const inside = Boolean(selected) && _within(group, selected);
      // Only the outside class carries styling (#202) — see the note in the
      // stylesheet. Emphasis is the others receding, not this one lighting up
      // in a way indistinguishable from being active.
      el.classList.toggle('tab-outside-group', Boolean(selected) && !inside);

      if (!group) return;

      el.classList.add('tab-group-member', `group-${colours[group] || 'slate'}`);
      el.dataset.group = group;

      const previous = tabs[index - 1];
      const sameAsPrevious = previous
        && (_tagCache.get(previous.sessionId) || '') === group;
      if (sameAsPrevious) return;

      // The run is marked, but not named (#199).
      //
      // #190 put the group's name on the first tab of each run, because
      // nothing else said which group a tab belonged to. That reason is gone:
      // the tree is permanently on screen now (#196) and says it far better
      // than a word squeezed onto a tab. What is left is the colour and the
      // run boundary, which cost no width at all.
      el.classList.add('tab-group-start');
    });

    _paintBrand();
    _markOverflow();
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
   * What the tab menu can offer, as data.
   *
   * This was a hand-written innerHTML template. Making the entries
   * configurable (#141) turns them into a list, which is the same move
   * advanced.py made for settings and for the same reason: two
   * hand-maintained lists of one thing drift, and the drift is silent. A new
   * entry is one row here — the menu and its Settings toggles both render
   * from it.
   *
   * Grouped, because **separators are derived, not placed**. A separator
   * written between two entries becomes a stray line the moment either is
   * switched off; a separator between two *groups that rendered something*
   * cannot.
   *
   * `when` is applicability — Reconnect on a live tab is meaningless whatever
   * the preference says. The setting is a second, independent condition.
   */
  const TAB_MENU_GROUPS = [
    [
      { action: 'reconnect', icon: 'add_circle', label: 'Reconnect',
        setting: 'reconnect', when: (tab) => tab && !tab.isConnected },
      // The fleet variant (#241): after an outage drops half the strip,
      // reconnecting one tab at a time is the chore this menu exists to end.
      { action: 'reconnect-all', icon: 'add_circle', label: 'Reconnect all tabs',
        setting: 'reconnect_all', when: () => tabs.some(t => !t.isConnected) },
      // The menu is where the mouse already is (#242) — same route as
      // Ctrl+T and the New button.
      { action: 'new-connection', icon: 'add', label: 'New connection',
        setting: 'new_connection' },
    ],
    [
      { action: 'clear', icon: 'backspace', label: 'Clear console',
        setting: 'clear' },
      // Named for what it has always done (#298): the loop walks the whole
      // xterm buffer, scrollback included. "Copy history" undersold it and
      // left people looking for the entry that copies everything.
      { action: 'copy', icon: 'content_copy', label: 'Copy all scrollback',
        setting: 'copy' },
      { action: 'copy-screen', icon: 'content_copy', label: 'Copy visible screen',
        setting: 'copy_screen' },
      // The address is what you need somewhere else: a ticket, a chat, a
      // firewall rule. The tab shows the device's *name* once it announces
      // itself, which is exactly when the address stops being on screen.
      { action: 'copy-address', icon: 'lan', label: 'Copy address',
        setting: 'copy_address', value: (tab) => _addressOf(tab) },
    ],
    [
      // An ad-hoc connection could not be kept: the only way was to retype
      // the whole thing into the dialog.
      { action: 'save-connection', icon: 'bookmark_add',
        label: 'Save this connection', setting: 'save_connection' },
      { action: 'duplicate', icon: 'tab_duplicate', label: 'Duplicate session',
        setting: 'duplicate' },
      // Two consoles on the same stack print the same prompt (#409). A name
      // given here is kept when the device announces its hostname.
      { action: 'rename', icon: 'edit', label: 'Rename tab…', setting: 'rename' },
    ],
    [
      // Through the session that is already open (#405, #407): a web page
      // on the far side, or a block of configuration with a preview first.
      { action: 'forwards', icon: 'lan', label: 'Port forwards…',
        setting: 'forwards', when: (tab) => tab && (tab.connectionType || 'ssh') === 'ssh' },
      { action: 'apply-config', icon: 'tune', label: 'Apply configuration…',
        setting: 'apply_config', when: (tab) => tab && tab.isConnected && (tab.connectionType || 'ssh') === 'ssh' },
    ],
    [
      // Colour-coding a tab is the thing a single global scheme could never
      // do: seeing at a glance which session is production.
      { action: 'scheme', icon: 'palette', label: 'Colour scheme',
        setting: 'scheme', value: (tab) => _schemeLabel(tab) },
      // Keep-alive is not free and not always wanted. The one session worth
      // keeping is usually a console during an upgrade, and turning it on
      // globally to protect one session is the wrong shape.
      { action: 'keepalive', icon: 'refresh',
        label: 'Keep this tab alive', setting: 'keep_alive',
        value: (tab) => _keepAliveLabel(tab) },
      // Its own setting, not a second row writing into 'keep_alive' (#209).
      // Two entries sharing a key rendered two checkboxes over one value, and
      // _collectTabMenu() takes them in DOM order — so the first was
      // overwritten by the second and could not be turned off on its own.
      { action: 'keepalive-all', icon: 'refresh',
        label: 'Keep all tabs alive', setting: 'keep_alive_all' },
    ],
    [
      // Ending the session without closing the tab (#208). Closing already
      // tears the session down; what this adds is keeping the buffer and the
      // Reconnect entry — the same state a session that dropped on its own
      // leaves behind.
      { action: 'disconnect', icon: 'stop_circle',
        label: 'Disconnect session', setting: 'disconnect',
        when: (tab) => tab && tab.isConnected },
      { action: 'disconnect-all', icon: 'stop_circle',
        label: 'Disconnect all sessions', setting: 'disconnect_all',
        when: () => tabs.some(t => t.isConnected) },
    ],
    [
      // The × on the tab does the same thing, but the menu is where people
      // look when they are already in it — and "all" has no other home (#214).
      { action: 'close', icon: 'close', label: 'Close tab',
        setting: 'close' },
      { action: 'close-all', icon: 'delete_sweep', label: 'Close all tabs',
        setting: 'close_all', when: () => tabs.length > 1 },
    ],
  ];

  /** "on" / "off", so a toggle shows its state rather than only setting it. */
  function _keepAliveLabel(tab) {
    if (!tab) return '';
    if (tab.keepAlive === true)  return 'on';
    if (tab.keepAlive === false) return 'off';
    const global = ((window.shellmateSettings || {}).terminal || {}).keep_alive;
    return global ? 'on (setting)' : 'off (setting)';
  }

  /** What scheme this tab is currently using, for the menu entry. */
  function _schemeLabel(tab) {
    if (!tab || typeof window.sessionScheme !== 'function') return '';
    const own = window.sessionScheme(tab.sessionId);
    return own ? own.replace(/_/g, ' ') : '';
  }

  /** The two sections that are not simple rows, so they can be toggled too. */
  const TAB_MENU_SECTIONS = [
    { setting: 'panes', label: 'Move to pane' },
    { setting: 'quick_broadcast', label: 'Quick broadcast' },
    { setting: 'special_commands', label: 'Send special command' },
  ];

  /** Every toggleable entry, for the Settings panel to render itself from. */
  function tabMenuItems() {
    return [
      ...TAB_MENU_GROUPS.flat().map(i => ({ setting: i.setting, label: i.label })),
      ...TAB_MENU_SECTIONS.map(s => ({ setting: s.setting, label: s.label })),
    ];
  }

  /** Whether an entry is switched on. Absent means on — see the defaults. */
  function _menuEnabled(setting) {
    const prefs = ((window.shellmateSettings || {}).interface || {}).tab_menu || {};
    return prefs[setting] !== false;
  }

  function _addressOf(tab) {
    const address = tab ? (tab.address || tab.hostname || '') : '';
    if (!address) return '';
    return (tab.port && tab.port !== 22 && tab.connectionType === 'ssh')
      ? `${address}:${tab.port}` : address;
  }

  function _showTabContextMenu(e, sessionId) {
    e.preventDefault();
    _hideTabContextMenu();
    _ctxSessionId = sessionId;

    const tab = tabs.find(t => t.sessionId === sessionId);

    _ctxMenu = document.createElement('div');
    _ctxMenu.className = 'tab-context-menu';

    // Built with createElement rather than innerHTML. The address is user
    // input from the connection dialog, and this way it cannot be markup at
    // all rather than being escaped on the way in.
    let rendered = 0;
    TAB_MENU_GROUPS.forEach(group => {
      const visible = group.filter(item =>
        _menuEnabled(item.setting) && (!item.when || item.when(tab)));
      if (!visible.length) return;

      if (rendered) {
        const sep = document.createElement('div');
        sep.className = 'ctx-sep';
        _ctxMenu.appendChild(sep);
      }
      rendered += 1;

      visible.forEach(item => {
        const button = document.createElement('button');
        button.dataset.action = item.action;

        const icon = document.createElement('span');
        icon.className = 'material-symbols-outlined';
        icon.textContent = item.icon;
        button.appendChild(icon);
        button.appendChild(document.createTextNode(item.label));

        const value = item.value ? item.value(tab) : '';
        if (value) {
          const shown = document.createElement('span');
          shown.className = 'ctx-value';
          shown.textContent = value;
          button.appendChild(shown);
        }
        _ctxMenu.appendChild(button);
      });
    });

    // Only offered when there is more than one pane to choose between —
    // "move to pane 1" on a single layout is a menu entry that does nothing.
    const panes = window.shellmateLayout ? window.shellmateLayout.panes() : 1;
    if (panes > 1 && _menuEnabled('panes')) {
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

    // Positioning, dismissal and the keyboard model come from the shared
    // menu (#425) — this one is only *built* here, because it is driven by
    // its own table and grows submenus after opening.
    window.shellmateMenu.attach(_ctxMenu, e);

    // Filled in behind the menu, so a right-click never waits on a request.
    if (_menuEnabled('special_commands')) _appendSpecialCommands(_ctxMenu, tab);
    if (_menuEnabled('quick_broadcast')) _appendQuickBroadcast(_ctxMenu, tab);

    _ctxMenu.addEventListener('click', (ev) => {
      const btn = ev.target.closest('[data-action]');
      if (!btn) return;
      const tab = tabs.find(t => t.sessionId === _ctxSessionId);
      if (tab) {
        switch (btn.dataset.action) {
          case 'reconnect': _reconnectSession(tab);   break;
          case 'reconnect-all':
            // Sequentially, not in a burst — the reconnects share the
            // backend and the credentials path, and a device that just came
            // back does not need five simultaneous logins from one client.
            (async () => {
              for (const t of tabs.filter(x => !x.isConnected)) {
                await _reconnectSession(t);
              }
            })();
            break;
          case 'new-connection':
            if (typeof window.openNewTabTarget === 'function') window.openNewTabTarget();
            else if (typeof window.showConnectionDialog === 'function') window.showConnectionDialog();
            break;
          case 'clear':     _clearConsole(tab);       break;
          case 'copy':        _copyHistory(tab);        break;
          case 'copy-screen': _copyHistory(tab, true);  break;
          case 'copy-address': _copyAddress(tab);      break;
          case 'save-connection': _saveConnection(tab);  break;
          case 'duplicate': _duplicateSession(tab);   break;
          case 'rename':    _renameTab(tab);          break;
          case 'forwards':  window.shellmateForwards && window.shellmateForwards.open(tab); break;
          case 'apply-config': window.shellmateConfigPush && window.shellmateConfigPush.open(tab); break;
          case 'scheme':    _chooseScheme(tab);       break;
          case 'keepalive':     _toggleKeepAlive([tab]);          break;
          case 'keepalive-all': _toggleKeepAlive(tabs.slice());   break;
          case 'disconnect':     _disconnectSessions([tab]);        break;
          case 'disconnect-all': _disconnectSessions(
                                   tabs.filter(t => t.isConnected)); break;
          // By session id, not index (#181): the menu can outlive a reorder.
          case 'close':     window.closeTabBySessionId(tab.sessionId); break;
          case 'close-all': _closeAllTabs();                        break;
          case 'pane':
            window.shellmateLayout.place(Number(btn.dataset.pane), tab.sessionId);
            break;
        }
      }
      _hideTabContextMenu();
    });
  }

  /**
   * End sessions without closing their tabs (#208).
   *
   * Confirmed, and the count named. This drops live connections, which is the
   * thing the interface is otherwise careful never to do by accident —
   * closing the window only hides it for exactly that reason.
   *
   * POST .../disconnect rather than DELETE: the tab, its buffer and its
   * transcript stay, and Reconnect is one click away.
   */
  async function _disconnectSessions(targets) {
    const live = (targets || []).filter(t => t && t.isConnected);
    if (!live.length) return;

    const many = live.length > 1;
    const ok = await window.shellmateDialog.confirm({
      title: many
        ? `Disconnect ${live.length} sessions?`
        : `Disconnect ${live[0].label}?`,
      body: 'The connection closes on the device as well as here, and '
            + 'anything running in it stops. The tab stays, so you keep what '
            + 'is on screen and can reconnect.',
      confirmLabel: many ? 'Disconnect them' : 'Disconnect it',
      danger: true,
    });
    if (!ok) return;

    for (const tab of live) {
      // Deliberate, so the drop is not treated as one to retry — the same
      // flag closeTab sets before it confirms.
      tab.closingDeliberately = true;
      _stopRetrying(tab, '');
      try {
        await fetch(`/api/sessions/${tab.sessionId}/disconnect`,
                    { method: 'POST' });
      } catch (_) { /* the tab going red is the report */ }
      updateTabStatus(tab.sessionId, false);
    }
  }

  /**
   * Close every tab, asking once for the lot rather than once per tab (#214).
   *
   * Each close still goes through closeTab — disconnect first, then teardown
   * — with `force` so the one confirmation here is the only question.
   */
  async function _closeAllTabs() {
    if (!tabs.length) return;

    const settings = (window.shellmateSettings || {}).interface || {};
    const connected = tabs.filter(t => t.isConnected).length;
    if (settings.confirm_close_tab !== false && connected) {
      const ok = await window.shellmateDialog.confirm({
        title: `Close all ${tabs.length} tabs?`,
        body: (connected === 1
                ? 'One session is still connected'
                : `${connected} sessions are still connected`)
              + ' and will be disconnected. Anything already scheduled on a '
              + 'device — a pending reload — carries on regardless.',
        confirmLabel: 'Close all',
        danger: true,
      });
      if (!ok) return;
    }

    // By session id, not index (#181): every close shifts the indices.
    for (const sid of tabs.map(t => t.sessionId)) {
      await window.closeTabBySessionId(sid, { force: true });
    }
  }

  function _hideTabContextMenu() {
    // The shared menu removes every `.tab-context-menu` on the page, the
    // submenus included, and takes its own listeners off (#429).
    if (window.shellmateMenu) window.shellmateMenu.close();
    else document.querySelectorAll('.tab-context-menu').forEach(el => el.remove());
    _ctxMenu = null;
  }

  /**
   * Turn keep-alive on or off for one tab, or for all of them.
   *
   * The state is held on the session server-side, because that is where the
   * timer runs. The tab keeps a copy purely so the menu can show which way
   * the toggle currently sits — a toggle that does not say whether it is on
   * is a toggle nobody trusts.
   */
  function _toggleKeepAlive(list) {
    const connected = list.filter(t => t.isConnected);
    if (!connected.length) return;

    // Toggling "all" follows the first tab, so the action has one outcome
    // rather than inverting each tab independently and leaving a mixture.
    const global = ((window.shellmateSettings || {}).terminal || {}).keep_alive === true;
    const current = connected[0].keepAlive;
    const wanted = !(current === undefined || current === null ? global : current);

    connected.forEach(tab => {
      tab.keepAlive = wanted;
      try {
        tab.websocket.send(JSON.stringify({ type: 'keep_alive', enabled: wanted }));
      } catch (_) { /* a closed socket has nothing to keep alive */ }
    });

    if (window.shellmateAlerts) {
      window.shellmateAlerts.notify({
        icon: 'refresh',
        title: wanted
          ? `Keeping ${connected.length} session${connected.length === 1 ? '' : 's'} alive`
          : `Stopped keeping ${connected.length} session${connected.length === 1 ? '' : 's'} alive`,
        body: wanted
          ? 'A space and a backspace are sent at a prompt when the session '
            + 'goes quiet, which nets to nothing typed.'
          : 'The device may now idle them out on its own timeout.',
      });
    }
  }

  /**
   * Give this tab its own colour scheme.
   *
   * Remembered against the address rather than the session, because a session
   * id does not survive a reconnect and the thing being marked is the
   * *device*. Keyed the same way the detected-hostname map is, so an ad-hoc
   * connection with no saved profile is colour-coded just as well as a saved
   * one — which matters, since the tab you most want marked red is often the
   * one you opened in a hurry.
   */
  async function _chooseScheme(tab) {
    const schemes = typeof window.colorSchemeList === 'function'
      ? window.colorSchemeList() : [];
    const current = typeof window.sessionScheme === 'function'
      ? window.sessionScheme(tab.sessionId) : '';

    const answer = await window.shellmateDialog.form({
      title: `Colour scheme for ${tab.label}`,
      body:  'Applies to this tab only, and to this device next time you '
             + 'connect to it.',
      note:  'The global scheme under Settings keeps its own value — changing '
             + 'it will not undo this.',
      confirmLabel: 'Apply',
      fields: [
        { name: 'scheme', label: 'Scheme', type: 'select', value: current,
          options: [{ value: '', label: 'Follow the global setting' },
                    ...schemes] },
      ],
    });
    if (!answer) return;

    if (typeof window.setSessionScheme === 'function') {
      window.setSessionScheme(tab.sessionId, answer.scheme);
    }
    _rememberScheme(tab, answer.scheme);
  }

  /** The key a per-device scheme is filed under. */
  function _schemeKey(tab) {
    const address = tab.address || tab.hostname || '';
    return address ? `${address}:${tab.port || 0}` : '';
  }

  function _rememberScheme(tab, scheme) {
    const key = _schemeKey(tab);
    if (!key || !window.shellmatePrefs) return;
    const all = { ...(((window.shellmateSettings || {}).interface || {}).tab_schemes || {}) };
    if (scheme) all[key] = scheme;
    else delete all[key];
    window.shellmatePrefs.set('tab_schemes', all);
  }

  /** Apply a remembered scheme when a tab opens. */
  function _restoreScheme(tab) {
    const key = _schemeKey(tab);
    if (!key) return;
    const all = ((window.shellmateSettings || {}).interface || {}).tab_schemes || {};
    if (all[key] && typeof window.setSessionScheme === 'function') {
      window.setSessionScheme(tab.sessionId, all[key]);
    }
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
  /**
   * Copy the buffer — everything, or just what is on screen (#298).
   *
   * Everything means the scrollback too, which is what this always did;
   * the visible screen is the narrower answer for pasting one command's
   * output into a ticket without the hour that preceded it.
   */
  function _copyHistory(tab, visibleOnly) {
    try {
      const buf   = tab.terminalInstance.buffer.active;
      const lines = [];
      const first = visibleOnly ? buf.viewportY : 0;
      const last  = visibleOnly
        ? Math.min(buf.length, buf.viewportY + tab.terminalInstance.rows)
        : buf.length;
      for (let i = first; i < last; i++) {
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

    const items = [
      { icon: 'add_circle', label: 'New session', onClick: () => {
          if (typeof window.showConnectionDialog === 'function') window.showConnectionDialog();
        } },
    ];
    if (profiles.length) {
      items.push('sep', { heading: 'Saved connections' });
      profiles.slice(0, 12).forEach(p => items.push({
        icon: p.connection_type === 'serial' ? 'cable' : 'terminal',
        // A profile name is user input; the builder sets it as text.
        label: p.name || p.hostname || p.serial_port || 'unnamed',
        onClick: () => {
          if (typeof window.showConnectionDialog === 'function') {
            window.showConnectionDialog(p);
          }
        },
      }));
    }
    _ctxMenu = window.shellmateMenu.open(e, items);
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
    const profile = await _profileForQuiet(tab);
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
    // Throws when the list cannot be fetched, deliberately. Returning null
    // here made "the request failed" indistinguishable from "no profile
    // matches", and callers cached the failure as the answer — five tabs
    // opening at once is exactly when a fetch is most likely to lose, and
    // four of them wore the wrong group for the life of the session (#215).
    const profiles = await _allProfiles();

    // Asked and answered, when the session recorded where it came from
    // (#187). The scoring below is a good guess and a guess is all it is:
    // across an estate sharing one address it returned whichever profile was
    // saved first, so tabs were labelled with another subgroup's name.
    if (tab.profileId) {
      const exact = profiles.find(p => p.id === tab.profileId);
      if (exact) return exact;
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
   * _profileFor with a fetch failure read as "no profile".
   *
   * For the reconnect and new-tab paths, where the conservative guess is the
   * right behaviour and a throw would break the flow. The group-tag path must
   * NOT use this — there, failure and "no group" mean different things (#215).
   */
  async function _profileForQuiet(tab) {
    try { return await _profileFor(tab); } catch (_) { return null; }
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

    let profile = options.profile || null;
    if (!profile) {
      profile = await _profileForQuiet(tab);
    }

    if (profile && profile.has_saved_credentials) {
      try {
        // Through the shared helper (#406), so a device that asks for a
        // second factor on reconnect gets asked, not refused.
        const posted = await window.postSession({
            connection_type: profile.connection_type || 'ssh',
            hostname:        profile.hostname || '',
            port:            profile.port || 22,
            username:        profile.username || '',
            serial_port:     profile.serial_port || '',
            baud_rate:       profile.baud_rate || 9600,
            display_label:   profile.name || '',
            profile_id:      profile.id,
        });
        const res = posted.response;
        if (res.ok) {
          const data = posted.data;
          // force: the tab being replaced is the dead one the user asked to
          // reconnect. It would not be asked about today — a disconnected tab
          // closes without a word — but saying so keeps this correct if that
          // ever changes, now that closing is asynchronous.
          //
          // By identity, not by an index taken at entry (#482). Several of
          // these run at once — "Reconnect all", the per-tab retry timers —
          // and each awaits twice before closing. The first to finish is
          // spliced out, and an index captured before that pointed at
          // whichever live tab had shifted into its place, which was then
          // force-closed and deleted on the server while the dead tab stayed.
          await _closeTabObject(tab, { force: true });
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
  /**
   * The control sequences a keyboard cannot send (#299).
   *
   * Data from settings rather than a list here, because which ones matter
   * depends on the estate — and because a break is worth reaching in a
   * hurry, halfway through a device booting. Break goes as its own message:
   * it is an out-of-band signal on the serial line, not a character.
   */
  function _appendSpecialCommands(menu, tab) {
    if (menu.dataset.specialAdded) return;
    menu.dataset.specialAdded = '1';

    const commands = ((window.shellmateSettings || {}).interface || {})
      .special_commands || [];
    if (!commands.length) return;

    if (menu.children.length) {
      const sep = document.createElement('div');
      sep.className = 'ctx-sep';
      menu.appendChild(sep);
    }

    // One row, opening a submenu (#306). Listed inline these were nine
    // entries on a menu that already had a dozen, and a menu long enough to
    // need reading is a menu nobody reads.
    const row = document.createElement('button');
    row.type = 'button';
    row.className = 'ctx-submenu-row';

    const icon = document.createElement('span');
    icon.className = 'material-symbols-outlined';
    icon.textContent = 'keyboard';

    const chevron = document.createElement('span');
    chevron.className = 'material-symbols-outlined ctx-submenu-arrow';
    chevron.textContent = 'keyboard_arrow_right';

    row.append(icon, document.createTextNode('Send special command'), chevron);

    // Opens on hover, the way a submenu is expected to (#307). The delay is
    // the whole trick: sliding the pointer down the menu crosses this row on
    // the way to something else, and opening instantly would flash a panel
    // over whatever was being aimed at.
    let hoverTimer = null;
    row.addEventListener('mouseenter', () => {
      clearTimeout(hoverTimer);
      hoverTimer = setTimeout(() => _openSpecialSubmenu(row, tab, commands),
                              SUBMENU_DELAY_MS);
    });
    row.addEventListener('mouseleave', () => clearTimeout(hoverTimer));

    // Click still opens it, immediately — for touch, for keyboards, and for
    // anyone who does not wait.
    row.addEventListener('click', (e) => {
      // Or the document-level dismissal takes the whole menu with it before
      // the submenu is on screen.
      e.stopPropagation();
      clearTimeout(hoverTimer);
      _openSpecialSubmenu(row, tab, commands);
    });

    // Leaving the row for anything else in the menu closes it again, so the
    // submenu does not sit over the rows below while they are being read.
    menu.addEventListener('mouseover', (e) => {
      if (row.contains(e.target)) return;
      if (e.target.closest && e.target.closest('.ctx-submenu')) return;
      clearTimeout(hoverTimer);
      document.querySelectorAll('.ctx-submenu').forEach(el => el.remove());
    });

    menu.appendChild(row);
  }

  /** How long the pointer must rest on a submenu row before it opens (#307). */
  const SUBMENU_DELAY_MS = 150;

  /** The submenu itself, beside the row that opened it (#306). */
  function _openSpecialSubmenu(row, tab, commands) {
    document.querySelectorAll('.ctx-submenu').forEach(el => el.remove());

    // Shares the context-menu class, so every existing dismissal path —
    // outside click, Escape, opening another menu — cleans it up too.
    const submenu = document.createElement('div');
    submenu.className = 'tab-context-menu ctx-submenu';

    commands.forEach(entry => {
      if (!entry || !entry.name) return;
      const button = document.createElement('button');
      button.type = 'button';

      const glyph = document.createElement('span');
      glyph.className = 'material-symbols-outlined';
      glyph.textContent = entry.kind === 'break' ? 'stop_circle' : 'keyboard';

      button.appendChild(glyph);
      button.appendChild(document.createTextNode(entry.name));
      if (entry.hint) button.title = entry.hint;

      // A break only means something on a serial line; offering it elsewhere
      // is offering something that will silently do nothing.
      const serialOnly = entry.kind === 'break'
                      && (tab.connectionType || 'ssh') !== 'serial';
      if (serialOnly || !tab.isConnected) {
        button.disabled = true;
        button.title = serialOnly
          ? 'Break is a serial signal — this session is not a serial console.'
          : 'The session is not connected.';
      } else {
        button.addEventListener('click', (e) => {
          e.stopPropagation();
          _hideTabContextMenu();
          _sendSpecial(tab, entry);
        });
      }
      submenu.appendChild(button);
    });

    document.body.appendChild(submenu);

    // Beside the row, flipping to its left when there is no room on the
    // right, and clamped vertically — the tab menu is often near an edge.
    const anchor = row.getBoundingClientRect();
    const width = submenu.offsetWidth;
    const height = submenu.offsetHeight;
    const right = anchor.right + width + 8 <= window.innerWidth
      ? anchor.right + 2
      : Math.max(8, anchor.left - width - 2);
    submenu.style.left = `${right}px`;
    submenu.style.top = `${Math.max(8,
      Math.min(anchor.top - 4, window.innerHeight - height - 8))}px`;
    // No dismissal of its own: it shares the class, so the shared menu's
    // outside-click, Escape and close() all sweep it up with the parent.
  }

  /** Put one special command on the wire. */
  function _sendSpecial(tab, entry) {
    const ws = tab.websocket;
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    if (entry.kind === 'break') {
      ws.send(JSON.stringify({ type: 'break' }));
    } else if (entry.data) {
      ws.send(JSON.stringify({ type: 'input', data: entry.data }));
    }
    if (window.shellmateAlerts && window.shellmateAlerts.notify) {
      // Said out loud, like every other thing this application types into a
      // live session on somebody's behalf.
      window.shellmateAlerts.notify({
        icon:  'keyboard',
        title: `Sent ${entry.name} to ${tab.label}`,
        sessionId: tab.sessionId,
      });
    }
  }

  async function _appendQuickBroadcast(menu, tab) {
    // Guard against being appended twice to one menu. The fetch is async, so
    // two calls can be in flight against the same element and the entries
    // appear doubled.
    if (menu.dataset.quickAdded) return;
    menu.dataset.quickAdded = '1';

    const snippets = await _loadQuick();
    if (!menu.isConnected) return;          // menu already dismissed

    // Only when there is something above to be divided from. With every
    // other entry switched off (#141) this section is the whole menu, and an
    // unconditional separator would lead with a stray line — the same
    // derived-not-placed rule the groups follow.
    if (menu.children.length) {
      const sep = document.createElement('div');
      sep.className = 'ctx-sep';
      menu.appendChild(sep);
    }

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
    const profile = await _profileForQuiet({
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
    profileId:      t.profileId || '',
    label:          t.label,
    hostname:       t.hostname,
    // What was dialled, never rewritten — `hostname` above becomes whatever
    // the device calls itself. Anything matching a tab to a saved connection
    // by address needs this one.
    address:        t.address || t.hostname || '',
    username:       t.username || '',
    // Address alone does not identify a device — a lab of containers behind
    // one address is distinguished only by port, which is why the group
    // counts match on both. Anything matching sessions to profiles needs it.
    port:           t.port || 0,
    connectionType: t.connectionType,
    isConnected:    t.isConnected,
  }));

  /** Return the tab object at 1-based tab number, or null. */
  window.getTabByNumber = (n) => tabs[n - 1] || null;

  /**
   * Recent-buffer size for one session, for the context meter.
   *
   * By session id rather than handing out the tab object — those carry live
   * xterm and WebSocket handles that nothing outside this module should be
   * reaching into.
   */
  window.getContextCharsBySessionId = (sessionId, lines) => {
    const t = tabs.find(x => x.sessionId === sessionId);
    return (t && t.getContextChars) ? t.getContextChars(lines) : 0;
  };

  window.createTab        = createTab;
  window.switchToTab      = switchToTab;
  window.switchToTabBySessionId = (sessionId) => {
    const idx = tabs.findIndex(t => t.sessionId === sessionId);
    if (idx !== -1) switchToTab(idx);
  };
  window.closeTab         = closeTab;
  /**
   * Close a tab by its session, rather than by where it currently sits.
   *
   * Closing several in a row shifts every index behind the one that went, so
   * a caller holding a list of indices closes the wrong tabs from the second
   * one onwards. The session id does not move (#181).
   */
  window.closeTabBySessionId = (sessionId, options) => {
    const tab = tabs.find(t => t.sessionId === sessionId);
    return tab ? _closeTabObject(tab, options) : Promise.resolve();
  };
  window.getActiveTab     = getActiveTab;
  /** Everything the hover card (#435) can say about a tab. */
  window.tabTooltipInfo = (sessionId) => {
    const tab = tabs.find(t => t.sessionId === sessionId);
    if (!tab) return null;
    return {
      sessionId,
      label:        tab.label,
      autoLabel:    tab.autoLabel || '',
      customLabel:  !!tab.customLabel,
      address:      _addressOf(tab),
      port:         tab.port || 0,
      connectionType: tab.connectionType || 'ssh',
      username:     tab.username || '',
      isConnected:  !!tab.isConnected,
      group:        _tagCache.get(sessionId) || '',
      keepAlive:    !!(tab.keepAlive || tab.keep_alive),
      logging:      !!(tab.logging || tab.logEnabled),
      profileId:    tab.profileId || '',
      uptime:       (window.shellmateUptime && typeof window.shellmateUptime.label === 'function')
                      ? window.shellmateUptime.label(sessionId) : '',
    };
  };
  /** What the tab palette (#410) searches: one plain object per tab. */
  window.listTabs = () => tabs.map((t, index) => ({
    sessionId:   t.sessionId,
    index,
    label:       t.label,
    hostname:    t.hostname || '',
    address:     _addressOf(t),
    isConnected: !!t.isConnected,
    groups:      Array.isArray(t.groupNames) ? t.groupNames : [],
  }));
  window.updateTabLabel   = updateTabLabel;
  window.setTabOrder      = setTabOrder;
  window.tabMenuItems     = tabMenuItems;
  window.repaintTabGroups = _paintGroups;
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

  // The socket between this window and ShellMate dropped and is being
  // reopened (#481). Not a disconnect — the session is still up on the
  // server — so the tab says what is happening rather than going red.
  window.addEventListener('shellmate:terminal-link', (e) => {
    const detail = e.detail || {};
    const tab = tabs.find(t => t.sessionId === detail.sessionId);
    if (!tab || !tab.labelEl) return;
    if (detail.state === 'reattaching') {
      tab.labelEl.textContent = `${tab.label} (reattaching ${detail.attempt}/${detail.attempts})`;
      tab.labelEl.title = 'The link between this window and ShellMate dropped. '
        + 'The session itself is still up; the socket is being reopened.';
    } else if (detail.state === 'attached') {
      tab.labelEl.textContent = tab.label;
      tab.labelEl.title = '';
      updateStatusBar();
    }
    // 'lost' is followed by updateTabStatus(false), which labels the tab.
  });

  window.updateTabStatus  = updateTabStatus;
  window.updateStatusBar  = updateStatusBar;
  window.refitTerminals   = refitTerminals;

})();
