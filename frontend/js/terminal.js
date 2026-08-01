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
      // Stockton (#57). Read at construction, which is why the renderer is
      // marked as needing a restart while the rest apply to the next tab.
      minimumContrastRatio: A('terminal.min_contrast', 1),
      wordSeparator:        A('terminal.word_separators', " ()[]{}',\"`"),
      scrollSensitivity:    A('terminal.scroll_sensitivity', 1),
      rendererType:         A('terminal.renderer', 'canvas'),
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

    terminal.loadAddon(fitAddon);
    terminal.loadAddon(webLinksAddon);

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
    window.addEventListener('resize', () => {
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
    });

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
    _instances[sessionId] = { terminal, fitAddon, websocket, containerId };

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

    Object.values(_instances).forEach(({ terminal, fitAddon }) => {
      if (schemeObj) {
        const theme = Object.assign({}, schemeObj.theme);
        if (a.foreground_override) theme.foreground = a.foreground_override;
        if (a.background_override) theme.background = a.background_override;
        terminal.options.theme = theme;
      }
      if (s.font_size)    terminal.options.fontSize    = s.font_size;
      if (s.font_family)  terminal.options.fontFamily   = s.font_family;
      if (s.line_height)  terminal.options.lineHeight   = s.line_height;
      if (s.cursor_style) terminal.options.cursorStyle  = s.cursor_style;
      terminal.options.cursorBlink  = s.cursor_blink !== false;
      terminal.options.copyOnSelect = !!s.copy_on_select;
      try { fitAddon.fit(); } catch (_) {}
    });
  });

  // -------------------------------------------------------------------------
  // Expose to global scope
  // -------------------------------------------------------------------------
  window.initTerminal = initTerminal;

})();
