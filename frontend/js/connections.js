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
  let typeSelect, serialPortSelect, serialPortHint;

  /** Default TCP port per transport, applied when the type changes. */
  const DEFAULT_PORTS = { ssh: 22, 'ssh-key': 22, telnet: 23 };

  /**
   * The picker's entries, and the transport each one actually is.
   *
   * "SSH — password" and "SSH — key or jump host" are two forms over one
   * transport. They must stay one `connection_type` underneath, because
   * several things key off it and would break quietly:
   * `profiles.identity()` treats it as part of what makes two saved
   * connections the same device, so a separate value would make the same
   * switch saved both ways into two profiles and stop the dedupe seeing them;
   * `ready_to_connect`, the discovery scanner's `suggested_type` and every
   * existing profiles.json all say `ssh`.
   *
   * So the split is in the interface and `auth_method` carries which form was
   * used. Nothing below the dialog changes.
   */
  const TRANSPORT = { ssh: 'ssh', 'ssh-key': 'ssh', telnet: 'telnet', serial: 'serial' };
  const AUTH_METHOD = { ssh: 'password', 'ssh-key': 'key' };

  /**
   * Profile the dialog was opened from, if any.
   *
   * Sent with the connect request so the backend can fill in credentials the
   * user asked it to remember, and so newly remembered ones are filed against
   * the right profile.
   */
  let activeProfileId = '';
  let activeProfileHasCredentials = false;
  /** Where this profile's credentials are kept: "vault", "plaintext" or "". */
  let activeProfileStorage = '';

  document.addEventListener('DOMContentLoaded', () => {
    overlay          = document.getElementById('modal-overlay');
    form             = document.getElementById('connection-form');
    errorBox         = document.getElementById('form-error');
    connectBtn       = document.getElementById('btn-connect');
    connectLabel     = document.getElementById('btn-connect-label');
    connectSpinner   = document.getElementById('btn-connect-spinner');
    typeSelect       = document.getElementById('field-conntype');
    serialPortSelect = document.getElementById('field-serial-port');
    serialPortHint   = document.getElementById('serial-port-hint');

    renderWelcomeProfiles();

    // What this opens is a preference now: the empty dialog, the last device,
    // or a chosen saved connection. openNewTabTarget resolves that and calls
    // back into showConnectionDialog, so there is still one dialog.
    document.getElementById('btn-new-tab')
      .addEventListener('click', () => {
        if (typeof window.openNewTabTarget === 'function') window.openNewTabTarget();
        else showConnectionDialog();
      });

    document.getElementById('btn-welcome-connect')
      .addEventListener('click', () => showConnectionDialog());

    // Home-view doors (#233). Each guards its opener: a missing module must
    // cost a dead button, not the whole dashboard.
    [['welcome-link-docs',    () => window.openDocs],
     ['welcome-link-history', () => window.openHistory],
     ['welcome-link-keys',    () => window.openKeys],
     ['welcome-link-support', () => window.openSupport],
    ].forEach(([id, opener]) => {
      const el = document.getElementById(id);
      if (el) el.addEventListener('click', () => {
        const fn = opener();
        if (typeof fn === 'function') fn();
      });
    });

    document.getElementById('modal-close')
      .addEventListener('click', hideConnectionDialog);

    document.getElementById('btn-cancel')
      .addEventListener('click', hideConnectionDialog);

    document.getElementById('btn-save-profile')
      .addEventListener('click', handleSaveProfile);

    document.getElementById('btn-refresh-ports')
      .addEventListener('click', () => loadSerialPorts());

    bindCredentialStorage();

    const forgetBtn = document.getElementById('btn-forget-credentials');
    if (forgetBtn) {
      forgetBtn.addEventListener('click', (e) => {
        e.preventDefault();
        forgetSavedCredentials();
      });
    }

    typeSelect.addEventListener('change', () => applyConnectionType(typeSelect.value));

    // Changing the groups changes what is inherited (#545), so the notice
    // follows the field rather than only what the dialog opened with.
    const tagsField = document.getElementById('field-tags');
    if (tagsField) tagsField.addEventListener('input', refreshInherited);

    ['field-baud', 'field-databits', 'field-parity', 'field-stopbits', 'field-flow']
      .forEach(id => {
        const el = document.getElementById(id);
        if (el) el.addEventListener('change', () => { el.dataset.userEdited = '1'; });
      });

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
    loadGroupChoices();
    clearError();
    form.reset();
    loadCredentialChoices();

    activeProfileId = (prefill && prefill.id) || '';
    activeProfileHasCredentials = Boolean(prefill && prefill.has_saved_credentials);
    activeProfileStorage = (prefill && prefill.credential_storage) || '';

    // The picker's value, not the transport — a key profile has to open on
    // the key form, and both store connection_type "ssh".
    const type = prefill ? pickerValue(prefill) : 'ssh';
    typeSelect.value = type;
    applyConnectionType(type);

    if (prefill) fillFromProfile(prefill);
    // What the groups named in the field lend (#545). Painted from what the
    // listing already resolved where there is one, then re-asked as the
    // Groups field is edited.
    paintInherited(prefill && prefill.inherited
      ? { values: prefill.inherited, from: prefill.inherited_from,
          conflicts: prefill.inherit_conflicts }
      : { values: {}, from: {}, conflicts: {} });
    refreshInherited();
    updateRememberHint();
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

  // -------------------------------------------------------------------------
  // What the groups lend this connection (#545)
  //
  // A blank field is not necessarily empty: a connection in `site-004` whose
  // jump host field is blank still goes through the site's bastion. Saying so
  // where the blank is, rather than leaving somebody to wonder why a field
  // they never filled in works, is the whole of this.
  //
  // Resolved by the server, never here. `/api/groups/defaults` answers with
  // the same function every connect path uses, so what the dialog says and
  // what the connection does cannot drift apart.
  // -------------------------------------------------------------------------

  /** Which input shows which inherited field. */
  const INHERITED_FIELDS = {
    username:      'field-username',
    port:          'field-port',
    jump_host:     'field-jump-host',
    jump_port:     'field-jump-port',
    jump_username: 'field-jump-username',
  };

  /** What each field is called in a sentence. */
  const INHERITED_LABELS = {
    username:       'username',
    port:           'port',
    platform:       'platform',
    credential_ref: 'shared credential',
    jump_host:      'jump host',
    jump_port:      'jump host port',
    jump_username:  'jump host username',
  };

  let inheritTimer = null;

  /** Ask what the groups currently named in the field lend, and show it. */
  function refreshInherited() {
    clearTimeout(inheritTimer);
    inheritTimer = setTimeout(async () => {
      const tags = value('field-tags');
      if (!tags) { paintInherited({ values: {}, from: {}, conflicts: {} }); return; }
      try {
        const res = await fetch('/api/groups/defaults?tags=' + encodeURIComponent(tags));
        paintInherited(await res.json());
      } catch (_) {
        // A failed lookup says nothing rather than something wrong.
        paintInherited({ values: {}, from: {}, conflicts: {} });
      }
    }, 150);
  }

  /** Draw the notice, and the placeholder in each blank field it fills. */
  function paintInherited(resolved) {
    const values    = (resolved && resolved.values) || {};
    const from      = (resolved && resolved.from) || {};
    const conflicts = (resolved && resolved.conflicts) || {};

    Object.entries(INHERITED_FIELDS).forEach(([field, id]) => {
      const el = document.getElementById(id);
      if (!el) return;
      // The placeholder the markup shipped with, kept so it can come back
      // when the connection leaves the group.
      if (el.dataset.ownPlaceholder === undefined) {
        el.dataset.ownPlaceholder = el.placeholder || '';
      }
      el.placeholder = (values[field] !== undefined && from[field])
        ? values[field] + ' \u2014 from ' + from[field]
        : el.dataset.ownPlaceholder;
    });

    const notice = document.getElementById('inherit-notice');
    if (notice) {
      const lines = Object.keys(values).map(field =>
        (INHERITED_LABELS[field] || field) + ' ' + values[field]
        + ' (from ' + from[field] + ')');
      notice.textContent = lines.length
        ? 'Left blank, this connection inherits: ' + lines.join(', ') + '.'
        : '';
      notice.classList.toggle('hidden', !lines.length);
    }

    const warn = document.getElementById('inherit-conflict');
    if (warn) {
      // Named in full. "Some fields conflict" would leave somebody opening
      // every group in the list to find out which two disagree.
      const lines = Object.entries(conflicts).map(([field, keys]) =>
        keys.join(' and ') + ' disagree about the '
        + (INHERITED_LABELS[field] || field));
      warn.textContent = lines.length
        ? lines.join('; ') + ' \u2014 so nothing is inherited for '
          + (lines.length === 1 ? 'it' : 'them') + '. Fill it in here.'
        : '';
      warn.classList.toggle('hidden', !lines.length);
    }
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

    if (type === 'serial') {
      loadSerialPorts();
      applySerialDefaults();
    }
  }

  /**
   * Start a serial connection from the defaults set in Settings.
   *
   * Only fills fields the user has not already changed in this dialog, so
   * switching connection type back and forth does not wipe what they typed.
   */
  function applySerialDefaults() {
    const d = (window.shellmateSettings || {}).serial || {};
    if (!Object.keys(d).length) return;
    const pairs = [
      ['field-baud',     d.baud_rate],
      ['field-databits', d.data_bits],
      ['field-parity',   d.parity],
      ['field-stopbits', d.stop_bits],
      ['field-flow',     d.flow_control],
    ];
    pairs.forEach(([id, value]) => {
      const el = document.getElementById(id);
      if (el && value !== undefined && value !== null && !el.dataset.userEdited) {
        el.value = String(value);
      }
    });
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

  /** Where a profile points, for tooltips and card subtitles. */
  function profileTarget(p) {
    if (p.connection_type === 'serial') return `${p.serial_port || '?'} @ ${p.baud_rate || 9600}`;
    return `${p.hostname || '?'}:${p.port || 22}`;
  }

  /**
   * The tile icon.
   *
   * A key connection is worth telling apart at a glance: when one fails the
   * cause is usually the key rather than a password, and knowing which kind
   * it is before clicking is most of the diagnosis.
   */
  function profileIcon(profile) {
    const type = typeof profile === 'string' ? profile : (profile.connection_type || 'ssh');
    if (type === 'serial') return 'cable';
    if (type === 'ssh' && pickerValue(
        typeof profile === 'string' ? { connection_type: profile } : profile) === 'ssh-key') {
      return 'key';
    }
    return 'terminal';
  }


  // -------------------------------------------------------------------------
  // Grouping the welcome screen
  //
  // Past roughly twenty tiles, finding the right one is a visual scan of an
  // unsorted grid. Better *names* — which record_detected_hostname() already
  // gives — do not help you find core-sw-04 among two hundred siblings.
  // -------------------------------------------------------------------------

  // The tag chip row lived here. It filtered the grid by tag, which is
  // exactly what a group tile does — so it went rather than sitting beside
  // the groups as a second way to do one thing. groups.js owns which group is
  // open; this file only asks.

  /**
   * Open a session to every device carrying a tag.
   *
   * Confirmed with the count, because forty tabs appearing at once is not
   * something to do on a mis-click — and the backend paces the handshakes so
   * a bastion is not buried by them.
   */
  window.connectTag = connectTag;
  // The same thing for a hand-picked set rather than a whole group (#537).
  window.connectMany = connectMany;
  // The tree's leaves connect the same way a dashboard tile does. A second
  // connect path would be one more thing to keep in step — openProfile
  // already switches to an existing tab rather than opening a duplicate.
  window.connectProfile = (profile) => openProfile(profile, null);

  async function connectTag(tag, count) {
    const ok = await (window.shellmateDialog
      ? window.shellmateDialog.confirm({
          title: `Open ${count} connection${count === 1 ? '' : 's'} tagged "${tag}"?`,
          body: 'Each opens its own tab. Any that cannot connect are reported '
                + 'rather than silently skipped.',
          confirmLabel: 'Open them',
        })
      : Promise.resolve(window.confirm(`Open ${count} connections tagged ${tag}?`)));
    if (!ok) return;

    await openBatch(`/api/tags/${encodeURIComponent(tag)}/connect`, null,
                    `Everything tagged "${tag}" is connected.`);
  }

  /**
   * Open a session to each of a hand-picked set of connections (#537).
   *
   * The tag route answers "connect this group"; this answers "connect these
   * six", which is the question somebody has after looking at a tree and
   * picking out the ones that matter. Same backend pacing, same per-device
   * reporting - one route, so the two cannot drift.
   */
  async function connectMany(ids, what) {
    const count = (ids || []).length;
    if (!count) return;

    const ok = await (window.shellmateDialog
      ? window.shellmateDialog.confirm({
          title: `Open ${count} connection${count === 1 ? '' : 's'}?`,
          body: 'Each opens its own tab. Any that cannot connect are reported '
                + 'rather than silently skipped.',
          confirmLabel: 'Open them',
        })
      : Promise.resolve(window.confirm(`Open ${count} connections?`)));
    if (!ok) return;

    await openBatch('/api/sessions/many', { profile_ids: ids },
                    `${what || 'The selection'} is connected.`);
  }

  /**
   * Post a batch connect and turn what comes back into tabs.
   *
   * Every result is accounted for. A device that failed is named with the
   * reason - the whole point of opening fifty at once is not watching each
   * one, so anything that did not open has to come and find you.
   */
  async function openBatch(url, body, allWell) {
    try {
      const res = await fetch(url, {
        method:  'POST',
        headers: body ? { 'Content-Type': 'application/json' } : undefined,
        body:    body ? JSON.stringify(body) : undefined,
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || 'Could not open them.');

      (data.results || []).forEach(r => {
        if (r.ok && typeof window.createTab === 'function') window.createTab(r.session);
      });

      const failed = (data.results || []).filter(r => !r.ok);
      if (window.shellmateAlerts && window.shellmateAlerts.notify) {
        window.shellmateAlerts.notify({
          severity: failed.length ? 'warning' : 'info',
          icon: failed.length ? 'warning' : 'check_circle',
          title: `${data.opened} of ${data.total} opened`,
          body: failed.length
            ? failed.map(r => `${r.name}: ${r.error}`).join('\n')
            : allWell,
        });
      }
    } catch (e) {
      if (window.shellmateAlerts && window.shellmateAlerts.notify) {
        window.shellmateAlerts.notify({
          severity: 'warning', icon: 'error',
          title: 'Could not open them', body: e.message,
        });
      }
    }
  }

  /**
   * Every saved connection, fetched once and kept (#188).
   *
   * 1.64 MB and ~190ms to build at estate size, re-downloaded on every group
   * click even though selecting a group cannot change it. Refilled when
   * something actually changes a profile, which is what
   * `renderWelcomeProfiles()` now means.
   */
  let profileCache = [];

  async function renderWelcomeProfiles() {
    try {
      const res = await fetch('/api/profiles');
      profileCache = await res.json();
    } catch (e) {
      return;   // silently skip if API unavailable
    }
    failedRecently.clear();
    await renderWelcomeTiles();
  }

  /** Draw from what is already loaded. No fetches. */
  async function renderWelcomeTiles() {
    const grid = document.getElementById('welcome-profiles-grid');
    if (!grid) return;
    try {
      const profiles = profileCache;
      grid.innerHTML = '';

      // The group row replaces the tag chips: both filtered the grid by tag,
      // so keeping the two would have been one job done twice.
      if (window.shellmateGroups) await window.shellmateGroups.render(profiles);
      const openGroup = window.shellmateGroups
        ? window.shellmateGroups.active() : '';

      _paintHero();

      // The dashboard shows what you have actually used, and never a list of
      // devices (#268) — not at the root, not inside a group. The tree lists
      // every connection under its group and launches them on click, so a
      // grid of the same names was the same job done twice, and at five
      // thousand it was the slowest paint in the application.
      const shown = _recentTiles(profiles);

      _paintEmptyState(openGroup, profiles.length, shown.length);

      shown.forEach(p => {
        const wrap = document.createElement('div');
        wrap.className = 'welcome-profile-wrap';

        const card = document.createElement('button');
        card.className = 'welcome-profile-card';
        // Dropped onto a group tile, this joins that group. Its own type, not
        // text/plain, so a group being dragged and a connection being dragged
        // cannot be mistaken for one another.
        card.draggable = true;
        card.addEventListener('dragstart', (e) => {
          e.dataTransfer.effectAllowed = 'copy';
          e.dataTransfer.setData('application/x-shellmate-profile', p.id);
        });
        card.title = `${profileTarget(p)} (${(p.connection_type || 'ssh').toUpperCase()})`;
        card.innerHTML = `
          <span class="material-symbols-outlined welcome-profile-icon">
            ${profileIcon(p)}
          </span>
          <span class="welcome-profile-name"></span>
          <span class="welcome-profile-host"></span>
        `;
        // Set via textContent, not innerHTML — a profile name is user input
        // and must never be parsed as markup.
        card.querySelector('.welcome-profile-name').textContent = p.name || '';

        // A padlock where a credential is already stored (#171), so the
        // connections that will just open are distinguishable from the ones
        // that will ask. has_saved_credentials is a boolean the backend
        // already sends — never the credential itself.
        if (p.has_saved_credentials) {
          const lock = document.createElement('span');
          lock.className = 'material-symbols-outlined welcome-profile-lock';
          lock.textContent = 'key';
          lock.title = 'A saved credential is stored for this connection';
          card.querySelector('.welcome-profile-name').appendChild(lock);
        }
        card.querySelector('.welcome-profile-host').textContent =
          p.connection_type === 'serial' ? (p.serial_port || '') : (p.hostname || '');
        // A recent connection that was never saved has nothing to connect
        // with — it opens the dialog prefilled rather than pretending (#268).
        card.addEventListener('click', () => {
          if (p._unsaved) showConnectionDialog(p);
          else openProfile(p, card);
        });

        // Editing a saved connection needs its own route once clicking
        // connects. Right-click is the one people try first.
        card.addEventListener('contextmenu', (e) => {
          e.preventDefault();
          showConnectionDialog(p);
        });
        if (!p.ready_to_connect && p.reason_not_ready) {
          card.title += ` — ${p.reason_not_ready} Click to fill in the rest.`;
        } else if (p.ready_to_connect) {
          card.title += ' — click to connect, right-click to edit';
        }

        wrap.appendChild(card);

        // The × deletes a saved connection, so it has no business on a tile
        // that only represents something recently opened (#268). Clearing
        // the list is the whole-list action beneath the grid.
        if (!p._unsaved && p.id) {
          const del = document.createElement('button');
          del.className = 'welcome-profile-delete';
          del.title = 'Delete this saved connection';
          del.innerHTML = '<span class="material-symbols-outlined">close</span>';
          del.addEventListener('click', async (e) => {
            e.stopPropagation();
            await fetch(`/api/profiles/${p.id}`, { method: 'DELETE' });
            renderWelcomeProfiles();
          });
          wrap.appendChild(del);
        }

        grid.appendChild(wrap);
      });
    } catch (e) { /* silently skip if API unavailable */ }
  }

  /**
   * Name the selection in the heading (#179).
   *
   * The group key already carries the path — `site-004/firewalls` — so both
   * halves are in it and this only has to split it. The last part is the
   * heading and everything above it is the trail, which is what makes a
   * subgroup called "firewalls" mean something on its own.
   */
  function _paintHero() {
    const title = document.getElementById('welcome-title');
    const trail = document.getElementById('welcome-breadcrumb');
    if (!title || !trail) return;

    const logo = document.getElementById('welcome-logo');
    const glyph = document.getElementById('welcome-hero-icon');
    const path = window.shellmateGroups ? window.shellmateGroups.activePath() : [];

    if (!path.length) {
      title.textContent = 'ShellMate Portable';
      trail.classList.add('hidden');
      trail.textContent = '';
      if (logo) logo.classList.remove('hidden');
      if (glyph) glyph.classList.add('hidden');
      return;
    }

    // The group's own icon takes the logo's place. The logo is the
    // application saying its name, which is not what the heading is about
    // once it is naming a group instead.
    if (logo) logo.classList.add('hidden');
    if (glyph) {
      glyph.textContent = (window.shellmateGroups.activeIcon() || 'folder');
      glyph.classList.remove('hidden');
    }

    title.textContent = path[path.length - 1];
    if (path.length > 1) {
      trail.textContent = path.slice(0, -1).join(' / ');
      trail.classList.remove('hidden');
    } else {
      trail.classList.add('hidden');
      trail.textContent = '';
    }
  }

  /**
   * What appears where the tiles are not (#177).
   *
   * An empty panel reads as broken, and "select a group" is the one thing
   * somebody landing on an empty dashboard needs told.
   */
  function _paintEmptyState(openGroup, total, recentCount) {
    const box = document.getElementById('welcome-empty');
    if (!box) return;

    box.classList.remove('hidden');
    box.textContent = '';

    if (recentCount) {
      // A heading rather than an apology: the tiles above are what you have
      // been working on, and the tree beside them is everything else (#268).
      const line = document.createElement('span');
      line.textContent = openGroup
        ? `Recently used. ${total} saved connection${total === 1 ? '' : 's'} — `
          + 'pick one from the tree on the left.'
        : `Recently used. ${total} saved connection${total === 1 ? '' : 's'} — `
          + 'the tree on the left has them all.';
      const clear = document.createElement('button');
      clear.type = 'button';
      clear.className = 'btn-tertiary welcome-clear-recents';
      clear.textContent = 'Clear';
      clear.title = 'Forget the recently-used list';
      clear.addEventListener('click', _clearRecents);
      box.append(line, clear);

      // Open the lot (#537). Only the ones that would open on a click of
      // their own tile: a recent connection that was never saved has nothing
      // to connect with, and one with no credential would only stall the
      // batch on a dialog.
      const ready = _recentTiles(profileCache)
        .filter(p => p.id && p.ready_to_connect);
      if (ready.length > 1) {
        const all = document.createElement('button');
        all.type = 'button';
        all.className = 'btn-tertiary welcome-clear-recents';
        all.textContent = `Connect ${ready.length}`;
        all.title = 'Open a tab for each of the recent connections that has '
                  + 'what it needs to connect';
        all.addEventListener('click',
          () => connectMany(ready.map(p => p.id), 'The recent connections'));
        box.appendChild(all);
      }
      return;
    }

    box.textContent = total
      ? `${total} saved connection${total === 1 ? '' : 's'} — pick one from the `
        + 'tree on the left, or start a new connection.'
      : 'No saved connections yet. Start one with New Connection.';
  }

  /**
   * The recently-used list, as things the dashboard can paint (#268).
   *
   * Written by tabs.js as connections are opened. Each entry is resolved
   * back to its saved profile where one exists, so the tile connects and
   * carries the padlock; an ad-hoc connection with nothing saved is still
   * shown, and opens the dialog prefilled rather than pretending it can
   * connect on one click.
   */
  function _recentTiles(profiles) {
    const recents =
      ((window.shellmateSettings || {}).interface || {}).recent_connections || [];
    return recents.map(entry => {
      if (entry.profile_id) {
        const saved = profiles.find(p => p.id === entry.profile_id);
        if (saved) return saved;
      }
      const match = profiles.find(p =>
        (p.connection_type || 'ssh') === (entry.connection_type || 'ssh')
        && (p.hostname || '') === (entry.hostname || '')
        && Number(p.port || 0) === Number(entry.port || 0));
      return match || {
        id: '',
        name: entry.label || entry.hostname || '',
        hostname: entry.hostname || '',
        port: entry.port || 0,
        connection_type: entry.connection_type || 'ssh',
        tags: [],
        _unsaved: true,
      };
    }).filter(Boolean).slice(0, 8);
  }

  /** Forget the recently-used list (#268). */
  async function _clearRecents() {
    if (window.shellmatePrefs) window.shellmatePrefs.set('recent_connections', []);
    await renderWelcomeTiles();
  }

  /**
   * Click handler for a saved-device tile.
   *
   * If a tab is already open for this profile, switch to it rather than
   * opening a second session to the same device. Otherwise open the dialog
   * pre-filled.
   */
  async function openProfile(p, card) {
    try {
      // From the tab strip, not a round trip to /api/sessions (#493): the
      // tabs already carry the profile id, the dialled address, port and
      // username, and they *are* the list of what is open.
      const open = (typeof window.getOpenTabs === 'function') ? window.getOpenTabs() : [];
      const match = open.find(t => sameTarget({
        profile_id:      t.profileId,
        connection_type: t.connectionType,
        // The address that was dialled. `hostname` on a tab is rewritten
        // with whatever the device calls itself, which is not what a saved
        // connection holds.
        hostname:        t.address,
        port:            t.port,
        username:        t.username,
      }, p));
      if (match && typeof window.switchToTabBySessionId === 'function') {
        window.switchToTabBySessionId(match.sessionId);
        return;
      }
    } catch (_) { /* fall through to dialog */ }

    // Everything it needs is already saved, so connect. The dialog would only
    // be showing filled-in fields and a Connect button.
    if (p.ready_to_connect && !failedRecently.has(p.id)) {
      connectFromTile(p, card);
      return;
    }

    showConnectionDialog(p);
  }

  /**
   * Profiles whose last click failed.
   *
   * A second click after a failure opens the dialog instead of repeating the
   * attempt — the password may have changed, or the address may be wrong, and
   * silently trying the same thing again is the one response that cannot
   * help. Cleared on a success, and when the list is next rebuilt.
   */
  const failedRecently = new Set();

  /**
   * POST /api/sessions, answering a keyboard-interactive challenge (#406).
   *
   * A 409 carrying `interactive` means the device asked something only the
   * user can answer — a one-time code, a second factor. This puts the
   * prompts up as a small form and posts again with the answers, up to
   * three rounds, so every caller gets MFA for free. Resolves to
   * `{ response, data }` exactly as a plain fetch would.
   */
  async function postSession(payload) {
    let body = Object.assign({}, payload);
    for (let round = 0; round < 3; round++) {
      const response = await fetch('/api/sessions', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify(body),
      });
      const data = await response.json().catch(() => ({}));
      const ask = response.status === 409 && data.detail && data.detail.interactive;
      if (!ask) return { response, data };

      const answers = await askInteractive(data.detail.interactive, body.hostname);
      if (answers === null) {
        return { response: { ok: false, status: 409 },
                 data: { detail: 'Cancelled: the device was still asking.' } };
      }
      body = Object.assign({}, body, { interactive_answers: answers });
    }
    return { response: { ok: false, status: 409 },
             data: { detail: 'The device kept asking after three attempts.' } };
  }

  /** Put the device's prompts to the user; null if they cancel. */
  async function askInteractive(challenge, hostname) {
    const prompts = (challenge && challenge.prompts) || [];
    if (!prompts.length) return [];
    const fields = prompts.map((p, i) => ({
      name: `p${i}`,
      label: (p.text || `Prompt ${i + 1}`).replace(/:\s*$/, ''),
      type: p.echo ? 'text' : 'password',
    }));
    const answer = await window.shellmateDialog.form({
      title: challenge.title || `${hostname || 'The device'} is asking`,
      body:  challenge.instructions
        || 'The device wants an answer beyond the password — a code, a second factor, or a confirmation.',
      confirmLabel: 'Send',
      fields,
    });
    if (!answer) return null;
    return fields.map(f => String(answer[f.name] || ''));
  }

  window.postSession = postSession;

  /**
   * Connect straight from a tile.
   *
   * The credentials are never sent from here: `POST /api/sessions` takes the
   * profile id and fills them in server-side, which is the whole reason a
   * saved password can stay out of the browser.
   */
  async function connectFromTile(p, card) {
    if (card) card.classList.add('connecting');
    clearTileError(card);

    try {
      // Field names must match CreateSessionRequest exactly (#309): Pydantic
      // silently drops unknown keys, so the old `label`/`baudrate` spellings
      // meant every tile connect lost its display name and every serial
      // profile opened at the 9600 default — a saved 115200 console showed
      // garbage. The serial framing fields ride along for the same reason.
      const { response, data } = await postSession({
          profile_id:      p.id,
          connection_type: p.connection_type || 'ssh',
          hostname:        p.hostname || '',
          port:            p.port || DEFAULT_PORTS[p.connection_type] || 22,
          username:        p.username || '',
          display_label:   p.name || '',
          serial_port:     p.serial_port || '',
          baud_rate:       p.baud_rate || 9600,
          data_bits:       p.data_bits || 8,
          parity:          p.parity || 'N',
          stop_bits:       p.stop_bits || 1,
          flow_control:    p.flow_control || 'none',
      });

      if (!response.ok) throw new Error(data.detail || `Server error ${response.status}`);

      failedRecently.delete(p.id);
      if (typeof window.createTab === 'function') window.createTab(data);

    } catch (err) {
      // Surfaced on the tile, not in the terminal pane — the click happened
      // here, and there is no terminal to look at yet.
      failedRecently.add(p.id);
      showTileError(card, err.message || 'Could not connect.');
    } finally {
      if (card) card.classList.remove('connecting');
    }
  }

  /**
   * Report a failed connection.
   *
   * The message goes to a notification rather than under the tile. A tile is
   * 110px wide, and an SSH error is a sentence — printed there it wrapped to
   * six lines, pushed every other tile down the page, and was still cut off.
   * The tile keeps a red border and the full text in its tooltip, which is
   * enough to say *which* one failed; the notification says what happened.
   */
  function showTileError(card, message) {
    const name = card
      ? (card.querySelector('.welcome-profile-name') || {}).textContent || ''
      : '';

    if (window.shellmateAlerts && window.shellmateAlerts.notify) {
      window.shellmateAlerts.notify({
        severity: 'warning',
        icon: 'error',
        title: name ? `Could not connect to ${name}` : 'Could not connect',
        body: message,
      });
    }

    if (!card) return;
    card.classList.add('tile-failed');
    card.title = `${message} Click again to check the details.`;
  }

  function clearTileError(card) {
    if (!card) return;
    card.classList.remove('tile-failed');
  }

  /** True when a live session and a profile point at the same device. */
  function sameTarget(session, profile) {
    // Identity first, where the session recorded one (#187).
    //
    // The resemblance test below is what this was, and on an estate where
    // every device sits behind one address it matched all of them: clicking a
    // disconnected device found somebody else's open session and switched to
    // that tab instead of connecting, so only the first device in an estate
    // could ever be opened (#192, #193).
    //
    // A session naming a *different* profile is decisively not this one, so
    // the resemblance test must not get a second opinion. It stays only for
    // sessions opened straight from the dialog, which have no profile.
    if (session.profile_id) return session.profile_id === profile.id;

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

  /**
   * Which of the picker's entries a saved profile belongs to.
   *
   * `auth_method` is what a profile saved since the split carries. Older ones
   * have none, so a stored key path or jump host stands in — those profiles
   * were created through the drawer this replaced, and putting them back in
   * the password form would hide the fields that make them work.
   */
  function pickerValue(p) {
    const kind = p.connection_type || 'ssh';
    if (kind !== 'ssh') return kind;
    if (p.auth_method === 'key') return 'ssh-key';
    if (p.auth_method === 'password') return 'ssh';
    return (p.private_key_path || p.jump_host) ? 'ssh-key' : 'ssh';
  }

  /**
   * Offer the shared credentials, by name.
   *
   * Never values: the backend resolves the reference the same way it resolves
   * a profile id, which is what keeps a saved password out of the browser.
   */
  /**
   * Show which of the two is actually in force.
   *
   * Picking a saved credential makes the typed password irrelevant — it is
   * dropped on submit (`remember_credentials: !value('field-credential')`)
   * and the backend resolves the reference instead. That was correct and
   * completely invisible: you could type a password, choose a credential, and
   * watch what you typed be ignored with nothing to say so.
   *
   * Disabled rather than hidden. A field that vanishes suggests it was never
   * relevant; one that greys out says it has been superseded, which is what
   * has happened.
   */
  /** A reference waiting for the picker's options to arrive (#159). */
  let _pendingCredentialRef = '';

  function _applyPendingCredential() {
    const select = document.getElementById('field-credential');
    if (!select) return;
    if (_pendingCredentialRef &&
        [...select.options].some(o => o.value === _pendingCredentialRef)) {
      select.value = _pendingCredentialRef;
      _pendingCredentialRef = '';
    }
    _syncCredentialChoice();
  }

  function _syncCredentialChoice() {
    const select = document.getElementById('field-credential');
    const password = document.getElementById('field-password');
    if (!select || !password) return;

    const usingSaved = Boolean(select.value);
    password.disabled = usingSaved;
    password.placeholder = usingSaved
      ? 'not needed — the saved credential supplies it'
      : '';
    password.classList.toggle('field-superseded', usingSaved);

    // Remembering is meaningless once a saved credential is chosen (#160):
    // there is nothing typed to remember, and the credential is already
    // stored. Offering it invites keeping a second copy of the very thing
    // that exists so it is kept once.
    ['field-remember', 'field-remember-plain'].forEach(id => {
      const box = document.getElementById(id);
      if (!box) return;
      box.disabled = usingSaved;
      if (usingSaved) box.checked = false;
      const row = box.closest('.checkbox-row');
      if (row) row.classList.toggle('field-superseded', usingSaved);
    });
  }

  /**
   * Offer the groups that exist, and prefill the one being looked at (#172).
   *
   * A connection could only be filed after it was made — dragged onto a
   * group, or created while one was open. The field for it existed and was
   * called "tags", which is what they are but not what anybody was looking
   * for.
   */
  async function loadGroupChoices() {
    const list = document.getElementById('known-groups');
    const field = document.getElementById('field-tags');
    if (!list || !field) return;

    try {
      const res = await fetch('/api/groups');
      const data = res.ok ? await res.json() : { groups: [] };
      list.innerHTML = '';
      (data.groups || []).forEach(group => {
        const option = document.createElement('option');
        option.value = group.key;
        option.label = `${group.name} (${group.count})`;
        list.appendChild(option);
      });
    } catch (_) { /* the field still takes free text */ }

    // Prefilled from the group on screen, so creating a connection from
    // inside one files it there without a second action. Only when the field
    // is empty: an explicit answer always wins over a helpful guess.
    const open = window.shellmateGroups ? window.shellmateGroups.active() : '';
    if (open && !field.value.trim()) field.value = open;
  }

  async function loadCredentialChoices() {
    const row = document.getElementById('field-credential-row');
    const select = document.getElementById('field-credential');
    if (!row || !select) return;

    // Bound once. The list is rebuilt every time the dialog opens, and
    // rebinding on each would stack a listener per open.
    if (!select.dataset.bound) {
      select.dataset.bound = '1';
      select.addEventListener('change', _syncCredentialChoice);
    }

    select.innerHTML = '';
    const none = document.createElement('option');
    none.value = '';
    // "or use the password above" cancelled itself out: "or use" offers an
    // alternative and "the password above" is the thing the alternative
    // replaces, so the default state of the selector was phrased as if it
    // were one of the choices. Named for its consequence instead, which is
    // the pattern the scanner's save dialog already uses ("None — ask me on
    // first connect").
    none.textContent = 'None — use the password above';
    select.appendChild(none);

    try {
      const res = await fetch('/api/credential-sets');
      if (!res.ok) throw new Error();
      const data = await res.json();
      // Every set, not only the ones already holding a secret. Filtering
      // meant a credential created but not yet given a password was simply
      // absent from the dialog, with nothing saying why — and the seeded
      // estate shipped in exactly that state (#175). An option you can see
      // and are told about beats one that is not there.
      const usable = data.sets || [];

      if (data.vault_locked) {
        // Saying so beats presenting an empty list as "nothing saved".
        const locked = document.createElement('option');
        locked.value = '';
        locked.disabled = true;
        locked.textContent = 'vault locked — unlock it to use a saved credential';
        select.appendChild(locked);
      }

      usable.forEach(s => {
        const option = document.createElement('option');
        option.value = s.id;
        option.textContent = (s.username ? `${s.name} (${s.username})` : s.name)
                           + (s.has_credentials ? '' : ' — no password saved yet');
        select.appendChild(option);
      });

      row.hidden = !usable.length && !data.vault_locked;
      const hint = document.getElementById('field-credential-hint');
      // Shown with the row, because the row is hidden until somebody owns a
      // shared credential — so the first sight of it is also the first time
      // they might wonder what one is.
      if (hint) hint.hidden = row.hidden;
      // The options exist now, so a reference recorded before they arrived
      // can finally be selected.
      _applyPendingCredential();
    } catch (e) {
      row.hidden = true;
    }
  }

  function fillFromProfile(p) {
    setField('field-label', p.name || '');
    setField('field-tags', (p.tags || []).join(', '));
    setField('field-conntype', pickerValue(p));

    setField('field-hostname', p.hostname || '');
    setField('field-port', p.port || DEFAULT_PORTS[p.connection_type] || 22);
    setField('field-username', p.username || '');

    setField('field-key-path', p.private_key_path || '');
    setField('field-key-username', p.private_key_username || '');
    setField('field-jump-host', p.jump_host || '');
    setField('field-jump-port', p.jump_port || 22);
    setField('field-jump-username', p.jump_username || '');

    setField('field-serial-port', p.serial_port || '');
    setField('field-baud', p.baud_rate || 9600);
    setField('field-databits', p.data_bits || 8);
    setField('field-parity', p.parity || 'N');
    setField('field-stopbits', p.stop_bits || 1);
    setField('field-flow', p.flow_control || 'none');

    // The saved credential this connection uses (#159). The reference was
    // stored and read back correctly all along — nothing restored it into
    // the picker, so reopening showed "None" and the choice looked lost.
    //
    // Recorded rather than applied: loadCredentialChoices() fills the options
    // from a fetch, and a value set before they exist is discarded silently.
    // A one-tick defer is not enough either — it has to be the loader that
    // applies it, once there is something to select.
    _pendingCredentialRef = p.credential_ref || '';
    _applyPendingCredential();

    // Nothing to open any more: the key form is its own connection type
    // rather than a drawer, so restoring a key profile shows its fields.
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
    const plain    = document.getElementById('field-remember-plain');
    const wantsPlain = Boolean(plain && plain.checked);

    const payload = {
      connection_type: TRANSPORT[type] || type,
      // Which form was filled in. Not a transport — see TRANSPORT above.
      auth_method:     AUTH_METHOD[type] || '',
      display_label:   value('field-label'),
      // Free text, split here and normalised server-side — one definition of
      // what a tag is, next to where they are written.
      tags:            value('field-tags').split(',').map(s => s.trim()).filter(Boolean),
      // The backend fills remembered credentials in server-side from this id,
      // so a saved password never travels to the browser.
      profile_id:           activeProfileId,
      // Choosing a saved credential and asking to remember a new one are
      // contradictory, so one suppresses the other rather than both being
      // sent and the backend picking.
      credential_ref:       value('field-credential'),
      remember_credentials: !value('field-credential')
                            && (wantsPlain || Boolean(remember && remember.checked)),
      credential_storage:   wantsPlain ? 'plaintext' : 'vault',
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

    if (type === 'ssh-key') {
      Object.assign(payload, {
        private_key_path:       value('field-key-path'),
        private_key_username:   value('field-key-username'),
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
    const hint  = document.getElementById('remember-hint');
    const box   = document.getElementById('field-remember');
    const plain = document.getElementById('field-remember-plain');
    const badge = document.getElementById('saved-credential-badge');
    if (!hint || !box) return;

    if (badge) badge.classList.toggle('hidden', !activeProfileHasCredentials);

    if (activeProfileHasCredentials) {
      hint.textContent = 'Leave the password blank to use the saved one, or type a new one to replace it.';
      // Tick whichever store this profile's credentials are actually in, so
      // reconnecting cannot silently move a password from one to the other.
      const plaintext = activeProfileStorage === 'plaintext';
      box.checked   = !plaintext;
      if (plain) plain.checked = plaintext;

      // And say which it is. The badge used to read "saved in vault"
      // regardless, which claimed encryption over a password that had none.
      const where = document.getElementById('saved-credential-where');
      if (where) where.textContent = plaintext ? 'saved in plain text' : 'saved in vault';
      if (badge) badge.classList.toggle('saved-badge-plain', plaintext);
      const field = document.getElementById('field-password');
      if (field) field.placeholder = 'Using saved password';
    } else {
      hint.textContent = activeProfileId
        ? 'Saved against this connection once it succeeds.'
        : 'Saved against this connection once it succeeds, encrypted on disk.';
      box.checked = false;
      if (plain) plain.checked = false;
      const field = document.getElementById('field-password');
      if (field) field.placeholder = '';
    }

    updatePlaintextWarning();
  }

  /**
   * Keep the two storage choices exclusive, and say what plaintext means.
   *
   * A credential lives in one place. Two checkboxes that could both be ticked
   * would suggest otherwise, and the user would have no way to tell which had
   * won.
   */
  function bindCredentialStorage() {
    const vaultBox = document.getElementById('field-remember');
    const plainBox = document.getElementById('field-remember-plain');
    if (!vaultBox || !plainBox) return;

    vaultBox.addEventListener('change', () => {
      if (vaultBox.checked) plainBox.checked = false;
      updatePlaintextWarning();
    });
    plainBox.addEventListener('change', () => {
      if (plainBox.checked) vaultBox.checked = false;
      updatePlaintextWarning();
    });
  }

  function updatePlaintextWarning() {
    const plainBox = document.getElementById('field-remember-plain');
    const warning  = document.getElementById('plaintext-warning');
    if (warning && plainBox) warning.classList.toggle('hidden', !plainBox.checked);
  }

  /** Forget the credential stored for the profile currently in the dialog. */
  async function forgetSavedCredentials() {
    if (!activeProfileId) return;
    try {
      await fetch(`/api/profiles/${activeProfileId}/credentials`, { method: 'DELETE' });
      activeProfileHasCredentials = false;
      activeProfileStorage = '';
      updateRememberHint();
      renderWelcomeProfiles();
    } catch (e) {
      showError('Could not forget the saved password.');
    }
  }

  /** Return an error message, or null when the form is good to submit. */
  function validate(payload) {
    if (payload.connection_type === 'serial') {
      return payload.serial_port ? null : 'Choose a serial port.';
    }
    if (!payload.hostname) return 'Hostname is required.';

    if (payload.connection_type === 'ssh') {
      // Three things satisfy it, not one. Someone connecting with a key may
      // have given the account name beside the key rather than above — and a
      // shared credential carries a username of its own, which is the whole
      // reason for not typing one. That third case was added with the picker
      // (#115) and never added here, so the dialog explained the feature and
      // then refused to let it be used.
      if (!payload.username && !payload.private_key_username
          && !payload.credential_ref) {
        return 'Username is required.';
      }
      // A key counts as a credential in its own right, and so does one already
      // in the vault — the backend fills that in, so a blank box is fine.
      //
      // A *shared* credential counts for the same reason and was missing here
      // too. Fixing only the username rule left the dialog refusing on the
      // next line instead, which is why this is one bug and not two: the
      // picker satisfies both requirements, and neither knew about it.
      if (!payload.password && !payload.private_key_path
          && !payload.credential_ref && !activeProfileHasCredentials) {
        return 'Enter a password or choose a private key file.';
      }
    }
    // Telnet needs neither: devices prompt in-band, and leaving the
    // credentials blank simply means the user logs in by hand.
    return null;
  }

  /**
   * The subset of a payload worth persisting — never a secret.
   *
   * The open group is applied here, where the profile body is built, rather
   * than in one caller. It was in autoSaveProfile, which runs only after a
   * *successful connection* — so Save profile, and a connection that failed
   * and was saved anyway, both missed it. Tagging attached to one of three
   * ways a connection is created is the bug; every writer goes through this.
   */
  function profileFrom(payload) {
    const tags = [...(payload.tags || [])];
    const openGroup = window.shellmateGroups
      ? window.shellmateGroups.active() : '';
    if (openGroup && !tags.includes(openGroup)) tags.push(openGroup);

    return {
      name:             payload.display_label || payload.hostname || payload.serial_port,
      tags,
      connection_type:  payload.connection_type,
      // Which of the two SSH forms this connection uses. Not part of its
      // identity — the same switch reached by key and by password is one
      // device, and profiles.identity() has to keep seeing it that way.
      auth_method:      payload.auth_method || '',
      hostname:         payload.hostname || '',
      port:             payload.port || 22,
      username:         payload.username || '',
      private_key_path:     payload.private_key_path || '',
      private_key_username: payload.private_key_username || '',
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
      const r = await fetch('/api/profiles', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify(profileFrom(payload)),
      });
      const saved = r.ok ? await r.json() : null;

      // The shared credential travels with it (#302).
      //
      // profileFrom() deliberately carries no secret, and the credential
      // reference went the same way — so saving a connection with a shared
      // credential chosen produced one that asked for a password anyway.
      // The reference is not a secret: it names a credential the server
      // already holds, and attaching it is the same call the connect path
      // makes.
      if (saved && saved.id) {
        const ref = value('field-credential');
        if (ref) {
          await fetch(`/api/profiles/${saved.id}/credential-set`, {
            method:  'PUT',
            headers: { 'Content-Type': 'application/json' },
            body:    JSON.stringify({ credential_ref: ref }),
          }).catch(() => { /* the connection is saved either way */ });
        }
      }

      renderWelcomeProfiles();

      // The backend returns the existing profile rather than appending a
      // second one. Saying nothing would look exactly like a button that did
      // not work, so say which of the two happened.
      showSaveNote(saved && saved.already_saved
        ? 'Already saved — the existing connection was updated.'
        : 'Connection saved.');
    } catch (e) {
      showError('Could not save profile.');
    }
  }

  /**
   * A short confirmation in the dialog's message area.
   *
   * Reuses the error box rather than adding a second one — two boxes that
   * appear in the same place at different times is a layout that shifts, and
   * the distinction the user needs is the colour, not the position.
   */
  function showSaveNote(text) {
    errorBox.textContent = text;
    errorBox.classList.add('form-note');
    errorBox.classList.remove('hidden');
    setTimeout(() => {
      if (errorBox.classList.contains('form-note')) clearError();
    }, 4000);
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
      const { response, data } = await postSession(payload);
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
      // Offered before the dialog closes, while the fields it prefills from
      // are still on screen (#161).
      const sharedName = wantsRemember
        ? await offerToNameCredential(payload) : '';

      hideConnectionDialog();
      if (typeof window.createTab === 'function') {
        window.createTab(data);
      } else {
        console.error('createTab() not found — is tabs.js loaded?');
      }

      await autoSaveProfile(payload, wantsRemember, credentials,
                            payload.credential_storage, sharedName);

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
  /**
   * Offer to make these credentials a named, shared one (#161).
   *
   * Remembering stores them against this connection alone. The same login
   * usually covers several devices, so without this somebody ends up with
   * the same password stored many times and no way to change it once.
   *
   * Optional on purpose: connecting to one device once should not make
   * anybody name anything, so an empty name simply keeps the old behaviour.
   */
  async function offerToNameCredential(payload) {
    if (!window.shellmateDialog || !payload.username || !payload.password) return '';

    const answer = await window.shellmateDialog.form({
      title: 'Remember these credentials',
      body:  `Saved for ${payload.hostname || 'this device'}. If other devices `
             + 'share this login, give it a name and they can all point at it.',
      note:  'Naming it means changing the password once updates every '
             + 'connection using it. Leave the name blank to keep it for this '
             + 'connection only.',
      confirmLabel: 'Save',
      fields: [
        { name: 'name', label: 'Name (optional)', placeholder: 'Lab admin',
          hint: 'What you will recognise when picking it later.' },
      ],
    });
    return answer ? (answer.name || '').trim() : '';
  }

  async function autoSaveProfile(payload, wantsRemember, credentials, storage,
                                 sharedName) {
    try {
      // No duplicate check here any more. save_profile() returns the existing
      // profile rather than appending a second one, so posting unconditionally
      // is safe — and there is now one rule rather than two, only one of which
      // used to be enforced.
      let profile = null;
      const created = await fetch('/api/profiles', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify(profileFrom(payload)),
      });
      if (created.ok) profile = await created.json();

      // A first-time connection has no profile id when it starts, so the
      // backend had nowhere to file the credentials. Now that the profile
      // exists, store them against it.
      // A named set, referenced by this connection — so the next device
      // sharing the login points at the same one rather than storing it
      // again (#161).
      if (sharedName && profile && profile.id) {
        try {
          const made = await fetch('/api/credential-sets', {
            method:  'POST',
            headers: { 'Content-Type': 'application/json' },
            body:    JSON.stringify({ name: sharedName,
                                      username: payload.username,
                                      password: payload.password,
                                      storage: storage || 'vault' }),
          });
          if (made.ok) {
            const set = await made.json();
            await fetch(`/api/profiles/${profile.id}/credential-set`, {
              method:  'PUT',
              headers: { 'Content-Type': 'application/json' },
              body:    JSON.stringify({ credential_ref: set.id }),
            });
            return;   // the set holds them; no per-profile copy needed
          }
        } catch (_) { /* fall through to the per-connection save */ }
      }

      if (wantsRemember && profile && profile.id && !payload.profile_id) {
        if (Object.values(credentials).some(Boolean)) {
          await fetch(`/api/profiles/${profile.id}/credentials`, {
            method:  'PUT',
            headers: { 'Content-Type': 'application/json' },
            body:    JSON.stringify({ ...credentials, storage: storage || 'vault' }),
          });
        }
      }

      renderWelcomeProfiles();
    } catch (_) { /* non-fatal */ }
  }

  // -------------------------------------------------------------------------
  // Helpers
  // -------------------------------------------------------------------------

  function showError(msg) {
    errorBox.classList.remove('form-note');
    errorBox.textContent = msg;
    errorBox.classList.remove('hidden');
  }

  function clearError() {
    errorBox.textContent = '';
    errorBox.classList.add('hidden');
    errorBox.classList.remove('form-note');
  }

  function setLoading(loading) {
    connectBtn.disabled = loading;
    connectLabel.textContent = loading ? 'Connecting…' : 'Connect';
    connectSpinner.classList.toggle('hidden', !loading);
  }

  // -------------------------------------------------------------------------
  // Quick connect — a typed target rather than a saved one (#533)
  // -------------------------------------------------------------------------

  /** A bare IPv4 address: unambiguous enough to offer connecting to. */
  const IPV4_RE = /^(?:\d{1,3}\.){3}\d{1,3}$/;

  /** A serial port, either spelling. */
  const SERIAL_RE = /^(?:COM\d+|\/dev\/tty\S+)$/i;

  /**
   * Read a typed target: `admin@10.1.20.5:2022`, `telnet 10.1.1.1 2003`,
   * `COM5 115200`, or a bare address.
   *
   * Returns null for anything that is not *unambiguously* a target, and that
   * restraint is the whole design. The palette is a tab finder first; if
   * every half-typed word offered "Connect to sw" the connect row would be
   * on screen permanently and mean nothing. So something has to say so — a
   * transport word, a username, a port, a COM port, or an IP address.
   *
   * IPv6 is not parsed. A raw `::1` is colons all the way down and cannot be
   * told from host:port without brackets nobody types into a search box; the
   * connection dialog takes one perfectly well.
   */
  function parseTarget(text) {
    const raw = String(text || '').trim();
    if (!raw || raw.length > 200) return null;

    let parts = raw.split(/\s+/);
    let type = '';
    const lead = parts[0].toLowerCase();
    if (lead === 'ssh' || lead === 'telnet' || lead === 'serial') {
      type = lead;
      parts = parts.slice(1);
    }
    if (!parts.length || !parts[0]) return null;

    // `-p 2022`, the way ssh spells it.
    let port = 0;
    const flag = parts.indexOf('-p');
    if (flag >= 0 && parts[flag + 1]) {
      port = parseInt(parts[flag + 1], 10) || 0;
      parts.splice(flag, 2);
    }
    if (!parts.length) return null;

    let host = parts[0];

    // Serial, by explicit word or by the shape of the port name. The second
    // word is the baud rate — `COM5 115200` is how a console is described
    // out loud, so it is what people type.
    if (type === 'serial' || SERIAL_RE.test(host)) {
      const baud = parseInt(parts[1], 10);
      return {
        connection_type: 'serial',
        serial_port: host,
        baud_rate: (Number.isFinite(baud) && baud > 0) ? baud : 9600,
        label: host,
        explicit: true,
      };
    }

    // `telnet 10.1.1.1 2003` — a terminal server port, and the commonest
    // reason anybody types a port at all.
    if (!port && parts[1] && /^\d{1,5}$/.test(parts[1])) {
      port = parseInt(parts[1], 10);
    }

    let username = '';
    const at = host.lastIndexOf('@');
    if (at > 0) {
      username = host.slice(0, at);
      host = host.slice(at + 1);
    }

    // Exactly one colon, or it is an IPv6 address rather than host:port.
    if (!port && host.split(':').length === 2) {
      const [name, tail] = host.split(':');
      if (/^\d{1,5}$/.test(tail)) {
        host = name;
        port = parseInt(tail, 10);
      }
    }

    if (!host || /[\s\/\\]/.test(host)) return null;
    if (port < 0 || port > 65535) return null;

    // The gate. Without one of these, this is a search term.
    const named = Boolean(type) || Boolean(username) || Boolean(port)
                  || IPV4_RE.test(host);
    if (!named) return null;

    const kind = type === 'telnet' ? 'telnet' : 'ssh';
    return {
      connection_type: kind,
      hostname: host,
      port: port || DEFAULT_PORTS[kind],
      username,
      label: host,
      explicit: Boolean(type),
    };
  }

  /** How the target will be dialled, in one line, for the palette row. */
  function describeTarget(target) {
    if (!target) return '';
    if (target.connection_type === 'serial') {
      return `Serial · ${target.serial_port} · ${target.baud_rate} baud`;
    }
    const who = target.username ? `${target.username}@` : '';
    return `${target.connection_type.toUpperCase()} · ${who}${target.hostname}:${target.port}`;
  }

  /**
   * A saved connection for this target, if there is one.
   *
   * A saved profile wins, always. It carries the credentials, the group, the
   * key and the jump host; connecting ad hoc to an address somebody has
   * already set up properly would ask for a password they saved months ago.
   */
  function _savedForTarget(target) {
    if (!target) return null;
    const wanted = String(target.hostname || target.serial_port || '').toLowerCase();
    return profileCache.find(p => {
      if ((p.connection_type || 'ssh') !== target.connection_type) return false;
      if (target.connection_type === 'serial') {
        return String(p.serial_port || '').toLowerCase() === wanted;
      }
      if (String(p.hostname || '').toLowerCase() !== wanted) return false;
      const savedPort = Number(p.port || DEFAULT_PORTS[p.connection_type] || 22);
      if (savedPort !== Number(target.port)) return false;
      // A username typed at the palette has to match, or two accounts on one
      // device resolve to whichever profile happens to be first.
      return !target.username
        || String(p.username || '').toLowerCase() === target.username.toLowerCase();
    }) || null;
  }

  /**
   * Open a typed target (#533).
   *
   * The dialog appears only when there is something left to ask. A saved
   * profile goes through openProfile(), which switches to an existing tab,
   * connects when everything is saved, and falls back to the dialog when it
   * is not. Telnet and serial need nothing beyond the address, so they
   * connect. SSH needs a password, so it lands on the dialog with the
   * password field focused and everything else filled in.
   */
  async function quickConnect(target) {
    if (!target) return false;

    const saved = _savedForTarget(target);
    if (saved) {
      await openProfile(saved, null);
      return true;
    }

    const prefill = {
      id: '',
      name: '',
      tags: [],
      connection_type: target.connection_type,
      hostname: target.hostname || '',
      port: target.port || 0,
      username: target.username || '',
      serial_port: target.serial_port || '',
      baud_rate: target.baud_rate || 9600,
    };

    if (target.connection_type !== 'ssh') {
      try {
        const { response, data } = await postSession({
          connection_type: target.connection_type,
          hostname:        target.hostname || '',
          port:            target.port || 0,
          username:        '',
          display_label:   '',
          serial_port:     target.serial_port || '',
          baud_rate:       target.baud_rate || 9600,
        });
        if (response.ok) {
          if (typeof window.createTab === 'function') window.createTab(data);
          return true;
        }
        // Failed with the details already known: the dialog is where the
        // message belongs, next to the fields that would fix it.
        showConnectionDialog(prefill);
        showError(data.detail || `Server error ${response.status}`);
        return true;
      } catch (err) {
        showConnectionDialog(prefill);
        showError(err.message || 'Could not connect.');
        return true;
      }
    }

    showConnectionDialog(prefill);
    return true;
  }

  window.savedProfileForTarget = _savedForTarget;
  window.parseConnectTarget = parseTarget;
  window.describeConnectTarget = describeTarget;
  window.quickConnect = quickConnect;

  // The estate as a spreadsheet (#535)
  //
  // A 200-site team already holds its estate somewhere — a spreadsheet, or an
  // export from whatever monitors it. Typing that into the dialog, or
  // sweeping every subnet to rediscover it, is the reason it stays there.
  //
  // The preview before the write is the whole point: the same parser that
  // will do the work counts the rows first, so "142 new, 17 already saved,
  // 3 unreadable rows" is a promise rather than an estimate.
  // -------------------------------------------------------------------------

  /** The columns, shown in the dialog and used as the placeholder. */
  const CSV_HEADER = 'name,hostname,port,type,username,groups,platform,credential';

  /** A file chosen from disk, read here rather than uploaded. */
  function _pickCsvFile() {
    return new Promise((resolve) => {
      const input = document.createElement('input');
      input.type = 'file';
      input.accept = '.csv,.txt,text/csv,text/plain';
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

  /** The "or choose a file" row that sits above the textarea. */
  function _importContent() {
    const row = document.createElement('div');
    row.className = 'csv-import-row';

    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'btn-secondary';
    button.innerHTML = '<span class="material-symbols-outlined">upload</span>'
      + 'Choose a CSV file…';
    button.addEventListener('click', async (e) => {
      e.preventDefault();
      const text = await _pickCsvFile();
      if (text === null) return;
      // The dialog builds its own controls, so the textarea is found rather
      // than held: there is exactly one in a ShellMate dialog at a time.
      const box = document.querySelector('.sm-dialog textarea');
      if (box) { box.value = text; box.focus(); }
    });

    const hint = document.createElement('p');
    hint.className = 'csv-import-hint';
    hint.textContent = 'Columns: ' + CSV_HEADER
      + '. A header row is detected; without one the columns are read in that '
      + 'order. Groups are comma-separated and may be nested (site-004/access). '
      + 'Credential is the name of a shared credential, and ShellMate will not '
      + 'import passwords.';

    row.append(button, hint);
    return row;
  }

  async function _postImport(text, apply) {
    const res = await fetch('/api/profiles/import', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ text, apply }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `Server error ${res.status}`);
    return data;
  }

  /** Paste or open a CSV, see what it comes to, then save it. */
  async function importEstate() {
    const answer = await window.shellmateDialog.form({
      title: 'Import connections',
      body:  'Paste rows from a spreadsheet, or choose a CSV file. Nothing is '
             + 'saved until you have seen what it comes to.',
      content: _importContent(),
      confirmLabel: 'Read it',
      fields: [{ name: 'text', label: 'Rows', type: 'textarea',
                 placeholder: CSV_HEADER }],
    });
    if (!answer || !(answer.text || '').trim()) return;

    let preview;
    try {
      preview = await _postImport(answer.text, false);
    } catch (err) {
      await window.shellmateDialog.alert({
        title: 'That file could not be read', body: String(err.message || err),
      });
      return;
    }

    const bad = preview.rejected || [];
    const ok = await window.shellmateDialog.confirm({
      title: `Import ${preview.new} connection${preview.new === 1 ? '' : 's'}?`,
      body:  `${preview.rows} row${preview.rows === 1 ? '' : 's'} read: `
             + `${preview.new} new, ${preview.already_saved} already saved, `
             + `${bad.length} unreadable. Rows already saved keep the name and `
             + `groups they have and gain any this file adds.`,
      // Named, not counted. "3 unreadable rows" tells nobody which three.
      list:  bad.slice(0, 12).map(row => ({ text: `Line ${row.line}: ${row.why}` })),
      confirmLabel: preview.new ? 'Import them' : 'Import anyway',
    });
    if (!ok) return;

    try {
      const result = await _postImport(answer.text, true);
      if (window.shellmateAlerts) {
        window.shellmateAlerts.notify({
          icon: 'upload', title: 'Connections imported',
          body: `${result.new} added, ${result.already_saved} already saved`
                + (result.rejected.length ? `, ${result.rejected.length} skipped.` : '.'),
        });
      }
      if (typeof window.renderWelcomeProfiles === 'function') {
        await window.renderWelcomeProfiles();
      }
    } catch (err) {
      await window.shellmateDialog.alert({
        title: 'Nothing was imported', body: String(err.message || err),
      });
    }
  }

  /**
   * Write the estate, or one group, out as CSV.
   *
   * Saved through the platform's folder dialog where there is a native
   * window, because a browser download there does nothing visible — the same
   * lesson logs.js records (#578). The blob is the fallback, not the plan.
   */
  async function exportEstate(group) {
    const key = group || '';
    try {
      const saved = await fetch('/api/profiles/export', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ group: key }),
      });
      if (saved.ok) {
        const data = await saved.json();
        if (data.cancelled) return;
        if (window.shellmateAlerts) {
          window.shellmateAlerts.notify({
            global: true, icon: 'download', title: 'Connections exported',
            body: `Saved to ${data.path}. The folder has been opened.`,
          });
        }
        return;
      }
      if (saved.status !== 409) {
        const err = await saved.json().catch(() => ({}));
        throw new Error(err.detail || `server returned ${saved.status}`);
      }
    } catch (err) {
      if (window.shellmateAlerts) {
        window.shellmateAlerts.notify({
          global: true, severity: 'warning', icon: 'error',
          title: 'Could not export the connections', body: String(err.message || err),
        });
      }
      return;
    }

    try {
      const res = await fetch('/api/profiles/export.csv?group=' + encodeURIComponent(key));
      if (!res.ok) throw new Error(`server returned ${res.status}`);
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = 'shellmate-' + (key.replace(/[^a-z0-9._-]+/gi, '-') || 'connections') + '.csv';
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 4000);
      if (window.shellmateAlerts) {
        window.shellmateAlerts.notify({
          global: true, icon: 'download', title: 'Download started',
          body: "Check your browser's downloads folder.",
        });
      }
    } catch (err) {
      if (window.shellmateAlerts) {
        window.shellmateAlerts.notify({
          global: true, severity: 'warning', icon: 'error',
          title: 'Could not export the connections', body: String(err.message || err),
        });
      }
    }
  }

  window.shellmateEstateCsv = { importEstate, exportEstate };

  window.showConnectionDialog  = showConnectionDialog;
  window.hideConnectionDialog  = hideConnectionDialog;
  window.renderWelcomeProfiles = renderWelcomeProfiles;
  // Redraw without reloading — what a selection change needs (#188).
  window.renderWelcomeTiles = renderWelcomeTiles;

})();
