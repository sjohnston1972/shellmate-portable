/**
 * mode.js — Learn / Troubleshoot mode toggle.
 *
 * The mode controls which AI persona is activated by the backend:
 *   - "tshoot" (default): terse senior engineer focused on fixing the problem.
 *   - "learn": patient mentor who explains the why before suggesting a command.
 *
 * The active mode is sent to the backend with every chat message (via
 * chat.js). A chat-header pill mirrors the welcome-screen toggle so the user
 * can flip modes mid-session.
 *
 * It used to be stored in localStorage *and* in settings.json under `ai.mode`,
 * with nothing to reconcile the two — so the pill and the settings panel could
 * disagree about which persona was active, and which one won depended on
 * whichever was read last. settings.json is now the only home.
 */
(function () {
  'use strict';

  const VALID_MODES = ['tshoot', 'learn'];

  let _currentMode = 'tshoot';

  function _fromSettings() {
    const stored = ((window.shellmateSettings || {}).ai || {}).mode;
    return VALID_MODES.includes(stored) ? stored : 'tshoot';
  }

  function getMode() { return _currentMode; }

  function setMode(mode, options) {
    if (!VALID_MODES.includes(mode)) return;
    _currentMode = mode;

    if (!(options && options.fromSettings)) {
      fetch('/api/settings', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        // Partial: the store deep-merges, so this touches nothing else.
        body:    JSON.stringify({ settings: { ai: { mode } } }),
      }).catch((e) => console.warn('Could not save the assistant mode:', e));

      window.shellmateSettings = window.shellmateSettings || {};
      window.shellmateSettings.ai =
        Object.assign({}, window.shellmateSettings.ai, { mode });
    }

    _refreshUI();
    window.dispatchEvent(new CustomEvent('shellmate:mode-changed', { detail: { mode } }));
  }

  const LABELS = { tshoot: 'Tshoot', learn: 'Learn' };

  function _refreshUI() {
    const btn  = document.getElementById('mode-toggle-btn');
    const text = document.getElementById('mode-toggle-text');
    if (btn)  btn.dataset.mode = _currentMode;
    if (text) text.textContent = LABELS[_currentMode] || LABELS.tshoot;
    if (btn) {
      const next = _currentMode === 'tshoot' ? 'Learn' : 'Troubleshoot';
      btn.title = `Active mode: ${LABELS[_currentMode]}. Click to switch to ${next}.`;
    }
  }

  document.addEventListener('DOMContentLoaded', () => {
    const btn = document.getElementById('mode-toggle-btn');
    if (btn) {
      btn.addEventListener('click', (e) => {
        e.preventDefault();
        setMode(_currentMode === 'tshoot' ? 'learn' : 'tshoot');
      });
    }

    // Settings may already have loaded, or may not have yet.
    const adopt = () => setMode(_fromSettings(), { fromSettings: true });
    if (window.shellmateSettings) adopt();
    window.addEventListener('shellmate:settings-loaded', adopt);
    window.addEventListener('shellmate:settings-changed', adopt);

    _refreshUI();
  });

  // Public API used by chat.js
  window.getShellmateMode = getMode;
  window.setShellmateMode = setMode;
})();
