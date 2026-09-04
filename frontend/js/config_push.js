/**
 * config_push.js — Apply configuration with a preview first (#407).
 *
 * Paste or type the lines, see each one marked new / already present /
 * removal against the running configuration, then send. Afterwards the
 * before-and-after diff opens in the drift panel, with a way to propose the
 * change back. Nothing is sent until the preview has been read and Apply
 * pressed, and a line the guardrail would hold refuses the whole push
 * unless it is confirmed.
 */
(function () {
  'use strict';

  async function open(tab, initialText) {
    if (!tab) return;
    const answer = await window.shellmateDialog.form({
      title: `Apply configuration to ${tab.label || tab.hostname || 'this device'}`,
      body: 'One command per line, exactly as you would type it in configuration mode. '
          + 'Nothing is sent until you have seen the preview.',
      confirmLabel: 'Preview',
      fields: [
        { name: 'text', label: 'Configuration', type: 'textarea', rows: 12,
          value: initialText || '', placeholder: 'interface GigabitEthernet0/2\n description uplink\n no shutdown' },
        { name: 'fresh', label: 'Capture the running configuration first (slower, exact)',
          type: 'checkbox', value: false },
      ],
      validate: (v) => (v.text && v.text.trim() ? '' : 'Nothing to apply yet.'),
    });
    if (!answer) return;
    await preview(tab, answer.text, !!answer.fresh);
  }

  async function preview(tab, text, fresh) {
    let report;
    try {
      const res = await fetch(`/api/configs/${tab.sessionId}/preview`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text, fresh }),
      });
      report = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(report.detail || `Server error ${res.status}`);
    } catch (err) {
      _warn('Preview failed', err.message);
      return;
    }

    const content = document.createElement('div');
    content.className = 'push-preview';
    report.lines.forEach(line => {
      const row = document.createElement('div');
      row.className = `diff-line push-${line.status}`;
      const mark = line.status === 'add' ? '+' : line.status === 'remove' ? '−' : '=';
      row.textContent = `${mark} ${line.text}`;
      content.appendChild(row);
    });
    if (report.dangerous && report.dangerous.length) {
      const warn = document.createElement('div');
      warn.className = 'push-danger';
      warn.textContent = `The guardrail would hold: ${report.dangerous.join('; ')}. Applying will need confirming.`;
      content.appendChild(warn);
    }

    const go = await window.shellmateDialog.form({
      title: `Preview — ${tab.label || tab.hostname || 'device'} (${report.platform})`,
      body: report.summary + ` It will be wrapped in "${report.commands.enter}" … "${report.commands.exit}".`,
      content,
      confirmLabel: 'Apply now',
      cancelLabel: 'Back',
      danger: !!(report.dangerous && report.dangerous.length),
      fields: [
        { name: 'save', label: report.commands.save
            ? `Save afterwards with "${report.commands.save}"` : 'Save afterwards (no save command known for this platform)',
          type: 'checkbox', value: false, disabled: !report.commands.save },
      ],
    });
    if (!go) { open(tab, text); return; }

    let force = false;
    if (report.dangerous && report.dangerous.length) {
      force = await window.shellmateDialog.confirm({
        title: 'Send the held commands too?',
        list: report.dangerous.map(t => ({ text: t, mono: true })),
        body: 'These are on the platform\'s dangerous list. They will be sent exactly as shown.',
        confirmLabel: 'Send them', danger: true,
      });
      if (!force) { open(tab, text); return; }
    }
    await apply(tab, text, !!go.save, force);
  }

  async function apply(tab, text, save, force) {
    if (window.shellmateAlerts) {
      window.shellmateAlerts.notify({ severity: 'info', icon: 'tune',
        title: 'Applying configuration', body: 'Watch the terminal — every line is echoed there.' });
    }
    let result;
    try {
      const res = await fetch(`/api/configs/${tab.sessionId}/apply`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text, save, force }),
      });
      result = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(result.detail || `Server error ${res.status}`);
    } catch (err) {
      _warn('Apply failed', err.message);
      return;
    }
    const diff = result.diff || {};
    if (typeof window.showConfigDiff === 'function' && diff.diff !== undefined) {
      window.showConfigDiff({
        hostname: tab.hostname || tab.label, changed: diff.changed,
        added: diff.added, removed: diff.removed, days_since: 0, diff: diff.diff,
        // Named so Explain in that window means *this push* rather than
        // whatever the connect-time drift check found (#549).
        old_id: result.before_id, new_id: result.after_id,
      }, { display_label: tab.label, session_id: tab.sessionId });
      _offerRestore(tab, result.before_id);
    } else if (window.shellmateAlerts) {
      window.shellmateAlerts.notify({ severity: 'info', icon: 'tune',
        title: 'Configuration applied',
        body: `${result.sent.length} lines sent. ${diff.changed ? diff.changed + ' lines differ' : 'No difference captured'}${result.saved ? ', and saved' : ''}.` });
    }
  }

  /** A button in the diff panel that proposes the way back. */
  function _offerRestore(tab, beforeId) {
    const header = document.querySelector('#diff-panel .panel-header');
    if (!header || !beforeId) return;
    header.querySelectorAll('.push-restore').forEach(el => el.remove());
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'btn-tertiary push-restore';
    button.title = 'Propose the change that would take the device back to the capture from before this push';
    button.innerHTML = '<span class="material-symbols-outlined">history</span> Propose the way back';
    button.addEventListener('click', () => restore(tab, beforeId));
    header.insertBefore(button, header.querySelector('#diff-close'));
  }

  async function restore(tab, snapshotId) {
    let proposal;
    try {
      const res = await fetch(`/api/configs/${tab.sessionId}/restore/${snapshotId}`);
      proposal = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(proposal.detail || `Server error ${res.status}`);
    } catch (err) {
      _warn('Could not build the proposal', err.message);
      return;
    }
    if (!proposal.line_count) {
      if (window.shellmateAlerts) {
        window.shellmateAlerts.notify({ severity: 'info', icon: 'history',
          title: 'Nothing to restore', body: 'The running configuration already matches that capture.' });
      }
      return;
    }
    if (window.shellmateAlerts) {
      window.shellmateAlerts.notify({ severity: 'warning', icon: 'history',
        title: 'Read it before you apply it', body: proposal.note });
    }
    open(tab, proposal.text);
  }

  function _warn(title, body) {
    if (window.shellmateAlerts) {
      window.shellmateAlerts.notify({ severity: 'warning', icon: 'error', title, body });
    }
  }

  window.shellmateConfigPush = { open, restore };
})();
