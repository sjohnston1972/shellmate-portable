/**
 * support.js — Assembling a support request worth answering.
 *
 * The ? used to open a `mailto:` carrying two facts and a polite request that
 * the user go and find `shellmate.log` themselves. Almost nobody did, so what
 * arrived was "it didn't work" and the first reply was always the same four
 * questions.
 *
 * This gathers those four answers, and shows every one of them before
 * anything leaves. That is the part worth defending: the manual already tells
 * people to read the log before sending it, and an instruction nobody follows
 * is worse than a preview nobody can avoid.
 *
 * `mailto:` cannot carry an attachment, so the bundle is written as one zip
 * into the data folder and the mail names it. One file to attach is the
 * version of that people actually complete.
 */
(function () {
  'use strict';

  const SUPPORT_EMAIL = 'support@foundry-ns.com';

  let overlay, listEl, noteEl, statusEl;
  let sections = [];
  /** Section id -> the collected text, once previewed. */
  let previews = {};

  document.addEventListener('DOMContentLoaded', () => {
    overlay = document.getElementById('support-overlay');
    if (!overlay) return;

    listEl   = document.getElementById('support-sections');
    noteEl   = document.getElementById('support-note');
    statusEl = document.getElementById('support-status');

    document.getElementById('support-close').addEventListener('click', close);
    overlay.addEventListener('click', (e) => { if (e.target === overlay) close(); });
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && !overlay.classList.contains('hidden')) close();
    });

    document.getElementById('support-build').addEventListener('click', build);
    document.getElementById('support-folder').addEventListener('click', reveal);
    document.getElementById('support-feedback').addEventListener('click', feedback);
  });

  async function open() {
    overlay.classList.remove('hidden');
    report('');
    previews = {};
    try {
      const res = await fetch('/api/support/sections');
      const data = await res.json();
      sections = data.sections || [];
      render();
    } catch (e) {
      report('Could not work out what can be gathered: ' + e.message, true);
    }
    setTimeout(() => noteEl && noteEl.focus(), 60);
  }

  function close() { overlay.classList.add('hidden'); }

  function render() {
    listEl.innerHTML = '';
    sections.forEach(section => {
      const row = document.createElement('div');
      row.className = 'setting-row support-row';

      const label = document.createElement('label');
      label.className = 'support-choice';

      const box = document.createElement('input');
      box.type = 'checkbox';
      box.checked = section.default_on;
      box.dataset.section = section.id;

      const text = document.createElement('span');
      text.className = 'support-text';

      const title = document.createElement('span');
      title.className = 'setting-label support-title';
      title.textContent = section.label;

      // Said on the row rather than in a note underneath: which of these
      // describe your estate, as opposed to ShellMate, is the only question
      // anyone needs to think about here.
      if (section.device_data) {
        const tag = document.createElement('span');
        tag.className = 'support-tag';
        tag.textContent = 'about your devices';
        title.appendChild(tag);
      }

      const summary = document.createElement('span');
      summary.className = 'settings-section-hint support-summary';
      summary.textContent = section.summary;

      text.append(title, summary);
      label.append(box, text);

      const view = document.createElement('button');
      view.type = 'button';
      view.className = 'btn-tertiary support-view';
      view.textContent = 'Preview';
      view.addEventListener('click', () => preview(section));

      row.append(label, view);
      listEl.appendChild(row);
    });
  }

  function chosen() {
    return [...listEl.querySelectorAll('input[type=checkbox]:checked')]
      .map(box => box.dataset.section);
  }

  /** Show exactly what would be sent for one section. */
  async function preview(section) {
    report('Gathering…');
    try {
      const res = await fetch('/api/support/preview', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ sections: [section.id] }),
      });
      const data = await res.json();
      previews[section.id] = (data.sections || {})[section.id] || '(nothing)';
      report('');
      showPreview(section, previews[section.id]);
    } catch (e) {
      report('Could not gather that: ' + e.message, true);
    }
  }

  function showPreview(section, text) {
    const box = document.getElementById('support-preview');
    const title = document.getElementById('support-preview-title');
    const body = document.getElementById('support-preview-body');
    if (!box) return;

    title.textContent = `Preview — ${section.label}`;
    // textContent — this is a log, a config or device output, and none of it
    // is ours to trust as markup.
    body.textContent = text;
    box.hidden = false;
    box.scrollIntoView({ block: 'nearest' });
    body.scrollTop = 0;
  }

  async function build() {
    const picked = chosen();
    if (!picked.length) {
      report('Choose at least one thing to include.', true);
      return;
    }

    report('Building…');
    try {
      const res = await fetch('/api/support/bundle', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ sections: picked, note: noteEl.value || '' }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || `server returned ${res.status}`);

      const kb = Math.max(1, Math.round(data.bytes / 1024));
      const name = data.path.split(/[\\/]/).pop();
      report(`Saved ${name} (${kb} KB). Attach it to the email.`);

      document.getElementById('support-folder').classList.remove('hidden');
      openMail(data.path);
    } catch (e) {
      report('Could not build the bundle: ' + e.message, true);
    }
  }


  /**
   * Write to the developer about anything that is not a fault.
   *
   * The bundle flow asks for a log file and a description of what went wrong,
   * which is the right shape for a bug and the wrong one for an idea. Anyone
   * with a suggestion either had to dress it up as a fault report or not send
   * it — so this is deliberately the opposite: no attachments, no gathering,
   * and a template that reads like writing to a person.
   *
   * Two version facts in the footer so a reply can be accurate. Anything more
   * turns it back into a support request.
   */
  async function feedback() {
    let build = 'unknown build';
    try {
      const res = await fetch('/api/system/info');
      const info = await res.json();
      build = info.portable ? 'portable build' : 'running from source';
    } catch (_) { /* the mail still opens without it */ }

    const body = [
      'Hello,',
      '',
      '',
      '',
      '---',
      `Sent from ShellMate Portable (${build}). Nothing else is attached.`,
    ].join('\n');

    window.location.href =
      `mailto:${SUPPORT_EMAIL}` +
      `?subject=${encodeURIComponent('ShellMate — feedback')}` +
      `&body=${encodeURIComponent(body)}`;

    report('Opening your email. Nothing was gathered or attached.');
  }

  function openMail(bundlePath) {
    const body = [
      'What I was doing:',
      '',
      '',
      'What happened instead:',
      '',
      '',
      '---',
      'A diagnostic bundle has been saved to:',
      bundlePath,
      '',
      'Please attach it to this email. Everything in it was chosen and',
      'previewed before it was written.',
    ].join('\n');

    window.location.href =
      `mailto:${SUPPORT_EMAIL}` +
      `?subject=${encodeURIComponent('ShellMate Portable — support request')}` +
      `&body=${encodeURIComponent(body)}`;
  }

  async function reveal() {
    try {
      const res = await fetch('/api/support/reveal', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({}),
      });
      const data = await res.json();
      if (!data.opened) report(`The bundles are in ${data.folder}`);
    } catch (e) {
      report('Could not open the folder: ' + e.message, true);
    }
  }

  function report(text, isError) {
    if (!statusEl) return;
    statusEl.textContent = text || '';
    statusEl.classList.toggle('field-warn', !!isError);
  }

  window.openSupport = open;
})();
