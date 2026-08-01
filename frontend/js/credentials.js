/**
 * credentials.js — Managing the passwords ShellMate has been asked to remember.
 *
 * A remembered credential used to be invisible from the moment it was saved.
 * Nothing listed them, nothing changed one, and the only way to remove one was
 * to find the connection it belonged to and reconnect with the box unticked.
 * A password kept as plain text was worse still: it lived in a JSON file with
 * nothing anywhere in the interface admitting it existed.
 *
 * The list here is deliberately asymmetric, and the asymmetry is the point:
 *
 *   - A **plaintext** credential can be shown, changed and encrypted. It is
 *     already readable in a text file, so refusing to display it in the
 *     interface protects nothing — it just sends people to a text editor.
 *   - A **vault** credential cannot be shown at any price. A vault that
 *     decrypts on demand for whatever asks is most of the way to not being a
 *     vault. It can be replaced or deleted; it cannot be read back.
 *
 * That difference is stated on every row rather than left to be discovered.
 */
(function () {
  'use strict';

  let listEl, noteEl, setsEl;

  document.addEventListener('DOMContentLoaded', () => {
    listEl = document.getElementById('credentials-list');
    noteEl = document.getElementById('credentials-note');
    if (!listEl) return;

    setsEl = document.getElementById('credential-sets');
    const addSet = document.getElementById('credential-set-add');
    if (addSet) addSet.addEventListener('click', createSet);

    const refresh = document.getElementById('credentials-refresh');
    if (refresh) refresh.addEventListener('click', load);

    const forgetAll = document.getElementById('credentials-forget-plain');
    if (forgetAll) forgetAll.addEventListener('click', forgetEverythingPlain);

    // The panel is built before it is opened, so populate on open rather than
    // on load — an unlocked vault may be unlocked between the two.
    const link = document.getElementById('sidebar-link-settings');
    if (link) link.addEventListener('click', () => setTimeout(load, 150));

    load();
  });

  async function load() {
    loadSets();
    if (!listEl) return;
    listEl.textContent = 'Loading…';
    try {
      const res = await fetch('/api/credentials');
      if (!res.ok) throw new Error('Could not read the credential list.');
      render(await res.json());
    } catch (e) {
      listEl.textContent = e.message;
    }
  }

  function render(data) {
    if (noteEl) noteEl.textContent = data.note || '';

    listEl.innerHTML = '';

    if (!data.entries.length) {
      const empty = document.createElement('p');
      empty.className = 'settings-section-hint';
      empty.textContent = data.vault_locked
        ? 'Nothing readable while the vault is locked.'
        : 'No device credentials are saved. Tick “Remember these credentials” '
          + 'when connecting to save one.';
      listEl.appendChild(empty);
      return;
    }

    data.entries.forEach(entry => listEl.appendChild(row(entry)));
  }

  function row(entry) {
    const el = document.createElement('div');
    el.className = 'credential-row';

    const who = document.createElement('div');
    who.className = 'credential-who';

    const name = document.createElement('span');
    name.className = 'credential-name';
    // textContent — a profile name is user input and is not ours to parse.
    name.textContent = entry.profile_name || entry.target;

    const detail = document.createElement('span');
    detail.className = 'credential-detail';
    detail.textContent = `${entry.field_label} · ${entry.connection_type.toUpperCase()} `
      + `${entry.target}${entry.username ? ` as ${entry.username}` : ''}`;

    who.append(name, detail);

    const badge = document.createElement('span');
    badge.className = 'credential-badge ' +
      (entry.storage === 'vault' ? 'credential-encrypted' : 'credential-plain');
    badge.textContent = entry.storage === 'vault' ? 'Encrypted' : 'Plain text';
    badge.title = entry.storage === 'vault'
      ? 'Stored encrypted. It cannot be displayed — replace it if you no longer know it.'
      : 'Stored as readable text in credentials-plaintext.json in your data folder.';

    const value = document.createElement('span');
    value.className = 'credential-value';
    value.textContent = entry.storage === 'vault'
      ? 'encrypted — cannot be displayed'
      : '••••••••';

    const actions = document.createElement('div');
    actions.className = 'credential-actions';

    if (entry.can_reveal) {
      actions.appendChild(button('Show', 'visibility', async (btn) => {
        if (btn.dataset.shown === '1') {
          value.textContent = '••••••••';
          btn.dataset.shown = '';
          btn.lastChild.textContent = 'Show';
          return;
        }
        const res = await fetch(
          `/api/credentials/${entry.profile_id}/${entry.field}/reveal`,
          { method: 'POST' });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || 'Could not read it.');
        value.textContent = data.value;
        btn.dataset.shown = '1';
        btn.lastChild.textContent = 'Hide';
      }));

      actions.appendChild(button('Encrypt', 'lock', async () => {
        const res = await fetch(`/api/credentials/${entry.profile_id}/encrypt`,
                                { method: 'POST' });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || 'Could not encrypt it.');
        load();
      }));
    }

    actions.appendChild(button('Change', 'edit', () => changeCredential(entry)));

    actions.appendChild(button('Delete', 'delete', async () => {
      const ok = await confirmWith(
        `Forget the ${entry.field_label.toLowerCase()} for `
        + `${entry.profile_name || entry.target}?`,
        'You will be asked for it the next time you connect.', true);
      if (!ok) return;
      const res = await fetch(
        `/api/credentials/${entry.profile_id}/${entry.field}`, { method: 'DELETE' });
      if (!res.ok) throw new Error('Could not delete it.');
      load();
    }, 'credential-danger'));

    el.append(who, badge, value, actions);
    return el;
  }

  /**
   * A button whose handler may throw.
   *
   * Every action here talks to the backend, and a failure has to land on the
   * row rather than in the console — this panel is the only place some of
   * these credentials are visible at all.
   */
  function button(label, icon, handler, extra) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'btn-secondary btn-tiny' + (extra ? ` ${extra}` : '');

    const glyph = document.createElement('span');
    glyph.className = 'material-symbols-outlined';
    glyph.textContent = icon;

    const text = document.createElement('span');
    text.textContent = label;

    btn.append(glyph, text);
    btn.addEventListener('click', async () => {
      btn.disabled = true;
      try {
        await handler(btn);
      } catch (e) {
        report(btn, e.message || 'That did not work.');
      } finally {
        btn.disabled = false;
      }
    });
    return btn;
  }

  function report(btn, message) {
    const row = btn.closest('.credential-row');
    if (!row) return;
    let note = row.querySelector('.credential-error');
    if (!note) {
      note = document.createElement('div');
      note.className = 'credential-error';
      row.appendChild(note);
    }
    note.textContent = message;
    setTimeout(() => note.remove(), 6000);
  }

  async function changeCredential(entry) {
    const value = await promptWith(
      `New ${entry.field_label.toLowerCase()} for `
      + `${entry.profile_name || entry.target}`,
      entry.storage === 'plaintext'
        ? 'It will be saved as readable text, as this one already is. '
          + 'Use Encrypt afterwards to move it into the vault.'
        : 'It will be encrypted, as this one already is.');
    if (!value) return;

    const res = await fetch(`/api/credentials/${entry.profile_id}/${entry.field}`, {
      method:  'PUT',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ value, storage: entry.storage }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'Could not save it.');
    load();
  }

  async function forgetEverythingPlain() {
    const ok = await confirmWith(
      'Forget every credential saved as plain text?',
      'Encrypted credentials are left alone. You will be asked for the '
      + 'others the next time you connect to those devices.', true);
    if (!ok) return;
    await fetch('/api/credentials/plaintext', { method: 'DELETE' });
    load();
  }

  // shellmateDialog replaced the native confirm()/prompt() everywhere; fall
  // back only so this panel still works if that script fails to load.
  function confirmWith(title, body) {
    if (window.shellmateDialog) return window.shellmateDialog.confirm(title, body);
    return Promise.resolve(window.confirm(`${title}\n\n${body}`));
  }

  function promptWith(title, body) {
    if (window.shellmateDialog) {
      return window.shellmateDialog.prompt(title, body, { password: true });
    }
    return Promise.resolve(window.prompt(`${title}\n\n${body}`));
  }

  // -------------------------------------------------------------------------
  // Shared credentials
  //
  // A named login several connections reference rather than each keeping a
  // copy. Copying works right up until the password changes, at which point
  // there are forty entries to update and nothing recording that they were
  // ever the same credential.
  // -------------------------------------------------------------------------

  async function loadSets() {
    if (!setsEl) return;
    try {
      const res = await fetch('/api/credential-sets');
      if (!res.ok) throw new Error('Could not read them.');
      renderSets(await res.json());
    } catch (e) {
      setsEl.textContent = e.message;
    }
  }

  function renderSets(data) {
    setsEl.innerHTML = '';

    if (!data.sets.length) {
      const empty = document.createElement('p');
      empty.className = 'settings-section-hint';
      empty.textContent = data.vault_locked
        ? 'The vault is locked, so shared credentials cannot be listed.'
        : 'None yet. One is worth creating before scanning a subnet — the '
          + 'devices it finds can then all point at it.';
      setsEl.appendChild(empty);
      return;
    }

    data.sets.forEach(entry => setsEl.appendChild(setRow(entry)));
  }

  function setRow(entry) {
    const el = document.createElement('div');
    el.className = 'credential-row';

    const who = document.createElement('div');
    who.className = 'credential-who';

    const name = document.createElement('span');
    name.className = 'credential-name';
    name.textContent = entry.name;

    const detail = document.createElement('span');
    detail.className = 'credential-detail';
    detail.textContent = (entry.username ? entry.username + ' · ' : '')
      + (entry.in_use === 1 ? 'used by 1 connection'
                            : 'used by ' + entry.in_use + ' connections');

    who.append(name, detail);

    const badge = document.createElement('span');
    badge.className = 'credential-badge ' +
      (entry.storage === 'vault' ? 'credential-encrypted' : 'credential-plain');
    badge.textContent = entry.storage === 'vault' ? 'Encrypted'
                      : entry.storage === 'plaintext' ? 'Plain text' : 'Empty';

    const value = document.createElement('span');
    value.className = 'credential-value';
    value.textContent = entry.storage === 'vault'
      ? 'encrypted — cannot be displayed'
      : entry.has_credentials ? '••••••••' : 'no password set';

    const actions = document.createElement('div');
    actions.className = 'credential-actions';

    actions.appendChild(button('Change', 'edit', async () => {
      const password = await promptWith(
        'New password for ' + entry.name,
        entry.in_use
          ? 'This is used by ' + entry.in_use + ' connection'
            + (entry.in_use === 1 ? '' : 's') + ', and all of them will use the new one.'
          : 'Nothing uses this yet.');
      if (!password) return;
      const res = await fetch('/api/credential-sets', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ id: entry.id, name: entry.name,
                                  username: entry.username, password,
                                  storage: entry.storage || 'vault' }),
      });
      if (!res.ok) throw new Error((await res.json()).detail || 'Could not save it.');
      loadSets();
    }));

    actions.appendChild(button('Delete', 'delete', async () => {
      // Naming the count is the point: deleting one that forty devices rely
      // on is a different decision from deleting an unused one.
      const ok = await confirmWith(
        'Delete the shared credential "' + entry.name + '"?',
        entry.in_use
          ? entry.in_use + ' connection' + (entry.in_use === 1 ? '' : 's')
            + ' use it and will ask for a password on next connect.'
          : 'Nothing is using it.',
        true);
      if (!ok) return;
      const res = await fetch('/api/credential-sets/' + entry.id, { method: 'DELETE' });
      if (!res.ok) throw new Error('Could not delete it.');
      load();
    }, 'credential-danger'));

    el.append(who, badge, value, actions);
    return el;
  }

  /**
   * Create a shared credential, in one form.
   *
   * This was three chained prompts — name, then username, then password —
   * which meant three confirmations, no way back, nothing visible once
   * entered, and validation only after all three had been answered. The
   * storage choice was not offered at all.
   */
  async function createSet() {
    const values = await window.shellmateDialog.form({
      title: 'New shared credential',
      body:  'A login several connections use. They point at it rather than '
             + 'each keeping a copy, so changing it later fixes all of them.',
      confirmLabel: 'Create',
      fields: [
        { name: 'name', label: 'Name', required: true,
          placeholder: 'Lab admin',
          hint: 'What you will recognise when picking it later.' },
        { name: 'username', label: 'Username',
          placeholder: 'neteng',
          hint: 'The account these connections log in as. Leave blank if it varies.' },
        { name: 'password', label: 'Password', type: 'password', required: true,
          hint: 'Cannot be read back once encrypted — use Show to check it.' },
        { name: 'storage', label: 'Keep it', type: 'select',
          options: [
            { value: 'vault',     label: 'Encrypted in the vault' },
            { value: 'plaintext', label: 'Plain text — readable on disk' },
          ],
          hint: 'Plain text puts one file in your data folder holding the '
                + 'password for every device using this.' },
      ],
    });
    if (!values) return;

    const res = await fetch('/api/credential-sets', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify(values),
    });
    if (!res.ok) {
      const detail = (await res.json()).detail || 'Could not save it.';
      if (setsEl) setsEl.textContent = detail;
      return;
    }
    loadSets();
  }

  function promptText(title, body) {
    if (window.shellmateDialog) {
      return window.shellmateDialog.prompt({ title, body, confirmLabel: 'Next' });
    }
    return Promise.resolve(window.prompt(title + '\n\n' + body));
  }

  window.shellmateCredentials = { load, loadSets };
})();
