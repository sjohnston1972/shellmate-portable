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

  /**
   * Read a Stockton value.
   *
   * The advanced settings arrive with everything else on /api/settings, under
   * "advanced", keyed "category.name". Defaults live in backend/advanced.py —
   * the caller passes the same one so a value absent from the file behaves
   * exactly as the backend would make it behave.
   */
  window.shellmateAdvanced = (key, fallback) => {
    const all = ((window.shellmateSettings || {}).advanced) || {};
    return all[key] === undefined ? fallback : all[key];
  };



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

    // On <body> rather than <html>, because the whole layout shifts: the rail
    // widens and its items go from centred to left-aligned.
    document.body.classList.toggle('sidebar-labelled', s.sidebar_labels === true);

    applyTabOrder(s);
    applyPanelTransition(s);

    if (window.shellmateLayout && s.default_layout &&
        window.shellmateLayout.current() !== s.default_layout) {
      window.shellmateLayout.set(s.default_layout);
    }

    applyChatWidth(s.chat_pane_fraction);
  }

  /**
   * How panels arrive: the choice from Settings, the timing from Stockton.
   *
   * Written onto the root as an attribute and two custom properties, so the
   * whole thing lives in CSS. Nothing has to be reapplied to a panel that is
   * already open, and a panel opened before the settings arrived picks up the
   * change the moment it closes.
   */
  const EASING = {
    decelerate: 'cubic-bezier(0.2, 0, 0, 1)',
    standard:   'cubic-bezier(0.4, 0, 0.2, 1)',
    linear:     'linear',
    sharp:      'cubic-bezier(0.4, 0, 0.6, 1)',
  };

  /**
   * Apply the tab ordering.
   *
   * tabs.js owns the strip, so this only hands over the mode. Guarded because
   * prefs applies on load and the tab module may not have registered yet —
   * settings arriving before the thing they configure is normal here, not an
   * error.
   */
  function applyTabOrder(s) {
    if (typeof window.setTabOrder === 'function') {
      window.setTabOrder(s.tab_order || 'manual');
    }
  }

  function applyPanelTransition(s) {
    const root = document.documentElement;
    const choice = ['slide', 'fade', 'scale', 'none']
      .includes(s.panel_transition) ? s.panel_transition : 'slide';
    root.setAttribute('data-panel-transition', choice);

    const advanced = window.shellmateAdvanced || (() => null);
    const inMs  = Number(advanced('files.panel_ms', 180));
    const outMs = Number(advanced('files.panel_ms_out', 140));
    const ease  = EASING[advanced('files.panel_easing', 'decelerate')]
                  || EASING.decelerate;

    root.style.setProperty('--panel-ms', `${Number.isFinite(inMs) ? inMs : 180}ms`);
    root.style.setProperty('--panel-ms-out', `${Number.isFinite(outMs) ? outMs : 140}ms`);
    root.style.setProperty('--panel-ease', ease);
  }

  function applyChatWidth(fraction) {
    const pane = document.getElementById('chat-pane');
    if (!pane || !fraction) return;
    const width = Math.round(window.innerWidth * Number(fraction));
    if (width > 200) pane.style.width = `${width}px`;
  }

  /**
   * Reopen the tabs that were open at quit, once.
   *
   * Bound to settings-loaded rather than to DOMContentLoaded: it needs the
   * preference, and it needs the profile list to match against. Guarded to
   * run once because apply() fires on every save too, and restoring a second
   * set of tabs when somebody changes their theme would be memorable for the
   * wrong reason.
   */
  let restoredOnce = false;
  function restoreOnce() {
    if (restoredOnce) return;
    restoredOnce = true;
    if (typeof window.restoreTabs === 'function') window.restoreTabs();
  }

  document.addEventListener('DOMContentLoaded', () => {
    // settings.js loads and publishes them; this may run before or after.
    if (window.shellmateSettings) { apply(); restoreOnce(); }
    window.addEventListener('shellmate:settings-changed', apply);
    window.addEventListener('shellmate:settings-loaded', () => {
      apply();
      restoreOnce();
    });
  });

  window.shellmatePrefs = { get, set, apply, flush };
  // Named for what callers are doing, not for the module.
  window.saveInterfacePreference = set;
})();
