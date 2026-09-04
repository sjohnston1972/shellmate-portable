/**
 * ansible_builder.js — Describe a change, get a playbook — with the assistant if one is configured (#586).
 *
 * Registered with the view rather than wired to the markup directly, so
 * this area can be built and replaced without touching the other seven.
 * Until it is built, it says so — an area that renders nothing at all
 * reads as a page that failed to load.
 */

(function () {
  'use strict';

  const view = window.ansibleView;
  if (!view) return;

  function render() {
    const body = document.getElementById('av-builder-body');
    if (!body) return;
    view.clear(body);
    body.appendChild(view.empty('Being built.'));
  }

  view.area('builder', { onShow: render });
})();
