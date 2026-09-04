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
    const copyUrl = document.getElementById('diag-copy-url');
    if (copyUrl) copyUrl.addEventListener('click', async () => {
      try {
        await navigator.clipboard.writeText(window.location.origin);
      } catch (_) { /* the address is on screen either way */ }
      if (typeof window._showCopyToast === 'function') window._showCopyToast();
    });

    const diagSupport = document.getElementById('diag-open-support');
    if (diagSupport) diagSupport.addEventListener('click', () => {
      closeSettings();
      if (typeof window.openSupport === 'function') window.openSupport();
    });
    // Ansible (#585): the test, and the door to the panel.
    const ansibleTest = document.getElementById('ansible-test');
    if (ansibleTest) ansibleTest.addEventListener('click', _testAnsible);
    const ansiblePanel = document.getElementById('ansible-open-panel');
    if (ansiblePanel) ansiblePanel.addEventListener('click', () => {
      closeSettings();
      if (typeof window.openAnsible === 'function') window.openAnsible();
    });

    // The Broadcast section's door to the panel the library lives in (#239).
    const broadcastOpen = document.getElementById('broadcast-open-panel');
    if (broadcastOpen) broadcastOpen.addEventListener('click', () => {
      closeSettings();
      if (typeof window.openBroadcast === 'function') {
        // With a way back (#283): Settings has to close for Broadcast to be
        // reachable, so Broadcast carries the return trip.
        window.openBroadcast({
          returnTo: {
            label: 'Settings',
            open: () => {
              if (typeof window.openSettingsSection === 'function') {
                window.openSettingsSection('Broadcast');
              } else if (typeof window.openSettings === 'function') {
                window.openSettings();
              }
            },
          },
        });
      }
    });

    // The global destructive-command switch (#252). It writes the two
    // advanced keys that gate typing and AI suggestions — one intent, two
    // gates, and a switch that only closed one would be a false promise.
    const destructive = document.getElementById('setting-confirm-destructive');
    if (destructive) destructive.addEventListener('change', async () => {
      const on = destructive.checked;
      try {
        await fetch('/api/advanced', {
          method:  'POST',
          headers: { 'Content-Type': 'application/json' },
          body:    JSON.stringify({ values: {
            'terminal.confirm_dangerous': on,
            'ai.confirm_dangerous':       on,
          } }),
        });
        window.dispatchEvent(new CustomEvent('shellmate:advanced-changed'));
      } catch (e) {
        console.warn('Could not save the destructive-command switch:', e);
      }
    });

    const addSpecial = document.getElementById('btn-special-add');
    if (addSpecial) addSpecial.addEventListener('click', () => {
      const host = document.getElementById('special-rows');
      if (host) host.appendChild(_specialRow({ name: '', kind: 'input', data: '' }));
    });

    // The door to the per-platform command lists (#251).
    const editDangerous = document.getElementById('behaviour-edit-dangerous');
    if (editDangerous) editDangerous.addEventListener('click', () => {
      if (typeof window.openSettingsSection === 'function') {
        window.openSettingsSection('Platform Definitions');
      }
    });

    // Toast appearance (#254). Picking a colour marks it custom; Reset hands
    // the severity back to the theme token.
    TOAST_ACCENTS.forEach(([id]) => {
      const el = document.getElementById(id);
      if (el) el.addEventListener('input', () => { el.dataset.custom = '1'; });
    });
    document.querySelectorAll('.toast-accent-reset').forEach(btn => {
      btn.addEventListener('click', () => {
        const entry = TOAST_ACCENTS.find(([id]) => id === btn.dataset.accent);
        const el = entry && document.getElementById(entry[0]);
        if (!el) return;
        el.dataset.custom = '0';
        el.value = _themeColour(entry[2]);
      });
    });

    // Toast duration mirrors the advanced alerts.toast_seconds row — one
    // value, two doors, saved immediately like every advanced row.
    const toastSeconds = document.getElementById('setting-toast-seconds');
    if (toastSeconds) toastSeconds.addEventListener('change', async () => {
      const value = Math.max(3, Math.min(120, parseInt(toastSeconds.value, 10) || 12));
      toastSeconds.value = value;
      try {
        await fetch('/api/advanced', {
          method:  'POST',
          headers: { 'Content-Type': 'application/json' },
          body:    JSON.stringify({ values: { 'alerts.toast_seconds': value } }),
        });
        window.dispatchEvent(new CustomEvent('shellmate:advanced-changed'));
      } catch (e) {
        console.warn('Could not save the toast duration:', e);
      }
    });

    // Where notifications appear applies live (#256). The setting always
    // worked — but only on Save, with nothing on screen to show it, which
    // reads as broken. The change now lands immediately and fires a sample
    // toast in the new corner; closing without saving puts it back.
    const toastPosition = document.getElementById('setting-toast-position');
    if (toastPosition) toastPosition.addEventListener('change', () => {
      const choice = toastPosition.value;
      if (!['bottom-right', 'bottom-left', 'top-right', 'top-left'].includes(choice)) return;
      document.documentElement.setAttribute('data-toast-position', choice);
      if (window.shellmateAlerts && window.shellmateAlerts.notify) {
        window.shellmateAlerts.notify({
          icon:  'check_circle',
          title: 'Notifications will appear here',
          body:  'Save Settings to keep this.',
          deadline_ms: Date.now() + 600000,
        });
      }
    });

    // One sample of each kind, with whatever is configured above (#254).
    const toastPreview = document.getElementById('toast-preview');
    if (toastPreview) toastPreview.addEventListener('click', () => {
      if (!window.shellmateAlerts || !window.shellmateAlerts.notify) return;
      // Unsaved colour picks are applied for the preview, so what fires is
      // what would be saved — Save Settings still makes it permanent.
      _applyToastAccents({
        accent_info:     _accentValue('setting-toast-accent-info'),
        accent_warning:  _accentValue('setting-toast-accent-warning'),
        accent_critical: _accentValue('setting-toast-accent-critical'),
      });
      [['info',     'check_circle', 'An ordinary confirmation.'],
       ['warning',  'warning',      'Something worth a look.'],
       ['critical', 'error',        'Something about to happen.'],
      ].forEach(([severity, icon, body], i) => {
        setTimeout(() => window.shellmateAlerts.notify({
          severity, icon, body,
          title: `Preview — ${severity}`,
          // Marks the toast urgent, so the open Settings overlay does not
          // swallow the non-critical ones.
          deadline_ms: Date.now() + 600000,
        }), i * 300);
      });
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
      _applyToastAccents(currentSettings.alerts);
      // Interface preferences — theme, layout, density — are applied by
      // prefs.js, which may have loaded before this resolved.
      window.dispatchEvent(new CustomEvent('shellmate:settings-loaded',
                                           { detail: currentSettings }));
    } catch (e) {
      console.warn('Could not load settings:', e);
    }
  }

  function openSettings() {
    // Before populateForm, so the saved choice still has an option to select
    // once the list has been rebuilt (#277).
    _buildFontPicker();
    populateForm(currentSettings);
    _populateDiagnostics();
    _populateDestructiveSwitch();
    _populateDangerousLists();
    overlay.classList.remove('hidden');
  }

  /**
   * Show every platform's destructive-command list in place (#258).
   *
   * Read-only here — the editor is Platform Definitions — but "which
   * commands are guarded" must be answerable by looking, not by knowing
   * which other section to open.
   */
  async function _populateDangerousLists() {
    const host = document.getElementById('dangerous-lists');
    if (!host) return;
    try {
      const data = await (await fetch('/api/platforms')).json();
      const profiles = Object.values(data.platforms || {})
        .filter(p => p && (p.dangerous_commands || []).length)
        .sort((a, b) => (a.name || a.id).localeCompare(b.name || b.id));

      host.innerHTML = '';
      profiles.forEach(p => {
        const row = document.createElement('div');
        row.className = 'dangerous-list-row';
        const name = document.createElement('span');
        name.className = 'dangerous-list-name';
        name.textContent = p.name || p.id;
        const commands = document.createElement('code');
        commands.className = 'dangerous-list-commands';
        commands.textContent = (p.dangerous_commands || []).join(' · ');
        row.append(name, commands);
        host.appendChild(row);
      });
    } catch (_) { /* the edit button still works; the preview is a bonus */ }
  }

  // -------------------------------------------------------------------------
  // Special commands (#299)
  // -------------------------------------------------------------------------

  /**
   * Show a sequence as something a person can read and retype.
   *
   * A raw control byte in a text box is invisible — it looks like an empty
   * field that somehow works — so the editor speaks in escapes and converts
   * both ways.
   */
  function _showSequence(data) {
    return String(data || '').replace(/[\x00-\x1f\x7f]/g, (ch) => {
      if (ch === '\r') return '\\r';
      if (ch === '\n') return '\\n';
      if (ch === '\t') return '\\t';
      return '\\x' + ch.charCodeAt(0).toString(16).padStart(2, '0');
    });
  }

  function _parseSequence(text) {
    return String(text || '')
      .replace(/\\x([0-9a-fA-F]{2})/g, (_, hex) => String.fromCharCode(parseInt(hex, 16)))
      .replace(/\\r/g, '\r').replace(/\\n/g, '\n').replace(/\\t/g, '\t');
  }

  function _renderSpecialRows(commands) {
    const host = document.getElementById('special-rows');
    if (!host) return;
    host.innerHTML = '';
    (commands || []).forEach(entry => host.appendChild(_specialRow(entry)));
  }

  function _specialRow(entry) {
    const row = document.createElement('div');
    row.className = 'special-row';

    const name = document.createElement('input');
    name.type = 'text';
    name.className = 'setting-input special-name';
    name.placeholder = 'Name';
    name.value = entry.name || '';

    const seq = document.createElement('input');
    seq.type = 'text';
    seq.className = 'setting-input special-seq';
    seq.placeholder = '\\x03';
    seq.value = entry.kind === 'break' ? 'break' : _showSequence(entry.data);
    seq.title = entry.kind === 'break'
      ? 'The serial break signal, not a character — leave this as "break".'
      : 'The characters to send. \\x03 is Ctrl+C, \\r is return.';

    const remove = document.createElement('button');
    remove.type = 'button';
    remove.className = 'btn-tertiary';
    remove.textContent = 'Remove';
    remove.addEventListener('click', () => row.remove());

    row.append(name, seq, remove);
    return row;
  }

  function _collectSpecialCommands() {
    const host = document.getElementById('special-rows');
    if (!host) return undefined;   // section absent: leave the setting alone
    return [...host.querySelectorAll('.special-row')].map(row => {
      const name = (row.querySelector('.special-name').value || '').trim();
      const raw  = (row.querySelector('.special-seq').value || '').trim();
      if (!name) return null;
      if (raw.toLowerCase() === 'break') return { name, kind: 'break' };
      return { name, kind: 'input', data: _parseSequence(raw) };
    }).filter(Boolean);
  }

  // -------------------------------------------------------------------------
  // The font picker (#277)
  // -------------------------------------------------------------------------

  /**
   * Monospace faces worth offering.
   *
   * Only JetBrains Mono ships with ShellMate; the rest depend on the machine.
   * Rather than pretend, the picker draws each option in its own face and
   * says which are not installed — choosing an absent font silently falls
   * back to the browser's default monospace, which looks like the setting
   * being ignored.
   */
  const FONT_CHOICES = [
    ['JetBrains Mono, monospace',   'JetBrains Mono'],
    ['Cascadia Code, monospace',    'Cascadia Code'],
    ['Cascadia Mono, monospace',    'Cascadia Mono'],
    ['Consolas, monospace',         'Consolas'],
    ['Fira Code, monospace',        'Fira Code'],
    ['Hack, monospace',             'Hack'],
    ['IBM Plex Mono, monospace',    'IBM Plex Mono'],
    ['Inconsolata, monospace',      'Inconsolata'],
    ['Iosevka, monospace',          'Iosevka'],
    ['Liberation Mono, monospace',  'Liberation Mono'],
    ['Lucida Console, monospace',   'Lucida Console'],
    ['Menlo, monospace',            'Menlo'],
    ['Monaco, monospace',           'Monaco'],
    ['DejaVu Sans Mono, monospace', 'DejaVu Sans Mono'],
    ['Roboto Mono, monospace',      'Roboto Mono'],
    ['Source Code Pro, monospace',  'Source Code Pro'],
    ['Ubuntu Mono, monospace',      'Ubuntu Mono'],
    ['Victor Mono, monospace',      'Victor Mono'],
    ['Courier New, monospace',      'Courier New'],
    ['monospace',                   'System default'],
  ];

  function _buildFontPicker() {
    const select = document.getElementById('setting-font-family');
    if (!select) return;

    const chosen = select.value;
    select.innerHTML = '';
    FONT_CHOICES.forEach(([value, label]) => {
      const family = value.split(',')[0].trim();
      // document.fonts.check needs a size; the family is what it answers on.
      let available = true;
      try {
        available = family === 'monospace'
          || document.fonts.check(`12px "${family}"`);
      } catch (_) { /* assume present rather than mislabel it */ }

      const opt = document.createElement('option');
      opt.value = value;
      opt.textContent = available ? label : `${label} — not installed`;
      opt.disabled = !available && family !== 'JetBrains Mono';
      // Drawn in its own face, so the choice is visible before applying it.
      if (available) opt.style.fontFamily = value;
      select.appendChild(opt);
    });
    if (chosen) select.value = chosen;
    if (!select.value) select.value = 'JetBrains Mono, monospace';
  }

  // -------------------------------------------------------------------------
  // Toast appearance (#254)
  // -------------------------------------------------------------------------

  /** Colour input id → [settings key, theme token it falls back to]. */
  const TOAST_ACCENTS = [
    ['setting-toast-accent-info',     'accent_info',     '--primary'],
    ['setting-toast-accent-warning',  'accent_warning',  '--warn'],
    ['setting-toast-accent-critical', 'accent_critical', '--error'],
  ];

  function _themeColour(token) {
    const raw = getComputedStyle(document.documentElement)
      .getPropertyValue(token).trim();
    return /^#[0-9a-fA-F]{6}$/.test(raw) ? raw : '#888888';
  }

  /**
   * A colour input cannot be empty, so "no custom colour" is carried in
   * dataset.custom rather than in the value — the input shows the theme's
   * own colour until the user actually picks one.
   */
  function _populateToastAccents(al) {
    TOAST_ACCENTS.forEach(([id, key, token]) => {
      const el = document.getElementById(id);
      if (!el) return;
      const saved = (al || {})[key] || '';
      el.value = /^#[0-9a-fA-F]{6}$/.test(saved) ? saved : _themeColour(token);
      el.dataset.custom = saved ? '1' : '0';
    });
  }

  function _accentValue(id) {
    const el = document.getElementById(id);
    return (el && el.dataset.custom === '1') ? el.value : '';
  }

  /**
   * Custom accents become CSS variables on :root; blank hands the toast back
   * to the theme token (#254). The stylesheet only ever reads
   * var(--alert-accent-*, <theme token>), so the theme rule holds either way.
   */
  function _applyToastAccents(al) {
    const root = document.documentElement.style;
    [['accent_info', '--alert-accent-info'],
     ['accent_warning', '--alert-accent-warning'],
     ['accent_critical', '--alert-accent-critical'],
    ].forEach(([key, cssVar]) => {
      const value = (al || {})[key] || '';
      if (/^#[0-9a-fA-F]{6}$/.test(value)) root.setProperty(cssVar, value);
      else root.removeProperty(cssVar);
    });
  }

  /**
   * Fill the ordinary rows that mirror advanced values, in one fetch.
   *
   * The destructive-command switch (#252): "any guard active" reads as on —
   * the switch sets both gates together, and a mixed state means someone
   * tuned the halves individually, which on represents more honestly.
   * The toast duration (#254) mirrors alerts.toast_seconds.
   */
  async function _populateDestructiveSwitch() {
    try {
      const registry = await (await fetch('/api/advanced')).json();
      const lookup = (key) => {
        const setting = (registry.settings || []).find(s => s.key === key);
        return setting ? setting.value : undefined;
      };

      const destructive = document.getElementById('setting-confirm-destructive');
      if (destructive) {
        destructive.checked =
          Boolean(lookup('terminal.confirm_dangerous') ?? true)
          || Boolean(lookup('ai.confirm_dangerous') ?? true);
      }

      const toastSeconds = document.getElementById('setting-toast-seconds');
      if (toastSeconds) {
        toastSeconds.value = Number(lookup('alerts.toast_seconds')) || 12;
      }
    } catch (_) { /* leave whatever state the rows had */ }
  }

  function closeSettings() {
    overlay.classList.add('hidden');
    // Un-saved live previews — the toast position (#256) — go back to what
    // is actually saved.
    if (window.shellmatePrefs && typeof window.shellmatePrefs.apply === 'function') {
      window.shellmatePrefs.apply();
    }
  }

  /**
   * Fill the Diagnostics section's read-only rows (#222).
   *
   * Asked on every open rather than once: the history counts move, and the
   * fallback-folder state can change between launches. A failed fetch leaves
   * the em-dash — Diagnostics failing must never break Settings.
   */
  let _updateBound = false;

  /** The Check for updates button (#420): on demand, and says what it found. */
  function _bindUpdateCheck() {
    if (_updateBound) return;
    const button = document.getElementById('diag-check-update');
    const result = document.getElementById('diag-update-result');
    if (!button || !result) return;
    _updateBound = true;
    button.addEventListener('click', async () => {
      button.disabled = true;
      result.textContent = 'Asking GitHub…';
      result.innerHTML = '';
      try {
        const info = await (await fetch('/api/system/update')).json();
        if (info.error) {
          result.textContent = info.error;
        } else if (info.note) {
          result.textContent = `${info.note} You are running ${info.current}.`;
        } else if (info.newer) {
          const link = document.createElement('a');
          link.href = info.url;
          link.target = '_blank';
          link.rel = 'noopener';
          link.className = 'diag-link';
          link.textContent = `Version ${info.latest} is available`;
          result.append(link, document.createTextNode(` — you are running ${info.current}.`));
        } else {
          result.textContent = `You are up to date (${info.current}; latest is ${info.latest || info.current}).`;
        }
      } catch (e) {
        result.textContent = 'The check failed: ' + e.message;
      } finally {
        button.disabled = false;
      }
    });
  }

  /**
   * The self-checks (#562).
   *
   * On the button, never on open. Two of the checks go over the network, and
   * a Settings panel that reaches out the moment it is opened spends the
   * air-gapped promise on the user's behalf — so they are a tick box beside
   * the button and off by default.
   *
   * Rendered as a row of chips, one per check, with the detail and what to do
   * about it underneath the one you pick. Anything that failed is selected
   * for you: a green row of chips with a problem hidden inside it would be
   * worse than no panel at all.
   */
  let _checksBound = false;

  function _bindChecks() {
    if (_checksBound) return;
    const button = document.getElementById('diag-run-checks');
    const host = document.getElementById('diag-checks');
    const summary = document.getElementById('diag-checks-summary');
    if (!button || !host || !summary) return;
    _checksBound = true;

    button.addEventListener('click', async () => {
      const probes = !!(document.getElementById('diag-check-probes') || {}).checked;
      button.disabled = true;
      summary.textContent = probes ? 'Checking, including the internet…' : 'Checking…';
      host.innerHTML = '';
      try {
        const res = await fetch(`/api/system/checks?probes=${probes ? 'true' : 'false'}`);
        const data = await res.json();
        summary.textContent = data.summary || '';
        _renderChecks(host, data.checks || []);
      } catch (e) {
        summary.textContent = 'The checks could not be run: ' + e.message;
      } finally {
        button.disabled = false;
      }
    });
  }

  function _renderChecks(host, rows) {
    host.innerHTML = '';
    if (!rows.length) return;

    const chips = document.createElement('div');
    chips.className = 'diag-chip-row';
    const detail = document.createElement('div');
    detail.className = 'diag-check-detail';

    const show = (row, chip) => {
      chips.querySelectorAll('.diag-chip').forEach(c => c.classList.remove('selected'));
      chip.classList.add('selected');
      detail.innerHTML = '';
      const what = document.createElement('p');
      what.className = 'diag-check-line';
      what.textContent = `${row.label}: ${row.detail}`;
      detail.appendChild(what);
      if (row.fix) {
        const fix = document.createElement('p');
        fix.className = 'settings-section-hint';
        fix.textContent = row.fix;
        detail.appendChild(fix);
      }
    };

    let worst = null;
    rows.forEach(row => {
      const chip = document.createElement('button');
      chip.type = 'button';
      chip.className = `diag-chip diag-${row.status || 'warn'}`;
      chip.textContent = row.label;
      chip.title = row.detail || '';
      chip.addEventListener('click', () => show(row, chip));
      chips.appendChild(chip);
      const rank = { fail: 2, warn: 1, ok: 0 }[row.status] || 0;
      if (!worst || rank > worst.rank) worst = { row, chip, rank };
    });

    host.append(chips, detail);
    if (worst && worst.rank > 0) show(worst.row, worst.chip);
  }

  async function _populateDiagnostics() {
    _bindChecks();
    const set = (id, text) => {
      const el = document.getElementById(id);
      if (el && text) el.textContent = text;
    };
    try {
      const info = await (await fetch('/api/system/info')).json();
      // "ShellMate 1.0.0 (a1b2c3d, built 2026-09-02 16:17)" from the build
      // record (#420); the older fields are the fallback for a server that
      // predates it.
      set('diag-version', [
        info.describe || info.app || 'ShellMate',
        !info.describe && info.built ? `built ${info.built}` : '',
        info.portable ? '(portable)' : '',
      ].filter(Boolean).join(' — ').replace(' — (', ' ('));
      _bindUpdateCheck();
      set('diag-data-dir', (info.data_dir || '') +
        (info.using_fallback
          ? ' — fallback: the folder beside the executable was not writable'
          : ''));
      set('diag-log-dir', info.log_dir ? `${info.log_dir}` : '');
    } catch (_) { /* rows keep their placeholder */ }
    // The address this page is already served from (#285) — asking the
    // window beats asking the server, since the port is chosen at startup
    // and this is by definition the one that worked.
    const url = document.getElementById('diag-web-url');
    if (url) {
      url.textContent = window.location.origin;
      url.href = window.location.origin;
      url.target = '_blank';
      url.rel = 'noopener noreferrer';
    }

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
    // Ansible (#585). Paths and an address, never a secret: the private key
    // is a file with its own permissions and only its location is stored,
    // which is why nothing here goes through the vault.
    const ans = s.ansible || {};
    _val('setting-ansible-url',     ans.runner_url  || '');
    // Dots when one is stored, empty when not — and never the real value.
    // Saving with the dots untouched leaves the stored token alone, so
    // changing a timeout does not mean retyping a token nobody can read.
    _val('setting-ansible-token',   ans.has_token ? '•'.repeat(8) : '');
    const ansTok = document.getElementById('setting-ansible-token');
    if (ansTok) ansTok.dataset.maskedOriginal = ans.has_token ? '•'.repeat(8) : '';
    _val('setting-ansible-cert',    ans.client_cert || '');
    _val('setting-ansible-key',     ans.client_key  || '');
    _val('setting-ansible-ca',      ans.ca_cert     || '');
    _checked('setting-ansible-verify', ans.verify_tls !== false);
    _val('setting-ansible-project', ans.project_dir || '/runner/project');
    _val('setting-ansible-timeout', ans.timeout ?? 30);

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
    _checked('setting-toasts-all',     al.toasts_all     !== false);
    _checked('setting-toast-info',     al.toast_info     !== false);
    _checked('setting-toast-warning',  al.toast_warning  !== false);
    _checked('setting-toast-critical', al.toast_critical !== false);
    _checked('setting-reduce-motion', !!al.reduce_motion);
    _populateToastAccents(al);

    const ui = s.interface || {};
    _val('setting-theme',             ui.theme || 'dark');
    _val('setting-density',           ui.density || 'comfortable');
    _val('setting-panel-transition',  ui.panel_transition || 'slide');
    _val('setting-tab-label-width',   ui.max_tab_label_px || 160);
    _checked('setting-connection-dot',    ui.show_connection_dot !== false);
    _checked('setting-sidebar-labels',    ui.sidebar_labels === true);
    _checked('setting-colourful-groups',  ui.colourful_groups !== false);
    _checked('setting-tab-fill',          ui.tab_fill === true);
    _renderSpecialRows(ui.special_commands);
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

  /**
   * The Ansible runner block (#585).
   *
   * Its own function because Test connection saves it alone, without the
   * rest of the form — trying a certificate path should not commit whatever
   * else happens to be open in Settings at the time.
   */
  function _collectAnsible() {
    return {
      runner_url:  _gval('setting-ansible-url').trim(),
      // Sent only when it actually changed. Sending the mask back would
      // store eight bullet characters as the token, and the failure would
      // arrive as "the runner refused ShellMate" with nothing to explain it.
      ..._tokenIfChanged(),
      client_cert: _gval('setting-ansible-cert').trim(),
      client_key:  _gval('setting-ansible-key').trim(),
      ca_cert:     _gval('setting-ansible-ca').trim(),
      verify_tls:  _gchecked('setting-ansible-verify'),
      project_dir: _gval('setting-ansible-project').trim() || '/runner/project',
      timeout:     _int('setting-ansible-timeout', 30),
    };
  }

  /**
   * Ask the runner, and say what actually came back.
   *
   * Honest rather than encouraging: `configured` and `reachable` are
   * different answers, and "not set up yet" must not be reported as a
   * failure to connect. The service's own message is passed through — it
   * names the missing certificate, or the host that refused.
   */
  async function _testAnsible() {
    const button = document.getElementById('ansible-test');
    const result = document.getElementById('ansible-test-result');
    if (!button || !result) return;
    button.disabled = true;
    result.classList.remove('ansible-note-bad');
    result.textContent = 'Asking the runner\u2026';
    try {
      // Saved first, because /api/ansible/status is answered from the stored
      // settings. Testing an address still sitting unsaved in a box would
      // report on the previous one, which is the worst possible answer.
      await fetch('/api/settings', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ settings: { ansible: _collectAnsible() } }),
      });
      await loadSettings();

      const data = await (await fetch('/api/ansible/status')).json();
      if (!data.configured) {
        result.classList.add('ansible-note-bad');
        result.textContent = data.detail
          ? `Not set up yet — still needed: ${data.detail}.`
          : 'Not set up yet.';
      } else if (data.reachable) {
        result.textContent = `The runner answered. ${data.detail || ''}`.trim();
      } else {
        result.classList.add('ansible-note-bad');
        result.textContent = `Configured, but it did not answer: ${data.detail || ''}`.trim();
      }
    } catch (e) {
      result.classList.add('ansible-note-bad');
      result.textContent = `Could not ask: ${e.message || e}`;
    } finally {
      button.disabled = false;
    }
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
        accent_info:     _accentValue('setting-toast-accent-info'),
        accent_warning:  _accentValue('setting-toast-accent-warning'),
        accent_critical: _accentValue('setting-toast-accent-critical'),
        toasts_all:      _gchecked('setting-toasts-all'),
        toast_info:      _gchecked('setting-toast-info'),
        toast_warning:   _gchecked('setting-toast-warning'),
        toast_critical:  _gchecked('setting-toast-critical'),
      },
      interface: {
        theme:               _gval('setting-theme') || 'dark',
        density:             _gval('setting-density') || 'comfortable',
        panel_transition:    _gval('setting-panel-transition') || 'slide',
        max_tab_label_px:    parseInt(_gval('setting-tab-label-width'), 10) || 160,
        show_connection_dot: _gchecked('setting-connection-dot'),
        sidebar_labels:      _gchecked('setting-sidebar-labels'),
        colourful_groups:    _gchecked('setting-colourful-groups'),
        tab_fill:            _gchecked('setting-tab-fill'),
        special_commands:    _collectSpecialCommands(),
        font_scale:          parseFloat(_gval('setting-font-scale')) || 1,
        toast_position:      _gval('setting-toast-position'),
        chat_enter:          _gval('setting-chat-enter'),
        tab_menu:            _collectTabMenu(),
        restore_tabs:        _gchecked('setting-restore-tabs'),
        new_tab_opens:       _gval('setting-new-tab-opens'),
        // An empty select means "not loaded yet", not "clear it" (#355):
        // the options arrive async, and saving before they do — or after
        // the fetch failed — must keep the configured profile.
        new_tab_profile:     _gval('setting-new-tab-profile')
          || (((window.shellmateSettings || {}).interface || {}).new_tab_profile || ''),
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
      ansible: _collectAnsible(),
      providers: _collectProviders(),
    };

    try {
      const res = await fetch('/api/settings', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ settings: s }),
      });
      const payload = await res.json().catch(() => ({}));
      if (!res.ok) {
        // A refused save is an error body, not settings (#347). Adopting it
        // set window.shellmateSettings to {detail: ...}, fired the changed
        // event with it (resetting theme and fonts to defaults), wiped the
        // form, and closed the panel as if it had worked — a locked vault
        // plus one typed API key silently discarded everything.
        _saveFailed(payload.detail
          || `The server refused the save (HTTP ${res.status}).`);
        return;
      }
      currentSettings = payload;
      window.shellmateSettings = currentSettings;
      _applyHighlightRules(currentSettings);
      _applyUiFontSize((currentSettings.appearance || {}).ui_font_size || 14);
      _applyToastAccents(currentSettings.alerts);
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
      // Say so where the user is looking (#356): the only signal used to be
      // the panel *not* closing, which reads as nothing at all.
      _saveFailed('Could not reach the server — nothing was saved.');
    }
  }

  /** A failed save, reported in place. The panel stays open with the
   *  user's edits intact, because closing is the success signal. */
  function _saveFailed(reason) {
    if (window.shellmateAlerts) {
      window.shellmateAlerts.notify({
        severity: 'warning', icon: 'error',
        title: 'Settings were not saved', body: reason || '',
      });
    }
  }

  /**
   * Build the providers payload from the form. For each field:
   *   - if the user typed nothing → omit (backend keeps existing or falls back to env)
   *   - if the user typed the masked placeholder → omit (no real change)
   *   - otherwise → send the new value
   */
  /**
   * The Ansible token, but only if it was really edited.
   *
   * The field shows a mask when one is stored, so an unchanged field means
   * "leave it alone" and an emptied one means "remove it". Sending the mask
   * back would store eight bullet characters as the token, and the failure
   * would arrive later as the runner refusing ShellMate, with nothing on
   * screen to connect the two.
   */
  function _tokenIfChanged() {
    const el = document.getElementById('setting-ansible-token');
    if (!el) return {};
    const typed = (el.value || '').trim();
    const mask = (el.dataset.maskedOriginal || '').trim();
    if (typed === mask) return {};
    return { token: typed };
  }

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
      // A bare array (#355) — reading `data.profiles` left this select
      // permanently empty, so the picker offered nothing and every save
      // wrote '' over the configured profile.
      let profiles = res.ok ? await res.json() : [];
      if (!Array.isArray(profiles)) profiles = profiles.profiles || [];
      profiles.forEach(profile => {
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
