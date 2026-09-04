/**
 * ansible_environments.js — Named settings a run inherits, so production is one choice not six fields (#586).
 *
 * An environment is a shortcut with opinions: pick "Production" and the
 * inventory, the limit, the variables and the verbosity are already decided
 * — decided once, by whoever set the environment up, rather than typed the
 * same way (or not quite the same way) every time somebody starts a run.
 *
 * `force_check` is the one field that gets special handling everywhere it
 * appears here. `backend/ansible_library.py` only ever lets an environment
 * turn checking ON — nothing a run sends can argue it back off — so this
 * file treats it as a standing fact about the environment rather than an
 * ordinary tickbox: it earns a pill on the card, not just a row in a form.
 *
 * The create/edit dialog is `shellmateDialog.form`, the same one every other
 * settings-shaped form in ShellMate uses. It has no field type for "a map
 * of names to values", so the variable list is built by hand and passed in
 * as `content` — a plain node the dialog drops into the layout and never
 * looks inside. Reading it back happens after the dialog resolves, from the
 * same closure that built it, rather than through the dialog's own
 * `values` — which only ever knows about the fields it rendered itself.
 */

(function () {
  'use strict';

  const view = window.ansibleView;
  if (!view) return;
  const { el, icon, clear, empty } = view;

  // -- The variable editor, dropped into the dialog as `content` -----------

  /**
   * A repeatable name/value row, because `EnvironmentRequest.variables` is a
   * map of arbitrary size and the form dialog only knows fixed fields.
   *
   * Blank rows are just how a form starts and how "delete a variable"
   * reads while the dialog is still open — they are dropped silently by
   * `collect()`, not flagged as an error, because a half-finished row is
   * the normal state of this control and not a mistake worth interrupting.
   */
  function buildVariableEditor(initial) {
    const rows = el('div', { class: 'av-env-vars-rows' });

    function addRow(name, value) {
      const row = el('div', { class: 'av-env-var-row' }, [
        el('input', { type: 'text', class: 'av-env-var-key', placeholder: 'name', value: name || '' }),
        el('input', { type: 'text', class: 'av-env-var-value', placeholder: 'value', value: value || '' }),
        el('button', {
          type: 'button', class: 'icon-btn', title: 'Remove this variable',
          onclick: () => row.remove(),
        }, icon('cancel')),
      ]);
      rows.appendChild(row);
    }

    Object.entries(initial || {}).forEach(([name, value]) => addRow(name, value));

    const wrap = el('div', { class: 'av-env-vars' }, [
      el('div', { class: 'av-env-vars-head' }, [
        el('span', { class: 'av-env-vars-title', text: 'Variables' }),
        el('button', {
          type: 'button', class: 'btn-tertiary', onclick: () => addRow('', ''),
        }, [icon('add'), 'Add variable']),
      ]),
      rows,
    ]);

    return {
      el: wrap,
      /** Blank-keyed rows are dropped; the last value typed for a name wins. */
      collect() {
        const out = {};
        rows.querySelectorAll('.av-env-var-row').forEach((row) => {
          const name = row.querySelector('.av-env-var-key').value.trim();
          if (name) out[name] = row.querySelector('.av-env-var-value').value;
        });
        return out;
      },
    };
  }

  // -- The estate's groups, for the inventory-source field -----------------

  /**
   * ShellMate's own connections, shaped as the "estate" source option.
   *
   * This is the same endpoint the Run dialog uses to preview an inventory
   * (`ansible.js`), asked for nothing more than the group names — it never
   * sends anything and works with the runner switched off, because an
   * environment is worth defining before the container is reachable.
   */
  async function groupOptions() {
    let inventory;
    try {
      inventory = await view.json('/api/ansible/inventory');
    } catch (e) {
      return [{ value: '', label: 'Every connection' }];
    }
    const groups = inventory.groups || {};
    const options = [{ value: '', label: `Every connection (${(inventory.hosts || []).length})` }];
    Object.keys(groups).sort().forEach((key) => {
      options.push({ value: key, label: `${key} (${groups[key].length})` });
    });
    return options;
  }

  // -- Create / edit --------------------------------------------------------

  async function openForm(entry) {
    const groups = await groupOptions();
    const varsEditor = buildVariableEditor(entry ? entry.variables : {});

    const fields = [
      { name: 'name', label: 'Name', value: entry ? entry.name : '', required: true,
        hint: 'Letters, digits, spaces, dots, dashes and underscores.' },
      { name: 'description', label: 'Description', value: entry ? entry.description : '' },
      { name: 'inventory_source', label: 'Inventory source', type: 'select',
        value: entry ? entry.inventory_source : 'estate',
        options: [
          { value: 'estate', label: 'A group of your own connections' },
          { value: 'runner', label: 'A path already on the runner' },
        ] },
      { name: 'group', label: 'Estate group', type: 'select',
        value: entry ? entry.group : '', options: groups,
        hint: 'Used when the source above is your own connections.' },
      { name: 'inventory_path', label: 'Runner inventory path', value: entry ? entry.inventory_path : '',
        placeholder: 'inventory/production',
        hint: 'Used when the source above is a path on the runner.' },
      { name: 'limit', label: 'Limit', value: entry ? entry.limit : '',
        hint: "Ansible's --limit pattern, to narrow the group further. Optional." },
      { name: 'forks', label: 'Forks', value: entry && entry.forks ? String(entry.forks) : '',
        hint: "How many hosts run at once. Blank uses the runner's own default." },
      { name: 'verbosity', label: 'Verbosity', type: 'select',
        value: entry ? String(entry.verbosity || 0) : '0',
        options: [0, 1, 2, 3, 4].map((n) => ({
          value: String(n), label: n === 0 ? 'Normal' : `-${'v'.repeat(n)}`,
        })) },
      { name: 'force_check', label: 'Force check mode', type: 'checkbox',
        value: entry ? Boolean(entry.force_check) : false,
        hint: 'Every run against this environment is forced into check (dry-run) '
            + 'mode — nothing a run sends can argue it back off, only this '
            + 'setting can turn it on. If a run sometimes needs to write, give '
            + 'it a different environment rather than expecting to override '
            + 'this one.' },
    ];

    const values = await window.shellmateDialog.form({
      title: entry ? `Edit ${entry.name}` : 'New environment',
      fields,
      content: varsEditor.el,
      confirmLabel: 'Save',
      validate: (vals) => {
        if (vals.forks && !/^\d+$/.test(vals.forks)) return 'Forks must be a whole number.';
        return '';
      },
    });
    if (!values) return;

    const payload = {
      id: entry ? entry.id : '',
      name: values.name,
      description: values.description,
      inventory_source: values.inventory_source,
      group: values.group,
      inventory_path: values.inventory_path,
      limit: values.limit,
      forks: values.forks ? parseInt(values.forks, 10) : null,
      verbosity: parseInt(values.verbosity, 10) || 0,
      force_check: values.force_check,
      variables: varsEditor.collect(),
    };

    try {
      await view.post('/api/ansible/environments', payload);
      await view.load();
    } catch (e) {
      view.toast(e.message, 'error');
    }
  }

  async function remove(entry) {
    const ok = await window.shellmateDialog.confirm({
      title: `Delete ${entry.name}?`,
      body: 'A run that names this environment will stop finding it. This cannot be undone.',
      danger: true,
      confirmLabel: 'Delete',
    });
    if (!ok) return;
    try {
      await view.del(`/api/ansible/environments/${encodeURIComponent(entry.id)}`);
      await view.load();
    } catch (e) {
      view.toast(e.message, 'error');
    }
  }

  // -- Rendering -------------------------------------------------------------

  function inventoryLabel(entry) {
    if (entry.inventory_source === 'runner') {
      return entry.inventory_path ? `Runner: ${entry.inventory_path}` : 'Runner (no path set)';
    }
    return entry.group ? `Group: ${entry.group}` : 'Every connection';
  }

  function metaRow(label, value) {
    return el('div', { class: 'av-env-meta-row' }, [
      el('dt', { text: label }), el('dd', { text: value }),
    ]);
  }

  function varChips(variables) {
    const entries = Object.entries(variables || {});
    if (!entries.length) return null;
    return el('div', { class: 'av-env-vars-chips' }, entries.map(([name, value]) =>
      el('span', { class: 'av-env-chip', title: `${name} = ${value}` }, `${name}=${value}`)));
  }

  function card(entry) {
    return el('article', { class: 'av-card av-env-card' }, [
      el('div', { class: 'av-env-card-head' }, [
        el('h4', { text: entry.name }),
        entry.force_check ? el('span', {
          class: 'av-pill av-pill-warn av-env-badge-check',
          title: 'Every run against this environment is forced into check mode. '
               + 'A run cannot turn this back off.',
        }, [icon('science'), 'Forces check mode']) : null,
      ]),
      entry.description ? el('p', { class: 'av-env-desc', text: entry.description }) : null,
      el('dl', { class: 'av-env-meta' }, [
        metaRow('Inventory', inventoryLabel(entry)),
        metaRow('Limit', entry.limit || 'None'),
        metaRow('Forks', entry.forks ? String(entry.forks) : "Runner's default"),
        metaRow('Verbosity', entry.verbosity ? `-${'v'.repeat(entry.verbosity)}` : 'Normal'),
      ]),
      varChips(entry.variables),
      el('div', { class: 'av-row-actions' }, [
        el('button', { type: 'button', class: 'icon-btn', title: 'Edit',
          onclick: () => openForm(entry) }, icon('edit')),
        el('button', { type: 'button', class: 'icon-btn', title: 'Delete',
          onclick: () => remove(entry) }, icon('delete_forever')),
      ]),
    ]);
  }

  function newButton(kind) {
    return el('button', {
      type: 'button', class: kind === 'primary' ? 'btn-primary' : 'btn-secondary',
      onclick: () => openForm(null),
    }, [icon('add'), 'New environment']);
  }

  function render(state) {
    const body = document.getElementById('av-environments-body');
    if (!body) return;
    clear(body);

    const list = (state.library && state.library.environments) || [];

    if (!list.length) {
      body.appendChild(empty(
        'No environments yet. An environment is a named set of run options, so '
        + '"run it against production" is one choice rather than six fields '
        + 'typed the same way every time.',
        newButton('secondary')));
      return;
    }

    body.appendChild(el('div', { class: 'av-env-toolbar' }, newButton('primary')));
    body.appendChild(el('div', { class: 'av-grid av-env-list' }, list.map(card)));
  }

  view.area('environments', {
    onShow: (state) => render(state),
    onData: (state) => { if (view.current === 'environments') render(state); },
  });
})();
