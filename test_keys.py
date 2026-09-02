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
