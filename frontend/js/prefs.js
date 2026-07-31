/**
 * prefs.js — Interface preferences that used to live in localStorage.
 *
 * The theme, the split layout, the chat pane width, the quick buttons and the
 * pop-out geometry were all kept in the browser's own storage. That quietly
 * broke a promise the manual makes plainly: *move the ShellMate-Data folder
 * and your setup moves with it*. Under the native window they did travel,
 * because pywebview's storage sits inside that folder — but open
 * http://localhost:8765 in a browser and you got an entirely different set of
 * preferences, with nothing to reconcile the two.
 *
 * So they live in settings.json now, alongside everything else the user has
 * chosen, and this module is the one place that reads and writes them.
 *
 * Writes are batched. Dragging the split divider produces a preference change
 * on every animation frame, and a POST per frame would be absurd.
 */
(function () {
  'use strict';

  const SAVE_DELAY_MS = 600;

  let pending = {};
  let timer = null;

  /** Current interface settings, or the defaults before they have loaded. */
  function all() {
    return (window.shellmateSettings || {}).interface || {};
  }

  function get(key, fallback) {
    const value = all()[key];
    return value === undefined || value === null ? fallback : value;
  }

  /**
   * Record a preference and schedule a write.
   *
   * The in-memory copy is updated immediately so anything reading it back
   * within the debounce window sees the new value rather than the old one.
   */
  function set(key, value) {
    window.shellmateSettings = window.shellmateSettings || {};
    window.shellmateSettings.interface =
      Object.assign({}, window.shellmateSettings.interface, { [key]: value });

    pending[key] = value;
    if (timer) clearTimeout(timer);
    timer = setTimeout(flush, SAVE_DELAY_MS);
  }

  async function flush() {
    timer = null;
    const body = pending;
    pending = {};
    if (!Object.keys(body).length) return;

    try {
      const res = await fetch('/api/settings', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        // The endpoint takes {settings: ...}, and the store deep-merges — so a
        // partial like this cannot clobber a setting owned elsewhere.
        body:    JSON.stringify({ settings: { interface: body } }),
      });
      // A preference that fails to save is not worth interrupting anyone over,
      // but it should not vanish silently either: a wrong body shape would
      // otherwise look exactly like everything working.
      if (!res.ok) console.warn('Could not save preferences:', res.status);
    } catch (e) {
      console.warn('Could not save preferences:', e);
    }
  }

  // Anything unsaved when the page goes away would otherwise be lost inside
  // the debounce window.
  window.addEventListener('beforeunload', () => {
    if (timer) { clearTimeout(timer); flush(); }
  });

  /**
   * Apply the stored preferences once settings have arrived.
   *
   * Fired on load and again whenever settings are saved, so changing the
   * theme in the settings panel takes effect without a reload.
   */
  function apply() {
    const s = all();

    if (window.shellmateTheme) window.shellmateTheme.apply(s.theme || 'dark');

    document.documentElement.setAttribute(
      'data-density', s.density === 'compact' ? 'compact' : 'comfortable');

    document.documentElement.style.setProperty(
      '--tab-label-max', `${Number(s.max_tab_label_px) || 160}px`);

    document.documentElement.classList.toggle(
      'hide-connection-dot', s.show_connection_dot === false);

    if (window.shellmateLayout && s.default_layout &&
        window.shellmateLayout.current() !== s.default_layout) {
      window.shellmateLayout.set(s.default_layout);
    }

    applyChatWidth(s.chat_pane_fraction);
  }

  function applyChatWidth(fraction) {
    const pane = document.getElementById('chat-pane');
    if (!pane || !fraction) return;
    const width = Math.round(window.innerWidth * Number(fraction));
    if (width > 200) pane.style.width = `${width}px`;
  }

  document.addEventListener('DOMContentLoaded', () => {
    // settings.js loads and publishes them; this may run before or after.
    if (window.shellmateSettings) apply();
    window.addEventListener('shellmate:settings-changed', apply);
    window.addEventListener('shellmate:settings-loaded', apply);
  });

  window.shellmatePrefs = { get, set, apply, flush };
  // Named for what callers are doing, not for the module.
  window.saveInterfacePreference = set;
})();
