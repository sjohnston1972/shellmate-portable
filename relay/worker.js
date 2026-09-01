/**
 * worker.js — The ShellMate feedback relay (#370).
 *
 * A Cloudflare Worker that accepts a bug/feature report from the app and
 * files it as a labelled GitHub issue. It exists because the portable
 * executable cannot carry a GitHub credential — anything inside it can be
 * read out of it — so the only token lives here, as a Worker secret.
 *
 * The token belongs to a low-privilege machine account, not the maintainer:
 * issues opened with the maintainer's own token would generate no
 * notification (GitHub never notifies you about your own activity), and a
 * leaked token would then be the maintainer's account rather than one that
 * can be deleted without ceremony.
 *
 * Everything the client sends is treated as hostile: the type is mapped to
 * labels server-side (a client cannot apply arbitrary labels), lengths are
 * capped, and anything else in the payload is ignored.
 *
 * Secrets (set with `wrangler secret put`):
 *   GITHUB_TOKEN — classic PAT of the machine account, `public_repo` scope.
 *   APP_KEY      — must match APP_KEY in backend/feedback.py. Not a secret
 *                  in the cryptographic sense (it ships inside the exe); it
 *                  filters drive-by POSTs and can be rotated in a release.
 *
 * Vars (wrangler.toml): GITHUB_REPO, e.g. "sjohnston1972/shellmate-portable".
 */

const MAX_TITLE = 200;
const MAX_DESCRIPTION = 5000;

const LABELS = {
  bug:     ['bug', 'user-reported'],
  feature: ['enhancement', 'user-reported'],
};

export default {
  async fetch(request, env) {
    if (request.method !== 'POST') {
      return json(405, { detail: 'POST only.' });
    }
    if (request.headers.get('X-ShellMate-Key') !== env.APP_KEY) {
      return json(403, { detail: 'Missing or wrong application key.' });
    }

    // Per-IP rate limit, on top of the app key: the key ships inside the
    // executable, so it slows scanners, not someone determined.
    if (env.RATE_LIMITER) {
      const ip = request.headers.get('CF-Connecting-IP') || 'unknown';
      const { success } = await env.RATE_LIMITER.limit({ key: ip });
      if (!success) {
        return json(429, { detail: 'Too many reports — try again in a minute.' });
      }
    }

    let report;
    try {
      report = await request.json();
    } catch (_) {
      return json(400, { detail: 'The body must be JSON.' });
    }

    const labels = LABELS[report.type];
    if (!labels) {
      return json(400, { detail: "type must be 'bug' or 'feature'." });
    }
    const title = String(report.title || '').trim().slice(0, MAX_TITLE);
    if (!title) {
      return json(400, { detail: 'A report needs a title.' });
    }
    const description =
      String(report.description || '').trim().slice(0, MAX_DESCRIPTION);

    const body = [
      description || '_No description given._',
      '',
      '---',
      `Reported from ShellMate (${report.portable ? 'portable build' : 'from source'})`
      + (report.platform ? ` on ${String(report.platform).slice(0, 200)}` : '') + '.',
    ].join('\n');

    const res = await fetch(
      `https://api.github.com/repos/${env.GITHUB_REPO}/issues`, {
        method: 'POST',
        headers: {
          'Authorization': `Bearer ${env.GITHUB_TOKEN}`,
          'Accept': 'application/vnd.github+json',
          'Content-Type': 'application/json',
          // GitHub rejects requests without a User-Agent.
          'User-Agent': 'shellmate-feedback-relay',
        },
        body: JSON.stringify({ title, body, labels }),
      });

    if (!res.ok) {
      // Say that it failed, not why in GitHub's words — the app shows this
      // to end users, and a raw API error would only confuse them.
      console.log('GitHub refused the issue:', res.status, await res.text());
      return json(502, { detail: 'The report could not be filed just now.' });
    }

    const issue = await res.json();
    return json(200, { status: 'ok', issue: issue.number });
  },
};

function json(status, payload) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}
