/**
 * prompts_editor.js — Reading and changing what the assistant is told.
 *
 * The two personas were constants in the source: invisible in the application
 * and unchangeable without a rebuild, despite being the largest single
 * influence on what the assistant says. This shows them and lets them be
 * edited, the same way Platform Definitions edits `platforms.json`.
 *
 * The one thing it guards is the `{command_rules}` marker. Those rules are
 * what make [SUGGEST_CMD] blocks render as clickable commands rather than as
 * literal tags in the reply. They are not editable — only their *position*
 * is — and deleting the marker is called out rather than left to be
 * discovered later when suggestions quietly stop appearing.
 */
(function () {
  'use strict';

  let select, body, status, modified, markerHint;
  /** Mode -> {body, default, modified, has_marker}, as last loaded. */
  let state = {};
  let marker = '{command_rules}';

  document.addEventListener('DOMContentLoaded', () => {
    select     = document.getElementById('prompt-mode-select');
    body       = document.getElementById('prompt-body');
    status     = document.getElementById('prompt-status');
    modified   = document.getElementById('prompt-modified');
    markerHint = document.getElementById('prompt-marker-hint');
    if (!select || !body) return;

    select.addEventListener('change', show);
    body.addEventListener('input', () => {
      describeMarker();
      report('');
    });

    document.getElementById('prompt-save')
      .addEventListener('click', save);
    document.getElementById('prompt-reset')
      .addEventListener('click', () => reset(select.value));
    document.getElementById('prompt-reset-all')
      .addEventListener('click', () => reset(null));

    load();
  });

  async function load() {
    try {
      const res = await fetch('/api/prompts');
      if (!res.ok) throw new Error(`server returned ${res.status}`);
      apply(await res.json());
    } catch (e) {
      report(`Could not load the prompts: ${e.message}`, true);
    }
  }

  function apply(data) {
    state  = data.prompts || {};
    marker = data.marker || marker;
    show();
  }

  function show() {
    const entry = state[select.value];
    if (!entry) return;
    body.value = entry.body || '';
    // Marked rather than merely stored: a prompt changed eight months ago
    // should be visibly not the shipped one.
    modified.textContent = entry.modified ? 'edited' : 'as shipped';
    modified.classList.toggle('setting-value-modified', !!entry.modified);
    describeMarker();
    report('');
  }

  /**
   * Say what the marker does, and what its absence costs.
   *
   * Stated while editing rather than after saving, because by then the
   * consequence is a feature that has silently stopped working.
   */
  function describeMarker() {
    if (!markerHint) return;
    if (body.value.includes(marker)) {
      markerHint.textContent =
        `${marker} is replaced with the rules that make suggested commands ` +
        `clickable. Move it wherever it reads best.`;
      markerHint.classList.remove('field-warn');
    } else {
      markerHint.textContent =
        `${marker} is missing. The command rules will be added at the end ` +
        `instead, so suggestions keep working — but they read better where ` +
        `you put them.`;
      markerHint.classList.add('field-warn');
    }
  }

  async function save() {
    try {
      const res = await fetch(`/api/prompts/${encodeURIComponent(select.value)}`, {
        method:  'PUT',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ body: body.value }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || `server returned ${res.status}`);
      apply(data);
      report('Saved. It applies to your next message — no reconnection needed.');
    } catch (e) {
      report(e.message, true);
    }
  }

  async function reset(mode) {
    // 'every prompt', not 'both': there were two when this was written
    // and there are five now (#552). A count in a sentence is a thing
    // that goes quietly wrong every time one is added.
    const which = mode ? 'this prompt' : 'every prompt';
    const ok = await window.shellmateDialog.confirm({
      title: `Restore ${which} to the shipped text?`,
      body: 'Anything you have written is discarded.',
      confirmLabel: 'Reset',
      danger: true,
    });
    if (!ok) return;

    try {
      const query = mode ? `?mode=${encodeURIComponent(mode)}` : '';
      const res = await fetch(`/api/prompts/reset${query}`, { method: 'POST' });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || `server returned ${res.status}`);
      apply(data);
      report(`Restored ${which}.`);
    } catch (e) {
      report(e.message, true);
    }
  }

  function report(text, isError) {
    if (!status) return;
    status.textContent = text || '';
    status.classList.toggle('field-warn', !!isError);
  }

  window.reloadPrompts = load;
})();
