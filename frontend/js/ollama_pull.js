/**
 * ollama_pull.js — Getting a local model without leaving the app (#555).
 *
 * The privacy story depends on Ollama, and until now a first-time user had
 * to go and find a shell, work out the command, and come back — and never
 * learned why answers about the top of the buffer had been wrong.
 *
 * **Polled, not streamed.** A pull is several gigabytes and outlives any
 * one request; it has to survive this panel being closed, the Settings
 * page being navigated away from, and the browser being reloaded. So the
 * server holds the state and this asks. That is the same shape the updater
 * uses, for the same reason.
 *
 * **The progress bar is one layer, not the model.** Ollama reports
 * `completed`/`total` per layer and emits trailing status-only lines, so a
 * bar driven naively runs to the end and then jumps back. The label says
 * which layer, and the bar is honest about being a layer.
 */
(function () {
  'use strict';

  let timer = null;

  function el(id) { return document.getElementById(id); }

  function init() {
    const start = el('ollama-pull-start');
    if (!start) return;

    start.addEventListener('click', beginPull);
    el('ollama-pull-cancel').addEventListener('click', cancelPull);

    // Ask once on load: a pull started before a reload is still running,
    // and a panel that showed nothing would invite somebody to start a
    // second one.
    refresh();
  }

  async function refresh() {
    let data;
    try {
      const res = await fetch('/api/ollama/pull');
      data = await res.json();
    } catch (_) {
      return;
    }

    fillSuggestions(data.recommended || []);
    paint(data);

    // Poll only while something is happening. A timer that runs forever on
    // a Settings page nobody has open is a request every second for the
    // life of the session.
    if (data.phase === 'pulling') {
      if (!timer) timer = setInterval(refresh, 1000);
    } else if (timer) {
      clearInterval(timer);
      timer = null;
    }
  }

  function fillSuggestions(recommended) {
    const list = el('ollama-recommended');
    if (!list || list.dataset.filled) return;
    list.dataset.filled = '1';
    recommended.forEach(model => {
      const option = document.createElement('option');
      option.value = model.name;
      // The reason, not just the name: "qwen2.5:14b" tells somebody
      // choosing their first local model nothing at all.
      option.label = `${model.why}${model.size ? ` (${model.size})` : ''}`;
      list.appendChild(option);
    });
  }

  function paint(data) {
    const progress = el('ollama-pull-progress');
    const bar = el('ollama-pull-bar').firstElementChild;
    const status = el('ollama-pull-status');
    const hint = el('ollama-pull-hint');
    const start = el('ollama-pull-start');
    const cancel = el('ollama-pull-cancel');

    const pulling = data.phase === 'pulling';
    progress.classList.toggle('hidden', !pulling);
    cancel.classList.toggle('hidden', !pulling);
    start.disabled = pulling;

    if (pulling) {
      const pct = data.total
        ? Math.min(100, Math.round((data.received / data.total) * 100)) : 0;
      bar.style.width = `${pct}%`;
      status.textContent = data.total
        ? `${data.status || 'downloading'} — ${pct}% of this layer `
          + `(${mb(data.received)} of ${mb(data.total)})`
        : (data.status || 'starting…');
      hint.textContent = `Pulling ${data.model}. You can leave this page; `
        + 'it carries on.';
      hint.className = 'settings-section-hint';
      return;
    }

    if (data.phase === 'done') {
      hint.textContent = `${data.model} is downloaded and will appear in the `
        + 'model picker.';
      hint.className = 'settings-section-hint ollama-pull-ok';
      if (typeof window.refreshProviderModels === 'function') {
        window.refreshProviderModels();
      }
    } else if (data.phase === 'failed') {
      hint.textContent = data.error || 'The pull failed.';
      hint.className = 'settings-section-hint ollama-pull-bad';
    } else {
      hint.textContent = '';
      hint.className = 'settings-section-hint';
    }
  }

  function mb(bytes) {
    const n = Number(bytes) || 0;
    return n >= 1024 * 1024 * 1024
      ? `${(n / 1024 / 1024 / 1024).toFixed(1)} GB`
      : `${Math.round(n / 1024 / 1024)} MB`;
  }

  async function beginPull() {
    const model = (el('setting-ollama-model').value || '').trim();
    if (!model) {
      el('ollama-pull-hint').textContent = 'Name a model to pull.';
      return;
    }
    try {
      const res = await fetch('/api/ollama/pull', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ model }),
      });
      const data = await res.json();
      if (!res.ok) {
        el('ollama-pull-hint').textContent = data.detail || `HTTP ${res.status}`;
        el('ollama-pull-hint').className = 'settings-section-hint ollama-pull-bad';
        return;
      }
      paint(data);
      if (!timer) timer = setInterval(refresh, 1000);
    } catch (e) {
      el('ollama-pull-hint').textContent = String(e.message || e);
    }
  }

  async function cancelPull() {
    try {
      await fetch('/api/ollama/pull', { method: 'DELETE' });
    } catch (_) { /* the next poll will say what happened */ }
    refresh();
  }

  document.addEventListener('DOMContentLoaded', init);
  window.shellmateOllamaPull = { refresh };
})();
