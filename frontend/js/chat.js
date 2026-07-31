/**
 * chat.js — AI chat panel for ShellMate.
 *
 * Manages the split-screen chat pane: message rendering, WebSocket to
 * /ws/chat, backend selector, streaming token display, and command
 * suggestion blocks.
 *
 * Context commands:
 *   /context all     — include all open session buffers
 *   /context 1-9     — include a specific tab's buffer
 */
(function () {
  'use strict';

  // -----------------------------------------------------------------------
  // State
  // -----------------------------------------------------------------------
  let chatWs          = null;
  let isStreaming     = false;
  let currentBackend  = 'claude';       // provider key, e.g. "claude", "ollama"
  let currentModel    = 'claude-sonnet-4-6'; // specific model string
  let contextMode     = 'active'; // 'active' | 'all' | '1'..'9'
  let streamingBubble = null;     // the <div> currently being filled
  let _outputWatcher  = null;     // active command output watcher

  const QUICK_BUTTONS_KEY  = 'mate:quick-buttons';
  const DEFAULT_QUICK_BTNS = [
    'Thoughts on this?',
    'What\'s wrong here?',
    'Any issues?',
    'Summarize',
    'Next steps?',
  ];

  // -----------------------------------------------------------------------
  // DOM refs
  // -----------------------------------------------------------------------
  let messagesEl, inputEl, sendBtn, backendSelect, contextIndicator;

  document.addEventListener('DOMContentLoaded', () => {
    messagesEl       = document.getElementById('chat-messages');
    inputEl          = document.getElementById('chat-input');
    sendBtn          = document.getElementById('chat-send');
    backendSelect    = document.getElementById('ai-backend-select');
    contextIndicator = document.getElementById('chat-context-indicator');

    // Parse "backend:model" value from the dropdown
    function _parseSelection(val) {
      const idx = val.indexOf(':');
      if (idx === -1) return { backend: val, model: val };
      return { backend: val.slice(0, idx), model: val.slice(idx + 1) };
    }

    function _applySelection(val) {
      const { backend, model } = _parseSelection(val);
      currentBackend = backend;
      currentModel   = model;
    }

    _applySelection(backendSelect.value || 'claude:claude-sonnet-4-6');

    // Dynamically populate local Ollama models
    fetch('/api/ollama/models').then(r => r.json()).then(models => {
      const group = document.getElementById('local-models-group');
      if (!group) return;
      group.innerHTML = '';
      if (!models.length) {
        const opt = document.createElement('option');
        opt.value = '_none'; opt.disabled = true; opt.textContent = 'None found';
        group.appendChild(opt);
        return;
      }
      models.forEach(m => {
        const opt = document.createElement('option');
        opt.value = `ollama:${m.name}`;
        opt.textContent = `${m.name}${m.size ? '  (' + m.size + ')' : ''}`;
        group.appendChild(opt);
      });
    }).catch(() => {
      const group = document.getElementById('local-models-group');
      if (group) { group.innerHTML = '<option value="_err" disabled>Ollama unavailable</option>'; }
    });

    // Wire up events
    sendBtn.addEventListener('click', sendMessage);
    inputEl.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
    });

    // Auto-resize textarea as user types
    inputEl.addEventListener('input', () => {
      inputEl.style.height = 'auto';
      inputEl.style.height = Math.min(inputEl.scrollHeight, 120) + 'px';
    });

    backendSelect.addEventListener('change', () => {
      _applySelection(backendSelect.value);
      updateContextIndicator();
    });

    document.getElementById('chat-clear').addEventListener('click', clearChat);
    document.getElementById('quick-btn-add').addEventListener('click', addQuickButton);

    // Render quick buttons from localStorage
    renderQuickButtons();

    // Set up draggable divider
    initDivider();

    // Pop-out / dock-in chat window
    initPopout();

    // Connect WebSocket
    connectChatWs();

    // Update context indicator when tab switches
    window.addEventListener('mate:tab-switched', (e) => updateContextIndicator(e.detail));
  });

  // -----------------------------------------------------------------------
  // WebSocket
  // -----------------------------------------------------------------------

  function connectChatWs() {
    const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const url = `${proto}//${window.location.host}/ws/chat`;
    chatWs = new WebSocket(url);

    chatWs.addEventListener('message', handleWsMessage);
    chatWs.addEventListener('close', () => {
      // Reconnect after a delay
      setTimeout(connectChatWs, 2000);
    });
    chatWs.addEventListener('error', () => {});
  }

  function handleWsMessage(event) {
    let msg;
    try { msg = JSON.parse(event.data); } catch (_) { return; }

    if (msg.type === 'chunk') {
      appendChunk(msg.data);
    } else if (msg.type === 'done') {
      finishStreaming();
    } else if (msg.type === 'error') {
      finishStreaming();
      appendErrorBubble(msg.message || 'Unknown error');
    }
  }

  // -----------------------------------------------------------------------
  // Sending messages
  // -----------------------------------------------------------------------

  function sendMessage() {
    if (isStreaming) return;
    const text = inputEl.value.trim();
    if (!text) return;

    // Jira shortcut — "send to jira" / "/jira" opens the conclude-session modal
    if (/^\/jira\b|send\s+to\s+jira|log\s+to\s+jira|create\s+jira/i.test(text)) {
      inputEl.value = '';
      if (typeof window.openJiraModal === 'function') window.openJiraModal();
      return;
    }

    // Parse context commands
    let message = text;
    let mode = contextMode;

    const ctxMatch = text.match(/^\/context\s+(all|\d+)\s*/i);
    if (ctxMatch) {
      mode = ctxMatch[1].toLowerCase();
      message = text.slice(ctxMatch[0].length).trim();
      if (!message) {
        // No message body — just set the context mode for future messages
        inputEl.value = '';
        contextMode = mode;
        updateContextIndicator();
        return;
      }
    }

    // Get active session id from tabs.js
    const activeTab = typeof window.getActiveTab === 'function' ? window.getActiveTab() : null;
    const sessionId = activeTab ? activeTab.sessionId : null;

    // Record in Jira chat history
    if (typeof window.addJiraChatMessage === 'function') window.addJiraChatMessage('user', text);

    // Render user bubble
    appendUserBubble(text);
    inputEl.value = '';
    inputEl.style.height = 'auto';

    // Start streaming AI bubble
    startStreamingBubble();
    isStreaming = true;
    sendBtn.disabled = true;

    if (chatWs && chatWs.readyState === WebSocket.OPEN) {
      const openIds = typeof window.getOpenSessionIds === 'function' ? window.getOpenSessionIds() : [];
      const aiMode = typeof window.getShellmateMode === 'function' ? window.getShellmateMode() : 'tshoot';

      // An explicit choice in the tab picker wins over the /context command,
      // which stays supported for anyone already using it.
      const picked = typeof window.getChatContextSelection === 'function'
        ? window.getChatContextSelection() : null;

      chatWs.send(JSON.stringify({
        message,
        session_id:        sessionId,
        open_session_ids:  picked || openIds,
        backend:           currentBackend,
        model:             currentModel,
        context_mode:      picked ? 'selected' : mode,
        mode:              aiMode,
      }));
    } else {
      finishStreaming();
      appendErrorBubble('Not connected to server. Reconnecting\u2026');
    }
  }

  // -----------------------------------------------------------------------
  // Message rendering
  // -----------------------------------------------------------------------

  function appendUserBubble(text) {
    const bubble = document.createElement('div');
    bubble.className = 'chat-bubble chat-bubble-user';
    bubble.textContent = text;
    messagesEl.appendChild(bubble);
    scrollToBottom(true);
  }

  function startStreamingBubble(auto = false) {
    const bubble = document.createElement('div');
    bubble.className = 'chat-bubble chat-bubble-ai streaming';
    const badge = auto ? '<span class="chat-auto-badge">auto</span>' : '';
    bubble.innerHTML = `${badge}<span class="chat-thinking"><span></span><span></span><span></span></span>`;
    messagesEl.appendChild(bubble);
    streamingBubble = bubble;
    scrollToBottom(true);
  }

  function appendChunk(text) {
    if (!streamingBubble) return;
    // Remove thinking indicator on first chunk
    const thinking = streamingBubble.querySelector('.chat-thinking');
    if (thinking) {
      thinking.remove();
      streamingBubble.dataset.raw = '';
      // Create a lightweight streaming text element — no command block parsing mid-stream
      const streamEl = document.createElement('div');
      streamEl.className = 'chat-text chat-stream-live';
      streamingBubble.appendChild(streamEl);
    }
    streamingBubble.dataset.raw = (streamingBubble.dataset.raw || '') + text;
    // Just update the text in-place — no full DOM rebuild on every chunk
    const streamEl = streamingBubble.querySelector('.chat-stream-live');
    if (streamEl) streamEl.innerHTML = formatText(streamingBubble.dataset.raw);
    scrollToBottom();
  }

  function finishStreaming() {
    if (streamingBubble) {
      streamingBubble.classList.remove('streaming');
      if (streamingBubble.dataset.raw) {
        // Record AI response in Jira history before rendering strips it
        if (typeof window.addJiraChatMessage === 'function') {
          window.addJiraChatMessage('ai', streamingBubble.dataset.raw);
        }
        renderBubbleContent(streamingBubble);
        wireCommandBlocks(streamingBubble);
      }
      streamingBubble = null;
    }
    isStreaming = false;
    sendBtn.disabled = false;
    inputEl.focus();
    scrollToBottom();
    updateContextIndicator();
  }

  function sendSilent(message, sessionId) {
    if (isStreaming) return;
    if (!chatWs || chatWs.readyState !== WebSocket.OPEN) return;

    const activeTab = typeof window.getActiveTab === 'function' ? window.getActiveTab() : null;
    const sid = sessionId || (activeTab ? activeTab.sessionId : null);

    // No user bubble — just start the AI bubble with a subtle "auto" badge
    startStreamingBubble(true);
    isStreaming = true;
    sendBtn.disabled = true;

    const aiMode = typeof window.getShellmateMode === 'function' ? window.getShellmateMode() : 'tshoot';
    chatWs.send(JSON.stringify({
      message,
      session_id:   sid,
      backend:      currentBackend,
      model:        currentModel,
      context_mode: contextMode,
      mode:         aiMode,
    }));
  }

  function _flashTab(tab) {
    if (!tab || !tab.tabEl) return;
    tab.tabEl.classList.remove('cmd-flash');
    // Force reflow to restart animation if already flashing
    void tab.tabEl.offsetWidth;
    tab.tabEl.classList.add('cmd-flash');
    tab.tabEl.addEventListener('animationend', () => {
      tab.tabEl.classList.remove('cmd-flash');
    }, { once: true });
  }

  function appendErrorBubble(msg) {
    const bubble = document.createElement('div');
    bubble.className = 'chat-bubble chat-bubble-error';
    bubble.textContent = '\u26a0 ' + msg;
    messagesEl.appendChild(bubble);
    scrollToBottom(true);
  }

  // -----------------------------------------------------------------------
  // Markdown-lite + command block rendering
  // -----------------------------------------------------------------------

  function renderBubbleContent(bubble) {
    let raw = bubble.dataset.raw || '';

    // Defensive normalisation — handle malformed tags the AI produces:
    //   "### [SUGGEST_CMD]cmd[/SUGGEST_CMD]"  → "[SUGGEST_CMD]cmd[/SUGGEST_CMD]"  (heading prefix)
    //   "### SUGGEST_CMD]cmd[/SUGGEST_CMD]"   → "[SUGGEST_CMD]cmd[/SUGGEST_CMD]"  (heading + missing [)
    //   "SUGGEST_CMD]cmd[/SUGGEST_CMD]"        → "[SUGGEST_CMD]cmd[/SUGGEST_CMD]"  (missing opening [)
    //   "[SUGGEST_CMD]cmd[/[SUGGEST_CMD]"      → "[SUGGEST_CMD]cmd[/SUGGEST_CMD]"  (extra [ in closing tag)
    raw = raw.replace(/^#{1,6}\s*\[?(SUGGEST_CMD|ADD_CMD)\]/gm, '[$1]');  // strip heading prefix
    raw = raw.replace(/(?<!\[)(SUGGEST_CMD|ADD_CMD)\]/g, '[$1]');          // fix missing opening [
    raw = raw.replace(/\[\/\[+(SUGGEST_CMD|ADD_CMD)\]/g, '[/$1]');         // fix extra [ in closing tag

    // Split on [SUGGEST_CMD]...[/SUGGEST_CMD] or [SUGGEST_CMD:N]...[/SUGGEST_CMD] blocks
    const parts = raw.split(/(\[(?:SUGGEST_CMD|ADD_CMD)(?::\d+)?\][\s\S]*?\[\/(?:SUGGEST_CMD|ADD_CMD)\])/g);
    bubble.innerHTML = '';

    parts.forEach(part => {
      // Group 1 = optional tab number, group 2 = command text
      const cmdMatch = part.match(/^\[(?:SUGGEST_CMD|ADD_CMD)(?::(\d+))?\]([\s\S]*?)\[\/(?:SUGGEST_CMD|ADD_CMD)\]$/);
      if (cmdMatch) {
        const tabNum = cmdMatch[1] ? parseInt(cmdMatch[1], 10) : null;
        bubble.appendChild(buildCommandBlock(cmdMatch[2].trim(), tabNum));
      } else if (part) {
        const textNode = document.createElement('div');
        textNode.className = 'chat-text';
        textNode.innerHTML = formatText(part);
        bubble.appendChild(textNode);
      }
    });
  }

  function formatText(text) {
    // Minimal markdown: fenced code blocks, inline code, bold
    return text
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/```([\s\S]*?)```/g, '<pre class="chat-code-block"><code>$1</code></pre>')
      .replace(/`([^`]+)`/g, '<code class="chat-inline-code">$1</code>')
      .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
      .replace(/\n/g, '<br>');
  }

  function buildCommandBlock(cmd, targetTabNum = null) {
    const wrap = document.createElement('div');
    wrap.className = 'cmd-block';
    if (targetTabNum) wrap.dataset.targetTab = targetTabNum;

    // Resolve label for the target tab
    let tabLabel = '';
    if (targetTabNum) {
      const t = typeof window.getTabByNumber === 'function' ? window.getTabByNumber(targetTabNum) : null;
      tabLabel = t ? `→ Tab ${targetTabNum}: ${t.label}` : `→ Tab ${targetTabNum}`;
    }

    wrap.innerHTML = `
      <pre class="cmd-block-text">${escHtml(cmd)}</pre>
      <div class="cmd-block-actions">
        ${tabLabel ? `<span class="cmd-target-label">${escHtml(tabLabel)}</span>` : ''}
        <button class="cmd-send btn-primary" title="${targetTabNum ? `Send to Tab ${targetTabNum}` : 'Send to active terminal'}">
          <span class="material-symbols-outlined">send</span> Send
        </button>
        <button class="cmd-edit btn-secondary" title="Edit before sending">
          <span class="material-symbols-outlined">edit</span>
        </button>
      </div>
    `;
    return wrap;
  }

  function wireCommandBlocks(bubble) {
    bubble.querySelectorAll('.cmd-block').forEach(block => {
      const pre       = block.querySelector('.cmd-block-text');
      const sendBtn2  = block.querySelector('.cmd-send');
      const editBtn   = block.querySelector('.cmd-edit');
      const targetTab = block.dataset.targetTab ? parseInt(block.dataset.targetTab, 10) : null;

      if (sendBtn2 && !sendBtn2.dataset.wired) {
        sendBtn2.dataset.wired = '1';
        sendBtn2.addEventListener('click', () => injectCommand(pre.textContent, targetTab));
      }

      if (editBtn && !editBtn.dataset.wired) {
        editBtn.dataset.wired = '1';
        editBtn.addEventListener('click', () => {
          pre.contentEditable = 'true';
          pre.focus();
          const range = document.createRange();
          range.selectNodeContents(pre);
          window.getSelection().removeAllRanges();
          window.getSelection().addRange(range);
          editBtn.style.display = 'none';
        });
      }
    });
  }

  function injectCommand(cmd, targetTabNum = null) {
    // If a specific tab number was given, route to that tab; otherwise use active tab
    let tab = null;
    if (targetTabNum && typeof window.getTabByNumber === 'function') {
      tab = window.getTabByNumber(targetTabNum);
      if (!tab) {
        appendErrorBubble(`Tab ${targetTabNum} is not open.`);
        return;
      }
      // Switch to target tab and flash it so the user notices the context change
      if (typeof window.switchToTabBySessionId === 'function') {
        window.switchToTabBySessionId(tab.sessionId);
      }
      _flashTab(tab);
    } else {
      tab = typeof window.getActiveTab === 'function' ? window.getActiveTab() : null;
      if (!tab) {
        appendErrorBubble('No active terminal session to send command to.');
        return;
      }
    }

    const ws = tab.websocket;
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      appendErrorBubble(`Tab ${targetTabNum || 'active'} is not connected.`);
      return;
    }

    const clean = cmd.replace(/^[`'"]+|[`'"]+$/g, '').trim();
    ws.send(JSON.stringify({ type: 'input', data: clean + '\r' }));

    const baselineLines = tab.getBufferLines ? tab.getBufferLines() : 0;
    startOutputWatcher(clean, baselineLines, tab.sessionId);
  }

  // -----------------------------------------------------------------------
  // Output watcher — feeds command output back to the AI automatically
  // -----------------------------------------------------------------------

  function startOutputWatcher(cmd, baselineLines, sessionId) {
    // Cancel any existing watcher
    if (_outputWatcher) {
      _outputWatcher.cancel();
    }

    let collected  = '';
    let idleTimer  = null;
    const IDLE_MS  = 2500; // wait this long after last output chunk before sending

    function onOutput(e) {
      if (e.detail.sessionId !== sessionId) return;
      collected += e.detail.data;
      resetIdle();
    }

    function resetIdle() {
      clearTimeout(idleTimer);
      idleTimer = setTimeout(flush, IDLE_MS);
    }

    function flush() {
      cleanup();
      const output = collected.trim();
      if (!output) return;

      // Strip ANSI escape codes
      const clean = output.replace(/\x1b\[[0-9;]*[mGKHF]/g, '').trim();
      if (!clean) return;

      // Send silently — no user bubble, AI responds as if it observed the output itself
      const silentMsg = `The user just ran \`${cmd}\` in the terminal and this output appeared:\n\`\`\`\n${clean}\n\`\`\`\nAnalyse it naturally, as if you are watching the terminal in real time. Do not say "you ran" or reference receiving a message — just respond as an engineer who can see the screen.`;

      setTimeout(() => sendSilent(silentMsg, sessionId), 300);
    }

    function cleanup() {
      window.removeEventListener('shellmate:terminal-output', onOutput);
      clearTimeout(idleTimer);
      _outputWatcher = null;
    }

    window.addEventListener('shellmate:terminal-output', onOutput);
    // Safety timeout — give up after 30s regardless
    idleTimer = setTimeout(flush, 30000);

    _outputWatcher = { cancel: cleanup };
  }

  // -----------------------------------------------------------------------
  // Quick chat buttons
  // -----------------------------------------------------------------------

  function loadQuickButtons() {
    try {
      const stored = localStorage.getItem(QUICK_BUTTONS_KEY);
      return stored ? JSON.parse(stored) : [...DEFAULT_QUICK_BTNS];
    } catch (_) {
      return [...DEFAULT_QUICK_BTNS];
    }
  }

  function saveQuickButtons(btns) {
    localStorage.setItem(QUICK_BUTTONS_KEY, JSON.stringify(btns));
  }

  function renderQuickButtons() {
    const list = document.getElementById('quick-buttons-list');
    if (!list) return;
    const btns = loadQuickButtons();
    list.innerHTML = '';

    btns.forEach((label, idx) => {
      const wrap = document.createElement('div');
      wrap.className = 'quick-btn-wrap';

      const btn = document.createElement('button');
      btn.className = 'quick-btn';
      btn.textContent = label;
      btn.title = 'Click to use · Right-click to edit';

      // Left-click: send immediately
      btn.addEventListener('click', () => {
        inputEl.value = label;
        sendMessage();
      });

      // Right-click: inline edit
      btn.addEventListener('contextmenu', (e) => {
        e.preventDefault();
        startInlineEdit(btn, idx);
      });

      const del = document.createElement('button');
      del.className = 'quick-btn-del';
      del.innerHTML = '<span class="material-symbols-outlined">close</span>';
      del.title = 'Remove';
      del.addEventListener('click', (e) => {
        e.stopPropagation();
        const current = loadQuickButtons();
        current.splice(idx, 1);
        saveQuickButtons(current);
        renderQuickButtons();
      });

      wrap.appendChild(btn);
      wrap.appendChild(del);
      list.appendChild(wrap);
    });
  }

  function startInlineEdit(btn, idx) {
    const original = btn.textContent;
    const input = document.createElement('input');
    input.type = 'text';
    input.className = 'quick-btn-edit-input';
    input.value = original;

    btn.replaceWith(input);
    input.focus();
    input.select();

    function commit() {
      const val = input.value.trim();
      if (val && val !== original) {
        const current = loadQuickButtons();
        current[idx] = val;
        saveQuickButtons(current);
      }
      renderQuickButtons();
    }

    input.addEventListener('blur', commit);
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); commit(); }
      if (e.key === 'Escape') { renderQuickButtons(); }
    });
  }

  function addQuickButton() {
    const current = loadQuickButtons();
    current.push('New question?');
    saveQuickButtons(current);
    renderQuickButtons();
    // Auto-open edit on the new button
    const list = document.getElementById('quick-buttons-list');
    if (!list) return;
    const lastBtn = list.querySelectorAll('.quick-btn');
    const last = lastBtn[lastBtn.length - 1];
    if (last) startInlineEdit(last, current.length - 1);
  }

  // -----------------------------------------------------------------------
  // Draggable split divider
  // -----------------------------------------------------------------------

  function initDivider() {
    const divider  = document.getElementById('split-divider');
    const chatPane = document.getElementById('chat-pane');
    if (!divider || !chatPane) return;

    let dragging   = false;
    let startX     = 0;
    let startWidth = 0;

    divider.addEventListener('mousedown', (e) => {
      dragging   = true;
      startX     = e.clientX;
      startWidth = chatPane.offsetWidth;
      divider.classList.add('dragging');
      document.body.style.cursor     = 'col-resize';
      document.body.style.userSelect = 'none';
    });

    document.addEventListener('mousemove', (e) => {
      if (!dragging) return;
      const delta    = startX - e.clientX;   // moving left = chat gets bigger
      const newWidth = Math.max(260, Math.min(window.innerWidth * 0.6, startWidth + delta));
      chatPane.style.width = newWidth + 'px';
      chatPane.style.flex  = 'none';
      // Refit active terminal
      if (typeof window.getActiveTab === 'function') {
        const tab = window.getActiveTab();
        if (tab && tab.fitAddon) {
          requestAnimationFrame(() => { try { tab.fitAddon.fit(); } catch (_) {} });
        }
      }
    });

    document.addEventListener('mouseup', () => {
      if (!dragging) return;
      dragging = false;
      divider.classList.remove('dragging');
      document.body.style.cursor     = '';
      document.body.style.userSelect = '';
    });
  }

  // -----------------------------------------------------------------------
  // Pop-out chat window — drag by header, resize from bottom-right corner
  // -----------------------------------------------------------------------

  const POPOUT_KEY = 'mate:chat-popout';

  function initPopout() {
    const btn      = document.getElementById('chat-popout');
    const icon     = document.getElementById('chat-popout-icon');
    const chatPane = document.getElementById('chat-pane');
    const header   = document.getElementById('chat-header');
    if (!btn || !chatPane || !header) return;

    function isPopped() { return chatPane.classList.contains('popped-out'); }

    function setIcon(popped) {
      icon.textContent = popped ? 'close_fullscreen' : 'open_in_new';
      btn.title = popped
        ? 'Dock chat back into the layout'
        : 'Pop out chat (drag to move, drag corner to resize)';
    }

    function applyState(state) {
      // state: { popped: bool, top, left, width, height }
      if (state && state.popped) {
        // Default placement: top-right with sensible size
        const w = Math.max(320, Math.min(window.innerWidth - 40, state.width  || 420));
        const h = Math.max(280, Math.min(window.innerHeight - 40, state.height || 600));
        const left = state.left != null
          ? Math.max(0, Math.min(window.innerWidth  - 80, state.left))
          : (window.innerWidth - w - 24);
        const top  = state.top != null
          ? Math.max(0, Math.min(window.innerHeight - 60, state.top))
          : 80;
        chatPane.classList.add('popped-out');
        chatPane.style.top    = top  + 'px';
        chatPane.style.left   = left + 'px';
        chatPane.style.width  = w + 'px';
        chatPane.style.height = h + 'px';
        document.body.classList.add('chat-popped');
      } else {
        chatPane.classList.remove('popped-out');
        chatPane.style.top = chatPane.style.left = '';
        chatPane.style.height = '';
        chatPane.style.width = ''; // restored to CSS default
        document.body.classList.remove('chat-popped');
      }
      setIcon(isPopped());
      // Refit active terminal because layout shifted
      if (typeof window.getActiveTab === 'function') {
        const tab = window.getActiveTab();
        if (tab && tab.fitAddon) {
          requestAnimationFrame(() => { try { tab.fitAddon.fit(); } catch (_) {} });
        }
      }
    }

    function saveState() {
      const r = chatPane.getBoundingClientRect();
      try {
        localStorage.setItem(POPOUT_KEY, JSON.stringify({
          popped: isPopped(),
          top:    r.top,
          left:   r.left,
          width:  r.width,
          height: r.height,
        }));
      } catch (_) {}
    }

    function loadState() {
      try {
        const raw = localStorage.getItem(POPOUT_KEY);
        return raw ? JSON.parse(raw) : null;
      } catch (_) { return null; }
    }

    // Restore last state on page load
    const saved = loadState();
    if (saved && saved.popped) applyState(saved);
    setIcon(isPopped());

    // Toggle button
    btn.addEventListener('click', () => {
      if (isPopped()) {
        applyState({ popped: false });
      } else {
        const prev = loadState() || {};
        applyState({ popped: true, ...prev, popped: true });
      }
      saveState();
    });

    // --- Drag by header --------------------------------------------------
    let dragging = false, startX = 0, startY = 0, startTop = 0, startLeft = 0;

    header.addEventListener('mousedown', (e) => {
      if (!isPopped()) return;
      // Don't start a drag when the user clicks a control inside the header
      if (e.target.closest('button, select, input, textarea, a')) return;
      dragging  = true;
      startX    = e.clientX;
      startY    = e.clientY;
      const r   = chatPane.getBoundingClientRect();
      startTop  = r.top;
      startLeft = r.left;
      chatPane.classList.add('dragging');
      e.preventDefault();
    });

    document.addEventListener('mousemove', (e) => {
      if (!dragging) return;
      const r   = chatPane.getBoundingClientRect();
      const newLeft = Math.max(0, Math.min(window.innerWidth  - r.width  - 4, startLeft + (e.clientX - startX)));
      const newTop  = Math.max(0, Math.min(window.innerHeight - r.height - 4, startTop  + (e.clientY - startY)));
      chatPane.style.left = newLeft + 'px';
      chatPane.style.top  = newTop  + 'px';
    });

    document.addEventListener('mouseup', () => {
      if (!dragging) return;
      dragging = false;
      chatPane.classList.remove('dragging');
      saveState();
    });

    // --- Persist resize -------------------------------------------------
    // CSS `resize: both` does the resizing; observe the size to persist it.
    if (typeof ResizeObserver === 'function') {
      new ResizeObserver(() => { if (isPopped()) saveState(); }).observe(chatPane);
    }

    // Keep the window inside the viewport when the browser is resized
    window.addEventListener('resize', () => {
      if (!isPopped()) return;
      const r = chatPane.getBoundingClientRect();
      const left = Math.max(0, Math.min(window.innerWidth  - r.width  - 4, r.left));
      const top  = Math.max(0, Math.min(window.innerHeight - r.height - 4, r.top));
      chatPane.style.left = left + 'px';
      chatPane.style.top  = top  + 'px';
      saveState();
    });
  }

  // -----------------------------------------------------------------------
  // Context indicator
  // -----------------------------------------------------------------------

  // -----------------------------------------------------------------------
  // Context size estimator
  // -----------------------------------------------------------------------
  // Claude Sonnet context window (tokens).  Ollama models vary but 32k is a
  // safe conservative estimate for the local models most people run.
  const CONTEXT_LIMITS = { claude: 200_000, ollama: 32_000, xai: 131_072, openai: 128_000, deepseek: 64_000 };

  function _estimateTokens() {
    // Chat history chars (tracked in jira.js via addJiraChatMessage)
    const history = typeof window.getJiraChatHistory === 'function'
      ? window.getJiraChatHistory() : [];
    const chatChars = history.reduce((s, m) => s + (m.text || '').length, 0);

    // Active terminal buffer — read the last 200 lines (matches backend's get_text(200))
    const activeTab = typeof window.getActiveTab === 'function' ? window.getActiveTab() : null;
    const bufChars  = (activeTab && activeTab.getContextChars) ? activeTab.getContextChars(200) : 0;

    // Fixed overhead: system prompt + per-request framing (~900 tokens)
    return 900 + Math.round((chatChars + bufChars) / 4);
  }

  function updateContextIndicator(tab) {
    // Update the chat-header label (shows active tab name)
    if (contextIndicator) {
      const activeTab = tab || (typeof window.getActiveTab === 'function' ? window.getActiveTab() : null);
      contextIndicator.textContent = activeTab ? (activeTab.label || 'active session') : 'no session';
    }

    // Update the status-bar context meter
    const statusEl = document.getElementById('status-context');
    if (!statusEl) return;

    const limit  = CONTEXT_LIMITS[currentBackend] || 200_000;
    const tokens = _estimateTokens();
    const pct    = Math.min(100, Math.round((tokens / limit) * 100));
    const kTok   = tokens >= 1_000 ? `${Math.round(tokens / 1_000)}k` : `${tokens}`;

    // Dot character + label
    const dot = '●';
    statusEl.textContent = `${dot} Context: ~${kTok} tok`;
    statusEl.title = `~${tokens.toLocaleString()} estimated tokens · ${pct}% of ${Math.round(limit/1000)}k ${currentBackend} limit.\nGreen <25% · Amber 25–65% · Red >65%`;

    statusEl.className = pct < 25 ? 'ctx-green'
                       : pct < 65 ? 'ctx-amber'
                       :            'ctx-red';
  }

  window.updateContextStatus = updateContextIndicator;

  // -----------------------------------------------------------------------
  // Helpers
  // -----------------------------------------------------------------------

  function clearChat() {
    messagesEl.innerHTML = '';
    streamingBubble      = null;
    isStreaming          = false;
    sendBtn.disabled     = false;
    // Reset Jira chat history so context estimate resets too
    if (typeof window._clearJiraChatHistory === 'function') window._clearJiraChatHistory();
    updateContextIndicator();
  }

  /**
   * Scroll the messages pane to the bottom.
   * @param {boolean} force - If true, always scroll (used when a new bubble appears).
   *                          If false (default), only scroll when the user is already
   *                          near the bottom — preserves scroll position while reading.
   */
  function scrollToBottom(force = false) {
    if (force) {
      messagesEl.scrollTop = messagesEl.scrollHeight;
      return;
    }
    const distFromBottom = messagesEl.scrollHeight - messagesEl.scrollTop - messagesEl.clientHeight;
    if (distFromBottom < 120) {
      messagesEl.scrollTop = messagesEl.scrollHeight;
    }
  }

  function escHtml(s) {
    return s
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;');
  }

  // Expose for test access and for settings.js to update the backend selector
  window._chatInjectCommand  = injectCommand;
  window._chatSend           = sendMessage;
  window._chatSetBackend     = (val) => {
    if (backendSelect) backendSelect.value = val;
    const idx = val.indexOf(':');
    currentBackend = idx === -1 ? val : val.slice(0, idx);
    currentModel   = idx === -1 ? val : val.slice(idx + 1);
  };

})();
