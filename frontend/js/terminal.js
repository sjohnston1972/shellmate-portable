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
  // { sessionId: { terminal, fitAddon, websocket, containerId, link } }
  const _instances = {};

  // Reattaching a dropped terminal socket (#481): the first retry after a
  // second, doubling to a ceiling, and a bound on how long the server can
  // stay unreachable before the tab is called disconnected — eight tries is
  // about two minutes. Not Stockton settings: the worst outcome of any value
  // here is a tab that says "disconnected" a minute early, and nothing about
  // the device changes with them.
  const RELINK_BASE_MS  = 1000;
  const RELINK_MAX_MS   = 30000;
  const RELINK_ATTEMPTS = 8;

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

  /**
   * Per-session colour scheme overrides, keyed by session id (#139).
   *
   * The point is being able to see at a glance which tab is production. One
   * scheme for every terminal is the one thing a scheme could not do.
   */
  const _schemeOverride = {};

  /**
   * Give one live terminal its own scheme.
   *
   * `terminal.options.theme` is assignable on a live instance —
   * applySettingsToAll already does exactly this to every terminal — so the
   * applying is trivial. The care is in not being undone: that function
   * overwrites every terminal's theme on every save, so without the skip
   * below, changing the font size would silently revert every per-tab colour.
   */
  function setSessionScheme(sessionId, schemeName) {
    if (schemeName) _schemeOverride[sessionId] = schemeName;
    else delete _schemeOverride[sessionId];

    const entry = _instances[sessionId];
    if (!entry) return;
    const theme = _themeFor(sessionId);
    if (theme) {
      try { entry.terminal.options.theme = theme; } catch (_) { /* disposed */ }
    }
  }

  function sessionScheme(sessionId) {
    return _schemeOverride[sessionId] || '';
  }

  /** The theme a session should use: its override, or the global scheme. */
  function _themeFor(sessionId) {
    const settings = window.shellmateSettings || {};
    const appearance = settings.appearance || {};
    const name = _schemeOverride[sessionId] || appearance.color_scheme;
    const scheme = typeof window.getColorScheme === 'function'
      ? window.getColorScheme(name) : null;
    if (!scheme) return null;

    const theme = Object.assign({}, scheme.theme);
    // A per-tab scheme is the whole point of the override, so the global
    // foreground/background overrides do not apply on top of it — they belong
    // to the scheme somebody chose globally.
    if (!_schemeOverride[sessionId]) {
      if (appearance.foreground_override) theme.foreground = appearance.foreground_override;
      if (appearance.background_override) theme.background = appearance.background_override;
    }
    const terminalSettings = settings.terminal || {};
    if (terminalSettings.cursor_colour)    theme.cursor = terminalSettings.cursor_colour;
    if (terminalSettings.selection_colour) theme.selectionBackground = terminalSettings.selection_colour;
    return theme;
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

  // One status-bar update per 250ms, not per output chunk (#318).
  // updateStatusBar walks 200 buffer lines to estimate the AI context, so
  // calling it for every WebSocket message put hundreds of full extractions
  // a second on the UI thread during a `show tech` dump. A quarter-second
  // of staleness on a line counter is invisible.
  let _statusTimer = 0;
  function _queueStatusUpdate() {
    if (_statusTimer) return;
    _statusTimer = setTimeout(() => {
      _statusTimer = 0;
      if (typeof window.updateStatusBar === 'function') window.updateStatusBar();
    }, 250);
  }

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

    // The addon reports "3 of 17" through an event rather than a return
    // value (#412). Only the active session's count is shown — a background
    // tab's decorations are not what the bar is describing.
    searchAddon.onDidChangeResults((results) => {
      const tab = typeof window.getActiveTab === 'function' ? window.getActiveTab() : null;
      if (!tab || tab.sessionId !== sessionId) return;
      _showSearchCount(results);
    });

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
      try { fitAddon.fit(); } catch (err) { _fitFailed(err); }
    });

    // ------------------------------------------------------------------
    // 4. The WebSocket to the backend
    // ------------------------------------------------------------------
    // `let`, not `const`, because the socket is replaced when it drops
    // (#481). Everything below reads the variable at call time rather than
    // capturing the first socket, so a reattached one is used by the next
    // keystroke, paste and resize without any of them knowing.
    const wsProto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl    = `${wsProto}//${window.location.host}/ws/terminal/${sessionId}`;
    let websocket  = null;

    // Where the reattach stands: attempts since the socket last opened, and
    // the timer for the next try. Kept on the instance so forgetTerminal()
    // can cancel a pending try when the tab closes mid-backoff.
    const link = { attempt: 0, timer: null, lost: false };

    // ------------------------------------------------------------------
    // 5. Wire WebSocket → terminal (incoming data from device)
    // ------------------------------------------------------------------
    function handleMessage(event) {
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
          _queueStatusUpdate();
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

        case 'on_connect':
          // The saved connection's own lines (#532): what is about to be
          // sent, and afterwards what was and what was not. Same rule as
          // the paging command — nothing reaches a device unannounced.
          window.dispatchEvent(new CustomEvent('shellmate:on-connect', {
            detail: { sessionId, ...msg }
          }));
          break;

        case 'paste_batch':
          // A pasted block being sent a line at a time (#523). The dialog
          // follows the progress; the toast is for the end — and above all
          // for an end that was not the end of the block.
          window.dispatchEvent(new CustomEvent('shellmate:paste-batch', {
            detail: { sessionId, ...msg }
          }));
          if (msg.state === 'done' || msg.state === 'refused') {
            _reportPaste(sessionId, msg);
          }
          break;

        case 'alias_expanded':
          window.dispatchEvent(new CustomEvent('shellmate:alias-expanded', {
            detail: { sessionId, typed: msg.typed, sent: msg.sent }
          }));
          break;

        case 'keep_alive_active':
          // Said once per session, not every nudge. ShellMate's rule is that
          // nothing is sent silently; forty announcements an hour would obey
          // the letter of that and destroy its purpose.
          if (window.shellmateAlerts) {
            window.shellmateAlerts.notify({
              icon: 'refresh',
              title: 'Keeping this session alive',
              body: `Quiet for ${msg.seconds}s, so a space and a backspace `
                    + 'were sent at the prompt — nothing typed. This is the '
                    + 'only way to reset what the device counts as idle.',
              sessionId,
            });
          }
          break;

        case 'guardrail_prompt':
          // A destructive command is being held. Nothing has reached the
          // device — the answer decides whether anything does.
          askBeforeSending(sessionId, websocket, msg.command, msg.device);
          break;

        case 'watch_hit':
          // One of the user's colour rules was marked to alert, and the
          // backend saw it match this session's output (#521). It is raised
          // here rather than from the highlighter because the highlighter
          // only runs on data reaching a visible terminal, and the tab worth
          // interrupting somebody about is the one they are not watching.
          if (window.shellmateAlerts && window.shellmateAlerts.watchHit) {
            window.shellmateAlerts.watchHit({
              sessionId,
              pattern:  msg.pattern,
              line:     msg.line,
              severity: msg.severity,
            });
          }
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
    }

    // ------------------------------------------------------------------
    // 5a. Reattaching a dropped socket (#481)
    //
    // The bridge ending means the *browser* went away, not the device. The
    // backend keeps the session and banks whatever the device says while
    // nobody is attached, so the right answer to a dropped socket — laptop
    // sleep, the hidden desktop window suspended, a proxy idle timeout — is
    // to open it again. Before this, every close was read as the device
    // hanging up: the tab went red, and with auto-reconnect on, a live
    // session was torn down and replaced with a fresh login.
    //
    // The tab is marked disconnected only when the server says the session
    // is gone or the device has closed, when the handshake is refused, or
    // after RELINK_ATTEMPTS failures to reach the server at all. That last
    // case is the one where the device may still be up but we cannot tell,
    // and after a couple of minutes "disconnected" is the honest label.
    // ------------------------------------------------------------------

    function handleClose(event) {
      const entry = _instances[sessionId];
      // closeTab() forgets the instance before it closes the socket, so a
      // close with no entry — or for a socket this instance has already
      // replaced — is deliberate and wants nothing done about it.
      if (!entry) return;
      if (event && event.target && event.target !== websocket) return;

      // 1008: the server refused the handshake (an origin or auth check).
      // Another attempt would be refused the same way.
      if (event && event.code === 1008) { _linkLost(); return; }

      _afterDrop();
    }

    function handleError(err) {
      console.error(`WebSocket error for session ${sessionId}:`, err);
    }

    /**
     * Ask the server whether the session is still there before deciding
     * what the close meant.
     *
     * Alive and connected: reattach. Absent, or connected=false: the device
     * side ended, which is the genuine disconnect the tab should show — and
     * the path tabs.js's own auto-reconnect is for. Server unreachable:
     * back off and try again, up to a bound.
     */
    async function _afterDrop() {
      let sessions = null;
      try {
        const res = await fetch('/api/sessions', { cache: 'no-store' });
        if (res.ok) sessions = await res.json();
      } catch (_) { /* the server could not be asked — treat as a link problem */ }

      // Closed while the question was in flight.
      if (!_instances[sessionId]) return;

      if (Array.isArray(sessions)) {
        const mine = sessions.find(s => s.session_id === sessionId);
        if (!mine || !mine.is_connected) { _linkLost(); return; }
      }

      link.attempt += 1;
      if (link.attempt > RELINK_ATTEMPTS) { _linkLost(); return; }

      const wait = Math.min(RELINK_BASE_MS * Math.pow(2, link.attempt - 1), RELINK_MAX_MS);
      link.lost = true;
      _linkState('reattaching', link.attempt);
      link.timer = setTimeout(() => {
        link.timer = null;
        if (_instances[sessionId]) connect();
      }, wait);
    }

    /** The session really is down: hand over to the tab's disconnected state. */
    function _linkLost() {
      link.attempt = 0;
      link.lost = false;
      _linkState('lost', 0);
      if (typeof window.updateTabStatus === 'function') {
        window.updateTabStatus(sessionId, false);
      }
    }

    /** Tell the tab strip what the link is doing, so the label can say. */
    function _linkState(state, attempt) {
      window.dispatchEvent(new CustomEvent('shellmate:terminal-link', {
        detail: { sessionId, state, attempt, attempts: RELINK_ATTEMPTS },
      }));
    }

    /** Open (or reopen) the socket and bind every handler to it. */
    function connect() {
      const ws = new WebSocket(wsUrl);
      websocket = ws;
      const entry = _instances[sessionId];
      if (entry) entry.websocket = ws;
      ws.addEventListener('message', handleMessage);
      ws.addEventListener('close', handleClose);
      ws.addEventListener('error', handleError);
      ws.addEventListener('open', handleOpen);
    }

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
      }).catch(err => _clipboardFailed('copy', err));
      return true;
    }

    /**
     * Send an approved paste, in whichever way was chosen (#523).
     *
     * "block" is the original path and the default: one stream, chunked by
     * bytes if Stockton asks for it, which is the right shape for a serial
     * input buffer that drops characters when a paste outruns it.
     *
     * The other two are lines, and they are paced by the *server* — see
     * PasteBatch in `backend/pipeline.py`. The pacing that a sixty-line ACL
     * needs is not a gap in milliseconds but the device being back at its
     * prompt, and that is not visible from in here.
     */
    function _sendPaste(text, choice) {
      if (websocket.readyState !== WebSocket.OPEN) return;
      const mode = (choice && choice.mode) || 'block';

      if (mode !== 'block') {
        // One trailing newline dropped, not all of them: a blank line in the
        // middle of a block is a bare Return the device may well need, but
        // the newline that ended the last line is not a line of its own.
        const lines = text.replace(/\r\n?/g, '\n').replace(/\n$/, '').split('\n');
        websocket.send(JSON.stringify({
          type:      'paste_lines',
          lines:     lines,
          mode:      mode === 'lines' ? 'lines' : 'prompt',
          delay_ms:  choice.delayMs,
          timeout_s: choice.timeoutS,
        }));
        return;
      }

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
    }

    function _pasteFromClipboard() {
      navigator.clipboard.readText().then(text => {
        if (!text) return;
        // Below the threshold a paste goes straight through. Every
        // multi-line paste used to ask, which somebody pasting short blocks
        // all day learns to click through.
        const lines = text.split('\n').length;
        if (lines < A('terminal.paste_confirm_lines', 1) + 1) {
          _sendPaste(text, { mode: 'block' });
          return;
        }

        // What the dialog opens on. The pace is a decision about the device
        // in front of you, so the setting is a starting point rather than
        // the answer — it can be changed for this paste alone.
        const opts = {
          sessionId: sessionId,
          mode:      A('terminal.paste_mode', 'block'),
          delayMs:   A('terminal.paste_line_delay', 200),
          timeoutS:  A('terminal.paste_prompt_timeout', 10),
        };
        if (window._showPasteModal) {
          window._showPasteModal(text, opts, _sendPaste);
        } else {
          _sendPaste(text, { mode: 'block' });
        }
      }).catch(err => _clipboardFailed('paste', err));
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

      // Application shortcuts (#413). xterm would otherwise consume these
      // and stop them reaching the document-level handler in tabs.js;
      // returning false hands them back without sending anything to the
      // device.
      if (typeof window.isAppShortcut === 'function' && window.isAppShortcut(e)) {
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
          .catch(err => _clipboardFailed('copy', err));
      }, 0);
    });

    // Right-click. Paste, the way PuTTY does, while the setting is on — and
    // the terminal's own menu (#411) on Shift+right-click, or on a plain
    // right-click when paste-on-right-click is off. Before this the "off"
    // case surrendered the click to the browser's menu, which offers
    // nothing useful over a terminal.
    container.addEventListener('contextmenu', (e) => {
      e.preventDefault();
      const settings = window.shellmateSettings || {};
      const pasteOnRight = !(settings.terminal && settings.terminal.right_click_paste === false);
      if (pasteOnRight && !e.shiftKey) { _pasteFromClipboard(); return; }
      _terminalMenu(e);
    });

    /** The terminal's context menu: what the mouse is over, and nothing else. */
    function _terminalMenu(e) {
      if (!window.shellmateMenu) return;
      const hasSelection = terminal.hasSelection();
      window.shellmateMenu.open(e, [
        { icon: 'content_copy', label: 'Copy', disabled: !hasSelection,
          title: hasSelection ? '' : 'Nothing is selected.',
          onClick: () => _copySelection() },
        { icon: 'content_paste', label: 'Paste', onClick: () => _pasteFromClipboard() },
        { icon: 'select_all', label: 'Select all', onClick: () => terminal.selectAll() },
        'sep',
        { icon: 'search', label: 'Find…', value: 'Ctrl+F', onClick: () => openSearch() },
        { icon: 'content_copy', label: 'Copy visible screen',
          onClick: () => copyOutput(sessionId, { visibleOnly: true }) },
        { icon: 'content_copy', label: 'Copy all scrollback',
          onClick: () => copyOutput(sessionId, {}) },
        'sep',
        { icon: 'backspace', label: 'Clear screen', onClick: () => terminal.clear() },
      ]);
    }

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
        } catch (err) { _fitFailed(err); }
      }
    };
    window.addEventListener('resize', onWindowResize);

    // Send the size once the socket is open — the first time and after every
    // reattach, since the server sizes the pty per bridge.
    function handleOpen() {
      try {
        fitAddon.fit();
        websocket.send(JSON.stringify({
          type: 'resize',
          cols: terminal.cols,
          rows: terminal.rows,
        }));
      } catch (_) {}
      // Back. The count restarts so a later drop gets its full allowance,
      // and the tab stops saying "reattaching".
      link.attempt = 0;
      if (link.lost) {
        link.lost = false;
        _linkState('attached', 0);
      }
    }

    // ------------------------------------------------------------------
    // 9. Register instance so settings changes can be applied live, then
    //    open the socket — registered first, because connect() records the
    //    socket on the entry and handleClose() reads it back from there.
    // ------------------------------------------------------------------
    _instances[sessionId] = { terminal, fitAddon, searchAddon, websocket,
                              containerId, onWindowResize, link };
    connect();

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

    // `websocket` is a getter, not a value: the socket is replaced on a
    // reattach (#481), and a caller holding the first one would be sending
    // keystrokes into a closed socket for the rest of the session.
    return {
      terminal, fitAddon, containerId,
      get websocket() { return websocket; },
      getBufferLines: () => _bufferLines,
      getContextChars,
    };
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
          // A session carrying its own scheme keeps it. Without this, saving
          // any setting at all — a font size — would silently revert every
          // per-tab colour, because this loop rewrites the theme of every
          // terminal on every save.
          const own = _themeFor(sessionId);
          if (_schemeOverride[sessionId] && own) {
            terminal.options.theme = own;
          } else {
            const theme = Object.assign({}, schemeObj.theme);
            if (a.foreground_override) theme.foreground = a.foreground_override;
            if (a.background_override) theme.background = a.background_override;
            if (s.cursor_colour)    theme.cursor = s.cursor_colour;
            if (s.selection_colour) theme.selectionBackground = s.selection_colour;
            terminal.options.theme = theme;
          }
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
        try { fitAddon.fit(); } catch (err) { _fitFailed(err); }
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
    // A reattach counting down for a tab that is closing must not fire and
    // open a socket to a session that is being deleted.
    if (entry.link && entry.link.timer) {
      clearTimeout(entry.link.timer);
      entry.link.timer = null;
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
        // The command is named so the answer acts on this hold and no other
        // — a pasted batch can put two prompts up, and an unnamed answer
        // could confirm the wrong one.
        websocket.send(JSON.stringify({ type: 'guardrail_answer', confirmed, command }));
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

  // -------------------------------------------------------------------------
  // Saying so when something fails (#426)
  // -------------------------------------------------------------------------

  let _lastClipboardWarning = 0;

  /**
   * A clipboard call that failed used to be swallowed, so a paste that never
   * left the browser — permission refused in the desktop window, a page not
   * focused, an insecure context — looked exactly like one the device
   * ignored. Rate-limited: copy-on-select would otherwise raise one per
   * drag while the clipboard is blocked.
   */
  function _clipboardFailed(what, err) {
    console.warn(`Clipboard ${what} failed`, err);
    const now = Date.now();
    if (now - _lastClipboardWarning < 30000) return;
    _lastClipboardWarning = now;
    const why = err && err.name === 'NotAllowedError'
      ? 'The browser refused clipboard access. Click in the terminal first, or check the site permission.'
      : (err && err.message) || 'The browser did not say why.';
    if (window.shellmateAlerts) {
      window.shellmateAlerts.notify({
        severity: 'warning', icon: 'error',
        title: what === 'copy' ? 'Nothing was copied' : 'Nothing was pasted',
        body: why,
      });
    }
  }

  /** How a batch that stopped early reads, by the server's reason code. */
  const PASTE_STOPPED = {
    'no-prompt':          'the device never came back to a prompt',
    'you started typing': 'you started typing',
    'you stopped it':     'you stopped it',
    'the session dropped': 'the session dropped',
    'the window closed':  'the window closed',
    'another paste started': 'another paste was started',
  };

  /**
   * Say how a line-paced paste ended (#523).
   *
   * The half that did not go out is the point. A block that stopped at line
   * 12 of 60 reads as success unless somebody says otherwise — and the next
   * thing anybody does is go and look at line 12, so it is named rather than
   * left to be worked out from a count.
   */
  function _reportPaste(sessionId, info) {
    if (!window.shellmateAlerts || !window.shellmateAlerts.notify) return;

    if (info.state === 'refused') {
      window.shellmateAlerts.notify({
        severity: 'warning', icon: 'error', sessionId,
        title: 'That paste was too long',
        body: `${info.total} lines, and ShellMate sends at most ${info.limit} `
              + 'this way. A configuration that size belongs in a file, '
              + 'applied with Apply configuration.',
      });
      return;
    }

    const remaining = Number(info.remaining) || 0;
    if (!info.reason && !remaining) {
      window.shellmateAlerts.notify({
        icon: 'content_paste', sessionId,
        title: `Pasted ${info.sent} lines`,
        body: 'Every line was sent and answered.',
      });
      return;
    }

    const where = info.stalled_at
      ? `line ${info.stalled_at} sent, no prompt seen`
      : `stopped after ${info.sent} of ${info.total} lines`;
    window.shellmateAlerts.notify({
      severity: 'warning', icon: 'content_paste', sessionId,
      title: `Paste stopped: ${where}`,
      body: `${PASTE_STOPPED[info.reason] || info.reason || 'it stopped'} — `
            + `${remaining} line${remaining === 1 ? '' : 's'} not sent. `
            + 'Nothing was queued: the rest is still on your clipboard.',
    });
  }

  /** A fit that throws is a layout bug worth a line in the console. */
  function _fitFailed(err) {
    console.warn('Terminal fit failed', err);
  }

  // -------------------------------------------------------------------------
  // Copying output (#414)
  // -------------------------------------------------------------------------

  /**
   * Copy lines from a session's buffer to the clipboard.
   *
   * @param {string} sessionId
   * @param {object} opts
   * @param {boolean} [opts.visibleOnly]  Just the rows on screen.
   * @param {number}  [opts.lastLines]    Only the most recent N lines.
   */
  function copyOutput(sessionId, opts) {
    const entry = _instances[sessionId];
    if (!entry) return false;
    const term = entry.terminal;
    const buf  = term.buffer.active;
    let first = 0;
    let last  = buf.length;
    if (opts && opts.visibleOnly) {
      first = buf.viewportY;
      last  = Math.min(buf.length, buf.viewportY + term.rows);
    } else if (opts && opts.lastLines > 0) {
      first = Math.max(0, buf.length - opts.lastLines);
    }
    const lines = [];
    for (let i = first; i < last; i++) {
      const line = buf.getLine(i);
      if (line) lines.push(line.translateToString(true));
    }
    // Trailing blank rows are the unused part of the screen, not output.
    while (lines.length && !lines[lines.length - 1].trim()) lines.pop();
    const text = lines.join('\n');
    if (!text) {
      if (window.shellmateAlerts) {
        window.shellmateAlerts.notify({ severity: 'info', icon: 'content_copy',
          title: 'Nothing to copy', body: 'This terminal has no output yet.' });
      }
      return false;
    }
    navigator.clipboard.writeText(text)
      .then(() => { window._showCopyToast && window._showCopyToast(text); })
      .catch(err => _clipboardFailed('copy', err));
    return true;
  }

  /** The status-bar button: the last N lines of the active terminal. */
  function copyRecentOutput() {
    const tab = typeof window.getActiveTab === 'function' ? window.getActiveTab() : null;
    if (!tab) return;
    const n = Number(A('terminal.copy_output_lines', 200)) || 200;
    copyOutput(tab.sessionId, { lastLines: n });
  }

  document.addEventListener('DOMContentLoaded', () => {
    const button = document.getElementById('status-copy-output');
    if (button) button.addEventListener('click', copyRecentOutput);
  });

  window.copyTerminalOutput = copyOutput;

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
      // The count arrives through onDidChangeResults; this is only the
      // fallback for an addon that never fires it.
      if (!found) _searchCount.textContent = 'no matches';
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

  /**
   * "3 of 17", "no matches", or "over 1000" — the addon stops counting past
   * a thousand and reports -1, which must not be shown as "-1 of -1".
   */
  function _showSearchCount(results) {
    if (!_searchCount) return;
    const { resultIndex, resultCount } = results || {};
    if (!_searchInput || !_searchInput.value) { _searchCount.textContent = ''; return; }
    if (resultCount === 0) _searchCount.textContent = 'no matches';
    else if (resultCount < 0) _searchCount.textContent = 'over 1000 matches';
    else _searchCount.textContent = `${resultIndex + 1} of ${resultCount}`;
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
        // Colour rules that alert are the same kind of decision (#521): a
        // watch is usually written in the middle of the change it is meant
        // to watch, so it has to arm now rather than at the next tab.
        websocket.send(JSON.stringify({ type: 'watch_changed' }));
      }
    });
  });

  window.setSessionScheme = setSessionScheme;
  window.sessionScheme    = sessionScheme;
  window.initTerminal = initTerminal;
  window.forgetTerminal = forgetTerminal;

  /**
   * Stop tracking a session's pending action (#265).
   *
   * Over the session's own terminal socket, because that is where the
   * tracker lives; the server clears it and answers with the empty pending.
   */
  /**
   * Type a command into a session on the user's behalf (#292).
   *
   * Used by the last-chance reload warning for its cancel button. Sent as
   * ordinary input with a carriage return, so the device sees exactly what
   * a person typing it would send, and the terminal shows it happening —
   * a command that goes in silently is the thing this application refuses
   * to do everywhere else.
   *
   * Returns whether it went, so the caller can say if it did not.
   */
  window.sendCommandToSession = (sessionId, command) => {
    const instance = _instances[sessionId];
    if (!instance || !instance.websocket
        || instance.websocket.readyState !== WebSocket.OPEN) return false;
    instance.websocket.send(JSON.stringify({
      type: 'input', data: String(command).trim() + '\r',
    }));
    return true;
  };

  /**
   * Type a command into a session and stop there (#522).
   *
   * The recall half of sendCommandToSession: the text arrives at the prompt
   * with no carriage return, so nothing runs until the person at the keyboard
   * presses Return themselves. That is the right default for a command
   * recalled from last month — it is a starting point to be edited far more
   * often than it is a thing to run verbatim.
   *
   * Sent as ordinary input, so it goes through the same pipeline every
   * keystroke does: the alias expansion and the dangerous-command guardrail
   * apply when the Return eventually comes, exactly as if it had been typed.
   *
   * Returns whether it went, so the caller can say if it did not.
   */
  window.insertIntoSession = (sessionId, text) => {
    const instance = _instances[sessionId];
    if (!instance || !instance.websocket
        || instance.websocket.readyState !== WebSocket.OPEN) return false;
    // Newlines stripped rather than passed on: a multi-line paste is what the
    // paste path is for, with its pacing and its confirmation. One recalled
    // command is one line.
    const line = String(text).replace(/[\r\n]+/g, ' ').trim();
    if (!line) return false;
    instance.websocket.send(JSON.stringify({ type: 'input', data: line }));
    return true;
  };

  /**
   * Stop a line-paced paste that is part-way through (#523).
   *
   * The other way to stop one is to type, which is how a live capture ends
   * too — but typing into a session mid-paste is exactly what somebody
   * watching a block go wrong does not want to do.
   */
  window.stopPasteInSession = (sessionId) => {
    const instance = _instances[sessionId];
    if (!instance || !instance.websocket
        || instance.websocket.readyState !== WebSocket.OPEN) return false;
    instance.websocket.send(JSON.stringify({ type: 'paste_stop' }));
    return true;
  };

  window.dismissPendingAction = (sessionId) => {
    const instance = _instances[sessionId];
    if (instance && instance.websocket
        && instance.websocket.readyState === WebSocket.OPEN) {
      instance.websocket.send(JSON.stringify({ type: 'dismiss_pending' }));
    }
  };

})();
