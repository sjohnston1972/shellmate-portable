/**
 * platforms_editor.js — Edit device platform definitions in the app.
 *
 * platforms.json drives paging-off, config retrieval, aliases and the
 * dangerous-command list. It was editable only by finding the file in the
 * data directory — fine for someone at a desk, useless for the person on a
 * customer site who has just met a device ShellMate does not recognise.
 *
 * The alias table is the part people will actually use day to day, so it gets
 * a proper editor rather than a JSON blob: one row per alias, add and remove,
 * and a note of which platform is being edited so a Junos command does not get
 * saved against IOS.
 */
(function () {
  'use strict';

  let data = { platforms: {}, builtin: [], path: '' };
  let current = null;

  /**
   * Alias names present when this platform was loaded.
   *
   * Needed to tell "removed" from "never existed": see collect().
   */
  let loadedAliases = new Set();

  document.addEventListener('DOMContentLoaded', () => {
    const host = document.getElementById('platform-editor');
    if (!host) return;

    document.getElementById('btn-platform-reload').addEventListener('click', load);
    document.getElementById('btn-platform-save').addEventListener('click', save);
    document.getElementById('btn-platform-reset').addEventListener('click', resetAll);
    document.getElementById('btn-alias-add').addEventListener('click', () => addAliasRow('', ''));
    document.getElementById('platform-select')
      .addEventListener('change', (e) => showPlatform(e.target.value));

    load();
  });

  async function load() {
    const status = document.getElementById('platform-status');
    try {
      const res = await fetch('/api/platforms');
      if (!res.ok) throw new Error(`server returned ${res.status}`);
      data = await res.json();
    } catch (e) {
      status.textContent = `Could not load platform definitions: ${e.message}`;
      return;
    }

    const select = document.getElementById('platform-select');
    const previous = select.value;
    select.innerHTML = '';

    Object.keys(data.platforms).sort().forEach(id => {
      const opt = document.createElement('option');
      opt.value = id;
      const builtin = data.builtin.includes(id);
      opt.textContent = `${data.platforms[id].name}${builtin ? '' : '  (yours)'}`;
      select.appendChild(opt);
    });

    select.value = (previous && data.platforms[previous]) ? previous : (Object.keys(data.platforms).sort()[0] || '');
    status.textContent = `Stored in ${data.path}`;
    showPlatform(select.value);
  }

  function showPlatform(id) {
    current = id;
    const profile = data.platforms[id];
    if (!profile) return;

    setValue('platform-name', profile.name);
    setValue('platform-paging', profile.paging_off);
    setValue('platform-showrun', profile.show_run);
    setValue('platform-version', profile.version_command);
    setValue('platform-signatures', (profile.signatures || []).join(', '));
    setValue('platform-dangerous', (profile.dangerous_commands || []).join(', '));

    const list = document.getElementById('alias-rows');
    list.innerHTML = '';
    loadedAliases = new Set(Object.keys(profile.aliases || {}));
    Object.keys(profile.aliases || {}).sort().forEach(name => {
      addAliasRow(name, profile.aliases[name]);
    });

    const del = document.getElementById('btn-platform-delete');
    // A built-in cannot be deleted — it would reappear from the defaults on
    // the next load, which would read as the delete having failed silently.
    del.classList.toggle('hidden', data.builtin.includes(id));
  }

  function setValue(id, value) {
    const el = document.getElementById(id);
    if (el) el.value = value || '';
  }

  function addAliasRow(name, command) {
    const list = document.getElementById('alias-rows');
    const row = document.createElement('div');
    row.className = 'alias-row';

    const key = document.createElement('input');
    key.type = 'text';
    key.className = 'alias-key';
    key.placeholder = 'ints';
    key.value = name;
    key.spellcheck = false;

    const arrow = document.createElement('span');
    arrow.className = 'alias-arrow';
    arrow.textContent = '→';

    const cmd = document.createElement('input');
    cmd.type = 'text';
    cmd.className = 'alias-cmd';
    cmd.placeholder = 'show ip interface brief';
    cmd.value = command;
    cmd.spellcheck = false;

    const remove = document.createElement('button');
    remove.type = 'button';
    remove.className = 'alias-remove';
    remove.title = 'Remove this alias';
    remove.innerHTML = '<span class="material-symbols-outlined">close</span>';
    remove.addEventListener('click', () => row.remove());

    row.append(key, arrow, cmd, remove);
    list.appendChild(row);
    if (!name) key.focus();
  }

  function collect() {
    const aliases = {};
    const present = new Set();

    document.querySelectorAll('#alias-rows .alias-row').forEach(row => {
      const name = row.querySelector('.alias-key').value.trim().toLowerCase();
      const cmd = row.querySelector('.alias-cmd').value.trim();
      if (name && cmd) { aliases[name] = cmd; present.add(name); }
    });

    // Aliases merge with the built-ins on load, so that upgrades deliver new
    // ones rather than freezing everybody on the set that existed the day
    // they first opened the file. The consequence is that simply omitting an
    // alias does not delete it — it comes back from the defaults. Removing
    // one has to be stated explicitly, which is what the empty string means.
    loadedAliases.forEach(name => {
      if (!present.has(name)) aliases[name] = '';
    });

    const list = (id) => (document.getElementById(id).value || '')
      .split(',').map(s => s.trim()).filter(Boolean);

    return {
      name:                document.getElementById('platform-name').value.trim() || current,
      paging_off:          document.getElementById('platform-paging').value.trim(),
      show_run:            document.getElementById('platform-showrun').value.trim(),
      version_command:     document.getElementById('platform-version').value.trim(),
      signatures:          list('platform-signatures'),
      dangerous_commands:  list('platform-dangerous'),
      aliases,
      config_mode_markers: (data.platforms[current] || {}).config_mode_markers || [],
      comment_prefix:      (data.platforms[current] || {}).comment_prefix || '!',
    };
  }

  async function save() {
    const status = document.getElementById('platform-status');
    if (!current) return;

    status.textContent = 'Saving…';
    try {
      const res = await fetch(`/api/platforms/${encodeURIComponent(current)}`, {
        method:  'PUT',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify(collect()),
      });
      const body = await res.json();
      if (!res.ok) { status.textContent = body.detail || 'Could not save.'; return; }
      status.textContent = `Saved ${body.name}.`;
      await load();
    } catch (e) {
      status.textContent = `Could not save: ${e.message}`;
    }
  }

  async function resetAll() {
    const ok = await window.shellmateDialog.confirm({
      title: 'Discard every platform edit?',
      body: 'The built-in definitions are restored. Any platform you added ' +
            'yourself is removed, along with every alias and command you have changed.',
      confirmLabel: 'Reset all',
      danger: true,
    });
    if (!ok) return;

    const status = document.getElementById('platform-status');
    try {
      const res = await fetch('/api/platforms/reset', { method: 'POST' });
      if (!res.ok) throw new Error(`server returned ${res.status}`);
      status.textContent = 'Restored the built-in definitions.';
      await load();
    } catch (e) {
      status.textContent = `Could not reset: ${e.message}`;
    }
  }

  window.reloadPlatforms = load;
})();
