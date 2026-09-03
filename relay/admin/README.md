# ShellMate licence service and admin portal

A Cloudflare Worker at `https://shellmate-admin.foundry-ns.com` (#447). It
signs licence keys, answers the application's refresh and revocation
questions, and serves the portal where keys are issued, renewed and revoked
and the people who hold them are recorded.

## How it fits together

- **Keys** are Ed25519-signed tokens (`SM1.<payload>.<signature>`). The
  private key is a Worker secret; the public half is `PUBLIC_KEY_B64` here
  and in `backend/licence.py`. ShellMate verifies a key offline.
- **Refresh**: ShellMate posts its key id to `/licence/refresh` now and then.
  The answer is the current token (a renewal arrives this way with no
  re-entry) or `revoked: true` with the reason.
- **Records** live in D1 (`schema.sql`): people, licences, and an event log.
- **The portal** is one page served by the Worker, password-protected, with
  an HMAC session cookie.

## Setting it up

```bash
cd relay/admin
wrangler d1 create shellmate-licences            # paste the id into wrangler.toml
wrangler d1 execute shellmate-licences --remote --file=schema.sql
wrangler secret put SIGNING_KEY_PKCS8_B64        # Ed25519 private key, PKCS#8 DER, base64
wrangler secret put ADMIN_PASSWORD               # the portal password
wrangler secret put SESSION_SECRET               # any long random string
wrangler deploy
```

The custom domain needs `foundry-ns.com` to be a zone on the same Cloudflare
account; `wrangler deploy` creates the DNS record.

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
| `POST /licence/refresh` `{id}` | the app | current token, or revoked + reason |
| `GET /licence/check?id=` | the app, support | kind, expiry, revoked |
| `GET /health` | anyone | liveness |
| `POST /admin/login` | the portal | password → session cookie |
| `/admin/api/licences` … | the portal (cookie) | list, issue, detail, renew, revoke, restore, delete, notes |
| `/admin/api/users` … | the portal (cookie) | list, add, detail, edit, delete |
| `/admin/api/stats` | the portal (cookie) | the overview numbers and recent events |

Everything is rate-limited per IP. The application endpoints answer about
one key at a time and never list anything.
