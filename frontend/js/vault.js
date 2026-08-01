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
  let status = { exists: false, mode: 'none', locked: false, dpapi_available: false };

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

    refreshStatus().then(() => {
      if (status.locked) showPrompt();
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
    overlay.classList.remove('hidden');
    setTimeout(() => passwordInput.focus(), 50);
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

  window.getVaultStatus     = () => status;
  window.refreshVaultStatus = refreshStatus;
})();
