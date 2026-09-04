/**
 * ansible_tls.js — The TLS indicator, and the probe behind it (#586).
 *
 * The runner pill answers "can I use this". This answers the question
 * underneath it: **is anything checking who is on the other end?**
 *
 * They are separate lights because they fail separately, and the dangerous
 * combination is the one where the first is green. A runner that answers
 * everything correctly over a connection nothing verifies looks entirely
 * healthy, and the only thing that would ever say otherwise is a light that
 * is not measuring reachability.
 *
 * The probe runs on a timer through `shellmateVisibility.every`, so a
 * hidden window stops polling — the desktop shell only hides on close, and
 * a "closed" ShellMate opening a TLS connection every half minute forever
 * is exactly the bug that helper exists to prevent. It also runs whenever
 * the view opens or the settings change, because those are the moments the
 * answer actually changes.
 *
 * Clicking the light shows the certificate ShellMate is talking to, with
 * its fingerprint. That is not decoration: a self-signed certificate can
 * only be trusted by comparing it against a value obtained some other way,
 * and showing the fingerprint is what makes that comparison possible.
 * ShellMate never decides a certificate is genuine.
 */

(function () {
  'use strict';

  const view = window.ansibleView;
  if (!view) return;
  const { el, icon } = view;

  /** How often to re-probe while the view is open and the window visible. */
  const WHILE_OPEN = 30000;

  /** And while the view is closed — the light still matters, less urgently. */
  const WHILE_CLOSED = 300000;

  let last = null;
  let timer = null;
  let inFlight = false;

  /** The badge on the dot, spelled out for the tooltip and the dialog. */
  function summary(health) {
    if (!health) return 'Checking the connection…';
    const bits = [health.label];
    if (health.detail) bits.push(health.detail);
    if (health.latency_ms) bits.push(`${health.latency_ms} ms`);
    return bits.join(' — ');
  }

  function paint(health) {
    const led = document.getElementById('av-tls-led');
    if (!led) return;
    const kind = (health && health.kind) || 'grey';
    led.className = `av-led av-led-${kind}`;
    const label = led.querySelector('.av-led-label');
    if (label) label.textContent = (health && health.label) || 'Checking…';
    led.title = summary(health);
    led.setAttribute('aria-label', `Connection security: ${summary(health)}`);
  }

  async function probe() {
    if (inFlight) return last;
    inFlight = true;
    try {
      last = await view.json('/api/ansible/health');
    } catch (e) {
      // A probe that cannot run is not a healthy connection, and must not
      // leave the previous colour sitting there looking current.
      last = { kind: 'grey', label: 'Unknown', state: 'off',
               detail: `The check itself failed: ${e.message || e}` };
    } finally {
      inFlight = false;
    }
    paint(last);
    document.dispatchEvent(new CustomEvent('shellmate:ansible-tls', {
      detail: last,
    }));
    return last;
  }

  // -- The details behind the light ----------------------------------------

  function when(iso) {
    if (!iso) return '—';
    try {
      return new Date(iso).toLocaleString();
    } catch (e) {
      return iso;
    }
  }

  /** A fingerprint in pairs, because it is read by eye against another one. */
  function grouped(hex) {
    return (hex || '').replace(/(.{4})/g, '$1 ').trim();
  }

  function rows(health) {
    const cert = health.certificate || {};
    const out = [
      ['Address', health.url || '—'],
      ['Encrypted', health.encrypted ? 'Yes' : 'No — plain HTTP'],
      ['Certificate checked', health.verified ? 'Yes'
        : (health.encrypted ? 'No' : 'Nothing to check')],
      ['Round trip', health.latency_ms ? `${health.latency_ms} ms` : '—'],
    ];
    if (health.ansible_core) out.push(['ansible-core', health.ansible_core]);
    if (!cert.available) {
      if (cert.why) out.push(['Certificate', cert.why]);
      return out;
    }
    out.push(
      ['Subject', cert.subject || '—'],
      ['Issuer', cert.self_signed ? `${cert.issuer} (self-signed)`
        : (cert.issuer || '—')],
      ['Valid until', `${when(cert.not_after)}`
        + (cert.days_left !== null && cert.days_left !== undefined
           ? ` — ${cert.days_left} day${cert.days_left === 1 ? '' : 's'} left` : '')],
      ['Covers', (cert.names || []).join(', ') || '—'],
      ['Protocol', [cert.protocol, cert.cipher].filter(Boolean).join(', ') || '—'],
    );
    return out;
  }

  async function showDetails() {
    const health = last || await probe();
    const cert = (health && health.certificate) || {};

    const body = el('div', { class: 'av-tls-detail' }, [
      el('p', { class: `av-tls-verdict av-tls-${health.kind || 'grey'}` },
         [icon(health.kind === 'ok' ? 'check_circle'
               : health.kind === 'bad' ? 'error' : 'info'),
          el('span', { text: health.detail || health.label || '' })]),
      el('dl', { class: 'av-tls-rows' },
         rows(health).flatMap(([term, value]) => [
           el('dt', { text: term }),
           el('dd', { text: String(value) }),
         ])),
    ]);

    if (cert.fingerprint) {
      body.appendChild(el('div', { class: 'av-tls-print' }, [
        el('h4', { text: 'SHA-256 fingerprint' }),
        el('code', { class: 'av-tls-hex', text: grouped(cert.fingerprint) }),
        el('p', { class: 'av-tls-note' },
          cert.self_signed
            ? 'This certificate vouches for itself, so the only thing that '
              + 'makes it trustworthy is matching this value against one you '
              + 'got another way — the container prints it at startup. '
              + 'ShellMate does not and cannot check that for you.'
            : 'Compare against the value the runner reports if you want to '
              + 'be certain which certificate you are talking to.'),
        el('button', {
          type: 'button', class: 'btn-secondary',
          onclick: () => {
            navigator.clipboard.writeText(cert.fingerprint || '');
            if (typeof window._showCopyToast === 'function') window._showCopyToast();
          },
        }, [icon('content_copy'), 'Copy the fingerprint']),
      ]));
    }

    await window.shellmateDialog.alert({
      title: `Connection security — ${health.label || ''}`,
      content: body,
      body: cert.fingerprint ? '' : (health.detail || ''),
    });
  }

  // -- When to look --------------------------------------------------------

  function retime(ms) {
    if (timer) timer.stop();
    timer = window.shellmateVisibility.every(probe, ms);
  }

  function wire() {
    const led = document.getElementById('av-tls-led');
    if (!led) return;
    led.addEventListener('click', showDetails);

    // Often while somebody is looking at it, rarely otherwise. The light is
    // still worth keeping current when the view is shut — a certificate can
    // expire while nobody is watching — but not at the same rate.
    retime(WHILE_CLOSED);
    document.addEventListener('shellmate:ansible-open', () => {
      retime(WHILE_OPEN);
      probe();
    });
    document.addEventListener('shellmate:ansible-close', () => retime(WHILE_CLOSED));

    // The settings are the one thing that changes the answer instantly, so
    // waiting up to five minutes to notice would look broken.
    // The real event is on window, not document, and is 'settings-changed'.
    window.addEventListener('shellmate:settings-changed', probe);
    document.addEventListener('shellmate:ansible-refresh', probe);

    probe();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', wire);
  } else {
    wire();
  }

  window.ansibleTls = { probe, show: showDetails, get last() { return last; } };
})();
