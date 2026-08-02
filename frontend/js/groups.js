/**
 * groups.js — Groups on the dashboard.
 *
 * A group is a tag with a face. Membership is the tag, so everything that
 * already understood tags — the tab strip's grouping, `/api/tags/{tag}/connect`,
 * search — understands groups without being told.
 *
 * This replaces the tag chip row. The two were the same feature seen twice:
 * chips filtered the grid by tag, and a group tile filters the grid by tag.
 * Keeping both would have meant two controls doing one job, differing only in
 * that one of them could be given a colour.
 *
 * **Diving in is a filter, not a navigation.** Nothing is torn down and no
 * session is touched — the grid narrows and a way back appears. That matters
 * because the dashboard is now reachable with twelve tabs running, and moving
 * around it must never cost a session.
 */
(function () {
  'use strict';

  /** The group currently opened, by key. '' means the whole dashboard. */
  let activeGroup = '';

  /** Cached so a re-render does not wait on a request. */
  let groupCache = [];
  let colours = ['slate'];

  document.addEventListener('DOMContentLoaded', () => {
    const button = document.getElementById('btn-new-group');
    if (button) button.addEventListener('click', () => newGroup());
  });

  // -------------------------------------------------------------------------
  // State
  // -------------------------------------------------------------------------

  function active() { return activeGroup; }

  function activeName() {
    const group = groupCache.find(g => g.key === activeGroup);
    return group ? group.name : activeGroup;
  }

  function open(key) {
    activeGroup = (activeGroup === key) ? '' : key;
    if (typeof window.renderWelcomeProfiles === 'function') {
      window.renderWelcomeProfiles();
    }
  }

  // -------------------------------------------------------------------------
  // Rendering
  // -------------------------------------------------------------------------

  /**
   * Draw the group row.
   *
   * Called from renderWelcomeProfiles with the profiles it already fetched, so
   * this costs one request for the groups rather than two for everything.
   */
  async function render(profiles) {
    const host = document.getElementById('welcome-groups');
    if (!host) return;

    try {
      const res = await fetch('/api/groups');
      const data = res.ok ? await res.json() : { groups: [] };
      groupCache = data.groups || [];
      if (data.colours && data.colours.length) colours = data.colours;
    } catch (_) {
      groupCache = [];
    }

    // A group that was open and has since been deleted must not leave the
    // dashboard filtered to nothing with no way back.
    if (activeGroup && !groupCache.some(g => g.key === activeGroup)) {
      activeGroup = '';
    }

    host.innerHTML = '';

    if (activeGroup) {
      host.appendChild(_insideGroup(profiles));
      return;
    }

    if (!groupCache.length) {
      host.classList.add('welcome-groups-empty');
      const hint = document.createElement('span');
      hint.className = 'welcome-groups-hint';
      hint.textContent = 'No groups yet — make one to arrange your connections.';
      host.appendChild(hint);
      return;
    }

    host.classList.remove('welcome-groups-empty');
    groupCache.forEach(group => host.appendChild(_tile(group)));
  }

  /** One group tile. */
  function _tile(group) {
    const tile = document.createElement('div');
    tile.className = `group-tile group-${group.colour}`
                   + (group.favourite ? ' group-favourite' : '');
    tile.dataset.key = group.key;
    tile.draggable = true;

    const name = document.createElement('button');
    name.type = 'button';
    name.className = 'group-tile-open';
    name.addEventListener('click', () => open(group.key));

    const label = document.createElement('span');
    label.className = 'group-tile-name';
    // textContent — a group name is user input.
    label.textContent = group.name;

    const count = document.createElement('span');
    count.className = 'group-tile-count';
    count.textContent = group.count;

    name.append(label, count);

    const star = document.createElement('button');
    star.type = 'button';
    star.className = 'group-tile-star';
    star.title = group.favourite ? 'Remove from favourites' : 'Favourite';
    // One icon, two states. The difference is the font's FILL axis, applied
    // in CSS — a ternary between two identical names, which is what this was,
    // renders both states the same and reads as though it distinguishes them.
    star.innerHTML = '<span class="material-symbols-outlined">bookmark_add</span>';
    star.addEventListener('click', async (e) => {
      e.stopPropagation();
      await _update(group.key, { favourite: !group.favourite });
    });

    const more = document.createElement('button');
    more.type = 'button';
    more.className = 'group-tile-more';
    more.title = 'Rename, recolour or delete';
    more.innerHTML = '<span class="material-symbols-outlined">tune</span>';
    more.addEventListener('click', (e) => {
      e.stopPropagation();
      _tileMenu(e, group);
    });

    tile.append(name, star, more);
    _bindDrag(tile, group);
    return tile;
  }

  /** The header shown while a group is open. */
  function _insideGroup(profiles) {
    const bar = document.createElement('div');
    bar.className = 'group-open-bar';

    const group = groupCache.find(g => g.key === activeGroup) || {};

    const back = document.createElement('button');
    back.type = 'button';
    back.className = 'group-back';
    back.innerHTML = '<span class="material-symbols-outlined">keyboard_arrow_up</span>'
                   + 'All groups';
    back.addEventListener('click', () => open(activeGroup));

    const title = document.createElement('span');
    title.className = `group-open-name group-${group.colour || 'slate'}`;
    title.textContent = group.name || activeGroup;

    const inGroup = profiles.filter(
      p => (p.tags || []).includes(activeGroup)).length;

    const count = document.createElement('span');
    count.className = 'group-open-count';
    count.textContent = `${inGroup} connection${inGroup === 1 ? '' : 's'}`;

    bar.append(back, title, count);

    // Opening a whole group is the point of having one. Offered only from
    // inside it — "connect all" over an unfiltered two hundred is not
    // something to put one click away.
    if (inGroup) {
      const openAll = document.createElement('button');
      openAll.type = 'button';
      openAll.className = 'btn-secondary btn-tiny';
      openAll.textContent = `Open all ${inGroup}`;
      openAll.addEventListener('click', () => {
        if (typeof window.connectTag === 'function') {
          window.connectTag(activeGroup, inGroup);
        }
      });
      bar.appendChild(openAll);
    }

    const edit = document.createElement('button');
    edit.type = 'button';
    edit.className = 'group-tile-more';
    edit.title = 'Rename, recolour or delete';
    edit.innerHTML = '<span class="material-symbols-outlined">tune</span>';
    edit.addEventListener('click', (e) => _tileMenu(e, group));
    bar.appendChild(edit);

    return bar;
  }

  // -------------------------------------------------------------------------
  // Creating and editing
  // -------------------------------------------------------------------------

  async function newGroup() {
    const answer = await window.shellmateDialog.form({
      title: 'New group',
      body:  'Groups arrange the dashboard. A connection can be in as many as '
             + 'you like — adding it to one never takes it out of another.',
      confirmLabel: 'Create',
      fields: [
        { name: 'name', label: 'Name', required: true, placeholder: 'Glasgow',
          hint: 'If you already tag connections with this name, they join it.' },
        { name: 'colour', label: 'Colour', type: 'select',
          options: colours.map(c => ({ value: c, label: _colourName(c) })) },
      ],
    });
    if (!answer) return;

    try {
      const res = await fetch('/api/groups', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify(answer),
      });
      if (!res.ok) throw new Error((await res.json()).detail || 'Could not create it.');
      const group = await res.json();
      // Straight into it, because the next thing anybody does after making a
      // group is put something in it.
      activeGroup = group.key;
      _refresh();
    } catch (e) {
      _warn('Could not create the group', e.message);
    }
  }

  async function editGroup(group) {
    const answer = await window.shellmateDialog.form({
      title: `Edit "${group.name}"`,
      confirmLabel: 'Save',
      fields: [
        { name: 'name', label: 'Name', value: group.name, required: true,
          hint: 'Renaming re-tags every connection in the group.' },
        { name: 'colour', label: 'Colour', type: 'select', value: group.colour,
          options: colours.map(c => ({ value: c, label: _colourName(c) })) },
        { name: 'favourite', label: 'Favourite', type: 'checkbox',
          value: group.favourite, hint: 'Favourites sit at the front.' },
      ],
    });
    if (!answer) return;
    await _update(group.key, answer);
  }

  async function deleteGroup(group) {
    // Named for what survives. Nothing else on the dashboard destroys
    // anything, and "delete group" reads like it takes the devices with it.
    const ok = await window.shellmateDialog.confirm({
      title: `Delete the group "${group.name}"?`,
      body:  group.count
        ? `The ${group.count} connection${group.count === 1 ? '' : 's'} in it are kept — `
          + 'they simply stop being grouped. Only the group and its colour go.'
        : 'It is empty, so nothing else changes.',
      confirmLabel: 'Delete the group',
      danger: true,
    });
    if (!ok) return;

    try {
      const res = await fetch(`/api/groups/${encodeURIComponent(group.key)}`,
                              { method: 'DELETE' });
      if (!res.ok) throw new Error('Could not delete it.');
      const data = await res.json();
      if (activeGroup === group.key) activeGroup = '';
      _refresh();
      if (window.shellmateAlerts && data.released) {
        window.shellmateAlerts.notify({
          title: `Group "${group.name}" deleted`,
          body:  `${data.released} connection${data.released === 1 ? '' : 's'} kept.`,
        });
      }
    } catch (e) {
      _warn('Could not delete the group', e.message);
    }
  }

  async function _update(key, changes) {
    try {
      const res = await fetch(`/api/groups/${encodeURIComponent(key)}`, {
        method:  'PUT',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify(changes),
      });
      if (!res.ok) throw new Error((await res.json()).detail || 'Could not save it.');
      const group = await res.json();
      // A rename changes the key, so anything open follows it.
      if (activeGroup === key) activeGroup = group.key;
      _refresh();
    } catch (e) {
      _warn('Could not update the group', e.message);
    }
  }

  /**
   * The tile's own menu.
   *
   * Reuses `.tab-context-menu`, which is already styled and already handles
   * the dismissal rules — a second menu that looked almost like the first
   * would be the kind of drift the interface has been kept free of.
   */
  function _tileMenu(event, group) {
    document.querySelectorAll('.group-menu').forEach(el => el.remove());

    const menu = document.createElement('div');
    menu.className = 'tab-context-menu group-menu';

    const item = (icon, text, onClick, danger) => {
      const button = document.createElement('button');
      button.type = 'button';
      if (danger) button.className = 'ctx-danger';
      button.innerHTML = `<span class="material-symbols-outlined">${icon}</span>`;
      button.appendChild(document.createTextNode(text));
      button.addEventListener('click', () => { menu.remove(); onClick(); });
      return button;
    };

    menu.appendChild(item('tune', 'Rename or recolour…', () => editGroup(group)));
    menu.appendChild(item(
      'bookmark_add',
      group.favourite ? 'Remove from favourites' : 'Add to favourites',
      () => _update(group.key, { favourite: !group.favourite })));

    const sep = document.createElement('div');
    sep.className = 'ctx-sep';
    menu.appendChild(sep);

    menu.appendChild(item('delete_forever', 'Delete group…',
                          () => deleteGroup(group), true));

    document.body.appendChild(menu);
    menu.style.left = `${event.clientX}px`;
    menu.style.top = `${event.clientY}px`;

    setTimeout(() => {
      document.addEventListener('click', () => menu.remove(), { once: true });
    }, 0);
  }

  // -------------------------------------------------------------------------
  // Dragging
  // -------------------------------------------------------------------------

  let dragKey = '';

  function _bindDrag(tile, group) {
    tile.addEventListener('dragstart', (e) => {
      dragKey = group.key;
      e.dataTransfer.effectAllowed = 'move';
      e.dataTransfer.setData('text/plain', group.key);
      tile.classList.add('group-dragging');
    });
    tile.addEventListener('dragend', () => {
      dragKey = '';
      tile.classList.remove('group-dragging');
      document.querySelectorAll('.group-drop').forEach(
        el => el.classList.remove('group-drop'));
    });

    tile.addEventListener('dragover', (e) => {
      e.preventDefault();
      tile.classList.add('group-drop');
    });
    tile.addEventListener('dragleave', () => tile.classList.remove('group-drop'));

    tile.addEventListener('drop', async (e) => {
      e.preventDefault();
      tile.classList.remove('group-drop');

      // A connection dropped on a group joins it. Adding, never moving — it
      // keeps every other group it was already in.
      const profileId = e.dataTransfer.getData('application/x-shellmate-profile');
      if (profileId) {
        await _addMember(group, profileId);
        return;
      }

      // Otherwise a group was dragged, and this is a rearrangement.
      if (!dragKey || dragKey === group.key) return;
      const keys = groupCache.map(g => g.key).filter(k => k !== dragKey);
      keys.splice(keys.indexOf(group.key), 0, dragKey);
      try {
        await fetch('/api/groups/order', {
          method:  'PUT',
          headers: { 'Content-Type': 'application/json' },
          body:    JSON.stringify({ keys }),
        });
      } catch (_) { /* the order is a preference, not the data */ }
      _refresh();
    });
  }

  async function _addMember(group, profileId) {
    try {
      const res = await fetch(
        `/api/groups/${encodeURIComponent(group.key)}/members`, {
          method:  'POST',
          headers: { 'Content-Type': 'application/json' },
          body:    JSON.stringify({ profile_id: profileId, member: true }),
        });
      if (!res.ok) throw new Error('Could not add it.');
      _refresh();
      if (window.shellmateAlerts) {
        window.shellmateAlerts.notify({
          title: `Added to "${group.name}"`,
          body:  'It keeps any other groups it was already in.',
        });
      }
    } catch (e) {
      _warn('Could not add it to the group', e.message);
    }
  }

  // -------------------------------------------------------------------------
  // Helpers
  // -------------------------------------------------------------------------

  function _refresh() {
    if (typeof window.renderWelcomeProfiles === 'function') {
      window.renderWelcomeProfiles();
    }
    // The tab strip carries group colours too (#140), and it has no other way
    // to know a colour changed.
    window.dispatchEvent(new CustomEvent('shellmate:groups-changed'));
  }

  function _colourName(key) {
    return key.charAt(0).toUpperCase() + key.slice(1);
  }

  function _warn(title, body) {
    if (window.shellmateAlerts) {
      window.shellmateAlerts.notify({
        severity: 'warning', icon: 'error', title, body: body || '',
      });
    }
  }

  window.shellmateGroups = {
    render, active, activeName, open, newGroup, editGroup, deleteGroup,
  };
})();
