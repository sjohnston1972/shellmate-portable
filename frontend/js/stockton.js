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

  /** Everything /api/advanced knows: the settings, their categories, the
   *  exclusions and which values have been moved off their defaults. */
  let registry = { settings: [], categories: {}, not_exposed: [] };

  /**
   * Whether a restart-only setting has been changed this session.
   *
   * Referenced by save() since the restart offer was written, but never
   * declared — so under 'use strict' the first restart-flagged save threw
   * ReferenceError inside the try and surfaced as a save-failure toast
   * (#221). It gates the per-row Restart button: greyed until a change
   * actually needs it.
   */
  let pendingRestart = false;

  /**
   * No panel of its own any more (#135) — the sections live in Settings.
   *
   * `openStockton(category)` is kept as a signpost. Several places still send
   * people here by name, and a function that silently does nothing is worse
   * than one that opens the right place.
   */
  document.addEventListener('DOMContentLoaded', () => {
    load();
    // Rebuilt on save, because a reset or a changed value alters what the
    // "modified" marks say.
    window.addEventListener('shellmate:settings-changed', load);
    // And when something else writes an advanced value — the global
    // destructive-command switch (#252) sets two of these rows' keys, and
    // the rows must not go on showing the old values.
    window.addEventListener('shellmate:advanced-changed', load);
  });

  async function open(category) {
    if (typeof window.openSettings === 'function') window.openSettings();
    await load();
    if (category && typeof window.openSettingsSection === 'function') {
      // A category rendered inside a hand-written section navigates to that
      // section — its own heading is only an h4 there and matches no nav
      // entry. Only a standalone category answers to its registry heading.
      const slot = document.querySelector(
        `.settings-slot[data-category="${category}"]`);
      const section = slot && slot.closest('.settings-section');
      const title = section
        ? ((section.querySelector('.settings-section-title') || {}).textContent || '')
        : (registry.categories || {})[category];
      if (title) window.openSettingsSection(title.trim());
    }
  }

  async function load() {
    try {
      const res = await fetch('/api/advanced');
      registry = await res.json();
      render();
    } catch (e) {
      report('Could not read the advanced settings: ' + e.message, true);
    }
  }

  /**
   * Render every category into the Settings panel (#135).
   *
   * Stockton was a second panel with its own nav, its own search and its own
   * mental model, and the standing question of which of the two a setting
   * lived in. The split has done its job — the values are exposed, bounded
   * and documented — and now costs more than it returns.
   *
   * **The registry is not what was removed.** advanced.py still makes the
   * declaration the default: ssh_handler.py calls
   * advanced("ssh.connect_timeout") rather than holding its own copy, and
   * these rows still render from that same list. Sixty hand-written rows
   * against sixty constants would drift silently, with the label describing
   * what the code no longer does. Only the *panel* is gone.
   *
   * Each category becomes a `.settings-section` with a
   * `.settings-section-title`, which is exactly what settings_nav.js builds
   * its nav and its search from — so they are listed and findable without
   * being told about, and the EXTRAS list that existed because search could
   * not see them is no longer needed for these.
   */
  function render() {
    const fallback = document.getElementById('settings-advanced');
    if (!fallback || !registry.settings) return;

    // Each category renders into the slot for its family (#151), so the
    // advanced values sit beside the hand-written settings they belong with
    // rather than in a block of their own at the end. A category with no slot
    // lands in the fallback — a new one added to CATEGORIES is then unsorted
    // rather than invisible, which is the failure worth avoiding.
    // Park the prompt editor before wiping (#348). It is *adopted* into the
    // ai slot below, so the innerHTML wipe destroyed it on the second render
    // — every advanced-value save or Save Settings — and "The system
    // prompts" was gone until a full page reload.
    const promptEditor = document.getElementById('prompt-editor-block');
    if (promptEditor) {
      promptEditor.hidden = true;
      document.body.appendChild(promptEditor);
    }

    document.querySelectorAll('.settings-slot').forEach(s => { s.innerHTML = ''; });
    fallback.innerHTML = '';

    Object.entries(registry.categories).forEach(([key, title]) => {
      const items = registry.settings.filter(s => s.category === key);
      if (!items.length) return;

      const host = document.querySelector(`.settings-slot[data-category="${key}"]`)
                || fallback;

      // A slot inside a section renders as a subsection — the same rows, one
      // heading fewer (#152). Only a slot standing on its own becomes a full
      // section with a nav entry of its own; that is the difference between
      // 24 headings and 16.
      if (host !== fallback && host.closest('.settings-section')) {
        const sub = document.createElement('div');
        sub.className = 'settings-subgroup';

        const heading = document.createElement('h4');
        heading.className = 'settings-subsection-title';
        heading.textContent = title;
        sub.appendChild(heading);

        const note = document.createElement('p');
        note.className = 'settings-section-hint';
        note.textContent = 'These apply as soon as you change them.';
        sub.appendChild(note);

        items.forEach(setting => sub.appendChild(row(setting, false)));
        host.appendChild(sub);
        return;
      }

      const el = section(title, items, false);

      // These save as you change them, unlike everything above, which waits
      // for Save Settings. Saying so beats letting somebody close the panel
      // believing they had discarded something.
      const note = document.createElement('p');
      note.className = 'settings-section-hint';
      note.textContent = 'These apply as soon as you change them, and each '
                       + 'says what it is for. Anything you have moved off '
                       + 'its default is marked.';
      el.insertBefore(note, el.children[1] || null);

      // The prompt editor is not a scalar with a default and a range, so it
      // cannot be a registry entry — it is two kilobytes of prose in a
      // different file with its own reset. Moved rather than rebuilt, so
      // prompts_editor.js keeps working untouched.
      if (key === 'ai') {
        const editor = document.getElementById('prompt-editor-block');
        if (editor) {
          editor.hidden = false;
          // Belt as well as braces. The attribute is what the markup carries;
          // the class is what any section-hiding code reaches for, and
          // clearing only one is how this block spent a while being present,
          // correctly built and invisible.
          editor.classList.remove('hidden');
          el.appendChild(editor);
        }
      }

      host.appendChild(el);
    });

    // The "Deliberately not here" section was rendered here (#150). The
    // reasoning it carried has not gone — NOT_EXPOSED in advanced.py still
    // pairs every excluded value with why, where the decision is actually
    // made and where whoever maintains it will look. It simply stopped
    // explaining itself to people who never asked.

    // The nav is built from the sections present, so it has to be told.
    window.dispatchEvent(new CustomEvent('shellmate:sections-changed'));
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

    // How a change lands. Nothing for "live", which is nearly all of them —
    // a tag on every row would say nothing.
    if (setting.applies && setting.applies !== 'live') {
      const tag = document.createElement('span');
      tag.className = 'stockton-restart';
      tag.textContent = setting.applies === 'tabs'
        ? 'applies to new tabs'
        : 'needs a restart';
      meta.appendChild(tag);

      // The way out, next to the sentence that creates the need (#221).
      // Greyed until a restart-only setting has actually been changed —
      // a live restart button beside an untouched setting is an invitation
      // to drop every session for nothing.
      if (setting.applies === 'restart') {
        const go = document.createElement('button');
        go.type = 'button';
        go.className = 'btn-tertiary stockton-restart-btn';
        go.textContent = 'Restart now';
        go.disabled = !pendingRestart;
        go.title = pendingRestart
          ? 'Restart ShellMate to apply the change'
          : 'Enabled once a setting that needs a restart is changed';
        go.addEventListener('click', offerRestart);
        meta.appendChild(go);
      }
    }

    text.append(label, summary, meta);
    el.append(text, control(setting));

    // A setting you can only judge by using it gets somewhere to use it
    // (#300). Word separators decide what a double-click selects, and the
    // only way to tell was to save, close Settings and try it on a device.
    if (setting.key === 'terminal.word_separators') {
      el.classList.add('setting-row-stack');
      el.appendChild(_separatorTester(setting));
    }
    return el;
  }

  /**
   * Double-click a sample line and see what the current separators select.
   *
   * The same rule xterm applies: a word runs until it meets one of these
   * characters. Read from the field beside it rather than from the saved
   * value, so an edit can be judged before it is committed.
   */
  function _separatorTester(setting) {
    const wrap = document.createElement('div');
    wrap.className = 'sep-tester';

    const sample = document.createElement('div');
    sample.className = 'sep-tester-sample';
    sample.title = 'Double-click a word to see what would be selected';
    sample.textContent =
      'GigabitEthernet0/1 10.20.30.40/24 up/up  vrf:MGMT  "core-sw-01"';

    const result = document.createElement('div');
    result.className = 'sep-tester-result';
    result.textContent = 'Double-click anywhere above.';

    sample.addEventListener('dblclick', (e) => {
      const field = document.getElementById(fieldId(setting));
      const separators = field ? field.value : setting.value;
      const text = sample.textContent;
      // Where the click landed, from the caret rather than a guess.
      const caret = document.caretPositionFromPoint
        ? document.caretPositionFromPoint(e.clientX, e.clientY)
        : null;
      let index = caret ? caret.offset : 0;
      if (index >= text.length) index = text.length - 1;

      const isSep = (ch) => ch === undefined || separators.includes(ch);
      if (isSep(text[index])) {
        result.textContent = 'That character is a separator — nothing selected.';
        return;
      }
      let start = index;
      let end = index;
      while (start > 0 && !isSep(text[start - 1])) start -= 1;
      while (end < text.length - 1 && !isSep(text[end + 1])) end += 1;
      result.textContent = `Selects: "${text.slice(start, end + 1)}"`;
    });

    wrap.append(sample, result);
    return wrap;
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

  async function save(setting, value) {
    try {
      const res = await fetch('/api/advanced', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ values: { [setting.key]: value } }),
      });
      const payload = await res.json().catch(() => ({}));
      if (!res.ok) {
        // An error body is not a registry (#353): adopting it made the
        // next line throw "cannot read properties of undefined" and left
        // `registry` corrupted until the next successful load.
        report(payload.detail || `The server refused it (HTTP ${res.status}).`, true);
        return;
      }
      registry = payload;

      // Re-read rather than trust what was typed: the backend clamps, and a
      // field still showing a rejected value would be a quiet lie.
      const stored = registry.settings.find(s => s.key === setting.key);
      if (setting.restart) pendingRestart = true;

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
      const payload = await res.json().catch(() => ({}));
      if (!res.ok) {
        report(payload.detail || `The server refused it (HTTP ${res.status}).`, true);
        return;
      }
      registry = payload;
      render();
      report('Back to the defaults.');
    } catch (e) {
      report(e.message, true);
    }
  }


  /**
   * The per-row Restart button's click (#221).
   *
   * Availability is asked at click time rather than held from an earlier
   * check — whether a restart can work depends on how the process was
   * started, and a button acting on a stale answer would either fail or lie.
   * The old footer-based offer targeted the removed Stockton panel and could
   * never render; this replaces it where the "needs a restart" text lives.
   */
  async function offerRestart() {
    let info = { available: false, sessions: [] };
    try {
      info = await (await fetch('/api/restart')).json();
    } catch (_) { /* fall through to the note below */ }

    if (!info.available) {
      // A button that cannot work is worse than a sentence that explains.
      report('Quit from the tray and start ShellMate again for that to take '
             + 'effect — this build cannot restart itself.', true);
      return;
    }
    doRestart(info.sessions || []);
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

  /**
   * Say what happened to a change.
   *
   * Used the panel's own footer, which no longer exists — the shared toast
   * stack is where every other confirmation goes, and one of those is
   * exactly what this is.
   */
  function report(text, isError) {
    if (!text || !window.shellmateAlerts) return;
    window.shellmateAlerts.notify({
      severity: isError ? 'warning' : 'info',
      icon: isError ? 'error' : 'check_circle',
      title: text,
    });
  }

  window.openStockton = open;
})();
