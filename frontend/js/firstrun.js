/**
 * firstrun.js — The four questions ShellMate already knows to ask (#564).
 *
 * `update.js announceIfNew` has had a branch that is precisely "this is a
 * fresh install" since the What's New modal was written. It records the
 * version and does nothing else. Meanwhile two decisions get taken silently
 * on that same first run, and both bite weeks later:
 *
 * - **where saved passwords live.** DPAPI or a master password, chosen
 *   implicitly by whichever happens on the first write. A DPAPI vault does
 *   not travel, so the person who copies their ShellMate folder to a laptop
 *   finds their credentials gone and no explanation of why.
 * - **where the data folder is.** Usually beside the executable, which is
 *   the whole portable story — except when that location is read-only, when
 *   it silently falls back to per-user storage and the folder somebody
 *   carries on their stick has nothing in it.
 *
 * A card, not a tour. Four things on one screen, each of which can be
 * skipped by closing it, and none of which is asked twice. A wizard with
 * four steps for four questions is three more screens than the questions
 * need, and the first thing anybody does with a wizard is click through it.
 *
 * Nothing here blocks reaching a device. The card is dismissible, every
 * choice has a working default already applied, and closing it without
 * answering leaves the application exactly as it was.
 *
 * It lives *in* the home screen rather than over the application. The
 * first version was a full-screen overlay, and the UI tests found what a
 * user would have: a scrim intercepts every click until it is dismissed,
 * which is the opposite of the sentence above. On the home screen it is
 * there when you come back and out of the way when you open a tab.
 */
(function () {
  'use strict';

  let card = null;
  let info = null;
  let vault = null;

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  document.addEventListener('DOMContentLoaded', () => {
    // The chip is not first-run-only: it is the permanent answer to "is
    // this the portable copy or the installed one", which people ask of a
    // machine they did not set up.
    setTimeout(paintChip, 1200);
  });

  // -------------------------------------------------------------------------
  // The status-bar chip
  // -------------------------------------------------------------------------

  /**
   * Portable, or a local profile, or the warning case.
   *
   * Three states, not two. "Portable" and "Local profile" are both
   * intentional and read as information; the third is the one that is
   * neither — the executable sits in a folder it cannot write to, so the
   * data went somewhere else. That one is a warning, because somebody is
   * carrying a stick that will turn out to be empty.
   */
  async function paintChip() {
    const host = document.getElementById('status-portable-wrap');
    const chip = document.getElementById('status-portable');
    if (!host || !chip) return;

    try {
      info = await (await fetch('/api/system/info')).json();
    } catch (_) {
      return;
    }

    let label, tip, warn = false;
    if (info.using_fallback) {
      label = 'Not portable';
      warn = true;
      tip = `ShellMate could not write beside the executable, so your data is `
          + `in ${info.data_dir}. It will not travel with the exe. Click for `
          + `Diagnostics.`;
    } else if (info.portable) {
      label = 'Portable';
      tip = `Your data is in ${info.data_dir}, beside the executable, and `
          + `travels with it. Click for Diagnostics.`;
    } else {
      label = 'From source';
      tip = `Running from source. Data is in ${info.data_dir}. Click for `
          + `Diagnostics.`;
    }

    chip.textContent = label;
    chip.title = tip;
    chip.classList.toggle('status-portable-warn', warn);
    host.classList.remove('hidden');

    chip.addEventListener('click', () => {
      if (typeof window.openSettingsSection === 'function') {
        window.openSettingsSection('Diagnostics');
      } else if (typeof window.openSettings === 'function') {
        window.openSettings();
      }
    });
  }

  // -------------------------------------------------------------------------
  // The card
  // -------------------------------------------------------------------------

  /** Called by update.js on the branch that means "never seen before". */
  async function offer() {
    if (card) return;
    // Mounted in the home screen, after its heading. No home screen means
    // no card — the rest of the interface does not stop for it.
    const home = document.getElementById('welcome-content');
    if (!home) return;

    try {
      info = info || await (await fetch('/api/system/info')).json();
      vault = await (await fetch('/api/vault/status')).json();
    } catch (_) {
      // Not worth blocking a first run over. The defaults are already in
      // force; this card only offers to change them.
      return;
    }

    card = build();
    const heading = home.querySelector('h1');
    if (heading && heading.nextSibling) {
      home.insertBefore(card, heading.nextSibling);
    } else {
      home.prepend(card);
    }
  }

  function build() {
    const card = el('div', 'firstrun-card');

    const head = el('div', 'firstrun-head');
    head.append(el('h2', 'firstrun-title', 'Welcome to ShellMate'),
                el('p', 'firstrun-sub',
                   'Four things worth deciding now. Everything here can be '
                   + 'changed later in Settings, and closing this card leaves '
                   + 'the defaults in place.'));
    card.appendChild(head);

    card.appendChild(themeRow());
    card.appendChild(vaultRow());
    card.appendChild(assistantRow());
    card.appendChild(dataRow());

    const actions = el('div', 'firstrun-actions');
    const done = el('button', 'btn-primary', 'Start using ShellMate');
    done.type = 'button';
    done.addEventListener('click', close);
    actions.appendChild(done);
    card.appendChild(actions);

    card.tabIndex = -1;
    card.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') close();
    });
    return card;
  }

  function section(title, hint) {
    const box = el('div', 'firstrun-section');
    box.append(el('h3', 'firstrun-section-title', title),
               el('p', 'firstrun-hint', hint));
    return box;
  }

  function choice(label, onPick, selected) {
    const b = el('button', 'firstrun-choice' + (selected ? ' on' : ''), label);
    b.type = 'button';
    b.addEventListener('click', () => {
      [...b.parentElement.children].forEach(other =>
        other.classList.toggle('on', other === b));
      onPick();
    });
    return b;
  }

  // --- theme ---------------------------------------------------------------

  function themeRow() {
    const box = section('Theme',
      'Dark is the default because it is what a terminal looks like. '
      + 'High contrast is a genuine third option, not dark with the '
      + 'brightness turned up.');
    const row = el('div', 'firstrun-choices');
    const current = (window.shellmateSettings
                     && window.shellmateSettings.interface
                     && window.shellmateSettings.interface.theme) || 'dark';

    [['dark', 'Dark'], ['light', 'Light'],
     ['high-contrast', 'High contrast'], ['system', 'Match my system']]
      .forEach(([value, label]) => {
        row.appendChild(choice(label, () => setTheme(value), value === current));
      });
    box.appendChild(row);
    return box;
  }

  function setTheme(value) {
    saveInterface({ theme: value });
    // Applied immediately rather than on the next load: a theme picker that
    // does nothing until you restart is a theme picker nobody believes.
    if (window.shellmateTheme) window.shellmateTheme.apply(value);
  }

  // --- the vault -----------------------------------------------------------

  /**
   * The one that actually matters.
   *
   * It is asked here because it is otherwise decided by whichever write
   * happens first, and the consequence — a vault that will not open on
   * another machine — appears weeks later with nothing to connect it back
   * to a decision nobody made.
   */
  function vaultRow() {
    const box = section('Where should saved passwords live?',
      'ShellMate never stores a device password unless you ask it to. When '
      + 'you do, this decides how it is encrypted.');

    if (vault && vault.exists) {
      // Somebody has already saved something. Changing the mode is a
      // re-encryption, which belongs in Settings with its own confirmation,
      // not on a welcome card.
      box.appendChild(el('p', 'firstrun-hint',
        'A vault already exists on this machine, so this is set. You can '
        + 'change it under Settings → Credentials vault.'));
      return box;
    }

    const row = el('div', 'firstrun-choices');
    const canDpapi = !vault || vault.dpapi_available;

    row.appendChild(choice(
      canDpapi ? 'This Windows account' : 'This Windows account (unavailable)',
      () => { if (canDpapi) chooseDpapi(); },
      canDpapi));
    row.appendChild(choice('A master password', chooseMasterPassword, !canDpapi));
    box.appendChild(row);

    // The trade-off, stated. This is the sentence whose absence costs
    // people their credentials.
    box.appendChild(el('p', 'firstrun-hint',
      'Windows account: nothing to remember, and nothing to type. But it is '
      + 'sealed to this account on this machine — copy ShellMate to another '
      + 'computer and the saved passwords will not come with it. A master '
      + 'password travels, and you type it once per session.'));
    return box;
  }

  function chooseDpapi() {
    fetch('/api/vault/mode', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode: 'dpapi' }),
    }).catch(() => {});
  }

  async function chooseMasterPassword() {
    if (!window.shellmateDialog) return;
    const answer = await window.shellmateDialog.form({
      title: 'Choose a master password',
      body: 'It encrypts the saved passwords, and ShellMate asks for it once '
          + 'per session. There is no way to recover it — if it is lost the '
          + 'saved passwords are lost with it, and nothing else is.',
      fields: [{ name: 'password', label: 'Master password', type: 'password' }],
      confirmLabel: 'Use this',
    });
    if (!answer || !(answer.password || '').trim()) return;

    try {
      const res = await fetch('/api/vault/mode', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mode: 'password', password: answer.password }),
      });
      if (!res.ok) {
        const data = await res.json();
        throw new Error(data.detail || `HTTP ${res.status}`);
      }
    } catch (e) {
      if (window.shellmateAlerts) window.shellmateAlerts.notify({
        severity: 'warning', icon: 'error',
        title: 'The master password was not set',
        body: String(e.message || e),
      });
    }
  }

  // --- the assistant -------------------------------------------------------

  function assistantRow() {
    const box = section('Turn the AI assistant on?',
      'Off by default, because it sends what is on your terminal to a model '
      + 'provider and that should never be a surprise. Ollama runs on this '
      + 'machine and sends nothing anywhere.');
    const row = el('div', 'firstrun-choices');
    const on = !!(window.shellmateSettings && window.shellmateSettings.ai
                  && window.shellmateSettings.ai.panel_enabled);
    row.appendChild(choice('Leave it off', () => setAssistant(false), !on));
    row.appendChild(choice('Turn it on', () => setAssistant(true), on));
    box.appendChild(row);
    return box;
  }

  /**
   * Through `toggleAiPanel`, not by writing the setting here.
   *
   * That function moves the pane, saves, and re-reads settings afterwards
   * — settings.js keeps its own copy for the form, and a stale one there
   * quietly undoes the change the next time somebody presses Save. Writing
   * the setting directly from this card would reintroduce exactly that.
   */
  function setAssistant(enabled) {
    const on = !!(window.shellmateSettings && window.shellmateSettings.ai
                  && window.shellmateSettings.ai.panel_enabled);
    if (on === enabled) return;
    if (typeof window.toggleAiPanel === 'function') window.toggleAiPanel();
  }

  // --- where the data is ---------------------------------------------------

  function dataRow() {
    const box = section('Where your data lives', '');
    const line = el('p', 'firstrun-hint');

    if (info && info.using_fallback) {
      // The warning case, said plainly. Somebody who reads "portable" on
      // the box and finds an empty stick has been failed by this sentence
      // not existing.
      line.className = 'firstrun-hint firstrun-warn';
      line.textContent = `ShellMate could not write beside the executable, so `
        + `your settings, profiles and history are in ${info.data_dir}. They `
        + `will not travel with the exe. Moving ShellMate to a folder you can `
        + `write to — not Program Files, and not a read-only share — fixes it.`;
    } else if (info && info.portable) {
      line.textContent = `${info.data_dir} — beside the executable, so it `
        + `travels with it. Copy the folder and ShellMate goes with it, `
        + `except the saved passwords if you chose the Windows account above.`;
    } else {
      line.textContent = `${info.data_dir}. You are running from source, so `
        + `this is a folder in the checkout rather than beside an executable.`;
    }
    box.appendChild(line);
    return box;
  }

  // -------------------------------------------------------------------------

  function saveInterface(changes) {
    window.shellmateSettings = window.shellmateSettings || {};
    window.shellmateSettings.interface =
      Object.assign({}, window.shellmateSettings.interface, changes);
    fetch('/api/settings', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ settings: { interface: changes } }),
    }).catch(() => {});
  }

  function close() {
    if (card) card.remove();
    card = null;
  }

  window.shellmateFirstRun = { offer, paintChip };
})();
