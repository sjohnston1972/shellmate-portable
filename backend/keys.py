"""
keys.py — Making SSH keys, not just using them.

ShellMate could already *use* a key — there is a path field, a passphrase, a
separate username for it, and a picker to find it — but it could not make one.
Everyone had to go elsewhere first: ``ssh-keygen`` on a machine that has it, or
PuTTYgen on one that does not, and then come back.

That is the wrong shape for a tool whose premise is one executable on a stick
with no install rights.  Generating a key is the one part of key authentication
that needs no network and no device, and it was the part missing.

Nothing here is invented.  ``cryptography`` is already a dependency — paramiko
pulls it in and ``build.spec`` already names it — and it has Ed25519, ECDSA,
RSA and OpenSSH serialisation.  There is no home-made key format and no
hand-rolled key derivation, because that is how key management goes wrong.

Four rules:

**The private half never crosses the API.**  No function here returns private
key material, exactly as none returns a password.  The interface sees the
public key, the fingerprint and the metadata; the private key exists on disk
and nowhere else.

**Written where the user's data lives.**  ``data_dir()/keys``, never a path
derived from ``__file__`` — under ``--onefile`` that directory is deleted when
the process exits, silently taking the key with it.

**Locked down at creation.**  OpenSSH refuses a private key other accounts can
read.  On Windows that is an ACL rather than a mode, so a key generated here
and used by ``ssh.exe`` elsewhere would be rejected unless the ACL is tightened
as it is written — which is much cheaper than a later bug report.

**A passphrase-less key is allowed, and said out loud.**  Plenty of lab work
does not want one. The interface states what it means at the point of choosing
rather than in a warning nobody reads.
"""

import logging
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa

from backend import paths

logger = logging.getLogger(__name__)

#: RSA below this is not worth offering. 2048 is still fine; 1024 is not.
MIN_RSA_BITS = 2048
RSA_SIZES = (2048, 3072, 4096)

#: NIST curves paramiko will negotiate.
CURVES = {
    "p256": ec.SECP256R1,
    "p384": ec.SECP384R1,
    "p521": ec.SECP521R1,
}

KEY_TYPES = ("ed25519", "ecdsa", "rsa")

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass
class KeyInfo:
    """What is publicly knowable about a key on disk."""

    name: str
    path: str
    public_path: str
    kind: str                 # ed25519 | ecdsa-... | rsa-3072 | unknown
    bits: int
    comment: str
    fingerprint_sha256: str
    fingerprint_md5: str
    public_key: str
    encrypted: bool
    created: float

    def as_dict(self) -> dict:
        # Deliberately spelled out rather than asdict(): a field added to this
        # class should have to be added here too, so private material can
        # never reach an API response by being added in one place.
        return {
            "name":               self.name,
            "path":               self.path,
            "public_path":        self.public_path,
            "kind":               self.kind,
            "bits":               self.bits,
            "comment":            self.comment,
            "fingerprint_sha256": self.fingerprint_sha256,
            "fingerprint_md5":    self.fingerprint_md5,
            "public_key":         self.public_key,
            "encrypted":          self.encrypted,
            "created":            self.created,
        }


def keys_dir() -> Path:
    """Where generated keys live. Inside the data folder, so they travel."""
    return paths.data_dir() / "keys"


def safe_name(name: str) -> str:
    """Reduce a key name to something a filesystem will take."""
    cleaned = _UNSAFE.sub("-", (name or "").strip()).strip("-._")
    return cleaned[:60] or "id_shellmate"


# ---------------------------------------------------------------------------
# Generating
# ---------------------------------------------------------------------------


def generate(name: str, kind: str = "ed25519", bits: int = 3072,
             curve: str = "p256", passphrase: str = "",
             comment: str = "") -> KeyInfo:
    """
    Create a key pair and write it in OpenSSH format.

    Args:
        name:       File name, sanitised. ``id_ed25519`` style.
        kind:       ed25519 (default), ecdsa or rsa.
        bits:       RSA size. Ignored otherwise.
        curve:      ECDSA curve. Ignored otherwise.
        passphrase: Encrypts the private key file. Empty means unencrypted.
        comment:    Ends up in the ``.pub`` and in the device's configuration.

    Returns:
        The public facts about what was written.

    Raises:
        ValueError: An unusable choice — an unknown type, an RSA size below
            the floor, or a name that already exists.
    """
    kind = (kind or "ed25519").lower()
    if kind not in KEY_TYPES:
        raise ValueError(f"'{kind}' is not a key type ShellMate can make.")

    if kind == "rsa":
        if bits < MIN_RSA_BITS:
            raise ValueError(
                f"RSA keys below {MIN_RSA_BITS} bits are not offered — they are "
                f"no longer considered strong enough."
            )
        private = rsa.generate_private_key(public_exponent=65537, key_size=bits)
    elif kind == "ecdsa":
        if curve not in CURVES:
            raise ValueError(f"'{curve}' is not a curve ShellMate knows about.")
        private = ec.generate_private_key(CURVES[curve]())
    else:
        private = ed25519.Ed25519PrivateKey.generate()

    stem = safe_name(name)
    directory = keys_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / stem

    if path.exists():
        raise ValueError(
            f"A key called '{stem}' already exists. Choose another name — "
            f"overwriting one is how somebody loses access to a device."
        )

    _write_private(path, private, passphrase)
    public_line = _public_line(private, comment)
    path.with_suffix(".pub").write_text(public_line + "\n", encoding="utf-8")

    logger.info("Generated a %s key at %s (%s)", kind, path,
                "with a passphrase" if passphrase else "no passphrase")
    return describe(path)


def _write_private(path: Path, private, passphrase: str) -> None:
    """Write the private half, then take everyone else off it."""
    encryption = (
        serialization.BestAvailableEncryption(passphrase.encode("utf-8"))
        if passphrase else serialization.NoEncryption()
    )
    pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.OpenSSH,
        encryption_algorithm=encryption,
    )

    # Created restrictively rather than written and then fixed: between the two
    # there is a window where the key is readable, and it is the only window
    # that matters.
    handle = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(handle, pem)
    finally:
        os.close(handle)

    restrict(path)


def restrict(path: Path) -> bool:
    """
    Make a private key unreadable to anyone but its owner.

    POSIX modes are advisory on Windows, and OpenSSH there checks the ACL —
    so a key ShellMate generated would be refused by ``ssh.exe`` with
    "permissions are too open" unless the inherited ACL is replaced. icacls is
    the documented way to do that and needs no administrator rights on a file
    you own.

    Returns False when it could not be done, which is worth knowing but never
    worth failing a generation over: paramiko does not check, so the key still
    works inside ShellMate.
    """
    try:
        os.chmod(path, 0o600)
    except OSError as exc:
        logger.info("Could not chmod %s: %s", path, exc)

    if sys.platform != "win32":
        return True

    user = os.environ.get("USERNAME") or ""
    if not user:
        return False
    try:
        # /inheritance:r drops the inherited entries — without it, "Users"
        # usually still has read, which is exactly what OpenSSH objects to.
        result = subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            logger.info("icacls on %s returned %s: %s",
                        path, result.returncode, result.stderr.strip())
            return False
        return True
    except (OSError, subprocess.SubprocessError) as exc:
        logger.info("Could not tighten permissions on %s: %s", path, exc)
        return False


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def _public_line(private, comment: str) -> str:
    blob = private.public_key().public_bytes(
        encoding=serialization.Encoding.OpenSSH,
        format=serialization.PublicFormat.OpenSSH,
    ).decode("ascii")
    return f"{blob} {comment}".strip()


def describe(path: Path) -> KeyInfo:
    """
    Everything publicly knowable about a key.

    Reads the ``.pub`` where there is one, so an encrypted key can be
    described without its passphrase — which is the common case in a listing.

    Raises:
        ValueError: There is nothing usable at that path.
    """
    import base64
    import hashlib
    import time

    path = Path(path)
    public_path = path.with_suffix(".pub")

    if public_path.exists():
        line = public_path.read_text(encoding="utf-8").strip()
    else:
        # No .pub: derive one, which needs the private key to be readable.
        try:
            private = _load_private(path.read_bytes())
        except PassphraseRequired:
            raise ValueError(
                "This key is encrypted and has no .pub file beside it, so its "
                "public half cannot be shown without the passphrase."
            ) from None
        line = _public_line(private, "")
        public_path.write_text(line + "\n", encoding="utf-8")

    parts = line.split(None, 2)
    if len(parts) < 2:
        raise ValueError("The public key file is not in OpenSSH format.")

    algorithm, blob = parts[0], parts[1]
    comment = parts[2] if len(parts) > 2 else ""
    raw = base64.b64decode(blob)

    digest = base64.b64encode(hashlib.sha256(raw).digest()).decode().rstrip("=")
    md5 = hashlib.md5(raw).hexdigest()                     # noqa: S324
    md5_pairs = ":".join(md5[i:i + 2] for i in range(0, len(md5), 2))

    kind, bits = _kind_and_bits(algorithm, line)

    try:
        encrypted = _is_encrypted(path)
        created = path.stat().st_mtime
    except OSError:
        encrypted, created = False, 0.0

    return KeyInfo(
        name=path.name,
        path=str(path),
        public_path=str(public_path),
        kind=kind,
        bits=bits,
        comment=comment,
        fingerprint_sha256=f"SHA256:{digest}",
        # MD5 because a great deal of network kit still prints only that, and
        # comparing what the switch shows against what you hold is the whole
        # point of a fingerprint.
        fingerprint_md5=f"MD5:{md5_pairs}",
        public_key=line,
        encrypted=encrypted,
        created=created,
    )


def _kind_and_bits(algorithm: str, line: str) -> tuple[str, int]:
    if algorithm == "ssh-ed25519":
        return "ed25519", 256
    if algorithm.startswith("ecdsa-sha2-nistp"):
        size = int(algorithm.rsplit("nistp", 1)[-1] or 0)
        return f"ecdsa-p{size}", size
    if algorithm == "ssh-rsa":
        try:
            key = serialization.load_ssh_public_key(line.encode())
            return f"rsa-{key.key_size}", key.key_size
        except Exception:
            return "rsa", 0
    return algorithm or "unknown", 0


class PassphraseRequired(ValueError):
    """The key is encrypted and no passphrase was given."""


class PassphraseWrong(ValueError):
    """A passphrase was given and it does not open the key."""


def _load_private(data: bytes, passphrase: str = ""):
    """
    Load a private key, distinguishing the two passphrase failures.

    ``cryptography`` signals "this key is encrypted" with a *ValueError*
    reading "Key is password-protected" — not the TypeError one might expect,
    and the same exception type it uses for a wrong passphrase. Telling those
    apart matters: "enter the passphrase" and "that passphrase is wrong" send
    somebody to completely different places.

    Note also that an *unencrypted* key loads happily when handed a passphrase,
    which the caller has to account for rather than treating as verification.
    """
    password = passphrase.encode("utf-8") if passphrase else None
    try:
        return serialization.load_ssh_private_key(data, password=password)
    except TypeError as exc:
        raise PassphraseRequired("This key needs a passphrase.") from exc
    except ValueError as exc:
        message = str(exc).lower()
        if not passphrase and "password" in message:
            raise PassphraseRequired("This key needs a passphrase.") from exc
        if passphrase:
            raise PassphraseWrong("That passphrase does not open this key.") from exc
        raise ValueError(f"That does not look like an SSH key: {exc}") from exc


def _is_encrypted(path: Path) -> bool:
    """Whether the private key needs a passphrase, without asking for one."""
    try:
        _load_private(path.read_bytes())
        return False
    except PassphraseRequired:
        return True
    except Exception:
        # Unreadable for some other reason. Not our question here, and the
        # listing already skips keys it cannot describe.
        return False


def listing() -> list[dict]:
    """
    Every key ShellMate knows about, newest first.

    A key it cannot read is skipped with a log line rather than failing the
    whole list — one unreadable file must not hide the rest.
    """
    directory = keys_dir()
    if not directory.exists():
        return []

    out = []
    for path in sorted(directory.iterdir()):
        if path.suffix == ".pub" or not path.is_file():
            continue
        try:
            out.append(describe(path).as_dict())
        except Exception as exc:
            logger.info("Skipping %s in the key list: %s", path.name, exc)

    out.sort(key=lambda k: k["created"], reverse=True)
    return out


# ---------------------------------------------------------------------------
# Changing and removing
# ---------------------------------------------------------------------------


def change_passphrase(path: str, old: str, new: str) -> KeyInfo:
    """
    Add, change or remove the passphrase on an existing key.

    Raises:
        ValueError: The old passphrase is wrong, or the key is unreadable.
    """
    target = _within_keys_dir(path)

    data = target.read_bytes()

    # An unencrypted key loads happily when handed a passphrase, so "the old
    # one was wrong" is only a meaningful answer for a key that actually has
    # one. Check what it is first, then say something true.
    if old and not _is_encrypted(target):
        raise ValueError(
            "This key has no passphrase, so there is nothing to enter as the "
            "current one. Leave it blank to add one."
        )

    try:
        private = _load_private(data, old)
    except PassphraseRequired:
        raise ValueError("This key has a passphrase. Enter the current one.") from None
    except PassphraseWrong:
        raise ValueError("That passphrase does not open this key.") from None
    except Exception as exc:
        raise ValueError(f"Could not read that key: {exc}") from exc

    _write_private(target, private, new)
    logger.info("Changed the passphrase on %s (%s)", target.name,
                "now encrypted" if new else "now unencrypted")
    return describe(target)


def delete(path: str) -> bool:
    """
    Remove a key and its public half.

    Only ever inside the keys folder — a delete endpoint that accepted an
    arbitrary path would be a file-removal API with a friendly name.
    """
    target = _within_keys_dir(path)
    removed = False
    for candidate in (target, target.with_suffix(".pub")):
        try:
            candidate.unlink()
            removed = True
        except OSError:
            continue
    if removed:
        logger.info("Deleted key %s", target.name)
    return removed


def import_key(source: str, name: str = "") -> KeyInfo:
    """
    Copy an existing key into ShellMate's keys folder.

    Copied rather than referenced so the inventory means one thing — "keys
    ShellMate is looking after" — and so permissions can be tightened on it.
    A key left where it was keeps working through the connection dialog's path
    field, which is unchanged.
    """
    origin = Path(source).expanduser()
    if not origin.is_file():
        raise ValueError(f"There is no file at {origin}.")

    stem = safe_name(name or origin.name)
    directory = keys_dir()
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / stem
    if target.exists():
        raise ValueError(f"A key called '{stem}' is already here.")

    data = origin.read_bytes()
    handle = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(handle, data)
    finally:
        os.close(handle)
    restrict(target)

    origin_pub = origin.with_suffix(".pub")
    if origin_pub.is_file():
        target.with_suffix(".pub").write_bytes(origin_pub.read_bytes())

    try:
        return describe(target)
    except ValueError:
        target.unlink(missing_ok=True)
        raise


def _within_keys_dir(path: str) -> Path:
    """
    Resolve a path and refuse anything outside the keys folder.

    Every mutating function goes through this. The endpoints take a path from
    the browser, and a path from the browser is not a path to be trusted.
    """
    target = Path(path).expanduser().resolve()
    root = keys_dir().resolve()
    if root not in target.parents:
        raise ValueError("That path is not inside ShellMate's keys folder.")
    if not target.is_file():
        raise ValueError("There is no key at that path.")
    return target
