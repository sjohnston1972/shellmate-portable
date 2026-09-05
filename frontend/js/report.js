/**
 * report.js — Export a session, a diff or a change as a file (#540).
 *
 * Three places offer this — the replay header, the diff window, and the
 * Jira modal as "Save as a file instead" — and they are one module rather
 * than three because the only thing that differs between them is which
 * three fields go in the body. Three copies of "ask which format, post,
 * toast, offer the folder" is how the copy buttons ended up behaving
 * differently from one another in #429.
 *
 * The format is asked for rather than assumed. Markdown is what goes into
 * a ticket or a repository; the HTML page is what gets printed to PDF for
 * a change board, and it is a different document to the person receiving
 * it even though it is the same content. Guessing would be wrong half the
 * time and there is no way to tell which half.
 *
 * The toast offers the folder rather than the file. A viewer's idea of
 * what opens an `.md` is not ShellMate's business, and the folder is also
 * the answer to "where do these go?", which is the question somebody asks
 * on their second export rather than their first.
 */
(function () {
  'use strict';

  /**
   * Post the report and say what happened.
   *
   * Errors are toasted rather than thrown: the export is a side errand
   * from whatever the person was actually doing, and a failure to write a
   * file must not take the panel they were reading with it.
   */
  async function write(body) {
    let res, data;
    try {
      res = await fetch('/api/reports', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      data = await res.json();
    } catch (e) {
      notify({
        severity: 'warning', icon: 'error',
        title: 'The report could not be written',
        body: e.message || String(e),
      });
      return null;
    }

    if (!res.ok) {
      notify({
        severity: 'warning', icon: 'error',
        title: 'The report could not be written',
        body: data && data.detail ? data.detail : `HTTP ${res.status}`,
      });
      return null;
    }

    notify({
      severity: 'info', icon: 'download',
      title: 'Report saved',
      body: data.name,
      tooltip: data.path,
      action: { label: 'Show me', onClick: reveal },
    });
    return data;
  }

  async function reveal() {
    try {
      const res = await fetch('/api/reports/reveal', { method: 'POST' });
      const data = await res.json();
      // Nothing opened is not a failure — a headless or server deployment
      // has no file manager to open. Saying where it is instead is the
      // useful answer, and the only one available.
      if (data && !data.opened) {
        notify({
          severity: 'info', icon: 'folder',
          title: 'Reports are kept here',
          body: data.folder || '',
        });
      }
    } catch (_) { /* the file is written either way */ }
  }

  function notify(spec) {
    if (window.shellmateAlerts) {
      window.shellmateAlerts.notify({ global: true, ...spec });
    } else {
      console.info(`[report] ${spec.title}: ${spec.body || ''}`);
    }
  }

  /**
   * Offer the two formats beneath a button.
   *
   * @param {Element} anchor  The button the menu hangs from.
   * @param {Function} body   Given "md" or "html", returns the request body.
   */
  function offer(anchor, body, extras) {
    if (!window.shellmateMenu) return;
    window.shellmateMenu.open(anchor, [
      { heading: 'Export as' },
      {
        icon: 'description', label: 'Markdown (.md)',
        title: 'For a ticket, a repository, or anywhere Markdown renders',
        onClick: () => write(body('md')),
      },
      {
        icon: 'download', label: 'Web page (.html)',
        title: 'Self-contained, and laid out for print-to-PDF',
        onClick: () => write(body('html')),
      },
      ...(extras || []),
    ]);
  }

  window.shellmateReport = {
    offer,
    write,
    reveal,

    /**
     * One session: its commands, its notes, and — where it makes sense —
     * the two documents that are not reports at all.
     *
     * @param {boolean} withPlayback  Offer the self-contained replay and the
     *        plain transcript as well (#574). The replay header does; the
     *        Jira modal does not, because somebody who came to raise a
     *        ticket wants the write-up, not a 300 KB page of terminal.
     */
    session(anchor, sessionId, extra, withPlayback) {
      const extras = withPlayback ? [
        'sep',
        {
          // 'replay', not 'play_arrow': the latter is outside the
          // committed font subset and would render as its own name in
          // plain text. It is also what the in-app Play button uses.
          icon: 'replay', label: 'Playback (.html)',
          title: 'A page that replays the session, with the same controls. '
               + 'Opens by double-clicking, on a machine without ShellMate.',
          onClick: () => write({ kind: 'playback', session_id: sessionId }),
        },
        {
          icon: 'article', label: 'Transcript (.txt)',
          title: 'Plain text, for pasting into a vendor case or a mail',
          onClick: () => write({ kind: 'transcript', session_id: sessionId }),
        },
      ] : [];
      offer(anchor, (format) => ({
        kind: 'session', format, session_id: sessionId, ...(extra || {}),
      }), extras);
    },

    /** The diff window: two snapshots and what moved between them. */
    diff(anchor, oldId, newId) {
      offer(anchor, (format) => ({
        kind: 'diff', format, old_id: oldId, new_id: newId,
      }));
    },

    /** A change record: the two snapshots and the commands typed between. */
    change(anchor, sessionId, oldId, newId, extra) {
      offer(anchor, (format) => ({
        kind: 'change', format, session_id: sessionId,
        old_id: oldId ?? null, new_id: newId ?? null, ...(extra || {}),
      }));
    },
  };
})();
