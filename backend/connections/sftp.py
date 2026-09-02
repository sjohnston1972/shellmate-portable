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
import re
import stat
from dataclasses import dataclass

import paramiko

from backend.connections.base import ConnectionError_
from backend.connections.ssh_handler import SSHHandler

logger = logging.getLogger(__name__)

# Cap on a single upload. Large enough for an IOS image, small enough that a
# runaway or mistaken upload cannot fill the device's flash unnoticed.
MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB


def _max_upload_bytes() -> int:
    """The cap on what may be pushed to a device."""
    try:
        from backend.advanced import get as advanced
        return int(advanced("files.max_upload_mb")) * 1024 * 1024
    except Exception:
        return MAX_UPLOAD_BYTES


def _max_download_bytes() -> int:
    """
    The cap on what may be pulled off one.

    Its own number rather than the upload cap, because the two are not the
    same shape: an upload is streamed from the request, while `read_file()`
    holds the whole thing in memory before it reaches the browser. Sharing
    one meant a row labelled *upload* deciding how much RSS a download could
    take in a portable process.
    """
    try:
        from backend.advanced import get as advanced
        return int(advanced("files.max_download_mb")) * 1024 * 1024
    except Exception:
        return 512 * 1024 * 1024


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
        if attrs.st_size and attrs.st_size > _max_download_bytes():
            raise ConnectionError_(
                f"{path} is {attrs.st_size / 1e6:.0f} MB, which is above the "
                f"{_max_download_bytes() / 1e6:.0f} MB download limit. "
                f"Raise it in Stockton if you mean to pull something this big."
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
    if len(data) > _max_upload_bytes():
        raise ConnectionError_(
            f"Upload is {len(data) / 1e9:.1f} GB, above the "
            f"{_max_upload_bytes() / 1e9:.0f} GB limit."
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


def rename(session: dict, path: str, new_path: str) -> dict:
    """Rename or move a file or directory (#418)."""
    sftp = _sftp_for(session)
    try:
        sftp.rename(path, new_path)
    except FileNotFoundError as exc:
        raise ConnectionError_(f"No such file: {path}") from exc
    except PermissionError as exc:
        raise ConnectionError_(f"Permission denied renaming {path}") from exc
    except OSError as exc:
        raise ConnectionError_(f"Could not rename {path}: {exc}") from exc
    logger.info("Renamed %s to %s", path, new_path)
    return {"path": new_path, "renamed_from": path}


def make_directory(session: dict, path: str) -> dict:
    """Create a directory (#418). Parents must exist."""
    sftp = _sftp_for(session)
    try:
        sftp.mkdir(path)
    except PermissionError as exc:
        raise ConnectionError_(f"Permission denied creating {path}") from exc
    except OSError as exc:
        raise ConnectionError_(f"Could not create {path}: {exc}") from exc
    logger.info("Created directory %s", path)
    return {"path": path, "created": True}


def set_permissions(session: dict, path: str, mode: str) -> dict:
    """
    chmod (#418). ``mode`` is octal text — "644", "0755" — as a person
    types it; anything else is refused before it reaches the device.
    """
    text = str(mode).strip()
    if not re.fullmatch(r"0?[0-7]{3,4}", text):
        raise ConnectionError_(f"'{mode}' is not an octal mode like 644 or 0755.")
    value = int(text, 8) & 0o7777
    sftp = _sftp_for(session)
    try:
        sftp.chmod(path, value)
    except FileNotFoundError as exc:
        raise ConnectionError_(f"No such file: {path}") from exc
    except PermissionError as exc:
        raise ConnectionError_(f"Permission denied changing {path}") from exc
    except OSError as exc:
        raise ConnectionError_(f"Could not change {path}: {exc}") from exc
    logger.info("chmod %o %s", value, path)
    return {"path": path, "mode": f"{value:o}"}


def _walk(sftp: paramiko.SFTPClient, path: str, budget: dict):
    """Yield (relative_path, attrs) for every file under *path*, bounded."""
    for attr in sftp.listdir_attr(path):
        child = f"{path.rstrip('/')}/{attr.filename}"
        budget["entries"] -= 1
        if budget["entries"] < 0:
            raise ConnectionError_(
                "That folder holds more entries than the transfer limit. "
                "Move in smaller pieces.")
        if stat.S_ISDIR(attr.st_mode or 0):
            yield from _walk(sftp, child, budget)
        else:
            yield child, attr


def delete_directory(session: dict, path: str) -> dict:
    """
    Delete a directory and everything beneath it (#418).

    Bounded by the same entry budget as a folder download, so a mistyped
    "/" does not become a device wipe: the walk refuses before removing
    anything.
    """
    if path.strip() in ("", "/", "."):
        raise ConnectionError_("Refusing to delete the root of the filesystem.")
    sftp = _sftp_for(session)
    try:
        budget = {"entries": _max_entries()}
        files = [child for child, _ in _walk(sftp, path, budget)]
        for child in files:
            sftp.remove(child)
        # Directories, deepest first.
        dirs: list[str] = []

        def collect(base: str) -> None:
            for attr in sftp.listdir_attr(base):
                if stat.S_ISDIR(attr.st_mode or 0):
                    child = f"{base.rstrip('/')}/{attr.filename}"
                    collect(child)
                    dirs.append(child)
        collect(path)
        for folder in reversed(dirs):
            sftp.rmdir(folder)
        sftp.rmdir(path)
    except FileNotFoundError as exc:
        raise ConnectionError_(f"No such directory: {path}") from exc
    except PermissionError as exc:
        raise ConnectionError_(f"Permission denied deleting {path}") from exc
    except OSError as exc:
        raise ConnectionError_(f"Could not delete {path}: {exc}") from exc
    logger.info("Deleted directory %s (%d files)", path, len(files))
    return {"path": path, "deleted": True, "files": len(files)}


def read_directory_zip(session: dict, path: str) -> bytes:
    """
    A directory as a zip archive, in memory (#418).

    The download limit applies to the total, and the entry budget to the
    count, so a whole flash: does not get pulled by accident.
    """
    import io
    import zipfile

    sftp = _sftp_for(session)
    buffer = io.BytesIO()
    total = 0
    limit = _max_download_bytes()
    base = path.rstrip("/") or "/"
    name = base.rsplit("/", 1)[-1] or "root"
    try:
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            budget = {"entries": _max_entries()}
            for child, attr in _walk(sftp, base, budget):
                total += attr.st_size or 0
                if total > limit:
                    raise ConnectionError_(
                        f"The folder is above the {limit / 1e6:.0f} MB download "
                        f"limit. Raise it in Stockton if you mean it.")
                with sftp.open(child, "rb") as handle:
                    handle.prefetch()
                    data = handle.read()
                inside = f"{name}/{child[len(base):].lstrip('/')}"
                archive.writestr(inside, data)
    except FileNotFoundError as exc:
        raise ConnectionError_(f"No such directory: {path}") from exc
    except PermissionError as exc:
        raise ConnectionError_(f"Permission denied reading {path}") from exc
    except OSError as exc:
        raise ConnectionError_(f"Could not read {path}: {exc}") from exc
    return buffer.getvalue()


def _max_entries() -> int:
    """Files a folder operation may touch. Bounded in Stockton."""
    try:
        return int(advanced("files.max_folder_entries"))
    except Exception:
        return 2000


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
