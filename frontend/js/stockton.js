/**
 * stockton.js — The advanced settings, rendered from the registry.
 *
 * Every row is built from what `backend/advanced.py` declares, and that
 * declaration *is* the default the code reads. Sixty hand-written rows against
 * sixty constants would drift — silently, because the label would go on
 * describing something the code no longer does.
 *
 * It wears the Settings chrome rather than one of its own: search box,
 * category rail, and sections using the same title/row/hint vocabulary. Two
 * panels that do the same kind of job should not need learning twice, and
 * fifty-three settings across ten categories is exactly the case a rail
 * exists to solve — the first attempt was a wall of accordions flush against
 * the panel edge, which is how it earned the rewrite.
 *
 * Two things the panel owes whoever opens it: saying plainly what this is,
 * and showing what has been changed. A setting altered eight months ago should
 * be visibly not standard, with its default beside it.
 */
(function () {
  'use strict';

  /** The pseudo-category for the "not exposed" list, kept last in the rail. */
  const EXCLUSIONS = '__excluded__';

  const ICONS = {
    identify:  'smart_toy',
    ssh:       'cable',
    terminal:  'terminal',
    history:   'search',
    capture:   'save',
    alerts:    'warning',
    broadcast: 'send',
    ai:        'smart_toy',
    files:     'description',
    diag:      'help',
  };

  let overlay, navEl, bodyEl, searchEl, countEl;
  let registry = { settings: [], categories: {}, not_exposed: [] };
  let active = null;
  /** Set once a setting that cannot be reapplied has been changed. */
  let pendingRestart = false;

  document.addEventListener('DOMContentLoaded', () => {
    overlay = document.getElementById('stockton-overlay');
    if (!overlay) return;

    navEl    = document.getElementById('stockton-nav');
    bodyEl   = document.getElementById('stockton-body');
    searchEl = document.getElementById('stockton-search');
    countEl  = document.getElementById('stockton-count');

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
    setTimeout(() => searchEl && searchEl.focus(), 60);
  }

  function close() { overlay.classList.add('hidden'); }

  async function load() {
    try {
      const res = await fetch('/api/advanced');
      registry = await res.json();
      if (!active) active = Object.keys(registry.categories)[0];
      buildNav();
      render();
    } catch (e) {
      report('Could not read the advanced settings: ' + e.message, true);
    }
  }

  function buildNav() {
    navEl.innerHTML = '';

    const entries = Object.keys(registry.categories)
      .map(id => [id, registry.categories[id]]);
    // The exclusions read as a category from the rail's point of view, and
    // belong at the end where somebody looking for a missing setting finds
    // them after failing to find it anywhere else.
    entries.push([EXCLUSIONS, 'Deliberately not here']);

    entries.forEach(([id, label]) => {
      const item = document.createElement('button');
      item.type = 'button';
      item.className = 'settings-nav-item' + (id === active ? ' active' : '');

      const icon = document.createElement('span');
      icon.className = 'material-symbols-outlined';
      icon.textContent = id === EXCLUSIONS ? 'block' : (ICONS[id] || 'tune');

      const text = document.createElement('span');
      text.textContent = label;

      item.append(icon, text);
      item.addEventListener('click', () => {
        active = id;
        searchEl.value = '';
        buildNav();
        render();
      });
      navEl.appendChild(item);
    });
  }

  function render() {
    const query = (searchEl.value || '').trim().toLowerCase();
    bodyEl.innerHTML = '';
    bodyEl.scrollTop = 0;

    const changed = registry.settings.filter(s => s.modified).length;
    if (countEl) {
      countEl.textContent = changed
        ? `${changed} of ${registry.settings.length} changed from the default`
        : `${registry.settings.length} settings, all at their defaults`;
    }

    // Searching drops the categories entirely — the point of a search is
    // finding a setting without knowing which one it lives under.
    if (query) {
      navEl.querySelectorAll('.settings-nav-item')
        .forEach(i => i.classList.remove('active'));

      const matches = registry.settings.filter(s =>
        s.label.toLowerCase().includes(query) ||
        s.key.toLowerCase().includes(query) ||
        (s.summary || '').toLowerCase().includes(query) ||
        (s.tip || '').toLowerCase().includes(query));

      if (!matches.length) {
        bodyEl.appendChild(emptyState('Nothing matches.'));
        return;
      }
      bodyEl.appendChild(section('Search results', matches, true));
      return;
    }

    if (active === EXCLUSIONS) {
      bodyEl.appendChild(exclusions());
      return;
    }

    const items = registry.settings.filter(s => s.category === active);
    bodyEl.appendChild(section(registry.categories[active], items, false));

    // The prompt editor is not a scalar with a default and a range, so it
    // cannot be a registry entry — it is two kilobytes of prose stored in a
    // different file with its own reset. It lives here anyway, because
    // rewriting what the assistant is told is tinkering by any definition.
    //
    // One bespoke section, moved rather than rebuilt, so prompts_editor.js
    // keeps working untouched.
    if (active === 'ai') {
      const editor = document.getElementById('prompt-editor-block');
      if (editor) {
        editor.hidden = false;
        // Belt as well as braces. The attribute is what the markup carries;
        // the class is what any section-hiding code reaches for, and clearing
        // only one of them is how this block spent a while being present,
        // correctly built and invisible.
        editor.classList.remove('hidden');
        bodyEl.appendChild(editor);
      }
    }
  }

  function section(title, items, showCategory) {
    const el = document.createElement('section');
    el.className = 'settings-section';

    const heading = document.createElement('h3');
    heading.className = 'settings-section-title';
    heading.textContent = title;
    el.appendChild(heading);

    items.forEach(setting => el.appendChild(row(setting, showCategory)));
    return el;
  }

  function row(setting, showCategory) {
    const el = document.createElement('div');
    el.className = 'setting-row stockton-row'
      + (setting.modified ? ' stockton-modified' : '')
      // A tick-list is a block, not a control that sits beside its label.
      + (setting.kind === 'algorithms' ? ' setting-row-stack stockton-stacked' : '');

    const text = document.createElement('div');
    text.className = 'stockton-text';

    const label = document.createElement('label');
    label.className = 'setting-label';
    label.htmlFor = fieldId(setting);
    label.textContent = setting.label;
    // The tooltip carries the trade-off, which is the one thing a label
    // cannot: what you give up by changing it.
    if (setting.tip) label.setAttribute('data-tip', setting.tip);

    const summary = document.createElement('div');
    summary.className = 'stockton-summary';
    summary.textContent = setting.summary;

    const meta = document.createElement('div');
    meta.className = 'stockton-meta';
    meta.textContent = describeDefault(setting, showCategory);

    // How a change lands. Nothing for "live", which is 54 of the 57 — a tag
    // on every row would say nothing.
    if (setting.applies && setting.applies !== 'live') {
      const tag = document.createElement('span');
      tag.className = 'stockton-restart';
      tag.textContent = setting.applies === 'tabs'
        ? 'applies to new tabs'
        : 'needs a restart';
      meta.appendChild(tag);
    }

    text.append(label, summary, meta);
    el.append(text, control(setting));
    return el;
  }

  function describeDefault(setting, showCategory) {
    const bits = [];
    if (showCategory) bits.push(registry.categories[setting.category]);
    bits.push(`default ${format(setting.default)}${setting.unit ? ' ' + setting.unit : ''}`);
    if (setting.min !== null && setting.max !== null) {
      bits.push(`${format(setting.min)}–${format(setting.max)}`);
    }
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
    wrap.className = 'setting-input-group stockton-control';

    let field;
    if (setting.kind === 'algorithms') {
      return algorithmPicker(setting);
    }
    if (setting.kind === 'bool') {
      // The same switch the rest of Settings uses, rather than a bare
      // checkbox that would read as a different application.
      const toggle = document.createElement('label');
      toggle.className = 'toggle';
      field = document.createElement('input');
      field.type = 'checkbox';
      field.checked = !!setting.value;
      field.addEventListener('change', () => save(setting, field.checked));
      const track = document.createElement('span');
      track.className = 'toggle-track';
      toggle.append(field, track);
      wrap.appendChild(toggle);
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
      wrap.appendChild(field);
    } else if (setting.kind === 'text') {
      field = document.createElement('input');
      field.type = 'text';
      field.className = 'setting-input';
      field.value = setting.value;
      field.addEventListener('change', () => save(setting, field.value));
      wrap.appendChild(field);
    } else {
      field = document.createElement('input');
      field.type = 'number';
      field.className = 'setting-input setting-input-sm';
      if (setting.min !== null) field.min = setting.min;
      if (setting.max !== null) field.max = setting.max;
      if (setting.kind === 'float') field.step = '0.1';
      field.value = setting.value;
      field.addEventListener('change', () => save(setting, field.value));
      wrap.appendChild(field);

      if (setting.unit) {
        const unit = document.createElement('span');
        unit.className = 'setting-unit';
        unit.textContent = setting.unit;
        wrap.appendChild(unit);
      }
    }

    field.id = fieldId(setting);

    // Only on a row that has been changed — a reset beside a value already at
    // its default is a button that does nothing.
    if (setting.modified) {
      const undo = document.createElement('button');
      undo.type = 'button';
      undo.className = 'btn-tertiary stockton-undo';
      undo.textContent = 'Reset';
      undo.title = `Back to ${format(setting.default)}`;
      undo.addEventListener('click', () => reset({ key: setting.key }));
      wrap.appendChild(undo);
    }

    return wrap;
  }


  /**
   * A tick-list of the algorithms paramiko will actually negotiate.
   *
   * These were free-text, comma-separated — the wrong control for the two
   * settings that exist to rescue a device you cannot currently reach. Nothing
   * told you the valid names, `diffie-hellman-group1-sha1` is not a string
   * anyone types from memory, and a typo was silently equivalent to naming
   * nothing: the entry did not match, so the algorithm you wanted was disabled
   * along with everything else.
   *
   * The order is paramiko's own preference order, not alphabetical — that
   * order is meaningful and sorting would put the weakest first.
   */
  function algorithmPicker(setting) {
    const wrap = document.createElement('div');
    wrap.className = 'stockton-control stockton-algorithms';

    // No list means paramiko's internals moved. Fall back to the text field
    // rather than showing an empty picker somebody cannot use at all.
    if (!setting.algorithms || !setting.algorithms.length) {
      const field = document.createElement('input');
      field.type = 'text';
      field.className = 'setting-input';
      field.id = fieldId(setting);
      field.value = setting.value;
      field.addEventListener('change', () => save(setting, field.value));
      wrap.appendChild(field);
      return wrap;
    }

    const chosen = new Set(
      String(setting.value || '').split(',').map(s => s.trim()).filter(Boolean));

    const list = document.createElement('div');
    list.className = 'algorithm-list';

    setting.algorithms.forEach(entry => {
      const row = document.createElement('label');
      row.className = 'algorithm-row';

      const box = document.createElement('input');
      box.type = 'checkbox';
      box.checked = chosen.has(entry.name);
      box.dataset.name = entry.name;
      box.addEventListener('change', () => {
        const picked = [...list.querySelectorAll('input:checked')]
          .map(b => b.dataset.name);
        save(setting, picked.join(','));
      });

      const name = document.createElement('code');
      name.textContent = entry.name;

      row.append(box, name);

      // Marked, not hidden. These are the entire reason the setting exists,
      // so it is a label rather than a warning.
      if (entry.legacy) {
        const tag = document.createElement('span');
        tag.className = 'algorithm-legacy';
        tag.textContent = 'legacy';
        row.appendChild(tag);
      }

      list.appendChild(row);
    });

    const summary = document.createElement('div');
    summary.className = 'algorithm-summary';
    summary.textContent = chosen.size
      ? `${chosen.size} chosen — only these are offered`
      : "Nothing chosen — paramiko's own set is offered";

    wrap.append(summary, list);

    if (chosen.size) {
      const clear = document.createElement('button');
      clear.type = 'button';
      clear.className = 'btn-tertiary stockton-undo';
      clear.textContent = 'Clear';
      clear.title = 'Back to offering the defaults';
      clear.addEventListener('click', () => reset({ key: setting.key }));
      wrap.appendChild(clear);
    }

    return wrap;
  }

  function emptyState(text) {
    const el = document.createElement('section');
    el.className = 'settings-section';
    const p = document.createElement('p');
    p.className = 'settings-section-hint';
    p.textContent = text;
    el.appendChild(p);
    return el;
  }

  /**
   * What was left out, and why.
   *
   * Part of the deliverable rather than an omission: without it somebody goes
   * looking for the vault's key-derivation parameters in settings.json and
   * concludes they were forgotten.
   */
  function exclusions() {
    const el = document.createElement('section');
    el.className = 'settings-section';

    const heading = document.createElement('h3');
    heading.className = 'settings-section-title';
    heading.textContent = 'Deliberately not here';
    el.appendChild(heading);

    const intro = document.createElement('p');
    intro.className = 'settings-section-hint';
    intro.textContent =
      'Each of these could break something rather than merely degrade it, ' +
      'which is the line everything above stays on the right side of.';
    el.appendChild(intro);

    (registry.not_exposed || []).forEach(entry => {
      const row = document.createElement('div');
      row.className = 'setting-row setting-row-stack stockton-excluded';

      const label = document.createElement('span');
      label.className = 'setting-label';
      label.textContent = entry.label;

      const why = document.createElement('p');
      why.className = 'settings-section-hint';
      why.textContent = entry.why;

      row.append(label, why);
      el.appendChild(row);
    });

    return el;
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
      if (setting.restart) pendingRestart = true;
      updateRestartOffer();

      report(stored && String(stored.value) !== String(value)
        ? `${setting.label} was adjusted to ${format(stored.value)} — its range is ` +
          `${format(setting.min)} to ${format(setting.max)}.`
        : `${setting.label} saved.` + (setting.restart ? ' Takes effect on restart.' : ''));
      render();
    } catch (e) {
      report(e.message, true);
    }
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
   * Offer a restart, once something that needs one has been changed.
   *
   * Two of fifty-seven settings genuinely cannot be reapplied — the rest are
   * read at the point of use. Telling somebody a restart is needed and then
   * leaving them to work out how is half a feature; the obvious move, closing
   * the window, is specifically not a shutdown here.
   */
  async function updateRestartOffer() {
    const footer = document.querySelector('#stockton-panel .settings-footer');
    if (!footer) return;

    const existing = document.getElementById('stockton-restart');
    if (!pendingRestart) { if (existing) existing.remove(); return; }
    if (existing) return;

    let info = { available: false, sessions: [] };
    try {
      info = await (await fetch('/api/restart')).json();
    } catch (_) { /* fall through to the note below */ }

    if (!info.available) {
      // A button that cannot work is worse than a sentence that explains.
      const note = document.createElement('span');
      note.id = 'stockton-restart';
      note.className = 'settings-footer-note field-warn';
      note.textContent = 'Quit from the tray and start ShellMate again for '
        + 'that to take effect.';
      footer.insertBefore(note, footer.firstChild);
      return;
    }

    const button = document.createElement('button');
    button.type = 'button';
    button.id = 'stockton-restart';
    button.className = 'btn-primary';
    button.textContent = 'Restart now';
    button.addEventListener('click', () => doRestart(info.sessions));
    footer.appendChild(button);
  }

  async function doRestart(sessions) {
    const ok = await window.shellmateDialog.confirm({
      title: 'Restart ShellMate?',
      body: sessions.length
        ? 'Every connection below is dropped. Anything already scheduled on '
          + 'a device — a pending reload — carries on regardless.'
        : 'Nothing is connected, so nothing is lost.',
      list: sessions.map(name => ({ text: name })),
      confirmLabel: 'Restart',
      danger: sessions.length > 0,
    });
    if (!ok) return;

    report('Restarting…');
    try {
      await fetch('/api/restart', { method: 'POST' });
    } catch (_) {
      // Expected: the process goes before the response arrives.
    }
    waitForReturn();
  }

  /**
   * Wait for the replacement to answer, then reload.
   *
   * The old process exits as soon as the new one is listening, so the page is
   * briefly talking to nothing. Reloading immediately shows a connection
   * error; waiting for an answer shows the application coming back.
   */
  function waitForReturn() {
    const started = Date.now();
    const tick = async () => {
      if (Date.now() - started > 60000) {
        report('ShellMate did not come back. Start it from the tray.', true);
        return;
      }
      try {
        const res = await fetch('/api/system/info', { cache: 'no-store' });
        if (res.ok) { window.location.reload(); return; }
      } catch (_) { /* still down */ }
      setTimeout(tick, 700);
    };
    setTimeout(tick, 1500);
  }

  function report(text, isError) {
    if (!countEl) return;
    const note = document.getElementById('stockton-message');
    if (note) note.remove();

    if (!text) return;
    const el = document.createElement('span');
    el.id = 'stockton-message';
    el.className = 'settings-footer-note' + (isError ? ' field-warn' : '');
    el.textContent = text;
    countEl.after(el);
    // The count is the steady state; a message is transient and should not
    // sit there for the rest of the session pretending to be current.
    setTimeout(() => el.remove(), 6000);
  }

  window.openStockton = open;
})();
