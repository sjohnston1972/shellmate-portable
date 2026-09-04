/**
 * ansible_estate.js — Turning something dragged out of the tree into a target (#601).
 *
 * The builder shipped with its own inventory rail, and then with a second
 * attempt at one shared with the Inventory area. Both were wrong for the
 * same reason: ShellMate already has a directory of the estate — the group
 * tree down the left-hand side, with the sites, the subgroups and the
 * devices in them — and it is on screen while the Ansible view is open.
 * Building another list of the same things is a second place for it to be
 * wrong and a second thing to keep in step.
 *
 * So there is no tree in here. The real one already publishes what is being
 * dragged, in its own MIME types, for moving connections between groups:
 *
 *   application/x-shellmate-group      the group's key, `site-1/routers`
 *   application/x-shellmate-profile    one connection's id
 *   application/x-shellmate-profiles   several, as JSON
 *
 * All this does is translate those into something a play can target, which
 * needs two facts the tree does not carry:
 *
 * - **Ansible's name for a group.** The tree knows `site-1/routers`;
 *   Ansible will call it `site_1_routers`. The mapping comes from the
 *   inventory rather than being re-derived here, because a second
 *   implementation of the sanitising rule would drift from the first, and
 *   the failure would be a play targeting a group that does not exist.
 * - **A connection's address.** The tree drags profile ids; Ansible dials
 *   addresses. The inventory carries both, so the lookup is local.
 *
 * A connection with no address — a serial console — resolves to nothing and
 * says why. Dragging one and getting a play targeting an empty string would
 * be worse than being told it cannot be done.
 */

(function () {
  'use strict';

  const view = window.ansibleView;
  if (!view) return;

  /** The last inventory read, shared by every caller. */
  let estate = null;
  let pending = null;

  /**
   * The estate, fetched at most once until something says otherwise.
   *
   * Concurrent callers share one request rather than racing: two identical
   * fetches would be two answers that can disagree about the same moment.
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
   * Take an unfiltered read somebody else already made.
   *
   * The Inventory area fetches the whole estate to draw its table; making
   * this module fetch it again would be two requests and two answers that
   * can disagree. Only an unfiltered read is accepted — a group-filtered
   * one is not the estate.
   */
  function adopt(data) {
    if (data && data.groups) estate = data;
  }

  /** Ansible's name for the group ShellMate calls `key`, or '' if unknown. */
  function ansibleGroup(key) {
    const names = (estate && estate.group_names) || {};
    const wanted = String(key || '').toLowerCase();
    return Object.keys(names).find(
      ansibleName => String(names[ansibleName]).toLowerCase() === wanted) || '';
  }

  /** The address and display name for a connection id, or null. */
  function host(profileId) {
    const vars = (estate && estate.hostvars) || {};
    const address = Object.keys(vars).find(
      addr => vars[addr].shellmate_id === profileId);
    return address
      ? { address, label: vars[address].shellmate_name || address }
      : null;
  }

  /**
   * What was dropped, as something a play can target.
   *
   * Returns null when the drag came from somewhere else, so a caller can
   * ignore it rather than guess — and a `{why}` with no `target` when the
   * thing dragged genuinely cannot be one, so the caller can say so instead
   * of appearing to do nothing.
   */
  function resolveDrop(event) {
    const data = event.dataTransfer;
    if (!data) return null;

    const groupKey = data.getData('application/x-shellmate-group');
    if (groupKey) {
      const name = ansibleGroup(groupKey);
      if (!name) {
        return { why: `"${groupKey}" has no connections Ansible can reach, so `
                    + 'there is nothing for a play to target.' };
      }
      return { kind: 'group', label: groupKey, target: name };
    }

    // Several at once, which the tree already supports and which is exactly
    // how somebody would target four switches out of a site.
    const batch = data.getData('application/x-shellmate-profiles');
    if (batch) {
      let rows = [];
      try {
        rows = JSON.parse(batch) || [];
      } catch (e) {
        rows = [];
      }
      const found = rows.map(r => host(r.id)).filter(Boolean);
      if (!found.length) {
        return { why: 'None of those connections has an address Ansible can '
                    + 'dial. A serial console cannot be a target.' };
      }
      return {
        kind: 'hosts',
        label: found.map(f => f.label).join(', '),
        target: found.map(f => f.address).join(','),
        partial: found.length < rows.length
          ? `${rows.length - found.length} left out — no address to dial.` : '',
      };
    }

    const profileId = data.getData('application/x-shellmate-profile');
    if (profileId) {
      const found = host(profileId);
      if (!found) {
        return { why: 'That connection has no address Ansible can dial — a '
                    + 'serial console has nothing to reach over the network.' };
      }
      return { kind: 'host', label: found.label, target: found.address };
    }
    return null;
  }

  /** Whether a drag came from the tree at all. */
  function isEstateDrag(event) {
    const types = (event.dataTransfer && event.dataTransfer.types) || [];
    return Array.from(types).some(t => t.startsWith('application/x-shellmate-'));
  }

  window.ansibleEstate = { load, known, adopt, ansibleGroup, host,
                           resolveDrop, isEstateDrag };
})();
