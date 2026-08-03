/**
 * settings.js — Settings panel for ShellMate.
 * Manages the settings overlay panel, loads/saves settings via the API,
 * and notifies other modules when settings change.
 */
(function () {
  'use strict';

  let panel, overlay;
  let currentSettings = {};

  /**
   * Colour schemes, loaded from the backend (#128).
   *
   * These were seven hardcoded objects here, so a house scheme — or one
   * matching the rest of somebody's tooling — meant editing the source and
   * rebuilding. They live in schemes.json in the data folder now, following
   * platforms.json and snippets.json: data, not code, and they travel with
   * the folder.
   *
   * Seeded with the default alone so that anything asking for a scheme
   * before the fetch returns gets a real theme rather than undefined. A
   * terminal drawn in the wrong colours for 200ms is a far better failure
   * than one that will not draw.
   */
  let COLOR_SCHEMES = {
    deep_space: {
      label: 'Deep Space (Default)',
      theme: {
        background:    '#0E0E0E',
        foreground:    '#E5E2E1',
        cursor:        '#C3C0FF',
        cursorAccent:  '#0E0E0E',
      },
    },
  };

  /** The colour keys a scheme carries, from the backend. */
  let SCHEME_KEYS = [];

  async function loadSchemes() {
    try {
      const res = await fetch('/api/schemes');
      if (!res.ok) return;
      const data = await res.json();
      if (data.schemes && Object.keys(data.schemes).length) {
        COLOR_SCHEMES = data.schemes;
      }
      SCHEME_KEYS = data.keys || [];
      _fillSchemeChoices();
      // Anything already on screen was drawn from the seed.
      window.dispatchEvent(new CustomEvent('shellmate:schemes-loaded'));
    } catch (_) {
      /* the seed is a working scheme; the picker simply offers less */
    }
  }

  /** Keep the scheme picker in step with what actually exists. */
  function _fillSchemeChoices() {
    const select = document.getElementById('setting-color-scheme');
    if (!select) return;
    const chosen = select.value;
    select.innerHTML = '';
    Object.entries(COLOR_SCHEMES).forEach(([value, scheme]) => {
      const option = document.createElement('option');
      option.value = value;
      option.textContent = scheme.label || value;
      select.appendChild(option);
    });
    if (chosen && COLOR_SCHEMES[chosen]) select.value = chosen;
  }

  document.addEventListener('DOMContentLoaded', () => {
    loadSchemes();
    panel   = document.getElementById('settings-panel');
    overlay = document.getElementById('settings-overlay');

    document.getElementById('sidebar-link-settings')
      .addEventListener('click', (e) => { e.preventDefault(); openSettings(); });

    document.getElementById('settings-close')
      .addEventListener('click', closeSettings);

    overlay.addEventListener('click', (e) => {
      if (e.target === overlay) closeSettings();
    });

    document.getElementById('settings-save')
      .addEventListener('click', saveSettings);

    // Diagnostics shortcuts (#222). Settings closes first — both open their
    // own overlay at the same level, and stacking them leaves the one behind
    // unreachable.
    const diagLogs = document.getElementById('diag-open-logs');
    if (diagLogs) diagLogs.addEventListener('click', () => {
      closeSettings();
      if (typeof window.openLogs === 'function') window.openLogs();
    });
    const diagSupport = document.getElementById('diag-open-support');
    if (diagSupport) diagSupport.addEventListener('click', () => {
      closeSettings();
      if (typeof window.openSupport === 'function') window.openSupport();
    });

    // The prompt editor is in this panel now (#135), further down. It was
    // reported as deleted once when it moved, so the signpost stays and
    // simply scrolls rather than opening a second panel.
    const signpost = document.getElementById('open-prompt-editor');
    if (signpost) signpost.addEventListener('click', () => {
      const target = document.getElementById('prompt-editor-block');
      if (target) target.scrollIntoView({ behavior: 'smooth', block: 'start' });
    });

    // Typing a path, or picking one with Browse, updates the line underneath
    // that says where it lands. Browse fires 'input' deliberately (see
    // filepicker.js setValue) so one listener covers both.
    const logDir = document.getElementById('setting-log-dir');
    if (logDir) logDir.addEventListener('input', describeLogDirectory);

    // Wire up color pickers and live preview
    _initColorPickers();

    // Every control in the section that has the preview drives the preview.
    //
    // This was an explicit list of four ids, which is how Cursor Style and
    // Cursor Blink came to do nothing to it — and how the next control added
    // there would have done nothing either. Derived from the section itself,
    // a new row is wired up by existing.
    const previewSection = document.querySelector('.settings-preview')
      ? document.querySelector('.settings-preview').closest('.settings-section')
      : null;
    if (previewSection) {
      previewSection.querySelectorAll('input, select').forEach(el => {
        el.addEventListener('change', _updatePreview);
        // Typing in a number or a font name should show as you type, not on
        // blur. Harmless on the others, which do not fire it.
        el.addEventListener('input', _updatePreview);
      });
    }

    // Wire Chroma "Test connection" button
    const testBtn = document.getElementById('setting-chroma-test');
    if (testBtn) testBtn.addEventListener('click', testChromaConnection);

    // Load settings on startup so terminals start with correct config
    loadSettings();
  });

  async function testChromaConnection() {
    const dot  = document.getElementById('setting-chroma-status-dot');
    const text = document.getElementById('setting-chroma-status-text');
    if (!dot || !text) return;

    // Save current form state first so the test uses the latest URL
    text.textContent = 'Saving and testing…';
    dot.className = 'chroma-dot chroma-dot-unknown';
    try {
      await saveSettings({ keepOpen: true, silent: true });
      const res = await fetch('/api/chroma/health');
      const data = await res.json();
      if (data.ok) {
        dot.className = 'chroma-dot chroma-dot-ok';
        text.textContent = `Connected (${data.url})`;
      } else {
        dot.className = 'chroma-dot chroma-dot-fail';
        text.textContent = data.message || 'Failed';
      }
    } catch (e) {
      dot.className = 'chroma-dot chroma-dot-fail';
      text.textContent = 'Test failed: ' + e;
    }
  }

  function _applyUiFontSize(size) {
    const px = (parseInt(size, 10) || 14) + 'px';
    document.documentElement.style.setProperty('--ui-font-size', px);
  }

  /**
   * Push the user's colour rules into the highlighter.
   *
   * Called on load and after every save so an edited rule takes effect on the
   * next line of output, without reconnecting or reloading the page.
   */
  function _applyHighlightRules(s) {
    if (window.shellmateHighlight) {
      window.shellmateHighlight.setRules(s.highlight || { enabled: false, rules: [] });
    }
  }

  async function loadSettings() {
    try {
      const res = await fetch('/api/settings');
      currentSettings = await res.json();
      window.shellmateSettings = currentSettings;
      _applyHighlightRules(currentSettings);
      _applyUiFontSize((currentSettings.appearance || {}).ui_font_size || 14);
      // Interface preferences — theme, layout, density — are applied by
      // prefs.js, which may have loaded before this resolved.
      window.dispatchEvent(new CustomEvent('shellmate:settings-loaded',
                                           { detail: currentSettings }));
    } catch (e) {
      console.warn('Could not load settings:', e);
    }
  }

  function openSettings() {
    populateForm(currentSettings);
    _populateDiagnostics();
    overlay.classList.remove('hidden');
  }

  function closeSettings() {
    overlay.classList.add('hidden');
  }

  /**
   * Fill the Diagnostics section's read-only rows (#222).
   *
   * Asked on every open rather than once: the history counts move, and the
   * fallback-folder state can change between launches. A failed fetch leaves
   * the em-dash — Diagnostics failing must never break Settings.
   */
  async function _populateDiagnostics() {
    const set = (id, text) => {
      const el = document.getElementById(id);
      if (el && text) el.textContent = text;
    };
    try {
      const info = await (await fetch('/api/system/info')).json();
      set('diag-version', [
        info.app || 'ShellMate',
        info.built ? `built ${info.built}` : '',
        info.portable ? '(portable)' : '',
      ].filter(Boolean).join(' — ').replace(' — (', ' ('));
      set('diag-data-dir', (info.data_dir || '') +
        (info.using_fallback
          ? ' — fallback: the folder beside the executable was not writable'
          : ''));
      set('diag-log-dir', info.log_dir ? `${info.log_dir}` : '');
    } catch (_) { /* rows keep their placeholder */ }
    try {
      const stats = await (await fetch('/api/history/stats')).json();
      const search = (stats.search || '').toUpperCase();
      set('diag-history-stats',
        `${(stats.sessions ?? 0).toLocaleString()} sessions · ` +
        `${(stats.commands ?? 0).toLocaleString()} commands · ` +
        `${(stats.snapshots ?? 0).toLocaleString()} snapshots` +
        (search ? ` · search: ${search}` : ''));
    } catch (_) { /* likewise */ }
  }

  function populateForm(s) {
    const t = s.terminal   || {};
    const l = s.logging    || {};
    const a = s.appearance || {};
    const p = s.providers  || {};
    const env = s.env_preconfigured || {};
    const hasVal = s.providers_has_value || {};

    _val('setting-font-family',      t.font_family      || 'JetBrains Mono, monospace');
    _val('setting-font-size',        t.font_size        || 14);
    _val('setting-line-height',      t.line_height      || 1.2);
    _val('setting-cursor-style',     t.cursor_style     || 'block');
    _val('setting-cursor-width',     t.cursor_width     || 0);
    _val('setting-letter-spacing',   t.letter_spacing   || 0);
    _val('setting-font-weight',      t.font_weight      || 'normal');
    _val('setting-font-weight-bold', t.font_weight_bold || 'bold');
    _val('setting-tab-stop',         t.tab_stop_width   || 8);
    // A colour input has no empty state — it falls back to black, which reads
    // as a deliberate choice of black. The stored value is what decides, and
    // the swatch shows the scheme's own colour when nothing is set, so the
    // picker opens on something sensible rather than on #000000.
    _colourOrScheme('setting-cursor-colour',    t.cursor_colour,    'cursor');
    _colourOrScheme('setting-selection-colour', t.selection_colour, 'selectionBackground');
    _val('setting-scrollback',       t.scrollback_lines || 5000);
    _checked('setting-cursor-blink',      t.cursor_blink      !== false);
    _checked('setting-bold-bright',       t.draw_bold_in_bright !== false);
    _checked('setting-screen-reader',     !!t.screen_reader_mode);
    _checked('setting-right-click-paste', t.right_click_paste !== false);
    _checked('setting-copy-on-select',    !!t.copy_on_select);
    _checked('setting-expand-aliases',    t.expand_aliases  !== false);
    _checked('setting-auto-paging',       t.auto_paging_off !== false);
    _checked('setting-keep-alive',        t.keep_alive === true);
    _val('setting-keep-alive-seconds',    t.keep_alive_seconds || 120);
    // Off unless explicitly on — the assistant is opt-in on a fresh install.
    // The row lives under Interface now, but the value stays under `ai` where
    // ai_panel.js and LEGACY_DEFAULTS already read it: moving a control is not
    // a reason to migrate everyone's settings file.
    _checked('setting-ai-enabled',        (s.ai || {}).panel_enabled === true);

    // Colour rules are a repeating row, so their editor owns the DOM.
    const h = s.highlight || {};
    _checked('setting-highlight-enabled', h.enabled !== false);
    if (window.highlightRulesEditor) {
      window.highlightRulesEditor.render(
        h.rules && h.rules.length ? h.rules : window.highlightRulesEditor.DEFAULT_RULES);
    }
    _checked('setting-logging-enabled',   !!l.enabled);
    _checked('setting-redact-secrets',    l.redact_secrets !== false);

    // Configuration capture. Capture and the diff have always happened, so
    // they default on; writing files to somewhere of the user's choosing is
    // the part that waits to be asked for.
    _checked('setting-capture-configs',   l.capture_configs !== false);
    _checked('setting-diff-on-connect',   l.diff_on_connect !== false);
    _checked('setting-save-config-files', !!l.save_config_files);
    _val('setting-config-dir',   l.config_directory      || 'configs');
    _val('setting-config-keep',  l.config_keep_per_device ?? 20);
    _val('setting-config-age',   l.config_max_age_days    ?? 365);
    _val('setting-config-size',  l.config_max_total_mb    ?? 200);
    describeArchive();
    describeLogDirectory();

    const al = s.alerts || {};
    _checked('setting-alert-flash',  al.flash_tab     !== false);
    _checked('setting-alert-sound',  al.sound         !== false);
    _checked('setting-alert-popup',  al.popup         !== false);
    _checked('setting-reduce-motion', !!al.reduce_motion);

    const ui = s.interface || {};
    _val('setting-theme',             ui.theme || 'dark');
    _val('setting-density',           ui.density || 'comfortable');
    _val('setting-panel-transition',  ui.panel_transition || 'slide');
    _val('setting-tab-label-width',   ui.max_tab_label_px || 160);
    _checked('setting-connection-dot',    ui.show_connection_dot !== false);
    _checked('setting-sidebar-labels',    ui.sidebar_labels === true);
    _val('setting-font-scale',            ui.font_scale || 1);
    _val('setting-toast-position',        ui.toast_position || 'bottom-right');
    _val('setting-chat-enter',            ui.chat_enter || 'send');
    _renderTabMenuToggles(ui.tab_menu || {});
    _checked('setting-restore-tabs',      ui.restore_tabs === true);
    _val('setting-new-tab-opens',         ui.new_tab_opens || 'welcome');
    _fillProfileChoices(ui.new_tab_profile || '');
    _val('setting-tab-order',             ui.tab_order || 'manual');
    _checked('setting-confirm-close-tab', ui.confirm_close_tab !== false);
    _checked('setting-confirm-quit',      ui.confirm_quit !== false);

    const win = s.window || {};
    _checked('setting-start-minimised', !!win.start_minimised);
    // Remembering is expressed as "is a size stored", which is what the user
    // means by it — there is no separate flag to fall out of step with.
    _checked('setting-remember-window', !!win.width);

    const ser = s.serial || {};
    _val('setting-serial-baud',      ser.baud_rate    || 9600);
    _val('setting-serial-databits',  ser.data_bits    || 8);
    _val('setting-serial-parity',    ser.parity       || 'N');
    _val('setting-serial-stopbits',  ser.stop_bits    || 1);
    _val('setting-serial-flow',      ser.flow_control || 'none');
    _val('setting-log-dir',          l.directory || 'logs');
    _val('setting-color-scheme',     a.color_scheme  || 'deep_space');
    _val('setting-ui-font-size',     a.ui_font_size  || 14);

    // Provider fields. The backend masks secrets — we show the masked value
    // so the user can tell something is saved, and provide a hint placeholder
    // when the env var is the active source.
    _populateProviderField('setting-anthropic-key', p.anthropic_api_key, hasVal.anthropic_api_key, env.anthropic_api_key);
    _populateProviderField('setting-openai-key',    p.openai_api_key,    hasVal.openai_api_key,    env.openai_api_key);
    _populateProviderField('setting-xai-key',       p.xai_api_key,       hasVal.xai_api_key,       env.xai_api_key);
    _populateProviderField('setting-deepseek-key',  p.deepseek_api_key,  hasVal.deepseek_api_key,  env.deepseek_api_key);
    _populateProviderField('setting-ollama-host',   p.ollama_host,       hasVal.ollama_host,       env.ollama_host);
    _populateProviderField('setting-chroma-url',        p.chroma_url,        hasVal.chroma_url,        env.chroma_url);
    _populateProviderField('setting-chroma-collection', p.chroma_collection, hasVal.chroma_collection, env.chroma_collection);

    // Populate color overrides — show scheme defaults if no override saved
    const schemeName = a.color_scheme || 'deep_space';
    const schemeTheme = (COLOR_SCHEMES[schemeName] || COLOR_SCHEMES.deep_space).theme;
    _setColorField('setting-fg-hex', 'setting-fg-swatch-inner', a.foreground_override || schemeTheme.foreground);
    _setColorField('setting-bg-hex', 'setting-bg-swatch-inner', a.background_override || schemeTheme.background);

    _updatePreview();
  }

  /**
   * Populate a provider-key field. If the user has explicitly set a value in
   * settings, show it (masked for secrets, plain for URLs). Otherwise:
   *   - if the matching env var is set, show "Already preconfigured by env variable" placeholder
   *   - else show the field's normal placeholder (left as-is in HTML)
   */
  function _populateProviderField(elId, value, hasUserValue, envPreconfigured) {
    const el = document.getElementById(elId);
    if (!el) return;
    if (hasUserValue) {
      el.value = value || '';
      el.placeholder = '';
    } else {
      el.value = '';
      if (envPreconfigured) {
        el.placeholder = 'Already preconfigured by env variable';
      }
    }
    // Stash the original masked value so we can detect "no change" on save
    el.dataset.maskedOriginal = hasUserValue ? (value || '') : '';
  }

  async function saveSettings(opts) {
    opts = opts || {};
    const schemeName = _gval('setting-color-scheme');
    const schemeTheme = (COLOR_SCHEMES[schemeName] || COLOR_SCHEMES.deep_space).theme;
    const fgHex = _gval('setting-fg-hex').trim();
    const bgHex = _gval('setting-bg-hex').trim();

    const s = {
      terminal: {
        font_family:       _gval('setting-font-family'),
        font_size:         parseInt(_gval('setting-font-size'), 10),
        line_height:       parseFloat(_gval('setting-line-height')),
        cursor_style:      _gval('setting-cursor-style'),
        cursor_blink:      _gchecked('setting-cursor-blink'),
        cursor_width:      parseInt(_gval('setting-cursor-width'), 10) || 0,
        cursor_colour:     _clearedColour('setting-cursor-colour'),
        selection_colour:  _clearedColour('setting-selection-colour'),
        letter_spacing:    parseFloat(_gval('setting-letter-spacing')) || 0,
        font_weight:       _gval('setting-font-weight'),
        font_weight_bold:  _gval('setting-font-weight-bold'),
        tab_stop_width:    parseInt(_gval('setting-tab-stop'), 10) || 8,
        draw_bold_in_bright: _gchecked('setting-bold-bright'),
        screen_reader_mode:  _gchecked('setting-screen-reader'),
        scrollback_lines:  parseInt(_gval('setting-scrollback'), 10),
        right_click_paste: _gchecked('setting-right-click-paste'),
        copy_on_select:    _gchecked('setting-copy-on-select'),
        expand_aliases:    _gchecked('setting-expand-aliases'),
        auto_paging_off:   _gchecked('setting-auto-paging'),
        keep_alive:        _gchecked('setting-keep-alive'),
        keep_alive_seconds: parseInt(_gval('setting-keep-alive-seconds'), 10) || 120,
      },
      ai: {
        panel_enabled: _gchecked('setting-ai-enabled'),
      },
      highlight: {
        enabled: _gchecked('setting-highlight-enabled'),
        rules:   window.highlightRulesEditor
          ? window.highlightRulesEditor.collect() : [],
      },
      logging: {
        enabled:        _gchecked('setting-logging-enabled'),
        directory:      _gval('setting-log-dir'),
        redact_secrets: _gchecked('setting-redact-secrets'),

        capture_configs:        _gchecked('setting-capture-configs'),
        diff_on_connect:        _gchecked('setting-diff-on-connect'),
        save_config_files:      _gchecked('setting-save-config-files'),
        config_directory:       _gval('setting-config-dir') || 'configs',
        // Zero is a meaningful answer for all three — "no limit" — so an
        // empty box must not silently become the default instead.
        config_keep_per_device: _int('setting-config-keep', 20),
        config_max_age_days:    _int('setting-config-age', 365),
        config_max_total_mb:    _int('setting-config-size', 200),
      },
      alerts: {
        flash_tab:     _gchecked('setting-alert-flash'),
        sound:         _gchecked('setting-alert-sound'),
        popup:         _gchecked('setting-alert-popup'),
        reduce_motion: _gchecked('setting-reduce-motion'),
      },
      interface: {
        theme:               _gval('setting-theme') || 'dark',
        density:             _gval('setting-density') || 'comfortable',
        panel_transition:    _gval('setting-panel-transition') || 'slide',
        max_tab_label_px:    parseInt(_gval('setting-tab-label-width'), 10) || 160,
        show_connection_dot: _gchecked('setting-connection-dot'),
        sidebar_labels:      _gchecked('setting-sidebar-labels'),
        font_scale:          parseFloat(_gval('setting-font-scale')) || 1,
        toast_position:      _gval('setting-toast-position'),
        chat_enter:          _gval('setting-chat-enter'),
        tab_menu:            _collectTabMenu(),
        restore_tabs:        _gchecked('setting-restore-tabs'),
        new_tab_opens:       _gval('setting-new-tab-opens'),
        new_tab_profile:     _gval('setting-new-tab-profile'),
        tab_order:           _gval('setting-tab-order'),
        confirm_close_tab:   _gchecked('setting-confirm-close-tab'),
        confirm_quit:        _gchecked('setting-confirm-quit'),
      },
      window: {
        start_minimised: _gchecked('setting-start-minimised'),
        // Unticking it clears what was stored, so the next launch uses the
        // default size rather than silently restoring a stale one.
        ...(_gchecked('setting-remember-window')
          ? {}
          : { width: 0, height: 0, x: null, y: null }),
      },
      serial: {
        baud_rate:    parseInt(_gval('setting-serial-baud'), 10) || 9600,
        data_bits:    parseInt(_gval('setting-serial-databits'), 10) || 8,
        parity:       _gval('setting-serial-parity') || 'N',
        stop_bits:    parseFloat(_gval('setting-serial-stopbits')) || 1,
        flow_control: _gval('setting-serial-flow') || 'none',
      },
      appearance: {
        color_scheme:        schemeName,
        ui_font_size:        parseInt(_gval('setting-ui-font-size'), 10) || 14,
        // Only store override if it differs from scheme default
        foreground_override: (_isValidHex(fgHex) && fgHex.toLowerCase() !== schemeTheme.foreground.toLowerCase()) ? fgHex : null,
        background_override: (_isValidHex(bgHex) && bgHex.toLowerCase() !== schemeTheme.background.toLowerCase()) ? bgHex : null,
      },
      providers: _collectProviders(),
    };

    try {
      const res = await fetch('/api/settings', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ settings: s }),
      });
      currentSettings = await res.json();
      window.shellmateSettings = currentSettings;
      _applyHighlightRules(currentSettings);
      _applyUiFontSize((currentSettings.appearance || {}).ui_font_size || 14);
      // Notify terminal.js to apply new settings
      window.dispatchEvent(new CustomEvent('shellmate:settings-changed', { detail: currentSettings }));
      // Refresh masked-original markers so subsequent edits are detected correctly
      populateForm(currentSettings);
      // A changed key or host changes what the providers can offer, so
      // re-discover straight away rather than waiting for someone to press
      // the test button and wonder why the new key made no difference.
      if (Object.keys(s.providers || {}).length &&
          typeof window.refreshProviderModels === 'function') {
        window.refreshProviderModels();
      }
      if (!opts.keepOpen) closeSettings();
    } catch (e) {
      console.error('Failed to save settings:', e);
    }
  }

  /**
   * Build the providers payload from the form. For each field:
   *   - if the user typed nothing → omit (backend keeps existing or falls back to env)
   *   - if the user typed the masked placeholder → omit (no real change)
   *   - otherwise → send the new value
   */
  function _collectProviders() {
    const fields = [
      { id: 'setting-anthropic-key',      key: 'anthropic_api_key' },
      { id: 'setting-openai-key',         key: 'openai_api_key' },
      { id: 'setting-xai-key',            key: 'xai_api_key' },
      { id: 'setting-deepseek-key',       key: 'deepseek_api_key' },
      { id: 'setting-ollama-host',        key: 'ollama_host' },
      { id: 'setting-chroma-url',         key: 'chroma_url' },
      { id: 'setting-chroma-collection',  key: 'chroma_collection' },
    ];
    const out = {};
    fields.forEach(({ id, key }) => {
      const el = document.getElementById(id);
      if (!el) return;
      const v = (el.value || '').trim();
      const original = (el.dataset.maskedOriginal || '').trim();
      // Empty input — only send (as "") if there *was* a stored value being cleared
      if (!v) {
        if (original) out[key] = ''; // explicit clear
        return;
      }
      // Identical to masked-original placeholder → no change, omit
      if (v === original) return;
      out[key] = v;
    });
    return out;
  }

  /**
   * Say where captures are going and what is already there.
   *
   * "configs" in a text box tells nobody where their files actually are —
   * it resolves against the data folder, which itself moves depending on
   * whether the portable location was writable.
   */
  /**
   * Show where the log directory setting actually points.
   *
   * Same reason as describeArchive(): a relative name resolves against the
   * data folder, and the data folder moves depending on whether the location
   * beside the executable was writable. Neither is visible from the text box.
   */
  async function describeLogDirectory() {
    const el    = document.getElementById('log-dir-resolved');
    const field = document.getElementById('setting-log-dir');
    if (!el || !field) return;

    const value = (field.value || '').trim() || 'logs';
    try {
      const res = await fetch(`/api/local/resolve?path=${encodeURIComponent(value)}`);
      if (!res.ok) return;
      const info = await res.json();
      el.textContent = info.exists
        ? info.resolved
        : `${info.resolved} — created when the first session is logged`;
    } catch (e) {
      /* describing the folder is a nicety, not a reason to fail */
    }
  }

  async function describeArchive() {
    const el = document.getElementById('config-archive-status');
    if (!el) return;
    try {
      const res = await fetch('/api/configs/archive');
      if (!res.ok) return;
      const info = await res.json();
      if (!info.exists || !info.captures) {
        el.textContent = `Nothing saved yet. Files will go to ${info.path}`;
        return;
      }
      const mb = (info.bytes / (1024 * 1024)).toFixed(1);
      el.textContent =
        `${info.captures} capture${info.captures === 1 ? '' : 's'} from ` +
        `${info.devices} device${info.devices === 1 ? '' : 's'}, ${mb} MB, in ${info.path}`;
    } catch (e) {
      /* the folder is a nicety to describe, not a reason to fail */
    }
  }

  function _int(id, fallback) {
    const raw = _gval(id);
    const value = parseInt(raw, 10);
    return raw === '' || Number.isNaN(value) ? fallback : Math.max(0, value);
  }

  function _val(id, v)     { const el = document.getElementById(id); if (el) el.value = v; }
  function _checked(id, v) { const el = document.getElementById(id); if (el) el.checked = !!v; }
  function _gval(id)       { const el = document.getElementById(id); return el ? el.value : ''; }
  function _gchecked(id)   { const el = document.getElementById(id); return el ? el.checked : false; }

  /**
   * Show a colour override, or the scheme's own colour when there is none.
   *
   * `<input type="color">` has no empty state: with no value it shows
   * #000000, which is indistinguishable from somebody deliberately choosing
   * black. So "unset" is carried on the element rather than in its value, and
   * the swatch is filled with whatever the active scheme uses — which is both
   * truthful about what will be drawn and a sane place for the picker to open.
   */
  function _colourOrScheme(id, stored, themeKey) {
    const el = document.getElementById(id);
    if (!el) return;

    if (_isValidHex(stored || '')) {
      el.value = stored;
      delete el.dataset.unset;
      return;
    }

    const scheme = COLOR_SCHEMES[_gval('setting-color-scheme')] || COLOR_SCHEMES.deep_space;
    const fromScheme = scheme && scheme.theme && scheme.theme[themeKey];
    el.value = _isValidHex(fromScheme || '') ? fromScheme : '#c3c0ff';
    el.dataset.unset = '1';
  }

  /** The override, or "" when the row is still following the scheme. */
  function _clearedColour(id) {
    const el = document.getElementById(id);
    if (!el || el.dataset.unset) return '';
    return _isValidHex(el.value) ? el.value : '';
  }

  // Choosing a colour is what makes it an override; "Use scheme" undoes that.
  document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('.setting-colour').forEach(el => {
      el.addEventListener('input', () => { delete el.dataset.unset; });
    });
    document.querySelectorAll('.setting-clear').forEach(button => {
      button.addEventListener('click', () => {
        const el = document.getElementById(button.dataset.clears);
        if (el) el.dataset.unset = '1';
      });
    });
  });

  /**
   * Fill the saved-connection picker, and show it only when it applies.
   *
   * Listed from /api/profiles rather than kept in step with a copy: the
   * connection somebody picks here can be renamed or deleted from three other
   * places, and a stale list would offer one that no longer exists.
   */
  async function _fillProfileChoices(selected) {
    const select = document.getElementById('setting-new-tab-profile');
    if (!select) return;

    select.innerHTML = '';
    try {
      const res = await fetch('/api/profiles');
      const data = res.ok ? await res.json() : { profiles: [] };
      (data.profiles || []).forEach(profile => {
        const option = document.createElement('option');
        option.value = profile.id;
        option.textContent = profile.name
          + (profile.hostname ? ` (${profile.hostname})` : '');
        select.appendChild(option);
      });
      if (!select.options.length) {
        const option = document.createElement('option');
        option.value = '';
        option.textContent = 'No saved connections yet';
        select.appendChild(option);
      }
    } catch (_) { /* the row simply offers nothing */ }

    if (selected) select.value = selected;
    _syncNewTabRow();
  }

  function _syncNewTabRow() {
    const row = document.getElementById('new-tab-profile-row');
    if (row) {
      row.style.display =
        _gval('setting-new-tab-opens') === 'profile' ? '' : 'none';
    }
  }

  document.addEventListener('DOMContentLoaded', () => {
    const mode = document.getElementById('setting-new-tab-opens');
    if (mode) mode.addEventListener('change', _syncNewTabRow);
  });

  /**
   * The tab-menu toggles, rendered from the menu's own list.
   *
   * tabs.js owns what the menu can offer; this asks it. Two hand-maintained
   * lists of the same entries would drift, and the drift would be silent —
   * a toggle for an entry that no longer exists, or an entry with no toggle.
   */
  function _renderTabMenuToggles(current) {
    const host = document.getElementById('tab-menu-toggles');
    if (!host) return;
    host.innerHTML = '';

    const items = typeof window.tabMenuItems === 'function'
      ? window.tabMenuItems() : [];

    items.forEach(item => {
      const row = document.createElement('label');
      row.className = 'tab-menu-toggle';

      const box = document.createElement('input');
      box.type = 'checkbox';
      box.dataset.menuSetting = item.setting;
      // Absent means on, matching the menu's own reading of it.
      box.checked = current[item.setting] !== false;

      const text = document.createElement('span');
      text.textContent = item.label;

      row.append(box, text);
      host.appendChild(row);
    });
  }

  function _collectTabMenu() {
    const out = {};
    document.querySelectorAll('#tab-menu-toggles input[type=checkbox]')
      .forEach(box => { out[box.dataset.menuSetting] = box.checked; });
    return out;
  }

  function _isValidHex(h) { return /^#[0-9A-Fa-f]{6}$/.test(h); }

  function _setColorField(hexId, swatchInnerId, color) {
    const hexEl    = document.getElementById(hexId);
    const swatchEl = document.getElementById(swatchInnerId);
    if (hexEl)    hexEl.value = color || '';
    if (swatchEl) swatchEl.style.background = _isValidHex(color) ? color : '#888';
  }

  // Update the live preview pane from current form values
  function _updatePreview() {
    const previewEl = document.getElementById('settings-preview-terminal');
    const previewPre = document.getElementById('settings-preview-text');
    if (!previewEl || !previewPre) return;

    const schemeName  = _gval('setting-color-scheme') || 'deep_space';
    const schemeTheme = (COLOR_SCHEMES[schemeName] || COLOR_SCHEMES.deep_space).theme;
    const fgHex = _gval('setting-fg-hex').trim();
    const bgHex = _gval('setting-bg-hex').trim();

    const fg = _isValidHex(fgHex) ? fgHex : schemeTheme.foreground;
    const bg = _isValidHex(bgHex) ? bgHex : schemeTheme.background;

    const fontFamily = _gval('setting-font-family') || 'JetBrains Mono, monospace';
    const fontSize   = parseInt(_gval('setting-font-size'), 10) || 14;
    const lineHeight = parseFloat(_gval('setting-line-height')) || 1.2;

    previewEl.style.background = bg;
    previewEl.style.color      = fg;

    // The cursor. Attribute for the shape, class for the blink — so the
    // rules stay in the stylesheet with the rest of the appearance.
    previewEl.dataset.cursor = _gval('setting-cursor-style') || 'block';
    previewEl.classList.toggle('preview-blink', _gchecked('setting-cursor-blink'));
    previewPre.style.fontFamily  = fontFamily;
    previewPre.style.fontSize    = `${fontSize}px`;
    previewPre.style.lineHeight  = String(lineHeight);
    // Prompt colour from scheme blue
    previewEl.querySelectorAll('.preview-prompt').forEach(el => {
      el.style.color = schemeTheme.blue || '#89b4fa';
    });
  }

  // Wire up color picker swatches and hex inputs
  function _initColorPickers() {
    // For each color field: swatch div opens a hidden <input type="color">
    [
      { swatchId: 'setting-fg-swatch', swatchInnerId: 'setting-fg-swatch-inner', hexId: 'setting-fg-hex', resetId: 'setting-fg-reset', schemeKey: 'foreground' },
      { swatchId: 'setting-bg-swatch', swatchInnerId: 'setting-bg-swatch-inner', hexId: 'setting-bg-hex', resetId: 'setting-bg-reset', schemeKey: 'background' },
    ].forEach(({ swatchId, swatchInnerId, hexId, resetId, schemeKey }) => {
      const swatchEl      = document.getElementById(swatchId);
      const swatchInnerEl = document.getElementById(swatchInnerId);
      const hexEl         = document.getElementById(hexId);
      const resetBtn      = document.getElementById(resetId);

      if (!swatchEl || !hexEl) return;

      // Create hidden native color input attached to the swatch
      const colorInput = document.createElement('input');
      colorInput.type = 'color';
      colorInput.style.cssText = 'position:absolute;width:1px;height:1px;opacity:0;pointer-events:none;';
      document.body.appendChild(colorInput);

      swatchEl.addEventListener('click', () => {
        colorInput.value = _isValidHex(hexEl.value) ? hexEl.value : '#888888';
        colorInput.click();
      });

      colorInput.addEventListener('input', () => {
        hexEl.value = colorInput.value.toUpperCase();
        if (swatchInnerEl) swatchInnerEl.style.background = colorInput.value;
        _updatePreview();
      });

      // Hex input: update swatch on valid input
      hexEl.addEventListener('input', () => {
        const v = hexEl.value.trim();
        if (_isValidHex(v)) {
          if (swatchInnerEl) swatchInnerEl.style.background = v;
          _updatePreview();
        }
      });

      // Reset: restore scheme default
      resetBtn.addEventListener('click', () => {
        const schemeName  = _gval('setting-color-scheme') || 'deep_space';
        const schemeTheme = (COLOR_SCHEMES[schemeName] || COLOR_SCHEMES.deep_space).theme;
        const defaultColor = schemeTheme[schemeKey];
        hexEl.value = defaultColor;
        if (swatchInnerEl) swatchInnerEl.style.background = defaultColor;
        _updatePreview();
      });
    });

    // When scheme changes, update swatch colours to scheme defaults (unless user has overridden)
    document.getElementById('setting-color-scheme').addEventListener('change', () => {
      const schemeName  = _gval('setting-color-scheme') || 'deep_space';
      const schemeTheme = (COLOR_SCHEMES[schemeName] || COLOR_SCHEMES.deep_space).theme;
      // Reset to new scheme defaults
      _setColorField('setting-fg-hex', 'setting-fg-swatch-inner', schemeTheme.foreground);
      _setColorField('setting-bg-hex', 'setting-bg-swatch-inner', schemeTheme.background);
      _updatePreview();
    });
  }

  // Public API
  window.getColorScheme     = (name) => COLOR_SCHEMES[name] || COLOR_SCHEMES.deep_space;
  // The names and labels, so the per-tab picker offers exactly what the
  // global one does rather than keeping its own list to fall out of step.
  window.colorSchemeList    = () => Object.entries(COLOR_SCHEMES)
    .map(([value, scheme]) => ({ value, label: scheme.label || value }));
  window.getAllColorSchemes  = () => COLOR_SCHEMES;
  window.openSettings       = openSettings;
  window.reloadSettings     = loadSettings;

})();
