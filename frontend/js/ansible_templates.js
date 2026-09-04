/**
 * ansible_templates.js — Parameterised plays: the holes, the form that fills them, and what comes out (#586).
 *
 * A template is a play with named `{{ holes }}` and a description of each
 * one. The area has three faces, one `mode` at a time:
 *
 * - **list** — what you hold, with the one fact that decides whether a
 *   template is safe to try without reading the YAML first: does it write
 *   to a device, or only look?
 * - **edit** — the body and the variable descriptions that produce that
 *   form. `backend.ansible_library.save_template` refuses a `{{ hole }}`
 *   the variables do not describe, and that refusal names the variable —
 *   so the "detected holes" helper exists to make the fix one click rather
 *   than a re-read of the body to find what changed.
 * - **fill** — the form built from `variables`, a preview rendered by the
 *   server (never client-side: substitution here is deliberately not
 *   Jinja, and the server is the one place that rule is enforced), and
 *   saving the result as a playbook.
 *
 * Typing into a field never triggers a full re-render — `render()` tears
 * the body down and rebuilds it, which would drop focus and the cursor
 * position mid-word. Only a structural change (add/remove a row, switch
 * mode, an explicit Preview or Save) calls it.
 */

(function () {
  'use strict';

  const view = window.ansibleView;
  if (!view) return;
  const { el, icon, clear } = view;

  /** Same pattern the backend checks a body against (`ansible_library._PLACEHOLDER_RE`). */
  const HOLE_RE = /{{\s*([a-z_][a-z0-9_]*)\s*(?:\|[^}]*)?}}/g;

  let mode = 'list';        // 'list' | 'edit' | 'fill'
  let draft = null;         // the template being created/edited
  let bodyError = '';       // the server's refusal, shown by the body rather than as a toast
  let fillTarget = null;    // the template being filled in
  let fillValues = {};
  let previewText = '';
  let previewError = '';
  let savedMessage = '';    // "Saved as X." after a successful save-as-playbook

  function root() {
    return document.getElementById('av-templates-body');
  }

  function render(state) {
    const body = root();
    if (!body) return;
    clear(body);
    if (mode === 'edit') body.appendChild(renderEditor());
    else if (mode === 'fill') body.appendChild(renderFill());
    else body.appendChild(renderList(state));
  }

  // -- A field, the shape every form on this area uses ---------------------

  function field(labelText, control, hint, required) {
    return el('div', { class: 'form-group' }, [
      el('label', {}, [labelText, required ? el('span', { class: 'required', text: ' *' }) : null]),
      control,
      hint ? el('p', { class: 'field-hint', text: hint }) : null,
    ]);
  }

  function checkField(labelText, checked, onChange, hint) {
    const row = el('label', { class: 'av-tpl-check' }, [
      el('input', { type: 'checkbox', checked, onchange: e => onChange(e.target.checked) }),
      el('span', { text: labelText }),
    ]);
    return hint ? el('div', { class: 'form-group' }, [row, el('p', { class: 'field-hint', text: hint })]) : row;
  }

  function backButton(label) {
    return el('button', {
      type: 'button', class: 'btn-tertiary',
      onclick: () => { mode = 'list'; render(view.state); },
    }, [icon('cancel'), label || 'Back to templates']);
  }

  // -- List ------------------------------------------------------------------

  function renderList(state) {
    const templates = ((state.library || {}).templates) || [];
    const wrap = el('div', {});

    wrap.appendChild(el('div', { class: 'av-tpl-toolbar' }, [
      el('button', { type: 'button', class: 'btn-primary', onclick: startCreate }, [icon('add'), 'New template']),
    ]));

    if (!templates.length) {
      wrap.appendChild(view.empty(
        'A template is a play with named holes in it — fill them in and it '
        + 'becomes a playbook you can run, and keep. Nothing saved yet.',
        el('button', { type: 'button', class: 'btn-primary', onclick: startCreate },
           [icon('add'), 'New template'])));
      return wrap;
    }

    const grid = el('div', { class: 'av-grid' });
    templates.forEach(t => grid.appendChild(renderCard(t)));
    wrap.appendChild(grid);
    return wrap;
  }

  function renderCard(t) {
    const count = (t.variables || []).length;
    const writes = t.writes !== false;
    return el('div', { class: 'av-card av-tpl-card' }, [
      el('div', { class: 'av-tpl-card-head' }, [
        el('h4', { text: t.name }),
        writes
          ? el('span', { class: 'av-pill av-pill-warn av-tpl-writes', title: 'Sends configuration to a device' },
                [icon('edit'), 'Writes'])
          : el('span', { class: 'av-pill av-pill-ok av-tpl-writes', title: 'Only reads — safe to try' },
                [icon('check_circle'), 'Read-only']),
      ]),
      t.description ? el('p', { text: t.description }) : null,
      el('div', { class: 'av-tpl-meta' }, [
        t.platform ? el('span', { class: 'av-pill av-pill-unknown', text: t.platform }) : null,
        el('span', { text: `${count} variable${count === 1 ? '' : 's'}` }),
      ]),
      el('div', { class: 'av-row-actions av-tpl-card-actions' }, [
        el('button', { type: 'button', class: 'btn-primary', onclick: () => startFill(t) },
           [icon('description'), 'Fill in']),
        el('button', { type: 'button', class: 'icon-btn', title: 'Edit template', onclick: () => startEdit(t) },
           icon('edit')),
        el('button', { type: 'button', class: 'icon-btn', title: 'Delete template', onclick: () => removeTemplate(t) },
           icon('delete_forever')),
      ]),
    ]);
  }

  async function removeTemplate(t) {
    const ok = await window.shellmateDialog.confirm({
      title: `Delete ${t.name}?`,
      body: 'The template is gone. Any playbook already saved from it stays exactly as it was rendered.',
      confirmLabel: 'Delete',
      danger: true,
    });
    if (!ok) return;
    try {
      await view.del(`/api/ansible/templates/${encodeURIComponent(t.id)}`);
      await view.load();
    } catch (e) {
      view.toast(e.message || String(e), 'error');
    }
    render(view.state);
  }

  // -- Create / edit -----------------------------------------------------

  function blankVariable(name) {
    return { name: name || '', label: name ? name.replace(/_/g, ' ') : '',
             help: '', default: '', required: true, choicesText: '' };
  }

  function startCreate() {
    draft = { id: '', name: '', description: '', platform: '', writes: true, body: '', variables: [] };
    bodyError = '';
    mode = 'edit';
    render(view.state);
  }

  function startEdit(t) {
    draft = {
      id: t.id, name: t.name || '', description: t.description || '',
      platform: t.platform || '', writes: t.writes !== false, body: t.body || '',
      variables: (t.variables || []).map(v => ({
        name: v.name || '', label: v.label || '', help: v.help || '',
        default: v.default === undefined || v.default === null ? '' : String(v.default),
        required: v.required !== false,
        choicesText: (v.choices || []).join(', '),
      })),
    };
    bodyError = '';
    mode = 'edit';
    render(view.state);
  }

  /** Every `{{ name }}` the body asks for, in the order it first appears. */
  function holesIn(body) {
    const found = [];
    const seen = new Set();
    let m;
    HOLE_RE.lastIndex = 0;
    while ((m = HOLE_RE.exec(body || '')) !== null) {
      if (!seen.has(m[1])) { seen.add(m[1]); found.push(m[1]); }
    }
    return found;
  }

  function renderEditor() {
    const isNew = !draft.id;
    const wrap = el('div', { class: 'av-tpl-form' });

    wrap.appendChild(el('div', { class: 'av-tpl-panel-head' }, [backButton()]));

    wrap.appendChild(field('Name', el('input', {
      type: 'text', value: draft.name, placeholder: 'Set interface description',
      oninput: e => { draft.name = e.target.value; },
    }), null, true));

    wrap.appendChild(field('Description', el('input', {
      type: 'text', value: draft.description, placeholder: 'What this play does, in one line',
      oninput: e => { draft.description = e.target.value; },
    })));

    wrap.appendChild(field('Platform', el('input', {
      type: 'text', value: draft.platform, placeholder: 'ios, nxos, asa — optional',
      oninput: e => { draft.platform = e.target.value; },
    }), 'Left blank, the template offers itself for every platform.'));

    wrap.appendChild(checkField('Writes to devices', draft.writes, checked => { draft.writes = checked; },
      'Off marks a template read-only — the mark somebody deciding whether to '
      + 'try it can trust without reading the YAML first.'));

    const bodyGroup = el('div', { class: 'form-group' }, [
      el('label', { class: 'av-tpl-body-label' }, [icon('code'), ' Body (YAML)']),
      el('textarea', {
        class: 'av-tpl-body-input', rows: 14, spellcheck: 'false',
        oninput: e => { draft.body = e.target.value; },
      }, draft.body),
      el('p', { class: 'field-hint', text: '{{ variable_name }} marks a hole. '
        + 'Every hole needs a variable below, or saving is refused.' }),
    ]);
    if (bodyError) {
      bodyGroup.appendChild(el('div', { class: 'av-notice av-notice-bad av-tpl-body-error' },
        [icon('error'), el('div', { text: bodyError })]));
    }
    wrap.appendChild(bodyGroup);

    wrap.appendChild(renderVariables());

    wrap.appendChild(el('div', { class: 'av-tpl-form-actions' }, [
      el('button', { type: 'button', class: 'btn-primary', onclick: saveDraft },
         [icon('save'), isNew ? 'Create template' : 'Save template']),
      backButton('Cancel'),
    ]));

    return wrap;
  }

  function renderVariables() {
    const wrap = el('div', { class: 'av-tpl-vars' });
    wrap.appendChild(el('div', { class: 'av-tpl-vars-head' }, [
      el('h4', { class: 'av-block-title', text: 'Variables' }),
      el('button', { type: 'button', class: 'btn-tertiary', onclick: () => {
        draft.variables.push(blankVariable(''));
        render(view.state);
      } }, [icon('add'), 'Add variable']),
    ]));

    const described = new Set(draft.variables.map(v => v.name).filter(Boolean));
    const undescribed = holesIn(draft.body).filter(name => !described.has(name));
    if (undescribed.length) {
      wrap.appendChild(el('div', { class: 'av-notice av-notice-info' }, [
        icon('info'),
        el('div', {}, [
          el('strong', { text: undescribed.join(', ') + ' ' }),
          `${undescribed.length === 1 ? 'is a hole' : 'are holes'} in the body `
          + `nothing describes yet.`,
        ]),
        el('button', { type: 'button', class: 'btn-secondary', onclick: () => {
          undescribed.forEach(name => draft.variables.push(blankVariable(name)));
          render(view.state);
        } }, [icon('science'), `Add row${undescribed.length === 1 ? '' : 's'} for `
             + `${undescribed.length === 1 ? 'it' : 'them'}`]),
      ]));
    }

    if (!draft.variables.length) {
      wrap.appendChild(el('p', { class: 'field-hint',
        text: 'No variables yet — the play runs the same every time until one is added.' }));
    } else {
      draft.variables.forEach((v, index) => wrap.appendChild(renderVariableRow(v, index)));
    }
    return wrap;
  }

  function renderVariableRow(v, index) {
    const row = el('div', { class: 'av-tpl-var-row' });
    row.appendChild(el('div', { class: 'av-tpl-var-grid' }, [
      field('Variable name', el('input', {
        type: 'text', value: v.name, placeholder: 'interface_name',
        oninput: e => { v.name = e.target.value.trim(); },
      }), null, true),
      field('Label', el('input', {
        type: 'text', value: v.label, placeholder: 'Interface name',
        oninput: e => { v.label = e.target.value; },
      })),
      field('Default', el('input', {
        type: 'text', value: v.default,
        oninput: e => { v.default = e.target.value; },
      })),
      field('Choices', el('input', {
        type: 'text', value: v.choicesText, placeholder: 'comma-separated — optional',
        oninput: e => { v.choicesText = e.target.value; },
      })),
    ]));
    row.appendChild(field('Help text', el('input', {
      type: 'text', value: v.help, placeholder: 'Shown under the field when this is filled in',
      oninput: e => { v.help = e.target.value; },
    })));
    row.appendChild(el('div', { class: 'av-tpl-var-row-actions' }, [
      checkField('Required', v.required, checked => { v.required = checked; }),
      el('button', { type: 'button', class: 'icon-btn', title: 'Remove variable', onclick: () => {
        draft.variables.splice(index, 1);
        render(view.state);
      } }, icon('delete_forever')),
    ]));
    return row;
  }

  async function saveDraft() {
    const name = draft.name.trim();
    if (!name) { view.toast('A template needs a name.', 'error'); return; }

    const variables = draft.variables.map(v => ({
      name: v.name.trim(),
      label: v.label.trim(),
      help: v.help.trim(),
      default: v.default,
      required: !!v.required,
      choices: (v.choicesText || '').split(',').map(s => s.trim()).filter(Boolean),
    }));

    const payload = {
      id: draft.id, name, description: draft.description.trim(), body: draft.body,
      variables, platform: draft.platform.trim(), writes: !!draft.writes,
    };

    try {
      await view.post('/api/ansible/templates', payload);
      bodyError = '';
      await view.load();
      mode = 'list';
    } catch (e) {
      // The single most likely failure — a hole nothing describes — names
      // the variable, so it belongs by the body rather than as a toast that
      // has already scrolled off by the time somebody looks back at it.
      bodyError = e.message || String(e);
    }
    render(view.state);
  }

  // -- Fill in -------------------------------------------------------------

  function startFill(t) {
    fillTarget = t;
    fillValues = {};
    (t.variables || []).forEach(v => {
      let value = v.default === undefined || v.default === null ? '' : String(v.default);
      const choices = v.choices || [];
      if (choices.length && !choices.includes(value)) {
        value = v.required ? (choices[0] || '') : '';
      }
      fillValues[v.name] = value;
    });
    previewText = '';
    previewError = '';
    savedMessage = '';
    mode = 'fill';
    render(view.state);
  }

  function renderFill() {
    const t = fillTarget;
    const wrap = el('div', { class: 'av-tpl-form av-tpl-fill' });

    wrap.appendChild(el('div', { class: 'av-tpl-panel-head' }, [backButton()]));
    wrap.appendChild(el('h4', { class: 'av-block-title', text: t.name }));
    if (t.description) wrap.appendChild(el('p', { class: 'field-hint', text: t.description }));

    (t.variables || []).forEach(v => {
      const choices = v.choices || [];
      let control;
      if (choices.length) {
        control = el('select', { onchange: e => { fillValues[v.name] = e.target.value; } }, [
          !v.required ? el('option', { value: '', text: '— none —',
            selected: !fillValues[v.name] }) : null,
          ...choices.map(c => el('option', { value: c, text: c, selected: fillValues[v.name] === c })),
        ]);
      } else {
        control = el('input', {
          type: 'text', value: fillValues[v.name] || '',
          placeholder: v.default ? String(v.default) : '',
          oninput: e => { fillValues[v.name] = e.target.value; },
        });
      }
      wrap.appendChild(field(v.label || v.name, control, v.help, v.required));
    });

    wrap.appendChild(el('div', { class: 'av-tpl-form-actions' }, [
      el('button', { type: 'button', class: 'btn-secondary', onclick: doPreview },
         [icon('visibility'), 'Preview']),
      el('button', { type: 'button', class: 'btn-primary', onclick: doSaveAs },
         [icon('save'), 'Save as playbook']),
    ]));

    if (savedMessage) {
      wrap.appendChild(el('div', { class: 'av-tpl-notice-ok' }, [
        icon('check_circle'), el('div', { text: savedMessage }),
      ]));
    }

    const preview = el('div', { class: 'av-tpl-preview' });
    if (previewError) {
      preview.appendChild(el('div', { class: 'av-notice av-notice-bad' },
        [icon('error'), el('div', { text: previewError })]));
    } else if (previewText) {
      preview.appendChild(el('pre', { class: 'av-tpl-preview-yaml', text: previewText }));
    } else {
      preview.appendChild(el('p', { class: 'field-hint',
        text: 'Preview to see the rendered play before it is kept as a playbook.' }));
    }
    wrap.appendChild(preview);

    return wrap;
  }

  async function doPreview() {
    try {
      const result = await view.post(
        `/api/ansible/templates/${encodeURIComponent(fillTarget.id)}/render`, { values: fillValues });
      previewText = result.text || '';
      previewError = '';
    } catch (e) {
      previewError = e.message || String(e);
      previewText = '';
    }
    savedMessage = '';
    render(view.state);
  }

  async function doSaveAs() {
    const name = await window.shellmateDialog.prompt({
      title: 'Save as playbook',
      label: 'Playbook name',
      value: fillTarget.name,
    });
    if (name === null) return;
    if (!name.trim()) { view.toast('A playbook needs a name.', 'error'); return; }

    try {
      const result = await view.post(
        `/api/ansible/templates/${encodeURIComponent(fillTarget.id)}/render`,
        { values: fillValues, save_as: name.trim() });
      previewText = result.text || previewText;
      previewError = '';
      savedMessage = `Saved as "${(result.saved || {}).name || name.trim()}." Find it under Playbooks.`;
    } catch (e) {
      previewError = e.message || String(e);
      savedMessage = '';
    }
    render(view.state);
  }

  // -- Registration ----------------------------------------------------------

  view.area('templates', {
    onShow: (state, changed) => {
      // Coming from another area starts fresh; a refresh click while
      // already here (changed === false) must not throw away an
      // in-progress edit or fill.
      if (changed) mode = 'list';
      render(state);
    },
    onData: (state) => {
      if (view.current === 'templates' && mode === 'list') render(state);
    },
  });
})();
