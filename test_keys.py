"""
test_keys.py — Generating SSH keys, and never handing one out.

The value of this feature is that nobody has to leave ShellMate to make a key.
The risk is entirely in the private half: it exists on disk and must exist
nowhere else — not in an API response, not in a listing, not in a log.

Two other properties matter enough to test. The key has to be *usable*, which
means paramiko must load what was written — a key ShellMate makes and ShellMate
cannot then use would be worse than none. And the path handling has to be
hostile-input safe: every mutating call takes a path from the browser, and a
delete endpoint that accepts an arbitrary one is a file-removal API with a
friendly name.

    python test_keys.py
"""

import shutil
import sys
import tempfile
from pathlib import Path

from backend import paths

_TEMP = Path(tempfile.mkdtemp(prefix="shellmate-keys-"))
paths._data_dir_cache = _TEMP

from backend import keys                                   # noqa: E402

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


def clean() -> None:
    shutil.rmtree(keys.keys_dir(), ignore_errors=True)


def test_generating() -> None:
    print("\n-- Making keys --")
    clean()

    cases = [
        ("id_ed25519", {"kind": "ed25519"}, "ed25519", 256),
        ("id_rsa",     {"kind": "rsa", "bits": 2048}, "rsa-2048", 2048),
        ("id_ecdsa",   {"kind": "ecdsa", "curve": "p384"}, "ecdsa-p384", 384),
    ]
    for name, options, kind, bits in cases:
        info = keys.generate(name, comment="neteng@laptop", **options)
        check(f"{kind} is generated", info.kind == kind, f"got {info.kind}")
        check(f"  and reports {bits} bits", info.bits == bits, f"got {info.bits}")
        check("  with both fingerprints",
              info.fingerprint_sha256.startswith("SHA256:")
              and info.fingerprint_md5.startswith("MD5:"))
        check("  and the comment reaches the public key",
              info.comment == "neteng@laptop" and "neteng@laptop" in info.public_key)
        check("  and a .pub is written beside it",
              Path(info.public_path).exists())

    check("fingerprints differ between keys",
          len({k["fingerprint_sha256"] for k in keys.listing()}) == 3)


def test_paramiko_can_use_them() -> None:
    """A key ShellMate makes and cannot then use would be worse than none."""
    print("\n-- Usable by the SSH client that will use them --")
    import paramiko

    clean()
    plain = keys.generate("usable_ed25519", kind="ed25519")
    locked = keys.generate("usable_rsa", kind="rsa", bits=2048, passphrase="hunter2")

    try:
        paramiko.Ed25519Key.from_private_key_file(plain.path)
        check("paramiko loads the ed25519 key", True)
    except Exception as exc:
        check("paramiko loads the ed25519 key", False, str(exc))

    try:
        paramiko.RSAKey.from_private_key_file(locked.path, password="hunter2")
        check("paramiko loads the encrypted RSA key with its passphrase", True)
    except Exception as exc:
        check("paramiko loads the encrypted RSA key with its passphrase", False, str(exc))

    try:
        paramiko.RSAKey.from_private_key_file(locked.path)
        check("and refuses it without one", False, "it loaded unencrypted")
    except Exception:
        check("and refuses it without one", True)


def test_fingerprints_match_ssh_keygen() -> None:
    """
    The fingerprint is only useful if it is the same one everyone else shows.

    Its entire purpose is comparing what a device prints against what you
    hold. A fingerprint computed slightly differently would look completely
    plausible and be worthless — so it is checked against ssh-keygen itself
    where that is available, rather than against our own arithmetic.
    """
    print("\n-- Fingerprints, against ssh-keygen --")
    import subprocess

    if not shutil.which("ssh-keygen"):
        print("       (ssh-keygen not on PATH — skipped)")
        return

    clean()
    info = keys.generate("fp_check", kind="ed25519", comment="neteng@laptop")

    for label, flag, ours in (
        ("SHA256", [], info.fingerprint_sha256),
        ("MD5", ["-E", "md5"], info.fingerprint_md5),
    ):
        result = subprocess.run(
            ["ssh-keygen", "-l", *flag, "-f", info.public_path],
            capture_output=True, text=True, timeout=15,
        )
        parts = result.stdout.split()
        theirs = parts[1] if len(parts) > 1 else ""
        check(f"{label} matches ssh-keygen", ours == theirs,
              f"ours {ours!r} != theirs {theirs!r}")

    result = subprocess.run(
        ["ssh-keygen", "-l", "-f", info.public_path],
        capture_output=True, text=True, timeout=15,
    )
    check("and ssh-keygen reads the public key at all",
          result.returncode == 0, result.stderr.strip())


def test_the_private_half_never_escapes() -> None:
    print("\n-- The private half stays on disk --")
    clean()
    info = keys.generate("secret_key", kind="ed25519", passphrase="hunter2")

    for label, blob in (
        ("as_dict()",  str(info.as_dict())),
        ("listing()",  str(keys.listing())),
        ("describe()", str(keys.describe(Path(info.path)).as_dict())),
    ):
        check(f"{label} carries no private key material",
              "PRIVATE KEY" not in blob and "OPENSSH PRIVATE" not in blob,
              "private material reached a public structure")

    check("nor the passphrase", "hunter2" not in str(keys.listing()))
    check("but the public key is there", "ssh-ed25519" in info.public_key)

    on_disk = Path(info.path).read_text(encoding="utf-8")
    check("the private key really was written", "PRIVATE KEY" in on_disk)


def test_passphrases() -> None:
    print("\n-- Passphrases --")
    clean()
    locked = keys.generate("locked", kind="ed25519", passphrase="first")
    plain = keys.generate("plain", kind="ed25519")

    check("a key made with one reports as encrypted", locked.encrypted)
    check("and one made without does not", not plain.encrypted)

    for label, args in (
        ("the wrong passphrase is refused", (locked.path, "wrong", "x")),
        ("a missing passphrase is refused", (locked.path, "", "x")),
        ("and an old one on an unencrypted key is refused", (plain.path, "any", "x")),
    ):
        try:
            keys.change_passphrase(*args)
            check(label, False, "it was accepted")
        except ValueError:
            check(label, True)

    keys.change_passphrase(locked.path, "first", "second")
    check("changing it keeps the key encrypted",
          keys.describe(Path(locked.path)).encrypted)

    keys.change_passphrase(locked.path, "second", "")
    check("removing it leaves the key usable",
          not keys.describe(Path(locked.path)).encrypted)

    keys.change_passphrase(plain.path, "", "added")
    check("adding one to a bare key works",
          keys.describe(Path(plain.path)).encrypted)


def test_refusals() -> None:
    print("\n-- What it will not do --")
    clean()
    keys.generate("taken", kind="ed25519")

    for label, call in (
        ("overwrite an existing key", lambda: keys.generate("taken", kind="ed25519")),
        ("make an unknown type",      lambda: keys.generate("x", kind="dsa")),
        ("make weak RSA",            lambda: keys.generate("y", kind="rsa", bits=1024)),
        ("use an unknown curve",     lambda: keys.generate("z", kind="ecdsa", curve="p192")),
    ):
        try:
            call()
            check(f"refuses to {label}", False, "it was allowed")
        except ValueError:
            check(f"refuses to {label}", True)


def test_paths_from_the_browser_are_not_trusted() -> None:
    """Every mutating call takes a path the browser supplied."""
    print("\n-- Hostile paths --")
    clean()
    keys.generate("real", kind="ed25519")

    outside = _TEMP / "not-a-key.txt"
    outside.write_text("important", encoding="utf-8")

    escapes = [
        str(outside),
        str(keys.keys_dir() / ".." / "not-a-key.txt"),
        str(_TEMP / "settings.json"),
    ]
    for candidate in escapes:
        for label, call in (("delete", keys.delete),
                            ("change the passphrase on",
                             lambda p: keys.change_passphrase(p, "", ""))):
            try:
                call(candidate)
                check(f"refuses to {label} {Path(candidate).name}", False, "it was allowed")
            except ValueError:
                check(f"refuses to {label} {Path(candidate).name}", True)

    check("and the file outside is untouched", outside.exists()
          and outside.read_text(encoding="utf-8") == "important")

    # Names a filesystem would refuse, or that try to walk out of the folder.
    check("a traversing name is neutralised",
          "/" not in keys.safe_name("../../etc/passwd")
          and "\\" not in keys.safe_name("..\\..\\windows"))
    check("an empty name still yields something",
          keys.safe_name("") == "id_shellmate")


def test_the_manual_covers_keys() -> None:
    """
    The bundled manual has a page on keys, reachable from the manual's list,
    and it answers the questions #400 was opened for.

    A page that exists in the folder but not in docs.js is invisible, and a
    page that quietly lost its IOS example would send the reader back to
    a search engine on the one platform where the paste is fiddly.
    """
    print("\n-- The manual --")
    root = Path(__file__).parent
    page = root / "frontend" / "docs" / "ssh-keys.md"
    check("ssh-keys.md exists", page.exists())
    if not page.exists():
        return

    docs_js = (root / "frontend" / "js" / "docs.js").read_text(encoding="utf-8")
    check("ssh-keys.md is in PAGES", "ssh-keys.md" in docs_js,
          "the file exists but nothing links to it")

    text = page.read_text(encoding="utf-8")
    expected = {
        "why keys over passwords":  "## Why keys",
        "which type to choose":     "Ed25519",
        "the IOS example":          "ip ssh pubkey-chain",
        "the NX-OS example":        "sshkey",
        "the Junos example":        "set system login user",
        "the Linux example":        "authorized_keys",
        "attaching to a connection": "SSH — key or jump host",
        "passphrase handling":      "### Passphrases",
        "jump hosts":               "## Jump hosts",
        "troubleshooting":          "## Troubleshooting",
        "the PuTTY format trap":    ".ppk",
        "the MD5 hash comparison":  "MD5",
    }
    for what, needle in expected.items():
        check(f"the page covers {what}", needle in text, f"missing {needle!r}")

    # Every message the page quotes has to be one the handler actually
    # raises, or the reader searches for text that never appears.
    handler = (root / "backend" / "connections" / "ssh_handler.py").read_text(encoding="utf-8")
    for phrase in ("Private key not found",
                   "is encrypted and the passphrase was",
                   "Unsupported or malformed private key",
                   "no password was given to fall back to",
                   "It will only accept:"):
        check(f"quoted message exists: {phrase!r}", phrase in handler)

    # Pages link to each other by file name; a link to a page that is not
    # registered silently does nothing when clicked.
    for other in ("connecting.md", "credentials.md", "troubleshooting.md"):
        source = (root / "frontend" / "docs" / other).read_text(encoding="utf-8")
        check(f"{other} links to the page", "(#ssh-keys)" in source)


def test_import_and_delete() -> None:
    print("\n-- Importing and removing --")
    clean()

    # Somewhere outside the keys folder, as a real import would be.
    elsewhere = _TEMP / "external"
    elsewhere.mkdir(exist_ok=True)
    original = keys.generate("to_move", kind="ed25519")
    moved = elsewhere / "id_external"
    shutil.move(original.path, moved)
    shutil.move(original.public_path, moved.with_suffix(".pub"))

    imported = keys.import_key(str(moved))
    check("an existing key can be imported", Path(imported.path).exists())
    check("into the keys folder", keys.keys_dir() in Path(imported.path).parents)
    check("keeping its fingerprint",
          imported.fingerprint_sha256 == original.fingerprint_sha256)
    check("and the original is left where it was", moved.exists())

    try:
        keys.import_key(str(elsewhere / "does-not-exist"))
        check("importing something absent is refused", False, "it was allowed")
    except ValueError:
        check("importing something absent is refused", True)

    check("deleting removes it", keys.delete(imported.path))
    check("and takes the .pub with it",
          not Path(imported.public_path).exists())


# ---------------------------------------------------------------------------
# Host keys — trust on first use, warn on change (#528)
#
# The half of SSH identity that was missing. The default policy auto-added an
# unknown key and nothing ever wrote it down, so a key that *changed* was
# never noticed — and a changed key is the one event here that carries
# information. These tests drive a real paramiko server and swap its host key
# under the client, which is what an RMA, a re-image and a man in the middle
# all look like from this side.
# ---------------------------------------------------------------------------


def _device_server(host_key_holder):
    """
    A password-only SSH server whose host key can be swapped between
    connections. Returns (port, listener).
    """
    import socket
    import threading

    import paramiko

    class Device(paramiko.ServerInterface):
        def get_allowed_auths(self, username):
            return "password"

        def check_auth_password(self, username, password):
            return (paramiko.AUTH_SUCCESSFUL if password == "letmein"
                    else paramiko.AUTH_FAILED)

        def check_channel_request(self, kind, chanid):
            return (paramiko.OPEN_SUCCEEDED if kind == "session"
                    else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED)

        def check_channel_pty_request(self, *args, **kwargs):
            return True

        def check_channel_shell_request(self, channel):
            return True

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    port = listener.getsockname()[1]
    keep: list = []

    def run():
        while True:
            try:
                sock, _ = listener.accept()
            except OSError:
                return
            transport = paramiko.Transport(sock)
            transport.add_server_key(host_key_holder[0])
            try:
                transport.start_server(server=Device())
            except Exception:
                continue
            keep.append((transport, transport.accept(10)))

    threading.Thread(target=run, daemon=True).start()
    return port, listener


def test_the_host_key_store() -> None:
    print("\n-- The host key store --")
    import paramiko

    from backend.connections import ssh_handler

    # OpenSSH's spelling, because an entry filed under the wrong name is an
    # entry nothing ever looks up — and every connection to a device on 2222
    # would look like a first connection.
    check("port 22 is spelled bare", keys.host_entry_name("sw1", 22) == "sw1")
    check("  and any other port is bracketed",
          keys.host_entry_name("sw1", 2222) == "[sw1]:2222")

    key_a = paramiko.RSAKey.generate(2048)
    key_b = paramiko.RSAKey.generate(2048)
    fp_a = keys.host_fingerprint(key_a)

    check("a fingerprint is spelled the way ssh-keygen spells one",
          fp_a.startswith("SHA256:") and "=" not in fp_a, fp_a)
    check("  and two keys do not share one", fp_a != keys.host_fingerprint(key_b))

    check("nothing is known before anything connects",
          keys.known_host_key("sw-store", 22) is None)

    keys.remember_host("sw-store", 22, key_a)
    check("a remembered key reads back", keys.known_host_key("sw-store", 22) == key_a)
    check("  and is listed for the panel",
          any(row["host"] == "sw-store" and row["fingerprint"] == fp_a
              for row in keys.known_hosts()))

    # The whole point of trust-on-first-use: a device already known is not
    # asked about again, and the same key is silence.
    check("the same key again is not an event",
          ssh_handler._verify_host_key("sw-store", 22, key_a) == fp_a)

    changed = None
    try:
        ssh_handler._verify_host_key("sw-store", 22, key_b)
    except Exception as exc:
        changed = exc
    check("a different key stops the connection",
          type(changed).__name__ == "HostKeyChanged", repr(changed))
    if changed is not None and hasattr(changed, "as_dict"):
        payload = changed.as_dict()
        check("  carrying both fingerprints, so it can be read out",
              payload["old_fingerprint"] == fp_a
              and payload["new_fingerprint"] == keys.host_fingerprint(key_b),
              str(payload))
        check("  and the store is untouched until somebody answers",
              keys.known_host_key("sw-store", 22) == key_a)

    check("trusting the new key replaces the old one",
          ssh_handler._verify_host_key("sw-store", 22, key_b, trust_new=True)
          == keys.host_fingerprint(key_b))
    check("  and the store now holds it",
          keys.known_host_key("sw-store", 22) == key_b)

    # A different algorithm from the same device is not a changed key. Raising
    # the alarm for that is how somebody learns to click through the warning
    # that matters.
    other_type = paramiko.ECDSAKey.generate()
    check("another algorithm from the same host is not a change",
          bool(ssh_handler._verify_host_key("sw-store", 22, other_type)))

    # Forgetting one, which is the honest answer when the thing answering
    # really is a different machine at the same address.
    #
    # This has a specific history. The suite carried an intermittent for
    # weeks (#586) and it was this: test_sftp.py stands up five fake SSH
    # servers, each with a freshly generated host key, each on an
    # ephemeral port — and the OS recycles ephemeral ports. When a later
    # server landed on a port an earlier one had used, ShellMate compared
    # the new key against the remembered one and refused, exactly as it
    # should. Nothing was wrong with the code; the fixture was two
    # different machines claiming to be one host. It bit about one full
    # run in three and never when the file ran alone, because forty other
    # files cycle through enough ports to make a collision likely.
    #
    # The fixture now forgets the key for the port it just took, through
    # the `forget_host` the Keys panel already used, and this is the
    # assertion that the operation it depends on keeps working —
    # including that it forgets *only* the host it was asked about.
    keys.remember_host("sw-keep", 22, key_a)
    check("forgetting a host that is known says so",
          keys.forget_host(keys.host_entry_name("sw-store", 22)) is True)
    check("  and it is unknown afterwards",
          keys.known_host_key("sw-store", 22) is None,
          "a recycled port is genuinely a host nobody has seen")
    check("  while every other host is left alone",
          keys.known_host_key("sw-keep", 22) == key_a,
          "rewriting the file must not take the rest of the estate with it")
    check("forgetting one that was never known says that instead",
          keys.forget_host(keys.host_entry_name("sw-store", 22)) is False)
    check("  and a fresh key is then simply a first sighting",
          ssh_handler._verify_host_key("sw-store", 22, key_b)
          == keys.host_fingerprint(key_b),
          "which is the whole point: no warning where there is no evidence")

    check("forgetting a host is the way back", keys.forget_host("sw-store"))
    check("  and it is then unknown again",
          keys.known_host_key("sw-store", 22) is None)
    check("forgetting something unknown is not an error",
          keys.forget_host("sw-store") is False)


def test_a_changed_key_reaches_the_user() -> None:
    print("\n-- A changed host key, end to end --")
    import paramiko

    from backend.connections.base import ConnectionParams, HostKeyChanged
    from backend.connections.ssh_handler import SSHHandler

    key_a = paramiko.RSAKey.generate(2048)
    key_b = paramiko.RSAKey.generate(2048)
    holder = [key_a]
    port, listener = _device_server(holder)

    def params(**extra):
        return ConnectionParams(hostname="127.0.0.1", port=port,
                                username="neteng", password="letmein", **extra)

    try:
        first = SSHHandler(params=params())
        first.connect()
        check("the first connection is trusted without asking", first.is_connected)
        check("  and the key is remembered",
              keys.known_host_key("127.0.0.1", port) == key_a)
        check("  and the session can say what it connected to",
              first.host_key_fingerprint == keys.host_fingerprint(key_a),
              first.host_key_fingerprint)
        first.disconnect()

        holder[0] = key_b                       # the device is re-imaged, or is not the device
        second = SSHHandler(params=params())
        raised = None
        try:
            second.connect()
        except HostKeyChanged as exc:
            raised = exc
        except Exception as exc:
            check("a changed key stops the connection", False,
                  f"{type(exc).__name__}: {exc}")
        check("a changed key stops the connection", raised is not None)
        if raised is not None:
            check("  naming both fingerprints",
                  raised.old_fingerprint == keys.host_fingerprint(key_a)
                  and raised.new_fingerprint == keys.host_fingerprint(key_b),
                  f"{raised.old_fingerprint} -> {raised.new_fingerprint}")
        check("  and no session was left open", not second.is_connected)
        check("  and nothing was written to the store",
              keys.known_host_key("127.0.0.1", port) == key_a)

        third = SSHHandler(params=params(trust_new_host_key=True))
        third.connect()
        check("answering 'trust the new key' connects", third.is_connected)
        check("  and the new key replaces the old one",
              keys.known_host_key("127.0.0.1", port) == key_b)
        third.disconnect()
    finally:
        listener.close()
        keys.forget_host(keys.host_entry_name("127.0.0.1", port))


def test_the_api_asks_rather_than_deciding() -> None:
    print("\n-- The API --")
    import paramiko
    from fastapi.testclient import TestClient

    from backend.app import app

    key_a = paramiko.RSAKey.generate(2048)
    key_b = paramiko.RSAKey.generate(2048)
    holder = [key_a]
    port, listener = _device_server(holder)
    client = TestClient(app, base_url="http://127.0.0.1")

    body = {"connection_type": "ssh", "hostname": "127.0.0.1", "port": port,
            "username": "neteng", "password": "letmein", "display_label": "hk"}
    try:
        first = client.post("/api/sessions", json=body)
        check("the first connection succeeds", first.status_code == 200,
              f"got {first.status_code} {first.text[:160]}")
        if first.status_code == 200:
            client.delete(f"/api/sessions/{first.json()['session_id']}")

        holder[0] = key_b
        second = client.post("/api/sessions", json=body)
        check("a changed key answers 409, not 200 and not 400",
              second.status_code == 409, f"got {second.status_code} {second.text[:160]}")
        detail = second.json().get("detail", {}) if second.status_code == 409 else {}
        changed = detail.get("host_key") or {}
        check("  with both fingerprints for the dialog",
              changed.get("old_fingerprint") == keys.host_fingerprint(key_a)
              and changed.get("new_fingerprint") == keys.host_fingerprint(key_b),
              str(changed))

        third = client.post("/api/sessions", json={**body, "trust_new_host_key": True})
        check("and the retry that trusts it connects", third.status_code == 200,
              f"got {third.status_code} {third.text[:160]}")
        if third.status_code == 200:
            client.delete(f"/api/sessions/{third.json()['session_id']}")

        listed = client.get("/api/keys/known-hosts")
        check("the panel can list what is trusted", listed.status_code == 200)
        hosts = listed.json().get("hosts", []) if listed.status_code == 200 else []
        name = keys.host_entry_name("127.0.0.1", port)
        check("  including this device", any(h["host"] == name for h in hosts))

        forgotten = client.post("/api/keys/known-hosts/forget", json={"host": name})
        check("and Forget removes it",
              forgotten.status_code == 200 and forgotten.json().get("removed") is True,
              forgotten.text[:120])
    finally:
        listener.close()
        keys.forget_host(keys.host_entry_name("127.0.0.1", port))


def main() -> int:
    print("\n" + "=" * 52)
    print("  SSH keys")
    print("=" * 52)

    for test in (
        test_generating,
        test_paramiko_can_use_them,
        test_fingerprints_match_ssh_keygen,
        test_the_private_half_never_escapes,
        test_passphrases,
        test_refusals,
        test_paths_from_the_browser_are_not_trusted,
        test_the_manual_covers_keys,
        test_import_and_delete,
        test_the_host_key_store,
        test_a_changed_key_reaches_the_user,
        test_the_api_asks_rather_than_deciding,
    ):
        try:
            test()
        except Exception as exc:
            failed.append(f"{test.__name__}: raised {type(exc).__name__}: {exc}")
            print(f"  FAIL {test.__name__} raised {type(exc).__name__}: {exc}")

    shutil.rmtree(_TEMP, ignore_errors=True)

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
