/**
 * vault.js — Unlock prompt and vault settings.
 *
 * Two jobs:
 *   1. At startup, if the vault is protected by a master password, ask for it
 *      before anything tries to read a key. A DPAPI vault needs no prompt —
 *      it unlocks silently as the logged-in Windows user, which is the whole
 *      point of that mode.
 *   2. Drive the vault section of the settings panel, where the two modes are
 *      switched.
 *
 * Unlocking is always skippable. A locked vault degrades to "no saved values"
 * rather than blocking the app, so someone who has forgotten their master
 * password can still get to a device — which, on a tool you reach for when
 * the network is already broken, matters more than tidiness.
 */
(function () {
  'use strict';

  let overlay, form, passwordInput, errorBox, unlockBtn;
  let status = { exists: false, mode: 'none', locked: false,
                 dpapi_available: false, unreadable: false,
                 unreadable_reason: '' };

  document.addEventListener('DOMContentLoaded', () => {
    overlay       = document.getElementById('vault-overlay');
    form          = document.getElementById('vault-form');
    passwordInput = document.getElementById('vault-password');
    errorBox      = document.getElementById('vault-error');
    unlockBtn     = document.getElementById('vault-unlock-btn');

    form.addEventListener('submit', handleUnlock);
    document.getElementById('vault-skip').addEventListener('click', hidePrompt);

    document.getElementById('vault-mode-select')
      .addEventListener('change', onModeChange);
    document.getElementById('vault-apply-mode')
      .addEventListener('click', applyMode);

    // Backup and restore (#565).
    document.getElementById('vault-export-backup')
      .addEventListener('click', exportBackup);
    document.getElementById('vault-import-backup')
      .addEventListener('click', () => importBackup(false));
    document.getElementById('vault-recovery-import')
      .addEventListener('click', () => importBackup(true));
    document.getElementById('vault-recovery-restart')
      .addEventListener('click', startFresh);

    refreshStatus().then(() => {
      // An unreadable vault raises the overlay too. It used to raise nothing:
      // every read degraded to "no value" and the user learned about it one
      // device at a time.
      if (status.locked || status.unreadable) showPrompt();
    });
  });

  // -------------------------------------------------------------------------
  // Status
  // -------------------------------------------------------------------------

  async function refreshStatus() {
    try {
      const res = await fetch('/api/vault/status');
      if (res.ok) status = await res.json();
    } catch (e) {
      // A vault we cannot reach is treated as absent; the app still works.
    }
    renderSettings();
    return status;
  }

  function describe() {
    if (status.unreadable) {
      return 'Cannot be read on this machine — '
        + (status.mode === 'dpapi'
            ? 'it belongs to another Windows account or another computer.'
            : 'the file could not be opened.');
    }
    if (!status.exists) {
      return status.dpapi_available
        ? 'Empty. The first key you save will be encrypted with your Windows account.'
        : 'Empty. The first key you save will need a master password.';
    }
    if (status.mode === 'password') {
      return status.locked
        ? 'Locked — enter your master password to use saved keys.'
        : 'Unlocked, protected by a master password.';
    }
    return 'Protected by your Windows account. Nothing to type.';
  }

  function renderSettings() {
    const text = document.getElementById('vault-status-text');
    if (text) text.textContent = describe();

    const select = document.getElementById('vault-mode-select');
    if (select) {
      if (status.mode !== 'none') select.value = status.mode;
      const dpapiOption = select.querySelector('option[value="dpapi"]');
      // Offering an option that cannot work is worse than not offering it.
      if (dpapiOption && !status.dpapi_available) {
        dpapiOption.disabled = true;
        dpapiOption.textContent = 'This Windows account (Windows only)';
      }
      onModeChange();
    }
  }

  function onModeChange() {
    const select = document.getElementById('vault-mode-select');
    const row    = document.getElementById('vault-password-row');
    const hint   = document.getElementById('vault-mode-hint');
    if (!select || !row) return;

    const wantsPassword = select.value === 'password';
    row.classList.toggle('hidden', !wantsPassword);

    if (hint) {
      hint.textContent = wantsPassword
        ? 'The vault travels with the executable and works on any machine, but you type the password each launch.'
        : 'Nothing to remember, and a lost USB stick is useless to anyone else — but the vault only opens under this Windows account on this machine.';
    }
  }

  async function applyMode() {
    const select  = document.getElementById('vault-mode-select');
    const result  = document.getElementById('vault-mode-result');
    const pw      = document.getElementById('vault-new-password');
    const confirm = document.getElementById('vault-confirm-password');

    const mode = select.value;
    let password = '';

    if (mode === 'password') {
      password = pw.value;
      if (!password) { showResult(result, 'Choose a master password.', true); return; }
      if (password !== confirm.value) {
        showResult(result, 'The two passwords do not match.', true);
        return;
      }
      // No recovery path exists by design — the key is derived from the
      // password and nothing else — so say so before it is too late.
      const ok = await window.shellmateDialog.confirm({
        title: 'There is no way to recover this password',
        body: 'The key is derived from the password and nothing else, so ' +
              'forgetting it means the stored keys and device passwords are ' +
              'gone. There is no reset and no backdoor.',
        note: 'ShellMate still starts without it and you can still reach every ' +
              'device — you would type your credentials by hand instead.',
        confirmLabel: 'Use a master password',
        danger: true,
      });
      if (!ok) return;
    }

    showResult(result, 'Re-encrypting…', false);
    try {
      const res = await fetch('/api/vault/mode', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ mode, password }),
      });
      const data = await res.json();
      if (!res.ok) { showResult(result, data.detail || 'Could not switch mode.', true); return; }

      status = data;
      pw.value = '';
      confirm.value = '';
      renderSettings();
      showResult(result, 'Saved.', false);
    } catch (e) {
      showResult(result, 'Could not reach the server.', true);
    }
  }

  function showResult(el, text, isError) {
    if (!el) return;
    el.textContent = text;
    el.classList.toggle('vault-result-error', Boolean(isError));
  }

  // -------------------------------------------------------------------------
  // Unlock prompt
  // -------------------------------------------------------------------------

  function showPrompt() {
    // Two different situations wearing one overlay. Locked is answered by
    // typing; unreadable is not answered by typing at all, so the password
    // box goes away rather than sitting there inviting attempts that cannot
    // work.
    const stuck = Boolean(status.unreadable);
    const recovery = document.getElementById('vault-recovery');
    const title = document.getElementById('vault-title');
    const lead = document.getElementById('vault-lead');

    if (recovery) recovery.classList.toggle('hidden', !stuck);
    form.classList.toggle('hidden', stuck);
    if (title) {
      title.textContent = stuck ? 'This vault cannot be read here'
                                : 'Unlock your vault';
    }
    if (lead) {
      lead.textContent = stuck
        ? 'ShellMate found a vault in your data folder and cannot decrypt it '
          + 'on this machine.'
        : 'Your API keys and saved device passwords are encrypted with a '
          + 'master password.';
    }
    const why = document.getElementById('vault-recovery-why');
    if (why) why.textContent = status.unreadable_reason || '';

    overlay.classList.remove('hidden');
    if (!stuck) setTimeout(() => passwordInput.focus(), 50);
  }

  function hidePrompt() {
    overlay.classList.add('hidden');
    passwordInput.value = '';
    clearError();
  }

  async function handleUnlock(e) {
    e.preventDefault();
    clearError();

    const password = passwordInput.value;
    if (!password) { showError('Enter your master password.'); return; }

    // scrypt takes a moment on purpose; say so rather than looking frozen.
    unlockBtn.disabled = true;
    unlockBtn.textContent = 'Unlocking…';

    try {
      const res = await fetch('/api/vault/unlock', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ password }),
      });
      const data = await res.json();

      if (!res.ok) { showError(data.detail || 'Could not unlock.'); return; }

      status = data;
      hidePrompt();
      renderSettings();
      // Settings were read while the vault was still shut, so refresh them.
      if (typeof window.reloadSettings === 'function') window.reloadSettings();
    } catch (err) {
      showError('Could not reach the server.');
    } finally {
      unlockBtn.disabled = false;
      unlockBtn.textContent = 'Unlock';
    }
  }

  function showError(msg) {
    errorBox.textContent = msg;
    errorBox.classList.remove('hidden');
  }

  function clearError() {
    errorBox.textContent = '';
    errorBox.classList.add('hidden');
  }

  // -------------------------------------------------------------------------
  // Backup and restore (#565)
  //
  // A DPAPI vault does not travel, by design. The escape hatch that does not
  // weaken that: the secrets leave under a passphrase the user chooses, and
  // the vault on this machine stays tied to this machine.
  // -------------------------------------------------------------------------

  /** Read a .smv backup off disk. Nothing is uploaded until it is sent. */
  function pickBackupFile() {
    return new Promise((resolve) => {
      const input = document.createElement('input');
      input.type = 'file';
      input.accept = '.smv,application/json';
      input.addEventListener('change', () => {
        const file = input.files && input.files[0];
        if (!file) { resolve(null); return; }
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result || ''));
        reader.onerror = () => resolve(null);
        reader.readAsText(file);
      });
      input.click();
    });
  }

  async function exportBackup() {
    const result = document.getElementById('vault-backup-result');
    const answer = await window.shellmateDialog.form({
      title: 'Export a vault backup',
      body:  'The file holds every API key and every remembered device '
             + 'password. It is encrypted with the passphrase you choose here '
             + 'and nothing else — there is no recovery for it, and no copy of '
             + 'it anywhere.',
      confirmLabel: 'Write the backup',
      fields: [
        { name: 'passphrase', label: 'Passphrase', type: 'password' },
        { name: 'confirm', label: 'Confirm', type: 'password' },
      ],
      validate: (v) => {
        if (!v.passphrase) return 'Choose a passphrase.';
        if (v.passphrase !== v.confirm) return 'The two do not match.';
        return '';
      },
    });
    if (!answer) return;

    showResult(result, 'Encrypting…', false);
    try {
      const res = await fetch('/api/vault/backup', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ passphrase: answer.passphrase }),
      });
      const data = await res.json();
      if (!res.ok) { showResult(result, data.detail || 'Could not write it.', true); return; }
      if (data.cancelled) { showResult(result, '', false); return; }
      status = data.status || status;
      renderSettings();
      showResult(result, `Written to ${data.path}`, false);
    } catch (e) {
      showResult(result, 'Could not reach the server.', true);
    }
  }

  /**
   * Restore a backup into this machine's vault.
   *
   * `forceReplace` is the recovery path: when the vault here cannot be read,
   * merging into it would quietly lose it, so replacing is the only honest
   * option and the dialog says so rather than offering a choice that fails.
   */
  async function importBackup(forceReplace) {
    const result = document.getElementById('vault-backup-result');
    const text = await pickBackupFile();
    if (text === null) return;

    const fields = [{ name: 'passphrase', label: 'Passphrase', type: 'password' }];
    if (!forceReplace) {
      fields.push({
        name: 'mode', label: 'What to do with what is already here',
        type: 'select',
        options: [
          { value: 'merge', label: 'Keep it and add the backup' },
          { value: 'replace', label: 'Replace it with the backup' },
        ],
      });
    }

    const answer = await window.shellmateDialog.form({
      title: 'Import a vault backup',
      body:  forceReplace
        ? 'The vault already here cannot be read on this machine, so it is '
          + 'replaced rather than merged — merging would quietly lose it. The '
          + 'old file is kept beside the new one.'
        : 'A backup made under a master password restores into a vault '
          + 'protected by your Windows account, and the other way round. Where '
          + 'both hold the same key, the backup wins.',
      confirmLabel: 'Restore',
      fields,
      validate: (v) => (v.passphrase ? '' : 'Enter the backup passphrase.'),
    });
    if (!answer) return;

    const replace = forceReplace || answer.mode === 'replace';
    if (replace && !forceReplace) {
      const ok = await window.shellmateDialog.confirm({
        title: 'Replace everything in the vault?',
        body:  'Every key and password currently stored on this machine is '
               + 'discarded and only what is in the backup remains.',
        confirmLabel: 'Replace it', danger: true,
      });
      if (!ok) return;
    }

    if (forceReplace) {
      // The unreadable file is kept, not deleted: unreadable *here* is not
      // unreadable everywhere, and the account that wrote it can still open it.
      try {
        await fetch('/api/vault/set-aside', { method: 'POST' });
      } catch (e) { /* the restore below reports anything that matters */ }
    }

    showResult(result, 'Decrypting…', false);
    try {
      const res = await fetch('/api/vault/restore', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ text, passphrase: answer.passphrase, replace }),
      });
      const data = await res.json();
      if (!res.ok) {
        if (forceReplace) showError(data.detail || 'Could not restore it.');
        showResult(result, data.detail || 'Could not restore it.', true);
        return;
      }
      status = data.status || status;
      renderSettings();
      showResult(result,
        `Restored ${data.restored} — ${data.added} new, ${data.changed} changed.`,
        false);
      hidePrompt();
      if (typeof window.reloadSettings === 'function') window.reloadSettings();
    } catch (e) {
      showResult(result, 'Could not reach the server.', true);
    }
  }

  /** Put an unreadable vault aside and carry on with an empty one. */
  async function startFresh() {
    const ok = await window.shellmateDialog.confirm({
      title: 'Start a new vault?',
      body:  'The vault that is here cannot be read on this machine. It is '
             + 'renamed rather than deleted, so the account that wrote it can '
             + 'still open it, and ShellMate starts a new one. Anything that '
             + 'was in it has to be entered again here.',
      confirmLabel: 'Start a new vault',
    });
    if (!ok) return;
    try {
      const res = await fetch('/api/vault/set-aside', { method: 'POST' });
      const data = await res.json();
      if (!res.ok) { showError(data.detail || 'Could not do that.'); return; }
      status = data.status || status;
      hidePrompt();
      renderSettings();
      if (window.shellmateAlerts) {
        window.shellmateAlerts.notify({
          global: true, icon: 'restore', title: 'A new vault has been started',
          body: data.path ? `The old file is kept as ${data.path}.` : '',
        });
      }
      if (typeof window.reloadSettings === 'function') window.reloadSettings();
    } catch (e) {
      showError('Could not reach the server.');
    }
  }



  window.getVaultStatus     = () => status;
  window.refreshVaultStatus = refreshStatus;
})();
