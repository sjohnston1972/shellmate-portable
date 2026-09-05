/**
 * neighbours.js — "What else is on this site?" (#542).
 *
 * On a site you did not build, the first switch you get into knows about
 * the other twelve. The subnet scanner cannot help — everything
 * interesting is usually across a routed boundary — but CDP and LLDP are
 * already running on those switches and already know the management
 * address, the platform and both ends of every link.
 *
 * The panel's job is mostly to be honest about three things:
 *
 * **Which of these can actually be saved.** LLDP frequently reports a name
 * and no management address at all. Those are shown, because leaving them
 * out would hide half a site, but they cannot become a connection — there
 * is nothing to dial — and the row says so rather than failing later.
 *
 * **Which are already known.** Offering to save a device that is already
 * in the tree is how somebody ends up with two of everything.
 *
 * **Where the platform came from.** It is a guess read out of a string one
 * device advertised about another, so the row shows the string beside the
 * guess and nothing acts on it.
 */

(function () {
  'use strict';

  let found = null;
  let host = '';

  /** Collect from the active tab, and show what came back. */
  async function findNeighbours(tab) {
    const target = tab || (typeof window.getActiveTab === 'function'
      ? window.getActiveTab() : null);
    if (!target || !target.sessionId) return;

    host = target.label || target.hostname || '';
    let data;
    try {
      const response = await fetch(
        `/api/sessions/${encodeURIComponent(target.sessionId)}/neighbours`,
        { method: 'POST' });
      data = await response.json();
      if (!response.ok) throw new Error(data.detail || `the server answered ${response.status}`);
    } catch (e) {
      // The refusal is the useful part — "telnet cannot open a second
      // channel, and running these in your own session would put two
      // lines you did not type into your transcript" is an explanation,
      // not an error code.
      window.shellmateDialog.alert({
        title: `Neighbours of ${host}`,
        body: String(e.message || e),
      });
      return;
    }

    found = data;
    await _show();
  }

  async function _show() {
    const body = document.createElement('div');
    body.className = 'nei-body';

    const summary = document.createElement('p');
    summary.className = 'nei-summary';
    summary.textContent = found.neighbours.length
      ? `${found.neighbours.length} neighbour`
        + `${found.neighbours.length === 1 ? '' : 's'} of ${found.host}, `
        + `from ${found.commands.join(' and ')}.`
      : `${found.host} reported no neighbours.`;
    body.appendChild(summary);

    // Why a protocol said nothing, when it did. An empty list on its own
    // reads as "this device has no neighbours", which is a much stronger
    // claim than "ShellMate could not read the output".
    (found.quiet || []).forEach(entry => {
      const note = document.createElement('p');
      note.className = 'nei-quiet';
      note.textContent = `${entry.protocol.toUpperCase()}: ${entry.why}`;
      body.appendChild(note);
    });

    if (found.neighbours.length) {
      body.appendChild(_table());
      const hint = document.createElement('p');
      hint.className = 'nei-hint';
      hint.textContent = 'Platforms are read from what each neighbour '
        + 'advertised about itself. ShellMate treats them as a guess and '
        + 'sends nothing to a device on the strength of one.';
      body.appendChild(hint);
    }

    const savable = found.neighbours.filter(n => n.reachable && !n.known);
    const go = await window.shellmateDialog.confirm({
      title: `Neighbours of ${found.host}`,
      content: body,
      confirmLabel: savable.length
        ? `Save ${savable.length} selected` : 'Close',
      cancelLabel: 'Close',
    });
    if (go && savable.length) await _save();
  }

  function _table() {
    const table = document.createElement('table');
    table.className = 'nei-table';

    const head = document.createElement('tr');
    ['', 'Device', 'Address', 'Platform', 'Seen on'].forEach(text => {
      const th = document.createElement('th');
      th.textContent = text;
      head.appendChild(th);
    });
    table.appendChild(head);

    found.neighbours.forEach((entry, index) => {
      const row = document.createElement('tr');

      const tick = document.createElement('td');
      const box = document.createElement('input');
      box.type = 'checkbox';
      box.dataset.index = String(index);
      // Only what can be saved is tickable, and each untickable row says
      // which of the two reasons applies.
      box.disabled = !entry.reachable || entry.known;
      box.checked = entry.reachable && !entry.known;
      box.title = entry.known ? 'Already saved'
        : (!entry.reachable ? 'No management address to connect to' : '');
      tick.appendChild(box);
      row.appendChild(tick);

      const name = document.createElement('td');
      name.className = 'nei-name';
      // textContent throughout: every one of these strings came off a
      // device and none of it is ours to trust as markup.
      name.textContent = entry.name;
      if (entry.known) {
        const known = document.createElement('span');
        known.className = 'nei-known';
        known.textContent = 'known';
        name.appendChild(known);
      }
      row.appendChild(name);

      const address = document.createElement('td');
      address.className = 'nei-address';
      address.textContent = entry.address || '—';
      if (!entry.address) {
        address.title = 'This neighbour advertised a name but no management '
                      + 'address, so there is nothing to connect to.';
      }
      row.appendChild(address);

      const platform = document.createElement('td');
      platform.textContent = entry.platform || '—';
      platform.title = entry.platform_description || '';
      platform.className = entry.platform ? 'nei-guess' : '';
      row.appendChild(platform);

      const port = document.createElement('td');
      port.className = 'nei-port';
      port.textContent = entry.local_port
        ? `${entry.local_port}${entry.remote_port ? ` → ${entry.remote_port}` : ''}`
        : '—';
      row.appendChild(port);

      table.appendChild(row);
    });
    return table;
  }

  /**
   * Save the ticked ones, into a group and against a credential.
   *
   * The devices a neighbour sweep finds are a site rather than a subnet,
   * and arriving ungrouped means dragging twelve of them one at a time.
   */
  async function _save() {
    const ticked = [...document.querySelectorAll('.nei-table input:checked')]
      .map(box => found.neighbours[Number(box.dataset.index)])
      .filter(Boolean);
    if (!ticked.length) return;

    let groups = [];
    try {
      groups = (await (await fetch('/api/groups')).json()).groups || [];
    } catch (e) { groups = []; }

    // The scanner's dialog, not a second one. It already handles a locked
    // vault, an empty credential list and creating one inline — three
    // things a copy would get wrong differently — and the group is asked
    // alongside rather than in a dialog of its own.
    const answers = await window.askForBulkLogin(ticked.length, [{
      name: 'group', label: 'Group', type: 'select',
      hint: 'The devices a neighbour sweep finds are a site, not a subnet.',
      options: [{ value: '', label: 'No group' },
                ...groups.map(g => ({ value: g.key, label: g.name }))],
    }]);
    if (!answers) return;

    try {
      const result = await (await fetch('/api/discovery/save', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          devices: ticked.map(entry => ({
            address: entry.address,
            hostname: entry.name,
            suggested_type: 'ssh',
            // The guess travels with it, so the profile arrives knowing
            // what it probably is — and, being below the threshold, it is
            // still confirmed on connect before anything is sent.
            platform: entry.platform || '',
          })),
          username: answers.username || '',
          credential_ref: answers.credential_ref || '',
          tags: answers.group ? [answers.group] : [],
        }),
      })).json();

      if (typeof window.renderWelcomeProfiles === 'function') {
        window.renderWelcomeProfiles();
      }
      window.shellmateDialog.alert({
        title: 'Saved',
        body: `${result.saved} added`
            + (result.already_saved ? `, ${result.already_saved} already known` : '')
            + (answers.group ? `, in ${answers.group}` : '') + '.',
      });
    } catch (e) {
      window.shellmateDialog.alert({
        title: 'Could not save',
        body: String(e.message || e),
      });
    }
  }

  window.findNeighbours = findNeighbours;
})();
