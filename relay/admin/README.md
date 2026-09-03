# ShellMate licence service and admin portal

A Cloudflare Worker at `https://shellmate-admin.foundry-ns.com` (#447). It
signs licence keys, answers the application's refresh and revocation
questions, emails keys to licensees, serves an optional public request page
that issues keys on its own, and serves the portal where keys are issued,
renewed and revoked, people are recorded, and reports are read.

## How it fits together

- **Keys** are Ed25519-signed tokens (`SM1.<payload>.<signature>`). The
  private key is a Worker secret; the public half is `PUBLIC_KEY_B64` here
  and in `backend/licence.py`. ShellMate verifies a key offline.
- **Refresh**: ShellMate posts its key id to `/licence/refresh` now and then.
  The answer is the current token (a renewal arrives this way with no
  re-entry) or `revoked: true` with the reason.
- **Records** live in D1 (`schema.sql`, `schema-v2.sql`, `schema-v3.sql`):
  people, licences, an event log, the portal's settings, and installations.
- **Installations**: a copy of ShellMate reports the machine it is on (name,
  user, platform, version, and a stable hash as id) when a key is entered,
  when it is removed, and at every refresh. The portal shows them on the
  licence page with a *Forget* to free a seat, counts them in the list and
  on the overview, and reports versions in use, keys never activated, and
  licences with more installations than seats. The event log records
  activated, updated, deactivated, over-seats and forgotten.
- **Email** goes through [Resend](https://resend.com) when the
  `RESEND_API_KEY` secret is set and the sender domain is verified there.
  Keys are emailed on issue and on renewal, with the `.key` file attached,
  and can be re-sent from a licence's page. Without the secret the portal
  says so and keys are copied by hand.
- **The public request page** (`/request`) is off until switched on under
  Settings. Open, it issues a personal key valid for a configurable number
  of days and emails it — one live key per address; a second request
  re-sends the existing one. Rate-limited per IP; needs email configured.
- **The portal** is one page served by the Worker, password-protected, with
  an HMAC session cookie and hash routes so Back works: Overview, Licences
  (search, filters by status and kind, sortable columns, CSV export),
  licence pages (key, email, renew, revoke, notes, history), People, Issue,
  Reports (issued per month, in force by kind and source, renewals due in
  30/60/90 days with CSV) and Settings.

## Setting it up

```bash
cd relay/admin
wrangler d1 create shellmate-licences            # paste the id into wrangler.toml
wrangler d1 execute shellmate-licences --remote --file=schema.sql
wrangler d1 execute shellmate-licences --remote --file=schema-v2.sql
wrangler d1 execute shellmate-licences --remote --file=schema-v3.sql
wrangler secret put SIGNING_KEY_PKCS8_B64        # Ed25519 private key, PKCS#8 DER, base64
wrangler secret put ADMIN_PASSWORD               # the portal password
wrangler secret put SESSION_SECRET               # any long random string
wrangler secret put RESEND_API_KEY               # optional: email
wrangler deploy
```

The custom domain needs `foundry-ns.com` to be a zone on the same Cloudflare
account; `wrangler deploy` creates the DNS record.

### Cloudflare Access in front of the portal

The portal sits behind Cloudflare Access (team `clydeford`), so reaching it
means signing in with the allowed Google account first. Four Access
applications cover the hostname:

| Application | Path | Policy |
|---|---|---|
| ShellMate Admin | everything | `finance-stevie-only` (Google login, one address) |
| ShellMate licence API (public) | `/licence` | bypass — the app's refresh and check calls |
| ShellMate request page (public) | `/request` | bypass |
| ShellMate health (public) | `/health` | bypass |

The Worker trusts the identity Access attaches: it verifies the
`Cf-Access-Jwt-Assertion` header against the team's published keys and the
application's audience tag (`ACCESS_TEAM_DOMAIN` and `ACCESS_AUD` in
`wrangler.toml`), and a valid one is a sign-in — the password page is never
shown. The password remains as a fallback, and is the only way in if Access
is removed. The sidebar names the signed-in address and *Sign out* ends the
Access session. Changing the Access application means updating `ACCESS_AUD`
to its new audience tag and redeploying.

### Email through Resend

1. Create an account at resend.com and add `foundry-ns.com` as a domain;
   publish the DNS records it gives you (SPF, DKIM) in Cloudflare.
2. Make an API key with sending permission and store it:
   `wrangler secret put RESEND_API_KEY`, then `wrangler deploy`.
3. In the portal's Settings, set the From address on that domain, the
   subject and the introduction, and press *Send test*.

### The signing key

Generate once, keep the private half only as the Worker secret:

```python
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives import serialization
import base64
priv = ed25519.Ed25519PrivateKey.generate()
print("SIGNING_KEY_PKCS8_B64 =", base64.b64encode(priv.private_bytes(
    serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption())).decode())
print("PUBLIC_KEY_B64 =", base64.b64encode(priv.public_key().public_bytes(
    serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode())
```

Rotating the key invalidates every issued licence, so it is a once-only
decision unless every licensee is re-issued.

## Endpoints

| Path | Who | What |
|---|---|---|
| `POST /licence/refresh` `{id, machine}` | the app | current token, or revoked + reason; records the installation |
| `POST /licence/activate` `{id, machine}` | the app | records the installation; answers seats and count |
| `POST /licence/deactivate` `{id, machine}` | the app | marks the installation removed |
| `GET /licence/check?id=` | the app, support | kind, expiry, revoked |
| `GET` / `POST /request` | the public | the request page; issues and emails a key when open |
| `GET /health` | anyone | liveness |
| `POST /admin/login` | the portal | password → session cookie |
| `/admin/api/licences` … | the portal (cookie) | list with filters, issue (emails), detail with installations, renew (emails), revoke, restore, send, delete, notes and email; `DELETE …/activations/:machine` forgets one |
| `/admin/api/users` … | the portal (cookie) | list, add, detail, edit, delete |
| `/admin/api/reports` | the portal (cookie) | issued per month, by kind and source, renewals due, seats in force |
| `/admin/api/settings` | the portal (cookie) | email wording, the request page, the overview notice |
| `/admin/api/mail/test` | the portal (cookie) | send a test message |
| `/admin/api/stats` | the portal (cookie) | the overview numbers and recent events |

Everything is rate-limited per IP. The application endpoints answer about
one key at a time and never list anything.
