/**
 * ansible_deployments.js — Infrastructure from a definition: sites, a scheme, a plan, then an apply.
 *
 * The cloud accounts hold zero hosts. Meraki is managed by calling its API
 * with ids, not by connecting to devices. So this area is not an inventory:
 * it is five hundred sites as a data set, a scheme, and two playbooks the
 * runner owns — committed to the repository in one commit, sent to the
 * runner, planned, and only then applied.
 *
 * Three things the interface enforces rather than suggests:
 *
 * **Plan before apply, always.** Meraki has no check mode — none of its
 * network modules declare it — so a `--check` run skips every task and
 * reports success. The plan playbook is the only preview there is. Apply
 * is not offered until a plan's result has been fetched and rendered here,
 * and the server refuses it anyway; the button follows the server's word
 * (`apply_blocked`) rather than keeping its own.
 *
 * **The columns are asked for, never guessed.** A site list is uploaded,
 * previewed, and the columns nominated — the same rule as inventories.
 *
 * **The ones that need reading come first.** In a plan table conflicts,
 * then creates; in an outcome table failures, then creates. Five hundred
 * rows that say "unchanged" are scrolled past, and the two that matter
 * must not be under them.
 */
(function () {
  'use strict';
  const view = window.ansibleView;
  if (!view) return;
  const { el, icon, clear, toast } = view;

  /** The deployments, fetched by this area rather than from the shared cache. */
  let list = [];
  let providers = [];
  /** The deployment open in the detail view, or null for the list. */
  let open = null;
  /** The last fetched plan/apply results, keyed by deployment id. */
  const results = {};
  /** Polling handles, one per running job. */
  const polls = {};

  // Per provider, not symmetric. Meraki's org id is a playbook variable;
  // Azure's subscription is read from the environment by the collection
  // and the resource group is per site from the scheme's rg_prefix.
  const SCOPE_FIELDS = {
    meraki: [{ name: 'meraki_org_id', label: 'Meraki organisation id', placeholder: '923103' }],
    azure:  [{ name: 'azure_subscription_id', label: 'Azure subscription id (sent as AZURE_SUBSCRIPTION_ID)' }],
    aws:    [{ name: 'aws_region', label: 'AWS region', placeholder: 'us-east-1' }],
  };

  /**
   * The scheme, as a form, per provider — the keys the runner's kit reads,
   * with the runner's own meaning and default as the hint.
   *
   * `kind` says how a field is typed and how it is written to the scheme:
   * text, csv (a list of strings), lines (a list of objects from
   * "a, b" rows, with `cols` naming the parts), or kv (a dict from
   * "key = value" lines). A provider absent here gets the JSON editor.
   */
  const SCHEME_FIELDS = {
    meraki: [
      { name: 'product_types', label: 'Product types (comma-separated, required)', kind: 'csv',
        required: true, dflt: ['appliance'], placeholder: 'appliance, switch' },
      { name: 'manage_prefix', label: 'Manage prefix (required)', kind: 'text', required: true,
        placeholder: 'deploy-',
        hint: 'Only sites whose name starts with this are configured beyond creation. '
            + 'It is what stops a deployment reaching into a network it did not create.' },
      { name: 'vlan_subnet_base', label: 'VLAN subnet base (optional)', kind: 'text', placeholder: '10.10' },
      { name: 'vlan_plan', label: 'VLAN plan — one per line: id, name, offset (optional)',
        kind: 'lines', cols: ['id:int', 'name', 'offset:int'], placeholder: '10, data, 0\n20, voice, 1' },
    ],
    azure: [
      { name: 'location', label: 'Location (required)', kind: 'text', required: true, placeholder: 'uksouth' },
      { name: 'rg_prefix', label: 'Resource group prefix (required)', kind: 'text', required: true, placeholder: 'rg-' },
      { name: 'vnet_prefix', label: 'VNet prefix (required)', kind: 'text', required: true, placeholder: 'vnet-' },
      { name: 'base_prefix', label: 'Base prefix (required) — vnet is <base>.<third octet>.0/24', kind: 'text',
        required: true, placeholder: '10.20' },
      { name: 'manage_prefix', label: 'Manage prefix (required)', kind: 'text', required: true,
        placeholder: 'azure-test-',
        hint: 'Only sites whose name starts with this are configured beyond the resource group.' },
      { name: 'subnets', label: 'Subnets — one per line: name, block (each a /26 at block×64; optional)',
        kind: 'lines', cols: ['name', 'block:int'], placeholder: 'data, 0\nvoice, 1' },
      { name: 'nsg_suffix', label: 'NSG name prefix (optional, default nsg-)', kind: 'text', placeholder: 'nsg-' },
      { name: 'tags', label: 'Tags — one per line: key = value (optional)', kind: 'kv', placeholder: 'owner = netops' },
    ],
  };

  // ---------------------------------------------------------------- data

  async function refresh() {
    try {
      const data = await view.json('/api/deployments');
      list = data.deployments || [];
      providers = data.providers || [];
    } catch (e) {
      list = [];
      toast(`Could not load deployments: ${e.message || e}`, 'error');
    }
    if (open) {
      try { open = await view.json(`/api/deployments/${encodeURIComponent(open.id)}`); }
      catch (_) { open = null; }
    }
    render();
  }

  // ---------------------------------------------------------------- create

  async function openForm(entry) {
    const state = view.state();
    const envs = (state.library && state.library.environments) || [];
    const keys = ((state.keys && state.keys.keys) || []).map(k => k.name);
    const provider = entry ? entry.provider : 'meraki';

    const answer = await window.shellmateDialog.form({
      title: entry ? `Edit ${entry.name}` : 'New deployment',
      body: 'A deployment is a site list, a scheme, and the runner’s plan and '
          + 'apply playbooks, committed together. Nothing is built until a plan '
          + 'has been read and an apply approved.',
      fields: [
        { name: 'name', label: 'Name', value: entry ? entry.name : '', placeholder: 'Retail estate 2026' },
        { name: 'provider', label: 'Provider', type: 'select', value: provider,
          options: providers.map(p => ({ value: p, label: p[0].toUpperCase() + p.slice(1) })) },
        { name: 'description', label: 'What is it for? (optional)',
          value: entry ? entry.description : '' },
        ...SCOPE_FIELDS[provider].map(f => ({
          name: `scope:${f.name}`, label: f.label, placeholder: f.placeholder || '',
          value: entry && entry.scope ? (entry.scope[f.name] || '') : '' })),
        { name: 'environment_id', label: 'Environment (optional)', type: 'select',
          value: entry ? entry.environment_id : '',
          options: [{ value: '', label: 'None' },
                    ...envs.map(e => ({ value: e.id, label: e.name }))] },
        ...keys.map(k => ({ name: `key:${k}`, label: `Send key: ${k}`, type: 'checkbox',
                            value: !!(entry && (entry.keys || []).includes(k)) })),
      ],
      confirmLabel: entry ? 'Save' : 'Create',
    });
    if (!answer) return;

    const scope = {};
    SCOPE_FIELDS[answer.provider || provider].forEach(f => {
      const v = (answer[`scope:${f.name}`] || '').trim();
      if (v) scope[f.name] = v;
    });
    const body = {
      id: entry ? entry.id : '',
      name: answer.name, provider: answer.provider || provider,
      description: answer.description || '', scope,
      keys: keys.filter(k => answer[`key:${k}`]),
      environment_id: answer.environment_id || '',
      scheme: entry ? entry.scheme : {},
    };
    try {
      const saved = await view.post('/api/deployments', body);
      toast(`${saved.name} saved.`);
      // A new deployment fetches its kit at once. Failing that is not
      // fatal — the step says "not fetched" and offers the button.
      if (!entry) {
        try { await view.post(`/api/deployments/${encodeURIComponent(saved.id)}/kit`, {}); }
        catch (e) { toast(`Kit not fetched yet: ${e.message || e}`, 'error'); }
      }
      open = null;
      await refresh();
      await show(saved.id);
    } catch (e) {
      toast(e.message || String(e), 'error');
    }
  }

  async function remove(entry) {
    const ok = await window.shellmateDialog.confirm({
      title: `Forget ${entry.name}?`,
      body: 'This deletes the definition from ShellMate. Nothing in the cloud is '
          + 'touched — tearing down what an apply built is an apply of its own, '
          + 'not a side effect of tidying this list.',
      confirmLabel: 'Forget it', danger: true,
    });
    if (!ok) return;
    await view.del(`/api/deployments/${encodeURIComponent(entry.id)}`);
    open = null;
    refresh();
  }

  // ---------------------------------------------------------------- sites

  function chooseSitesFile(entry) {
    const input = document.createElement('input');
    input.type = 'file';
    input.accept = '.csv,.txt,.tsv';
    input.addEventListener('change', () => {
      const file = input.files && input.files[0];
      if (!file) return;
      const reader = new FileReader();
      reader.onload = () => uploadSites(entry, String(reader.result || ''), file.name);
      reader.readAsText(file);
    });
    input.click();
  }

  /**
   * Preview, nominate the columns, then store.
   *
   * The preview shows the headers and asks which is the site name. It
   * remembers the last mapping, so re-uploading next quarter's list is
   * one confirmation rather than four choices.
   */
  async function uploadSites(entry, text, filename) {
    let read;
    try {
      read = await view.post(`/api/deployments/${encodeURIComponent(entry.id)}/sites`,
                             { text, filename, preview: true });
    } catch (e) {
      toast(e.message || String(e), 'error');
      return;
    }
    const headers = read.headers || [];
    const remembered = read.mapping || {};
    const options = [{ value: '', label: '(none)' },
                     ...headers.map(h => ({ value: h, label: h }))];
    const pick = (field, label, required) => ({
      name: field, label: label + (required ? '' : ' (optional)'), type: 'select',
      value: remembered[field] && headers.includes(remembered[field]) ? remembered[field] : '',
      options,
    });
    const answer = await window.shellmateDialog.form({
      title: `${read.count} row${read.count === 1 ? '' : 's'} in ${filename}`,
      body: 'Say which column is which. ShellMate will not guess: a header called '
          + '"site" and one called "Network Name" mean the same thing, and one '
          + 'called "serial" does not. Serials may be blank now and filled in later.',
      fields: [
        pick('name', 'Site name', true),
        pick('tags', 'Tags', false),
        pick('mx', 'MX serial', false),
        pick('ms', 'MS serial', false),
        pick('third_octet', 'Third octet (0–255; the site\'s subnets are built on it — required for Azure)', false),
        pick('timezone', 'Timezone (blank means Etc/UTC)', false),
        { name: 'extra', label: 'Other columns to carry through, comma-separated (optional)',
          value: (remembered.extra || []).join(', ') },
      ],
      confirmLabel: 'Use these columns',
    });
    if (!answer) return;

    const mapping = { name: answer.name, tags: answer.tags, mx: answer.mx, ms: answer.ms,
                      third_octet: answer.third_octet, timezone: answer.timezone,
                      extra: (answer.extra || '').split(',').map(s => s.trim()).filter(Boolean) };
    try {
      const out = await view.post(`/api/deployments/${encodeURIComponent(entry.id)}/sites`,
                                  { text, filename, mapping });
      toast(`${out.sites} site${out.sites === 1 ? '' : 's'} loaded.`);
      refresh();
    } catch (e) {
      toast(e.message || String(e), 'error');
    }
  }

  // ---------------------------------------------------------------- scheme

  /**
   * The scheme as editable YAML-ish JSON.
   *
   * A generic editor until each provider's kit declares its fields — the
   * runner owns what a scheme means, and a form invented here for fields
   * the playbook does not read would be a form that lies.
   */
  function schemeToForm(field, value) {
    if (value === undefined || value === null) value = field.dflt;
    if (value === undefined || value === null) return '';
    if (field.kind === 'csv') return (value || []).join(', ');
    if (field.kind === 'lines') {
      return (value || []).map(row => field.cols.map(c => {
        const v = row[c.split(':')[0]]; return v === undefined || v === null ? '' : String(v);
      }).join(', ')).join('\n');
    }
    if (field.kind === 'kv') return Object.entries(value || {}).map(([k, v]) => `${k} = ${v}`).join('\n');
    return String(value);
  }

  function formToScheme(field, text) {
    const t = (text || '').trim();
    if (!t) return undefined;
    if (field.kind === 'csv') return t.split(',').map(x => x.trim()).filter(Boolean);
    if (field.kind === 'lines') {
      return t.split('\n').map(l => l.trim()).filter(Boolean).map(l => {
        const parts = l.split(',').map(x => x.trim());
        const row = {};
        field.cols.forEach((c, i) => {
          const [key, type] = c.split(':');
          if (parts[i] === undefined || parts[i] === '') return;
          row[key] = type === 'int' ? Number(parts[i]) : parts[i];
        });
        return row;
      });
    }
    if (field.kind === 'kv') {
      const out = {};
      t.split('\n').map(l => l.trim()).filter(Boolean).forEach(l => {
        const i = l.indexOf('=');
        if (i > 0) out[l.slice(0, i).trim()] = l.slice(i + 1).trim();
      });
      return out;
    }
    return t;
  }

  /** A provider's scheme as a form; null when the provider has no spec yet. */
  async function editSpecScheme(entry) {
    const spec = SCHEME_FIELDS[entry.provider];
    if (!spec) return null;
    const s = entry.scheme || {};
    const answer = await window.shellmateDialog.form({
      title: `Scheme for ${entry.name}`,
      body: 'What every site is built to. The runner reads these from scheme.yml; '
          + 'provisioning logic stays in Ansible.',
      fields: spec.map(f => ({
        name: f.name, label: f.label + (f.hint ? ` — ${f.hint}` : ''),
        type: f.kind === 'lines' || f.kind === 'kv' ? 'textarea' : 'text',
        rows: 5, value: schemeToForm(f, s[f.name]), placeholder: f.placeholder || '',
      })),
      confirmLabel: 'Save scheme',
      validate: (v) => {
        for (const f of spec) {
          if (f.required && !(v[f.name] || '').trim()) return `${f.label.split(' (')[0]} is required.`;
          if (f.kind === 'lines') {
            const bad = (v[f.name] || '').split('\n').map(l => l.trim()).filter(Boolean)
              .find(l => l.split(',').length < f.cols.filter(c => !c.endsWith(':int') || true).length - 1);
            if (bad) return `Line "${bad}" needs ${f.cols.map(c => c.split(':')[0]).join(', ')}.`;
          }
        }
        return '';
      },
    });
    if (!answer) return false;
    const scheme = {};
    spec.forEach(f => { const v = formToScheme(f, answer[f.name]); if (v !== undefined) scheme[f.name] = v; });
    await saveScheme(entry, scheme);
    return true;
  }

  async function editScheme(entry) {
    const handled = await editSpecScheme(entry);
    if (handled !== null) return;
    const answer = await window.shellmateDialog.form({
      title: `Scheme for ${entry.name}`,
      body: 'What every site is built to: the base prefix, the VLAN plan, the rule '
          + 'sets, the port profiles. JSON here; it is written to scheme.yml and '
          + 'read by the runner’s playbooks with vars_files. Provisioning logic '
          + 'stays in Ansible — ShellMate never computes a VLAN.',
      fields: [{ name: 'scheme', label: 'Scheme (JSON)', type: 'textarea', rows: 14,
                 value: JSON.stringify(entry.scheme || {}, null, 2) }],
      confirmLabel: 'Save scheme',
      validate: (v) => { try { const o = JSON.parse(v.scheme || '{}');
                               return (o && typeof o === 'object' && !Array.isArray(o))
                                 ? '' : 'The scheme is a JSON object.'; }
                         catch (e) { return `Not valid JSON: ${e.message}`; } },
    });
    if (!answer) return;
    await saveScheme(entry, JSON.parse(answer.scheme || '{}'));
  }

  async function saveScheme(entry, scheme) {
    try {
      await view.post('/api/deployments', {
        id: entry.id, name: entry.name, provider: entry.provider,
        description: entry.description || '', scope: entry.scope || {},
        keys: entry.keys || [], environment_id: entry.environment_id || '',
        scheme,
      });
      toast('Scheme saved. Publish to send it to the runner.');
      refresh();
    } catch (e) {
      toast(e.message || String(e), 'error');
    }
  }

  /**
   * Snapshot the provider's plan and apply from the runner's kit.
   *
   * The runner owns provider knowledge. The snapshot is what the
   * deployment commits, so a later kit change does not silently rewrite a
   * deployment already built — fetching again is a deliberate act.
   */
  async function fetchKit(entry) {
    try {
      const out = await view.post(`/api/deployments/${encodeURIComponent(entry.id)}/kit`, {});
      toast(`Kit fetched: ${out.fetched.length} playbooks from the runner.`);
      refresh();
    } catch (e) {
      toast(e.message || String(e), 'error');
    }
  }

  // ---------------------------------------------------------------- runs

  async function commitKit(provider) {
    try {
      const out = await view.post(`/api/deployments/kits/${encodeURIComponent(provider)}/commit`, {});
      toast(`Committed the ${provider} kit as ${out.commit.sha} (${out.files.length} files).`);
    } catch (e) {
      toast(e.message || String(e), 'error');
    }
  }

  async function publish(entry) {
    try {
      const out = await view.post(`/api/deployments/${encodeURIComponent(entry.id)}/publish`, {});
      const replaced = (out.replaced || []).length;
      toast((out.commit
        ? `Committed ${out.commit.sha} and sent ${out.sent.length} files to the runner`
        : `Sent ${out.sent.length} files to the runner`)
        + ` — ${(out.changed || []).length} changed.`
        // The one case worth a sentence: a copy on the host had been
        // edited by hand, and the commit is what won.
        + (replaced ? ` ${replaced} replaced a copy on the runner that differed from the commit.` : '')
        + (out.skipped_git ? ` ${out.skipped_git}` : ''));
      refresh();
    } catch (e) {
      toast(e.message || String(e), 'error');
    }
  }

  async function startRun(entry, kind) {
    if (kind === 'apply') {
      const ok = await window.shellmateDialog.confirm({
        title: `Apply ${entry.name}?`,
        body: 'This builds what the plan you have just read described, in the '
            + 'live account. Sites are done one at a time; one failing does not '
            + 'stop the rest, and re-running skips what already exists.',
        confirmLabel: 'Apply it', danger: true,
      });
      if (!ok) return;
    }
    try {
      const started = await view.post(`/api/deployments/${encodeURIComponent(entry.id)}/${kind}`, {});
      toast(`${kind === 'plan' ? 'Plan' : 'Apply'} started (${started.id}).`);
      await refresh();
      poll(entry.id, kind);
    } catch (e) {
      toast(e.message || String(e), 'error');
    }
  }

  /** Poll a run's result until the job finishes, then render it. */
  function poll(id, kind) {
    const key = `${id}:${kind}`;
    if (polls[key]) clearInterval(polls[key]);
    polls[key] = setInterval(async () => {
      let out;
      try {
        out = await view.json(`/api/deployments/${encodeURIComponent(id)}/result?kind=${kind}`);
      } catch (e) {
        clearInterval(polls[key]); delete polls[key];
        toast(e.message || String(e), 'error');
        return;
      }
      if (!out.finished) return;
      clearInterval(polls[key]); delete polls[key];
      results[key] = out;
      await refresh();
    }, 3000);
  }

  async function fetchResult(entry, kind) {
    try {
      const out = await view.json(`/api/deployments/${encodeURIComponent(entry.id)}/result?kind=${kind}`);
      results[`${entry.id}:${kind}`] = out;
      if (!out.finished) { poll(entry.id, kind); toast('Still running — this will refresh.'); }
      await refresh();
    } catch (e) {
      toast(e.message || String(e), 'error');
    }
  }

  // ---------------------------------------------------------------- results

  const PLAN_ORDER = { conflict: 0, create: 1, update: 2, unchanged: 3 };
  const APPLY_ORDER = { failed: 0, created: 1, updated: 2, skipped: 3 };

  function pill(text, tone) {
    return el('span', { class: `av-pill${tone ? ` av-pill-${tone}` : ''}`, text });
  }

  function planTable(payload) {
    const plan = payload.plan || {};
    const counts = plan.counts || {};
    const head = el('div', { class: 'av-dep-summary' }, [
      pill(`${counts.create || 0} to create`, counts.create ? 'ok' : ''),
      pill(`${counts.update || 0} to update`, counts.update ? 'warn' : ''),
      pill(`${counts.unchanged || 0} unchanged`),
      pill(`${counts.conflict || 0} conflict${counts.conflict === 1 ? '' : 's'}`,
           counts.conflict ? 'bad' : ''),
      plan.truncated ? pill('list truncated — the runner capped it', 'warn') : null,
    ]);
    const rows = (plan.sites || []).slice()
      .sort((a, b) => (PLAN_ORDER[a.action] ?? 9) - (PLAN_ORDER[b.action] ?? 9));
    const table = el('table', { class: 'av-dep-table' }, [
      el('thead', {}, el('tr', {}, ['Site', 'Action', 'Managed', 'Detail', 'Network'].map(h => el('th', { text: h })))),
      el('tbody', {}, rows.map(r => el('tr', { class: `av-dep-${r.action}` }, [
        el('td', { text: r.name }),
        el('td', {}, pill(r.action, r.action === 'conflict' ? 'bad' : r.action === 'create' ? 'ok' : '')),
        // Whether the scheme's manage_prefix covers this site. A site
        // outside it is planned and reported and never touched — which
        // must read as a fact on the row, not be inferred from a blank.
        el('td', {}, r.managed === false
          ? pill('planned, not touched', 'warn')
          : (r.managed === true ? pill('managed') : el('span', { text: '—' }))),
        el('td', {}, [
          el('div', { text: r.detail || '' }),
          (r.changes || []).length
            ? el('ul', { class: 'av-dep-changes' }, r.changes.map(c => el('li', { text: c })))
            : null,
        ]),
        el('td', { class: 'av-dep-id', text: r.network_id || '—' }),
      ]))),
    ]);
    return el('div', { class: 'av-dep-result' }, [head, table]);
  }

  function applyTable(payload) {
    const apply = payload.apply || {};
    const counts = apply.counts || {};
    const head = el('div', { class: 'av-dep-summary' }, [
      pill(`${counts.created || 0} created`, counts.created ? 'ok' : ''),
      pill(`${counts.updated || 0} updated`),
      pill(`${counts.skipped || 0} skipped`),
      pill(`${counts.failed || 0} failed`, counts.failed ? 'bad' : ''),
      apply.plan_job ? pill(`against plan ${apply.plan_job}`) : null,
      apply.truncated ? pill('list truncated — the runner capped it', 'warn') : null,
    ]);
    const rows = (apply.sites || []).slice()
      .sort((a, b) => (APPLY_ORDER[a.outcome] ?? 9) - (APPLY_ORDER[b.outcome] ?? 9));
    const table = el('table', { class: 'av-dep-table' }, [
      el('thead', {}, el('tr', {}, ['Site', 'Outcome', 'VLANs', 'Reason', 'Ids'].map(h => el('th', { text: h })))),
      el('tbody', {}, rows.map(r => el('tr', { class: `av-dep-${r.outcome}` }, [
        el('td', { text: r.name }),
        el('td', {}, pill(r.outcome, r.outcome === 'failed' ? 'bad'
                            : (r.outcome === 'created' || r.outcome === 'updated') ? 'ok' : '')),
        el('td', { class: 'av-dep-id', text: r.vlans === undefined ? '—' : String(r.vlans) }),
        el('td', { text: r.reason || '' }),
        el('td', { class: 'av-dep-id', text: Object.entries(r.ids || {})
          .map(([k, v]) => `${k}: ${v}`).join(', ') || '—' }),
      ]))),
    ]);
    return el('div', { class: 'av-dep-result' }, [head, table]);
  }

  function resultBlock(entry, kind) {
    const run = entry[`last_${kind}`];
    if (!run || !run.job) return null;
    const cached = results[`${entry.id}:${kind}`];
    const payload = (cached && cached.result) || run.result;
    const title = kind === 'plan' ? 'Plan' : 'Apply';
    const when = run.at ? new Date(run.at * 1000).toLocaleString() : '';
    const header = el('div', { class: 'av-dep-run-head' }, [
      el('h5', { text: `${title} ${run.job}` }),
      el('span', { class: 'av-inv-note', text: when }),
      el('button', { type: 'button', class: 'btn-tertiary',
                     onclick: () => fetchResult(entry, kind) },
         [icon('refresh'), payload ? 'Fetch again' : 'Fetch the result']),
    ]);
    let body;
    if (payload) body = kind === 'plan' ? planTable(payload) : applyTable(payload);
    else if (cached && cached.finished && !cached.has_result) {
      body = el('p', { class: 'av-inv-note',
        text: 'The run finished but published no result. Read its output under Runs.' });
    } else {
      body = el('p', { class: 'av-inv-note', text: polls[`${entry.id}:${kind}`]
        ? 'Running — this refreshes on its own.' : 'Not fetched yet.' });
    }
    return el('section', { class: 'av-dep-run' }, [header, body]);
  }

  // ---------------------------------------------------------------- views

  function card(entry) {
    return el('article', { class: 'av-card av-dep-card', onclick: () => show(entry.id) }, [
      el('div', { class: 'av-env-card-head' }, [
        el('h4', { text: entry.name }),
        pill(entry.provider),
      ]),
      entry.description ? el('p', { class: 'av-env-desc', text: entry.description }) : null,
      el('dl', { class: 'av-env-meta' }, [
        metaRow('Sites', String(entry.sites || 0)),
        metaRow('Built', String(entry.built || 0)),
        metaRow('Committed', entry.last_commit ? entry.last_commit.sha : 'not yet'),
        metaRow('Last plan', entry.last_plan ? entry.last_plan.job : 'none'),
      ]),
    ]);
  }

  function metaRow(label, value) {
    return el('div', {}, [el('dt', { text: label }), el('dd', { text: value })]);
  }

  async function show(id) {
    try { open = await view.json(`/api/deployments/${encodeURIComponent(id)}`); }
    catch (e) { toast(e.message || String(e), 'error'); return; }
    render();
  }

  function detail(entry) {
    const blocked = entry.apply_blocked || '';
    const steps = el('div', { class: 'av-dep-steps' }, [
      step(1, 'Sites', `${(entry.sites || []).length} loaded`,
           el('button', { type: 'button', class: 'btn-secondary',
                          onclick: () => chooseSitesFile(entry) }, [icon('upload'), 'Upload a site list'])),
      step(2, 'Scheme', Object.keys(entry.scheme || {}).length
             ? `${Object.keys(entry.scheme).length} field${Object.keys(entry.scheme).length === 1 ? '' : 's'}` : 'empty',
           el('button', { type: 'button', class: 'btn-secondary',
                          onclick: () => editScheme(entry) }, [icon('edit'), 'Edit the scheme'])),
      step(3, 'Kit', entry.kit_fetched
             ? `plan and apply from the runner, ${new Date(entry.kit_fetched * 1000).toLocaleString()}`
             : 'not fetched',
           el('button', { type: 'button', class: 'btn-secondary',
                          onclick: () => fetchKit(entry) },
              [icon('download'), entry.kit_fetched ? 'Fetch again' : 'Fetch from the runner'])),
      step(4, 'Publish', entry.last_published
             ? `sent ${new Date(entry.last_published.at * 1000).toLocaleString()}`
               + (entry.last_commit ? ` · commit ${entry.last_commit.sha}` : ' · not committed')
             : 'not yet',
           el('button', { type: 'button', class: 'btn-secondary',
                          onclick: () => publish(entry) }, [icon('upload'), 'Commit and send'])),
      step(5, 'Plan', entry.last_plan ? `last ${entry.last_plan.job}` : 'none yet',
           // `disabled` only when true: el() sets any attribute it is given,
           // and a button with disabled="false" is a disabled button.
           el('button', { type: 'button', class: 'btn-secondary',
                          ...(entry.last_published ? {} : { disabled: true }),
                          onclick: () => startRun(entry, 'plan') }, [icon('science'), 'Run a plan'])),
      step(6, 'Apply', blocked ? 'not yet' : 'ready',
           el('button', { type: 'button', class: 'btn-primary',
                          ...(blocked ? { disabled: true } : {}),
                          title: blocked || 'Build what the plan described',
                          onclick: () => startRun(entry, 'apply') }, [icon('bolt'), 'Apply'])),
    ]);
    // The reason the button is off, in words beside it — a disabled
    // button with no sentence is a puzzle, and this one has a rule behind
    // it worth knowing.
    const why = blocked ? el('p', { class: 'av-dep-why', text: blocked }) : null;

    return el('div', { class: 'av-dep-detail' }, [
      el('div', { class: 'av-dep-head' }, [
        el('button', { type: 'button', class: 'btn-tertiary', onclick: () => { open = null; render(); } },
           [icon('keyboard_arrow_up'), 'All deployments']),
        el('h4', { text: entry.name }),
        pill(entry.provider),
        el('div', { class: 'av-row-actions' }, [
          el('button', { type: 'button', class: 'icon-btn', title: 'Edit', onclick: () => openForm(entry) }, icon('edit')),
          el('button', { type: 'button', class: 'icon-btn', title: 'Forget', onclick: () => remove(entry) }, icon('delete_forever')),
        ]),
      ]),
      entry.description ? el('p', { class: 'av-env-desc', text: entry.description }) : null,
      el('p', { class: 'av-inv-note', text: `Files: ${Object.values(entry.runner_paths || {}).join(', ')}` }),
      steps, why,
      resultBlock(entry, 'plan'),
      resultBlock(entry, 'apply'),
    ]);
  }

  function step(n, title, status, control) {
    return el('div', { class: 'av-dep-step' }, [
      el('span', { class: 'av-dep-step-n', text: String(n) }),
      el('div', { class: 'av-dep-step-text' }, [
        el('strong', { text: title }), el('span', { class: 'av-inv-note', text: status })]),
      control,
    ]);
  }

  function render() {
    const body = document.getElementById('av-deployments-body');
    if (!body) return;
    clear(body);
    if (open) { body.appendChild(detail(open)); return; }
    if (!list.length) {
      body.appendChild(view.blank({
        icon: 'automation',
        title: 'Build it from a definition',
        lines: [
          'A deployment is a site list, a scheme, and the runner’s plan and apply '
          + 'playbooks, committed to your repository together. Five hundred Meraki '
          + 'networks with an MX and an MS in each, VLANs and rules per site — from a '
          + 'CSV and a form, not five hundred playbooks.',
          'Nothing is built until a plan has been read. Meraki has no check mode, so '
          + 'the plan is the only preview there is, and Apply stays off until one has '
          + 'been fetched here.',
          'One site failing does not stop the rest, and re-running skips what already '
          + 'exists — so a run that dies at site 213 is resumed by running it again.',
        ],
        action: el('button', { type: 'button', class: 'btn-primary', onclick: () => openForm(null) },
                   [icon('add'), 'New deployment']),
      }));
      return;
    }
    body.appendChild(el('div', { class: 'av-env-toolbar' }, [
      el('button', { type: 'button', class: 'btn-primary', onclick: () => openForm(null) },
         [icon('add'), 'New deployment']),
      // Nothing commits the runner's working tree, so a kit the runner
      // session wrote exists only on its disk until this does.
      ...providers.map(p => el('button', { type: 'button', class: 'btn-tertiary',
        title: `Fetch the ${p} kit from the runner and commit it to the repository`,
        onclick: () => commitKit(p) }, [icon('save'), `Commit the ${p} kit`])),
    ]));
    body.appendChild(el('div', { class: 'av-grid av-env-list' }, list.map(card)));
  }

  view.area('deployments', {
    onShow: () => refresh(),
    onData: () => { if (view.current === 'deployments') render(); },
  });

  window.ansibleDeployments = { refresh, show, planTable, applyTable };
})();
