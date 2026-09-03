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
  let currentModel    = 'claude-sonnet-5'; // specific model string
  let contextMode     = 'active'; // 'active' | 'all' | '1'..'9'
  let streamingBubble = null;     // the <div> currently being filled
  const _outputWatchers = new Map();  // active command output watchers, by session (#317)

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

    _applySelection(backendSelect.value || 'claude:claude-sonnet-5');

    /**
     * Restore the saved model.
     *
     * This used to live only in `currentModel`, so choosing DeepSeek and
     * reloading the page put you back on Claude Sonnet. The quick buttons
     * moved out of localStorage into settings.json precisely so a preference
     * would survive a reload and travel with the data folder; this never
     * followed.
     *
     * Only applied when the option still exists — a key that has stopped
     * working, or Ollama not running, should not leave the picker showing a
     * model that cannot answer.
     */
    function applySavedModel() {
      const saved = ((window.shellmateSettings || {}).ai || {}).default_model;
      if (!saved) return;
      if (![...backendSelect.options].some(o => o.value === saved)) {
        console.info('Saved model %s is not available; keeping the current one', saved);
        return;
      }
      backendSelect.value = saved;
      _applySelection(saved);
    }

    applySavedModel();
    // The picker is rebuilt from the providers' own model lists after a
    // connection test, so the saved choice has to be re-applied afterwards.
    window.addEventListener('shellmate:settings-loaded', applySavedModel);
    window.addEventListener('shellmate:models-refreshed', applySavedModel);

    // Dynamically populate local Ollama models
    function loadLocalModels() {
      fetch('/api/ollama/models').then(r => r.json()).then(models => {
        const group = document.getElementById('local-models-group');
        if (!group) return;
        const previous = backendSelect.value;
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
        // Rewriting the group's options can silently move the selection; put
        // it back when the selected model still exists.
        if ([...backendSelect.options].some(o => o.value === previous)) {
          backendSelect.value = previous;
        }
      }).catch(() => {
        const group = document.getElementById('local-models-group');
        if (group) { group.innerHTML = '<option value="_err" disabled>Ollama unavailable</option>'; }
      });
    }
    loadLocalModels();
    // The picker is rebuilt from the model cache on load and from live
    // discovery after a test — both replace the local group's contents, and
    // the cache's idea of Ollama may be older than Ollama itself. Asking
    // Ollama again after every rebuild keeps the local list live.
    window.addEventListener('shellmate:models-refreshed', loadLocalModels);

    // Wire up events
    sendBtn.addEventListener('click', sendMessage);
    inputEl.addEventListener('keydown', (e) => {
      if (e.key !== 'Enter') return;
      // Enter sends by default. Anybody writing more than a sentence to the
      // assistant wants the opposite, and Shift+Enter alone is not enough
      // when the whole message is three paragraphs.
      const mode = ((window.shellmateSettings || {}).interface || {}).chat_enter;
      const sendsOnEnter = mode !== 'newline';
      if (sendsOnEnter ? !e.shiftKey : (e.ctrlKey || e.metaKey)) {
        e.preventDefault();
        sendMessage();
      }
    });

    // Auto-resize textarea as user types
    inputEl.addEventListener('input', () => {
      inputEl.style.height = 'auto';
      inputEl.style.height = Math.min(inputEl.scrollHeight, 120) + 'px';
    });

    backendSelect.addEventListener('change', () => {
      _applySelection(backendSelect.value);
      updateContextIndicator();
      // A picker rebuild dispatches a synthetic change to sync this state,
      // marked so it is not *persisted* (#312) — only a person choosing a
      // model should overwrite the saved default.
      if (backendSelect.dataset.rebuilding) return;
      saveModelChoice(backendSelect.value);
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
      // A drop mid-reply must release the chat (#315): isStreaming stayed
      // true and the send button stayed disabled forever — the reconnected
      // socket was unusable until "Clear chat" happened to be clicked.
      if (isStreaming) {
        finishStreaming();
        appendErrorBubble('The connection dropped mid-reply. Reconnecting…');
      }
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
    } else if (msg.type === 'usage') {
      _recordUsage(msg);
    } else if (msg.type === 'done') {
      finishStreaming();
    } else if (msg.type === 'error') {
      finishStreaming();
      appendErrorBubble(msg.message || 'Unknown error');
      // A retired model id is self-healing: refresh the picker from the
      // providers so the dead option disappears and the saved default is
      // re-pointed at something that exists (#230). Guarded so a run of
      // failures does not hammer every provider's models endpoint.
      if (/does not recognise the model/i.test(msg.message || '')
          && typeof window.refreshProviderModels === 'function'
          && !handleWsMessage._refreshedForModel) {
        handleWsMessage._refreshedForModel = true;
        setTimeout(() => { handleWsMessage._refreshedForModel = false; }, 30000);
        window.refreshProviderModels();
      }
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

    // "/investigate <problem>" switches to Investigate mode and asks (#403).
    let text2 = text;
    const inv = text.match(/^\/investigate\b\s*/i);
    if (inv) {
      if (typeof window.setShellmateMode === 'function') window.setShellmateMode('investigate');
      text2 = text.slice(inv[0].length).trim();
      if (!text2) {
        inputEl.value = '';
        appendErrorBubble('Investigate mode is on. Describe the problem and the assistant will plan the steps.');
        return;
      }
    }

    // Parse context commands
    let message = text2;
    let mode = contextMode;

    const ctxMatch = text2.match(/^\/context\s+(all|\d+)\s*/i);
    if (ctxMatch) {
      mode = ctxMatch[1].toLowerCase();
      message = text2.slice(ctxMatch[0].length).trim();
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

    // The earlier turns, taken before this one is added to the transcript
    // so the question is not sent twice (#402).
    const history = _recentHistory();
    // Record in Jira chat history
    if (typeof window.addJiraChatMessage === 'function') window.addJiraChatMessage('user', text);

    // Render user bubble
    appendUserBubble(text);
    inputEl.value = '';
    inputEl.style.height = 'auto';

    // Start streaming AI bubble. The bubble remembers which session the
    // question was asked about (#308), so the command blocks in the answer
    // can be sent to that session however many tabs are switched meanwhile.
    startStreamingBubble();
    if (streamingBubble && sessionId) {
      streamingBubble.dataset.contextSession = sessionId;
    }
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
        history,
        investigate_step:  _investigation.steps,
        session_id:        sessionId,
        // Always the real tab order. The selection used to be sent *as* this
        // list, which renumbered the AI's session summary over the subset —
        // so its tab numbers stopped matching the tab bar (#213).
        open_session_ids:  openIds,
        context_session_ids: picked,
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
    const streamEl = streamingBubble.querySelector('.chat-stream-live');
    if (streamEl) {
      // The delta as a text node, with a full format pass at most every
      // 300ms (#319). Re-running formatText over the whole accumulated
      // message per chunk was six regex passes plus a DOM parse, O(n²)
      // across a long answer. Raw markdown may show for a beat between
      // passes; finishStreaming renders the exact final form.
      streamEl.appendChild(document.createTextNode(text));
      const now = performance.now();
      if (!streamingBubble._lastFormat || now - streamingBubble._lastFormat > 300) {
        streamingBubble._lastFormat = now;
        streamEl.innerHTML = formatText(streamingBubble.dataset.raw);
      }
    }
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

  function sendSilent(message, sessionId, autoAnalysis) {
    if (isStreaming) return;
    if (!chatWs || chatWs.readyState !== WebSocket.OPEN) return;
    // A convenience, not the guarantee — the server enforces this too, so a
    // page left open cannot keep shipping output after it is switched off.
    if (autoAnalysis && window.shellmateAdvanced
        && !window.shellmateAdvanced('ai.analyse_output', true)) return;

    const activeTab = typeof window.getActiveTab === 'function' ? window.getActiveTab() : null;
    const sid = sessionId || (activeTab ? activeTab.sessionId : null);

    // No user bubble — just start the AI bubble with a subtle "auto" badge
    startStreamingBubble(true);
    if (streamingBubble && sid) {
      streamingBubble.dataset.contextSession = sid;
    }
    isStreaming = true;
    sendBtn.disabled = true;

    const aiMode = typeof window.getShellmateMode === 'function' ? window.getShellmateMode() : 'tshoot';
    // The same context the user chose for typed messages. This path used to
    // send none of it, so in a flow of approved commands — where most AI
    // turns arrive through here — the picker's selection silently "wore off"
    // after the first reply (#213).
    const openIds = typeof window.getOpenSessionIds === 'function' ? window.getOpenSessionIds() : [];
    const picked = typeof window.getChatContextSelection === 'function'
      ? window.getChatContextSelection() : null;
    chatWs.send(JSON.stringify({
      message:       message || '',
      history:       _recentHistory(),
      investigate_step: _investigation.steps,
      // The command and the device's reply as data. The server composes the
      // prompt from them, which is what lets it mask the output — composed
      // here it arrived as an ordinary user message with the configuration
      // already inside it.
      auto_analysis: autoAnalysis || null,
      session_id:    sid,
      open_session_ids: openIds,
      context_session_ids: picked,
      backend:       currentBackend,
      model:         currentModel,
      context_mode:  picked ? 'selected' : contextMode,
      mode:          aiMode,
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
    raw = raw.replace(/^#{1,6}\s*\[?(SUGGEST_CMD)\]/gm, '[$1]');  // strip heading prefix
    raw = raw.replace(/(?<!\[)(SUGGEST_CMD)\]/g, '[$1]');          // fix missing opening [
    raw = raw.replace(/\[\/\[+(SUGGEST_CMD)\]/g, '[/$1]');         // fix extra [ in closing tag

    // Split on [SUGGEST_CMD]...[/SUGGEST_CMD] or [SUGGEST_CMD:N]...[/SUGGEST_CMD] blocks
    // One tag only. An ADD_CMD variant was accepted here for years without
    // ever being taught to the model, so no reply carried it (#430).
    const parts = raw.split(/(\[SUGGEST_CMD(?::\d+)?\][\s\S]*?\[\/SUGGEST_CMD\]|\[PLAN\][\s\S]*?\[\/PLAN\])/g);
    bubble.innerHTML = '';

    parts.forEach(part => {
      // Group 1 = optional tab number, group 2 = command text
      const cmdMatch = part.match(/^\[SUGGEST_CMD(?::(\d+))?\]([\s\S]*?)\[\/SUGGEST_CMD\]$/);
      const planMatch = part.match(/^\[PLAN\]([\s\S]*?)\[\/PLAN\]$/);
      if (cmdMatch) {
        const tabNum = cmdMatch[1] ? parseInt(cmdMatch[1], 10) : null;
        bubble.appendChild(buildCommandBlock(cmdMatch[2].trim(), tabNum,
                                             bubble.dataset.contextSession || ''));
      } else if (planMatch) {
        bubble.appendChild(buildPlanCard(planMatch[1]));
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

  /**
   * The investigation plan as a checklist (#403). One line per step:
   * "1. [x] show ip interface brief — which ports are down". Marks: [ ]
   * pending, [x] done, [-] dropped. Anything else is shown as plain text so
   * a model that drifts from the format still gets its plan on screen.
   */
  function buildPlanCard(body) {
    const card = document.createElement('div');
    card.className = 'plan-card';
    const title = document.createElement('div');
    title.className = 'plan-card-title';
    title.innerHTML = '<span class="material-symbols-outlined">list_alt</span> Plan';
    card.appendChild(title);
    const list = document.createElement('ol');
    list.className = 'plan-list';
    body.trim().split('\n').forEach(line => {
      const m = line.match(/^\s*(\d+)[.)]\s*\[([ xX-])\]\s*(.*)$/);
      const li = document.createElement('li');
      if (m) {
        const mark = m[2].toLowerCase();
        li.className = 'plan-step ' + (mark === 'x' ? 'done' : mark === '-' ? 'dropped' : 'todo');
        li.value = Number(m[1]);
        const box = document.createElement('span');
        box.className = 'material-symbols-outlined plan-mark';
        // Only glyphs the shipped font subset carries (test_icons.py).
        box.textContent = mark === 'x' ? 'check_circle' : mark === '-' ? 'cancel' : 'pending';
        const text = document.createElement('span');
        text.className = 'plan-text';
        const [cmd, ...rest] = m[3].split(/\s+[—–-]\s+/);
        const code = document.createElement('code');
        code.className = 'chat-inline-code';
        code.textContent = cmd.trim();
        text.appendChild(code);
        if (rest.length) text.appendChild(document.createTextNode(' — ' + rest.join(' — ')));
        li.append(box, text);
      } else if (line.trim()) {
        li.className = 'plan-step note';
        li.textContent = line.trim();
      } else {
        return;
      }
      list.appendChild(li);
    });
    card.appendChild(list);
    return card;
  }

  function buildCommandBlock(cmd, targetTabNum = null, contextSession = '') {
    const wrap = document.createElement('div');
    wrap.className = 'cmd-block';

    // The block is bound to a *session*, at render time (#308, #316). Tab
    // numbers are positions, and positions move: a `[SUGGEST_CMD:2]` block
    // resolved at click time injected into whatever had been re-sorted into
    // slot 2, and an untargeted block went to whichever tab was active when
    // it was clicked — the wrong-session approval failure the design forbids.
    let tabLabel = '';
    if (targetTabNum) {
      const t = typeof window.getTabByNumber === 'function' ? window.getTabByNumber(targetTabNum) : null;
      if (t) wrap.dataset.targetSession = t.sessionId;
      tabLabel = t ? `→ Tab ${targetTabNum}: ${t.label}` : `→ Tab ${targetTabNum}`;
    } else if (contextSession) {
      // The session the question was asked about — not the active tab.
      wrap.dataset.targetSession = contextSession;
      const tabs = typeof window.getOpenTabs === 'function' ? window.getOpenTabs() : [];
      const t = tabs.find(x => x.sessionId === contextSession);
      if (t) tabLabel = `→ ${t.label}`;
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
      const targetSession = block.dataset.targetSession || null;

      if (sendBtn2 && !sendBtn2.dataset.wired) {
        sendBtn2.dataset.wired = '1';
        sendBtn2.addEventListener('click', () => injectCommand(pre.textContent, targetSession));
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

  function injectCommand(cmd, targetSessionId = null) {
    // Routed by session id, bound when the block was rendered (#308, #316).
    // Falling back to the active tab is last resort only — for blocks that
    // predate the binding (a restored chat) — because "active at click time"
    // is exactly how a command meant for a core switch reaches a firewall.
    let tab = null;
    if (targetSessionId) {
      const tabs = typeof window.getOpenTabs === 'function' ? window.getOpenTabs() : [];
      tab = tabs.find(t => t.sessionId === targetSessionId) || null;
      if (!tab) {
        appendErrorBubble('The session this command was suggested for is no longer open.');
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

    // Through the terminal's own socket, looked up by session id at send
    // time (#453). The tab list handed out by tabs.js is a projection with
    // no socket on it, so `tab.websocket` was always undefined and every
    // targeted send answered "is not connected" while the tab sat there
    // connected. The active-tab path only worked because it read a
    // different object.
    const clean = cmd.replace(/^[`'"]+|[`'"]+$/g, '').trim();
    const sent = typeof window.sendCommandToSession === 'function'
      && window.sendCommandToSession(tab.sessionId, clean);
    if (!sent) {
      appendErrorBubble(`"${tab.label || 'The target session'}" is not connected.`
        + (tab.isConnected ? ' Its terminal has no open socket; try reconnecting the tab.' : ''));
      return;
    }

    const baselineLines = tab.getBufferLines ? tab.getBufferLines() : 0;

    // An approved command is one investigation step (#403). Past the
    // budget the result is not fed back: the model was told to conclude,
    // and a loop that keeps feeding it is how "one more step" never ends.
    const aiMode = typeof window.getShellmateMode === 'function' ? window.getShellmateMode() : 'tshoot';
    if (aiMode === 'investigate') {
      _investigation.steps += 1;
      const budget = Number(A('ai.investigate_max_steps', 8)) || 8;
      if (_investigation.steps > budget) {
        appendErrorBubble(`The investigation's budget of ${budget} steps is spent. `
          + 'Ask for a conclusion, or clear the chat to start another.');
        return;
      }
    }
    startOutputWatcher(clean, baselineLines, tab.sessionId);
  }

  /** Where an Investigate-mode run stands: approved commands so far. */
  const _investigation = { steps: 0 };
  const A = (key, fallback) =>
    (window.shellmateAdvanced ? window.shellmateAdvanced(key, fallback) : fallback);
  window.addEventListener('shellmate:mode-changed', () => { _investigation.steps = 0; });

  // -----------------------------------------------------------------------
  // Output watcher — feeds command output back to the AI automatically
  // -----------------------------------------------------------------------

  function startOutputWatcher(cmd, baselineLines, sessionId) {
    // One watcher per session, not one global (#317): approving a command on
    // tab B used to silently cancel tab A's still-collecting watcher, and
    // A's analysis never arrived, with no sign why.
    const existing = _outputWatchers.get(sessionId);
    if (existing) existing.cancel();

    let collected  = '';
    let idleTimer  = null;
    let hardTimer  = null;
    const IDLE_MS  = 2500;    // wait this long after last output chunk
    const MAX_MS   = 30000;   // the ceiling, whatever the device is doing
    const MAX_CHARS = 200000; // a flood flushes early rather than growing

    function onOutput(e) {
      if (e.detail.sessionId !== sessionId) return;
      collected += e.detail.data;
      if (collected.length >= MAX_CHARS) {
        flush();
        return;
      }
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

      // The command and the device's reply go as *data*, not as a prompt
      // composed here. Composed here, the message reached the provider as
      // an ordinary user message with the output already inside it, so the
      // server-side redaction never saw it — an approved
      // `show running-config` sent the configuration, hashes and community
      // strings included. The server composes it now, masks it, caps it,
      // and decides whether to send it at all.
      setTimeout(() => sendSilent(null, sessionId,
                                  { command: cmd, output: clean }), 300);
    }

    function cleanup() {
      window.removeEventListener('shellmate:terminal-output', onOutput);
      clearTimeout(idleTimer);
      clearTimeout(hardTimer);
      _outputWatchers.delete(sessionId);
    }

    window.addEventListener('shellmate:terminal-output', onOutput);
    idleTimer = setTimeout(flush, IDLE_MS * 4);   // nothing at all arriving
    // A real ceiling (#317). The old "30s safety timeout" was the idle timer
    // itself, so the first chunk of a debug flood cleared it and continuous
    // output collected forever, never flushing.
    hardTimer = setTimeout(flush, MAX_MS);

    _outputWatchers.set(sessionId, { cancel: cleanup });
  }

  // -----------------------------------------------------------------------
  // Quick chat buttons
  // -----------------------------------------------------------------------

  // Kept in settings.json rather than localStorage, so buttons someone has
  // curated travel with the data folder — see frontend/js/prefs.js.
  function loadQuickButtons() {
    const stored = window.shellmatePrefs
      ? window.shellmatePrefs.get('quick_buttons', null) : null;
    return (Array.isArray(stored) && stored.length)
      ? stored : [...DEFAULT_QUICK_BTNS];
  }

  function saveQuickButtons(btns) {
    if (window.shellmatePrefs) window.shellmatePrefs.set('quick_buttons', btns);
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

      // Remembered as a fraction rather than a pixel width, so it still makes
      // sense when ShellMate is next opened on a different-sized screen.
      if (window.shellmatePrefs) {
        window.shellmatePrefs.set(
          'chat_pane_fraction',
          Math.round((chatPane.offsetWidth / window.innerWidth) * 1000) / 1000);
      }
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
      if (!window.shellmatePrefs) return;
      window.shellmatePrefs.set('chat_popout', {
        popped: isPopped(),
        top:    r.top,
        left:   r.left,
        width:  r.width,
        height: r.height,
      });
    }

    function loadState() {
      return window.shellmatePrefs
        ? window.shellmatePrefs.get('chat_popout', null) : null;
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
  // Memory (#402) and measured usage (#416)
  // -----------------------------------------------------------------------

  /**
   * The earlier turns, newest last, for the request to carry. The server
   * trims to the configured number of turns; this only caps the payload.
   */
  function _recentHistory() {
    const all = typeof window.getJiraChatHistory === 'function'
      ? window.getJiraChatHistory() : [];
    return all.slice(-40).map(m => ({ role: m.role, text: m.text || '' }));
  }

  let lastUsage = null;
  const totalUsage = { input: 0, output: 0, cache_read: 0, replies: 0 };

  function _recordUsage(msg) {
    lastUsage = {
      input:      Number(msg.input) || 0,
      output:     Number(msg.output) || 0,
      cache_read: Number(msg.cache_read) || 0,
      cache_write: Number(msg.cache_write) || 0,
      provider:   msg.provider || currentBackend,
    };
    totalUsage.input      += lastUsage.input + lastUsage.cache_read;
    totalUsage.output     += lastUsage.output;
    totalUsage.cache_read += lastUsage.cache_read;
    totalUsage.replies    += 1;
  }

  function _resetUsage() {
    lastUsage = null;
    totalUsage.input = totalUsage.output = totalUsage.cache_read = totalUsage.replies = 0;
  }

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
    let bufChars  = (activeTab && activeTab.getContextChars) ? activeTab.getContextChars(200) : 0;

    // Sessions added through the tab picker. The meter used to count only the
    // active tab, so adding sessions moved nothing on screen and read as the
    // assistant not seeing them (#213). 100 lines each, matching the backend.
    const picked = typeof window.getChatContextSelection === 'function'
      ? window.getChatContextSelection() : null;
    if (picked && typeof window.getContextCharsBySessionId === 'function') {
      const activeId = activeTab ? activeTab.sessionId : null;
      picked.forEach(id => {
        if (id !== activeId) bufChars += window.getContextCharsBySessionId(id, 100);
      });
    }

    // Fixed overhead: system prompt + per-request framing (~900 tokens)
    return 900 + Math.round((chatChars + bufChars) / 4);
  }

  /**
   * Remember the chosen model.
   *
   * Written to settings.json rather than held in the page, so it survives a
   * reload and moves with the data folder. Failure is logged and otherwise
   * ignored — losing the preference must not stop the conversation.
   */
  function saveModelChoice(value) {
    if (!value || value.startsWith('_')) return;   // "None found", "unavailable"

    if (window.shellmateSettings) {
      window.shellmateSettings.ai =
        Object.assign({}, window.shellmateSettings.ai, { default_model: value });
    }

    fetch('/api/settings', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ settings: { ai: { default_model: value } } }),
    }).catch((e) => console.warn('Could not save the model choice:', e));
  }

  function updateContextIndicator(tab) {
    // Update the chat-header label: the active tab's name, or — when sessions
    // are chosen in the picker — how many the assistant will see. Showing the
    // active tab regardless was half of #213: the choice worked, but nothing
    // on screen acknowledged it.
    if (contextIndicator) {
      const activeTab = tab || (typeof window.getActiveTab === 'function' ? window.getActiveTab() : null);
      const picked = typeof window.getChatContextSelection === 'function'
        ? window.getChatContextSelection() : null;
      if (picked) {
        const seen = new Set(picked);
        if (activeTab) seen.add(activeTab.sessionId);
        contextIndicator.textContent = `${seen.size} session${seen.size === 1 ? '' : 's'}`;
        contextIndicator.title = 'The assistant sees the active tab plus the sessions chosen in the picker';
      } else {
        contextIndicator.textContent = activeTab ? (activeTab.label || 'active session') : 'no session';
        contextIndicator.title = '';
      }
    }

    // Update the status-bar context meter
    const statusEl = document.getElementById('status-context');
    if (!statusEl) return;

    const limit  = CONTEXT_LIMITS[currentBackend] || 200_000;
    // The provider's own count of the last request beats the chars/4 guess
    // whenever there has been one (#416); cache reads are still context.
    const measured = lastUsage && lastUsage.provider === currentBackend
      ? lastUsage.input + lastUsage.cache_read : 0;
    const tokens = measured || _estimateTokens();
    const pct    = Math.min(100, Math.round((tokens / limit) * 100));
    const kTok   = tokens >= 1_000 ? `${(tokens / 1_000).toFixed(tokens < 10_000 ? 1 : 0)}k` : `${tokens}`;

    // Dot character + label
    const dot = '●';
    statusEl.textContent = `${dot} Context: ${measured ? '' : '~'}${kTok} tok`;
    let title = measured
      ? `${tokens.toLocaleString()} tokens in the last request, as counted by the provider`
      : `~${tokens.toLocaleString()} estimated tokens`;
    title += ` · ${pct}% of ${Math.round(limit/1000)}k ${currentBackend} limit.`;
    if (lastUsage) {
      title += `\nLast reply: ${lastUsage.input.toLocaleString()} in`
        + (lastUsage.cache_read ? ` (+${lastUsage.cache_read.toLocaleString()} from cache)` : '')
        + ` / ${lastUsage.output.toLocaleString()} out.`;
      title += `\nThis conversation: ${totalUsage.input.toLocaleString()} in / `
        + `${totalUsage.output.toLocaleString()} out over ${totalUsage.replies} repl${totalUsage.replies === 1 ? 'y' : 'ies'}`
        + (totalUsage.cache_read ? `, ${totalUsage.cache_read.toLocaleString()} served from cache.` : '.');
    }
    title += '\nGreen <25% · Amber 25–65% · Red >65%';
    statusEl.title = title;

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
    _resetUsage();
    _investigation.steps = 0;
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
