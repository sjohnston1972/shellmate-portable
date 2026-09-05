/**
 * ansible_inventory.js — What a run is pointed at, and where a list comes from (#586, #608).
 *
 * "What would this actually touch" has three different answers, and this
 * area keeps them apart rather than blending them into one confident list:
 *
 * - **The estate.** ShellMate's own connections and groups, shaped fresh by
 *   `backend.ansible.inventory_from_estate()` for whatever is picked here.
 *   Nothing is sent by looking — the runner keeps none of it, and it is
 *   only ever sent inline with a run that is actually started. What gets
 *   left out (a serial connection has no address to dial) is shown with
 *   its reason, as prominently as what gets in — a run that silently
 *   drops half a site is the exact failure this screen exists to catch.
 * - **A list somebody built (#608).** Either picked out of the estate —
 *   "the switches I am upgrading this weekend" is not a group and should
 *   not have to become one, because making it a group changes the tree
 *   everyone else sees for a list that stops mattering on Monday — or
 *   uploaded from somewhere ShellMate has never connected to at all.
 * - **The runner's own.** A directory it holds, with its own group_vars
 *   and host_vars. There is no endpoint that lists what is in it, so this
 *   says that plainly rather than rendering an empty list that would read
 *   as a bug.
 *
 * Two rules the upload half is built around, both of them refusals:
 *
 * **The host column is asked for, never guessed.** A header may say
 * `LAN IP`, `mgmt`, `ip_address` or nothing at all. Picking one by pattern
 * produces an inventory that is well-formed, looks populated, and targets
 * nothing — and the run that follows reports a problem about hosts rather
 * than about the file.
 *
 * **Whether the first row is a heading is stated, not assumed.** The
 * backend concludes one and says which; this shows that conclusion as a
 * tick box, because getting it wrong in either direction is a silent loss
 * — a heading read as a device adds a host called `ansible_host`, and a
 * device read as a heading drops one switch out of forty and nobody
 * notices.
 *
 * The generated INI used to be printed at the bottom of this area. It has
 * gone: what matters is which hosts are in and which were left out, and
 * both are already answered above, in tables somebody can read.
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

  /** The custom inventories, and what an upload may declare itself to be. */
  let customs = [];
  let platformOptions = [];
  let customsError = '';
  /** Worked examples of the shapes an upload arrives in (#608). */
  let examples = [];

  /** Addresses ticked in the estate table, for building a curated list. */
  const chosen = new Set();

  /** The upload being mapped, or null when there is none in progress. */
  let upload = null;

  async function refreshGroups() {
    try {
      const data = await view.json('/api/groups');
      groupsList = data.groups || [];
    } catch (e) {
      groupsList = [];
    }
  }

  async function refreshCustoms() {
    try {
      const data = await view.json('/api/ansible/inventories');
      customs = data.inventories || [];
      platformOptions = data.platforms || [];
      customsError = '';
      if (!examples.length) {
        // Once per session: they are shipped constants, not state.
        const shipped = await view.json('/api/ansible/inventories/examples');
        examples = shipped.examples || [];
      }
    } catch (e) {
      customs = [];
      customsError = e.message || String(e);
    }
  }

  async function refreshInventory() {
    loading = true;
    loadError = '';
    render();
    try {
      inventory = await view.json('/api/ansible/inventory'
        + (selectedGroup ? `?group=${encodeURIComponent(selectedGroup)}` : ''));
      // The whole estate goes to the shared source, so the builder's tree
      // and this table are one fetch and one answer rather than two that
      // can disagree (#601). A filtered read is this area's own business.
      if (!selectedGroup && window.ansibleEstate) {
        window.ansibleEstate.adopt(inventory);
      }
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
      // Ticks belong to the list that was on screen. Carrying them across
      // a change of group would build a list out of hosts nobody can see.
      chosen.clear();
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
        onclick: () => Promise.all([refreshGroups(), refreshCustoms()])
          .then(refreshInventory),
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
    return el('table', { class: 'av-table av-inv-hosts' }, [
      el('thead', {}, el('tr', {}, [
        el('th', { class: 'av-inv-tick' }, el('input', {
          type: 'checkbox', id: 'av-inv-tick-all',
          title: 'Tick every host shown',
          onchange: (e) => {
            hosts.forEach(addr => (e.currentTarget.checked
              ? chosen.add(addr) : chosen.delete(addr)));
            render();
          },
        })),
        el('th', { text: 'Host' }), el('th', { text: 'Name' }),
        el('th', { text: 'Platform mapping' }), el('th', { text: 'Ansible groups' }),
      ])),
      el('tbody', {}, hosts.map((addr) => {
        const vars = hostvars[addr] || {};
        return el('tr', { class: chosen.has(addr) ? 'av-inv-picked' : '' }, [
          el('td', { class: 'av-inv-tick' }, el('input', {
            type: 'checkbox', 'data-host': addr, checked: chosen.has(addr),
            onchange: (e) => {
              if (e.currentTarget.checked) chosen.add(addr);
              else chosen.delete(addr);
              render();
            },
          })),
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

  // -------------------------------------------------------------------------
  // Lists somebody built (#608)
  // -------------------------------------------------------------------------

  /**
   * Save what is ticked in the estate table as a named list.
   *
   * The rows carry what ShellMate already knew — address, name, user, port
   * and the platform id — so a curated list is not a set of pointers that
   * rot when a profile is edited, but neither does it invent anything the
   * estate did not hold.
   */
  async function saveCurated() {
    if (!chosen.size) return;
    const hostvars = (inventory && inventory.hostvars) || {};
    const answers = await window.shellmateDialog.form({
      title: `Save ${chosen.size} host${chosen.size === 1 ? '' : 's'} as a list`,
      body: 'A named list of devices to point a run at. It does not change '
            + 'your groups, and deleting it later touches nothing else.',
      fields: [
        { name: 'name', label: 'Name', placeholder: 'Weekend upgrade' },
        { name: 'description', label: 'What it is for (optional)',
          placeholder: 'The access switches going to 17.9' },
      ],
      confirmLabel: 'Save list',
    });
    if (!answers || !(answers.name || '').trim()) return;

    const hosts = [...chosen].map((addr) => {
      const vars = hostvars[addr] || {};
      const row = { host: addr };
      if (vars.shellmate_name && vars.shellmate_name !== addr) row.name = vars.shellmate_name;
      if (vars.ansible_user) row.user = vars.ansible_user;
      if (vars.ansible_port) row.port = vars.ansible_port;
      if (vars.shellmate_platform) row.platform = vars.shellmate_platform;
      return row;
    });

    try {
      await view.post('/api/ansible/inventories', {
        name: answers.name, description: answers.description || '',
        source: 'estate', hosts,
      });
      chosen.clear();
      await refreshCustoms();
      render();
      view.toast(`Saved “${answers.name}”.`, 'ok');
    } catch (e) {
      view.toast(e.message || String(e), 'error');
    }
  }

  /** A file from anywhere — Meraki, an IPAM, a spreadsheet, a plain list. */
  function pickFile() {
    const input = document.getElementById('av-inv-file');
    if (input) input.click();
  }

  async function readFile(file) {
    if (!file) return;
    const text = await file.text();
    await previewUpload(text, file.name, null);
  }

  /**
   * Ask the backend what the file is, deciding nothing here.
   *
   * `headed` is passed straight back through on a re-read so the tick box
   * actually changes the parse rather than only the picture of it — a
   * preview that disagrees with what gets saved is worse than no preview.
   */
  async function previewUpload(text, filename, headed) {
    try {
      const read = await view.post('/api/ansible/inventories/preview',
        { text, filename, headed });
      const before = upload;
      upload = {
        text, filename, read,
        // Column choices survive a header re-read where the name still
        // exists; positional names change when the toggle flips, and a
        // mapping pointing at a column that is gone would be saved as a
        // refusal the user cannot see the cause of.
        mapping: {}, platform: '', name: (before && before.name)
          || filename.replace(/\.[^.]+$/, ''),
      };
      if (before && read.headers) {
        Object.entries(before.mapping || {}).forEach(([field, column]) => {
          if (read.headers.includes(column)) upload.mapping[field] = column;
        });
        upload.platform = before.platform || '';
      }
      render();
    } catch (e) {
      upload = null;
      render();
      view.toast(e.message || String(e), 'error');
    }
  }

  function columnSelect(field, label, required) {
    const select = el('select', {
      class: 'setting-input', id: `av-inv-map-${field}`,
      onchange: (e) => { upload.mapping[field] = e.currentTarget.value; },
    }, [
      el('option', { value: '', text: required ? 'Choose the column…' : 'not mapped' }),
      ...(upload.read.headers || []).map(h => el('option', { value: h, text: h })),
    ]);
    select.value = upload.mapping[field] || '';
    return el('div', { class: 'av-inv-map-field' }, [
      el('label', { class: 'setting-label', for: `av-inv-map-${field}`, text: label }),
      select,
    ]);
  }

  function platformSelect() {
    const select = el('select', {
      class: 'setting-input', id: 'av-inv-platform',
      onchange: (e) => { upload.platform = e.currentTarget.value; },
    }, [
      el('option', { value: '', text: 'Not saying — no platform mapping' }),
      ...platformOptions.map(p => el('option', { value: p.id,
        text: `${p.label} (${p.network_os})` })),
    ]);
    select.value = upload.platform || '';
    return select;
  }

  /**
   * The mapping panel.
   *
   * Everything on it is a statement the user makes, not one the file makes:
   * which column is the address, whether the first row is a heading, and
   * what the devices are. The one thing this screen refuses to do is fill
   * any of them in from a pattern.
   */
  function uploadPanel() {
    const read = upload.read;
    const isTable = read.kind === 'table';
    const rows = read.rows || [];

    const heading = el('label', { class: 'av-inv-headed' }, [
      el('input', {
        type: 'checkbox', id: 'av-inv-headed', checked: read.headed === true,
        onchange: (e) => previewUpload(upload.text, upload.filename,
          e.currentTarget.checked),
      }),
      el('span', { text: 'The first row is column headings' }),
    ]);

    return el('section', { class: 'av-block av-inv-upload' }, [
      el('div', { class: 'av-inv-upload-head' }, [
        el('h4', { class: 'av-block-title',
          text: `${upload.filename || 'Pasted list'} — ${read.count} row`
                + `${read.count === 1 ? '' : 's'}` }),
        el('button', {
          type: 'button', class: 'btn-tertiary',
          onclick: () => { upload = null; render(); },
        }, 'Cancel'),
      ]),

      isTable ? heading : el('p', { class: 'av-inv-note',
        text: 'One host per line, so there is nothing to map. Lines starting '
              + 'with # are ignored.' }),

      el('table', { class: 'av-table av-inv-preview' }, [
        isTable ? el('thead', {}, el('tr', {},
          (read.headers || []).map(h => el('th', { text: h })))) : null,
        el('tbody', {}, rows.slice(0, 6).map(row => el('tr', {},
          (Array.isArray(row) ? row : [row]).map(cell =>
            el('td', { text: String(cell === undefined ? '' : cell) }))))),
      ]),
      read.count > 6 ? el('p', { class: 'av-inv-note',
        text: `Showing the first 6 of ${read.count}.` }) : null,

      isTable ? el('div', { class: 'av-inv-mapping' }, [
        el('p', { class: 'av-inv-note',
          text: 'Which column is which. The address is asked for rather than '
                + 'guessed: a list built from the wrong column looks '
                + 'populated and dials nothing.' }),
        el('div', { class: 'av-inv-map-grid' }, [
          columnSelect('host', 'Address (required)', true),
          columnSelect('name', 'Name', false),
          columnSelect('user', 'Username', false),
          columnSelect('port', 'Port', false),
        ]),
      ]) : null,

      el('div', { class: 'av-inv-map-grid' }, [
        el('div', { class: 'av-inv-map-field' }, [
          el('label', { class: 'setting-label', for: 'av-inv-name', text: 'Name this list' }),
          el('input', {
            type: 'text', class: 'setting-input', id: 'av-inv-name',
            value: upload.name || '', autocomplete: 'off',
            oninput: (e) => { upload.name = e.currentTarget.value; },
          }),
        ]),
        el('div', { class: 'av-inv-map-field' }, [
          el('label', { class: 'setting-label', for: 'av-inv-platform',
            text: 'What these devices are' }),
          platformSelect(),
        ]),
      ]),
      el('p', { class: 'av-inv-note',
        text: 'Leaving the platform unsaid is the safe answer. A wrong one '
              + 'makes Ansible treat a firewall as a switch, and the failure '
              + 'arrives from a module several steps from the cause.' }),

      el('div', { class: 'av-inv-upload-actions' }, el('button', {
        type: 'button', class: 'btn-primary', id: 'av-inv-save-upload',
        onclick: saveUpload,
      }, [icon('save'), 'Save list'])),
    ]);
  }

  async function saveUpload() {
    if (!upload) return;
    try {
      await view.post('/api/ansible/inventories', {
        name: upload.name || '', source: 'upload',
        filename: upload.filename || '', platform: upload.platform || '',
        mapping: upload.mapping, headed: upload.read.headed === undefined
          ? null : upload.read.headed,
        text: upload.text,
      });
      upload = null;
      await refreshCustoms();
      render();
      view.toast('List saved.', 'ok');
    } catch (e) {
      // The server's own words: it is the side that knows why a mapping
      // was refused, and rewriting the reason here loses the reason.
      view.toast(e.message || String(e), 'error');
    }
  }

  async function removeCustom(entry) {
    const go = await window.shellmateDialog.confirm({
      title: `Delete “${entry.name}”?`,
      body: 'The list goes. The devices, the groups and the connections it '
            + 'named are untouched.',
      confirmLabel: 'Delete',
      destructive: true,
    });
    if (!go) return;
    try {
      await view.del(`/api/ansible/inventories/${encodeURIComponent(entry.id)}`);
      await refreshCustoms();
      render();
    } catch (e) {
      view.toast(e.message || String(e), 'error');
    }
  }

  function customsTable() {
    return el('table', { class: 'av-table av-inv-customs' }, [
      el('thead', {}, el('tr', {}, [
        el('th', { text: 'List' }), el('th', { text: 'Where it came from' }),
        el('th', { text: 'Hosts' }), el('th', { text: 'Platform' }),
        el('th', { text: '' }),
      ])),
      el('tbody', {}, customs.map(entry => el('tr', {}, [
        el('td', {}, el('div', { class: 'av-inv-custom-name' }, [
          el('span', { class: 'av-inv-host' }, [icon('list_alt'), entry.name]),
          entry.description
            ? el('span', { class: 'av-inv-note', text: entry.description })
            : null,
        ])),
        el('td', {}, el('span', { class: 'av-pill' },
          entry.source === 'upload'
            ? (entry.filename || 'an uploaded file') : 'picked from the estate')),
        el('td', { class: 'av-inv-count', text: String(entry.hosts) }),
        el('td', {}, entry.platform
          ? el('span', { class: 'av-pill av-pill-ok', text: entry.platform })
          : el('span', { class: 'av-inv-unmapped', text: 'none — not said' })),
        el('td', { class: 'av-row-actions' }, el('button', {
          type: 'button', class: 'icon-btn', title: 'Delete',
          onclick: () => removeCustom(entry),
        }, icon('delete'))),
      ]))),
    ]);
  }

  /**
   * Worked examples of what an upload can look like.
   *
   * Two ways into each, because they answer different questions. "Try it"
   * loads the file through the very same parse a real upload goes
   * through, so the mapping step can be seen before anybody goes hunting
   * for their own export; the download is for taking the shape away and
   * making their file match it.
   *
   * Each is asserted in the tests against that same code path. An example
   * that only worked because something special-cased it would be teaching
   * a shape the parser does not accept.
   */
  function examplesRow() {
    if (!examples.length) return null;
    return el('div', { class: 'av-inv-examples' }, [
      el('span', { class: 'av-inv-examples-label', text: 'Worked examples:' }),
      ...examples.map(ex => el('span', { class: 'av-inv-example' }, [
        el('button', {
          type: 'button', class: 'btn-tertiary',
          'data-example': ex.id, title: ex.note,
          onclick: () => tryExample(ex),
        }, ex.title),
        el('a', {
          class: 'av-inv-example-get', href: `/api/ansible/inventories/examples/${ex.id}`,
          download: ex.filename, title: `Download ${ex.filename}`,
        }, icon('download')),
      ])),
    ]);
  }

  async function tryExample(ex) {
    let text = '';
    try {
      const response = await fetch(`/api/ansible/inventories/examples/${ex.id}`);
      if (!response.ok) throw new Error(`the server answered ${response.status}`);
      text = await response.text();
    } catch (e) {
      view.toast(e.message || String(e), 'error');
      return;
    }
    await previewUpload(text, ex.filename, null);
    // The mapping the example was written for, filled in — this one is a
    // demonstration, and leaving it blank would demonstrate the empty
    // form rather than the shape.
    if (upload) {
      Object.entries(ex.mapping || {}).forEach(([field, column]) => {
        if ((upload.read.headers || []).includes(column)) upload.mapping[field] = column;
      });
      render();
    }
  }

  /**
   * Lists somebody built, above the estate rather than below it.
   *
   * They are the narrower answer, and a run pointed at four switches is
   * the commoner intent than one pointed at everything — so the thing
   * most often wanted is the thing first on the screen.
   */
  function customsSection() {
    const file = el('input', {
      type: 'file', id: 'av-inv-file', class: 'av-inv-file',
      accept: '.csv,.txt,.tsv,text/csv,text/plain',
      onchange: (e) => {
        const picked = e.currentTarget.files && e.currentTarget.files[0];
        e.currentTarget.value = '';
        readFile(picked);
      },
    });

    return el('section', { class: 'av-block' }, [
      el('div', { class: 'av-inv-customs-head' }, [
        el('h4', { class: 'av-block-title', text: 'Custom inventories' }),
        el('span', { class: 'av-spacer' }),
        el('button', {
          type: 'button', class: 'btn-secondary', id: 'av-inv-curate',
          disabled: !chosen.size,
          title: chosen.size ? '' : 'Tick hosts in the estate table below',
          onclick: saveCurated,
        }, [icon('bookmark_add'),
            chosen.size ? `Save ${chosen.size} ticked as a list`
                        : 'Save ticked hosts as a list']),
        el('button', {
          type: 'button', class: 'btn-secondary', id: 'av-inv-upload',
          onclick: pickFile,
        }, [icon('upload'), 'Upload a file']),
        file,
      ]),
      el('p', { class: 'av-inv-note',
        text: 'A list to point a run at, without turning it into a group. '
              + 'Pick devices out of the estate below, or upload a CSV or a '
              + 'plain list of addresses from somewhere else entirely.' }),
      customsError
        ? el('div', { class: 'av-notice av-notice-warn' }, [
            icon('error'), el('div', { text: customsError })])
        : null,
      examplesRow(),
      customs.length ? customsTable() : empty(
        'No custom inventories yet. Tick some hosts below and save them as a '
        + 'list, or upload a file.'),
      upload ? uploadPanel() : null,
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
    body.appendChild(customsSection());

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

    // A group that does not exist is not an empty group. Both produce nought
    // hosts, and "0 hosts would be included" sends somebody to look at their
    // devices when the answer is that nothing answers to that name at all.
    if (inventory.group_known === false) {
      body.appendChild(el('div', { class: 'av-notice av-notice-warn' }, [
        icon('error'),
        el('div', {}, [
          el('strong', { text: `There is no group called “${inventory.group}”. ` }),
          'Nothing is tagged with it and no group of that name exists, so this '
          + 'is a name to check rather than an estate to fix — an empty '
          + 'result here would have looked exactly like a group whose devices '
          + 'are all serial consoles.',
        ]),
      ]));
      return;
    }

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
    body.appendChild(runnersOwnSection());
  }

  function onShow() {
    render();
    Promise.resolve()
      .then(() => (groupsList.length ? null : refreshGroups()))
      .then(refreshCustoms)
      .then(() => refreshInventory());
  }

  view.area('inventory', { onShow });
})();
