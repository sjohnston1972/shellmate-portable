/**
 * ansible_keys.js — Credentials a run needs, held in the vault and sent only with a run that needs them (#586).
 *
 * `backend/ansible_keys.py` states the limits of this store in its own
 * module docstring, because they are the kind of thing a UI is tempted to
 * paper over. This file states them again, on screen, for the same reason:
 * a locked door with a sign reading "not actually locked" is worse than no
 * sign at all. Nothing below softens any of the three:
 *
 *   1. There is no way to read a stored value back. Listing returns names,
 *      kinds and where each is delivered — never the value. A key nobody
 *      remembers has to be replaced.
 *   2. The value does reach the runner. It has to — Ansible is what uses
 *      it. What the vault buys is that the secret is never in a playbook,
 *      never in a file on the container, and never in a shell history.
 *   3. While a run executes, the value sits in the runner container's
 *      process environment, and a playbook that prints it leaks it itself.
 *      `no_log: true` is the play author's job, not something reachable
 *      from here.
 *
 * The create/edit dialog is `shellmateDialog.form`, which already masks a
 * password field and gives it a reveal toggle — that toggle shows what was
 * just typed into the box, which is not the same thing as reading back what
 * is stored, and does not contradict point 1 above.
 */

(function () {
  'use strict';

  const view = window.ansibleView;
  if (!view) return;
  const { el, icon, clear, empty } = view;

  /** What the backend falls back to before `/api/ansible/keys` has answered. */
  const FALLBACK_KINDS = {
    generic: 'Something else',
    cloud: 'A cloud API credential',
    device: 'A device or network credential',
    vault: 'An Ansible Vault password',
    ssh: 'An SSH key passphrase',
  };

  function currentKinds() {
    const kinds = (view.state.keys || {}).kinds || {};
    return Object.keys(kinds).length ? kinds : FALLBACK_KINDS;
  }

  // -- Create / edit ---------------------------------------------------------

  async function openForm(entry) {
    const kinds = currentKinds();
    const kindOptions = Object.entries(kinds).map(([value, label]) => ({ value, label }));

    const fields = [
      { name: 'name', label: 'Name', value: entry ? entry.name : '', required: true,
        hint: 'Lower case letters, digits and underscores, starting with a '
            + 'letter or underscore — this is the name a run refers to.' },
      { name: 'kind', label: 'Kind', type: 'select',
        value: entry ? entry.kind : 'generic', options: kindOptions },
      { name: 'delivery', label: 'Delivered as', type: 'select',
        value: entry ? entry.delivery : 'env',
        options: [
          { value: 'env', label: 'Environment variable — visible to the whole play' },
          { value: 'extra_var', label: 'Extra var — a task has to name it' },
        ] },
      { name: 'target', label: 'Target name', value: entry ? entry.target : '',
        hint: 'Leave blank for the default: the name in capitals for an '
            + 'environment variable, or the name as it is for an extra var.' },
      { name: 'description', label: 'Description', value: entry ? entry.description : '' },
      { name: 'value', label: entry ? 'New value' : 'Value', type: 'password',
        required: !entry,
        hint: entry
          ? 'Leave this blank to keep the value already stored — nothing here '
          + 'can show you what that is, so this is the only way to leave it '
          + 'alone. Type a new one only to replace it.'
          : "Held in the encrypted vault, never written into a playbook or "
          + "into this list. It reaches the runner's own process environment "
          + "the moment a run needs it, and there is no way to read it back "
          + "afterwards." },
    ];

    const values = await window.shellmateDialog.form({
      title: entry ? `Edit ${entry.name}` : 'New key',
      fields,
      confirmLabel: 'Save',
    });
    if (!values) return;

    const payload = {
      id: entry ? entry.id : '',
      name: values.name,
      kind: values.kind,
      delivery: values.delivery,
      target: values.target,
      description: values.description,
      value: values.value,
    };

    try {
      await view.post('/api/ansible/keys', payload);
      await view.load();
    } catch (e) {
      view.toast(e.message, 'error');
    }
  }

  async function remove(entry) {
    const ok = await window.shellmateDialog.confirm({
      title: `Delete ${entry.name}?`,
      body: 'The stored value is deleted with it — this cannot be undone, and '
          + 'nothing can bring the value back.',
      danger: true,
      confirmLabel: 'Delete',
    });
    if (!ok) return;
    try {
      await view.del(`/api/ansible/keys/${encodeURIComponent(entry.id)}`);
      await view.load();
    } catch (e) {
      view.toast(e.message, 'error');
    }
  }

  // -- Rendering ---------------------------------------------------------------

  /**
   * The three limits, said once at the top rather than folded into a
   * tooltip somebody would have to go looking for.
   */
  function notice() {
    return el('div', { class: 'av-notice av-notice-info av-key-notice' }, [
      icon('info'),
      el('div', {}, [
        el('p', {}, [
          el('strong', { text: 'There is no way to see a stored value again. ' }),
          'Listing only ever returns names, kinds and where each is delivered. '
          + 'A key nobody remembers has to be replaced.',
        ]),
        el('p', {}, [
          el('strong', { text: 'The value does reach the runner. ' }),
          'It has to — Ansible is what uses it. What the vault buys is that '
          + 'the secret is never in a playbook, never in a file on the '
          + 'container, and never in a shell history.',
        ]),
        el('p', {}, [
          el('strong', { text: 'While a run executes, the value sits in the '
            + "runner container's process environment. " }),
          'A playbook that prints the variable leaks it itself — '
          + 'no_log: true is the play author’s job, and neither of these '
          + 'can be closed from ShellMate’s side.',
        ]),
      ]),
    ]);
  }

  function statusCell(entry) {
    if (entry.readable) {
      return el('span', { class: 'av-key-status av-pill av-pill-ok' },
        [icon('encrypted'), 'Readable']);
    }
    return el('div', {}, [
      el('span', { class: 'av-key-status av-pill av-pill-warn' },
        [icon('lock'), 'Unreadable']),
      el('p', { class: 'av-key-status-fix',
        text: 'This will stop a run that needs it. Unlock the vault, or set '
            + 'the value again.' }),
    ]);
  }

  function row(entry) {
    return el('tr', {}, [
      el('td', { class: 'av-key-name', text: entry.name }),
      el('td', {}, el('code', { class: 'av-key-target', text:
        `${entry.delivery === 'extra_var' ? 'extra var' : 'env'} ${entry.target}` })),
      el('td', { class: 'av-key-desc', title: entry.description || '',
        text: entry.description || '—' }),
      el('td', {}, statusCell(entry)),
      el('td', { class: 'av-row-actions' }, [
        el('button', { type: 'button', class: 'icon-btn', title: 'Edit',
          onclick: () => openForm(entry) }, icon('edit')),
        el('button', { type: 'button', class: 'icon-btn', title: 'Delete',
          onclick: () => remove(entry) }, icon('delete_forever')),
      ]),
    ]);
  }

  function kindBlock(label, rows) {
    return el('section', { class: 'av-block' }, [
      el('h4', { class: 'av-block-title', text: `${label} (${rows.length})` }),
      el('table', { class: 'av-table av-key-table' }, [
        el('thead', {}, el('tr', {}, [
          el('th', { text: 'Name' }), el('th', { text: 'Delivered as' }),
          el('th', { text: 'Description' }), el('th', { text: 'Status' }),
          el('th', { text: '' }),
        ])),
        el('tbody', {}, rows.map(row)),
      ]),
    ]);
  }

  function newButton(style) {
    return el('button', {
      type: 'button', class: style === 'primary' ? 'btn-primary' : 'btn-secondary',
      onclick: () => openForm(null),
    }, [icon('add'), 'New key']);
  }

  function render(state) {
    const body = document.getElementById('av-keys-body');
    if (!body) return;
    clear(body);
    body.appendChild(notice());

    const keys = ((state.keys || {}).keys) || [];
    const kinds = currentKinds();

    if (!keys.length) {
      body.appendChild(view.blank({
        icon: 'key',
        title: 'Credentials a run needs, held in the vault',
        lines: [
          'A key holds one credential a playbook needs and ShellMate does not '
          + 'otherwise have — a cloud API key, an Ansible Vault password. It '
          + 'lives in the encrypted vault under a name, and a run refers to it '
          + 'by that name; the value is fetched only as the run starts.',
          'Each key is delivered either as an environment variable, which '
          + 'every task can see, or as an extra var, which a playbook has to '
          + 'name to use. They are not the same thing.',
          'No value can ever be read back. Listing keys returns names and '
          + 'whether the vault can currently read them, and nothing returns a '
          + 'value — so a key you cannot remember has to be replaced.',
          'And the part worth being plain about: the value does reach the '
          + 'runner, because Ansible is what uses it. What the vault buys is '
          + 'that it is not in a playbook, not in a file on the container and '
          + 'not in a shell history. It is not end-to-end secrecy.',
        ],
        action: newButton('primary'),
      }));
      return;
    }

    body.appendChild(el('div', { class: 'av-key-toolbar' }, newButton('primary')));

    const grouped = new Set();
    Object.keys(kinds).forEach((kind) => {
      const rows = keys.filter((k) => k.kind === kind);
      if (!rows.length) return;
      grouped.add(kind);
      body.appendChild(kindBlock(kinds[kind], rows));
    });
    const strays = keys.filter((k) => !grouped.has(k.kind));
    if (strays.length) body.appendChild(kindBlock('Other', strays));
  }

  view.area('keys', {
    onShow: (state) => render(state),
    onData: (state) => { if (view.current === 'keys') render(state); },
  });
})();
