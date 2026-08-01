/**
 * stockton.js — The advanced settings, rendered from the registry.
 *
 * Every row here is built from what `backend/advanced.py` declares, and that
 * declaration *is* the default the code reads. Sixty hand-written rows against
 * sixty constants would drift — silently, because the label would go on
 * describing something the code no longer does.
 *
 * Two things the panel owes the person opening it:
 *
 * **Saying what this is.** Not a scare — a signpost. Every value has a sane
 * default, nothing here can stop ShellMate starting, and the way back is on
 * the same screen.
 *
 * **Showing what has been changed.** A setting altered eight months ago should
 * be visibly not standard, with its default beside it, or the panel becomes a
 * place where things quietly differ from everyone else's.
 */
(function () {
  'use strict';

  let overlay, bodyEl, searchEl, statusEl;
  let registry = { settings: [], categories: {}, not_exposed: [] };

  document.addEventListener('DOMContentLoaded', () => {
    overlay = document.getElementById('stockton-overlay');
    if (!overlay) return;

    bodyEl   = document.getElementById('stockton-body');
    searchEl = document.getElementById('stockton-search');
    statusEl = document.getElementById('stockton-status');

    const link = document.getElementById('sidebar-link-stockton');
    if (link) link.addEventListener('click', (e) => { e.preventDefault(); open(); });

    document.getElementById('stockton-close').addEventListener('click', close);
    overlay.addEventListener('click', (e) => { if (e.target === overlay) close(); });
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && !overlay.classList.contains('hidden')) close();
    });

    searchEl.addEventListener('input', render);
    document.getElementById('stockton-reset-all')
      .addEventListener('click', () => reset({}));
  });

  async function open() {
    overlay.classList.remove('hidden');
    await load();
  }

  function close() { overlay.classList.add('hidden'); }

  async function load() {
    try {
      const res = await fetch('/api/advanced');
      registry = await res.json();
      render();
    } catch (e) {
      report('Could not read the advanced settings: ' + e.message, true);
    }
  }

  function render() {
    const query = (searchEl.value || '').trim().toLowerCase();
    bodyEl.innerHTML = '';

    const matches = registry.settings.filter(s =>
      !query ||
      s.label.toLowerCase().includes(query) ||
      s.key.toLowerCase().includes(query) ||
      (s.summary || '').toLowerCase().includes(query) ||
      (s.tip || '').toLowerCase().includes(query));

    const changed = registry.settings.filter(s => s.modified).length;
    const count = document.getElementById('stockton-count');
    if (count) {
      count.textContent = changed
        ? `${changed} of ${registry.settings.length} changed from the default`
        : `${registry.settings.length} settings, all at their defaults`;
    }

    if (!matches.length) {
      const empty = document.createElement('div');
      empty.className = 'broadcast-empty';
      empty.textContent = 'Nothing matches.';
      bodyEl.appendChild(empty);
      return;
    }

    Object.keys(registry.categories).forEach(category => {
      const items = matches.filter(s => s.category === category);
      if (!items.length) return;

      const group = document.createElement('details');
      group.className = 'stockton-group';
      group.open = !!query || items.some(s => s.modified);

      const summary = document.createElement('summary');
      summary.className = 'snippet-group-head';

      const title = document.createElement('span');
      title.textContent = registry.categories[category];

      const reset = document.createElement('button');
      reset.type = 'button';
      reset.className = 'btn-tertiary stockton-reset';
      reset.textContent = 'Reset these';
      reset.addEventListener('click', (e) => {
        e.preventDefault();
        e.stopPropagation();
        resetCategory(category, registry.categories[category]);
      });

      summary.append(title, reset);
      group.appendChild(summary);

      items.forEach(s => group.appendChild(row(s)));
      bodyEl.appendChild(group);
    });

    bodyEl.appendChild(notExposed());
  }

  function row(setting) {
    const el = document.createElement('div');
    el.className = 'stockton-row' + (setting.modified ? ' stockton-modified' : '');

    const label = document.createElement('label');
    label.className = 'stockton-label';
    label.htmlFor = fieldId(setting);
    label.textContent = setting.label;
    // The tooltip carries the trade-off, which is the only thing a label
    // cannot: what you give up by changing it.
    if (setting.tip) label.setAttribute('data-tip', setting.tip);

    const text = document.createElement('div');
    text.className = 'stockton-text';

    const summary = document.createElement('div');
    summary.className = 'stockton-summary';
    summary.textContent = setting.summary;

    text.append(label, summary);

    const meta = document.createElement('div');
    meta.className = 'stockton-meta';
    meta.textContent = describeDefault(setting);
    text.appendChild(meta);

    if (setting.restart) {
      const tag = document.createElement('span');
      tag.className = 'snippet-tag stockton-restart';
      tag.textContent = 'needs a restart';
      meta.appendChild(tag);
    }

    el.append(text, control(setting));
    return el;
  }

  function describeDefault(setting) {
    const bits = [`default ${format(setting.default)}${setting.unit ? ' ' + setting.unit : ''}`];
    if (setting.min !== null && setting.max !== null) {
      bits.push(`${format(setting.min)}–${format(setting.max)}`);
    }
    bits.push(setting.key);
    return bits.join('  ·  ');
  }

  function format(value) {
    if (value === true) return 'on';
    if (value === false) return 'off';
    if (value === '') return 'blank';
    return String(value);
  }

  function fieldId(setting) { return 'stockton-' + setting.key.replace(/\./g, '-'); }

  function control(setting) {
    const wrap = document.createElement('div');
    wrap.className = 'stockton-control';

    let field;
    if (setting.kind === 'bool') {
      field = document.createElement('input');
      field.type = 'checkbox';
      field.checked = !!setting.value;
      field.addEventListener('change', () => save(setting, field.checked));
    } else if (setting.kind === 'choice') {
      field = document.createElement('select');
      field.className = 'setting-input';
      setting.choices.forEach(choice => {
        const opt = document.createElement('option');
        opt.value = choice;
        opt.textContent = choice;
        field.appendChild(opt);
      });
      field.value = setting.value;
      field.addEventListener('change', () => save(setting, field.value));
    } else if (setting.kind === 'text') {
      field = document.createElement('input');
      field.type = 'text';
      field.className = 'setting-input';
      field.value = setting.value;
      field.addEventListener('change', () => save(setting, field.value));
    } else {
      field = document.createElement('input');
      field.type = 'number';
      field.className = 'setting-input setting-input-sm';
      if (setting.min !== null) field.min = setting.min;
      if (setting.max !== null) field.max = setting.max;
      if (setting.kind === 'float') field.step = '0.1';
      field.value = setting.value;
      field.addEventListener('change', () => save(setting, field.value));
    }

    field.id = fieldId(setting);
    wrap.appendChild(field);

    if (setting.unit) {
      const unit = document.createElement('span');
      unit.className = 'setting-unit';
      unit.textContent = setting.unit;
      wrap.appendChild(unit);
    }

    // Only on a row that has been changed — a reset button beside a value
    // already at its default is a button that does nothing.
    if (setting.modified) {
      const undo = document.createElement('button');
      undo.type = 'button';
      undo.className = 'btn-tertiary';
      undo.textContent = 'Reset';
      undo.title = `Back to ${format(setting.default)}`;
      undo.addEventListener('click', () => reset({ key: setting.key }));
      wrap.appendChild(undo);
    }

    return wrap;
  }

  async function save(setting, value) {
    try {
      const res = await fetch('/api/advanced', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ values: { [setting.key]: value } }),
      });
      registry = await res.json();

      // Re-read rather than trust what was typed: the backend clamps, and a
      // field still showing a rejected value would be a quiet lie.
      const stored = registry.settings.find(s => s.key === setting.key);
      report(stored && String(stored.value) !== String(value)
        ? `${setting.label} was adjusted to ${format(stored.value)} — its range is ` +
          `${format(setting.min)} to ${format(setting.max)}.`
        : `${setting.label} saved.` + (setting.restart ? ' Takes effect on restart.' : ''));
      render();
    } catch (e) {
      report(e.message, true);
    }
  }

  async function resetCategory(category, label) {
    const ok = await window.shellmateDialog.confirm({
      title: `Reset everything under ${label}?`,
      body: 'Those settings go back to their defaults.',
      confirmLabel: 'Reset',
    });
    if (ok) reset({ category });
  }

  async function reset(what) {
    if (!what.key && !what.category) {
      const ok = await window.shellmateDialog.confirm({
        title: 'Reset every advanced setting?',
        body: 'All of them go back to their defaults. Nothing else in Settings ' +
              'is touched.',
        confirmLabel: 'Reset everything',
        danger: true,
      });
      if (!ok) return;
    }

    try {
      const res = await fetch('/api/advanced/reset', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(what),
      });
      registry = await res.json();
      render();
      report('Back to the defaults.');
    } catch (e) {
      report(e.message, true);
    }
  }

  /**
   * What was left out, and why.
   *
   * Part of the deliverable rather than an omission: without it somebody goes
   * looking for the scrypt parameters in settings.json and concludes they were
   * forgotten.
   */
  function notExposed() {
    const group = document.createElement('details');
    group.className = 'stockton-group';

    const summary = document.createElement('summary');
    summary.className = 'snippet-group-head';
    summary.textContent = 'Deliberately not here';
    group.appendChild(summary);

    (registry.not_exposed || []).forEach(entry => {
      const row = document.createElement('div');
      row.className = 'stockton-row';

      const text = document.createElement('div');
      text.className = 'stockton-text';

      const label = document.createElement('div');
      label.className = 'stockton-label';
      label.textContent = entry.label;

      const why = document.createElement('div');
      why.className = 'stockton-summary';
      why.textContent = entry.why;

      text.append(label, why);
      row.appendChild(text);
      group.appendChild(row);
    });

    return group;
  }

  function report(text, isError) {
    if (!statusEl) return;
    statusEl.textContent = text || '';
    statusEl.classList.toggle('field-warn', !!isError);
  }

  window.openStockton = open;
})();
