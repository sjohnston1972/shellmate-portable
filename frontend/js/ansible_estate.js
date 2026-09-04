/**
 * ansible_estate.js — One source for the estate, shared by the areas that need it (#601).
 *
 * The nested canvas shipped with its own inventory rail: a bespoke list of
 * group names, built for the builder alone, next to an Inventory area that
 * already knew about groups, subgroups, connections and their platform
 * mappings. That is the same defect as the runner block the Playbooks area
 * used to repeat — a second implementation of something that exists, which
 * is a second place for it to be wrong and a second thing to keep in step.
 *
 * So this holds the estate once and renders it once. The Inventory area
 * reads its data; the builder renders its tree beside the canvas and drags
 * out of it.
 *
 * Two details it exists to get right in one place rather than two:
 *
 * - **Two names for one group.** ShellMate calls it `Site-1/Routers` and
 *   Ansible will call it `site_1_routers`. The tree shows the first and
 *   carries the second, because an interface that can only show the
 *   mangled form is asking somebody to recognise their own estate through
 *   a transformation ShellMate performed.
 * - **What is missing and why.** A serial connection has no address for
 *   Ansible to dial, so it is not in the inventory at all. It still
 *   appears here, greyed and unusable, with its reason — a device that
 *   simply is not in the list reads as a bug in ShellMate rather than as a
 *   fact about the connection.
 */

(function () {
  'use strict';

  const view = window.ansibleView;
  if (!view) return;
  const { el, icon } = view;

  /** The last inventory read, shared by every caller. */
  let estate = null;
  let pending = null;

  /**
   * The estate, fetched at most once until something says otherwise.
   *
   * Concurrent callers share one request rather than racing: the builder
   * and the Inventory area both ask on the same view-open, and two
   * identical fetches would be two answers that can disagree.
   */
  async function load(force) {
    if (estate && !force) return estate;
    if (pending && !force) return pending;
    pending = view.json('/api/ansible/inventory')
      .then((data) => { estate = data; pending = null; return data; })
      .catch((error) => {
        pending = null;
        estate = { groups: {}, group_names: {}, hostvars: {}, hosts: [],
                   skipped: [], error: String(error.message || error) };
        return estate;
      });
    return pending;
  }

  function known() {
    return estate;
  }

  /**
   * The groups as a tree, nested on the separator ShellMate already uses.
   *
   * `site-1/routers` is a subgroup of `site-1` because the name says so —
   * nesting is the name, everywhere else in ShellMate, and inventing a
   * different structure here would make the same estate look like two
   * different shapes on two screens.
   */
  function tree(data) {
    const groups = (data && data.groups) || {};
    const labels = (data && data.group_names) || {};
    const root = { children: new Map(), hosts: [], key: '', label: '' };

    Object.keys(groups).sort().forEach((ansibleName) => {
      const display = labels[ansibleName] || ansibleName;
      let node = root;
      let path = '';
      display.split('/').forEach((part, depth, all) => {
        path = path ? `${path}/${part}` : part;
        if (!node.children.has(part)) {
          node.children.set(part, {
            children: new Map(), hosts: [], key: '', label: part, path,
          });
        }
        node = node.children.get(part);
        if (depth === all.length - 1) {
          // Only the leaf is a real Ansible group; the segments above it
          // may be bare path pieces nothing was ever tagged with.
          node.key = ansibleName;
          node.hosts = groups[ansibleName] || [];
        }
      });
    });
    return root;
  }

  /** What a host is called, falling back to the address it is dialled on. */
  function hostLabel(data, address) {
    const vars = ((data && data.hostvars) || {})[address] || {};
    return vars.shellmate_name && vars.shellmate_name !== address
      ? vars.shellmate_name : address;
  }

  /**
   * Render the estate as a pickable, draggable tree.
   *
   * @param {object} opts
   * @param {Function} [opts.onPick]  Called with {kind, label, target, hosts}
   *   when an item is clicked. `target` is what a play should be pointed at.
   * @param {boolean} [opts.draggable] Whether items can be dragged out.
   * @param {boolean} [opts.showSkipped] Whether to list what was left out.
   */
  function render(host, data, opts) {
    const options = opts || {};
    const root = tree(data);

    function item(kind, label, target, hosts, extra) {
      const node = el('button', {
        type: 'button',
        class: `av-est-item av-est-${kind}${extra && extra.disabled ? ' av-est-off' : ''}`,
        title: extra && extra.why ? extra.why
          : (kind === 'host' ? `Target ${label} alone`
                             : `Target ${target} — ${hosts.length} host`
                               + `${hosts.length === 1 ? '' : 's'}`),
        disabled: !!(extra && extra.disabled),
        // An enumerated attribute, not a boolean one: draggable="" means
        // "auto", which is not draggable. It has to be the word.
        draggable: (options.draggable && !(extra && extra.disabled))
          ? 'true' : null,
        onclick: () => options.onPick
          && options.onPick({ kind, label, target, hosts: hosts.slice() }),
        ondragstart: (event) => {
          // Plain text as well as the typed payload: a drop handler that
          // only reads the custom type silently ignores a drag from
          // anywhere else, and plain text costs nothing to include.
          const payload = JSON.stringify({ kind, label, target, hosts });
          event.dataTransfer.setData('application/x-shellmate-target', payload);
          event.dataTransfer.setData('text/plain', target);
          event.dataTransfer.effectAllowed = 'copy';
        },
      }, [
        icon(kind === 'host' ? 'dns' : 'lan'),
        el('span', { class: 'av-est-label', text: label }),
        kind === 'group'
          ? el('span', { class: 'av-est-count', text: String(hosts.length) })
          : null,
      ]);
      return node;
    }

    function walk(node, into, depth) {
      Array.from(node.children.values())
        .sort((a, b) => a.label.localeCompare(b.label))
        .forEach((child) => {
          const wrap = el('div', { class: 'av-est-branch',
                                   style: `--av-est-depth:${depth}` });
          wrap.appendChild(item('group', child.label, child.key || child.path,
                                child.hosts));
          if (child.hosts.length) {
            child.hosts.forEach(address => wrap.appendChild(
              el('div', { class: 'av-est-branch',
                          style: `--av-est-depth:${depth + 1}` },
                 item('host', hostLabel(data, address), address, [address]))));
          }
          walk(child, wrap, depth + 1);
          into.appendChild(wrap);
        });
    }

    view.clear(host);
    host.appendChild(el('h4', { class: 'av-rail-title', text: 'Inventory' }));

    if (data && data.error) {
      host.appendChild(el('p', { class: 'av-rail-note', text: data.error }));
      return;
    }
    if (!Object.keys((data && data.groups) || {}).length) {
      host.appendChild(el('p', { class: 'av-rail-note' },
        'No groups with reachable connections yet. A play can still target a '
        + 'host pattern typed by hand.'));
    }

    const body = el('div', { class: 'av-est-tree' });
    walk(root, body, 0);
    host.appendChild(body);

    // Named rather than omitted: a device that is simply absent reads as a
    // bug in ShellMate, not as a fact about the connection.
    const skipped = (data && data.skipped) || [];
    if (options.showSkipped !== false && skipped.length) {
      host.appendChild(el('div', { class: 'av-est-skipped' }, [
        el('h5', { class: 'av-est-skipped-title',
                   text: `${skipped.length} left out` }),
        ...skipped.map(x => el('div', {
          class: 'av-est-item av-est-off', title: x.why,
        }, [icon('cancel'), el('span', { class: 'av-est-label', text: x.name })])),
      ]));
    }

    host.appendChild(el('p', { class: 'av-rail-note' },
      options.note || 'Managed separately, referenced by plays.'));
  }

  /**
   * Accept a drop of an estate item.
   *
   * Returns what was dropped, or null when the drag came from somewhere
   * else — so a caller can ignore it rather than guess.
   */
  function dropped(event) {
    const raw = event.dataTransfer
      && event.dataTransfer.getData('application/x-shellmate-target');
    if (!raw) return null;
    try {
      return JSON.parse(raw);
    } catch (e) {
      return null;
    }
  }

  /**
   * Take an unfiltered read somebody else already made.
   *
   * The Inventory area fetches the whole estate to draw its table; making
   * this module fetch it again would be two requests and two answers that
   * can disagree about the same moment. Only an unfiltered read is
   * accepted — a group-filtered one is not the estate.
   */
  function adopt(data) {
    if (data && data.groups) estate = data;
  }

  window.ansibleEstate = { load, known, adopt, tree, render, dropped, hostLabel };
})();
