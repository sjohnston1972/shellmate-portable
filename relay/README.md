# ShellMate feedback relay

Receives bug/feature reports from the in-app feedback widget and files each
one as a GitHub issue labelled `user-reported` (+ `bug` or `enhancement`).
It exists because the portable exe cannot carry a GitHub token — anything
inside it can be read out of it — so the only credential lives here.

## One-time setup (~15 minutes)

1. **Machine account.** Create a fresh GitHub account, e.g.
   `shellmate-reporter`. Issues will arrive authored by it — which is what
   makes GitHub notify you (GitHub never notifies you of your *own*
   activity, so using your own token would silently kill notifications).
   The repo is **private**, so invite the machine account as a collaborator
   (`gh api repos/<owner>/<repo>/collaborators/<account> -X PUT
   -f permission=push` — personal repos offer no lighter role that can
   apply labels) and accept the invitation as that account.

2. **Token.** Signed in as the machine account:
   Settings → Developer settings → Personal access tokens → **classic** →
   generate with the `repo` scope — the only classic scope that reaches a
   private repo; `public_repo` gets a 404. (A fine-grained token won't
   work here at all — it can't reach a repo the machine account doesn't
   own.) Set an expiry and put a reminder in your calendar. The trade-off,
   stated plainly: this token can write to the repo, it lives only in
   Cloudflare's secret store, and deleting the machine account revokes it.

3. **Labels.** In the repo, create the `user-reported` label
   (`bug` and `enhancement` already exist).

4. **Deploy.** With a free Cloudflare account and
   [wrangler](https://developers.cloudflare.com/workers/wrangler/) installed:

   ```
   cd relay
   wrangler login
   wrangler secret put GITHUB_TOKEN     # paste the machine account's PAT
   wrangler secret put APP_KEY          # paste the APP_KEY value from backend/feedback.py
   wrangler deploy
   ```

   Deploy prints the URL, e.g.
   `https://shellmate-feedback.<your-subdomain>.workers.dev`.

5. **Point ShellMate at it.** The deployed URL is the default of the
   `feedback.relay_url` advanced setting, so a build after that change
   needs nothing. If the worker moves: Settings → Advanced (Stockton) →
   *Bug and feature reports* → **Feedback relay URL**. While unreachable,
   reports queue in `ShellMate-Data/feedback-outbox.json` and are sent
   when a relay next answers.

6. **Check notifications.** You watch your own repos by default; confirm
   under the repo's Watch settings that Issues are included, and open a test
   report from the app.

## Abuse posture

The endpoint is public by definition. Three layers, honest about what each
buys: the `X-ShellMate-Key` header (extractable from the exe — filters
scanners, rotatable in a release), a per-IP rate limit (5/minute), and
hard caps on title/body size. If it ever gets abused anyway, rotate
`APP_KEY`, or tear the worker down — the app degrades to its local outbox
and says so.
