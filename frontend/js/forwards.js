/**
 * forwards.js — Port forwards on a session (#405).
 *
 * The dialog lists what the session holds, lets one be added — local,
 * dynamic (SOCKS5) or remote — and removed, and can keep a forward on the
 * saved connection so it starts with every session from it.
 */
(function () {
  'use strict';

  const KIND_LABELS = {
    local:   'Local — a port here reaches a host via the device',
    dynamic: 'Dynamic — a SOCKS5 proxy here, via the device',
    remote:  'Remote — a port on the device reaches a host here',
  };

  async function open(tab) {
    if (!tab) return;
    let listing = { forwards: [], limit: 8 };
    try {
      const res = await fetch(`/api/sessions/${tab.sessionId}/forwards`);
      if (res.ok) listing = await res.json();
    } catch (_) { /* shown as empty */ }

    const content = document.createElement('div');
    content.className = 'forwards-list';
    if (!listing.forwards.length) {
      const none = document.createElement('div');
      none.className = 'forwards-empty';
      none.textContent = 'No forwards on this session yet.';
      content.appendChild(none);
    }
    listing.forwards.forEach(f => {
      const row = document.createElement('div');
      row.className = 'forwards-row';
      const text = document.createElement('span');
      text.className = 'forwards-text';
      text.textContent = f.describe + (f.connections ? ` · ${f.connections} connection${f.connections === 1 ? '' : 's'}` : '');
      const remove = document.createElement('button');
      remove.type = 'button';
      remove.className = 'btn-tertiary';
      remove.textContent = 'Stop';
      remove.title = 'Stop this forward';
      remove.addEventListener('click', async () => {
        remove.disabled = true;
        const forget = tab.profileId
          ? await window.shellmateDialog.confirm({
              title: 'Also forget it on the saved connection?',
              body: 'Stop only, or stop and remove it from the connection so it does not start next time.',
              confirmLabel: 'Stop and forget', cancelLabel: 'Stop only' })
          : false;
        await fetch(`/api/sessions/${tab.sessionId}/forwards/${f.id}?forget=${forget ? 'true' : 'false'}`,
                    { method: 'DELETE' });
        row.remove();
      });
      row.append(text, remove);
      content.appendChild(row);
    });

    const answer = await window.shellmateDialog.form({
      title: `Port forwards — ${tab.label || tab.hostname || 'session'}`,
      body: `Listeners bind to this machine only. Up to ${listing.limit} per session.`,
      content,
      confirmLabel: 'Add forward',
      cancelLabel: 'Close',
      fields: [
        { name: 'kind', label: 'Kind', type: 'select',
          options: Object.entries(KIND_LABELS).map(([value, label]) => ({ value, label })) },
        { name: 'listen_port', label: 'Listening port', type: 'text', placeholder: '8443' },
        { name: 'host', label: 'Destination host', type: 'text',
          placeholder: 'as the device sees it (blank for dynamic)' },
        { name: 'port', label: 'Destination port', type: 'text', placeholder: '443' },
        { name: 'remember', label: 'Start this forward with every session from the saved connection',
          type: 'checkbox', value: false },
      ],
      validate: (v) => {
        const lp = Number(v.listen_port);
        if (!(lp >= 1 && lp <= 65535)) return 'The listening port must be 1–65535.';
        if (v.kind !== 'dynamic' && (!v.host || !(Number(v.port) >= 1 && Number(v.port) <= 65535))) {
          return 'A destination host and port are needed for a local or remote forward.';
        }
        return '';
      },
    });
    if (!answer) return;

    try {
      const res = await fetch(`/api/sessions/${tab.sessionId}/forwards`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          kind: answer.kind, listen_port: Number(answer.listen_port),
          host: answer.host || '', port: Number(answer.port) || 0,
          remember: !!answer.remember && !!tab.profileId,
        }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || `Server error ${res.status}`);
      if (window.shellmateAlerts) {
        window.shellmateAlerts.notify({ severity: 'info', icon: 'lan',
          title: 'Forward started', body: data.describe });
      }
      // Back to the list, so the new one is seen and another can be added.
      open(tab);
    } catch (err) {
      if (window.shellmateAlerts) {
        window.shellmateAlerts.notify({ severity: 'warning', icon: 'error',
          title: 'Forward not started', body: err.message });
      }
    }
  }

  window.shellmateForwards = { open };
})();
