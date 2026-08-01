/**
 * terminal.js — xterm.js terminal initialisation for ShellMate.
 *
 * Each call to initTerminal() creates an independent xterm.js Terminal
 * instance, opens it in a new <div>, and connects it to the backend via
 * a WebSocket at /ws/terminal/{sessionId}.
 *
 * Terminal appearance is driven by settings loaded from /api/settings
 * (via settings.js).  When the user saves new settings, a
 * 'shellmate:settings-changed' event is fired and all open terminals are updated
 * live without requiring a page reload.
 *
 * Copy / paste behaviour:
 *   - Select text with mouse → auto-copies if copyOnSelect is enabled in settings.
 *   - Ctrl+Shift+C  → copy current selection to clipboard.
 *   - Ctrl+C        → copy if text is selected, otherwise sends ^C to device.
 *   - Ctrl+V / Ctrl+Shift+V → paste from clipboard (shows confirmation modal).
 *   - Double-click  → select word and copy.
 *   - Right-click   → paste from clipboard (respects right_click_paste setting).
 *
 * Inactive terminals are hidden with CSS (display:none on the container)
 * but the Terminal and WebSocket objects remain alive — so background
 * sessions continue to receive data and fill their buffers.
 */

(function () {
  'use strict';

  /** Shorthand for a Stockton value. */
  const A = (key, fallback) =>
    (window.shellmateAdvanced ? window.shellmateAdvanced(key, fallback) : fallback);

  // Track all live terminal instances so we can update them when settings change
  // { sessionId: { terminal, fitAddon, websocket, containerId } }
  const _instances = {};

  // -------------------------------------------------------------------------
  // Helpers
  // -------------------------------------------------------------------------

  /**
   * Build xterm.js constructor options from the current settings.
   */
  function _buildOptions() {
    const s = (window.shellmateSettings || {}).terminal || {};
    const a  = (window.shellmateSettings || {}).appearance || {};
    const schemeName = a.color_scheme || 'deep_space';
    const schemeObj  = typeof window.getColorScheme === 'function'
      ? window.getColorScheme(schemeName)
      : null;
    const theme = schemeObj ? Object.assign({}, schemeObj.theme) : _fallbackTheme();
    // Apply per-channel overrides if set
    if (a.foreground_override) theme.foreground = a.foreground_override;
    if (a.background_override) theme.background = a.background_override;
    // Cursor and selection are the same arrangement foreground and background
    // already had: the scheme decides unless something is set here. A bar
    // cursor inherits the scheme's colour, which on a busy screen is often the
    // one thing it should not blend into.
    if (s.cursor_colour)    theme.cursor = s.cursor_colour;
    if (s.selection_colour) theme.selectionBackground = s.selection_colour;

    return {
      theme,
      fontFamily:       s.font_family      || "'JetBrains Mono', 'Cascadia Code', 'Fira Code', 'Consolas', monospace",
      fontSize:         s.font_size        || 14,
      lineHeight:       s.line_height      || 1.2,
      cursorBlink:      s.cursor_blink     !== false,
      cursorStyle:      s.cursor_style     || 'block',
      scrollback:       s.scrollback_lines || 5000,
      copyOnSelect:     !!s.copy_on_select,
      allowProposedApi: true,
      // Passed through to xterm.js. Every default here is xterm's own, so a
      // settings file written before these existed behaves exactly as it did.
      letterSpacing:    s.letter_spacing   || 0,
      fontWeight:       s.font_weight      || 'normal',
      fontWeightBold:   s.font_weight_bold || 'bold',
      tabStopWidth:     s.tab_stop_width   || 8,
      drawBoldTextInBrightColors: s.draw_bold_in_bright !== false,
      screenReaderMode: !!s.screen_reader_mode,
      // Omitted rather than zeroed: xterm.js derives a sensible width from the
      // font, and passing 0 would draw no cursor at all.
      ...(s.cursor_width ? { cursorWidth: s.cursor_width } : {}),
      // Stockton (#57). Read at construction, which is why the renderer is
      // marked as needing a restart while the rest apply to the next tab.
      minimumContrastRatio: A('terminal.min_contrast', 1),
      wordSeparator:        A('terminal.word_separators', " ()[]{}',\"`"),
      scrollSensitivity:    A('terminal.scroll_sensitivity', 1),
      // `rendererType` was removed in xterm.js 5: the renderer is chosen by
      // loading an addon, and none is bundled. Passing it did nothing at all,
      // so the setting has moved to NOT_EXPOSED where the panel can say why
      // rather than offering a choice that changes nothing.
    };
  }

  /** Fallback theme used before settings.js has loaded. */
  function _fallbackTheme() {
    return {
      background:   '#0E0E0E',
      foreground:   '#E5E2E1',
      cursor:       '#C3C0FF',
      cursorAccent: '#0E0E0E',
    };
  }

  // -------------------------------------------------------------------------
  // initTerminal
  // -------------------------------------------------------------------------

  /**
   * Initialise a new xterm.js terminal and connect it to the backend.
   *
   * @param {string} sessionId - The UUID of the session this terminal belongs to.
   * @returns {{terminal: Terminal, fitAddon: FitAddon, websocket: WebSocket, containerId: string}}
   */
  function initTerminal(sessionId) {
    // Running count of newlines received — used by the status bar
    let _bufferLines = 0;

    // ------------------------------------------------------------------
    // 1. Create the xterm.js Terminal instance with current settings
    // ------------------------------------------------------------------
    const terminal = new window.Terminal(_buildOptions());

    // ------------------------------------------------------------------
    // 2. Load addons
    // ------------------------------------------------------------------
    const fitAddon      = new window.FitAddon.FitAddon();
    const webLinksAddon = new window.WebLinksAddon.WebLinksAddon();
    const searchAddon   = new window.SearchAddon.SearchAddon();

    terminal.loadAddon(fitAddon);
    terminal.loadAddon(webLinksAddon);
    terminal.loadAddon(searchAddon);

    // ------------------------------------------------------------------
    // 3. Create the container div and mount xterm.js
    // ------------------------------------------------------------------
    const containerId = `terminal-${sessionId}`;
    const container   = document.createElement('div');
    container.id        = containerId;
    container.className = 'terminal-container';

    document.getElementById('terminals-container').appendChild(container);

    terminal.open(container);

    // Fit after a brief paint delay so the container has real dimensions
    requestAnimationFrame(() => {
      try { fitAddon.fit(); } catch (_) {}
    });

    // ------------------------------------------------------------------
    // 4. Open WebSocket to the backend
    // ------------------------------------------------------------------
    const wsProto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl    = `${wsProto}//${window.location.host}/ws/terminal/${sessionId}`;
    const websocket = new WebSocket(wsUrl);

    // ------------------------------------------------------------------
    // 5. Wire WebSocket → terminal (incoming data from device)
    // ------------------------------------------------------------------
    websocket.addEventListener('message', (event) => {
      let msg;
      try {
        msg = JSON.parse(event.data);
      } catch (_) {
        terminal.write(event.data);
        return;
      }

      switch (msg.type) {
        case 'output':
          // Apply the user's colour rules on the way to the screen. The
          // unmodified text is what gets buffered and sent to the AI — the
          // colour is a reading aid, not part of the data.
          terminal.write(
            window.shellmateHighlight
              ? window.shellmateHighlight.apply(msg.data)
              : msg.data
          );
          // Count newlines to maintain a running buffer line total
          _bufferLines += (msg.data.match(/\n/g) || []).length;
          if (typeof window.updateStatusBar === 'function') window.updateStatusBar();
          // Notify chat.js so it can feed command output back to the AI
          window.dispatchEvent(new CustomEvent('shellmate:terminal-output', {
            detail: { sessionId, data: msg.data, totalLines: _bufferLines }
          }));
          break;

        case 'hostname_detected':
          if (typeof window.updateTabLabel === 'function') {
            window.updateTabLabel(sessionId, msg.hostname);
          }
          if (typeof window.updateStatusBar === 'function') {
            window.updateStatusBar();
          }
          break;

        case 'device_identified':
          // Say what the device was recognised as, and what was sent to it.
          // Nothing is typed into someone's session without telling them.
          window.dispatchEvent(new CustomEvent('shellmate:device-identified', {
            detail: { sessionId, ...msg }
          }));
          break;

        case 'alias_expanded':
          window.dispatchEvent(new CustomEvent('shellmate:alias-expanded', {
            detail: { sessionId, typed: msg.typed, sent: msg.sent }
          }));
          break;

        case 'guardrail_prompt':
          // A destructive command is being held. Nothing has reached the
          // device — the answer decides whether anything does.
          askBeforeSending(sessionId, websocket, msg.command, msg.device);
          break;

        case 'pending_action':
          // Something is scheduled on this device — a reload, a commit
          // waiting to be confirmed. alerts.js owns what that looks like.
          window.dispatchEvent(new CustomEvent('shellmate:pending-action', {
            detail: { sessionId, pending: msg.pending }
          }));
          break;

        default:
          break;
      }
    });

    websocket.addEventListener('close', () => {
      if (typeof window.updateTabStatus === 'function') {
        window.updateTabStatus(sessionId, false);
      }
    });

    websocket.addEventListener('error', (err) => {
      console.error(`WebSocket error for session ${sessionId}:`, err);
    });

    // ------------------------------------------------------------------
    // 6. Wire terminal → WebSocket (outgoing keystrokes to device)
    // ------------------------------------------------------------------
    terminal.onData((data) => {
      if (websocket.readyState === WebSocket.OPEN) {
        websocket.send(JSON.stringify({ type: 'input', data }));
      }
    });

    // ------------------------------------------------------------------
    // 7. Copy / paste behaviour
    // ------------------------------------------------------------------

    function _copySelection() {
      const sel = terminal.getSelection();
      if (!sel) return false;
      navigator.clipboard.writeText(sel).then(() => {
        terminal.clearSelection();
        window._showCopyToast && window._showCopyToast(sel);
      }).catch(() => {});
      return true;
    }

    function _pasteFromClipboard() {
      navigator.clipboard.readText().then(text => {
        if (!text) return;
        // Below the threshold a paste goes straight through. Every
        // multi-line paste used to ask, which somebody pasting short blocks
        // all day learns to click through.
        const lines = text.split('\n').length;
        const send = () => {
          if (websocket.readyState !== WebSocket.OPEN) return;
          // Chunked when Stockton asks for it: some devices drop characters
          // when a paste arrives faster than their input buffer drains.
          const size = A('terminal.paste_chunk_bytes', 0);
          if (!size) {
            websocket.send(JSON.stringify({ type: 'input', data: text }));
            return;
          }
          // A delay of zero makes chunking inert: `index * 0` schedules every
          // chunk at timeout 0, they all fire in one macrotask batch, and the
          // paste reaches the device exactly as fast as it did unchunked —
          // which is the one thing the setting exists to prevent. So setting
          // a chunk size and leaving the delay alone now paces anyway.
          const delay = Math.max(A('terminal.paste_chunk_delay', 20), 1);

          // Split on *bytes*, not characters. The setting is named for bytes
          // and a device's input buffer is measured in them, so a paste of
          // box-drawing or accented text was sending up to three times the
          // intended amount per chunk — on the kit that needs this, that is
          // the difference between working and dropping characters.
          const encoder = new TextEncoder();
          const decoder = new TextDecoder();
          const bytes = encoder.encode(text);
          const chunks = [];
          for (let at = 0; at < bytes.length; at += size) {
            // stream: true keeps a multi-byte character split across a chunk
            // boundary from being decoded as a replacement character.
            chunks.push(decoder.decode(bytes.slice(at, at + size), { stream: true }));
          }
          chunks.push(decoder.decode());     // flush anything held back

          chunks.filter(Boolean).forEach((chunk, index) => {
            setTimeout(() => {
              if (websocket.readyState === WebSocket.OPEN) {
                websocket.send(JSON.stringify({ type: 'input', data: chunk }));
              }
            }, index * delay);
          });
        };

        if (lines < A('terminal.paste_confirm_lines', 1) + 1) {
          send();
          return;
        }
        window._showPasteModal && window._showPasteModal(text, send);
      }).catch(() => {});
    }

    // Keyboard shortcuts — intercept before xterm.js handles them.
    // Return false = we handle it (suppress default). Return true = let xterm handle it.
    terminal.attachCustomKeyEventHandler((e) => {
      if (e.type !== 'keydown') return true;

      // Ctrl+F → find in this terminal.
      //
      // It has to be here rather than on document: xterm's own key handling
      // calls stopPropagation() for the keys it consumes, so a document-level
      // listener never sees this one. Returning false also stops ^F being
      // sent to the device, which is what would otherwise happen.
      if ((e.ctrlKey || e.metaKey) && !e.shiftKey && e.key.toLowerCase() === 'f') {
        openSearch();
        return false;
      }

      // Ctrl+Shift+C → copy
      if (e.ctrlKey && e.shiftKey && e.key === 'C') {
        _copySelection();
        return false;
      }

      // Ctrl+Shift+V or Ctrl+V → paste
      if ((e.ctrlKey && e.shiftKey && e.key === 'V') ||
          (e.ctrlKey && !e.shiftKey && e.key === 'v')) {
        _pasteFromClipboard();
        return false;
      }

      // Ctrl+C with an active selection → copy instead of sending ^C
      if (e.ctrlKey && !e.shiftKey && e.key === 'c' && terminal.hasSelection()) {
        _copySelection();
        return false;
      }

      return true;
    });

    // Copy whatever the mouse selects — a dragged range or a double-clicked
    // word. This previously only handled double-click, by flagging the second
    // mousedown and reading the selection in onSelectionChange, so dragging
    // out a range selected it but copied nothing.
    //
    // Waiting for mouseup rather than onSelectionChange is what makes a drag
    // work: xterm fires selection changes continuously while the pointer
    // moves, so copying on those would clip the selection to wherever the
    // mouse happened to be part-way through.
    container.addEventListener('mouseup', (e) => {
      if (e.button !== 0) return;            // left button only
      // The setting was passed to xterm as `copyOnSelect` and then ignored
      // here, so this handler copied whatever was selected regardless — the
      // switch appeared to do nothing because the code below did the copying
      // itself. Somebody turning it off is usually protecting a clipboard
      // they are pasting somewhere else.
      const prefs = (window.shellmateSettings || {}).terminal || {};
      if (prefs.copy_on_select === false) return;

      // Let xterm commit the selection before reading it.
      setTimeout(() => {
        if (!terminal.hasSelection()) return;
        const sel = terminal.getSelection();
        if (!sel) return;
        navigator.clipboard.writeText(sel)
          .then(() => { window._showCopyToast && window._showCopyToast(sel); })
          .catch(() => {});
      }, 0);
    });

    // Right-click: paste from clipboard
    container.addEventListener('contextmenu', (e) => {
      e.preventDefault();
      const settings = window.shellmateSettings || {};
      if (settings.terminal && settings.terminal.right_click_paste === false) return;
      _pasteFromClipboard();
    });

    // ------------------------------------------------------------------
    // 8. Handle window resize — refit the active terminal
    // ------------------------------------------------------------------
    // Named rather than anonymous so closeTab() can take it off again. An
    // anonymous closure per instance is a listener that outlives its terminal
    // and keeps the whole thing — xterm instance, addons, socket — reachable
    // forever.
    const onWindowResize = () => {
      const containerEl = document.getElementById(containerId);
      if (containerEl && containerEl.classList.contains('active')) {
        try {
          fitAddon.fit();
          if (websocket.readyState === WebSocket.OPEN) {
            websocket.send(JSON.stringify({
              type: 'resize',
              cols: terminal.cols,
              rows: terminal.rows,
            }));
          }
        } catch (_) {}
      }
    };
    window.addEventListener('resize', onWindowResize);

    // Send initial resize once the socket is open
    websocket.addEventListener('open', () => {
      try {
        fitAddon.fit();
        websocket.send(JSON.stringify({
          type: 'resize',
          cols: terminal.cols,
          rows: terminal.rows,
        }));
      } catch (_) {}
    });

    // ------------------------------------------------------------------
    // 9. Register instance so settings changes can be applied live
    // ------------------------------------------------------------------
    _instances[sessionId] = { terminal, fitAddon, searchAddon, websocket,
                              containerId, onWindowResize };

    // Returns the character count of the last `n` lines — matches what the
    // backend sends to the AI via buf.get_text(n), so the context estimate is accurate.
    function getContextChars(n = 200) {
      const buf = terminal.buffer.active;
      const start = Math.max(0, buf.length - n);
      let chars = 0;
      for (let i = start; i < buf.length; i++) {
        const line = buf.getLine(i);
        if (line) chars += line.translateToString(true).trimEnd().length + 1; // +1 for newline
      }
      return chars;
    }

    return { terminal, fitAddon, websocket, containerId, getBufferLines: () => _bufferLines, getContextChars };
  }

  // -------------------------------------------------------------------------
  // Live settings update — apply new settings to all open terminals
  // -------------------------------------------------------------------------

  window.addEventListener('shellmate:settings-changed', (e) => {
    const detail = e.detail || {};
    const s      = detail.terminal   || {};
    const a      = detail.appearance || {};

    const schemeObj = typeof window.getColorScheme === 'function'
      ? window.getColorScheme(a.color_scheme)
      : null;

    // Each in its own try. A disposed instance throws on `terminal.options`,
    // and the unguarded loop stopped there — so one stale entry meant every
    // tab after it silently kept the old settings. forgetTerminal() should
    // prevent stale entries now; this is what keeps one from mattering.
    Object.entries(_instances).forEach(([sessionId, { terminal, fitAddon }]) => {
      try {
        if (schemeObj) {
          const theme = Object.assign({}, schemeObj.theme);
          if (a.foreground_override) theme.foreground = a.foreground_override;
          if (a.background_override) theme.background = a.background_override;
          if (s.cursor_colour)    theme.cursor = s.cursor_colour;
          if (s.selection_colour) theme.selectionBackground = s.selection_colour;
          terminal.options.theme = theme;
        }
        if (s.font_size)    terminal.options.fontSize    = s.font_size;
        if (s.font_family)  terminal.options.fontFamily   = s.font_family;
        if (s.line_height)  terminal.options.lineHeight   = s.line_height;
        if (s.cursor_style) terminal.options.cursorStyle  = s.cursor_style;
        terminal.options.cursorBlink  = s.cursor_blink !== false;
        terminal.options.copyOnSelect = !!s.copy_on_select;

        // Applied to open tabs as well as new ones. These are all readability
        // settings, and the tab somebody is squinting at while changing them
        // is the one they want to see change.
        terminal.options.letterSpacing  = s.letter_spacing   || 0;
        terminal.options.fontWeight     = s.font_weight      || 'normal';
        terminal.options.fontWeightBold = s.font_weight_bold || 'bold';
        terminal.options.tabStopWidth   = s.tab_stop_width   || 8;
        terminal.options.drawBoldTextInBrightColors = s.draw_bold_in_bright !== false;
        terminal.options.screenReaderMode = !!s.screen_reader_mode;
        if (s.cursor_width) terminal.options.cursorWidth = s.cursor_width;
        try { fitAddon.fit(); } catch (_) {}
      } catch (err) {
        console.info('Dropping a terminal that no longer accepts settings', err);
        forgetTerminal(sessionId);
      }
    });
  });

  // -------------------------------------------------------------------------
  // Expose to global scope
  // -------------------------------------------------------------------------
  /**
   * Forget a closed session's terminal.
   *
   * closeTab() tore down everything else — the socket, the xterm instance,
   * the container, the tab, the uptime clock, the layout entry — and there
   * was no way to reach this map, so every terminal ever opened stayed in it
   * along with its addons and its socket.
   *
   * The leak was the smaller half. `applySettingsToAll()` iterates this map,
   * so changing a setting after closing a tab called `terminal.options` on a
   * disposed instance, which throws — and the loop stopped there, leaving
   * every terminal after it in the map unchanged. One closed tab and settings
   * silently stopped applying to the tabs that were still open.
   */
  function forgetTerminal(sessionId) {
    const entry = _instances[sessionId];
    if (!entry) return false;
    if (entry.onWindowResize) {
      window.removeEventListener('resize', entry.onWindowResize);
    }
    delete _instances[sessionId];
    return true;
  }

  /**
   * Ask before a destructive command reaches the device.
   *
   * The device is named, and named first, because the mistake this exists to
   * catch is not "did I mean to type reload" — it is "which tab am I in".
   * A confirmation that only quotes the command answers the wrong question.
   *
   * Answering is not optional: an unanswered prompt leaves the command held
   * server-side forever, so cancelling is the default on every route out
   * including Escape and clicking away.
   */
  function askBeforeSending(sessionId, websocket, command, device) {
    const reply = (confirmed) => {
      if (websocket.readyState === WebSocket.OPEN) {
        websocket.send(JSON.stringify({ type: 'guardrail_answer', confirmed }));
      }
    };

    if (!window.shellmateDialog) {
      reply(window.confirm(`Send "${command}" to ${device}?`));
      return;
    }

    window.shellmateDialog.confirm({
      title: `Send this to ${device}?`,
      body:  `${command}\n\nThis is on the list of destructive commands for `
             + `this platform. Nothing has been sent yet.`,
      confirmLabel: 'Send it',
      cancelLabel:  'Cancel',
      danger: true,
    }).then(reply).catch(() => reply(false));
  }


  // -------------------------------------------------------------------------
  // Searching the buffer in front of you
  //
  // History search answers a different question — "what did I change on the
  // Glasgow core last Tuesday" — out of SQLite, from records written only
  // once the transcript parser sees the next prompt. It cannot reach a
  // `show run` still scrolling past, which is where somebody looks a hundred
  // times a day, and it finds nothing at all when history.record is off.
  //
  // One bar, reused by whichever terminal is active, because a search box per
  // tab is a box you have to find before you can use it.
  // -------------------------------------------------------------------------

  let _searchBar, _searchInput, _searchCount;

  function activeSearchAddon() {
    const tab = typeof window.getActiveTab === 'function' ? window.getActiveTab() : null;
    if (!tab) return null;
    const entry = _instances[tab.sessionId];
    return entry ? entry.searchAddon : null;
  }

  function initSearchBar() {
    _searchBar   = document.getElementById('term-search');
    _searchInput = document.getElementById('term-search-input');
    _searchCount = document.getElementById('term-search-count');
    if (!_searchBar) return;

    const decorations = {
      matchBackground:          '#4F46E5',
      matchBorder:              '#C3C0FF',
      matchOverviewRuler:       '#C3C0FF',
      activeMatchBackground:    '#C3C0FF',
      activeMatchBorder:        '#4F46E5',
      activeMatchColorOverviewRuler: '#C3C0FF',
    };

    const find = (forward) => {
      const addon = activeSearchAddon();
      const term  = _searchInput.value;
      if (!addon || !term) { _searchCount.textContent = ''; return; }
      const options = { decorations, incremental: false };
      const found = forward ? addon.findNext(term, options)
                            : addon.findPrevious(term, options);
      // xterm's addon reports found/not-found rather than a count, so say
      // that rather than inventing a number it has not given us.
      _searchCount.textContent = found ? '' : 'no matches';
    };

    _searchInput.addEventListener('input', () => find(true));
    _searchInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter')  { e.preventDefault(); find(!e.shiftKey); }
      if (e.key === 'Escape') { e.preventDefault(); closeSearch(); }
    });
    document.getElementById('term-search-next').addEventListener('click', () => find(true));
    document.getElementById('term-search-prev').addEventListener('click', () => find(false));
    document.getElementById('term-search-close').addEventListener('click', closeSearch);

    // Focus outside a terminal — on the tab bar, say — still gets Ctrl+F,
    // because "find in this terminal" is about the visible session rather
    // than about what happens to hold focus. When focus *is* in a terminal
    // the custom key handler above has already claimed it, so this never
    // double-fires.
    document.addEventListener('keydown', (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'f' && !e.shiftKey) {
        if (!activeSearchAddon()) return;   // no session: leave it to the browser
        if (_searchBar && !_searchBar.classList.contains('hidden')) return;
        e.preventDefault();
        openSearch();
      }
    });
  }

  function openSearch() {
    if (!_searchBar) return;
    _searchBar.classList.remove('hidden');
    _searchInput.select();
    _searchInput.focus();
  }

  function closeSearch() {
    if (!_searchBar) return;
    _searchBar.classList.add('hidden');
    _searchCount.textContent = '';
    const addon = activeSearchAddon();
    if (addon) { try { addon.clearDecorations(); } catch (_) {} }
    // Back to the device, which is where the next keystroke is meant to go.
    const tab = typeof window.getActiveTab === 'function' ? window.getActiveTab() : null;
    const entry = tab && _instances[tab.sessionId];
    if (entry) { try { entry.terminal.focus(); } catch (_) {} }
  }

  document.addEventListener('DOMContentLoaded', initSearchBar);

  // Session logging is a decision people make mid-change — "I want a record
  // of this" arrives ten minutes in, not at connect. Tell every open session
  // so it can start or stop now rather than at the next tab.
  window.addEventListener('shellmate:settings-changed', () => {
    Object.values(_instances).forEach(({ websocket }) => {
      if (websocket && websocket.readyState === WebSocket.OPEN) {
        websocket.send(JSON.stringify({ type: 'logging_changed' }));
      }
    });
  });

  window.initTerminal = initTerminal;
  window.forgetTerminal = forgetTerminal;

})();
