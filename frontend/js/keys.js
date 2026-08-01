/**
 * keys.js — Making an SSH key without leaving the application.
 *
 * ShellMate could use a key but not make one, so everybody went to ssh-keygen
 * or PuTTYgen first and came back. For a tool whose premise is one executable
 * on a stick with no install rights, that was the wrong missing piece:
 * generating a key needs no network and no device.
 *
 * The panel is deliberately one field and a button, with everything else
 * behind **Advanced**. Somebody who knows they need 4096-bit RSA because the
 * switch is ancient can find it; somebody who does not should never have to
 * form an opinion about elliptic curves to get a key.
 *
 * Every choice that has a consequence carries a tooltip, because this is the
 * part of the application where the vocabulary is least shared and the
 * consequences are largest.
 */
(function () {
  'use strict';

  let overlay, listEl, statusEl;
  let meta = { types: [], rsa_sizes: [], curves: [] };

  document.addEventListener('DOMContentLoaded', () => {
    overlay = document.getElementById('keys-overlay');
    if (!overlay) return;

    listEl   = document.getElementById('keys-list');
    statusEl = document.getElementById('keys-status');

    const link = document.getElementById('sidebar-link-keys');
    if (link) link.addEventListener('click', (e) => { e.preventDefault(); open(); });

    document.getElementById('keys-close').addEventListener('click', close);
    overlay.addEventListener('click', (e) => { if (e.target === overlay) close(); });
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && !overlay.classList.contains('hidden')) close();
    });

    document.getElementById('key-generate').addEventListener('click', generate);
    document.getElementById('key-import').addEventListener('click', importKey);
    document.getElementById('key-kind').addEventListener('change', showRelevantOptions);

    showRelevantOptions();
  });

  async function open() {
    overlay.classList.remove('hidden');
    report('');
    await load();
    setTimeout(() => document.getElementById('key-name').focus(), 60);
  }

  function close() { overlay.classList.add('hidden'); }

  /** Only show the options that apply to the chosen type. */
  function showRelevantOptions() {
    const kind = document.getElementById('key-kind').value;
    document.getElementById('key-bits-row').classList.toggle('hidden', kind !== 'rsa');
    document.getElementById('key-curve-row').classList.toggle('hidden', kind !== 'ecdsa');
  }

  async function load() {
    try {
      const res = await fetch('/api/keys');
      const data = await res.json();
      meta = data;
      render(data.keys || []);
      const folder = document.getElementById('keys-folder');
      if (folder) folder.textContent = data.folder || '';
    } catch (e) {
      report('Could not read the keys folder: ' + e.message, true);
    }
  }

  function render(items) {
    listEl.innerHTML = '';

    if (!items.length) {
      const empty = document.createElement('div');
      empty.className = 'broadcast-empty';
      empty.textContent = 'No keys yet.';
      listEl.appendChild(empty);
      return;
    }

    items.forEach(key => listEl.appendChild(row(key)));
  }

  function row(key) {
    const el = document.createElement('div');
    el.className = 'key-row';

    const head = document.createElement('div');
    head.className = 'key-head';

    const name = document.createElement('span');
    name.className = 'key-name';
    name.textContent = key.name;

    const kind = document.createElement('span');
    kind.className = 'snippet-tag';
    kind.textContent = key.kind;

    head.append(name, kind);

    if (key.encrypted) {
      const lock = document.createElement('span');
      lock.className = 'snippet-tag key-locked';
      lock.textContent = 'passphrase';
      head.appendChild(lock);
    } else {
      // Said every time the key is looked at, not once when it was made. An
      // unencrypted private key is a password sitting in a folder.
      const warn = document.createElement('span');
      warn.className = 'snippet-tag key-open';
      warn.textContent = 'no passphrase';
      head.appendChild(warn);
    }

    el.appendChild(head);

    if (key.comment) {
      const comment = document.createElement('div');
      comment.className = 'key-comment';
      comment.textContent = key.comment;
      el.appendChild(comment);
    }

    // Both fingerprints: SHA256 is what modern OpenSSH prints, MD5 is what a
    // great deal of network kit still shows, and comparing what the device
    // says against what you hold is the entire purpose.
    el.appendChild(fingerprint('SHA256', key.fingerprint_sha256));
    el.appendChild(fingerprint('MD5', key.fingerprint_md5));

    const actions = document.createElement('div');
    actions.className = 'key-actions';

    actions.appendChild(button('Copy public key', () => copy(key.public_key),
      'What you paste into the device'));
    actions.appendChild(button('Use for a connection', () => useForConnection(key),
      'Fill this key into the connection dialog'));
    actions.appendChild(button('Passphrase…', () => changePassphrase(key),
      'Add, change or remove it'));

    const remove = button('Delete', () => remove_(key), 'Remove this key');
    remove.classList.add('key-danger');
    actions.appendChild(remove);

    el.appendChild(actions);
    return el;
  }

  function fingerprint(label, value) {
    const el = document.createElement('div');
    el.className = 'key-fingerprint';

    const tag = document.createElement('span');
    tag.className = 'key-fingerprint-label';
    tag.textContent = label;

    const text = document.createElement('code');
    text.textContent = value;

    const copyBtn = document.createElement('button');
    copyBtn.type = 'button';
    copyBtn.className = 'diff-copy';
    copyBtn.textContent = 'Copy';
    copyBtn.addEventListener('click', () => copy(value));

    el.append(tag, text, copyBtn);
    return el;
  }

  function button(label, onClick, title) {
    const el = document.createElement('button');
    el.type = 'button';
    el.className = 'btn-tertiary';
    el.textContent = label;
    if (title) el.title = title;
    el.addEventListener('click', onClick);
    return el;
  }

  // ---------------------------------------------------------------------------
  // Actions
  // ---------------------------------------------------------------------------

  async function generate() {
    const body = {
      name:       document.getElementById('key-name').value.trim() || 'id_shellmate',
      kind:       document.getElementById('key-kind').value,
      bits:       parseInt(document.getElementById('key-bits').value, 10) || 3072,
      curve:      document.getElementById('key-curve').value,
      passphrase: document.getElementById('key-passphrase').value,
      comment:    document.getElementById('key-comment').value.trim(),
    };

    if (!body.passphrase) {
      const ok = await window.shellmateDialog.confirm({
        title: 'Create this key without a passphrase?',
        body: 'The private key file will not be encrypted. Anyone who can read ' +
              'your data folder — including anyone who picks up the stick ' +
              'ShellMate is running from — can use it to reach whatever it opens.',
        note: 'Reasonable for a lab. Think twice for anything on the production estate.',
        confirmLabel: 'Create it anyway',
        danger: true,
      });
      if (!ok) return;
    }

    report('Generating…');
    try {
      const res = await fetch('/api/keys', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || `server returned ${res.status}`);

      document.getElementById('key-passphrase').value = '';
      await load();
      report(`Created ${data.name}. Copy the public key into the device to use it.`);
    } catch (e) {
      report(e.message, true);
    }
  }

  async function importKey() {
    const field = document.getElementById('key-import-path');
    const path = field.value.trim();
    if (!path) { report('Choose a key file first.', true); return; }

    try {
      const res = await fetch('/api/keys/import', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || `server returned ${res.status}`);
      field.value = '';
      await load();
      report(`Imported ${data.name}.`);
    } catch (e) {
      report(e.message, true);
    }
  }

  async function changePassphrase(key) {
    const current = key.encrypted
      ? await window.shellmateDialog.prompt({
          title: `Current passphrase for ${key.name}`,
          label: 'Passphrase',
        })
      : '';
    if (key.encrypted && current === null) return;

    const next = await window.shellmateDialog.prompt({
      title: key.encrypted ? 'New passphrase' : `Add a passphrase to ${key.name}`,
      label: 'Leave blank to remove it entirely',
    });
    if (next === null) return;

    try {
      const res = await fetch('/api/keys/passphrase', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          path: key.path, old_passphrase: current || '', new_passphrase: next,
        }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || `server returned ${res.status}`);
      await load();
      report(next ? 'Passphrase set.' : 'Passphrase removed.');
    } catch (e) {
      report(e.message, true);
    }
  }

  async function remove_(key) {
    const ok = await window.shellmateDialog.confirm({
      title: `Delete ${key.name}?`,
      list: [{ text: key.fingerprint_sha256, mono: true }],
      note: 'Any device that trusts this key will stop accepting it. There is ' +
            'no copy anywhere else.',
      confirmLabel: 'Delete',
      danger: true,
    });
    if (!ok) return;

    try {
      await fetch('/api/keys/delete', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: key.path }),
      });
      await load();
      report(`Deleted ${key.name}.`);
    } catch (e) {
      report(e.message, true);
    }
  }

  /** Fill the key straight into the connection dialog — where it is wanted. */
  function useForConnection(key) {
    close();
    if (typeof window.showConnectionDialog === 'function') window.showConnectionDialog();
    setTimeout(() => {
      const field = document.getElementById('field-key-path');
      if (!field) return;
      field.value = key.path;
      field.dispatchEvent(new Event('input', { bubbles: true }));
      // The key options are collapsed by default; open them so the change is
      // visible rather than filed away somewhere the user has to go looking.
      const details = field.closest('details');
      if (details) details.open = true;
    }, 120);
  }

  async function copy(text) {
    try {
      await navigator.clipboard.writeText(text);
    } catch (e) {
      const scratch = document.createElement('textarea');
      scratch.value = text;
      scratch.style.position = 'fixed';
      scratch.style.opacity = '0';
      document.body.appendChild(scratch);
      scratch.select();
      try { document.execCommand('copy'); } catch (_) { /* nothing left to try */ }
      scratch.remove();
    }
    if (typeof window._showCopyToast === 'function') window._showCopyToast();
  }

  function report(text, isError) {
    if (!statusEl) return;
    statusEl.textContent = text || '';
    statusEl.classList.toggle('field-warn', !!isError);
  }

  window.openKeys = open;
})();
