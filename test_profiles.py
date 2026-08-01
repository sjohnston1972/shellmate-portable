"""
test_profiles.py — Saved connections, and not saving the same one twice.

The duplicate check used to live in the browser, in `autoSaveProfile()`, which
meant the automatic save after a successful connection was careful and the
**Save profile** button was not. Real data confirmed it: two identical entries
for `ssh 192.168.20.17:22 as steven`, both shown as tiles on the welcome
screen.

So the rule moved to `save_profile()`, next to the write, for the same reason
`SECRET_FIELDS` lives there — a rule enforced where the data is written cannot
be got wrong by a caller that has not heard of it.

What these tests care about most is the *merge*, because that is the part that
can lose something. Credentials are keyed by profile id, so discarding the
wrong one of two duplicates loses a saved password and strands the real one in
the vault where nothing can reach it.

    python test_profiles.py
"""

import json
import shutil
import sys
import tempfile
from pathlib import Path

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


# A temporary data directory, so a test run cannot touch anybody's real
# profiles.json — this module deletes and rewrites that file.
_temp = Path(tempfile.mkdtemp(prefix="shellmate-profiles-"))

from backend import paths  # noqa: E402

paths.data_dir = lambda: _temp                                     # type: ignore
paths.profiles_file = lambda: _temp / "profiles.json"              # type: ignore

from backend import profiles  # noqa: E402


def reset(entries: list[dict] | None = None) -> None:
    """Start from a known profiles.json."""
    profiles._save(entries or [])
    plaintext = _temp / "credentials-plaintext.json"
    if plaintext.exists():
        plaintext.unlink()


def test_identity() -> None:
    print("\n-- What counts as the same connection --")

    same = profiles.identity({"hostname": "switch01", "port": 22, "username": "steven"})

    check("case does not make it a different device",
          profiles.identity({"hostname": "SWITCH01", "port": 22,
                             "username": "Steven"}) == same)
    check("nor does a pasted trailing space",
          profiles.identity({"hostname": " switch01 ", "port": 22,
                             "username": "steven "}) == same)
    check("an omitted port means the default for the transport",
          profiles.identity({"hostname": "switch01", "username": "steven"}) == same)
    check("and telnet's default is 23, not 22",
          profiles.identity({"connection_type": "telnet",
                             "hostname": "sw"})[2] == 23)

    check("a different username is a different connection",
          profiles.identity({"hostname": "switch01", "port": 22,
                             "username": "neteng"}) != same,
          "merging them would silently discard one of the two passwords")
    check("a different port is a different connection",
          profiles.identity({"hostname": "switch01", "port": 2222,
                             "username": "steven"}) != same)

    # The display name is rewritten by record_detected_hostname() the moment
    # the device says what it is called. Identity cannot depend on a field
    # that changes by itself.
    check("the display name is not part of it",
          profiles.identity({"hostname": "10.20.30.40", "port": 22,
                             "username": "steven", "name": "10.20.30.40"})
          == profiles.identity({"hostname": "10.20.30.40", "port": 22,
                                "username": "steven", "name": "core-sw-01"}),
          "a profile renamed after connecting would stop matching itself")

    check("serial is identified by its port",
          profiles.identity({"connection_type": "serial", "serial_port": "com3",
                             "baudrate": 9600})
          == profiles.identity({"connection_type": "serial", "serial_port": "COM3",
                                "baudrate": 9600}))
    check("and the baud rate is part of it",
          profiles.identity({"connection_type": "serial", "serial_port": "COM3",
                             "baudrate": 9600})
          != profiles.identity({"connection_type": "serial", "serial_port": "COM3",
                                "baudrate": 115200}))


def test_save_refuses_to_duplicate() -> None:
    print("\n-- Saving the same thing twice --")
    reset()

    first = profiles.save_profile({
        "hostname": "192.168.20.17", "port": 22, "username": "steven",
        "connection_type": "ssh", "name": "192.168.20.17",
    })
    check("the first save creates a profile", bool(first.get("id")))
    check("and does not claim it already existed",
          not first.get("already_saved"))

    second = profiles.save_profile({
        "hostname": "192.168.20.17", "port": 22, "username": "steven",
        "connection_type": "ssh", "name": "192.168.20.17",
    })
    check("the second save returns the same profile",
          second.get("id") == first.get("id"),
          f"{second.get('id')} != {first.get('id')}")
    check("and says so, so the button does not look broken",
          second.get("already_saved") is True)
    check("only one profile is on disk", len(profiles._load()) == 1,
          f"{len(profiles._load())} profiles")

    # The exact case the frontend comparison missed.
    profiles.save_profile({
        "hostname": "  192.168.20.17 ", "port": None, "username": "STEVEN",
        "connection_type": "ssh",
    })
    check("a differently-typed copy of the same device is still one profile",
          len(profiles._load()) == 1,
          f"{len(profiles._load())} profiles")

    # And one that genuinely is a different connection.
    profiles.save_profile({
        "hostname": "192.168.20.17", "port": 22, "username": "neteng",
        "connection_type": "ssh",
    })
    check("a different username still gets its own profile",
          len(profiles._load()) == 2)


def test_saving_again_is_an_edit() -> None:
    print("\n-- Pressing Save on something already saved --")
    reset()

    profiles.save_profile({
        "hostname": "10.0.0.1", "port": 22, "username": "admin",
        "connection_type": "ssh", "name": "10.0.0.1",
    })
    profiles.save_profile({
        "hostname": "10.0.0.1", "port": 22, "username": "admin",
        "connection_type": "ssh", "name": "Glasgow core, top of rack",
        "jump_host": "bastion.example.net",
    })

    stored = profiles._load()
    check("still one profile", len(stored) == 1)
    check("the new name is taken", stored[0].get("name") == "Glasgow core, top of rack",
          f"got {stored[0].get('name')!r} — an edit that does nothing looks broken")
    check("and so is a field that was not there before",
          stored[0].get("jump_host") == "bastion.example.net")

    # An empty value is an absence, not an instruction to clear.
    profiles.save_profile({
        "hostname": "10.0.0.1", "port": 22, "username": "admin",
        "connection_type": "ssh", "name": "",
    })
    check("an empty field does not wipe what is there",
          profiles._load()[0].get("name") == "Glasgow core, top of rack")


def test_existing_duplicates_are_merged() -> None:
    print("\n-- Duplicates already in the file --")

    # Two entries for one device, as found in real data. The *second* is the
    # one with credentials, so a merge that simply keeps the first loses them.
    reset([
        {"id": "aaa", "hostname": "192.168.20.17", "port": 22,
         "username": "steven", "connection_type": "ssh", "name": "192.168.20.17"},
        {"id": "bbb", "hostname": "192.168.20.17", "port": 22,
         "username": "steven", "connection_type": "ssh", "name": "core-sw-01",
         "detected_hostname": "core-sw-01"},
        {"id": "ccc", "hostname": "10.0.0.9", "port": 22,
         "username": "admin", "connection_type": "ssh"},
    ])
    profiles.save_plaintext_credentials("bbb", {"password": "hunter2"})

    removed = profiles.dedupe_existing()
    stored = profiles._load()

    check("one duplicate is removed", removed == 1, f"removed {removed}")
    check("two profiles remain", len(stored) == 2, f"{len(stored)} remain")

    survivor = next((p for p in stored if p["hostname"] == "192.168.20.17"), None)
    check("the entry holding the credentials is the one kept",
          survivor is not None and survivor["id"] == "bbb",
          "keeping the other one loses the saved password and strands it "
          "in the store where no interface can reach it")
    check("and its credentials are still there",
          profiles.has_credentials("bbb"))
    check("the unrelated profile is untouched",
          any(p["id"] == "ccc" for p in stored))

    check("running it again changes nothing",
          profiles.dedupe_existing() == 0)


def test_merge_keeps_what_the_discarded_entry_knew() -> None:
    print("\n-- What a merge must not throw away --")

    # Here the credentials are on the *plainer* entry, so the survivor is the
    # one without the detected hostname — which it has to inherit.
    reset([
        {"id": "keep", "hostname": "10.1.1.1", "port": 22, "username": "u",
         "connection_type": "ssh", "name": "10.1.1.1"},
        {"id": "drop", "hostname": "10.1.1.1", "port": 22, "username": "u",
         "connection_type": "ssh", "name": "10.1.1.1",
         "detected_hostname": "dist-sw-02", "jump_host": "bastion"},
    ])
    profiles.save_plaintext_credentials("keep", {"password": "x"})

    profiles.dedupe_existing()
    stored = profiles._load()
    check("one profile remains", len(stored) == 1)
    check("the survivor is the one with credentials", stored[0]["id"] == "keep")
    check("it inherits the detected hostname",
          stored[0].get("detected_hostname") == "dist-sw-02")
    check("and the jump host", stored[0].get("jump_host") == "bastion")


def test_orphaned_credentials_are_not_left_behind() -> None:
    print("\n-- Credentials belonging to a discarded entry --")

    reset([
        {"id": "one", "hostname": "10.2.2.2", "port": 22, "username": "u",
         "connection_type": "ssh"},
        {"id": "two", "hostname": "10.2.2.2", "port": 22, "username": "u",
         "connection_type": "ssh"},
    ])
    profiles.save_plaintext_credentials("one", {"password": "first"})
    profiles.save_plaintext_credentials("two", {"password": "second"})

    profiles.dedupe_existing()
    stored = profiles._load()
    check("one profile remains", len(stored) == 1)

    gone = "two" if stored[0]["id"] == "one" else "one"
    check("the discarded entry's credentials are forgotten",
          not profiles.has_credentials(gone),
          "otherwise they sit in the store forever with nothing pointing at them")
    check("and the survivor's are intact",
          profiles.has_credentials(stored[0]["id"]))


def test_listing_tidies_up() -> None:
    print("\n-- Reading the list --")
    reset([
        {"id": "p1", "hostname": "10.3.3.3", "port": 22, "username": "u",
         "connection_type": "ssh"},
        {"id": "p2", "hostname": "10.3.3.3", "port": 22, "username": "u",
         "connection_type": "ssh"},
    ])
    listed = profiles.get_profiles()
    check("the welcome screen sees one tile, not two", len(listed) == 1)
    check("each carries the credential flag",
          all("has_saved_credentials" in p for p in listed))
    check("and where they are kept",
          all("credential_storage" in p for p in listed))


def test_a_secret_still_cannot_reach_the_file() -> None:
    """The dedupe path is a second way into save_profile. It must not be a way round."""
    print("\n-- Secrets, on both paths through the save --")
    reset()

    profiles.save_profile({"hostname": "10.4.4.4", "port": 22, "username": "u",
                           "connection_type": "ssh", "password": "first-write"})
    profiles.save_profile({"hostname": "10.4.4.4", "port": 22, "username": "u",
                           "connection_type": "ssh", "password": "second-write",
                           "private_key_passphrase": "also-secret"})

    raw = (_temp / "profiles.json").read_text(encoding="utf-8")
    check("nothing from the first save is in the file", "first-write" not in raw)
    check("nothing from the merge is either", "second-write" not in raw,
          "the already-saved branch absorbs fields — it must absorb the "
          "cleaned ones, not the raw request")
    check("nor a passphrase", "also-secret" not in raw)

    stored = json.loads(raw)
    check("and no secret field exists at all",
          not (set(stored[0]) & profiles.SECRET_FIELDS),
          str(set(stored[0]) & profiles.SECRET_FIELDS))


def main() -> int:
    print("\n" + "=" * 52)
    print("  Connection profiles")
    print("=" * 52)

    for test in (test_identity, test_save_refuses_to_duplicate,
                 test_saving_again_is_an_edit,
                 test_existing_duplicates_are_merged,
                 test_merge_keeps_what_the_discarded_entry_knew,
                 test_orphaned_credentials_are_not_left_behind,
                 test_listing_tidies_up,
                 test_a_secret_still_cannot_reach_the_file):
        try:
            test()
        except Exception as exc:
            failed.append(f"{test.__name__}: raised {type(exc).__name__}: {exc}")
            print(f"  FAIL {test.__name__} raised {type(exc).__name__}: {exc}")

    shutil.rmtree(_temp, ignore_errors=True)

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
