/**
 * ansible_builder.js — A playbook, drawn as the thing it is (#586, #600).
 *
 * The builder used to be a flat list of blocks producing one play. Real
 * playbooks are nested — a playbook holds plays, a play holds tasks, tasks
 * notify handlers — and a flat list cannot express that at all. Worse, it
 * did not *look* like what it was building, so the structure had to be held
 * in somebody's head rather than on the screen.
 *
 * So it is a canvas of boxes inside boxes, matching the structure exactly.
 * Two rules follow from that and are worth stating, because they are what
 * make it work rather than merely look right:
 *
 * - **Each level owns its own add affordance.** "+ add task" sits inside
 *   the play it adds to; "+ add play" sits inside the playbook. Where you
 *   click is where the thing lands, so nothing has to be moved afterwards.
 * - **Inventory is a rail beside the canvas, not part of it.** Groups are
 *   managed elsewhere and *referenced* by plays, which is exactly Ansible's
 *   own relationship. Drawing them inside the playbook would say the
 *   playbook owns them, and then somebody would expect deleting a play to
 *   delete a group.
 *
 * A play names what it targets, on the play, because that is where Ansible
 * puts it — a playbook that configures switches and then checks a firewall
 * is two plays, and one global target cannot say so.
 *
 * What has not changed: the read-back panel underneath says what the
 * playbook would actually do, and anything the assistant drafts is a draft.
 * The canvas changes how a playbook is assembled, not what is claimed
 * about it.
 */

(function () {
  'use strict';

  const view = window.ansibleView;
  if (!view) return;
  const { el, icon, clear } = view;

  /** The block vocabulary and platform list, fetched once. */
  let vocabulary = null;

  /** Estate groups, for the inventory rail. */
  let estateGroups = [];

  /** The playbook being built: plays, each with tasks and handlers. */
  let plays = [];
  let uid = 1;

  /** The current text and what reading it back found. */
  let current = { text: '', found: null, source: '', error: '' };

  function newPlay(hosts) {
    return { uid: uid++, name: '', hosts: hosts || 'all',
             gather_facts: false, tasks: [], handlers: [] };
  }

  function playName(play) {
    return play.name || `Play ${plays.indexOf(play) + 1}`;
  }

  /** Which provider and model to ask, from the chat panel's own selector. */
  function assistant() {
    const select = document.getElementById('ai-backend-select');
    const raw = (select && select.value) || '';
    const [backend, model] = raw.split(':');
    return { backend: backend || '', model: model || '',
             label: select && select.selectedOptions[0]
               ? select.selectedOptions[0].textContent.trim() : '' };
  }

  // -- Editing --------------------------------------------------------------

  /**
   * The form for one task, built from the block's own declared fields.
   *
   * Handlers are offered as a list rather than typed, because a `notify`
   * that does not match a handler's name exactly is silently ignored by
   * Ansible: the play runs, nothing is restarted, and nothing anywhere
   * says why.
   */
  async function editTask(play, task, isHandler) {
    const meta = vocabulary.blocks[task.kind] || { fields: [], why: '' };
    const fields = [
      { name: 'label', label: 'What this step is called', required: true,
        value: task.label || meta.label, hint: meta.why },
      ...(meta.fields || []).map(f => ({
        name: f.name, label: f.label,
        type: f.multiline ? 'textarea' : 'text',
        value: task.fields[f.name] || '',
        placeholder: f.placeholder || '',
        required: !!f.required,
      })),
    ];

    if (!isHandler && play.handlers.length) {
      fields.push({
        name: 'notify', label: 'Notifies', type: 'select',
        value: (task.notify || [])[0] || '',
        options: [{ value: '', label: 'Nothing' }].concat(
          play.handlers.map(h => ({ value: h.label, label: h.label }))),
        hint: 'Handlers run once, at the end, and only if this task actually '
            + 'changed something.',
      });
    }

    const answer = await window.shellmateDialog.form({
      title: task.label ? `Edit "${task.label}"` : `New ${meta.label.toLowerCase()}`,
      body: meta.writes ? 'This step changes the device.'
                        : 'This step only reads from the device.',
      confirmLabel: 'Save',
      fields,
    });
    if (!answer) return false;

    task.label = (answer.label || '').trim() || meta.label;
    (meta.fields || []).forEach(f => { task.fields[f.name] = answer[f.name] || ''; });
    if (!isHandler) task.notify = answer.notify ? [answer.notify] : [];
    return true;
  }

  async function addTask(play, isHandler) {
    const kinds = Object.entries(vocabulary.blocks);
    const answer = await window.shellmateDialog.form({
      title: isHandler ? `New handler in "${playName(play)}"`
                       : `New task in "${playName(play)}"`,
      body: isHandler
        ? 'A handler runs at the end, once, and only if a task notified it.'
        : 'Tasks run in order, against whatever this play targets.',
      confirmLabel: 'Next',
      fields: [{
        name: 'kind', label: 'What it does', type: 'select',
        options: kinds.map(([key, meta]) => ({
          value: key,
          label: meta.writes ? `${meta.label} — changes the device`
                             : `${meta.label} — read only`,
        })),
      }],
    });
    if (!answer) return;

    const task = { uid: uid++, kind: answer.kind, label: '', fields: {}, notify: [] };
    if (await editTask(play, task, isHandler)) {
      (isHandler ? play.handlers : play.tasks).push(task);
      renderCanvas();
    }
  }

  async function editPlay(play) {
    const answer = await window.shellmateDialog.form({
      title: play.name ? `Edit "${play.name}"` : 'New play',
      body: 'A play targets one set of hosts. Configuring switches and then '
          + 'checking a firewall is two plays, because Ansible puts the '
          + 'target on the play rather than on the file.',
      confirmLabel: 'Save',
      fields: [
        { name: 'name', label: 'What this play is called', value: play.name,
          placeholder: 'Configure the access switches' },
        { name: 'hosts', label: 'Targets', value: play.hosts, placeholder: 'all',
          hint: estateGroups.length
            ? `Groups on the left, or an Ansible pattern. Yours: `
              + estateGroups.slice(0, 6).join(', ')
            : 'A group name, or an Ansible host pattern.' },
        { name: 'gather_facts', label: 'Gather facts first', type: 'checkbox',
          value: !!play.gather_facts,
          hint: 'Off by default: network devices often do not answer the way '
              + 'fact gathering expects, and it is slow.' },
      ],
    });
    if (!answer) return false;
    play.name = (answer.name || '').trim();
    play.hosts = (answer.hosts || '').trim() || 'all';
    play.gather_facts = !!answer.gather_facts;
    return true;
  }

  // -- Drawing --------------------------------------------------------------

  function taskCard(play, task, isHandler) {
    const meta = vocabulary.blocks[task.kind] || {};
    const number = isHandler ? 'Handler' : `Task ${play.tasks.indexOf(task) + 1}`;
    const notifies = (task.notify || []).filter(Boolean);

    return el('div', {
      class: `av-node av-node-task${meta.writes ? ' av-node-writes' : ''}`,
      onclick: async (event) => {
        if (event.target.closest('[data-remove]')) return;
        if (await editTask(play, task, isHandler)) renderCanvas();
      },
    }, [
      el('div', { class: 'av-node-row' }, [
        el('span', { class: 'av-node-title' },
           `${number} · ${task.label || meta.label || task.kind}`),
        meta.writes
          ? el('span', { class: 'av-node-mark av-node-mark-writes',
                         title: 'Changes the device' }, icon('error'))
          : el('span', { class: 'av-node-mark av-node-mark-reads',
                         title: 'Reads only' }, icon('check_circle')),
        el('button', {
          type: 'button', class: 'icon-btn av-node-remove', 'data-remove': 'true',
          title: 'Remove this step',
          onclick: () => {
            const list = isHandler ? play.handlers : play.tasks;
            list.splice(list.indexOf(task), 1);
            renderCanvas();
          },
        }, icon('delete_forever')),
      ]),
      notifies.length
        ? el('div', { class: 'av-node-sub' }, `↳ notifies → ${notifies.join(', ')}`)
        : null,
    ]);
  }

  function addButton(label, onclick) {
    return el('button', { type: 'button', class: 'av-add', onclick }, label);
  }

  function playCard(play, index) {
    const children = play.tasks.map(t => taskCard(play, t, false));
    children.push(addButton('+ add task', () => addTask(play, false)));

    if (play.handlers.length) {
      children.push(el('div', { class: 'av-node-label', text: 'Handlers' }));
      play.handlers.forEach(h => children.push(taskCard(play, h, true)));
    }
    children.push(addButton('+ add handler', () => addTask(play, true)));

    return el('div', { class: 'av-node av-node-play' }, [
      el('div', { class: 'av-node-row av-node-head' }, [
        el('button', {
          type: 'button', class: 'av-node-title av-node-button',
          title: 'Rename this play, or change what it targets',
          onclick: async () => { if (await editPlay(play)) renderCanvas(); },
        }, play.name || `Play ${index + 1}`),
        el('span', { class: 'av-node-targets' }, `targets: ${play.hosts}`),
        el('button', {
          type: 'button', class: 'icon-btn av-node-remove', title: 'Remove this play',
          onclick: () => { plays.splice(plays.indexOf(play), 1); renderCanvas(); },
        }, icon('delete_forever')),
      ]),
      ...children,
    ]);
  }

  function renderCanvas() {
    const host = document.getElementById('av-bld-canvas');
    if (!host) return;
    clear(host);

    const children = plays.map((play, i) => playCard(play, i));
    children.push(addButton('+ add play', async () => {
      const play = newPlay(estateGroups[0] || 'all');
      if (await editPlay(play)) { plays.push(play); renderCanvas(); }
    }));

    host.appendChild(el('div', { class: 'av-node av-node-playbook' }, [
      el('div', { class: 'av-node-row av-node-head' }, [
        el('span', { class: 'av-node-title av-node-playbook-title' }, 'PLAYBOOK'),
        el('button', {
          type: 'button', class: 'av-run', onclick: buildAndRun,
          disabled: !plays.length,
          title: plays.length ? 'Save it and go to Playbooks to run it'
                              : 'Add a play first',
        }, [icon('bolt'), 'Run']),
      ]),
      ...children,
    ]));

    // Rebuilt on every structural change, because a read-back describing a
    // playbook that has since been edited is worse than none: it is read
    // and believed.
    if (plays.length) refreshText();
    else { current = { text: '', found: null, source: '', error: '' }; paint(); }
  }

  function rail() {
    const host = document.getElementById('av-bld-rail');
    if (!host) return;
    clear(host);
    host.appendChild(el('h4', { class: 'av-rail-title', text: 'Inventory' }));

    if (!estateGroups.length) {
      host.appendChild(el('p', { class: 'av-rail-note' },
        'No groups yet. A play can still target a host pattern.'));
    }
    estateGroups.forEach(name => {
      host.appendChild(el('button', {
        type: 'button', class: 'av-rail-chip',
        title: `Add a play targeting ${name}`,
        onclick: () => {
          const play = newPlay(name);
          play.name = `Configure ${name}`;
          plays.push(play);
          renderCanvas();
        },
      }, name));
    });

    host.appendChild(el('button', {
      type: 'button', class: 'av-rail-add',
      title: 'Groups are managed on the dashboard, with the connections',
      onclick: () => view.close(),
    }, '+ group'));

    host.appendChild(el('p', { class: 'av-rail-note' },
      'Managed separately, referenced by plays.'));
  }

  // -- The playbook ---------------------------------------------------------

  function strip(task) {
    return { kind: task.kind, label: task.label, fields: task.fields,
             notify: task.notify || [] };
  }

  function spec() {
    return {
      family: (document.getElementById('av-bld-family') || {}).value || 'generic',
      plays: plays.map(p => ({
        name: playName(p), hosts: p.hosts, gather_facts: p.gather_facts,
        tasks: p.tasks.map(strip), handlers: p.handlers.map(strip),
      })),
    };
  }

  function inspectText(text) {
    return view.post('/api/ansible/inspect', { text }).catch(() => null);
  }

  /**
   * Rebuild the text from the canvas.
   *
   * Sequenced, because two of these overlap constantly: adding a play
   * starts one that fails ("no tasks in it"), and adding the first task
   * starts another that succeeds — and the failing one, being older and
   * slower, was landing last and overwriting the good result. The canvas
   * then showed a correct playbook and an error about it at the same time.
   *
   * A counter rather than cancellation: the request is cheap and local,
   * and what matters is only that a stale answer cannot win.
   */
  let refreshSeq = 0;

  async function refreshText() {
    const mine = ++refreshSeq;
    let next;
    try {
      const built = await view.post('/api/ansible/build', spec());
      next = { text: built.text, found: await inspectText(built.text),
               source: 'blocks', error: '' };
    } catch (e) {
      // A half-finished play is the normal state of a canvas being built,
      // so this is shown where the YAML goes rather than raised as a dialog
      // to dismiss after every edit.
      next = { text: '', found: null, source: '', error: String(e.message || e) };
    }
    if (mine !== refreshSeq) return;
    current = next;
    paint();
  }

  async function buildAndRun() {
    await refreshText();
    if (!current.text) {
      view.toast(current.error || 'Nothing to run yet.', 'error');
      return;
    }
    const name = await window.shellmateDialog.prompt({
      title: 'Save and run',
      body: 'It is saved to your library first — the runner can only run a '
          + 'playbook it holds, and sending it there is a separate step from '
          + 'Playbooks.',
      placeholder: 'vlan-change.yml',
    });
    if (!name) return;
    try {
      await view.json(`/api/ansible/library/${encodeURIComponent(name)}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: current.text }),
      });
      await view.load();
      view.show('playbooks');
    } catch (e) {
      view.toast(e.message || String(e), 'error');
    }
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
      placeholder: 'vlan-change.yml',
    });
    if (!name) return;
    try {
      await view.json(`/api/ansible/library/${encodeURIComponent(name)}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text }),
      });
      await view.load();
      await window.shellmateDialog.alert({
        title: 'Saved',
        body: `${name} is in your library. Open Playbooks to send it to the `
            + 'runner and run it.',
      });
    } catch (e) {
      view.toast(e.message || String(e), 'error');
    }
  }

  // -- The read-back, unchanged in what it claims ---------------------------

  function paint() {
    const box = document.getElementById('av-bld-text');
    if (box) box.value = current.text;
    renderFound();
  }

  function renderFound() {
    const host = document.getElementById('av-bld-found');
    if (!host) return;
    clear(host);
    const found = current.found;

    if (current.error) {
      host.appendChild(el('div', { class: 'av-notice av-notice-warn' }, [
        icon('info'), el('div', { text: current.error }),
      ]));
      return;
    }
    if (!current.text) {
      host.appendChild(view.empty('Add a play and it is read back here.'));
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

  // -- The assistant --------------------------------------------------------

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
        hosts: (plays[0] && plays[0].hosts) || 'all',
        family: (document.getElementById('av-bld-family') || {}).value || 'generic',
        session_id: (document.getElementById('av-bld-context') || {}).checked
          ? ((window.getActiveTab && window.getActiveTab()) || {}).sessionId || ''
          : '',
        backend: who.backend, model: who.model,
      });
      // A draft arrives as text and stays as text. Reading YAML back into
      // canvas nodes would be a parser guessing at intent, and a wrong
      // guess would silently rewrite what the model produced — so the
      // canvas keeps what you built and the draft sits in the editor.
      current = { text: drafted.text, found: drafted, source: 'assistant', error: '' };
      paint();
      if (status) status.textContent = '';
    } catch (e) {
      if (status) status.textContent = '';
      view.toast(e.message || String(e), 'error');
    } finally {
      if (button) button.disabled = false;
    }
  }

  async function reread() {
    const box = document.getElementById('av-bld-text');
    current.text = (box && box.value) || '';
    current.error = '';
    current.found = current.text ? await inspectText(current.text) : null;
    if (current.source === 'assistant') current.source = 'edited';
    renderFound();
  }

  // -- The screen -----------------------------------------------------------

  function layout() {
    const families = (vocabulary.families || []).map(f =>
      el('option', { value: f.id, text: f.label }));

    return [
      el('div', { class: 'av-bld-shell' }, [
        el('aside', { id: 'av-bld-rail', class: 'av-rail' }),
        el('div', { class: 'av-bld-main' }, [
          el('div', { class: 'av-bld-row' }, [
            el('div', { class: 'av-bld-field' }, [
              el('label', { for: 'av-bld-family', text: 'Platform' }),
              el('select', {
                id: 'av-bld-family', class: 'av-bld-input',
                onchange: () => { if (plays.length) refreshText(); },
              }, families),
            ]),
          ]),
          el('div', { id: 'av-bld-canvas' }),
        ]),
      ]),

      el('section', { class: 'av-block' }, [
        el('h4', { class: 'av-block-title' }, 'Or describe it'),
        el('p', { class: 'av-bld-why' },
           'Ask the assistant for a first draft. It is good at the shape of a '
           + 'play and unreliable about the details, so what comes back is '
           + 'read back to you before you do anything with it, and it lands '
           + 'in the editor rather than in the canvas.'),
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
          id: 'av-bld-text', rows: 14, class: 'av-bld-input av-bld-mono',
          spellcheck: 'false',
          placeholder: 'Nothing built yet. Add a play above, or describe what '
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

  async function loadGroups() {
    try {
      const data = await view.json('/api/ansible/inventory');
      estateGroups = Object.keys((data && data.groups) || {}).sort();
    } catch (e) {
      estateGroups = [];
    }
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
      await loadGroups();
    }
    if (body.dataset.built === '1') return;   // keep what is on screen
    clear(body);
    layout().forEach(node => body.appendChild(node));
    body.dataset.built = '1';
    rail();
    renderCanvas();
  }

  view.area('builder', { onShow: render });
})();
