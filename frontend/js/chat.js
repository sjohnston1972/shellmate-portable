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
  // The last question's payload, so a tool exchange can be resumed with
  // the same question rather than a reconstructed one (#560).
  let lastSentPayload = null;
  const _outputWatchers = new Map();  // active command output watchers, by session (#317)
  // Auto-analyses that arrived while a reply was still streaming (#489),
  // sent one at a time as each reply finishes. Dropping them was silent.
  const _pendingSilent = [];

  /**
   * Ansible mode (#602).
   *
   * The assistant answers as a ShellMate-Ansible expert while the Ansible
   * view is open — not a generic one, which would be confidently wrong here
   * in specific ways ("run ansible-playbook", "add it to your inventory
   * file") that are wrong about this integration rather than about Ansible.
   *
   * Chosen by where the user is rather than by the mode toggle, and given
   * back the moment they leave. A persona that silently persisted into a
   * terminal session would be worse than not having one — somebody would
   * ask about a switch and be answered about a container.
   */
  let ansibleMode = false;

  const ANSIBLE_QUICK_BTNS = [
    'Write a play that shuts a port',
    'What would this playbook do?',
    'Why did my last run fail?',
    'How do I get this to the runner?',
    'Check mode or not?',
  ];

  const ANSIBLE_WELCOME =
    'Ansible mode. I know how ShellMate drives your runner — the container, '
    + 'the inventory it generates from your estate, how a playbook actually '
    + 'reaches it, and where credentials come from. Ask for a playbook and '
    + 'you will get one you can send straight to the builder.';

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
    watchAnsibleView();

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

  // Reconnect backoff (#492). Every close used to schedule another attempt
  // two seconds later, forever — including when the server had refused the
  // handshake and would refuse the next one the same way, and with the
  // backend down it was a steady loop of error/close events.
  const CHAT_RETRY_BASE_MS = 2000;
  const CHAT_RETRY_MAX_MS  = 30000;
  let _chatRetries = 0;       // consecutive failures; reset by a successful open

  function connectChatWs() {
    const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const url = `${proto}//${window.location.host}/ws/chat`;
    chatWs = new WebSocket(url);

    chatWs.addEventListener('open', () => { _chatRetries = 0; });
    chatWs.addEventListener('message', handleWsMessage);
    chatWs.addEventListener('close', (event) => {
      // A drop mid-reply must release the chat (#315): isStreaming stayed
      // true and the send button stayed disabled forever — the reconnected
      // socket was unusable until "Clear chat" happened to be clicked.
      if (isStreaming) {
        finishStreaming();
        appendErrorBubble('The connection dropped mid-reply. Reconnecting…');
      }
      // 1008 is the server saying no — an origin or authentication check
      // failed. That does not change by asking again; say so and stop.
      if (event && event.code === 1008) {
        appendErrorBubble('The server refused the assistant connection. '
          + 'Reload the page; if it happens again, check the server log.');
        return;
      }
      // Doubling from two seconds, capped at thirty: quick when the server
      // is restarting, quiet when it is gone.
      const wait = Math.min(CHAT_RETRY_BASE_MS * Math.pow(2, _chatRetries), CHAT_RETRY_MAX_MS);
      _chatRetries += 1;
      setTimeout(connectChatWs, wait);
    });
    chatWs.addEventListener('error', () => {});
  }

  function handleWsMessage(event) {
    let msg;
    try { msg = JSON.parse(event.data); } catch (_) { return; }

    if (msg.type === 'chunk') {
      appendChunk(msg.data);
    } else if (msg.type === 'tables') {
      // Parsed rows for a real table (#554).
      handleTables(msg.tables);
    } else if (msg.type === 'context') {
      // Exactly what this reply was built from (#553).
      attachContext(msg.text || '');
    } else if (msg.type === 'tool_request') {
      // The model asked to run something (#560). Rendered as the command
      // block a suggestion tag produces; nothing runs until it is clicked.
      handleToolRequest(msg);
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

  async function sendMessage() {
    if (isStreaming) return;
    const text = inputEl.value.trim();
    if (!text) return;

    // Jira shortcut — "send to jira" / "/jira" opens the conclude-session modal
    if (/^\/jira\b|send\s+to\s+jira|log\s+to\s+jira|create\s+jira/i.test(text)) {
      inputEl.value = '';
      if (typeof window.openJiraModal === 'function') window.openJiraModal();
      return;
    }

    // "/diff" asks about what changed on this device since the last visit
    // (#549). The diff is fetched, capped and masked by the server; nothing
    // about it is assembled here.
    if (/^\/diff\s*$/i.test(text)) {
      inputEl.value = '';
      askAboutDiff({});
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
    // Asked before anything is drawn (#556), so declining leaves the
    // question in the box rather than a half-started reply on screen.
    if (!(await _budgetAllows())) return;
    if (!(await _largeRequestAllows(_estimateTokens()))) return;

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

      lastSentPayload = {
        message,
        history,
        // What the engineer pointed at, if anything (#551). Redacted
        // server-side, and cleared once it has gone.
        attachment: takeAttachment(),
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
        mode:              ansibleMode ? 'ansible' : aiMode,
        ...(ansibleMode ? { ansible_canvas: canvasState() } : {}),
      };
      chatWs.send(JSON.stringify(lastSentPayload));
    } else {
      finishStreaming();
      appendErrorBubble('Not connected to server. Reconnecting\u2026');
    }
  }

  // -------------------------------------------------------------------------
  // Native tool use (#560)
  //
  // The model asked to run something. That arrives as a `tool_request`
  // rather than a `[SUGGEST_CMD]` tag, and it renders as the same command
  // block — deliberately, because the person approving should not have to
  // learn a second thing depending on which provider answered.
  //
  // What is different is what happens *after* the click. A tag's command is
  // sent and that is the end of it; the model finds out on the next turn, in
  // prose. A tool call's output goes back as the result of the model's own
  // request, so it continues the exchange it started. That is the whole
  // point of the feature, and it is why this waits for the output rather
  // than firing and forgetting.
  // -------------------------------------------------------------------------

  /** The exchange waiting on somebody's decision, or null. */
  let pendingTool = null;

  /** How long to wait for the device to finish before giving up. */
  const TOOL_OUTPUT_TIMEOUT_MS = 45000;
  const TOOL_POLL_MS = 700;

  function handleToolRequest(msg) {
    pendingTool = {
      shape: msg.shape || 'openai',
      text: msg.text || '',
      calls: msg.calls || [],
      // Read-only calls from the same turn, already answered server-side.
      // They travel back with the approved command's result so the model
      // does not have to ask for them a second time.
      answered: msg.answered || [],
      // The calls those answers belong to. They have to travel with
      // the results or the ids in the exchange match nothing.
      readOnlyCalls: msg.read_only_calls || [],
    };

    const call = pendingTool.calls[0];
    if (!call) { pendingTool = null; return; }

    const command = String((call.arguments || {}).command || '').trim();
    const why = String((call.arguments || {}).why || '').trim();
    if (!command) { pendingTool = null; return; }

    // The live bubble, or the last assistant one: a request arriving
    // after a reconnect has nowhere else to go, and dropping it leaves
    // the model waiting for an answer nobody can give.
    const bubble = streamingBubble
      || messagesEl.querySelector('.chat-bubble-ai:last-of-type');
    if (!bubble) { pendingTool = null; return; }

    if (why) {
      const line = document.createElement('p');
      line.className = 'tool-why';
      line.textContent = why;
      bubble.appendChild(line);
    }

    // Bound to the session the question was asked about, exactly as a
    // suggestion block is (#308, #316) — never to whatever is active
    // when it is clicked.
    const block = buildCommandBlock(
      command, null, bubble.dataset.contextSession || '');
    block.dataset.toolCallId = call.id || '';
    // Marked so the approval knows to send a result back afterwards, and
    // so the block reads as part of an exchange rather than a loose
    // suggestion.
    block.classList.add('cmd-block-tool');

    const decline = document.createElement('button');
    decline.type = 'button';
    decline.className = 'btn-secondary cmd-decline';
    decline.title = 'Tell the assistant you would rather not run this';
    decline.innerHTML = '<span class="material-symbols-outlined">block</span>';
    decline.addEventListener('click', () => declineTool(block));
    const actions = block.querySelector('.cmd-block-actions');
    if (actions) actions.appendChild(decline);

    bubble.appendChild(block);
    wireCommandBlocks(bubble);
    wireToolApproval(block);
    scrollToBottom();
  }

  /**
   * Take over the Send button for a tool block.
   *
   * The ordinary handler injects and stops. This one injects, waits for
   * the device to finish, and sends the output back as the result — so
   * the model continues rather than being told about it next turn.
   */
  function wireToolApproval(block) {
    const send = block.querySelector('.cmd-send');
    const pre = block.querySelector('.cmd-block-text');
    if (!send || !pre) return;

    // Replaced rather than added to: the ordinary listener is already
    // wired, and two handlers would inject the command twice.
    const fresh = send.cloneNode(true);
    send.parentNode.replaceChild(fresh, send);

    fresh.addEventListener('click', async () => {
      const command = pre.textContent.trim();
      const target = block.dataset.targetSession || null;
      fresh.disabled = true;
      block.classList.add('cmd-block-running');

      // The moment of approval, so a previous run of the same command is
      // not mistaken for this one's answer.
      const approvedAt = Date.now() / 1000;
      const sessionId = target
        || (typeof window.getActiveTab === 'function'
            ? (window.getActiveTab() || {}).sessionId : null);

      injectCommand(command, target);
      if (!sessionId) { finishToolBlock(block, 'no session'); return; }

      const output = await waitForOutput(sessionId, command, approvedAt);
      finishToolBlock(block, output === null ? 'timeout' : 'done');
      sendToolResult(command, output);
    });
  }

  /**
   * Poll for the finished record, or null if the device never settled.
   *
   * Reads the records `transcript.py` produces rather than scraping the
   * terminal here — prompt detection is difficult and there is already one
   * implementation of it that is careful.
   */
  async function waitForOutput(sessionId, command, approvedAt) {
    const deadline = Date.now() + TOOL_OUTPUT_TIMEOUT_MS;
    while (Date.now() < deadline) {
      await new Promise(r => setTimeout(r, TOOL_POLL_MS));
      try {
        const params = new URLSearchParams({ command, after: String(approvedAt) });
        const res = await fetch(
          `/api/sessions/${encodeURIComponent(sessionId)}/last-output?${params}`);
        if (!res.ok) break;
        const data = await res.json();
        if (data.ready) return data.output || '';
      } catch (_) {
        break;
      }
    }
    return null;
  }

  function finishToolBlock(block, how) {
    block.classList.remove('cmd-block-running');
    if (how === 'timeout') block.classList.add('cmd-block-timeout');
  }

  /**
   * Send the result back, continuing the model's own exchange.
   *
   * A timeout is reported as one rather than as empty output: "the device
   * did not finish" and "the command printed nothing" are different facts
   * and a model told the second will draw a conclusion from it.
   */
  function sendToolResult(command, output) {
    if (!pendingTool || !chatWs || chatWs.readyState !== WebSocket.OPEN) {
      pendingTool = null;
      return;
    }
    const call = pendingTool.calls[0];
    const results = [...pendingTool.answered];
    results.push(output === null
      ? { id: call.id, is_error: true,
          content: `The engineer approved \`${command}\` but the device had `
                 + 'not finished answering when ShellMate stopped waiting. '
                 + 'The output is on their screen.' }
      : { id: call.id, content: output || '(the command produced no output)' });

    const handoff = {
      shape: pendingTool.shape,
      text: pendingTool.text,
      calls: [...pendingTool.calls, ...pendingTool.readOnlyCalls],
      results,
    };
    pendingTool = null;
    resumeAfterTool(handoff);
  }

  /** Tell the model the engineer would rather not, and let it carry on. */
  function declineTool(block) {
    if (!pendingTool) return;
    const call = pendingTool.calls[0];
    block.classList.add('cmd-block-declined');
    block.querySelectorAll('button').forEach(b => { b.disabled = true; });

    const handoff = {
      shape: pendingTool.shape,
      text: pendingTool.text,
      calls: [...pendingTool.calls, ...pendingTool.readOnlyCalls],
      results: [...pendingTool.answered, {
        id: call.id, is_error: true,
        content: 'The engineer declined to run this. Suggest something else, '
               + 'or explain what you would look for and why.',
      }],
    };
    pendingTool = null;
    resumeAfterTool(handoff);
  }

  /**
   * Ask the model to continue, carrying the exchange.
   *
   * The same question is sent again with the exchange attached, rather than
   * a new one: the model is answering what it was originally asked, having
   * now seen what it asked for.
   */
  function resumeAfterTool(handoff) {
    if (!chatWs || chatWs.readyState !== WebSocket.OPEN || !lastSentPayload) {
      return;
    }
    startStreamingBubble();
    if (streamingBubble && lastSentPayload.session_id) {
      streamingBubble.dataset.contextSession = lastSentPayload.session_id;
    }
    isStreaming = true;
    sendBtn.disabled = true;
    // The same payload as the question that started this, with the
    // exchange attached: the model is answering what it was originally
    // asked, having now seen what it asked for.
    chatWs.send(JSON.stringify({ ...lastSentPayload, tool_result: handoff }));
  }

  // -------------------------------------------------------------------------
  // What the assistant saw (#553)
  //
  // Each reply carries the exact context block the request was built from,
  // and an inspector on the bubble opens it. The point is checkability: the
  // model is told to say when it cannot see enough, and until now "I cannot
  // see that" was an assertion nobody could test. It is also the proof a
  // team lead asks for — that redaction ran, and that the answer rests on
  // what was actually on screen rather than on something invented.
  //
  // The text arrives already redacted, because it is the same string the
  // provider received. Reconstructing it here would show what ShellMate
  // *believes* it sent, which is exactly the thing under question.
  //
  // Capped, because a context block is a few thousand lines and twenty of
  // them held forever is a chat panel that grows until the tab dies.
  // -------------------------------------------------------------------------

  const CONTEXT_KEEP = 20;

  /** Bubbles holding a stored context, oldest first. */
  const contextHolders = [];

  function attachContext(text) {
    const bubble = streamingBubble;
    if (!bubble || !text) return;

    bubble._shellmateContext = text;
    contextHolders.push(bubble);
    // The oldest lose the text but keep the button, which then says why —
    // silently removing the control would read as a feature that comes and
    // goes.
    while (contextHolders.length > CONTEXT_KEEP) {
      const old = contextHolders.shift();
      if (old) old._shellmateContext = null;
    }

    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'context-inspect';
    button.title = 'Show exactly what the assistant was given for this reply';
    button.innerHTML =
      '<span class="material-symbols-outlined">visibility</span> What it saw';
    button.addEventListener('click', () => showContext(bubble));
    bubble.appendChild(button);
  }

  function showContext(bubble) {
    const text = bubble && bubble._shellmateContext;
    if (!text) {
      window.shellmateDialog.alert({
        title: 'No longer kept',
        body: `Only the last ${CONTEXT_KEEP} replies keep their context, so `
            + 'this one has been let go. Newer replies still have theirs.',
      });
      return;
    }

    const overlay = document.getElementById('context-overlay');
    const body = document.getElementById('context-body');
    if (!overlay || !body) return;

    // textContent: this is device output, and a running configuration
    // containing a tag is the ordinary case rather than the attack.
    body.textContent = text;

    const meta = document.getElementById('context-meta');
    if (meta) {
      const lines = text.split('\n').length;
      const masked = (text.match(/\*{4,}/g) || []).length;
      meta.textContent = `${lines.toLocaleString()} lines, `
        + `${text.length.toLocaleString()} characters`
        // Said as a number rather than a reassurance. "Redaction is on" is a
        // claim about a setting; "9 values were masked" is a claim about
        // this request, and it is the one somebody asked for.
        + (masked ? ` · ${masked} value(s) masked before sending`
                  : ' · nothing matched a secret pattern');
    }
    overlay.classList.remove('hidden');
  }

  function initContextInspector() {
    const overlay = document.getElementById('context-overlay');
    if (!overlay) return;
    const close = () => overlay.classList.add('hidden');
    document.getElementById('context-close').addEventListener('click', close);
    overlay.addEventListener('click', (e) => {
      if (e.target === overlay) close();
    });
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && !overlay.classList.contains('hidden')) close();
    });
    const copy = document.getElementById('context-copy');
    if (copy) copy.addEventListener('click', async () => {
      const text = document.getElementById('context-body').textContent;
      try {
        await navigator.clipboard.writeText(text);
      } catch (_) {
        const ta = document.createElement('textarea');
        ta.value = text;
        document.body.appendChild(ta);
        ta.select();
        try { document.execCommand('copy'); } catch (_) { /* give up */ }
        ta.remove();
      }
      if (typeof window._showCopyToast === 'function') window._showCopyToast();
    });
  }

  // -------------------------------------------------------------------------
  // Pointing at something (#551)
  //
  // Mid-outage the engineer wants to say "these six lines", not ask a
  // question over a two-hundred-line window and hope the model picks the
  // right ones out of it. Three ways in — a terminal selection, the last
  // command's output, and a paste into the chat box — and one shape out.
  //
  // The text is carried unredacted to the server and masked there, not
  // here. The browser is where the unmasked text already is; the promise
  // is about what leaves the machine, and keeping the masking at that
  // boundary means one place to be right rather than two.
  // -------------------------------------------------------------------------

  /** What is attached to the next question, or null. */
  let attached = null;

  /** A paste longer than this becomes an attachment rather than a message. */
  const PASTE_AS_ATTACHMENT = 400;

  const ATTACHMENT_LABEL = {
    selection: 'Selected output',
    record: 'Last command',
    paste: 'Pasted text',
  };

  function attach(kind, text, sessionId) {
    const body = String(text || '').trim();
    if (!body) {
      appendErrorBubble('Nothing was selected.');
      return;
    }
    attached = { kind, text: body, sessionId: sessionId || null };
    renderAttachment();
    if (inputEl) inputEl.focus();
  }

  /**
   * The newest finished command on a session, attached.
   *
   * Read from the server's records rather than scraped off the screen:
   * the terminal has no idea where one command ends and the next begins,
   * and `transcript.py` does.
   */
  async function explainLast(sessionId) {
    const sid = sessionId
      || (typeof window.getActiveTab === 'function'
          ? (window.getActiveTab() || {}).sessionId : null);
    if (!sid) { appendErrorBubble('No active session.'); return; }

    try {
      const res = await fetch(
        `/api/sessions/${encodeURIComponent(sid)}/last-output`);
      const data = await res.json();
      if (!data.ready) {
        appendErrorBubble('No finished command has been recorded on this '
                        + 'session yet.');
        return;
      }
      attach('record', `${data.command}\n${data.output}`, sid);
      // Asked straight away: "explain the last command" is a whole
      // instruction, and making somebody type one after choosing it from a
      // menu is asking them to say it twice.
      // Through the input rather than around it, so the question
      // appears in the transcript as the engineer's own.
      if (inputEl) {
        inputEl.value = 'What does this output mean?';
        sendMessage();
      }
    } catch (e) {
      appendErrorBubble(`Could not read the last command: ${e.message || e}`);
    }
  }

  /** The chip above the input, showing what is going with the question. */
  function renderAttachment() {
    const host = document.getElementById('chat-attachment');
    if (!host) return;
    host.innerHTML = '';
    if (!attached) { host.classList.add('hidden'); return; }
    host.classList.remove('hidden');

    const lines = attached.text.split('\n').length;
    const chip = document.createElement('div');
    chip.className = 'chat-attachment-chip';

    const label = document.createElement('span');
    label.className = 'chat-attachment-label';
    label.textContent = `${ATTACHMENT_LABEL[attached.kind] || 'Attached'} · `
      + `${lines} line${lines === 1 ? '' : 's'}`;

    // Collapsed, with the first line as the summary: a pasted running
    // configuration is the ordinary case, and a chip that grows to fill
    // the panel would push the thing being written off the screen.
    const preview = document.createElement('details');
    preview.className = 'chat-attachment-preview';
    const summary = document.createElement('summary');
    summary.textContent = attached.text.split('\n')[0].slice(0, 80);
    const pre = document.createElement('pre');
    pre.textContent = attached.text;
    preview.append(summary, pre);

    const remove = document.createElement('button');
    remove.type = 'button';
    remove.className = 'chat-attachment-remove';
    remove.title = 'Do not send this with the question';
    remove.innerHTML = '<span class="material-symbols-outlined">close</span>';
    remove.addEventListener('click', () => { attached = null; renderAttachment(); });

    chip.append(label, remove);
    host.append(chip, preview);
  }

  /**
   * A long paste into the chat box becomes an attachment.
   *
   * Left as a message, a pasted configuration is a question with two
   * hundred lines of noise in front of it — and the model reads the whole
   * thing as something the engineer wrote. As an attachment it is labelled
   * for what it is: text from somewhere else, possibly another device.
   */
  function handlePaste(event) {
    const text = (event.clipboardData || window.clipboardData)
      ? (event.clipboardData || window.clipboardData).getData('text')
      : '';
    if (!text || text.length < PASTE_AS_ATTACHMENT) return;
    event.preventDefault();
    attach('paste', text, null);
  }

  /** What travels with the next question, and clears after it. */
  function takeAttachment() {
    const out = attached;
    attached = null;
    renderAttachment();
    return out ? { kind: out.kind, text: out.text } : null;
  }

  // -------------------------------------------------------------------------
  // Real tables, and proper Markdown (#554)
  //
  // `formatText` rendered code, bold and line breaks. Everything else the
  // model wrote — headings, lists, and above all tables — arrived as prose
  // with the pipes still in it. Meanwhile `markdown.js` has rendered all of
  // that for the manual since the manual existed, and escapes before it
  // produces any markup, which is what makes it safe to point at model
  // output.
  //
  // The parsed rows are the other half. A 48-port interface table is the
  // example the assistant documentation gives of what models get wrong;
  // read as line-wrapped prose the engineer gets it wrong too. So the model
  // keeps the fixed-width text it reads best and the browser gets columns.
  // -------------------------------------------------------------------------

  /** Tables waiting for the reply they belong to. */
  let pendingTables = [];

  function handleTables(list) {
    pendingTables = Array.isArray(list) ? list : [];
  }

  /**
   * Attach whatever tables arrived with this reply.
   *
   * After the text, because the model's own words are the answer and the
   * rows are the evidence under it — and because a table above the first
   * sentence pushes the answer off the screen on a short panel.
   */
  function attachTables(bubble) {
    if (!bubble || !pendingTables.length) { pendingTables = []; return; }
    pendingTables.forEach(t => bubble.appendChild(buildTable(t)));
    pendingTables = [];
    scrollToBottom();
  }

  function buildTable(spec) {
    const wrap = document.createElement('details');
    wrap.className = 'chat-table';
    wrap.open = true;

    const summary = document.createElement('summary');
    summary.textContent = `${spec.command} — ${spec.rows.length} row`
      + `${spec.rows.length === 1 ? '' : 's'}`
      // Said, because a table quietly showing sixty of four hundred rows
      // is a table somebody will count on being complete.
      + (spec.truncated ? ` of ${spec.total}` : '');
    wrap.appendChild(summary);

    const tools = document.createElement('div');
    tools.className = 'chat-table-tools';

    const filter = document.createElement('input');
    filter.type = 'search';
    filter.className = 'chat-table-filter';
    filter.placeholder = 'Filter rows…';

    const copy = document.createElement('button');
    copy.type = 'button';
    copy.className = 'btn-tertiary';
    copy.textContent = 'Copy CSV';
    copy.addEventListener('click', () => copyCsv(spec));

    tools.append(filter, copy);
    wrap.appendChild(tools);

    const scroller = document.createElement('div');
    scroller.className = 'chat-table-scroll';
    const table = document.createElement('table');

    const head = document.createElement('thead');
    const headRow = document.createElement('tr');
    spec.columns.forEach((name, index) => {
      const th = document.createElement('th');
      th.textContent = name;
      th.title = 'Sort by this column';
      th.addEventListener('click', () => sortBy(table, index, th));
      headRow.appendChild(th);
    });
    head.appendChild(headRow);

    const body = document.createElement('tbody');
    spec.rows.forEach(row => {
      const tr = document.createElement('tr');
      row.forEach(cell => {
        const td = document.createElement('td');
        // textContent throughout: these are device values, and an
        // interface description is the user's text on somebody else's box.
        td.textContent = cell;
        tr.appendChild(td);
      });
      body.appendChild(tr);
    });

    filter.addEventListener('input', () => {
      const needle = filter.value.trim().toLowerCase();
      [...body.rows].forEach(tr => {
        tr.hidden = needle
          ? !tr.textContent.toLowerCase().includes(needle)
          : false;
      });
    });

    table.append(head, body);
    scroller.appendChild(table);
    wrap.appendChild(scroller);
    return wrap;
  }

  /**
   * Sort, alternating direction, numerically where the column is numbers.
   *
   * Interface counters and VLAN ids sorted as text put 10 before 9, which
   * is precisely the wrong answer for the columns anybody sorts.
   */
  function sortBy(table, index, th) {
    const body = table.tBodies[0];
    const rows = [...body.rows];
    const descending = th.dataset.sort === 'asc';

    const numeric = rows.every(r => {
      const v = (r.cells[index].textContent || '').trim();
      return v === '' || !Number.isNaN(Number(v));
    });

    rows.sort((a, b) => {
      const x = (a.cells[index].textContent || '').trim();
      const y = (b.cells[index].textContent || '').trim();
      const result = numeric ? Number(x || 0) - Number(y || 0)
                             : x.localeCompare(y);
      return descending ? -result : result;
    });

    [...table.tHead.rows[0].cells].forEach(c => { delete c.dataset.sort; });
    th.dataset.sort = descending ? 'desc' : 'asc';
    rows.forEach(r => body.appendChild(r));
  }

  async function copyCsv(spec) {
    // Quoted properly: an interface description with a comma in it would
    // otherwise silently become two columns in whatever this is pasted into.
    const escape = (v) => {
      const s = String(v == null ? '' : v);
      return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
    };
    const csv = [spec.columns.map(escape).join(','),
                 ...spec.rows.map(r => r.map(escape).join(','))].join('\n');
    try {
      await navigator.clipboard.writeText(csv);
    } catch (_) {
      const ta = document.createElement('textarea');
      ta.value = csv;
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand('copy'); } catch (_) { /* give up */ }
      ta.remove();
    }
    if (typeof window._showCopyToast === 'function') window._showCopyToast();
  }

  // -------------------------------------------------------------------------
  // What this is costing (#556)
  //
  // Two questions, and they are asked by different people. The lead's is
  // "what does an incident cost"; the engineer's is "did I just ship forty
  // thousand tokens without noticing". Both are answered from counts the
  // provider itself reported — never from an estimate, and never from a
  // price ShellMate guessed.
  //
  // **No price ships as a default.** Published rates go stale, differ by
  // region and by contract, and a wrong number is worse than no number
  // because somebody would plan against it. Zero means the prices are
  // simply not shown.
  //
  // **The budget is per conversation in this browser.** It cannot see what
  // anything else spends against the same key, and clearing the chat starts
  // a new one. Labelled that way, because a budget that sounds like a
  // spending cap and is not one is worse than no budget at all.
  // -------------------------------------------------------------------------

  /** Whether the engineer has been asked about this conversation already. */
  let budgetAcknowledged = false;

  /** A line of money for the meter's tooltip, or "" when no price is set. */
  function _conversationCost() {
    const inRate = Number(A('ai.price_per_million_in', 0)) || 0;
    const outRate = Number(A('ai.price_per_million_out', 0)) || 0;
    if (!inRate && !outRate) return '';
    // Cache reads are input the provider counted separately and usually
    // charges less for. Priced at the input rate rather than at a guessed
    // discount: overstating slightly is honest, and inventing a cache rate
    // ShellMate was never told is not.
    const cost = (totalUsage.input / 1_000_000) * inRate
               + (totalUsage.output / 1_000_000) * outRate;
    return `About ${cost.toFixed(cost < 1 ? 4 : 2)} for this conversation, `
         + 'at the rates you entered.';
  }

  /**
   * Ask once when the budget is passed, and once more if it is doubled.
   *
   * Once, because a dialog on every message after the first overrun is a
   * dialog people click through without reading — at which point the budget
   * has stopped meaning anything. Again at double, because "you are over"
   * and "you are twice over" are different facts.
   */
  async function _budgetAllows() {
    const budget = Number(A('ai.conversation_token_budget', 0)) || 0;
    if (!budget) return true;

    const spent = totalUsage.input + totalUsage.output;
    if (spent < budget) return true;
    if (budgetAcknowledged && spent < budget * 2) return true;

    const over = Math.round((spent / budget) * 100);
    const yes = await window.shellmateDialog.confirm({
      title: 'This conversation is over its budget',
      body: `It has used ${spent.toLocaleString()} tokens against a budget of `
          + `${budget.toLocaleString()} — ${over}%. Clearing the chat starts a `
          + 'new conversation and a fresh budget; carrying on keeps the '
          + 'history, which is what makes each further question cost more '
          + 'than the last.',
      confirmLabel: 'Ask anyway',
    });
    if (yes) budgetAcknowledged = true;
    return yes;
  }

  /**
   * Warn before an unusually large request, once.
   *
   * `/context all` across a dozen busy tabs is the case: nothing about
   * typing a short question suggests it is about to send two hundred
   * thousand tokens, and the bill arrives a month later.
   */
  async function _largeRequestAllows(estimate) {
    const budget = Number(A('ai.conversation_token_budget', 0)) || 0;
    // Half a budget in a single request is the threshold, because a request
    // that size makes the budget unreachable in two more questions. With no
    // budget set there is no threshold and nothing is asked.
    if (!budget || estimate < budget / 2) return true;
    if (budgetAcknowledged) return true;

    const yes = await window.shellmateDialog.confirm({
      title: 'That is a large request',
      body: `This question would send roughly ${estimate.toLocaleString()} `
          + `tokens — about ${Math.round((estimate / budget) * 100)}% of the `
          + 'whole conversation budget in one go. Narrowing the tab selection, '
          + 'or asking about one device, sends far less.',
      confirmLabel: 'Send it',
    });
    if (yes) budgetAcknowledged = true;
    return yes;
  }

  // -----------------------------------------------------------------------
  // Message rendering
  // -----------------------------------------------------------------------

  /**
   * Say the persona changed, once, in the transcript.
   *
   * In the transcript rather than a toast, because it is a fact about the
   * conversation: the replies above it came from somewhere else. Marked so
   * a second entry is not added when somebody moves between Ansible areas.
   */
  function announceMode() {
    if (!messagesEl || messagesEl.querySelector('.chat-mode-note[data-mode="ansible"]')) return;
    const note = document.createElement('div');
    note.className = 'chat-mode-note';
    note.dataset.mode = 'ansible';
    note.textContent = ANSIBLE_WELCOME;
    messagesEl.appendChild(note);
    scrollToBottom(true);
  }

  /**
   * A playbook the assistant offered (#602).
   *
   * Deliberately not a [SUGGEST_CMD] block. Those render as something you
   * click to type into a live device, and pasting forty lines of YAML into
   * a switch would be a genuinely bad afternoon. This goes to the builder,
   * where it is read back task by task before anybody can keep it — which
   * is the same route a draft has always taken, and the reason moving the
   * box into the chat did not move it out from behind that.
   */
  function buildPlaybookBlock(text) {
    const wrap = document.createElement('div');
    wrap.className = 'chat-playbook';

    const head = document.createElement('div');
    head.className = 'chat-playbook-head';
    head.innerHTML = '<span class="material-symbols-outlined">description</span>'
                   + '<strong>Playbook</strong>'
                   + '<span class="chat-playbook-note">a draft — read it before you keep it</span>';
    wrap.appendChild(head);

    const pre = document.createElement('pre');
    pre.className = 'chat-playbook-body';
    pre.textContent = text;
    wrap.appendChild(pre);

    const actions = document.createElement('div');
    actions.className = 'chat-playbook-actions';

    const open = document.createElement('button');
    open.type = 'button';
    open.className = 'btn-primary';
    open.textContent = 'Open in the builder';
    open.addEventListener('click', () => {
      if (window.ansibleBuilder && window.ansibleBuilder.accept) {
        if (window.ansibleView) window.ansibleView.open('builder');
        window.ansibleBuilder.accept(text);
      }
    });
    actions.appendChild(open);

    const copy = document.createElement('button');
    copy.type = 'button';
    copy.className = 'btn-tertiary';
    copy.textContent = 'Copy';
    copy.addEventListener('click', () => {
      navigator.clipboard.writeText(text);
      if (typeof window._showCopyToast === 'function') window._showCopyToast();
    });
    actions.appendChild(copy);

    wrap.appendChild(actions);
    return wrap;
  }

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
        attachTables(streamingBubble);
      }
      streamingBubble = null;
    }
    isStreaming = false;
    sendBtn.disabled = false;
    inputEl.focus();
    scrollToBottom();
    updateContextIndicator();

    // The output of a command approved mid-reply goes next (#489). One at a
    // time: this runs again when that reply finishes.
    const queued = _pendingSilent.shift();
    if (queued) {
      setTimeout(() => sendSilent(queued.message, queued.sessionId, queued.autoAnalysis), 0);
    }
  }

  function sendSilent(message, sessionId, autoAnalysis) {
    if (isStreaming) {
      // Approve a command while the previous answer is still streaming —
      // common in Investigate mode — and its output used to be dropped here
      // with no message. Queued instead; finishStreaming() sends it.
      if (autoAnalysis) _pendingSilent.push({ message, sessionId, autoAnalysis });
      return;
    }
    if (!chatWs || chatWs.readyState !== WebSocket.OPEN) {
      if (autoAnalysis) {
        appendErrorBubble(`The output of "${autoAnalysis.command}" was not analysed: `
          + 'the assistant is not connected.');
      }
      return;
    }
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
      mode:          ansibleMode ? 'ansible' : aiMode,
      ...(ansibleMode ? { ansible_canvas: canvasState() } : {}),
    }));
  }

  /**
   * Ask the assistant about a configuration diff (#549).
   *
   * The diff never travels from here. Only which snapshots to compare goes
   * up — `{old_id, new_id}`, or nothing at all for this session's connect-time
   * drift report — and the server reads them out of its own archive, masks
   * them and writes the question. A prompt built in this file would reach the
   * provider with the running configuration inside it and `outbound` would
   * never see it, which is exactly the shape of bug #489.
   *
   * @param {{oldId?: number, newId?: number, sessionId?: string, label?: string}} spec
   */
  function askAboutDiff(spec) {
    const opts = spec || {};
    _revealPanel();
    if (isStreaming) {
      appendErrorBubble('One question at a time — this one is still being answered.');
      return;
    }
    if (!chatWs || chatWs.readyState !== WebSocket.OPEN) {
      appendErrorBubble('The assistant is not connected.');
      return;
    }
    const activeTab = typeof window.getActiveTab === 'function' ? window.getActiveTab() : null;
    const sid = opts.sessionId || (activeTab ? activeTab.sessionId : null);

    const asked = opts.label
      ? `Explain the configuration changes — ${opts.label}`
      : 'Explain the configuration changes on this device.';
    appendUserBubble(asked);
    if (typeof window.addJiraChatMessage === 'function') {
      window.addJiraChatMessage('user', asked);
    }

    const history = _recentHistory();
    startStreamingBubble();
    if (streamingBubble && sid) streamingBubble.dataset.contextSession = sid;
    isStreaming = true;
    sendBtn.disabled = true;

    const openIds = typeof window.getOpenSessionIds === 'function' ? window.getOpenSessionIds() : [];
    const picked = typeof window.getChatContextSelection === 'function'
      ? window.getChatContextSelection() : null;
    chatWs.send(JSON.stringify({
      message:      '',
      history,
      diff_request: { old_id: opts.oldId || null, new_id: opts.newId || null },
      session_id:   sid,
      open_session_ids: openIds,
      context_session_ids: picked,
      backend:      currentBackend,
      model:        currentModel,
      context_mode: picked ? 'selected' : contextMode,
      mode:         ansibleMode ? 'ansible'
        : (typeof window.getShellmateMode === 'function' ? window.getShellmateMode() : 'tshoot'),
      ...(ansibleMode ? { ansible_canvas: canvasState() } : {}),
    }));
  }

  /**
   * Ask the assistant to review a proposed configuration change (#550).
   *
   * The preview dialog stays open behind this; the answer lands in the chat
   * pane beside it. Only the lines the engineer typed go up — the server
   * re-runs the preview against the stored capture, so the classification and
   * the surrounding stanzas are read, capped and masked where `outbound` can
   * see them. Nothing here reaches the device: reviewing a change must not
   * start a second conversation with a switch at the moment somebody is
   * deciding whether to have the first.
   *
   * @param {{text: string, sessionId?: string, label?: string}} spec
   */
  function askForReview(spec) {
    const opts = spec || {};
    if (!opts.text || !opts.text.trim()) return;
    _revealPanel();
    if (isStreaming) {
      appendErrorBubble('One question at a time — this one is still being answered.');
      return;
    }
    if (!chatWs || chatWs.readyState !== WebSocket.OPEN) {
      appendErrorBubble('The assistant is not connected, so the change cannot be reviewed.');
      return;
    }
    const activeTab = typeof window.getActiveTab === 'function' ? window.getActiveTab() : null;
    const sid = opts.sessionId || (activeTab ? activeTab.sessionId : null);

    const asked = `Review this configuration change before I apply it${opts.label ? ' — ' + opts.label : ''}.`;
    appendUserBubble(asked);
    if (typeof window.addJiraChatMessage === 'function') {
      window.addJiraChatMessage('user', asked);
    }

    const history = _recentHistory();
    startStreamingBubble();
    if (streamingBubble && sid) streamingBubble.dataset.contextSession = sid;
    isStreaming = true;
    sendBtn.disabled = true;

    const openIds = typeof window.getOpenSessionIds === 'function' ? window.getOpenSessionIds() : [];
    const picked = typeof window.getChatContextSelection === 'function'
      ? window.getChatContextSelection() : null;
    chatWs.send(JSON.stringify({
      message:        '',
      history,
      review_request: { text: opts.text },
      session_id:     sid,
      open_session_ids: openIds,
      context_session_ids: picked,
      backend:        currentBackend,
      model:          currentModel,
      context_mode:   picked ? 'selected' : contextMode,
      mode:           typeof window.getShellmateMode === 'function' ? window.getShellmateMode() : 'tshoot',
    }));
  }

  /** Bring the chat pane back if it is hidden — an answer nobody can see is none. */
  function _revealPanel() {
    const ai = (window.shellmateSettings || {}).ai || {};
    if (ai.panel_enabled === false && typeof window.toggleAiPanel === 'function') {
      window.toggleAiPanel();
    }
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
    const parts = raw.split(/(\[SUGGEST_CMD(?::\d+)?\][\s\S]*?\[\/SUGGEST_CMD\]|\[PLAN\][\s\S]*?\[\/PLAN\]|\[PLAYBOOK\][\s\S]*?\[\/PLAYBOOK\])/g);
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
      } else if (/^\[PLAYBOOK\]/.test(part)) {
        bubble.appendChild(buildPlaybookBlock(
          part.replace(/^\[PLAYBOOK\]\s*/, '').replace(/\s*\[\/PLAYBOOK\]$/, '')));
      } else if (part) {
        const textNode = document.createElement('div');
        textNode.className = 'chat-text';
        textNode.innerHTML = formatText(part);
        bubble.appendChild(textNode);
      }
    });
  }

  function formatText(text) {
    // The renderer the manual already uses (#554). It escapes before
    // it produces any markup, which is what makes it safe to point at
    // model output — and it renders headings, lists and tables, which
    // the four replaces below never did. The [SUGGEST_CMD] and [PLAN]
    // splitting happens before this, in renderBubbleContent, so a
    // command block is never handed to a Markdown parser.
    if (window.shellmateMarkdown && window.shellmateMarkdown.render) {
      try {
        return window.shellmateMarkdown.render(text);
      } catch (_) {
        // Fall through: a renderer that threw on one odd reply must
        // not lose the reply.
      }
    }
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
    startOutputWatcher(clean, tab.sessionId);
  }

  /** Where an Investigate-mode run stands: approved commands so far. */
  const _investigation = { steps: 0 };
  const A = (key, fallback) =>
    (window.shellmateAdvanced ? window.shellmateAdvanced(key, fallback) : fallback);
  window.addEventListener('shellmate:mode-changed', () => { _investigation.steps = 0; });

  // -----------------------------------------------------------------------
  // Output watcher — feeds command output back to the AI automatically
  // -----------------------------------------------------------------------

  // No baseline line count any more (#489): it was read from the tab
  // projection, which never carries getBufferLines, so it was always 0 —
  // and nothing in here ever used it. The watcher collects from the event
  // stream, which needs no baseline.
  function startOutputWatcher(cmd, sessionId) {
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
  /** The plays on the builder's canvas, if it has any. */
  function canvasState() {
    return (window.ansibleBuilder && window.ansibleBuilder.canvasState)
      ? window.ansibleBuilder.canvasState() : null;
  }

  /**
   * Turn Ansible mode on or off, and show that it happened.
   *
   * Everything visible changes together — the pill, the greeting, the quick
   * chats — because a persona that answers differently while looking
   * identical is a trap.
   */
  function setAnsibleMode(on) {
    if (ansibleMode === !!on) return;
    ansibleMode = !!on;
    document.body.classList.toggle('chat-ansible-mode', ansibleMode);
    renderQuickButtons();
    updateContextIndicator();
    paintWelcome();
    lockTerminalControls(ansibleMode);
    if (ansibleMode) announceMode();
  }

  /**
   * The two controls that mean nothing in Ansible mode.
   *
   * Troubleshoot / Learn / Investigate are personas for reading a terminal,
   * and this persona is not one of them — leaving the toggle live would let
   * somebody pick Learn and get no change at all, which reads as a broken
   * button rather than as an inapplicable one. The session picker is the
   * same: the Ansible persona is answering about the integration, not about
   * a chosen set of tabs.
   *
   * Disabled with a reason rather than hidden. A control that vanishes and
   * comes back is harder to trust than one that says why it is unavailable.
   */
  function lockTerminalControls(locked) {
    [['mode-toggle-btn', 'The persona is set by the Ansible view while it is open.'],
     ['chat-tabs-btn', 'Ansible mode answers about the integration rather than '
                     + 'a chosen set of sessions.']].forEach(([id, why]) => {
      const el = document.getElementById(id);
      if (!el) return;
      el.disabled = locked;
      el.classList.toggle('chat-pill-locked', locked);
      if (locked) {
        if (!el.dataset.titleWas) el.dataset.titleWas = el.title || '';
        el.title = why;
      } else if (el.dataset.titleWas !== undefined) {
        el.title = el.dataset.titleWas;
        delete el.dataset.titleWas;
      }
    });
  }

  /** The empty transcript says what this assistant is for, here. */
  function paintWelcome() {
    const welcome = document.querySelector('#chat-messages .chat-welcome');
    if (!welcome) return;
    const lead = welcome.querySelector('p:not(.chat-welcome-hint)');
    const hint = welcome.querySelector('.chat-welcome-hint');
    if (!lead || !hint) return;
    if (!welcome.dataset.leadWas) {
      welcome.dataset.leadWas = lead.textContent;
      welcome.dataset.hintWas = hint.textContent;
    }
    if (ansibleMode) {
      lead.textContent = 'Ask about your automation.';
      hint.textContent = 'I know how ShellMate drives your runner — the '
        + 'inventory it builds from your estate, how a playbook reaches the '
        + 'container, and where credentials come from. Ask for a playbook and '
        + 'you can send it straight to the builder.';
    } else {
      lead.textContent = welcome.dataset.leadWas;
      hint.textContent = welcome.dataset.hintWas;
    }
  }

  /** The view decides, and gives the assistant back when it closes. */
  function watchAnsibleView() {
    document.addEventListener('shellmate:ansible-open', () => setAnsibleMode(true));
    document.addEventListener('shellmate:ansible-close', () => setAnsibleMode(false));
  }

  function loadQuickButtons() {
    // Ansible mode brings its own, rather than offering "What's wrong
    // here?" against a playbook. The curated set is untouched and comes
    // back when the view closes.
    if (ansibleMode) return [...ANSIBLE_QUICK_BTNS];
    const stored = window.shellmatePrefs
      ? window.shellmatePrefs.get('quick_buttons', null) : null;
    return (Array.isArray(stored) && stored.length)
      ? stored : [...DEFAULT_QUICK_BTNS];
  }

  function saveQuickButtons(btns) {
    // Never while Ansible mode is showing its own set: saving would write
    // the borrowed buttons over the ones somebody curated (#602).
    if (ansibleMode) return;
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
      btn.title = ansibleMode
        ? 'Click to use. These are Ansible mode’s own; your usual ones '
          + 'come back when the view closes.'
        : 'Click to use · Right-click to edit';

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
    // `input` is the uncached prompt on every provider (#499); what the
    // request contained is that plus what the cache served or stored.
    totalUsage.input      += lastUsage.input + lastUsage.cache_read + lastUsage.cache_write;
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
    if (contextIndicator && ansibleMode) {
      // Which persona is answering matters more than which sessions it can
      // see, when the persona is not the usual one (#602).
      contextIndicator.textContent = 'Ansible mode';
      contextIndicator.title = 'Answering as a ShellMate-Ansible expert while '
        + 'the Ansible view is open. Close it to go back to the usual '
        + 'assistant.';
      contextIndicator.classList.add('chat-pill-ansible');
    } else if (contextIndicator) {
      contextIndicator.classList.remove('chat-pill-ansible');
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
      ? lastUsage.input + lastUsage.cache_read + lastUsage.cache_write : 0;
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

    // The conversation's own budget (#556), which is a different
    // question from the context window: one asks whether this request
    // fits, the other what the whole conversation has cost.
    const budget = Number(A('ai.conversation_token_budget', 0)) || 0;
    const spent = totalUsage.input + totalUsage.output;
    let budgetPct = 0;
    if (budget > 0) {
      budgetPct = Math.round((spent / budget) * 100);
      statusEl.textContent += ` · ${budgetPct}% of budget`;
      title += `\nBudget: ${spent.toLocaleString()} of `
        + `${budget.toLocaleString()} tokens this conversation `
        + '(cleared with the chat).';
    }

    const money = _conversationCost();
    if (money) title += `\n${money}`;

    statusEl.title = title;

    // Whichever is worse. A conversation inside its context window
    // but past its budget is not green, and the reverse is equally
    // true — the meter has one colour and two things to say with it.
    const worst = Math.max(pct, budgetPct);
    statusEl.className = worst < 25 ? 'ctx-green'
                       : worst < 65 ? 'ctx-amber'
                       :             'ctx-red';
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
    // A cleared chat is a fresh start; output waiting for the old
    // conversation would be analysed with no conversation to belong to.
    _pendingSilent.length = 0;
    _resetUsage();
    // A fresh conversation is a fresh budget, and a fresh chance to
    // be asked about it (#556).
    budgetAcknowledged = false;
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
  // Exposed so the block renderer can be exercised without a provider: the
  // shapes it produces are a contract — a playbook must never come out as a
  // command block, which is clicked to type into a live device (#602).
  // The entry point the socket uses, exported (#560). A tool request has
  // to be deliverable without a live provider to test the approval gate
  // at all, and restoring a saved conversation will replay through here.
  initContextInspector();
  if (inputEl) inputEl.addEventListener('paste', handlePaste);

  window.shellmateChatMessage = handleWsMessage;

  window.shellmateChat = {
    attach,
    explainLast,
    renderRaw: (bubble) => { renderBubbleContent(bubble); wireCommandBlocks(bubble); },
    ansibleMode: () => ansibleMode,
  };
  window._chatInjectCommand  = injectCommand;
  window._chatSend           = sendMessage;
  // Used by the diff window's Explain button and the config-push review
  // (#549, #550). Both send identifiers, never device configuration.
  window.shellmateAskAboutDiff = askAboutDiff;
  window.shellmateAskForReview = askForReview;
  window._chatSetBackend     = (val) => {
    if (backendSelect) backendSelect.value = val;
    const idx = val.indexOf(':');
    currentBackend = idx === -1 ? val : val.slice(0, idx);
    currentModel   = idx === -1 ? val : val.slice(idx + 1);
  };

})();
