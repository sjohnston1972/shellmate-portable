/**
 * ansible_inventory.js — What a run is pointed at, from the estate or from the runner (#586).
 *
 * "What would this actually touch" has two different answers, and this
 * area keeps them apart rather than blending them into one confident list:
 *
 * - **The estate.** ShellMate's own connections and groups, shaped fresh by
 *   `backend.ansible.inventory_from_estate()` for whatever is picked here.
 *   Nothing is sent by looking — the runner keeps none of it, and it is
 *   only ever sent inline with a run that is actually started. What gets
 *   left out (a serial connection has no address to dial) is shown with
 *   its reason, as prominently as what gets in — a run that silently
 *   drops half a site is the exact failure this screen exists to catch.
 * - **The runner's own.** A directory it holds, with its own group_vars
 *   and host_vars. There is no endpoint that lists what is in it, so this
 *   says that plainly rather than rendering an empty list that would read
 *   as a bug.
 */

(function () {
  'use strict';

  const view = window.ansibleView;
  if (!view) return;
  const { el, icon, clear, empty } = view;

  /** Every group, fetched once per visit — cheap, and groups can change. */
  let groupsList = [];
  /** The group key chosen ("" means every SSH connection). */
  let selectedGroup = '';
  /** The last answer from /api/ansible/inventory, or null before the first. */
  let inventory = null;
  let loading = true;
  let loadError = '';

  async function refreshGroups() {
    try {
      const data = await view.json('/api/groups');
      groupsList = data.groups || [];
    } catch (e) {
      groupsList = [];
    }
  }

  async function refreshInventory() {
    loading = true;
    loadError = '';
    render();
    try {
      inventory = await view.json('/api/ansible/inventory'
        + (selectedGroup ? `?group=${encodeURIComponent(selectedGroup)}` : ''));
    } catch (e) {
      inventory = null;
      loadError = e.message || String(e);
    } finally {
      loading = false;
    }
    render();
  }

  function groupSelect() {
    const select = el('select', { class: 'setting-input', id: 'av-inv-group' }, [
      el('option', { value: '', text: 'Every SSH connection' }),
      ...groupsList.map(g => el('option', { value: g.key, text: `${g.name} (${g.count})` })),
    ]);
    select.value = selectedGroup;
    select.addEventListener('change', () => {
      selectedGroup = select.value;
      refreshInventory();
    });
    return select;
  }

  function toolbar() {
    return el('div', { class: 'av-inv-toolbar' }, [
      el('label', { class: 'av-inv-toolbar-label', for: 'av-inv-group', text: 'Group' }),
      groupSelect(),
      el('button', {
        type: 'button', class: 'icon-btn', title: 'Rebuild the inventory',
        onclick: () => refreshGroups().then(refreshInventory),
      }, icon('refresh')),
    ]);
  }

  /** A device ShellMate has not identified gets no `ansible_network_os`.
   *  That is the honest default, not a fault — so it reads as a fact, not
   *  a warning. */
  function platformCell(vars) {
    if (vars.ansible_network_os) {
      return el('span', { class: 'av-pill av-pill-ok av-inv-platform' },
        [icon('check_circle'), vars.ansible_network_os]);
    }
    return el('span', { class: 'av-inv-unmapped', text: 'none — unidentified' });
  }

  function hostsTable(hosts, hostvars, memberOf) {
    return el('table', { class: 'av-table' }, [
      el('thead', {}, el('tr', {}, [
        el('th', { text: 'Host' }), el('th', { text: 'Name' }),
        el('th', { text: 'Platform mapping' }), el('th', { text: 'Ansible groups' }),
      ])),
      el('tbody', {}, hosts.map((addr) => {
        const vars = hostvars[addr] || {};
        return el('tr', {}, [
          el('td', {}, el('span', { class: 'av-inv-host' }, [icon('dns'), addr])),
          el('td', { text: vars.shellmate_name && vars.shellmate_name !== addr
            ? vars.shellmate_name : '—' }),
          el('td', {}, platformCell(vars)),
          el('td', { class: 'av-inv-groups', text: (memberOf[addr] || []).join(', ') || '—' }),
        ]);
      })),
    ]);
  }

  /**
   * The left-out list. First-class, not a footnote: it sits directly under
   * the hosts it was built alongside, at the same size, so it cannot be
   * scrolled past without noticing there was anything to leave out.
   */
  function skippedSection(skipped) {
    if (!skipped.length) {
      return el('section', { class: 'av-block' }, [
        el('h4', { class: 'av-block-title', text: 'Left out' }),
        el('div', { class: 'av-notice av-notice-info' }, [
          icon('check_circle'),
          el('div', { text: 'Nothing was left out — every connection in this '
            + 'selection has an address Ansible can dial.' }),
        ]),
      ]);
    }
    return el('section', { class: 'av-block' }, [
      el('h4', { class: 'av-block-title' },
        el('span', { class: 'av-inv-skip-count', text: `${skipped.length} left out` })),
      el('table', { class: 'av-table' }, [
        el('thead', {}, el('tr', {}, [
          el('th', { text: 'Connection' }), el('th', { text: 'Why it is left out' }),
        ])),
        el('tbody', {}, skipped.map(s => el('tr', {}, [
          el('td', {}, el('span', { class: 'av-inv-skip-name' },
            [icon('error'), s.name || '(unnamed)'])),
          el('td', { class: 'av-inv-skip-why', text: s.why || '' }),
        ]))),
      ]),
    ]);
  }

  async function copyIni(button) {
    const text = (inventory && inventory.text) || '';
    if (!text) return;
    try {
      await navigator.clipboard.writeText(text);
    } catch (_) {
      // The route older webviews need; the clipboard API is not always there.
      const box = document.createElement('textarea');
      box.value = text;
      document.body.appendChild(box);
      box.select();
      try { document.execCommand('copy'); } catch (_) { /* give up quietly */ }
      box.remove();
    }
    if (typeof window._showCopyToast === 'function') window._showCopyToast();
    else if (button) {
      const was = button.textContent;
      button.textContent = 'Copied';
      setTimeout(() => { button.textContent = was; }, 1200);
    }
  }

  function iniSection() {
    const copyBtn = el('button', {
      type: 'button', class: 'btn-tertiary',
      onclick: (e) => copyIni(e.currentTarget),
    }, [icon('content_copy'), 'Copy']);
    return el('section', { class: 'av-block' }, [
      el('div', { class: 'av-inv-ini-head' }, [
        el('h4', { class: 'av-block-title', text: 'Generated inventory (INI)' }),
        copyBtn,
      ]),
      el('p', { class: 'av-inv-note', text: 'Exactly what would travel with a run against '
        + 'this selection. Nothing here is sent by looking at it — only a run that is '
        + 'actually started sends anything.' }),
      el('pre', { class: 'av-inv-ini', text: inventory.text || '' }),
    ]);
  }

  /**
   * The runner's own inventory has no endpoint that enumerates it — its
   * shape is a directory ansible-runner reads, with group_vars and
   * host_vars merged in beside it. Saying that plainly beats rendering an
   * empty list that would read as this screen having failed to find
   * anything.
   */
  function runnersOwnSection() {
    return el('section', { class: 'av-block' }, [
      el('h4', { class: 'av-block-title', text: "The runner's own inventory" }),
      el('div', { class: 'av-notice av-notice-info' }, [
        icon('storage'),
        el('div', {}, [
          'The service has no endpoint that lists what it holds. Its inventory '
          + "is a directory on the runner's own host, with group_vars and "
          + 'host_vars beside it that Ansible merges in automatically — there '
          + 'is nothing here for ShellMate to browse. A run either uses its '
          + "own default, or names a path on the runner directly; that choice "
          + 'is made from the Run dialog on a playbook, not here.',
        ]),
      ]),
    ]);
  }

  function render() {
    const body = document.getElementById('av-inventory-body');
    if (!body) return;
    clear(body);
    body.appendChild(toolbar());

    if (loadError) {
      body.appendChild(el('div', { class: 'av-notice av-notice-bad' }, [
        icon('error'),
        el('div', {}, [el('strong', { text: 'Could not build the inventory. ' }), loadError]),
      ]));
    }

    if (loading && !inventory) {
      body.appendChild(empty('Building the inventory…'));
      return;
    }
    if (!inventory) return;

    const hosts = inventory.hosts || [];
    const hostvars = inventory.hostvars || {};
    const skipped = inventory.skipped || [];
    const groupsMap = inventory.groups || {};

    const memberOf = {};
    Object.entries(groupsMap).forEach(([group, addrs]) => {
      (addrs || []).forEach((addr) => {
        (memberOf[addr] = memberOf[addr] || []).push(group);
      });
    });

    body.appendChild(el('section', { class: 'av-block' }, [
      el('h4', { class: 'av-block-title',
        text: `${hosts.length} host${hosts.length === 1 ? '' : 's'} would be included` }),
      hosts.length
        ? hostsTable(hosts, hostvars, memberOf)
        : empty('No saved SSH connections match this selection. Serial and telnet '
          + 'connections have no address for Ansible to dial, and are always left out.'),
    ]));

    body.appendChild(skippedSection(skipped));
    body.appendChild(iniSection());
    body.appendChild(runnersOwnSection());
  }

  function onShow() {
    render();
    Promise.resolve()
      .then(() => (groupsList.length ? null : refreshGroups()))
      .then(() => refreshInventory());
  }

  view.area('inventory', { onShow });
})();
