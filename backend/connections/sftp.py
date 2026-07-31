"""
connections/sftp.py — SFTP file access over an existing SSH session.

Pulling a config off a switch or pushing an IOS image onto one is routine
work, and doing it in the same tab you are already logged into beats opening
WinSCP and authenticating a second time.

The SFTP channel is multiplexed onto the SSH connection that is already open,
so there is no second login and no second set of credentials to handle.  It is
created lazily — most sessions never transfer a file, and plenty of network
devices have no SFTP subsystem at all, so paying that cost on every connect
would be wasted and would produce confusing errors on devices that cannot
support it.

Note there is no path sandboxing here, deliberately.  The user already has an
interactive shell on this device; anything reachable over SFTP is equally
reachable with ``more`` at the CLI. A restriction would be theatre, not
security. The bounds that *are* enforced are on the local side: what a
downloaded file may be named, and how large an upload may be.
"""

import logging
import stat
from dataclasses import dataclass

import paramiko

from backend.connections.base import ConnectionError_
from backend.connections.ssh_handler import SSHHandler

logger = logging.getLogger(__name__)

# Cap on a single upload. Large enough for an IOS image, small enough that a
# runaway or mistaken upload cannot fill the device's flash unnoticed.
MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB


@dataclass
class RemoteEntry:
    """One file or directory in a remote listing."""

    name: str
    path: str
    is_dir: bool
    size: int
    modified: float
    permissions: str

    def as_dict(self) -> dict:
        return {
            "name":        self.name,
            "path":        self.path,
            "is_dir":      self.is_dir,
            "size":        self.size,
            "modified":    self.modified,
            "permissions": self.permissions,
        }


def _sftp_for(session: dict) -> paramiko.SFTPClient:
    """
    Return the session's SFTP client, opening it on first use.

    Cached on the session dict so repeated browsing does not open a new
    channel per request.

    Raises:
        ConnectionError_: The session is not SSH, is closed, or the device
            has no SFTP subsystem — the last being common on network gear
            and worth saying plainly.
    """
    existing = session.get("sftp")
    if existing is not None:
        return existing

    handler = session.get("handler")
    if not isinstance(handler, SSHHandler):
        raise ConnectionError_(
            f"File transfer needs an SSH session. This tab is "
            f"{session.get('connection_type', 'unknown')}."
        )
    if not handler.is_connected:
        raise ConnectionError_("The SSH session is no longer connected.")

    client = handler._client
    transport = client.get_transport() if client else None
    if transport is None:
        raise ConnectionError_("The SSH session is no longer connected.")

    try:
        sftp = paramiko.SFTPClient.from_transport(transport)
    except paramiko.SSHException as exc:
        raise ConnectionError_(
            f"This device does not support SFTP: {exc}. Many switches and "
            f"routers run an SSH shell without an SFTP subsystem."
        ) from exc

    if sftp is None:
        raise ConnectionError_(
            "This device does not offer an SFTP subsystem. Many switches and "
            "routers run an SSH shell without one."
        )

    session["sftp"] = sftp
    return sftp


def close_sftp(session: dict) -> None:
    """Close the cached SFTP channel, if one was ever opened."""
    sftp = session.pop("sftp", None)
    if sftp is not None:
        try:
            sftp.close()
        except Exception:
            pass


def list_directory(session: dict, path: str = ".") -> dict:
    """
    List a remote directory.

    Args:
        session: The session dict from SessionManager.
        path:    Remote directory. "." resolves to the login directory.

    Returns:
        The resolved absolute path plus its entries, directories first.
    """
    sftp = _sftp_for(session)

    try:
        # Resolve "." and any symlinks so the UI can show, and navigate from,
        # a real absolute path.
        resolved = sftp.normalize(path or ".")
        attrs = sftp.listdir_attr(resolved)
    except FileNotFoundError as exc:
        raise ConnectionError_(f"No such directory: {path}") from exc
    except PermissionError as exc:
        raise ConnectionError_(f"Permission denied: {path}") from exc
    except OSError as exc:
        raise ConnectionError_(f"Could not list {path}: {exc}") from exc

    entries: list[RemoteEntry] = []
    for attr in attrs:
        is_dir = stat.S_ISDIR(attr.st_mode or 0)
        child = f"{resolved.rstrip('/')}/{attr.filename}"
        entries.append(
            RemoteEntry(
                name=attr.filename,
                path=child,
                is_dir=is_dir,
                size=attr.st_size or 0,
                modified=float(attr.st_mtime or 0),
                permissions=stat.filemode(attr.st_mode or 0),
            )
        )

    # Directories first, then case-insensitive by name — the ordering people
    # expect from a file browser.
    entries.sort(key=lambda e: (not e.is_dir, e.name.lower()))

    parent = resolved.rsplit("/", 1)[0] or "/"
    return {
        "path":    resolved,
        "parent":  parent if resolved != "/" else None,
        "entries": [e.as_dict() for e in entries],
    }


def read_file(session: dict, path: str) -> bytes:
    """
    Read a remote file into memory for download.

    Held in memory rather than streamed because the frontend needs a
    Content-Length to show progress, and the files this is used for —
    configs, crash logs, small images — comfortably fit.
    """
    sftp = _sftp_for(session)
    try:
        attrs = sftp.stat(path)
        if attrs.st_size and attrs.st_size > MAX_UPLOAD_BYTES:
            raise ConnectionError_(
                f"{path} is {attrs.st_size / 1e9:.1f} GB, which is above the "
                f"{MAX_UPLOAD_BYTES / 1e9:.0f} GB transfer limit."
            )
        with sftp.open(path, "rb") as handle:
            handle.prefetch()
            return handle.read()
    except FileNotFoundError as exc:
        raise ConnectionError_(f"No such file: {path}") from exc
    except PermissionError as exc:
        raise ConnectionError_(f"Permission denied reading {path}") from exc
    except OSError as exc:
        raise ConnectionError_(f"Could not read {path}: {exc}") from exc


def write_file(session: dict, path: str, data: bytes) -> dict:
    """Upload *data* to the remote *path*, overwriting what is there."""
    if len(data) > MAX_UPLOAD_BYTES:
        raise ConnectionError_(
            f"Upload is {len(data) / 1e9:.1f} GB, above the "
            f"{MAX_UPLOAD_BYTES / 1e9:.0f} GB limit."
        )

    sftp = _sftp_for(session)
    try:
        with sftp.open(path, "wb") as handle:
            handle.write(data)
    except PermissionError as exc:
        raise ConnectionError_(f"Permission denied writing {path}") from exc
    except OSError as exc:
        raise ConnectionError_(f"Could not write {path}: {exc}") from exc

    logger.info("Uploaded %s bytes to %s", len(data), path)
    return {"path": path, "size": len(data)}


def delete_file(session: dict, path: str) -> dict:
    """Delete a remote file."""
    sftp = _sftp_for(session)
    try:
        sftp.remove(path)
    except FileNotFoundError as exc:
        raise ConnectionError_(f"No such file: {path}") from exc
    except PermissionError as exc:
        raise ConnectionError_(f"Permission denied deleting {path}") from exc
    except OSError as exc:
        raise ConnectionError_(f"Could not delete {path}: {exc}") from exc

    logger.info("Deleted %s", path)
    return {"path": path, "deleted": True}
