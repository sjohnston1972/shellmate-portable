/**
 * ansible_repositories.js — Where a set of playbooks came from, and how it gets to the runner (#586).
 *
 * ShellMate does not clone anything. The runner has no git API, and a
 * portable executable carrying its own git implementation to drive a
 * container it cannot reach directly would be the wrong shape for what is
 * a desktop tool. So this is a record, not a sync: the remote, the branch,
 * the path inside it the playbooks live under, and the last revision
 * somebody actually looked at and noted — enough to say "the runner is
 * three commits behind" in a change record, and nothing this cannot back
 * up by having actually done the work.
 *
 * Noting a revision is deliberately a separate action from editing the
 * rest of the record (`backend.ansible_library.note_revision`, wired to
 * `POST /api/ansible/repositories/{id}/note`). Folding it into the ordinary
 * save would stamp "checked" on every unrelated correction — fixing a typo
 * in the branch name would silently claim somebody had just checked the
 * remote, which they had not.
 */

(function () {
  'use strict';

  const view = window.ansibleView;
  if (!view) return;
  const { el, icon, clear, empty, toast } = view;

  /** How long ago, in words. The exact time goes in the title attribute. */
  function ago(seconds) {
    if (!seconds) return 'not noted';
    const delta = Math.max(0, Date.now() / 1000 - seconds);
    if (delta < 60) return 'noted just now';
    if (delta < 3600) return `noted ${Math.round(delta / 60)} min ago`;
    if (delta < 86400) return `noted ${Math.round(delta / 3600)} h ago`;
    return `noted ${Math.round(delta / 86400)} d ago`;
  }

  /**
   * What this area is and is not, said once at the top rather than implied
   * by an absent "Sync now" button somebody would otherwise go looking for.
   */
  function notice() {
    return el('div', { class: 'av-notice av-notice-info' }, [
      icon('info'),
      el('div', {}, [
        el('strong', { text: 'This is a record, not a sync. ' }),
        'ShellMate does not clone or pull anything. A playbook written here '
        + 'reaches the runner because its project directory is a bind mount '
        + "from the container host — either files are put there directly, "
        + 'or that directory is kept in git and pulled to it. What is held '
        + 'here is what somebody last noted: the remote, the branch, and the '
        + "revision seen — enough to say the runner is behind, in words a "
        + 'change record can use.',
      ]),
    ]);
  }

  /** The fields a repository form asks for, prefilled when editing. */
  function fields(repo) {
    repo = repo || {};
    return [
      { name: 'name', label: 'Name', required: true, value: repo.name || '',
        placeholder: 'network-playbooks',
        hint: 'What you will recognise it by here — not the remote name.' },
      { name: 'url', label: 'URL', required: true, value: repo.url || '',
        placeholder: 'git@github.com:org/network-playbooks.git',
        hint: 'https, ssh, or a git@ address. This is never dialled by '
             + 'ShellMate — it is only kept for the record.' },
      { name: 'branch', label: 'Branch', value: repo.branch || 'main' },
      { name: 'path', label: 'Path', value: repo.path || '',
        placeholder: 'playbooks/',
        hint: 'Where the playbooks live inside the repository, if not the '
             + 'root.' },
      { name: 'notes', label: 'Notes', type: 'textarea', rows: 3,
        value: repo.notes || '',
        hint: 'Anything else worth remembering — who owns it, what it is '
             + 'not for.' },
    ];
  }

  async function openForm(repo) {
    const editing = Boolean(repo && repo.id);
    const answer = await window.shellmateDialog.form({
      title: editing ? `Edit ${repo.name}` : 'Add a repository',
      confirmLabel: editing ? 'Save' : 'Add',
      fields: fields(repo),
    });
    if (!answer) return;
    try {
      await view.post('/api/ansible/repositories', {
        id: editing ? repo.id : '',
        name: answer.name,
        url: answer.url,
        branch: answer.branch || 'main',
        path: answer.path,
        notes: answer.notes,
        // The edit form leaves the revision as it was — noting one is its
        // own action below, so a correction to the URL cannot accidentally
        // blank out what was last actually seen.
        revision: (repo && repo.revision) || '',
      });
      await view.load(true);
    } catch (e) {
      toast(e.message || String(e), 'error');
    }
  }

  async function noteRevision(repo) {
    const revision = await window.shellmateDialog.prompt({
      title: `Note a revision for ${repo.name}`,
      label: 'The commit or tag last seen at the remote',
      value: repo.revision || '',
    });
    if (revision === null) return;
    if (!revision.trim()) {
      toast('A revision is needed to note one.', 'error');
      return;
    }
    try {
      await view.post(`/api/ansible/repositories/${encodeURIComponent(repo.id)}/note`,
                       { revision: revision.trim() });
      await view.load(true);
    } catch (e) {
      toast(e.message || String(e), 'error');
    }
  }

  async function removeRepo(repo) {
    const go = await window.shellmateDialog.confirm({
      title: `Delete ${repo.name}?`,
      body: 'This removes the record. It does not touch the runner or any '
           + 'playbook already copied there.',
      confirmLabel: 'Delete', danger: true,
    });
    if (!go) return;
    try {
      await view.del(`/api/ansible/repositories/${encodeURIComponent(repo.id)}`);
      await view.load(true);
    } catch (e) {
      toast(e.message || String(e), 'error');
    }
  }

  function row(repo) {
    const nameCell = [
      el('div', { class: 'av-repo-name' }, [icon('folder'), el('strong', { text: repo.name })]),
    ];
    if (repo.notes) {
      nameCell.push(el('div', { class: 'av-repo-notes', title: repo.notes },
        [icon('description'), repo.notes]));
    }

    const remoteCell = [el('span', { class: 'av-repo-remote', title: repo.url }, repo.url)];
    if (repo.path) {
      remoteCell.push(el('div', { class: 'av-repo-path' }, [icon('code'), repo.path]));
    }

    const revisionCell = repo.revision
      ? [
          el('div', { class: 'av-repo-revision', title: repo.revision },
            [icon('commit'), el('code', { text: repo.revision })]),
          el('div', { class: 'av-repo-when', title: repo.checked
            ? new Date(repo.checked * 1000).toLocaleString() : '' },
            [icon('history'), ago(repo.checked)]),
        ]
      : [el('span', { class: 'av-repo-none', text: 'none recorded' })];

    return el('tr', {}, [
      el('td', {}, nameCell),
      el('td', {}, remoteCell),
      el('td', {}, el('span', { class: 'av-pill av-pill-unknown av-repo-branch' },
        [icon('tag'), repo.branch || 'main'])),
      el('td', {}, revisionCell),
      el('td', { class: 'av-row-actions' }, [
        el('button', {
          type: 'button', class: 'icon-btn', title: 'Note the revision seen',
          onclick: () => noteRevision(repo),
        }, icon('commit')),
        el('button', {
          type: 'button', class: 'icon-btn', title: 'Edit',
          onclick: () => openForm(repo),
        }, icon('edit')),
        el('button', {
          type: 'button', class: 'icon-btn', title: 'Delete',
          onclick: () => removeRepo(repo),
        }, icon('delete_forever')),
      ]),
    ]);
  }

  /**
   * Install what the runner's playbooks need, from its requirements file.
   *
   * This belongs here rather than in Playbooks because it is a property of
   * where the playbooks came from: a repository brings its collections
   * with it in `requirements.yml`, and "the module is not found" is what
   * happens when nobody ran this. The runner does the work; ShellMate asks
   * and reports what came back.
   */
  async function installCollections() {
    const button = document.getElementById('av-repo-galaxy');
    const said = document.getElementById('av-repo-galaxy-said');
    const named = ((document.getElementById('av-repo-requirements') || {}).value
                   || '').trim();
    if (button) button.disabled = true;
    if (said) said.textContent = 'Asking the runner to install…';
    try {
      // As a query parameter, not a body. The runner reads it from the
      // query; sent as JSON it was accepted, ignored, and the default file
      // installed while answering 200. A file that is not there now fails
      // instead, so naming one is safe.
      const result = await view.post('/api/ansible/galaxy'
        + (named ? `?requirements=${encodeURIComponent(named)}` : ''), {});
      const text = (result && (result.stdout || result.detail || result.status))
        || 'The runner reported nothing further.';
      if (said) said.textContent = 'Done.';
      await window.shellmateDialog.alert({
        title: `Installed from ${named || 'requirements.yml'}`,
        body: String(text).slice(0, 4000),
      });
    } catch (e) {
      if (said) said.textContent = '';
      await window.shellmateDialog.alert({
        title: 'Could not install the collections',
        body: String(e.message || e),
      });
    } finally {
      if (button) button.disabled = false;
    }
  }

  function collectionsBlock() {
    return el('section', { class: 'av-block' }, [
      el('h4', { class: 'av-block-title' }, 'Collections'),
      el('p', { class: 'av-repo-hint' },
        "A repository usually brings its collections with it in a "
        + "requirements.yml. The runner installs them, not ShellMate — and "
        + "\"module not found\" three tasks into a run is what happens when "
        + "nobody has."),
      el('div', { class: 'av-row-actions' }, [
        el('input', {
          type: 'text', id: 'av-repo-requirements', class: 'av-repo-input',
          placeholder: 'requirements.yml',
          title: 'A path the runner resolves against /runner and then '
               + '/runner/project, so myrepo/requirements.yml works as well '
               + 'as project/myrepo/requirements.yml. Leave it empty for '
               + 'requirements.yml.',
        }),
        el('button', {
          type: 'button', class: 'btn-secondary', id: 'av-repo-galaxy',
          onclick: installCollections,
        }, [icon('download'), 'Install collections']),
        el('span', { id: 'av-repo-galaxy-said', class: 'av-repo-hint' }),
      ]),
      el('p', { class: 'av-repo-hint' },
        'A repository that keeps its own requirements file can be named '
        + 'here: the runner looks under /runner and then /runner/project, so '
        + 'myrepo/requirements.yml is enough. A file that is not there is '
        + 'reported as missing rather than quietly falling back to the '
        + 'default, and a path that climbs out or starts at the root is '
        + 'refused.'),
    ]);
  }

  function render(state) {
    const body = document.getElementById('av-repositories-body');
    if (!body) return;
    clear(body);
    body.appendChild(notice());

    const repos = ((state.library || {}).repositories) || [];

    const addBtn = el('button', {
      type: 'button', class: 'btn-primary', onclick: () => openForm(null),
    }, [icon('add'), 'Add repository']);

    // One primary action, not two: the toolbar button and the empty state's
    // button were the same command shown twice (#597).
    if (!repos.length) {
      body.appendChild(view.blank({
        icon: 'folder',
        title: 'Where a set of playbooks came from',
        lines: [
          'ShellMate does not clone anything and the runner has no git of its '
          + 'own, so this is a record rather than a sync: the remote, the '
          + 'branch, and the revision somebody last noted.',
          'That is enough to say "the runner is three commits behind", and '
          + 'enough to put in a change record — which is usually what the '
          + 'question is really about.',
          'Files still reach the runner the ordinary way: into its project '
          + 'directory on the container host, or by keeping that directory in '
          + 'git and pulling it there.',
        ],
        action: el('button', {
          type: 'button', class: 'btn-primary', onclick: () => openForm(null),
        }, [icon('add'), 'Add repository']),
      }));
      // Still offered: the runner can have playbooks and a requirements
      // file without anybody having written down where they came from.
      body.appendChild(collectionsBlock());
      return;
    }

    body.appendChild(el('div', { class: 'av-repo-toolbar' }, [
      el('h4', { class: 'av-block-title', text: `${repos.length} repositor${repos.length === 1 ? 'y' : 'ies'}` }),
      addBtn,
    ]));

    body.appendChild(el('table', { class: 'av-table' }, [
      el('thead', {}, el('tr', {}, [
        el('th', { text: 'Name' }), el('th', { text: 'Remote' }),
        el('th', { text: 'Branch' }), el('th', { text: 'Revision' }),
        el('th', { text: '' }),
      ])),
      el('tbody', {}, repos.map(row)),
    ]));
    body.appendChild(collectionsBlock());
  }

  view.area('repositories', {
    onShow: (state) => render(state),
    onData: (state) => {
      if (view.current === 'repositories') render(state);
    },
  });
})();
