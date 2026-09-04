/**
 * ansible_templates.js — Parameterised plays: the holes, the form that fills them, and what comes out (#586).
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
    const body = document.getElementById('av-templates-body');
    if (!body) return;
    view.clear(body);
    body.appendChild(view.empty('Being built.'));
  }

  view.area('templates', { onShow: render });
})();
