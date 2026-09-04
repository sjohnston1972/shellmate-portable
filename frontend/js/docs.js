/**
 * docs.js — The built-in manual, and the support link.
 *
 * The documentation ships inside the executable and is rendered locally,
 * because a tool whose whole point is working on an air-gapped network cannot
 * have a Docs button that needs the internet.
 */
(function () {
  'use strict';

  /** Order matters: this is the order someone should read them in. */
  const PAGES = [
    { file: 'getting-started.md',   title: 'Getting started',      icon: 'terminal' },
    { file: 'whats-new.md',         title: "What's new",          icon: 'bolt' },
    { file: 'connecting.md',        title: 'Connecting',           icon: 'add_circle' },
    { file: 'device-awareness.md',  title: 'Device awareness',     icon: 'smart_toy' },
    { file: 'history-and-drift.md', title: 'History and drift',    icon: 'search' },
    { file: 'alerts.md',            title: 'Alerts',               icon: 'warning' },
    { file: 'credentials.md',       title: 'Credentials',          icon: 'bookmark_add' },
    { file: 'ssh-keys.md',          title: 'SSH keys',             icon: 'key' },
    { file: 'assistant.md',         title: 'AI assistant',         icon: 'smart_toy' },
    { file: 'automation.md',        title: 'Broadcast and the API', icon: 'list_alt' },
    { file: 'ansible.md',           title: 'Ansible',              icon: 'automation' },
    { file: 'settings.md',          title: 'Settings',             icon: 'settings' },
    { file: 'troubleshooting.md',   title: 'Troubleshooting',      icon: 'help' },
    // Last, but it has to be here rather than only in the repository: the
    // bundled manual is the only documentation available offline, and an
    // attribution nobody can reach has not really been given.
    { file: 'legal.md',             title: 'Legal and licences',   icon: 'gavel' },
  ];

  const SUPPORT_EMAIL = 'support@foundry-ns.com';

  let overlay, nav, content, search, countEl;
  let hitIndex = -1;
  const cache = {};

  document.addEventListener('DOMContentLoaded', () => {
    overlay = document.getElementById('docs-overlay');
    nav     = document.getElementById('docs-nav');
    content = document.getElementById('docs-content');
    search  = document.getElementById('docs-search');
    if (!overlay) return;

    document.getElementById('sidebar-link-docs')
      .addEventListener('click', (e) => { e.preventDefault(); open(); });

    // The support panel owns this now (#53). It gathers what the first reply
    // always asks for, and shows all of it before anything is sent.
    document.getElementById('sidebar-link-support')
      .addEventListener('click', (e) => {
        e.preventDefault();
        if (typeof window.openSupport === 'function') window.openSupport();
        else contactSupport();
      });

    document.getElementById('docs-close').addEventListener('click', close);
    overlay.addEventListener('click', (e) => { if (e.target === overlay) close(); });
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && !overlay.classList.contains('hidden')) close();
    });

    search.addEventListener('input', () => filter(search.value.trim().toLowerCase()));
    // Enter steps through the hits on the page; Escape clears (#436).
    search.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); stepHit(e.shiftKey ? -1 : 1); }
      if (e.key === 'Escape') { e.preventDefault(); search.value = ''; filter(''); }
    });
    // The count sits beside the box.
    const wrap = document.createElement('span');
    wrap.className = 'docs-search-wrap';
    search.parentNode.insertBefore(wrap, search);
    wrap.appendChild(search);
    countEl = document.createElement('span');
    countEl.className = 'docs-search-count';
    wrap.appendChild(countEl);

    buildNav();
  });

  function buildNav() {
    nav.innerHTML = '';
    PAGES.forEach((page, index) => {
      const item = document.createElement('button');
      item.type = 'button';
      item.className = 'docs-nav-item';
      item.dataset.index = String(index);

      const icon = document.createElement('span');
      icon.className = 'material-symbols-outlined';
      icon.textContent = page.icon;

      const label = document.createElement('span');
      label.textContent = page.title;

      item.append(icon, label);
      item.addEventListener('click', () => show(index));
      nav.appendChild(item);
    });
  }

  async function open() {
    overlay.classList.remove('hidden');
    await show(0);
  }

  function close() { overlay.classList.add('hidden'); }

  async function load(file) {
    if (cache[file]) return cache[file];
    const res = await fetch(`/static/docs/${file}`);
    if (!res.ok) throw new Error(`could not load ${file}`);
    cache[file] = await res.text();
    return cache[file];
  }

  async function show(index) {
    nav.querySelectorAll('.docs-nav-item').forEach((item, i) => {
      item.classList.toggle('active', i === index);
    });

    content.innerHTML = '<div class="docs-loading">Loading…</div>';

    try {
      const source = await load(PAGES[index].file);
      const { html } = window.shellmateMarkdown.render(source);
      content.innerHTML = html;
      content.scrollTop = 0;
      wireInternalLinks();
      highlight(search ? search.value.trim().toLowerCase() : '');
    } catch (e) {
      content.innerHTML = '';
      const err = document.createElement('div');
      err.className = 'docs-loading';
      err.textContent = `Could not load this page: ${e.message}`;
      content.appendChild(err);
    }
  }

  /**
   * Make cross-page links work.
   *
   * The documents link to each other with anchors like `#device-awareness`,
   * which are page names rather than headings within the current page.
   */
  function wireInternalLinks() {
    content.querySelectorAll('a[href^="#"]').forEach(link => {
      const target = link.getAttribute('href').slice(1).toLowerCase();
      const index = PAGES.findIndex(p =>
        p.file.replace('.md', '') === target ||
        p.title.toLowerCase().replace(/\s+/g, '-') === target);
      if (index >= 0) {
        link.addEventListener('click', (e) => { e.preventDefault(); show(index); });
      }
    });
  }

  /**
   * Search every page, not just the one on screen.
   *
   * Loads them all on first use — they are a few kilobytes each and already
   * on disk, so there is nothing to be gained by being clever about it.
   */
  async function filter(query) {
    nav.querySelectorAll('.docs-nav-count').forEach(el => el.remove());
    if (!query) {
      nav.querySelectorAll('.docs-nav-item').forEach(i => i.classList.remove('hidden'));
      highlight('');
      return;
    }

    await Promise.all(PAGES.map(p => load(p.file).catch(() => '')));

    let firstIndex = -1;
    nav.querySelectorAll('.docs-nav-item').forEach((item, i) => {
      const text = (cache[PAGES[i].file] || '').toLowerCase();
      const hits = count(text, query);
      const matches = hits > 0 || PAGES[i].title.toLowerCase().includes(query);
      item.classList.toggle('hidden', !matches);
      if (matches && firstIndex < 0) firstIndex = i;
      if (hits) {
        // How many hits each page has, so the nav says where to look (#436).
        const badge = document.createElement('span');
        badge.className = 'docs-nav-count';
        badge.textContent = String(hits);
        item.appendChild(badge);
      }
    });

    const active = nav.querySelector('.docs-nav-item.active');
    const activeVisible = active && !active.classList.contains('hidden');
    if (activeVisible) highlight(query);
    else if (firstIndex >= 0) show(firstIndex);
    else if (countEl) countEl.textContent = 'no matches';
  }

  function count(text, query) {
    if (!query) return 0;
    let n = 0, at = 0;
    while ((at = text.indexOf(query, at)) !== -1) { n += 1; at += query.length; }
    return n;
  }

  /**
   * Wrap every occurrence on the rendered page in <mark> (#436). Text nodes
   * only, so markup is never re-parsed, and skipping <mark>s already made.
   */
  function highlight(query) {
    content.querySelectorAll('mark.search-hit').forEach(m => {
      m.replaceWith(document.createTextNode(m.textContent));
    });
    content.normalize();
    hitIndex = -1;
    if (!query) { if (countEl) countEl.textContent = ''; return; }
    const walker = document.createTreeWalker(content, NodeFilter.SHOW_TEXT);
    const nodes = [];
    for (let n = walker.nextNode(); n; n = walker.nextNode()) nodes.push(n);
    let total = 0;
    nodes.forEach(node => {
      const text = node.nodeValue;
      const lower = text.toLowerCase();
      if (!lower.includes(query)) return;
      const frag = document.createDocumentFragment();
      let at = 0, found;
      while ((found = lower.indexOf(query, at)) !== -1) {
        if (found > at) frag.appendChild(document.createTextNode(text.slice(at, found)));
        const mark = document.createElement('mark');
        mark.className = 'search-hit';
        mark.textContent = text.slice(found, found + query.length);
        frag.appendChild(mark);
        at = found + query.length;
        total += 1;
      }
      if (at < text.length) frag.appendChild(document.createTextNode(text.slice(at)));
      node.replaceWith(frag);
    });
    if (countEl) countEl.textContent = total ? `${total} on this page` : 'not on this page';
    if (total) stepHit(1);
  }

  /** Move to the next or previous hit on the page. */
  function stepHit(delta) {
    const marks = [...content.querySelectorAll('mark.search-hit')];
    if (!marks.length) return;
    marks.forEach(m => m.classList.remove('search-current'));
    hitIndex = (hitIndex + delta + marks.length) % marks.length;
    const mark = marks[hitIndex];
    mark.classList.add('search-current');
    mark.scrollIntoView({ block: 'center' });
    if (countEl) countEl.textContent = `${hitIndex + 1} of ${marks.length} on this page`;
  }

  /**
   * Open a mail composer with the details support will ask for anyway.
   *
   * Deliberately does not attach the log: that file is the user's to read and
   * decide about before it leaves their organisation.
   */
  async function contactSupport() {
    let info = {};
    try {
      const res = await fetch('/api/system/info');
      if (res.ok) info = await res.json();
    } catch (_) { /* the mail still opens without it */ }

    const body = [
      'What I was doing:',
      '',
      '',
      'What happened instead:',
      '',
      '',
      '---',
      `Portable build: ${info.portable ? 'yes' : 'no (running from source)'}`,
      `Built: ${info.built || 'unknown'}`,
      `Data folder: ${info.data_dir || 'unknown'}`,
      '',
      'The log is at ShellMate-Data/shellmate.log — please read it through and',
      'attach it if you are happy to.',
    ].join('\n');

    window.location.href =
      `mailto:${SUPPORT_EMAIL}` +
      `?subject=${encodeURIComponent('ShellMate Portable — support request')}` +
      `&body=${encodeURIComponent(body)}`;
  }

  window.openDocs = open;
  /** Open the manual at a page, by file name — for the what's-new toast (#441). */
  window.openDocsPage = async (file) => {
    const index = PAGES.findIndex(p => p.file === file);
    overlay.classList.remove('hidden');
    await show(index >= 0 ? index : 0);
  };
})();
