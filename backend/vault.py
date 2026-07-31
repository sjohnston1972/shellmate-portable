"""
vault.py — Encrypted storage for API keys and device credentials.

Before this existed, provider API keys sat in plain text in settings.json.
On a tool whose whole point is being carried around on a USB stick, that means
a lost stick leaks every key on it.

Two backends, because "portable" pulls in two directions:

**DPAPI** (default on Windows) encrypts with the logged-in Windows account's
key. Nothing to remember, nothing to type, and a stolen stick is inert on any
other machine — the ciphertext cannot be decrypted by another user or another
computer. The trade-off is that it is tied to that one account, so the vault
does not travel.

**Master password** derives a key with scrypt and encrypts with AES-GCM. The
vault travels anywhere, at the cost of typing a passphrase each launch.

Design notes worth keeping:

* The *whole* entry set is encrypted as a single blob rather than value by
  value. Per-entry encryption would leak which keys exist and, worse, would let
  anyone with write access to the file swap or replay individual entries
  undetected. One AEAD blob makes the entire set tamper-evident together.
* A wrong master password is detected by AES-GCM's authentication tag failing,
  so there is no separate password verifier to get wrong, and no oracle that
  distinguishes "wrong password" from "corrupted file".
* Nothing here is a substitute for full-disk encryption. It protects against a
  lost USB stick and casual file access, not against malware running as the
  user — which can simply ask DPAPI to decrypt, exactly as we do.
"""

import base64
import ctypes
import json
import logging
import os
import sys
from ctypes import wintypes
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from backend import paths

logger = logging.getLogger(__name__)

VAULT_VERSION = 1

MODE_DPAPI = "dpapi"
MODE_PASSWORD = "password"

# scrypt cost parameters. n=2**15 puts key derivation at roughly 100 ms on a
# typical laptop: slow enough to make brute-forcing a weak passphrase costly,
# fast enough that unlocking at startup does not feel broken.
SCRYPT_N = 2 ** 15
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32

# Bound scrypt's memory use explicitly. The default cap in hashlib is lower
# than n*r*128 needs at these parameters and raises rather than degrading.
SCRYPT_MAXMEM = 128 * SCRYPT_N * SCRYPT_R * 2

# Mixed into DPAPI as secondary entropy, so a blob from this application
# cannot be decrypted by another program simply running as the same user.
DPAPI_ENTROPY = b"ShellMate Portable vault v1"


class VaultError(Exception):
    """Raised for vault failures, with a message suitable for the user."""


class VaultLocked(VaultError):
    """Raised when the vault needs a master password that has not been given."""


# ---------------------------------------------------------------------------
# Windows DPAPI via ctypes
# ---------------------------------------------------------------------------


class _Blob(ctypes.Structure):
    """DATA_BLOB — how crypt32 passes byte buffers in and out."""

    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    def as_bytes(self) -> bytes:
        return ctypes.string_at(self.pbData, self.cbData)


def dpapi_available() -> bool:
    """True when DPAPI can be used (Windows only)."""
    return sys.platform == "win32"


def _blob_in(data: bytes) -> _Blob:
    buffer = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))


def _dpapi_call(func, data: bytes, description: str) -> bytes:
    """Shared plumbing for CryptProtectData / CryptUnprotectData."""
    blob_in = _blob_in(data)
    entropy = _blob_in(DPAPI_ENTROPY)
    blob_out = _Blob()

    # CRYPTPROTECT_UI_FORBIDDEN — never pop a Windows dialog. This runs inside
    # a web request; a modal nobody can see would hang the call forever.
    flags = 0x01

    ok = func(
        ctypes.byref(blob_in), None, ctypes.byref(entropy),
        None, None, flags, ctypes.byref(blob_out),
    )
    if not ok:
        raise VaultError(f"{description} failed: {ctypes.WinError(ctypes.get_last_error())}")

    try:
        return blob_out.as_bytes()
    finally:
        # The buffer is allocated by Windows; leaking it would leak plaintext.
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def dpapi_encrypt(data: bytes) -> bytes:
    """Encrypt with the current Windows user's key."""
    if not dpapi_available():
        raise VaultError("DPAPI is only available on Windows. Use a master password instead.")
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    crypt32.CryptProtectData.restype = wintypes.BOOL
    return _dpapi_call(crypt32.CryptProtectData, data, "Encrypting the vault")


def dpapi_decrypt(data: bytes) -> bytes:
    """Decrypt data produced by :func:`dpapi_encrypt` on this account."""
    if not dpapi_available():
        raise VaultError("DPAPI is only available on Windows.")
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    try:
        return _dpapi_call(crypt32.CryptUnprotectData, data, "Decrypting the vault")
    except VaultError as exc:
        raise VaultError(
            "This vault was encrypted by a different Windows account or on a "
            "different machine, so it cannot be read here. Switch the vault to "
            "a master password if you need it to travel."
        ) from exc


# ---------------------------------------------------------------------------
# Master-password encryption
# ---------------------------------------------------------------------------


def _derive_key(password: str, salt: bytes) -> bytes:
    """Derive a 32-byte AES key from a passphrase using scrypt."""
    if not password:
        raise VaultError("A master password is required.")
    return __import__("hashlib").scrypt(
        password.encode("utf-8"), salt=salt,
        n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=SCRYPT_DKLEN,
        maxmem=SCRYPT_MAXMEM,
    )


def password_encrypt(data: bytes, password: str) -> dict:
    """Encrypt *data* under *password*, returning the stored representation."""
    salt = os.urandom(16)
    nonce = os.urandom(12)
    key = _derive_key(password, salt)
    ciphertext = AESGCM(key).encrypt(nonce, data, None)
    return {
        "salt":       base64.b64encode(salt).decode(),
        "nonce":      base64.b64encode(nonce).decode(),
        "ciphertext": base64.b64encode(ciphertext).decode(),
    }


def password_decrypt(stored: dict, password: str) -> bytes:
    """
    Decrypt a payload produced by :func:`password_encrypt`.

    A wrong password and a tampered file both surface as InvalidTag, and both
    are reported the same way — distinguishing them would tell an attacker
    which of the two they achieved.
    """
    try:
        salt = base64.b64decode(stored["salt"])
        nonce = base64.b64decode(stored["nonce"])
        ciphertext = base64.b64decode(stored["ciphertext"])
    except (KeyError, ValueError) as exc:
        raise VaultError("The vault file is malformed.") from exc

    key = _derive_key(password, salt)
    try:
        return AESGCM(key).decrypt(nonce, ciphertext, None)
    except InvalidTag as exc:
        raise VaultError("Incorrect master password, or the vault file has been altered.") from exc


# ---------------------------------------------------------------------------
# The vault
# ---------------------------------------------------------------------------


class Vault:
    """
    Encrypted key/value store for secrets.

    Held as a single process-wide instance (see :data:`vault` below). Entries
    are cached in memory once decrypted so that a master-password vault is
    unlocked once per launch rather than per read.
    """

    def __init__(self) -> None:
        self._entries: dict[str, str] | None = None
        self._password: str | None = None   # held only for a password-mode vault

    # -- state ---------------------------------------------------------------

    @property
    def path(self):
        return paths.data_dir() / "vault.json"

    def exists(self) -> bool:
        return self.path.exists()

    def _read_file(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise VaultError(f"Could not read the vault file: {exc}") from exc

    def mode(self) -> str:
        """Return the storage mode: "dpapi", "password", or "none" if absent."""
        if not self.exists():
            return "none"
        try:
            return self._read_file().get("mode", MODE_DPAPI)
        except VaultError:
            return "none"

    def is_locked(self) -> bool:
        """
        True when the vault holds secrets that cannot be read yet.

        Only ever true for a master-password vault that has not been unlocked;
        a DPAPI vault unlocks implicitly as the logged-in user.
        """
        if not self.exists():
            return False
        if self._entries is not None:
            return False
        return self.mode() == MODE_PASSWORD

    def status(self) -> dict:
        """Summary for the UI and the /api/vault/status endpoint."""
        return {
            "exists":          self.exists(),
            "mode":            self.mode(),
            "locked":          self.is_locked(),
            "dpapi_available": dpapi_available(),
            "entry_count":     len(self._entries) if self._entries is not None else None,
        }

    # -- unlocking -----------------------------------------------------------

    def unlock(self, password: str = "") -> None:
        """
        Load and decrypt the vault into memory.

        Args:
            password: Required for a master-password vault, ignored otherwise.

        Raises:
            VaultError: Wrong password, tampered file, or a DPAPI blob that
                belongs to another account.
        """
        if not self.exists():
            self._entries = {}
            return

        payload = self._read_file()
        mode = payload.get("mode", MODE_DPAPI)

        if mode == MODE_PASSWORD:
            if not password:
                raise VaultLocked("This vault needs its master password.")
            raw = password_decrypt(payload.get("payload", {}), password)
            self._password = password
        else:
            blob = base64.b64decode(payload.get("payload", ""))
            raw = dpapi_decrypt(blob)
            self._password = None

        try:
            self._entries = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise VaultError("The vault decrypted to something unreadable.") from exc

    def lock(self) -> None:
        """Drop the decrypted entries from memory."""
        self._entries = None
        self._password = None

    def _ensure_loaded(self) -> dict[str, str]:
        """Return the decrypted entries, unlocking a DPAPI vault on demand."""
        if self._entries is None:
            if self.is_locked():
                raise VaultLocked("The vault is locked. Enter the master password to continue.")
            self.unlock()
        return self._entries if self._entries is not None else {}

    # -- reading and writing -------------------------------------------------

    def get(self, key: str, default: str = "") -> str:
        """Return a secret, or *default* when absent or the vault is locked."""
        try:
            return self._ensure_loaded().get(key, default)
        except VaultError:
            # A locked or unreadable vault must degrade to "no value" rather
            # than breaking every settings read in the application.
            return default

    def set(self, key: str, value: str) -> None:
        """Store a secret. An empty value removes the entry."""
        entries = self._ensure_loaded()
        if value:
            entries[key] = value
        else:
            entries.pop(key, None)
        self._write(entries)

    def set_many(self, values: dict[str, str]) -> None:
        """Store several secrets in one write."""
        entries = self._ensure_loaded()
        for key, value in values.items():
            if value:
                entries[key] = value
            else:
                entries.pop(key, None)
        self._write(entries)

    def delete(self, key: str) -> None:
        """Remove a secret."""
        self.set(key, "")

    def has(self, key: str) -> bool:
        """True when a secret is stored under *key*."""
        try:
            return bool(self._ensure_loaded().get(key))
        except VaultError:
            return False

    def keys(self) -> list[str]:
        """Names of stored secrets. Never returns the values."""
        try:
            return sorted(self._ensure_loaded())
        except VaultError:
            return []

    # -- persistence ---------------------------------------------------------

    def _write(self, entries: dict[str, str]) -> None:
        """Encrypt and persist the entry set."""
        mode = self.mode()
        if mode == "none":
            mode = MODE_DPAPI if dpapi_available() else MODE_PASSWORD

        raw = json.dumps(entries).encode("utf-8")

        if mode == MODE_PASSWORD:
            if not self._password:
                raise VaultLocked("The vault must be unlocked before it can be written to.")
            document = {
                "version": VAULT_VERSION,
                "mode":    MODE_PASSWORD,
                "payload": password_encrypt(raw, self._password),
            }
        else:
            document = {
                "version": VAULT_VERSION,
                "mode":    MODE_DPAPI,
                "payload": base64.b64encode(dpapi_encrypt(raw)).decode(),
            }

        self._atomic_write(document)
        self._entries = entries

    def _atomic_write(self, document: dict) -> None:
        """
        Write via a temporary file and replace.

        A half-written vault is unrecoverable — losing power mid-write would
        otherwise cost every stored secret — so the new file is completed
        before it takes the place of the old one.
        """
        target = self.path
        temporary = target.with_suffix(".tmp")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps(document, indent=2), encoding="utf-8")
            os.replace(temporary, target)
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise VaultError(f"Could not write the vault: {exc}") from exc

    # -- changing mode -------------------------------------------------------

    def set_mode(self, mode: str, password: str = "") -> None:
        """
        Switch between DPAPI and master-password storage, re-encrypting.

        The vault must already be readable — switching is a re-encryption of
        the existing secrets, not a way to recover ones that cannot be
        decrypted.
        """
        if mode not in (MODE_DPAPI, MODE_PASSWORD):
            raise VaultError(f"Unknown vault mode: {mode}")
        if mode == MODE_DPAPI and not dpapi_available():
            raise VaultError("DPAPI is only available on Windows.")
        if mode == MODE_PASSWORD and not password:
            raise VaultError("Choose a master password.")

        entries = self._ensure_loaded()

        document: dict[str, Any] = {"version": VAULT_VERSION, "mode": mode}
        raw = json.dumps(entries).encode("utf-8")

        if mode == MODE_PASSWORD:
            document["payload"] = password_encrypt(raw, password)
            self._password = password
        else:
            document["payload"] = base64.b64encode(dpapi_encrypt(raw)).decode()
            self._password = None

        self._atomic_write(document)
        self._entries = entries
        logger.info("Vault re-encrypted using %s", mode)


# Process-wide instance. The vault is a single file, so a single owner avoids
# two callers writing over one another.
vault = Vault()
