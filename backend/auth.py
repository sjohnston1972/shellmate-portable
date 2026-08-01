"""
auth.py — Optional authentication, for the deployments that need it.

ShellMate's security model is a bargain: there is no authentication precisely
because nothing but this machine can reach the port. That is defensible for
the portable single-user case it is built for, and it is the whole reason
`NOT_EXPOSED` refuses to make the bind address a setting.

The repository shipped a supported deployment that broke it. `docker-compose.yml`
binds `0.0.0.0`, attaches to an external Docker network, and the README
explains that it "pairs naturally with a Cloudflare tunnel" — that is remote
access, with no authentication, to a tool that opens SSH sessions to network
equipment, runs commands on them, browses the filesystem and holds saved
credentials.

So authentication exists now, off by default, and the bargain is stated as
"loopback, **or** a token" rather than as something the repository no longer
kept.

**Refusing to start is the important part.** A warning in a log nobody reads
is how an installation ends up exposed; a hard failure at startup is how it
does not. `run.py` checks before it serves anything.

**A cookie, not a header.** The WebSocket path is what actually matters —
`/ws/chat` reads every open session — and browser WebSocket clients cannot set
headers. A cookie covers REST and WebSockets with one mechanism, and composes
with the Origin check rather than duplicating it.

**Not a Stockton setting.** It has to be readable before the application is
up, and a setting that can lock you out of the panel that sets it fails the
"worst outcome is degraded" rule that governs everything in there.
"""

import hashlib
import hmac
import logging
import os
import secrets

logger = logging.getLogger(__name__)

#: The environment variable that turns it on.
ENV_VAR = "SHELLMATE_AUTH_TOKEN"

#: Cookie holding proof that a token was presented.
COOKIE = "shellmate_auth"

#: Random per-process salt, so the cookie is derived from the token rather
#: than being the token. A stolen cookie proves someone authenticated once; it
#: does not hand over the credential itself, and every restart invalidates it.
_SALT = secrets.token_bytes(32)


def token() -> str:
    """The configured token, or "" when authentication is off."""
    return (os.environ.get(ENV_VAR) or "").strip()


def enabled() -> bool:
    return bool(token())


def is_loopback(host: str) -> bool:
    """Whether a bind address reaches only this machine."""
    return (host or "").strip() in ("127.0.0.1", "::1", "localhost")


def startup_refusal(host: str) -> str:
    """
    Why ShellMate must not start, or "" when it may.

    The one case that is refused: bound somewhere other than loopback with no
    token set. Everything else — loopback with or without a token, or any
    address with one — is a deliberate configuration.
    """
    if is_loopback(host) or enabled():
        return ""

    return (
        f"ShellMate is set to bind {host}, which is reachable from other "
        f"machines, and no {ENV_VAR} is set.\n\n"
        f"There is no authentication by default because nothing but this "
        f"machine can normally reach the port. Binding wider removes the only "
        f"thing protecting SSH sessions to your network equipment, saved "
        f"credentials and the file browser.\n\n"
        f"Set {ENV_VAR} to a long random string and restart, or set "
        f"SHELLMATE_HOST=127.0.0.1."
    )


def cookie_value() -> str:
    """The value proving the token was presented."""
    return hashlib.sha256(_SALT + token().encode("utf-8")).hexdigest()


def check_token(presented: str) -> bool:
    """Whether a submitted token matches, compared in constant time."""
    if not enabled():
        return True
    return hmac.compare_digest((presented or "").strip(), token())


def check_cookie(presented: str) -> bool:
    """Whether a request's cookie proves an earlier successful login."""
    if not enabled():
        return True
    return hmac.compare_digest(presented or "", cookie_value())


#: Paths reachable without a cookie — the login page and what it needs to
#: render. Deliberately short: anything else would be a way round.
PUBLIC_PATHS = frozenset({"/login", "/api/login", "/favicon.ico"})


def is_public(path: str) -> bool:
    """
    Whether a path may be served to someone who has not logged in.

    Static assets are allowed so the login page can have a stylesheet and its
    font. They are the interface's own files and carry nothing about any
    device, any session or any credential — the API is where all of that
    lives, and none of it is public.
    """
    return path in PUBLIC_PATHS or path.startswith("/static/")
