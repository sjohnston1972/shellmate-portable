/**
 * ansible_runs.js — What has run, and what it did (#591).
 *
 * This was a third section inside Playbooks, which already held the
 * library, the editor and the live run. A record of what happened is not
 * something you do to a playbook, so it has its own area.
 *
 * Two records, and they are not the same thing:
 *
 * - **The runner's**, from its jobs endpoint. Authoritative, survives a
 *   restart, and includes runs somebody started from elsewhere. The hint
 *   this area replaced said the service had no endpoint that lists past
 *   runs; that stopped being true and nothing had gone back to correct it.
 * - **ShellMate's own note**, of the runs this browser watched. It is the
 *   only place the tallies and the report exist, because those are built
 *   from the event stream as it arrives rather than asked for afterwards.
 *
 * Showing both, labelled, rather than merging them: a run in one and not
 * the other is informative — it says either "started somewhere else" or
 * "the runner has forgotten it" — and a merged list would hide exactly
 * that.
 */

(function () {
  'use strict';

  const view = window.ansibleView;
  if (!view) return;
  const { el, icon, clear } = view;

  function ago(iso) {
    if (!iso) return '';
    const then = typeof iso === 'number' ? iso * 1000 : Date.parse(iso);
    if (!then || Number.isNaN(then)) return String(iso);
    const delta = Math.max(0, (Date.now() - then) / 1000);
    if (delta < 60) return 'just now';
    if (delta < 3600) return `${Math.round(delta / 60)} min ago`;
    if (delta < 86400) return `${Math.round(delta / 3600)} h ago`;
    return `${Math.round(delta / 86400)} d ago`;
  }

  function statusPill(status) {
    const kind = {
      successful: 'ok', running: 'warn', starting: 'warn',
      failed: 'bad', canceled: 'bad', cancelled: 'bad', timeout: 'bad',
    }[String(status || '').toLowerCase()] || 'unknown';
    return el('span', { class: `av-pill av-pill-${kind}`, text: status || 'unknown' });
  }

  /**
   * The runner's own record.
   *
   * Reported rather than raised when it cannot be read: the browser's own
   * notes below are still worth showing, and an area that blanks because
   * something else failed is what makes a tool feel fragile.
   */
  async function renderJobs() {
    const host = document.getElementById('ansible-jobs');
    if (!host) return;
    clear(host);

    let jobs = null;
    try {
      jobs = await view.json('/api/ansible/jobs');
    } catch (e) {
      host.appendChild(el('div', { class: 'av-notice av-notice-warn' }, [
        icon('info'),
        el('div', { text: `The runner's own record could not be read: ${e.message || e}` }),
      ]));
      return;
    }

    const rows = Array.isArray(jobs) ? jobs : (jobs.jobs || []);
    if (!rows.length) {
      host.appendChild(view.empty(
        'The runner has no runs on record. One started from here will appear '
        + 'in both lists.'));
      return;
    }

    host.appendChild(el('table', { class: 'av-table' }, [
      el('thead', {}, el('tr', {}, [
        el('th', { text: 'Playbook' }), el('th', { text: 'Result' }),
        el('th', { text: 'Started' }), el('th', { text: '' }),
      ])),
      el('tbody', {}, rows.slice(0, 40).map(job => el('tr', {}, [
        el('td', {}, el('span', { class: 'av-job-name',
                                  text: job.playbook || job.id || '—' })),
        el('td', {}, statusPill(job.status)),
        el('td', { class: 'av-when',
                   title: job.started ? String(job.started) : '' },
           ago(job.started)),
        el('td', { class: 'av-row-actions' }, el('button', {
          type: 'button', class: 'btn-tertiary',
          title: 'Follow this run and read its output',
          onclick: () => {
            if (typeof window.ansibleWatchJob === 'function') {
              window.ansibleWatchJob(job);
            } else {
              view.show('playbooks');
            }
          },
        }, [icon('visibility'), 'Watch'])),
      ]))),
    ]));
  }

  function render() {
    renderJobs();
    // The browser's own notes are rendered by ansible.js, which owns the
    // history and the report built from the event stream.
    if (typeof window.ansibleRenderHistory === 'function') {
      window.ansibleRenderHistory();
    }
  }

  view.area('runs', { onShow: render });
  document.addEventListener('shellmate:ansible-refresh', () => {
    if (view.current === 'runs') render();
  });
})();
