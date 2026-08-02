/**
 * device.js — Report what ShellMate has worked out about each device.
 *
 * Two things are surfaced, both for the same reason: this code sends commands
 * into a live session and rewrites what the user typed, and doing either
 * silently would be unacceptable in a tool whose output people have to account
 * for afterwards.
 *
 *   - The identified platform, its confidence, and whether that was enough to
 *     act on — in the status bar.
 *   - A one-line note in the terminal when a command was sent on the user's
 *     behalf, an alias was expanded, or — the case this used to get wrong — a
 *     command was deliberately *not* sent.
 *
 * That last case is the common one and it used to read as success. Paging-off
 * only fires when the device was identified confidently, and a device whose
 * banner is a legal warning rather than a version string is identified from
 * its prompt alone and scores below the bar. The note said "identified Cisco
 * IOS" and stopped: the status bar claimed the device was known, the setting
 * was ticked, and paging was still on. Now it says what it did not do and why,
 * and offers the way out.
 */
(function () {
  'use strict';

  /** Shorthand for a Stockton value. */
  const A = (key, fallback) =>
    (window.shellmateAdvanced ? window.shellmateAdvanced(key, fallback) : fallback);

  /** sessionId -> fingerprint, so switching tabs shows the right device. */
  const identified = new Map();

  /** Platform list for the override menu, fetched once and reused. */
  let platformsPromise = null;

  /** How each identification source reads in prose. */
  const SOURCE = {
    'banner':          'its login banner',
    'prompt':          'its prompt alone',
    'version-command': 'a version command',
    'you':             'you',
    'ssh-version':     'its SSH version string',
    'none':            'nothing it could match',
  };

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

    const status = document.getElementById('status-device');
    if (status) status.addEventListener('click', () => openChooser());
  });

  // ---------------------------------------------------------------------------
  // Status bar
  // ---------------------------------------------------------------------------

  function updateStatus(sessionId) {
    const el = document.getElementById('status-device');
    if (!el) return;

    const info = sessionId ? identified.get(sessionId) : null;
    el.classList.remove('status-device-known', 'status-device-unsure');

    if (!info || info.platform === 'generic') {
      el.textContent = 'Device: unidentified';
      el.title = info
        ? 'ShellMate could not identify this device, so no platform commands ' +
          'are sent to it. Click to say what it is.'
        : 'Detected device platform';
      return;
    }

    const version = info.version ? ` ${info.version}` : '';

    // The consequence, not just the identity. "Identified but not acted on" is
    // the state people were being left in without being told, so it gets its
    // own wording in the bar itself rather than only in the tooltip.
    if (info.confident) {
      el.textContent = `Device: ${info.profile_name}${version}`;
      el.classList.add('status-device-known');
    } else {
      el.textContent = `Device: ${info.profile_name}${version} (unconfirmed)`;
      el.classList.add('status-device-unsure');
    }

    el.title =
      `Identified from ${SOURCE[info.source] || info.source} ` +
      `(confidence ${info.confidence} of 1.0).` +
      (info.model ? ` Model ${info.model}.` : '') +
      (info.confident
        ? ''
        // "and aliases are inactive" was wrong. The confidence gate covers
        // exactly one thing — the paging command — because that is what gets
        // *sent to the device* unasked. pipeline.platform is set regardless,
        // so alias expansion is live here, and telling somebody it is not is
        // how they end up surprised by a rewrite they were told could not
        // happen. Expansion is always announced ("typed → sent"), which is
        // what makes it acceptable on a device we are unsure of.
        : ` Below the ${info.act_threshold ?? 0.6} needed before ShellMate ` +
          `sends anything to a device, so paging-off was withheld. Aliases ` +
          `still expand — every expansion is shown as it happens.`) +
      (info.alias_count
        ? ` ${info.alias_count} alias${info.alias_count === 1 ? '' : 'es'} ` +
          `for this platform.`
        : '') +
      ' Click to set it yourself.';
  }

  // ---------------------------------------------------------------------------
  // Saying what was done — and what was not
  // ---------------------------------------------------------------------------

  /**
   * Say what was sent on the user's behalf, or why nothing was.
   *
   * Every branch names the command at stake. "No commands sent" leaves the
   * reader wondering which commands; "not confident enough to send
   * `terminal length 0`" tells them what they are missing and lets them
   * decide whether they care.
   */
  /**
   * ", 14 aliases active" — or nothing at all.
   *
   * Identifying the platform is what switches alias expansion on, so it is
   * part of what just happened and belongs in the same sentence. It was only
   * ever mentioned in the branch where nothing was sent, which is the rarest
   * one.
   *
   * Silent in three cases, each for its own reason:
   *
   * - The platform has no aliases. Nothing happened, so nothing is claimed.
   * - `terminal.expand_aliases` is off. The profile still carries them and
   *   the backend still reports them, but none will expand — and announcing
   *   a feature the user has switched off is how a setting comes to look
   *   broken.
   * - The device was not recognised. That branch says "aliases off" already,
   *   which is the accurate thing to say when the generic profile is in use.
   */
  function aliasSuffix(info) {
    const count = Number(info.alias_count) || 0;
    if (!count) return '';

    const terminal = (window.shellmateSettings || {}).terminal || {};
    if (terminal.expand_aliases === false) return '';

    return `, ${count} alias${count === 1 ? '' : 'es'} active`;
  }

  function announce(info) {
    // A remembered answer is not "you" in the present tense — it is a
    // decision made on some earlier connection, and somebody has to be able
    // to tell that is why a command went out today.
    const from = info.remembered
      ? 'what you told it last time'
      : (SOURCE[info.source] || info.source);

    // A remembered value that a banner contradicted. The device has probably
    // been replaced, and saying nothing would leave the old answer looking
    // like it is still in force.
    if (info.remembered_overridden) {
      note(`identified ${info.profile_name} from its login banner — this was `
           + `remembered as ${info.remembered_overridden}, so the device has `
           + `likely changed. Re-identify it if the new answer is wrong.`,
           'device', { offerOverride: true });
      return;
    }

    if (info.paging_command) {
      note(`identified ${info.profile_name} from ${from} — sent `
           + `"${info.paging_command}"${aliasSuffix(info)}`, 'device');
      return;
    }

    switch (info.paging_skipped) {
      case 'unconfident':
        // Aliases are not gated on confidence — only the paging command is,
        // because that is the one thing sent to the device. So this branch
        // has aliases even though it sent nothing, and saying so is the
        // difference between "identification half worked" and "it did
        // nothing".
        note(`identified ${info.profile_name} from ${from} — not confident enough ` +
             `to send "${info.paging_available}", so paging is still on` +
             `${aliasSuffix(info)}`,
             'device', { offerOverride: true });
        break;
      case 'unidentified':
        note('device not recognised — no commands sent, aliases off',
             'device', { offerOverride: true });
        break;
      case 'off':
        note(`identified ${info.profile_name} from ${from} — paging-off is ` +
             `switched off in Settings, so nothing was sent` +
             `${aliasSuffix(info)}`, 'device');
        break;
      case 'no-command':
        note(`identified ${info.profile_name} from ${from} — nothing to send` +
             `${aliasSuffix(info) || ', and no aliases for it'}`, 'device');
        break;
      default:
        note(`identified ${info.profile_name} from ${from}${aliasSuffix(info)}`,
             'device');
    }
  }

  /**
   * Show a transient note above the status bar.
   *
   * Deliberately not written into the terminal itself: that buffer is the
   * record of what the device said, and injecting our own commentary into it
   * would corrupt the very transcript the evidence pack depends on.
   */
  /** How long an actionable device notice stays: a multiple of the ordinary. */
  function _noticeSeconds() {
    const base = window.shellmateAdvanced
      ? Number(window.shellmateAdvanced('alerts.toast_seconds', 12)) : 12;
    return Math.round((base || 12) * 2.5);
  }

  function note(text, kind, opts) {
    if (!window.shellmateAlerts || !window.shellmateAlerts.notify) return;
    opts = opts || {};

    // This was a floating element of its own at bottom-right, z-index 45,
    // while the drift prompt sat at bottom:16px too — so the note announcing
    // what ShellMate sent to the device landed on top of the question asking
    // whether you wanted to see what had changed, covering its button.
    //
    // Same stack as everything else now. A note that explains a feature did
    // nothing still carries the fix rather than leaving the reader to go
    // looking for it, but as the toast's own action.
    window.shellmateAlerts.notify({
      icon:  kind === 'alias' ? 'edit' : 'smart_toy',
      title: text,
      // It times out either way (#148).
      //
      // This used to be sticky whenever it carried the override button, on
      // the reasoning that an offer which withdraws itself is worse than no
      // offer. That holds for the drift prompt — a question with a
      // consequence, which somebody must answer — and not here.
      // Identification is best-effort, most people will never set a platform,
      // and the notice became a permanent fixture in the corner.
      //
      // Nothing is lost by it going: the device chip in the status bar is
      // permanent, says the platform is unconfirmed, and opens the same
      // chooser on click. The offer has a home; this was only its
      // announcement.
      sticky: false,
      // Longer than an ordinary toast, because it does carry an action —
      // long enough to notice and act on, not long enough to live there.
      seconds: opts.offerOverride ? _noticeSeconds() : undefined,
      action: opts.offerOverride
        ? { label: 'Set platform', onClick: () => openChooser() }
        : null,
    });
  }

  // ---------------------------------------------------------------------------
  // "This is a…" — the manual override
  // ---------------------------------------------------------------------------

  function loadPlatforms() {
    if (!platformsPromise) {
      platformsPromise = fetch('/api/platforms')
        .then(r => r.ok ? r.json() : [])
        .catch(() => []);
    }
    return platformsPromise;
  }

  async function openChooser() {
    const tab = typeof window.getActiveTab === 'function' ? window.getActiveTab() : null;
    if (!tab || !tab.sessionId) return;

    document.querySelectorAll('.device-chooser').forEach(m => m.remove());

    // /api/platforms returns them keyed by id, including "generic" — which is
    // the one thing there is no point choosing deliberately.
    const platforms = await loadPlatforms();
    const list = Object.values(platforms.platforms || {})
      .filter(p => p && p.id && p.id !== 'generic')
      .sort((a, b) => (a.name || a.id).localeCompare(b.name || b.id));
    if (!list.length) return;

    const menu = document.createElement('div');
    menu.className = 'device-chooser';

    const heading = document.createElement('div');
    heading.className = 'device-chooser-title';
    heading.textContent = 'This device is a…';
    menu.appendChild(heading);

    const hint = document.createElement('p');
    hint.className = 'device-chooser-hint';
    hint.textContent =
      'Applies the platform’s aliases straight away, and sends its paging ' +
      'command if that setting is on. Use it when a legal banner or a ' +
      'terminal server hides what the device is.';
    menu.appendChild(hint);

    list.forEach(profile => {
      const row = document.createElement('button');
      row.type = 'button';
      row.className = 'device-chooser-row';
      row.textContent = profile.name || profile.id;
      if (profile.paging_off) {
        const cmd = document.createElement('span');
        cmd.className = 'device-chooser-cmd';
        cmd.textContent = profile.paging_off;
        row.appendChild(cmd);
      }
      row.addEventListener('click', () => {
        menu.remove();
        choose(tab.sessionId, profile.id);
      });
      menu.appendChild(row);
    });

    document.body.appendChild(menu);

    // Anchored to the status-bar entry it belongs to, opening upwards since
    // the status bar is the last row on screen.
    const anchor = document.getElementById('status-device');
    if (anchor) {
      const box = anchor.getBoundingClientRect();
      menu.style.left = `${Math.max(8, box.left)}px`;
      menu.style.bottom = `${window.innerHeight - box.top + 6}px`;
    }

    const dismiss = (e) => {
      if (menu.contains(e.target)) return;
      menu.remove();
      document.removeEventListener('mousedown', dismiss);
    };
    setTimeout(() => document.addEventListener('mousedown', dismiss), 0);
  }

  async function choose(sessionId, platform) {
    try {
      const res = await fetch(`/api/sessions/${sessionId}/platform`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ platform }),
      });
      if (!res.ok) {
        const detail = await res.json().catch(() => ({}));
        note(detail.detail || 'Could not set the platform', 'device');
        return;
      }
      const summary = await res.json();
      summary.sessionId = sessionId;
      identified.set(sessionId, summary);
      updateStatus(sessionId);
      announce(summary);
    } catch (e) {
      note('Could not reach ShellMate to set the platform', 'device');
    }
  }

  window.getDeviceInfo = (sessionId) => identified.get(sessionId) || null;
  window.chooseDevicePlatform = openChooser;
})();
