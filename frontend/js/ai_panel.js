/**
 * ai_panel.js — Provider testing, model discovery, and hiding the AI panel.
 *
 * Adding an API key used to give no feedback: you saved it and found out
 * whether it was right the next time you asked a question and got an error in
 * the chat pane. And the model list was hardcoded, so pulling a new model into
 * Ollama or gaining access to a new Claude model meant editing the HTML.
 *
 * Both are answered by asking the providers themselves — listing models needs
 * a valid key, costs nothing, and doubles as the connection test.
 */
(function () {
  'use strict';

  /** Friendly names, so results read as prose rather than config keys. */
  const LABELS = {
    anthropic: 'Anthropic',
    openai:    'OpenAI',
    xai:       'xAI',
    deepseek:  'DeepSeek',
    ollama:    'Ollama (local)',
  };

  /** Which chat backend each provider maps to in the model picker. */
  const BACKEND = {
    anthropic: 'claude',
    openai:    'openai',
    xai:       'xai',
    deepseek:  'deepseek',
    ollama:    'ollama',
  };

  document.addEventListener('DOMContentLoaded', () => {
    const test = document.getElementById('btn-test-providers');
    if (test) test.addEventListener('click', testProviders);

    const refresh = document.getElementById('btn-refresh-models');
    if (refresh) refresh.addEventListener('click', testProviders);

    // The picker starts as whatever index.html was written with, which goes
    // stale the day after it is written. The last successful discovery is
    // served back on load, so the hardcoded list is only ever a first-run
    // fallback.
    restoreCachedModels();

    const toggle = document.getElementById('btn-ai-toggle');
    if (toggle) {
      toggle.addEventListener('click', (e) => { e.preventDefault(); togglePanel(); });
    }

    applyPanelVisibility();
    window.addEventListener('shellmate:settings-changed', applyPanelVisibility);
    window.addEventListener('shellmate:settings-loaded',  applyPanelVisibility);
  });

  // -------------------------------------------------------------------------
  // #15, #44  Showing and hiding the assistant
  // -------------------------------------------------------------------------

  function panelEnabled() {
    // Defaults to off: a fresh install should not open with a third of the
    // window given to a pane that cannot answer anything yet.
    return ((window.shellmateSettings || {}).ai || {}).panel_enabled === true;
  }

  function applyPanelVisibility() {
    const enabled = panelEnabled();

    const pane    = document.getElementById('chat-pane');
    const divider = document.getElementById('split-divider');
    if (!pane || !divider) return;

    pane.classList.toggle('hidden', !enabled);
    divider.classList.toggle('hidden', !enabled);

    const toggle = document.getElementById('btn-ai-toggle');
    if (toggle) {
      toggle.classList.toggle('active', enabled);
      toggle.title = enabled ? 'Hide the AI assistant' : 'Show the AI assistant';
    }

    // The terminal has to be told its size changed, or xterm keeps rendering
    // at the old width and the extra space stays blank.
    setTimeout(() => window.dispatchEvent(new Event('resize')), 60);
  }

  /**
   * Turn the assistant on or off from the sidebar.
   *
   * Saved rather than held in the page, so the answer survives a reload — and
   * because it is the same setting the Settings panel edits, not a second one
   * that could disagree with it.
   */
  async function togglePanel() {
    const wanted = !panelEnabled();

    // Applied optimistically: the pane should move when the icon is clicked,
    // not a round trip later.
    window.shellmateSettings = window.shellmateSettings || {};
    window.shellmateSettings.ai = { ...(window.shellmateSettings.ai || {}),
                                    panel_enabled: wanted };
    applyPanelVisibility();

    try {
      const res = await fetch('/api/settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ settings: { ai: { panel_enabled: wanted } } }),
      });
      if (res.ok && typeof window.reloadSettings === 'function') {
        // Re-read rather than patching the global: settings.js keeps its own
        // copy for the form, and a stale one there would quietly undo this
        // the next time someone pressed Save.
        await window.reloadSettings();
        applyPanelVisibility();
      }
    } catch (e) {
      console.warn('Could not save the assistant panel setting:', e);
    }
  }

  // -------------------------------------------------------------------------
  // #14  Testing providers and refreshing models
  // -------------------------------------------------------------------------

  async function testProviders() {
    const status  = document.getElementById('provider-test-status');
    const results = document.getElementById('provider-results');
    // Two ways in — the Settings button and the refresh beside the picker —
    // and both go quiet while a test runs, whichever one was pressed.
    const buttons = [
      document.getElementById('btn-test-providers'),
      document.getElementById('btn-refresh-models'),
    ].filter(Boolean);
    if (!results) return;

    buttons.forEach(b => { b.disabled = true; });
    status.textContent = 'Testing…';
    results.innerHTML = '';

    const done = () => buttons.forEach(b => { b.disabled = false; });

    let data;
    try {
      const res = await fetch('/api/providers/models');
      if (!res.ok) throw new Error(`server returned ${res.status}`);
      data = await res.json();
    } catch (e) {
      status.textContent = '';
      renderRow(results, 'error', 'Could not reach ShellMate’s own API.', String(e.message || e));
      done();
      return;
    }

    const names = Object.keys(data);
    if (!names.length) {
      status.textContent = '';
      renderRow(results, 'muted', 'No providers configured.',
                'Add an API key above, or start Ollama locally.');
      done();
      return;
    }

    let working = 0;
    names.forEach(name => {
      const result = data[name];
      if (result.ok) working += 1;
      renderRow(results, result.ok ? 'ok' : 'error',
                LABELS[name] || name, result.message);
    });

    status.textContent = `${working} of ${names.length} working.`;
    populateModelPicker(data);
    done();
  }

  /**
   * Fill the picker from the last successful discovery, on page load.
   *
   * No network beyond our own API: this must not slow the page down or fail
   * when offline. If nothing has ever been discovered, the hardcoded HTML
   * list stays — a first run has nothing better to offer.
   */
  async function restoreCachedModels() {
    let data;
    try {
      const res = await fetch('/api/providers/cached');
      if (!res.ok) return;
      data = await res.json();
    } catch (e) {
      return;
    }
    const usable = Object.values(data || {})
      .some(r => r && r.ok && r.models && r.models.length);
    if (usable) populateModelPicker(data);
  }

  function renderRow(host, kind, title, detail) {
    const row = document.createElement('div');
    row.className = `provider-row provider-${kind}`;

    const icon = document.createElement('span');
    icon.className = 'material-symbols-outlined';
    icon.textContent = kind === 'ok' ? 'check_circle' : kind === 'error' ? 'close' : 'help';

    const text = document.createElement('div');
    const strong = document.createElement('div');
    strong.className = 'provider-name';
    strong.textContent = title;
    const small = document.createElement('div');
    small.className = 'provider-detail';
    // textContent — provider error messages are not ours to trust as markup.
    small.textContent = detail || '';
    text.appendChild(strong);
    text.appendChild(small);

    row.appendChild(icon);
    row.appendChild(text);
    host.appendChild(row);
  }

  /**
   * Rebuild the chat model picker from what the providers actually offer.
   *
   * The current selection is preserved when it still exists, so refreshing
   * does not silently move someone onto a different model mid-conversation.
   */
  function populateModelPicker(data) {
    const select = document.getElementById('ai-backend-select');
    if (!select) return;

    const previous = select.value;
    select.innerHTML = '';

    const cloud = document.createElement('optgroup');
    cloud.label = '☁ Cloud';
    const local = document.createElement('optgroup');
    local.label = '⚡ Local';
    // chat.js refreshes the local list live from Ollama and finds the group
    // by this id — a rebuild must not take the anchor away.
    local.id = 'local-models-group';

    Object.keys(data).forEach(name => {
      const result = data[name];
      if (!result.ok || !result.models.length) return;
      const group = name === 'ollama' ? local : cloud;
      result.models.forEach(model => {
        const opt = document.createElement('option');
        opt.value = `${BACKEND[name] || name}:${model.id}`;
        opt.textContent = model.label || model.id;
        group.appendChild(opt);
      });
    });

    if (!cloud.children.length && !local.children.length) {
      const opt = document.createElement('option');
      opt.value = '';
      opt.textContent = 'No models available';
      opt.disabled = true;
      select.appendChild(opt);
      return;
    }

    if (cloud.children.length) select.appendChild(cloud);
    // The local group is kept even when empty: chat.js fills it live from
    // Ollama, and dropping it here would leave that refresh nowhere to land.
    if (!local.children.length) {
      const opt = document.createElement('option');
      opt.value = '_none';
      opt.disabled = true;
      opt.textContent = 'None found';
      local.appendChild(opt);
    }
    select.appendChild(local);

    if ([...select.options].some(o => o.value === previous)) {
      select.value = previous;
    }
    select.dispatchEvent(new Event('change'));
    // The list has just been rebuilt from what the providers actually offer,
    // so the saved default may only now have become selectable.
    window.dispatchEvent(new Event('shellmate:models-refreshed'));
  }

  window.refreshProviderModels = testProviders;
  window.toggleAiPanel = togglePanel;
})();
