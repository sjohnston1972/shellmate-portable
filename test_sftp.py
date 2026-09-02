"""
test_sftp.py — Tests for SFTP file transfer over an SSH session.

Runs against a real in-process SSH server with an SFTP subsystem, backed by a
temporary directory, rather than mocking paramiko.  The parts of this feature
most likely to break are the ones that only exist when a real SFTP channel is
involved: directory listings, the multiplexed channel opened lazily on the
existing transport, and the error paths for devices that offer no SFTP at all.

Also covers SSH key authentication, since the server here can require it and
a live device cannot be scripted into rejecting a password on demand.

    python test_sftp.py
"""

import os
import shutil
import socket
import sys
import tempfile
import threading
from pathlib import Path

import paramiko

from backend.connections.base import ConnectionError_, ConnectionParams
from backend.connections.manager import SessionManager
from backend.connections import sftp as sftp_module

passed = 0
failed: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    global passed
    if condition:
        passed += 1
        print(f"  OK  {name}")
    else:
        failed.append(f"{name}: {detail}")
        print(f"  FAIL {name}\n       {detail}")


# ---------------------------------------------------------------------------
# An in-process SSH server with an SFTP subsystem
# ---------------------------------------------------------------------------


class _Server(paramiko.ServerInterface):
    """Accepts one username with either a fixed password or a known key."""

    def __init__(self, password: str | None, public_key: paramiko.PKey | None):
        self.password = password
        self.public_key = public_key

    def check_auth_password(self, username, password):
        if self.password is not None and password == self.password:
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def check_auth_publickey(self, username, key):
        if self.public_key is not None and key == self.public_key:
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def get_allowed_auths(self, username):
        allowed = []
        if self.password is not None:
            allowed.append("password")
        if self.public_key is not None:
            allowed.append("publickey")
        return ",".join(allowed) or "none"

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED

    def check_channel_shell_request(self, channel):
        return True

    def check_channel_pty_request(self, *args, **kwargs):
        return True


class _SFTPHandle(paramiko.SFTPHandle):
    def stat(self):
        try:
            return paramiko.SFTPAttributes.from_stat(os.fstat(self.readfile.fileno()))
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)


class _SFTPServer(paramiko.SFTPServerInterface):
    """Serves a single directory from the local filesystem."""

    ROOT = ""

    def _real(self, path: str) -> str:
        # Map the client's absolute-looking path onto the temp root.
        return os.path.join(self.ROOT, path.lstrip("/\\").replace("/", os.sep))

    def list_folder(self, path):
        try:
            out = []
            for name in os.listdir(self._real(path)):
                attr = paramiko.SFTPAttributes.from_stat(
                    os.stat(os.path.join(self._real(path), name))
                )
                attr.filename = name
                out.append(attr)
            return out
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)

    def stat(self, path):
        try:
            return paramiko.SFTPAttributes.from_stat(os.stat(self._real(path)))
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)

    lstat = stat

    def open(self, path, flags, attr):
        try:
            mode = "rb"
            if flags & os.O_WRONLY or flags & os.O_RDWR:
                mode = "wb"
            handle = _SFTPHandle(flags)
            fileobj = open(self._real(path), mode)
            handle.filename = path
            handle.readfile = fileobj
            handle.writefile = fileobj
            return handle
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)

    def remove(self, path):
        try:
            os.remove(self._real(path))
            return paramiko.SFTP_OK
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)

    def rename(self, oldpath, newpath):
        try:
            os.rename(self._real(oldpath), self._real(newpath))
            return paramiko.SFTP_OK
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)

    def mkdir(self, path, attr):
        try:
            os.mkdir(self._real(path))
            return paramiko.SFTP_OK
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)

    def rmdir(self, path):
        try:
            os.rmdir(self._real(path))
            return paramiko.SFTP_OK
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)

    def chattr(self, path, attr):
        try:
            if attr.st_mode is not None:
                os.chmod(self._real(path), attr.st_mode & 0o7777)
            return paramiko.SFTP_OK
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)

    def canonicalize(self, path):
        # Keep everything inside the served root; "." resolves to "/".
        if path in ("", "."):
            return "/"
        return "/" + path.lstrip("/").replace("\\", "/")


class SSHTestServer:
    """
    A real SSH server on localhost, optionally with an SFTP subsystem.

    Each connection is handled on its own thread. Only one client at a time is
    needed, which keeps this small.
    """

    def __init__(self, root: str, password: str | None = "secret",
                 public_key: paramiko.PKey | None = None, enable_sftp: bool = True):
        self.root = root
        self.password = password
        self.public_key = public_key
        self.enable_sftp = enable_sftp
        self.host_key = paramiko.RSAKey.generate(2048)

        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(5)
        self.port = self._listener.getsockname()[1]

        self._stop = threading.Event()
        self._transports: list[paramiko.Transport] = []
        threading.Thread(target=self._accept_loop, daemon=True).start()

    def _accept_loop(self):
        self._listener.settimeout(0.5)
        while not self._stop.is_set():
            try:
                conn, _ = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        try:
            transport = paramiko.Transport(conn)
            transport.add_server_key(self.host_key)
            if self.enable_sftp:
                _SFTPServer.ROOT = self.root
                transport.set_subsystem_handler(
                    "sftp", paramiko.SFTPServer, _SFTPServer
                )
            transport.start_server(server=_Server(self.password, self.public_key))
            self._transports.append(transport)
            # Hold the connection open; the client drives from here.
            while not self._stop.is_set() and transport.is_active():
                self._stop.wait(0.2)
        except Exception:
            pass

    def close(self):
        self._stop.set()
        for transport in self._transports:
            try:
                transport.close()
            except Exception:
                pass
        try:
            self._listener.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_sftp_operations() -> None:
    """List, read, write and delete over a real SFTP channel."""
    print("\n-- SFTP operations --")
    root = tempfile.mkdtemp(prefix="shellmate-sftp-")
    # write_bytes, not write_text: on Windows text mode rewrites "\n" as
    # "\r\n", which would make the size and content assertions below test
    # Python's newline translation rather than the transfer.
    config = b"hostname switch01\n"
    (Path(root) / "running-config.txt").write_bytes(config)
    (Path(root) / "subdir").mkdir()

    server = SSHTestServer(root)
    manager = SessionManager()
    session_id = None
    try:
        created = manager.create_session(ConnectionParams(
            connection_type="ssh", hostname="127.0.0.1", port=server.port,
            username="neteng", password="secret",
        ))
        session_id = created["session_id"]
        session = manager.get_session(session_id)

        listing = sftp_module.list_directory(session, ".")
        names = {e["name"] for e in listing["entries"]}
        check("lists remote files", "running-config.txt" in names, f"got {names}")
        check("lists remote directories", "subdir" in names, f"got {names}")

        dirs_first = [e["is_dir"] for e in listing["entries"]]
        check("directories sort before files", dirs_first == sorted(dirs_first, reverse=True),
              f"got {[e['name'] for e in listing['entries']]}")

        entry = next(e for e in listing["entries"] if e["name"] == "running-config.txt")
        check("reports file size", entry["size"] == len("hostname switch01\n"),
              f"got {entry['size']}")
        check("reports permissions", entry["permissions"].startswith("-"),
              f"got {entry['permissions']}")

        content = sftp_module.read_file(session, "/running-config.txt")
        check("downloads file contents", content == b"hostname switch01\n", f"got {content!r}")

        sftp_module.write_file(session, "/uploaded.cfg", b"interface Gi0/1\n")
        check("upload lands on disk",
              (Path(root) / "uploaded.cfg").read_bytes() == b"interface Gi0/1\n")

        sftp_module.delete_file(session, "/uploaded.cfg")
        check("delete removes the file", not (Path(root) / "uploaded.cfg").exists())

        check("missing file raises a clear error",
              _raises_message(lambda: sftp_module.read_file(session, "/nope.txt"), "No such file"))

        # The folder operations (#418).
        sftp_module.make_directory(session, "/newdir")
        check("mkdir creates the folder", (Path(root) / "newdir").is_dir())
        sftp_module.write_file(session, "/newdir/a.txt", b"a\n")
        sftp_module.make_directory(session, "/newdir/inner")
        sftp_module.write_file(session, "/newdir/inner/b.txt", b"bb\n")
        sftp_module.rename(session, "/newdir/a.txt", "/newdir/renamed.txt")
        check("rename moves the file", (Path(root) / "newdir" / "renamed.txt").exists()
              and not (Path(root) / "newdir" / "a.txt").exists())
        import zipfile, io
        archive = zipfile.ZipFile(io.BytesIO(sftp_module.read_directory_zip(session, "/newdir")))
        names_in_zip = set(archive.namelist())
        check("a folder downloads as a zip with its tree",
              {"newdir/renamed.txt", "newdir/inner/b.txt"} <= names_in_zip, str(names_in_zip))
        check("chmod refuses a mode that is not octal",
              _raises_message(lambda: sftp_module.set_permissions(session, "/newdir/renamed.txt", "rw"), "octal"))
        result = sftp_module.set_permissions(session, "/newdir/renamed.txt", "0600")
        check("chmod reports the mode it set", result["mode"] == "600", str(result))
        check("deleting the root is refused",
              _raises_message(lambda: sftp_module.delete_directory(session, "/"), "root"))
        gone = sftp_module.delete_directory(session, "/newdir")
        check("a folder delete takes the tree with it",
              not (Path(root) / "newdir").exists() and gone["files"] == 2, str(gone))

        check("SFTP channel is cached on the session", session.get("sftp") is not None)

    finally:
        if session_id:
            manager.destroy_session(session_id)
        server.close()
        shutil.rmtree(root, ignore_errors=True)


def test_sftp_channel_closed_with_session() -> None:
    """A destroyed session must not leave an SFTP channel behind."""
    print("\n-- SFTP teardown --")
    root = tempfile.mkdtemp(prefix="shellmate-sftp-")
    server = SSHTestServer(root)
    manager = SessionManager()
    try:
        created = manager.create_session(ConnectionParams(
            connection_type="ssh", hostname="127.0.0.1", port=server.port,
            username="neteng", password="secret",
        ))
        session = manager.get_session(created["session_id"])
        sftp_module.list_directory(session, ".")
        check("channel opened", session.get("sftp") is not None)

        manager.destroy_session(created["session_id"])
        check("channel cleared on destroy", session.get("sftp") is None)
    finally:
        server.close()
        shutil.rmtree(root, ignore_errors=True)


def test_device_without_sftp() -> None:
    """Network gear commonly has SSH but no SFTP; say so plainly."""
    print("\n-- Device with no SFTP subsystem --")
    root = tempfile.mkdtemp(prefix="shellmate-nosftp-")
    server = SSHTestServer(root, enable_sftp=False)
    manager = SessionManager()
    session_id = None
    try:
        created = manager.create_session(ConnectionParams(
            connection_type="ssh", hostname="127.0.0.1", port=server.port,
            username="neteng", password="secret",
        ))
        session_id = created["session_id"]
        session = manager.get_session(session_id)

        check("explains the device has no SFTP subsystem",
              _raises_message(lambda: sftp_module.list_directory(session, "."), "SFTP"))
    finally:
        if session_id:
            manager.destroy_session(session_id)
        server.close()
        shutil.rmtree(root, ignore_errors=True)


def test_key_authentication() -> None:
    """A private key should authenticate, and a wrong one should not."""
    print("\n-- SSH key authentication --")
    root = tempfile.mkdtemp(prefix="shellmate-key-")
    key = paramiko.Ed25519Key.generate() if hasattr(paramiko.Ed25519Key, "generate") \
        else paramiko.RSAKey.generate(2048)
    key_path = Path(root) / "id_key"
    key.write_private_key_file(str(key_path))

    server = SSHTestServer(root, password=None, public_key=key)
    manager = SessionManager()
    session_id = None
    try:
        created = manager.create_session(ConnectionParams(
            connection_type="ssh", hostname="127.0.0.1", port=server.port,
            username="neteng", private_key_path=str(key_path),
        ))
        session_id = created["session_id"]
        check("connects using a private key", created["is_connected"])

        wrong = paramiko.RSAKey.generate(2048)
        wrong_path = Path(root) / "wrong_key"
        wrong.write_private_key_file(str(wrong_path))
        # Names the key it tried, rather than the old "Authentication failed.
        # Check the username, password or key" — which listed three things to
        # check and told you nothing about which of them went wrong.
        check("rejects the wrong key, and says which key",
              _raises_message(
                  lambda: manager.create_session(ConnectionParams(
                      connection_type="ssh", hostname="127.0.0.1", port=server.port,
                      username="neteng", private_key_path=str(wrong_path),
                  )),
                  "refused the key wrong_key",
              ))

        check("missing key file is reported clearly",
              _raises_message(
                  lambda: manager.create_session(ConnectionParams(
                      connection_type="ssh", hostname="127.0.0.1", port=server.port,
                      username="neteng", private_key_path=str(Path(root) / "absent"),
                  )),
                  "not found",
              ))
    finally:
        if session_id:
            manager.destroy_session(session_id)
        server.close()
        shutil.rmtree(root, ignore_errors=True)


def test_secondary_channel() -> None:
    """
    Background features depend on opening a second channel on one transport.

    Must return None rather than raising when a device refuses, since many
    switches cap concurrent sessions at one.
    """
    print("\n-- Secondary channel --")
    root = tempfile.mkdtemp(prefix="shellmate-chan-")
    server = SSHTestServer(root)
    manager = SessionManager()
    session_id = None
    try:
        created = manager.create_session(ConnectionParams(
            connection_type="ssh", hostname="127.0.0.1", port=server.port,
            username="neteng", password="secret",
        ))
        session_id = created["session_id"]
        handler = manager.get_session(session_id)["handler"]

        channel = handler.open_secondary_channel()
        check("opens a second channel on the same transport", channel is not None)
        if channel:
            channel.close()

        handler.disconnect()
        check("returns None rather than raising once disconnected",
              handler.open_secondary_channel() is None)
    finally:
        if session_id:
            manager.destroy_session(session_id)
        server.close()
        shutil.rmtree(root, ignore_errors=True)


def _raises_message(fn, fragment: str) -> bool:
    """True if calling fn raises ConnectionError_ mentioning *fragment*."""
    try:
        fn()
    except ConnectionError_ as exc:
        return fragment.lower() in str(exc).lower()
    except Exception:
        return False
    return False


def main() -> int:
    print("=" * 52)
    print("  SFTP and SSH authentication tests")
    print("=" * 52)

    for test in (
        test_sftp_operations,
        test_sftp_channel_closed_with_session,
        test_device_without_sftp,
        test_key_authentication,
        test_secondary_channel,
    ):
        try:
            test()
        except Exception as exc:
            failed.append(f"{test.__name__}: raised {type(exc).__name__}: {exc}")
            print(f"  FAIL {test.__name__} raised {type(exc).__name__}: {exc}")

    print("\n" + "=" * 52)
    print(f"  {passed} passed  |  {len(failed)} failed")
    print("=" * 52)
    if failed:
        print("\nFAILURES:")
        for item in failed:
            print(f"  {item}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
