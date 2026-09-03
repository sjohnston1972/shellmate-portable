"""
licence.py — Licence keys, verified without a network (#446).

The model: ShellMate works without a licence; what a licence buys is the
ability to **update from inside the application** (#448). So a missing or
expired key never stops a device being reached — it stops the updater.

A key is a short signed token:

    SM1.<base64url(payload JSON)>.<base64url(Ed25519 signature)>

The payload names the licensee, the kind (``person`` or ``org``), seats,
when it was issued and when it expires, a grace period, and the features it
covers. The public key ships here; the private key lives only in the
licence service (the Cloudflare Worker under ``relay/admin``). Verification
is therefore entirely local: an air-gapped installation is licensed exactly
like a connected one, and nothing about a key can be forged without the
private half.

Two moments need the network, and both degrade rather than fail:

- **Expiry.** A key past its expiry is still honoured for ``grace_days`` when
  the service cannot be reached to confirm a renewal, and the status says so.
- **Revocation** is only learned by asking the service. A revoked key
  remembered locally stays revoked.

The key file is ``licence.key`` in the data folder — never settings.json,
which is meant to be shared, and it never leaves the machine except to the
licence service for a refresh, where only its id is sent.
"""

import base64
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric import ed25519

from backend import paths

logger = logging.getLogger(__name__)

#: The licence service's public key (raw Ed25519, base64). The private half
#: is a secret in the licence Worker and nowhere else.
PUBLIC_KEY_B64 = "5LJXi4D4Lil3nYxIkY+otMGD2O05SrIWJypmQ4Qhqn8="

#: Where the licence service lives. Refresh and revocation only.
SERVICE_URL = "https://shellmate-admin.foundry-ns.com"

PREFIX = "SM1"
KINDS = ("person", "org")
DEFAULT_GRACE_DAYS = 14


@dataclass
class Licence:
    id: str
    kind: str
    licensee: str
    email: str = ""
    seats: int = 1
    issued: str = ""            # ISO date
    expires: str = ""           # ISO date, "" for perpetual
    grace_days: int = DEFAULT_GRACE_DAYS
    features: list = field(default_factory=lambda: ["updates"])
    token: str = ""             # the key as pasted, for refresh and display

    def as_dict(self) -> dict:
        return {
            "id": self.id, "kind": self.kind, "licensee": self.licensee,
            "email": self.email, "seats": self.seats, "issued": self.issued,
            "expires": self.expires, "grace_days": self.grace_days,
            "features": list(self.features),
        }


class LicenceError(ValueError):
    """A key that cannot be accepted, with a reason a person can act on."""


# ---------------------------------------------------------------- codec
def _b64url_decode(text: str) -> bytes:
    padded = text + "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def public_key(b64: str = PUBLIC_KEY_B64) -> ed25519.Ed25519PublicKey:
    return ed25519.Ed25519PublicKey.from_public_bytes(base64.b64decode(b64))


def parse(token: str, key_b64: str | None = None) -> Licence:
    """
    Verify a key's signature and shape. Raises LicenceError otherwise.

    Expiry is *not* checked here — a parsed key past its date is still a
    genuine key, and :func:`status` says what that means today.
    """
    text = (token or "").strip().replace("\n", "").replace(" ", "")
    parts = text.split(".")
    if len(parts) != 3 or parts[0] != PREFIX:
        raise LicenceError("That is not a ShellMate licence key. It starts with SM1. and has three parts.")
    try:
        payload_raw = _b64url_decode(parts[1])
        signature = _b64url_decode(parts[2])
    except Exception as exc:
        raise LicenceError("The key is not intact — check it was copied whole.") from exc
    try:
        public_key(key_b64 or PUBLIC_KEY_B64).verify(signature, payload_raw)
    except InvalidSignature as exc:
        raise LicenceError("The key's signature does not match. It was not issued by ShellMate's licence service, or it has been altered.") from exc
    try:
        data = json.loads(payload_raw.decode("utf-8"))
    except Exception as exc:
        raise LicenceError("The key's contents could not be read.") from exc
    if not isinstance(data, dict) or not data.get("id") or data.get("kind") not in KINDS:
        raise LicenceError("The key is missing its id or kind.")
    try:
        seats = max(1, int(data.get("seats", 1)))
        grace = max(0, int(data.get("grace_days", DEFAULT_GRACE_DAYS)))
    except (TypeError, ValueError) as exc:
        raise LicenceError("The key's seat count or grace period is not a number.") from exc
    for field_name in ("issued", "expires"):
        value = data.get(field_name) or ""
        if value:
            try:
                datetime.fromisoformat(str(value))
            except ValueError as exc:
                raise LicenceError(f"The key's {field_name} date is not a date.") from exc
    features = data.get("features") or ["updates"]
    if not isinstance(features, list):
        features = ["updates"]
    return Licence(
        id=str(data["id"]), kind=str(data["kind"]),
        licensee=str(data.get("licensee") or ""), email=str(data.get("email") or ""),
        seats=seats, issued=str(data.get("issued") or ""), expires=str(data.get("expires") or ""),
        grace_days=grace, features=[str(f) for f in features], token=text,
    )


def sign(payload: dict, private_key_b64: str) -> str:
    """
    Make a key. Used by the tests and by an operator who signs offline; the
    service does the same thing in JavaScript.
    """
    private = ed25519.Ed25519PrivateKey.from_private_bytes(base64.b64decode(private_key_b64))
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return f"{PREFIX}.{_b64url_encode(raw)}.{_b64url_encode(private.sign(raw))}"


# ---------------------------------------------------------------- storage
def key_file():
    return paths.data_dir() / "licence.key"


def _state_file():
    return paths.data_dir() / "licence-state.json"


def _state() -> dict:
    try:
        return json.loads(_state_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_state(state: dict) -> None:
    try:
        _state_file().parent.mkdir(parents=True, exist_ok=True)
        _state_file().write_text(json.dumps(state, indent=2), encoding="utf-8")
    except OSError as exc:
        logger.warning("Could not write licence state: %s", exc)


def load() -> Licence | None:
    """The installed key, verified, or None when there is none or it is bad."""
    try:
        text = key_file().read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        return parse(text)
    except LicenceError as exc:
        logger.warning("The installed licence key is not usable: %s", exc)
        return None


# ---------------------------------------------------------------- this installation
def machine_info() -> dict:
    """
    What identifies this copy to the licence service: the machine name, the
    user, the platform and the ShellMate version. The id is a hash of the
    stable parts so the same machine is counted once however often the key
    is re-entered. Nothing about the devices ShellMate connects to is here.
    """
    import getpass
    import hashlib
    import platform as platform_module
    import socket
    from backend import version as app_version
    hostname = socket.gethostname() or "unknown"
    try:
        user = getpass.getuser()
    except Exception:                                 # no login name in some services
        user = ""
    system = f"{platform_module.system()} {platform_module.release()}".strip()
    raw = f"{hostname}|{user}|{platform_module.machine()}".encode("utf-8")
    return {"id": hashlib.sha256(raw).hexdigest()[:16], "hostname": hostname,
            "user": user, "platform": system, "version": app_version.VERSION}


def _post(path: str, body: dict, timeout: float):
    """One call to the service. None when it cannot be reached."""
    try:
        import httpx
        return httpx.post(f"{SERVICE_URL}{path}", json=body, timeout=timeout,
                          headers={"User-Agent": "ShellMate-licence"})
    except Exception as exc:
        logger.info("Licence service unreachable for %s (%s)", path, exc.__class__.__name__)
        return None


def announce(what: str = "activate", licence_id: str | None = None, timeout: float = 8.0) -> dict:
    """
    Tell the service this copy installed (or removed) a key, so the holder
    of an organisation licence can see where the seats are in use. Best
    effort: an unreachable service is noted and the next refresh carries
    the same information anyway.
    """
    if licence_id is None:
        licence = load()
        if licence is None:
            return {"announced": False, "reason": "no key"}
        licence_id = licence.id
    resp = _post(f"/licence/{what}", {"id": licence_id, "machine": machine_info()}, timeout)
    if resp is None:
        return {"announced": False, "reason": "unreachable"}
    if what == "activate" and resp.status_code == 200:
        state = _state()
        state["announced_at"] = time.time()
        _write_state(state)
    return {"announced": resp.status_code == 200, "status": resp.status_code}


def announce_async(what: str = "activate", licence_id: str | None = None) -> None:
    """The same, on a daemon thread, for the API routes that must not wait."""
    import threading
    threading.Thread(target=announce, args=(what, licence_id), daemon=True,
                     name=f"licence-{what}").start()


def install(token: str) -> Licence:
    """Verify and keep a key. The old key, if any, is replaced."""
    licence = parse(token)
    key_file().parent.mkdir(parents=True, exist_ok=True)
    key_file().write_text(licence.token + "\n", encoding="utf-8")
    state = _state()
    state.pop("revoked", None)
    state["installed_at"] = time.time()
    _write_state(state)
    logger.info("Licence installed: %s (%s, %s seats)", licence.licensee, licence.kind, licence.seats)
    return licence


def remove() -> bool:
    try:
        key_file().unlink()
    except FileNotFoundError:
        return False
    except OSError as exc:
        logger.warning("Could not remove the licence key: %s", exc)
        return False
    _write_state({})
    return True


# ---------------------------------------------------------------- status
def _days_between(later: str, earlier_ts: float) -> float:
    try:
        end = datetime.fromisoformat(later)
    except ValueError:
        return 0.0
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    return (end.timestamp() - earlier_ts) / 86400.0


def status(licence: Licence | None = None, now: float | None = None) -> dict:
    """
    What the licence means today, for the interface and the updater.

    ``valid`` is the single answer the updater asks for. ``state`` is one of
    ``none``, ``active``, ``grace``, ``expired``, ``revoked``; ``detail`` is
    a sentence for a person.
    """
    now = time.time() if now is None else now
    if licence is None:
        licence = load()
    state = _state()
    if licence is None:
        return {"valid": False, "state": "none", "licence": None,
                "detail": "No licence key is installed. ShellMate works without one; "
                          "updating from inside the application needs one."}
    base = {"licence": licence.as_dict(), "refreshed_at": state.get("refreshed_at"),
            "service": SERVICE_URL}
    if state.get("revoked"):
        return {**base, "valid": False, "state": "revoked",
                "detail": "This key has been revoked by the licence service."
                          + (f" Reason: {state['revoked']}" if isinstance(state.get("revoked"), str) else "")}
    if not licence.expires:
        return {**base, "valid": True, "state": "active", "days_left": None,
                "detail": f"Licensed to {licence.licensee} ({licence.kind}, "
                          f"{licence.seats} seat{'s' if licence.seats != 1 else ''}), no expiry."}
    days = _days_between(licence.expires, now)
    if days >= 0:
        return {**base, "valid": True, "state": "active", "days_left": int(days),
                "detail": f"Licensed to {licence.licensee} until {licence.expires[:10]}"
                          + (f" — {int(days)} day{'s' if int(days) != 1 else ''} left." if days < 30 else ".")}
    overdue = -days
    if overdue <= licence.grace_days:
        left = int(licence.grace_days - int(overdue))
        return {**base, "valid": True, "state": "grace", "days_left": left,
                "detail": f"The licence expired on {licence.expires[:10]}. It is honoured for "
                          f"{left} more day{'s' if left != 1 else ''} while a renewal is confirmed."}
    return {**base, "valid": False, "state": "expired", "days_left": 0,
            "detail": f"The licence expired on {licence.expires[:10]} and the grace period has passed. "
                      f"Renew it to update from inside ShellMate."}


def has_feature(name: str = "updates") -> bool:
    """The updater's one question."""
    licence = load()
    if licence is None:
        return False
    return status(licence)["valid"] and name in licence.features


# ---------------------------------------------------------------- refresh
def refresh(timeout: float = 8.0) -> dict:
    """
    Ask the service about the installed key: renewed expiry, revocation.

    The key's id travels, with this machine's name, user and version so the
    installation is on record. A renewed key comes back as a fresh token and
    replaces the installed one. Unreachable is not an error — the status
    simply stays what the local key says.
    """
    licence = load()
    if licence is None:
        return status(None)
    try:
        import httpx
        resp = httpx.post(f"{SERVICE_URL}/licence/refresh",
                          json={"id": licence.id, "machine": machine_info()}, timeout=timeout,
                          headers={"User-Agent": "ShellMate-licence"})
    except Exception as exc:
        logger.info("Licence refresh: service unreachable (%s)", exc.__class__.__name__)
        return {**status(licence), "refresh": "unreachable"}
    state = _state()
    state["refreshed_at"] = time.time()
    if resp.status_code == 404:
        state["revoked"] = "the key is not known to the licence service"
        _write_state(state)
        return {**status(licence), "refresh": "unknown"}
    if resp.status_code != 200:
        _write_state(state)
        return {**status(licence), "refresh": f"service answered {resp.status_code}"}
    data = resp.json()
    if data.get("revoked"):
        state["revoked"] = str(data.get("reason") or True)
        _write_state(state)
        return {**status(licence), "refresh": "revoked"}
    state.pop("revoked", None)
    _write_state(state)
    token = data.get("token")
    if token and token != licence.token:
        try:
            licence = install(token)
            return {**status(licence), "refresh": "renewed"}
        except LicenceError as exc:
            logger.warning("The service sent a key that does not verify: %s", exc)
    return {**status(licence), "refresh": "current"}


# ---------------------------------------------------------------- background refresh
REFRESH_EVERY_DAYS = 3


def maybe_refresh(min_age_days: float = REFRESH_EVERY_DAYS) -> dict | None:
    """
    Refresh the installed key if it has not been refreshed lately.

    Called from a background thread at startup so a renewal made in the
    portal reaches the copy without anyone pressing Refresh, and a
    revocation is learned within days rather than never. Nothing happens
    without an installed key, and an unreachable service is not an error.
    """
    licence = load()
    if licence is None:
        return None
    last = float(_state().get("refreshed_at") or 0)
    if time.time() - last < min_age_days * 86400:
        return None
    return refresh()


def start_background_refresh(delay_seconds: float = 20.0) -> None:
    """A daemon thread: one refresh shortly after start, then every few days."""
    import threading

    def loop() -> None:
        time.sleep(delay_seconds)
        while True:
            try:
                maybe_refresh()
            except Exception as exc:                  # never let the thread die
                logger.debug("Licence refresh: %s", exc)
            time.sleep(6 * 3600)

    threading.Thread(target=loop, daemon=True, name="licence-refresh").start()

