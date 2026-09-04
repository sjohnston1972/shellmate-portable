/**
 * ansible_builder.js — Getting to a first playbook without writing YAML (#586).
 *
 * Two ways across, and the order on screen is the argument. Blocks come
 * first: a small vocabulary of network tasks assembled into correct YAML
 * locally, with no network, no key and no waiting, and the same output
 * every time. The assistant comes second, because it is better at the
 * awkward middle ground and worse in the way a language model is worse —
 * it writes something plausible and wrong with exactly as much confidence
 * as something right.
 *
 * Whatever produced the playbook, the same thing happens next: it is read
 * back and the screen says what it would do, task by task, with anything
 * that writes to a device marked. That panel is the whole safety story.
 * A draft nobody read is a draft nobody should run, and the only useful
 * response to that is to make the reading easy rather than to add a
 * warning nobody clicks through.
 */

(function () {
  'use strict';

  const view = window.ansibleView;
  if (!view) return;
  const { el, icon, clear } = view;

  /** The block vocabulary and platform list, fetched once. */
  let vocabulary = null;

  /** The blocks the user has added, in order. */
  let chosen = [];

  /** The current playbook text and what reading it back found. */
  let current = { text: '', found: null, source: '' };

  let nextId = 1;

  // -- What the assistant runs on ------------------------------------------

  /**
   * Which provider and model to ask.
   *
   * Taken from the chat panel's own selector rather than a second one
   * here. Two places to choose a model is two places for them to disagree,
   * and the answer to "which model wrote this?" should not depend on which
   * screen you were looking at.
   */
  function assistant() {
    const select = document.getElementById('ai-backend-select');
    const raw = (select && select.value) || '';
    const [backend, model] = raw.split(':');
    return { backend: backend || '', model: model || '',
             label: select && select.selectedOptions[0]
               ? select.selectedOptions[0].textContent.trim() : '' };
  }

  // -- The blocks ----------------------------------------------------------

  function addBlock(kind) {
    const meta = vocabulary.blocks[kind];
    if (!meta) return;
    chosen.push({ uid: nextId++, kind, label: meta.label, fields: {} });
    renderBlocks();
  }

  function moveBlock(uid, by) {
    const at = chosen.findIndex(b => b.uid === uid);
    const to = at + by;
    if (at < 0 || to < 0 || to >= chosen.length) return;
    [chosen[at], chosen[to]] = [chosen[to], chosen[at]];
    renderBlocks();
  }

  function blockCard(block) {
    const meta = vocabulary.blocks[block.kind] || { fields: [], why: '' };
    const fields = (meta.fields || []).map(field => {
      const id = `av-bld-${block.uid}-${field.name}`;
      const input = field.multiline
        ? el('textarea', {
            id, rows: 4, class: 'av-bld-input av-bld-mono',
            placeholder: field.placeholder || '',
            oninput: (e) => { block.fields[field.name] = e.target.value; },
          })
        : el('input', {
            id, type: 'text', class: 'av-bld-input',
            placeholder: field.placeholder || '',
            oninput: (e) => { block.fields[field.name] = e.target.value; },
          });
      input.value = block.fields[field.name] || '';
      return el('div', { class: 'av-bld-field' }, [
        el('label', { for: id, text: field.label }),
        input,
      ]);
    });

    return el('div', { class: `av-bld-block${meta.writes ? ' av-bld-writes' : ''}` }, [
      el('div', { class: 'av-bld-block-head' }, [
        icon(meta.writes ? 'error' : 'check_circle'),
        el('strong', { text: block.label }),
        meta.writes
          ? el('span', { class: 'av-pill av-pill-warn', text: 'changes the device' })
          : el('span', { class: 'av-pill av-pill-ok', text: 'read only' }),
        el('div', { class: 'av-row-actions av-bld-block-actions' }, [
          el('button', { type: 'button', class: 'icon-btn', title: 'Move up',
                         onclick: () => moveBlock(block.uid, -1) }, icon('upload')),
          el('button', { type: 'button', class: 'icon-btn', title: 'Move down',
                         onclick: () => moveBlock(block.uid, 1) }, icon('download')),
          el('button', { type: 'button', class: 'icon-btn', title: 'Remove',
                         onclick: () => {
                           chosen = chosen.filter(b => b.uid !== block.uid);
                           renderBlocks();
                         } }, icon('delete_forever')),
        ]),
      ]),
      el('p', { class: 'av-bld-why', text: meta.why || '' }),
      ...fields,
    ]);
  }

  function renderBlocks() {
    const host = document.getElementById('av-bld-blocks');
    if (!host) return;
    clear(host);
    if (!chosen.length) {
      host.appendChild(view.empty(
        'No tasks yet. Add one below — "Gather facts" is a safe first one.'));
      return;
    }
    chosen.forEach(block => host.appendChild(blockCard(block)));
  }

  // -- Reading the result back ---------------------------------------------

  /**
   * Say what the playbook would do.
   *
   * The list is per task and marks the ones that write, because "this
   * playbook changes things" is not useful and "task 3 pushes
   * configuration and task 4 saves it" is. An unrecognised module counts
   * as a write and is named: ShellMate not knowing a module is a reason
   * for the reader to look, not a reason to call it safe.
   */
  function renderFound() {
    const host = document.getElementById('av-bld-found');
    if (!host) return;
    clear(host);
    const found = current.found;
    if (!current.text) {
      host.appendChild(view.empty('Build or draft a playbook and it is read back here.'));
      return;
    }
    if (!found) return;

    if (current.source === 'assistant') {
      host.appendChild(el('div', { class: 'av-notice av-notice-warn' }, [
        icon('smart_toy'),
        el('div', {}, [
          el('strong', { text: 'This is a draft. ' }),
          'A model wrote it, so read every task before you keep it. It will '
          + 'write something plausible and wrong as readily as something '
          + 'right, and ShellMate cannot tell the difference either.',
        ]),
      ]));
    }

    const rows = (found.tasks || []).map((task, index) => el('tr', {}, [
      el('td', { class: 'av-when', text: String(index + 1) }),
      el('td', { text: task.name || task.module }),
      el('td', {}, el('code', { class: 'av-bld-module', text: task.module })),
      el('td', {}, task.writes
        ? el('span', { class: 'av-pill av-pill-warn', text: 'writes' })
        : el('span', { class: 'av-pill av-pill-ok', text: 'reads' })),
    ]));

    host.appendChild(el('table', { class: 'av-table' }, [
      el('thead', {}, el('tr', {}, [
        el('th', { text: '#' }), el('th', { text: 'Task' }),
        el('th', { text: 'Module' }), el('th', { text: '' }),
      ])),
      el('tbody', {}, rows.length ? rows : [
        el('tr', {}, el('td', { colspan: 4, text: 'No tasks were found in this text.' })),
      ]),
    ]));

    if ((found.unknown_modules || []).length) {
      host.appendChild(el('div', { class: 'av-notice av-notice-warn' }, [
        icon('info'),
        el('div', {}, [
          el('strong', { text: 'Modules ShellMate does not recognise: ' }),
          found.unknown_modules.join(', '),
          '. They are counted as changing the device, which may be wrong in '
          + 'either direction. Check them before this runs anywhere real.',
        ]),
      ]));
    }

    if (found.check_mode_note) {
      host.appendChild(el('div', { class: 'av-notice av-notice-info' }, [
        icon('info'), el('div', { text: found.check_mode_note }),
      ]));
    }
  }

  function renderText() {
    const box = document.getElementById('av-bld-text');
    if (box) box.value = current.text;
  }

  // -- Actions -------------------------------------------------------------

  function spec() {
    return {
      name: (document.getElementById('av-bld-name') || {}).value || '',
      hosts: (document.getElementById('av-bld-hosts') || {}).value || 'all',
      family: (document.getElementById('av-bld-family') || {}).value || 'generic',
      gather_facts: !!(document.getElementById('av-bld-gather') || {}).checked,
      blocks: chosen.map(b => ({ kind: b.kind, label: b.label, fields: b.fields })),
    };
  }

  async function buildFromBlocks() {
    try {
      const built = await view.post('/api/ansible/build', spec());
      current = { text: built.text, found: await inspectText(built.text),
                  source: 'blocks' };
      renderText();
      renderFound();
    } catch (e) {
      view.toast(e.message || String(e), 'error');
    }
  }

  function inspectText(text) {
    return view.post('/api/ansible/inspect', { text }).catch(() => null);
  }

  async function askAssistant() {
    const description = (document.getElementById('av-bld-ask') || {}).value || '';
    if (!description.trim()) {
      view.toast('Describe what the playbook should do first.', 'error');
      return;
    }
    const button = document.getElementById('av-bld-draft');
    const status = document.getElementById('av-bld-draft-status');
    const who = assistant();
    if (button) button.disabled = true;
    if (status) status.textContent = `Asking ${who.label || who.backend || 'the assistant'}…`;
    try {
      const drafted = await view.post('/api/ansible/draft', {
        description,
        hosts: (document.getElementById('av-bld-hosts') || {}).value || 'all',
        family: (document.getElementById('av-bld-family') || {}).value || 'generic',
        session_id: (document.getElementById('av-bld-context') || {}).checked
          ? ((window.getActiveTab && window.getActiveTab()) || {}).sessionId || ''
          : '',
        backend: who.backend, model: who.model,
      });
      current = { text: drafted.text, found: drafted, source: 'assistant' };
      renderText();
      renderFound();
      if (status) status.textContent = '';
    } catch (e) {
      if (status) status.textContent = '';
      view.toast(e.message || String(e), 'error');
    } finally {
      if (button) button.disabled = false;
    }
  }

  /** Re-read whatever is in the box, so hand edits are reflected. */
  async function reread() {
    const box = document.getElementById('av-bld-text');
    current.text = (box && box.value) || '';
    current.found = current.text ? await inspectText(current.text) : null;
    if (current.source === 'assistant' && box) current.source = 'edited';
    renderFound();
  }

  async function saveAsPlaybook() {
    const box = document.getElementById('av-bld-text');
    const text = (box && box.value) || '';
    if (!text.trim()) {
      view.toast('There is nothing to save yet.', 'error');
      return;
    }
    const name = await window.shellmateDialog.prompt({
      title: 'Save as a playbook',
      body: 'It goes into your own library. Sending it to the runner is a '
          + 'separate step, from Playbooks.',
      placeholder: 'ntp-servers.yml',
    });
    if (!name) return;
    try {
      // Through the build endpoint only when it came from blocks; edited or
      // drafted text is saved as it stands, because what was read back has
      // to be what gets stored. Saving a rebuild would file something
      // nobody inspected.
      await view.json(`/api/ansible/library/${encodeURIComponent(name)}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text }),
      });
      await view.load();
      view.toast(`Saved as ${name}.`);
      await window.shellmateDialog.alert({
        title: 'Saved',
        body: `${name} is in your library. Open Playbooks to send it to the `
            + 'runner and run it.',
      });
    } catch (e) {
      view.toast(e.message || String(e), 'error');
    }
  }

  // -- The screen ----------------------------------------------------------

  function form() {
    const families = (vocabulary.families || []).map(f =>
      el('option', { value: f.id, text: f.label }));

    const familySelect = el('select', { id: 'av-bld-family', class: 'av-bld-input' },
                            families);

    const adders = Object.entries(vocabulary.blocks || {}).map(([kind, meta]) =>
      el('button', {
        type: 'button', class: 'btn-secondary', title: meta.why,
        onclick: () => addBlock(kind),
      }, [icon('add'), meta.label]));

    return [
      el('section', { class: 'av-block' }, [
        el('h4', { class: 'av-block-title' }, 'The play'),
        el('div', { class: 'av-bld-row' }, [
          el('div', { class: 'av-bld-field' }, [
            el('label', { for: 'av-bld-name', text: 'What it is called' }),
            el('input', { id: 'av-bld-name', type: 'text', class: 'av-bld-input',
                          placeholder: 'Set NTP servers' }),
          ]),
          el('div', { class: 'av-bld-field' }, [
            el('label', { for: 'av-bld-hosts', text: 'Which hosts' }),
            el('input', { id: 'av-bld-hosts', type: 'text', class: 'av-bld-input',
                          value: 'all', placeholder: 'a group or pattern' }),
          ]),
          el('div', { class: 'av-bld-field' }, [
            el('label', { for: 'av-bld-family', text: 'Platform' }),
            familySelect,
          ]),
        ]),
        el('label', { class: 'av-bld-check' }, [
          el('input', { id: 'av-bld-gather', type: 'checkbox' }),
          " Let Ansible gather facts first (slower, and it is off because "
          + "network devices often do not answer the way it expects)",
        ]),
      ]),

      el('section', { class: 'av-block' }, [
        el('h4', { class: 'av-block-title' }, 'Tasks'),
        el('div', { id: 'av-bld-blocks', class: 'av-bld-blocks' }),
        el('div', { class: 'av-bld-adders' }, adders),
        el('div', { class: 'av-bld-actions' }, [
          el('button', { type: 'button', class: 'btn-primary',
                         onclick: buildFromBlocks }, [icon('build'), 'Build it']),
        ]),
      ]),

      el('section', { class: 'av-block' }, [
        el('h4', { class: 'av-block-title' }, 'Or describe it'),
        el('p', { class: 'av-bld-why' },
           'Ask the assistant for a first draft. It is good at the shape of a '
           + 'play and unreliable about the details, so what comes back is '
           + 'read back to you before you do anything with it.'),
        el('textarea', {
          id: 'av-bld-ask', rows: 3, class: 'av-bld-input',
          placeholder: 'Shut ports Gi1/0/10 to Gi1/0/20 on the access switches '
                     + 'and leave a description saying why',
        }),
        el('label', { class: 'av-bld-check' }, [
          el('input', { id: 'av-bld-context', type: 'checkbox' }),
          ' Include the active terminal session as context (it is redacted '
          + 'before it leaves this machine)',
        ]),
        el('div', { class: 'av-bld-actions' }, [
          el('button', { type: 'button', class: 'btn-secondary', id: 'av-bld-draft',
                         onclick: askAssistant }, [icon('smart_toy'), 'Draft it']),
          el('span', { id: 'av-bld-draft-status', class: 'av-bld-why' }),
        ]),
      ]),

      el('section', { class: 'av-block' }, [
        el('h4', { class: 'av-block-title' }, 'The playbook'),
        el('textarea', {
          id: 'av-bld-text', rows: 16, class: 'av-bld-input av-bld-mono',
          spellcheck: 'false',
          placeholder: 'Nothing built yet. Add a task above, or describe what '
                     + 'you want.',
          onchange: reread,
        }),
        el('div', { class: 'av-bld-actions' }, [
          el('button', { type: 'button', class: 'btn-tertiary', onclick: reread },
             [icon('refresh'), 'Read it back']),
          el('button', { type: 'button', class: 'btn-primary', onclick: saveAsPlaybook },
             [icon('save'), 'Save as a playbook']),
        ]),
      ]),

      el('section', { class: 'av-block' }, [
        el('h4', { class: 'av-block-title' }, 'What it would do'),
        el('div', { id: 'av-bld-found' }),
      ]),
    ];
  }

  async function render() {
    const body = document.getElementById('av-builder-body');
    if (!body) return;
    if (!vocabulary) {
      clear(body);
      body.appendChild(view.empty('Loading the blocks…'));
      try {
        vocabulary = await view.json('/api/ansible/builder');
      } catch (e) {
        clear(body);
        body.appendChild(view.empty(
          `The builder could not load its blocks: ${e.message || e}`));
        return;
      }
    }
    if (body.dataset.built === '1') return;   // keep what is on screen
    clear(body);
    form().forEach(node => body.appendChild(node));
    body.dataset.built = '1';
    renderBlocks();
    renderFound();
  }

  view.area('builder', { onShow: render });
})();
