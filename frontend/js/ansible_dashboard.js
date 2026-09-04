/**
 * ansible_dashboard.js — The Ansible view's first screen (#586).
 *
 * The question this answers is "what is going on", not "what can I do".
 * So it leads with runs — the thing that is actually happening to devices
 * right now — and puts what you hold underneath as counts you can click
 * through. A dashboard that leads with a grid of buttons is a menu with
 * extra steps.
 *
 * When the runner is unreachable the library half still paints. A template
 * is still worth editing with the container off, and blanking a screen
 * because something else failed is what makes a tool feel fragile.
 */

(function () {
  'use strict';

  const view = window.ansibleView;
  if (!view) return;
  const { el, icon, clear } = view;

  /** How long ago, in words. Exact timestamps go in the title attribute. */
  function ago(seconds) {
    if (!seconds) return '';
    const delta = Math.max(0, Date.now() / 1000 - seconds);
    if (delta < 60) return 'just now';
    if (delta < 3600) return `${Math.round(delta / 60)} min ago`;
    if (delta < 86400) return `${Math.round(delta / 3600)} h ago`;
    return `${Math.round(delta / 86400)} d ago`;
  }

  /** A job's state, as a word and a colour. */
  function statusPill(status) {
    const kind = {
      successful: 'ok', running: 'warn', starting: 'warn',
      failed: 'bad', canceled: 'bad', cancelled: 'bad', timeout: 'bad',
    }[String(status || '').toLowerCase()] || 'unknown';
    return el('span', { class: `av-pill av-pill-${kind}`, text: status || 'unknown' });
  }

  /**
   * A count with a name, that takes you to the thing it counts.
   *
   * The number is the point, so it is the biggest thing in the tile; the
   * label under it says what was counted. Clicking goes to that area,
   * because a figure you cannot act on is decoration.
   */
  function tile(figure, label, area, hint) {
    return el('button', {
      type: 'button', class: 'av-card av-tile', title: hint || '',
      onclick: () => view.show(area),
    }, [
      el('span', { class: 'av-figure', text: String(figure) }),
      el('span', { class: 'av-tile-label', text: label }),
    ]);
  }

  function renderJobs(jobs) {
    if (!jobs.length) {
      return view.empty('No runs yet. Start one from Playbooks.');
    }
    const rows = jobs.map(job => el('tr', {}, [
      el('td', {}, el('span', { class: 'av-job-name', text: job.playbook || job.id })),
      el('td', {}, statusPill(job.status)),
      el('td', { class: 'av-when', title: job.started
        ? new Date(job.started * 1000).toLocaleString() : '' },
        ago(job.started)),
      el('td', { class: 'av-row-actions' },
        el('button', {
          type: 'button', class: 'btn-tertiary',
          onclick: () => {
            view.show('playbooks');
            document.dispatchEvent(new CustomEvent('shellmate:ansible-open-job', {
              detail: { id: job.id },
            }));
          },
        }, [icon('visibility'), 'Open'])),
    ]));
    return el('table', { class: 'av-table' }, [
      el('thead', {}, el('tr', {}, [
        el('th', { text: 'Playbook' }), el('th', { text: 'Result' }),
        el('th', { text: 'Started' }), el('th', { text: '' }),
      ])),
      el('tbody', {}, rows),
    ]);
  }

  /**
   * What to do when the runner is not answering.
   *
   * Named separately from an empty list because they are different
   * problems: "nothing has run" is fine, "I cannot ask" is not, and one
   * message covering both would be wrong half the time.
   */
  function runnerTrouble(runner) {
    if (!runner.configured) {
      return el('div', { class: 'av-notice av-notice-info' }, [
        icon('info'),
        el('div', {}, [
          el('strong', { text: 'No runner yet. ' }),
          'ShellMate drives a container that runs Ansible. Give it the '
          + 'address and token under Settings, and this fills in.',
        ]),
        el('button', {
          type: 'button', class: 'btn-secondary',
          onclick: () => (window.openSettings ? window.openSettings('ansible') : null),
        }, 'Open Settings'),
      ]);
    }
    if (runner.reachable && runner.authenticated === false) {
      return el('div', { class: 'av-notice av-notice-warn' }, [
        icon('key'),
        el('div', {}, [
          el('strong', { text: 'The runner will not accept us. ' }),
          'It answered, so the address and the network are right. The token '
          + 'is the part to check.',
        ]),
      ]);
    }
    if (runner.kind === 'certificate') {
      return el('div', { class: 'av-notice av-notice-bad' }, [
        icon('encrypted'),
        el('div', {}, [
          el('strong', { text: 'The runner answered, but its certificate was refused. ' }),
          runner.detail || '',
          ' This is a trust problem, not a network one: give ShellMate the '
          + "runner's CA certificate rather than turning verification off.",
        ]),
        el('button', {
          type: 'button', class: 'btn-secondary',
          onclick: () => (window.openSettings ? window.openSettings('ansible') : null),
        }, 'Open Settings'),
      ]);
    }
    return el('div', { class: 'av-notice av-notice-bad' }, [
      icon('error'),
      el('div', {}, [
        el('strong', { text: 'Cannot reach the runner. ' }),
        runner.detail || `Nothing answered at ${runner.url || 'the address given'}.`,
      ]),
    ]);
  }

  function render(state) {
    const body = document.getElementById('av-dashboard-body');
    if (!body) return;
    clear(body);

    const overview = state.overview || {};
    const runner = overview.runner || {};
    const counts = overview.library || {};
    const books = overview.playbooks || {};
    const keys = (state.keys || {}).keys || [];

    if (!runner.reachable) {
      body.appendChild(runnerTrouble(runner));
    } else if (runner.encrypted && runner.verified === false) {
      body.appendChild(el('div', { class: 'av-notice av-notice-warn' }, [
        icon('encrypted'),
        el('div', {}, [
          el('strong', { text: 'The certificate is not being checked. ' }),
          'Runs are encrypted but nothing verifies who is on the other end. '
          + 'That is a reasonable trade for a development certificate and a '
          + 'poor one for anything else.',
        ]),
      ]));
    } else if (runner.encrypted === false) {
      body.appendChild(el('div', { class: 'av-notice av-notice-warn' }, [
        icon('info'),
        el('div', {}, [
          el('strong', { text: 'Plain HTTP. ' }),
          'The token and anything a run carries cross the network in the '
          + 'clear. Fine on a trusted management LAN; put TLS in front of '
          + 'the runner the day it moves.',
        ]),
      ]));
    }

    body.appendChild(el('section', { class: 'av-block' }, [
      el('h4', { class: 'av-block-title' }, 'Recent runs'),
      renderJobs(overview.jobs || []),
    ]));

    body.appendChild(el('section', { class: 'av-block' }, [
      el('h4', { class: 'av-block-title' }, 'What you have'),
      el('div', { class: 'av-grid av-tiles' }, [
        tile((books.runner || 0) + (books.library || 0), 'Playbooks', 'playbooks',
             `${books.runner || 0} on the runner, ${books.library || 0} written here`),
        tile(counts.templates || 0, 'Templates', 'templates',
             'Parameterised plays, filled in each time'),
        tile(counts.environments || 0, 'Environments', 'environments',
             'Named settings a run inherits'),
        tile(keys.length, 'Keys', 'keys',
             'Credentials held in the vault, sent only with a run that needs them'),
        tile(counts.repositories || 0, 'Repositories', 'repositories',
             'Where a set of playbooks came from'),
      ]),
    ]));
  }

  view.area('dashboard', {
    onShow: (state) => render(state),
    onData: (state) => {
      if (view.current === 'dashboard') render(state);
    },
  });
})();
