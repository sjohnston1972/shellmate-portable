/**
 * device.js — Report what ShellMate has worked out about each device.
 *
 * Two things are surfaced, both for the same reason: this code sends commands
 * into a live session and rewrites what the user typed, and doing either
 * silently would be unacceptable in a tool whose output people have to account
 * for afterwards.
 *
 *   - The identified platform and version, in the status bar.
 *   - A one-line note in the terminal when a command was sent on the user's
 *     behalf or an alias was expanded.
 */
(function () {
  'use strict';

  /** sessionId -> fingerprint, so switching tabs shows the right device. */
  const identified = new Map();

  document.addEventListener('DOMContentLoaded', () => {
    window.addEventListener('shellmate:device-identified', (e) => {
      const info = e.detail;
      identified.set(info.sessionId, info);
      updateStatus(info.sessionId);
      announce(info);
    });

    window.addEventListener('shellmate:alias-expanded', (e) => {
      note(`${e.detail.typed}  →  ${e.detail.sent}`, 'alias');
    });

    // Tab switches are announced by tabs.js (as "mate:tab-switched"); follow
    // them so the status bar always describes the tab actually on screen.
    window.addEventListener('mate:tab-switched', (e) => {
      const tab = e.detail || (typeof window.getActiveTab === 'function' ? window.getActiveTab() : null);
      updateStatus(tab ? tab.sessionId : null);
    });
  });

  function updateStatus(sessionId) {
    const el = document.getElementById('status-device');
    if (!el) return;

    const info = sessionId ? identified.get(sessionId) : null;
    if (!info || info.platform === 'generic') {
      el.textContent = 'Device: unidentified';
      el.classList.remove('status-device-known');
      el.title = info
        ? 'ShellMate could not identify this device, so no platform commands are sent to it.'
        : 'Detected device platform';
      return;
    }

    const version = info.version ? ` ${info.version}` : '';
    el.textContent = `Device: ${info.profile_name}${version}`;
    el.classList.add('status-device-known');
    el.title =
      `Identified from the ${info.source} (confidence ${info.confidence}).` +
      (info.model ? ` Model ${info.model}.` : '');
  }

  /** Say what was sent on the user's behalf, if anything was. */
  function announce(info) {
    if (info.paging_command) {
      note(`identified ${info.profile_name} — sent "${info.paging_command}"`, 'device');
    } else if (info.platform === 'generic') {
      note('device not recognised — no commands sent, aliases off', 'device');
    } else {
      note(`identified ${info.profile_name}`, 'device');
    }
  }

  /**
   * Show a transient note above the status bar.
   *
   * Deliberately not written into the terminal itself: that buffer is the
   * record of what the device said, and injecting our own commentary into it
   * would corrupt the very transcript the evidence pack depends on.
   */
  function note(text, kind) {
    const host = document.getElementById('terminals-container');
    if (!host) return;

    const el = document.createElement('div');
    el.className = `device-note device-note-${kind}`;

    const icon = document.createElement('span');
    icon.className = 'material-symbols-outlined';
    icon.textContent = kind === 'alias' ? 'edit' : 'smart_toy';

    const label = document.createElement('span');
    label.textContent = text;

    el.appendChild(icon);
    el.appendChild(label);
    host.appendChild(el);

    setTimeout(() => el.classList.add('device-note-fade'), 3500);
    setTimeout(() => el.remove(), 4200);
  }

  window.getDeviceInfo = (sessionId) => identified.get(sessionId) || null;
})();
