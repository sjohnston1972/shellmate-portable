/**
 * tabtip.js — The hover card on a tab (#435).
 *
 * A tab's title attribute could only say the label. This shows what a
 * person hovering actually wants: the groups the session is in, address
 * and port, how it was connected and as whom, how long it has been up or
 * when it dropped, what the device was identified as, and whether
 * keep-alive, logging or port forwards are on. Built from nodes, never
 * markup; shown on hover after a short delay and on keyboard focus.
 */
(function () {
  'use strict';

  let card = null;
  let timer = null;
  let shownFor = null;

  document.addEventListener('DOMContentLoaded', () => {
    const list = document.getElementById('tab-list');
    if (!list) return;
    list.addEventListener('mouseover', (e) => {
      const tab = e.target.closest('.tab');
      if (!tab || tab.contains(e.relatedTarget)) return;
      schedule(tab);
    });
    list.addEventListener('mouseout', (e) => {
      const tab = e.target.closest('.tab');
      if (!tab || tab.contains(e.relatedTarget)) return;
      cancel();
    });
    list.addEventListener('focusin', (e) => {
      const tab = e.target.closest('.tab');
      if (tab) schedule(tab, 0);
    });
    list.addEventListener('focusout', cancel);
    list.addEventListener('mousedown', cancel);
    list.addEventListener('contextmenu', cancel);
    window.addEventListener('scroll', cancel, true);
    window.addEventListener('resize', cancel);
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape') cancel(); });
  });

  function schedule(tab, delay) {
    cancel();
    timer = setTimeout(() => show(tab), delay === undefined ? 450 : delay);
  }

  function cancel() {
    clearTimeout(timer);
    timer = null;
    if (card) { card.remove(); card = null; }
    shownFor = null;
  }

  function row(key, value, cls) {
    const k = document.createElement('span');
    k.className = 'tab-tip-key';
    k.textContent = key;
    const v = document.createElement('span');
    v.className = 'tab-tip-val' + (cls ? ' ' + cls : '');
    v.textContent = value;
    return [k, v];
  }

  async function show(tab) {
    const sessionId = tab.dataset.sessionId;
    const info = typeof window.tabTooltipInfo === 'function' ? window.tabTooltipInfo(sessionId) : null;
    if (!info) return;
    // The native title would appear on top of this; it says less.
    tab.querySelectorAll('[title]').forEach(el => {
      if (!el.dataset.tipTitle) { el.dataset.tipTitle = el.title; el.title = ''; }
    });

    card = document.createElement('div');
    card.className = 'tab-tip';
    card.setAttribute('role', 'tooltip');

    const title = document.createElement('div');
    title.className = 'tab-tip-title';
    title.textContent = info.label;
    if (info.customLabel && info.autoLabel && info.autoLabel !== info.label) {
      const sub = document.createElement('span');
      sub.className = 'tab-tip-sub';
      sub.textContent = ` (${info.autoLabel})`;
      title.appendChild(sub);
    }
    card.appendChild(title);

    const grid = document.createElement('div');
    grid.className = 'tab-tip-row';
    const add = (key, value, cls) => { if (value) grid.append(...row(key, value, cls)); };

    const target = info.connectionType === 'serial'
      ? info.address
      : info.address + (info.port ? `:${info.port}` : '');
    add('Address', target);
    add('Via', info.connectionType.toUpperCase() + (info.username ? ` as ${info.username}` : ''));
    add('State', info.isConnected
      ? 'Connected' + (info.uptime ? ` for ${info.uptime}` : '')
      : 'Disconnected' + (info.uptime ? ` after ${info.uptime}` : ''),
      info.isConnected ? 'tab-tip-state-up' : 'tab-tip-state-down');

    // What this device is, from the saved connection (#536): it survives
    // the tab, so it is here even before this session has asked.
    if (info.inventory) add('Hardware', info.inventory);

    const groups = groupsFor(info.group);
    if (groups) {
      const [k, v] = row('Groups', groups);
      v.classList.add('tab-tip-groups');
      grid.append(k, v);
    }

    const device = typeof window.getDeviceInfo === 'function' ? window.getDeviceInfo(sessionId) : null;
    if (device && (device.name || device.platform)) {
      const sure = device.confidence >= 0.9 ? '' : device.confidence >= 0.6 ? ' (probably)' : ' (a guess)';
      add('Device', [device.name || device.platform, device.version].filter(Boolean).join(' ') + sure);
    }

    const flags = [];
    if (info.keepAlive) flags.push('keep-alive');
    if (info.logging) flags.push('logging');
    if (!info.profileId) flags.push('not saved');
    if (flags.length) add('Also', flags.join(', '));

    card.appendChild(grid);
    document.body.appendChild(card);
    shownFor = sessionId;
    place(tab);

    // Forwards are a round trip; they fill in behind the card.
    if (info.connectionType === 'ssh') {
      try {
        const res = await fetch(`/api/sessions/${sessionId}/forwards`);
        if (res.ok && card && shownFor === sessionId) {
          const data = await res.json();
          if (data.forwards && data.forwards.length) {
            grid.append(...row('Forwards', data.forwards.map(f => f.describe).join('\n')));
            place(tab);
          }
        }
      } catch (_) { /* the card stands without it */ }
    }
  }

  /** "site-3 › access" for a nested group key; blank for none. */
  function groupsFor(key) {
    if (!key) return '';
    return key.split('/').join(' › ');
  }

  function place(tab) {
    if (!card) return;
    const box = tab.getBoundingClientRect();
    const size = card.getBoundingClientRect();
    const margin = 8;
    let left = Math.max(margin, Math.min(box.left, window.innerWidth - size.width - margin));
    let top = box.bottom + 6;
    if (top + size.height > window.innerHeight - margin) top = Math.max(margin, box.top - size.height - 6);
    card.style.left = `${left}px`;
    card.style.top = `${top}px`;
  }
})();
