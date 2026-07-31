/**
 * connections.js — Connection dialog and profile management.
 *
 * Handles showing/hiding the modal, switching the form between transports
 * (SSH / serial / telnet), reading the fields, POSTing to the backend to
 * create a session, and saving/loading connection profiles.
 * On connect success it calls createTab() (defined in tabs.js).
 *
 * The form is one dialog rather than three, with field groups tagged
 * data-for="ssh telnet" and shown or hidden as the type changes. Keeping a
 * single form means the display name, saved profiles and error area are
 * written once instead of per transport.
 */
(function () {
  'use strict';

  let overlay, form, errorBox, connectBtn, connectLabel, connectSpinner;
  let profilesList, typeSelect, serialPortSelect, serialPortHint;

  /** Default TCP port per transport, applied when the type changes. */
  const DEFAULT_PORTS = { ssh: 22, telnet: 23 };

  /**
   * Profile the dialog was opened from, if any.
   *
   * Sent with the connect request so the backend can fill in credentials the
   * user asked it to remember, and so newly remembered ones are filed against
   * the right profile.
   */
  let activeProfileId = '';
  let activeProfileHasCredentials = false;

  document.addEventListener('DOMContentLoaded', () => {
    overlay          = document.getElementById('modal-overlay');
    form             = document.getElementById('connection-form');
    errorBox         = document.getElementById('form-error');
    connectBtn       = document.getElementById('btn-connect');
    connectLabel     = document.getElementById('btn-connect-label');
    connectSpinner   = document.getElementById('btn-connect-spinner');
    profilesList     = document.getElementById('saved-profiles-list');
    typeSelect       = document.getElementById('field-conntype');
    serialPortSelect = document.getElementById('field-serial-port');
    serialPortHint   = document.getElementById('serial-port-hint');

    renderWelcomeProfiles();

    document.getElementById('btn-new-tab')
      .addEventListener('click', () => showConnectionDialog());

    document.getElementById('btn-welcome-connect')
      .addEventListener('click', () => showConnectionDialog());

    document.getElementById('modal-close')
      .addEventListener('click', hideConnectionDialog);

    document.getElementById('btn-cancel')
      .addEventListener('click', hideConnectionDialog);

    document.getElementById('btn-save-profile')
      .addEventListener('click', handleSaveProfile);

    document.getElementById('btn-refresh-ports')
      .addEventListener('click', () => loadSerialPorts());

    typeSelect.addEventListener('change', () => applyConnectionType(typeSelect.value));

    overlay.addEventListener('click', (e) => {
      if (e.target === overlay) hideConnectionDialog();
    });

    form.addEventListener('submit', handleSubmit);

    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && !overlay.classList.contains('hidden')) {
        hideConnectionDialog();
      }
    });
  });

  // -------------------------------------------------------------------------
  // Dialog visibility and transport switching
  // -------------------------------------------------------------------------

  function showConnectionDialog(prefill) {
    clearError();
    form.reset();

    activeProfileId = (prefill && prefill.id) || '';
    activeProfileHasCredentials = Boolean(prefill && prefill.has_saved_credentials);

    const type = (prefill && prefill.connection_type) || 'ssh';
    typeSelect.value = type;
    applyConnectionType(type);

    if (prefill) fillFromProfile(prefill);
    updateRememberHint();
    loadProfiles();
    overlay.classList.remove('hidden');

    // Prefilled means the details are known and only the secret is missing,
    // so land on the password. Otherwise start at the first thing to type.
    setTimeout(() => {
      const target = type === 'serial'
        ? 'field-serial-port'
        : (prefill ? 'field-password' : 'field-hostname');
      const el = document.getElementById(target);
      if (el) el.focus();
    }, 50);
  }

  function hideConnectionDialog() {
    overlay.classList.add('hidden');
    setLoading(false);
  }

  /**
   * Show only the field groups that apply to the selected transport.
   *
   * Hidden inputs also get `disabled`, so the browser skips their validation.
   * Without that, a `required` field inside a hidden group blocks submit with
   * a validation bubble pointing at something the user cannot see.
   */
  function applyConnectionType(type) {
    document.querySelectorAll('.conn-fields, .field-hint[data-for]').forEach(group => {
      const applies = group.dataset.for.split(' ').includes(type);
      group.classList.toggle('hidden', !applies);
      group.querySelectorAll('input, select').forEach(el => { el.disabled = !applies; });
    });

    const portField = document.getElementById('field-port');
    if (DEFAULT_PORTS[type] && portField) portField.value = DEFAULT_PORTS[type];

    if (type === 'serial') loadSerialPorts();
  }

  /**
   * Populate the serial port picker from the machine's actual ports.
   *
   * The description matters as much as the device name: "COM5" alone is not
   * enough to pick the right one when a laptop has a dock and two USB
   * adapters attached.
   */
  async function loadSerialPorts() {
    if (!serialPortSelect) return;
    const previous = serialPortSelect.value;
    serialPortSelect.innerHTML = '';

    try {
      const res   = await fetch('/api/serial/ports');
      const ports = res.ok ? await res.json() : [];

      if (!ports.length) {
        serialPortSelect.innerHTML = '<option value="">No serial ports found</option>';
        serialPortHint.textContent =
          'Nothing detected. Check the console cable is plugged in and the USB-to-serial driver is installed.';
        return;
      }

      ports.forEach(p => {
        const opt = document.createElement('option');
        opt.value = p.device;
        opt.textContent = p.description ? `${p.device} — ${p.description}` : p.device;
        serialPortSelect.appendChild(opt);
      });

      if (previous) serialPortSelect.value = previous;
      serialPortHint.textContent = `${ports.length} port${ports.length === 1 ? '' : 's'} detected.`;
    } catch (e) {
      serialPortSelect.innerHTML = '<option value="">Could not list ports</option>';
      serialPortHint.textContent = '';
    }
  }

  // -------------------------------------------------------------------------
  // Profiles
  // -------------------------------------------------------------------------

  async function loadProfiles() {
    if (!profilesList) return;
    try {
      const res = await fetch('/api/profiles');
      renderProfiles(await res.json());
    } catch (e) {
      profilesList.innerHTML = '';
    }
  }

  /** Where a profile points, for tooltips and card subtitles. */
  function profileTarget(p) {
    if (p.connection_type === 'serial') return `${p.serial_port || '?'} @ ${p.baud_rate || 9600}`;
    return `${p.hostname || '?'}:${p.port || 22}`;
  }

  function profileIcon(type) {
    if (type === 'serial') return 'cable';
    if (type === 'telnet') return 'terminal';
    return 'terminal';
  }

  async function renderWelcomeProfiles() {
    const grid = document.getElementById('welcome-profiles-grid');
    if (!grid) return;
    try {
      const res = await fetch('/api/profiles');
      const profiles = await res.json();
      grid.innerHTML = '';
      profiles.forEach(p => {
        const wrap = document.createElement('div');
        wrap.className = 'welcome-profile-wrap';

        const card = document.createElement('button');
        card.className = 'welcome-profile-card';
        card.title = `${profileTarget(p)} (${(p.connection_type || 'ssh').toUpperCase()})`;
        card.innerHTML = `
          <span class="material-symbols-outlined welcome-profile-icon">
            ${profileIcon(p.connection_type)}
          </span>
          <span class="welcome-profile-name"></span>
          <span class="welcome-profile-host"></span>
        `;
        // Set via textContent, not innerHTML — a profile name is user input
        // and must never be parsed as markup.
        card.querySelector('.welcome-profile-name').textContent = p.name || '';
        card.querySelector('.welcome-profile-host').textContent =
          p.connection_type === 'serial' ? (p.serial_port || '') : (p.hostname || '');
        card.addEventListener('click', () => openProfile(p));

        const del = document.createElement('button');
        del.className = 'welcome-profile-delete';
        del.title = 'Remove';
        del.innerHTML = '<span class="material-symbols-outlined">close</span>';
        del.addEventListener('click', async (e) => {
          e.stopPropagation();
          await fetch(`/api/profiles/${p.id}`, { method: 'DELETE' });
          renderWelcomeProfiles();
        });

        wrap.appendChild(card);
        wrap.appendChild(del);
        grid.appendChild(wrap);
      });
    } catch (e) { /* silently skip if API unavailable */ }
  }

  function renderProfiles(profiles) {
    if (!profilesList) return;
    profilesList.innerHTML = '';
    if (!profiles.length) {
      profilesList.innerHTML = '<span class="profiles-empty">No saved connections</span>';
      return;
    }
    profiles.forEach(p => {
      const chip = document.createElement('div');
      chip.className = 'profile-chip';

      const label = document.createElement('span');
      label.className = 'profile-chip-label';
      label.title = profileTarget(p);
      label.textContent = p.name || '';
      label.addEventListener('click', () => {
        typeSelect.value = p.connection_type || 'ssh';
        applyConnectionType(typeSelect.value);
        fillFromProfile(p);
      });

      const del = document.createElement('button');
      del.className = 'profile-chip-delete';
      del.title = 'Delete';
      del.textContent = 'x';
      del.addEventListener('click', async (e) => {
        e.stopPropagation();
        await fetch(`/api/profiles/${p.id}`, { method: 'DELETE' });
        await loadProfiles();
        renderWelcomeProfiles();
      });

      chip.appendChild(label);
      chip.appendChild(del);
      profilesList.appendChild(chip);
    });
  }

  /**
   * Click handler for a saved-device tile.
   *
   * If a tab is already open for this profile, switch to it rather than
   * opening a second session to the same device. Otherwise open the dialog
   * pre-filled.
   */
  async function openProfile(p) {
    try {
      const openIds = (typeof window.getOpenSessionIds === 'function')
        ? window.getOpenSessionIds() : [];
      if (openIds.length) {
        const r = await fetch('/api/sessions');
        if (r.ok) {
          const sessions = await r.json();
          const openSet  = new Set(openIds);
          const match = sessions.find(s => openSet.has(s.session_id) && sameTarget(s, p));
          if (match && typeof window.switchToTabBySessionId === 'function') {
            window.switchToTabBySessionId(match.session_id);
            return;
          }
        }
      }
    } catch (_) { /* fall through to dialog */ }

    showConnectionDialog(p);
  }

  /** True when a live session and a profile point at the same device. */
  function sameTarget(session, profile) {
    const type = profile.connection_type || 'ssh';
    if (session.connection_type !== type) return false;
    if (type === 'serial') return session.hostname === profile.serial_port;
    return session.hostname === profile.hostname &&
           (session.port || 22) === (profile.port || 22) &&
           session.username === profile.username;
  }

  /** Set a field's value, tolerating fields that may not exist. */
  function setField(id, value) {
    const el = document.getElementById(id);
    if (el != null && value !== undefined && value !== null) el.value = value;
  }

  function fillFromProfile(p) {
    setField('field-label', p.name || '');
    setField('field-conntype', p.connection_type || 'ssh');

    setField('field-hostname', p.hostname || '');
    setField('field-port', p.port || DEFAULT_PORTS[p.connection_type] || 22);
    setField('field-username', p.username || '');

    setField('field-key-path', p.private_key_path || '');
    setField('field-jump-host', p.jump_host || '');
    setField('field-jump-port', p.jump_port || 22);
    setField('field-jump-username', p.jump_username || '');

    setField('field-serial-port', p.serial_port || '');
    setField('field-baud', p.baud_rate || 9600);
    setField('field-databits', p.data_bits || 8);
    setField('field-parity', p.parity || 'N');
    setField('field-stopbits', p.stop_bits || 1);
    setField('field-flow', p.flow_control || 'none');

    // Open the advanced section when it holds something, so restored key or
    // jump-host settings are not silently hidden behind a collapsed summary.
    const advanced = document.getElementById('ssh-advanced');
    if (advanced) advanced.open = Boolean(p.private_key_path || p.jump_host);
  }

  // -------------------------------------------------------------------------
  // Reading the form
  // -------------------------------------------------------------------------

  function value(id) {
    const el = document.getElementById(id);
    return el ? el.value.trim() : '';
  }

  function number(id, fallback) {
    const parsed = parseFloat(value(id));
    return Number.isFinite(parsed) ? parsed : fallback;
  }

  /** Build the POST body for /api/sessions from the current form state. */
  function buildPayload() {
    const type = typeSelect.value;
    const remember = document.getElementById('field-remember');

    const payload = {
      connection_type: type,
      display_label:   value('field-label'),
      // The backend fills remembered credentials in server-side from this id,
      // so a saved password never travels to the browser.
      profile_id:           activeProfileId,
      remember_credentials: Boolean(remember && remember.checked),
    };

    if (type === 'serial') {
      Object.assign(payload, {
        serial_port:  value('field-serial-port'),
        baud_rate:    number('field-baud', 9600),
        data_bits:    number('field-databits', 8),
        parity:       value('field-parity') || 'N',
        stop_bits:    number('field-stopbits', 1),
        flow_control: value('field-flow') || 'none',
      });
      return payload;
    }

    Object.assign(payload, {
      hostname: value('field-hostname'),
      port:     number('field-port', DEFAULT_PORTS[type] || 22),
      username: value('field-username'),
      password: document.getElementById('field-password').value,
    });

    if (type === 'ssh') {
      Object.assign(payload, {
        private_key_path:       value('field-key-path'),
        private_key_passphrase: document.getElementById('field-key-passphrase').value,
        jump_host:              value('field-jump-host'),
        jump_port:              number('field-jump-port', 22),
        jump_username:          value('field-jump-username'),
        jump_password:          document.getElementById('field-jump-password').value,
        jump_private_key_path:  value('field-jump-key-path'),
      });
    }

    return payload;
  }

  /**
   * Explain what will happen with credentials for the profile in hand.
   *
   * When a password is already remembered, the field can be left blank — so
   * say that, rather than letting the user wonder why an empty box is
   * accepted.
   */
  function updateRememberHint() {
    const hint = document.getElementById('remember-hint');
    const box  = document.getElementById('field-remember');
    if (!hint || !box) return;

    if (activeProfileHasCredentials) {
      hint.textContent = 'A password is already saved for this connection — leave the field blank to use it.';
      box.checked = true;
    } else if (!activeProfileId) {
      hint.textContent = 'Saved against this connection once it succeeds, encrypted on disk.';
      box.checked = false;
    } else {
      hint.textContent = 'Saved against this connection once it succeeds.';
      box.checked = false;
    }
  }

  /** Return an error message, or null when the form is good to submit. */
  function validate(payload) {
    if (payload.connection_type === 'serial') {
      return payload.serial_port ? null : 'Choose a serial port.';
    }
    if (!payload.hostname) return 'Hostname is required.';

    if (payload.connection_type === 'ssh') {
      if (!payload.username) return 'Username is required.';
      // A key counts as a credential in its own right, and so does one already
      // in the vault — the backend fills that in, so a blank box is fine.
      if (!payload.password && !payload.private_key_path && !activeProfileHasCredentials) {
        return 'Enter a password or choose a private key file.';
      }
    }
    // Telnet needs neither: devices prompt in-band, and leaving the
    // credentials blank simply means the user logs in by hand.
    return null;
  }

  /** The subset of a payload worth persisting — never a secret. */
  function profileFrom(payload) {
    return {
      name:             payload.display_label || payload.hostname || payload.serial_port,
      connection_type:  payload.connection_type,
      hostname:         payload.hostname || '',
      port:             payload.port || 22,
      username:         payload.username || '',
      private_key_path: payload.private_key_path || '',
      jump_host:        payload.jump_host || '',
      jump_port:        payload.jump_port || 22,
      jump_username:    payload.jump_username || '',
      serial_port:      payload.serial_port || '',
      baud_rate:        payload.baud_rate || 9600,
      data_bits:        payload.data_bits || 8,
      parity:           payload.parity || 'N',
      stop_bits:        payload.stop_bits || 1,
      flow_control:     payload.flow_control || 'none',
    };
  }

  async function handleSaveProfile() {
    const payload = buildPayload();
    const problem = validate(payload);
    if (problem) { showError(problem); return; }

    try {
      await fetch('/api/profiles', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify(profileFrom(payload)),
      });
      await loadProfiles();
      renderWelcomeProfiles();
    } catch (e) {
      showError('Could not save profile.');
    }
  }

  // -------------------------------------------------------------------------
  // Form submission
  // -------------------------------------------------------------------------

  async function handleSubmit(e) {
    e.preventDefault();
    clearError();

    const payload = buildPayload();
    const problem = validate(payload);
    if (problem) { showError(problem); return; }

    setLoading(true);

    try {
      const response = await fetch('/api/sessions', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify(payload),
      });

      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || `Server error ${response.status}`);

      // Capture before hiding — hideConnectionDialog resets the form, and the
      // credentials are needed below if this connection creates a new profile.
      const credentials = {
        password:                    payload.password || '',
        private_key_passphrase:      payload.private_key_passphrase || '',
        jump_password:               payload.jump_password || '',
        jump_private_key_passphrase: payload.jump_private_key_passphrase || '',
      };
      const wantsRemember = payload.remember_credentials;

      hideConnectionDialog();
      if (typeof window.createTab === 'function') {
        window.createTab(data);
      } else {
        console.error('createTab() not found — is tabs.js loaded?');
      }

      await autoSaveProfile(payload, wantsRemember, credentials);

    } catch (err) {
      showError(err.message || 'Could not connect. Check the address and credentials.');
      setLoading(false);
    }
  }

  /**
   * Remember a successful connection so it appears on the welcome screen.
   * Skipped when an equivalent profile already exists, or the list would fill
   * with duplicates of the device someone reconnects to twenty times a day.
   */
  async function autoSaveProfile(payload, wantsRemember, credentials) {
    try {
      const r = await fetch('/api/profiles');
      const existing = r.ok ? await r.json() : [];
      const candidate = profileFrom(payload);

      const matches = (p) =>
        (p.connection_type || 'ssh') === candidate.connection_type &&
        (candidate.connection_type === 'serial'
          ? p.serial_port === candidate.serial_port
          : p.hostname === candidate.hostname &&
            (p.port || 22) === (candidate.port || 22) &&
            p.username === candidate.username);

      let profile = existing.find(matches);

      if (!profile) {
        const created = await fetch('/api/profiles', {
          method:  'POST',
          headers: { 'Content-Type': 'application/json' },
          body:    JSON.stringify(candidate),
        });
        if (created.ok) profile = await created.json();
      }

      // A first-time connection has no profile id when it starts, so the
      // backend had nowhere to file the credentials. Now that the profile
      // exists, store them against it.
      if (wantsRemember && profile && profile.id && !payload.profile_id) {
        if (Object.values(credentials).some(Boolean)) {
          await fetch(`/api/profiles/${profile.id}/credentials`, {
            method:  'PUT',
            headers: { 'Content-Type': 'application/json' },
            body:    JSON.stringify(credentials),
          });
        }
      }

      await loadProfiles();
      renderWelcomeProfiles();
    } catch (_) { /* non-fatal */ }
  }

  // -------------------------------------------------------------------------
  // Helpers
  // -------------------------------------------------------------------------

  function showError(msg) {
    errorBox.textContent = msg;
    errorBox.classList.remove('hidden');
  }

  function clearError() {
    errorBox.textContent = '';
    errorBox.classList.add('hidden');
  }

  function setLoading(loading) {
    connectBtn.disabled = loading;
    connectLabel.textContent = loading ? 'Connecting…' : 'Connect';
    connectSpinner.classList.toggle('hidden', !loading);
  }

  window.showConnectionDialog  = showConnectionDialog;
  window.hideConnectionDialog  = hideConnectionDialog;
  window.renderWelcomeProfiles = renderWelcomeProfiles;

})();
