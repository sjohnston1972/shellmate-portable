/**
 * settings.js — Settings panel for ShellMate.
 * Manages the settings overlay panel, loads/saves settings via the API,
 * and notifies other modules when settings change.
 */
(function () {
  'use strict';

  let panel, overlay;
  let currentSettings = {};

  // Color scheme definitions (xterm.js theme objects)
  const COLOR_SCHEMES = {
    deep_space: {
      label: 'Deep Space (Default)',
      theme: {
        background:    '#0E0E0E',
        foreground:    '#E5E2E1',
        cursor:        '#C3C0FF',
        cursorAccent:  '#0E0E0E',
        black:         '#2A2A2A',
        red:           '#FFB4AB',
        green:         '#B7C8E1',
        yellow:        '#F9E2AF',
        blue:          '#C3C0FF',
        magenta:       '#CBA6F7',
        cyan:          '#89DCEB',
        white:         '#E5E2E1',
        brightBlack:   '#353535',
        brightRed:     '#FFB4AB',
        brightGreen:   '#B7C8E1',
        brightYellow:  '#F9E2AF',
        brightBlue:    '#C3C0FF',
        brightMagenta: '#CBA6F7',
        brightCyan:    '#89DCEB',
        brightWhite:   '#FFFFFF',
      },
    },
    solarized_dark: {
      label: 'Solarized Dark',
      theme: {
        background:    '#002B36',
        foreground:    '#839496',
        cursor:        '#839496',
        cursorAccent:  '#002B36',
        black:         '#073642',
        red:           '#DC322F',
        green:         '#859900',
        yellow:        '#B58900',
        blue:          '#268BD2',
        magenta:       '#D33682',
        cyan:          '#2AA198',
        white:         '#EEE8D5',
        brightBlack:   '#002B36',
        brightRed:     '#CB4B16',
        brightGreen:   '#586E75',
        brightYellow:  '#657B83',
        brightBlue:    '#839496',
        brightMagenta: '#6C71C4',
        brightCyan:    '#93A1A1',
        brightWhite:   '#FDF6E3',
      },
    },
    nord: {
      label: 'Nord',
      theme: {
        background:    '#2E3440',
        foreground:    '#D8DEE9',
        cursor:        '#D8DEE9',
        cursorAccent:  '#2E3440',
        black:         '#3B4252',
        red:           '#BF616A',
        green:         '#A3BE8C',
        yellow:        '#EBCB8B',
        blue:          '#81A1C1',
        magenta:       '#B48EAD',
        cyan:          '#88C0D0',
        white:         '#E5E9F0',
        brightBlack:   '#4C566A',
        brightRed:     '#BF616A',
        brightGreen:   '#A3BE8C',
        brightYellow:  '#EBCB8B',
        brightBlue:    '#81A1C1',
        brightMagenta: '#B48EAD',
        brightCyan:    '#8FBCBB',
        brightWhite:   '#ECEFF4',
      },
    },
    one_dark: {
      label: 'One Dark',
      theme: {
        background:    '#282C34',
        foreground:    '#ABB2BF',
        cursor:        '#528BFF',
        cursorAccent:  '#282C34',
        black:         '#3F4451',
        red:           '#E06C75',
        green:         '#98C379',
        yellow:        '#E5C07B',
        blue:          '#61AFEF',
        magenta:       '#C678DD',
        cyan:          '#56B6C2',
        white:         '#ABB2BF',
        brightBlack:   '#4F5666',
        brightRed:     '#BE5046',
        brightGreen:   '#98C379',
        brightYellow:  '#E5C07B',
        brightBlue:    '#61AFEF',
        brightMagenta: '#C678DD',
        brightCyan:    '#56B6C2',
        brightWhite:   '#FFFFFF',
      },
    },
    gruvbox: {
      label: 'Gruvbox Dark',
      theme: {
        background:    '#282828',
        foreground:    '#EBDBB2',
        cursor:        '#EBDBB2',
        cursorAccent:  '#282828',
        black:         '#282828',
        red:           '#CC241D',
        green:         '#98971A',
        yellow:        '#D79921',
        blue:          '#458588',
        magenta:       '#B16286',
        cyan:          '#689D6A',
        white:         '#A89984',
        brightBlack:   '#928374',
        brightRed:     '#FB4934',
        brightGreen:   '#B8BB26',
        brightYellow:  '#FABD2F',
        brightBlue:    '#83A598',
        brightMagenta: '#D3869B',
        brightCyan:    '#8EC07C',
        brightWhite:   '#EBDBB2',
      },
    },
    dracula: {
      label: 'Dracula',
      theme: {
        background:    '#282A36',
        foreground:    '#F8F8F2',
        cursor:        '#F8F8F2',
        cursorAccent:  '#282A36',
        black:         '#21222C',
        red:           '#FF5555',
        green:         '#50FA7B',
        yellow:        '#F1FA8C',
        blue:          '#BD93F9',
        magenta:       '#FF79C6',
        cyan:          '#8BE9FD',
        white:         '#F8F8F2',
        brightBlack:   '#6272A4',
        brightRed:     '#FF6E6E',
        brightGreen:   '#69FF94',
        brightYellow:  '#FFFFA5',
        brightBlue:    '#D6ACFF',
        brightMagenta: '#FF92DF',
        brightCyan:    '#A4FFFF',
        brightWhite:   '#FFFFFF',
      },
    },
    monokai: {
      label: 'Monokai',
      theme: {
        background:    '#272822',
        foreground:    '#F8F8F2',
        cursor:        '#F8F8F2',
        cursorAccent:  '#272822',
        black:         '#272822',
        red:           '#F92672',
        green:         '#A6E22E',
        yellow:        '#F4BF75',
        blue:          '#66D9E8',
        magenta:       '#AE81FF',
        cyan:          '#A1EFE4',
        white:         '#F8F8F2',
        brightBlack:   '#75715E',
        brightRed:     '#F92672',
        brightGreen:   '#A6E22E',
        brightYellow:  '#F4BF75',
        brightBlue:    '#66D9E8',
        brightMagenta: '#AE81FF',
        brightCyan:    '#A1EFE4',
        brightWhite:   '#F9F8F5',
      },
    },
  };

  document.addEventListener('DOMContentLoaded', () => {
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

    // Wire up color pickers and live preview
    _initColorPickers();

    // Update preview whenever appearance fields change
    ['setting-color-scheme', 'setting-font-family', 'setting-font-size', 'setting-line-height'].forEach(id => {
      const el = document.getElementById(id);
      if (el) el.addEventListener('change', _updatePreview);
    });
    document.getElementById('setting-font-size').addEventListener('input', _updatePreview);

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
    } catch (e) {
      console.warn('Could not load settings:', e);
    }
  }

  function openSettings() {
    populateForm(currentSettings);
    overlay.classList.remove('hidden');
  }

  function closeSettings() {
    overlay.classList.add('hidden');
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
    _val('setting-scrollback',       t.scrollback_lines || 5000);
    _checked('setting-cursor-blink',      t.cursor_blink      !== false);
    _checked('setting-right-click-paste', t.right_click_paste !== false);
    _checked('setting-copy-on-select',    !!t.copy_on_select);
    _checked('setting-expand-aliases',    t.expand_aliases  !== false);
    _checked('setting-auto-paging',       t.auto_paging_off !== false);

    // Colour rules are a repeating row, so their editor owns the DOM.
    const h = s.highlight || {};
    _checked('setting-highlight-enabled', h.enabled !== false);
    if (window.highlightRulesEditor) {
      window.highlightRulesEditor.render(
        h.rules && h.rules.length ? h.rules : window.highlightRulesEditor.DEFAULT_RULES);
    }
    _checked('setting-logging-enabled',   !!l.enabled);
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
        scrollback_lines:  parseInt(_gval('setting-scrollback'), 10),
        right_click_paste: _gchecked('setting-right-click-paste'),
        copy_on_select:    _gchecked('setting-copy-on-select'),
        expand_aliases:    _gchecked('setting-expand-aliases'),
        auto_paging_off:   _gchecked('setting-auto-paging'),
      },
      highlight: {
        enabled: _gchecked('setting-highlight-enabled'),
        rules:   window.highlightRulesEditor
          ? window.highlightRulesEditor.collect() : [],
      },
      logging: {
        enabled:   _gchecked('setting-logging-enabled'),
        directory: _gval('setting-log-dir'),
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

  function _val(id, v)     { const el = document.getElementById(id); if (el) el.value = v; }
  function _checked(id, v) { const el = document.getElementById(id); if (el) el.checked = !!v; }
  function _gval(id)       { const el = document.getElementById(id); return el ? el.value : ''; }
  function _gchecked(id)   { const el = document.getElementById(id); return el ? el.checked : false; }

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
  window.getAllColorSchemes  = () => COLOR_SCHEMES;
  window.openSettings       = openSettings;
  window.reloadSettings     = loadSettings;

})();
