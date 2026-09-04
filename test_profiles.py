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

import csv
import io
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
    """
    Start from a known profiles.json, with no credentials attached.

    Both stores are cleared, not just the file. Vault entries are keyed by
    profile id and survive profiles.json being rewritten, so reusing an id
    between tests would otherwise carry a credential across — which is exactly
    the orphaning `delete_profile()` exists to prevent, showing up here as a
    test that passes or fails depending on what ran before it.
    """
    for existing in profiles._load() + (entries or []):
        profiles.forget_credentials(existing.get("id", ""))

    # Named credentials outlive profiles.json too, and their values are keyed
    # under the set id rather than a profile — so one left behind by an
    # earlier test is a credential the next test can neither see nor account
    # for.
    for entry in profiles.credential_sets():
        profiles.delete_credential_set(entry["id"])

    profiles._save(entries or [])
    plaintext = _temp / "credentials-plaintext.json"
    if plaintext.exists():
        plaintext.unlink()


#: A value that must never reach an export or profiles.json.
CSV_SECRET_VALUE = "TOP-SECRET-DEVICE-PASSWORD"


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


def test_listing_does_not_mutate() -> None:
    """
    Reading the list must never change it.

    This asserted the opposite — that get_profiles() collapsed two entries
    into one — and that behaviour was a data-loss bug at any size above a lab.
    Five thousand connections to one address became one the first time
    anything listed them, because identity() is host, port, username and
    transport, and a terminal server fronting fifty devices is one address.

    Merging still happens where it belongs: on save, and when asked.
    """
    print(chr(10) + "-- Reading the list --")
    reset([
        {"id": "p1", "name": "first", "hostname": "10.3.3.3", "port": 22,
         "username": "u", "connection_type": "ssh"},
        {"id": "p2", "name": "second", "hostname": "10.3.3.3", "port": 22,
         "username": "u", "connection_type": "ssh"},
    ])
    listed = profiles.get_profiles()
    check("both connections survive being listed", len(listed) == 2,
          f"{len(listed)} came back - reading deleted one")

    on_disk = json.loads(paths.profiles_file().read_text(encoding="utf-8"))
    check("and the file is untouched", len(on_disk) == 2,
          f"{len(on_disk)} left on disk")

    check("each carries the credential flag",
          all("has_saved_credentials" in p for p in listed))
    check("and where they are kept",
          all("credential_storage" in p for p in listed))

    # The explicit action still merges, which is the half worth keeping.
    removed = profiles.dedupe_existing()
    check("dedupe_existing() still merges when asked", removed == 1,
          f"removed {removed}")
    check("leaving one", len(profiles.get_profiles()) == 1)


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


def test_what_is_saved_can_be_listed() -> None:
    """
    Which credentials exist, and where each one lives.

    The grain is one *field*, not one profile. CREDENTIAL_FIELDS is four
    things — a password, a key passphrase, and the same two for a jump host —
    and a listing that showed only "password" would be lying about what is
    stored.
    """
    print("\n-- What is saved --")
    reset([
        {"id": "p1", "hostname": "10.5.5.5", "port": 22, "username": "u",
         "connection_type": "ssh", "name": "core"},
    ])
    profiles.save_plaintext_credentials(
        "p1", {"password": "shown", "jump_password": "also-shown"})

    found = profiles.credential_fields("p1")
    check("both saved fields are listed", set(found) == {"password", "jump_password"},
          str(found))
    check("and each says where it lives",
          set(found.values()) == {"plaintext"}, str(found))
    check("a field with nothing saved is absent",
          "private_key_passphrase" not in found)
    check("every field has a name for the screen",
          all(f in profiles.FIELD_LABELS for f in profiles.CREDENTIAL_FIELDS),
          "a row would be labelled with its internal field name")


def test_a_plaintext_credential_can_be_read_back() -> None:
    """
    The narrow, deliberate exception to "no endpoint returns a secret".

    The value is already sitting in a JSON file the user can open, so refusing
    to show it in the interface protects nothing and only sends them to a text
    editor. It reads the plaintext file and nothing else.
    """
    print("\n-- Showing one that was never encrypted --")
    reset([{"id": "p1", "hostname": "10.5.5.5", "port": 22, "username": "u",
            "connection_type": "ssh"}])
    profiles.save_plaintext_credentials("p1", {"password": "readable"})

    check("it comes back", profiles.read_plaintext_credential("p1", "password")
          == "readable")
    check("a field with nothing saved returns empty, not an error",
          profiles.read_plaintext_credential("p1", "jump_password") == "")

    raised = False
    try:
        profiles.read_plaintext_credential("p1", "not_a_field")
    except ValueError:
        raised = True
    check("an unknown field is refused", raised,
          "otherwise a typo silently reads nothing and looks like an empty store")


def test_changing_one_does_not_leave_the_old_copy() -> None:
    """
    Saving into one store clears the other.

    This is the part that matters. A credential moved from plaintext into the
    vault, with the plaintext copy left behind, is a password the user
    believes is encrypted sitting readable on disk — worse than never having
    offered the move.
    """
    print("\n-- Changing where one lives --")
    reset([{"id": "p1", "hostname": "10.6.6.6", "port": 22, "username": "u",
            "connection_type": "ssh"}])
    profiles.save_plaintext_credentials("p1", {"password": "in-the-open"})

    where = profiles.set_credential("p1", "password", "now-encrypted", "vault")
    if where != "vault":
        check("the vault accepted it", False, "vault unavailable in this environment")
        return

    check("it is recorded as encrypted",
          profiles.credential_fields("p1").get("password") == "vault")
    raw = (_temp / "credentials-plaintext.json")
    body = raw.read_text(encoding="utf-8") if raw.exists() else ""
    check("and the readable copy is gone", "in-the-open" not in body, body[:200])
    check("as is the new value", "now-encrypted" not in body, body[:200])

    # And back the other way, which the panel does not offer but the store
    # must still handle correctly if anything ever asks.
    profiles.set_credential("p1", "password", "back-in-the-open", "plaintext")
    check("moving the other way clears the encrypted copy",
          profiles.credential_fields("p1").get("password") == "plaintext",
          str(profiles.credential_fields("p1")))


def test_encrypting_never_loses_the_password() -> None:
    """
    Move to the vault, and the ordering that makes it safe.

    The plaintext copy is deleted only once the encrypted one is written. The
    other order loses the password outright whenever the vault refuses.
    """
    print("\n-- Encrypting what was in the open --")
    reset([{"id": "p1", "hostname": "10.7.7.7", "port": 22, "username": "u",
            "connection_type": "ssh"}])
    profiles.save_plaintext_credentials(
        "p1", {"password": "one", "private_key_passphrase": "two"})

    moved = profiles.move_to_vault("p1")
    if not moved:
        check("the vault accepted them", False, "vault unavailable in this environment")
        return

    check("every field moves, not just the password",
          set(moved) == {"password", "private_key_passphrase"}, str(moved))
    check("they are now encrypted",
          set(profiles.credential_fields("p1").values()) == {"vault"},
          str(profiles.credential_fields("p1")))

    raw = _temp / "credentials-plaintext.json"
    body = raw.read_text(encoding="utf-8") if raw.exists() else ""
    check("and nothing readable is left behind",
          "one" not in body and "two" not in body, body[:200])

    check("a profile with nothing in the open is a no-op",
          profiles.move_to_vault("p1") == [])


def test_forgetting() -> None:
    print("\n-- Forgetting --")
    reset([{"id": "p1", "hostname": "10.8.8.8", "port": 22, "username": "u",
            "connection_type": "ssh"},
           {"id": "p2", "hostname": "10.8.8.9", "port": 22, "username": "u",
            "connection_type": "ssh"}])
    profiles.save_plaintext_credentials("p1", {"password": "a", "jump_password": "b"})
    profiles.save_plaintext_credentials("p2", {"password": "c"})

    check("one field goes on its own",
          profiles.forget_credential("p1", "password") is True)
    check("and the others are untouched",
          profiles.credential_fields("p1") == {"jump_password": "plaintext"},
          str(profiles.credential_fields("p1")))
    check("forgetting nothing is not an error",
          profiles.forget_credential("p1", "password") is False)

    check("and the lot can go at once", profiles.forget_all_plaintext() == 2)
    check("leaving none", not profiles.credential_fields("p1")
          and not profiles.credential_fields("p2"))


def test_the_api_still_keeps_its_promise() -> None:
    """
    The listing describes; exactly one endpoint discloses.

    Adding a way to read a stored secret is the sort of change that quietly
    grows a second one. This checks the boundary rather than trusting it.
    """
    print("\n-- What the API gives out --")
    from fastapi.testclient import TestClient

    from backend.app import app

    reset([{"id": "plain1", "hostname": "10.9.9.9", "port": 22, "username": "u",
            "connection_type": "ssh", "name": "lab-sw"}])
    profiles.save_plaintext_credentials("plain1", {"password": "TOP-SECRET-VALUE"})

    client = TestClient(app, base_url="http://127.0.0.1")

    listing = client.get("/api/credentials")
    check("the listing answers", listing.status_code == 200, listing.text)
    check("and carries no secret at all", "TOP-SECRET-VALUE" not in listing.text,
          "a credential reached a listing endpoint")

    rows = listing.json()["entries"]
    row = next((r for r in rows if r["profile_id"] == "plain1"), None)
    check("the row is there", row is not None)
    check("it says which store", row and row["storage"] == "plaintext")
    check("and that this one can be shown", row and row["can_reveal"] is True)

    shown = client.post("/api/credentials/plain1/password/reveal")
    check("revealing a plaintext credential works",
          shown.status_code == 200 and shown.json()["value"] == "TOP-SECRET-VALUE",
          shown.text)

    # And the half that must never work.
    if profiles.set_credential("plain1", "password", "ENCRYPTED-VALUE", "vault") == "vault":
        refused = client.post("/api/credentials/plain1/password/reveal")
        check("revealing an encrypted one is refused", refused.status_code == 400,
              refused.text)
        check("and the value is not in the refusal either",
              "ENCRYPTED-VALUE" not in refused.text, refused.text)

        again = client.get("/api/credentials")
        check("the listing marks it as not showable",
              not next(r["can_reveal"] for r in again.json()["entries"]
                       if r["profile_id"] == "plain1"))

    missing = client.post("/api/credentials/plain1/not_a_field/reveal")
    check("an unknown field is a 404, not a blank success",
          missing.status_code == 404, missing.text)


def test_a_credential_can_belong_to_more_than_one_connection() -> None:
    """
    Forty switches off one scan share one login.

    A credential used to be keyed to a single profile, so "use the login I
    already have" could not be expressed at all. Copying it to each device
    would work right up until the password changed, at which point there are
    forty entries to update and nothing recording that they were ever the same
    credential.
    """
    print("\n-- One credential, many connections --")
    reset([
        {"id": "sw1", "hostname": "10.10.0.1", "port": 22, "username": "neteng",
         "connection_type": "ssh"},
        {"id": "sw2", "hostname": "10.10.0.2", "port": 22, "username": "neteng",
         "connection_type": "ssh"},
    ])

    entry = profiles.save_credential_set("Lab admin", "neteng", "labpass", "vault")
    check("a named credential is created", bool(entry.get("id")))
    check("and it knows where it is stored", entry.get("storage") == "vault",
          str(entry.get("storage")))

    profiles.attach_credential_set("sw1", entry["id"])
    profiles.attach_credential_set("sw2", entry["id"])

    for profile_id in ("sw1", "sw2"):
        resolved = profiles.load_credentials(profile_id)
        check(f"{profile_id} resolves the shared password",
              resolved.get("password") == "labpass", str(resolved))
        check(f"{profile_id} reports itself as having credentials",
              profiles.has_credentials(profile_id))
        check(f"{profile_id} reports where they are kept",
              profiles.credential_storage(profile_id) == "vault",
              profiles.credential_storage(profile_id))

    # The point of a reference rather than a copy.
    profiles.save_credential_set("Lab admin", "neteng", "changed-once",
                                 "vault", entry["id"])
    check("changing it once changes it for both",
          profiles.load_credentials("sw1").get("password") == "changed-once"
          and profiles.load_credentials("sw2").get("password") == "changed-once")

    listed = profiles.credential_sets()
    check("the listing counts what uses it",
          listed and listed[0]["in_use"] == 2, str(listed))
    check("and carries no value", "changed-once" not in json.dumps(listed),
          "a credential reached a listing")


def test_a_devices_own_password_wins() -> None:
    """
    The one switch in the lab whose password was changed.

    Somebody who sets a password on a specific device meant it for that
    device. A shared credential is a fallback, not an override.
    """
    print("\n-- When a device has its own --")
    reset([
        {"id": "odd", "hostname": "10.10.0.9", "port": 22, "username": "neteng",
         "connection_type": "ssh"},
    ])
    entry = profiles.save_credential_set("Lab admin", "neteng", "shared", "vault")
    profiles.attach_credential_set("odd", entry["id"])
    check("it uses the shared one to begin with",
          profiles.load_credentials("odd").get("password") == "shared")

    profiles.set_credential("odd", "password", "just-this-one", "vault")
    check("its own password takes over",
          profiles.load_credentials("odd").get("password") == "just-this-one",
          str(profiles.load_credentials("odd")))

    profiles.forget_credential("odd", "password")
    check("and removing it falls back to the shared one again",
          profiles.load_credentials("odd").get("password") == "shared",
          "a profile that forgets its own credential should not be left with "
          "nothing when it references a shared one")


def test_deleting_a_shared_credential_detaches_what_used_it() -> None:
    """
    A reference to something that no longer exists is worse than no reference.

    Left behind, those connections keep reporting themselves ready to connect
    with nothing to connect with — and the failure arrives at the device
    rather than in the interface.
    """
    print("\n-- Deleting one that is in use --")
    reset([
        {"id": "a", "hostname": "10.11.0.1", "port": 22, "username": "u",
         "connection_type": "ssh"},
        {"id": "b", "hostname": "10.11.0.2", "port": 22, "username": "u",
         "connection_type": "ssh"},
    ])
    entry = profiles.save_credential_set("Shared", "u", "pw", "vault")
    profiles.attach_credential_set("a", entry["id"])
    profiles.attach_credential_set("b", entry["id"])

    detached = profiles.delete_credential_set(entry["id"])
    check("it reports how many were relying on it", detached == 2, str(detached))
    check("the set is gone", not profiles.credential_sets())

    for profile_id in ("a", "b"):
        check(f"{profile_id} no longer claims to have credentials",
              not profiles.has_credentials(profile_id))
    check("and no dangling reference is left in the file",
          all("credential_ref" not in p for p in profiles._load()),
          str(profiles._load()))


def test_a_set_needs_a_name_and_holds_no_secret() -> None:
    print("\n-- What the set file may contain --")
    reset()

    raised = ""
    try:
        profiles.save_credential_set("   ", "u", "pw")
    except ValueError as exc:
        raised = str(exc)
    check("an unnamed set is refused", bool(raised), "the name is how it is picked")

    profiles.save_credential_set("Named", "neteng", "SECRET-SET-VALUE", "vault")
    body = (_temp / "credential-sets.json").read_text(encoding="utf-8")
    check("the set file holds the name", "Named" in body)
    check("and the username", "neteng" in body)
    check("and no password at all", "SECRET-SET-VALUE" not in body, body)


def test_the_two_ssh_forms_are_one_device() -> None:
    """
    Splitting the dialog must not split the data.

    "SSH — password" and "SSH — key or jump host" are two forms over one
    transport. If the key form stored a different `connection_type`, the same
    switch saved both ways would become two profiles, the dedupe would stop
    seeing them, `ready_to_connect` would branch on a value it does not know,
    and every existing profiles.json would still say `ssh`.

    So `auth_method` carries which form was used and identity ignores it.
    """
    print("\n-- Two forms, one transport --")
    reset()

    by_password = {"hostname": "10.30.0.1", "port": 22, "username": "neteng",
                   "connection_type": "ssh", "auth_method": "password"}
    by_key = {"hostname": "10.30.0.1", "port": 22, "username": "neteng",
              "connection_type": "ssh", "auth_method": "key",
              "private_key_path": "C:/keys/lab_ed25519"}

    check("the same device by either method is one identity",
          profiles.identity(by_password) == profiles.identity(by_key),
          "a switch reached by key and by password is one switch")

    first = profiles.save_profile(by_password)
    second = profiles.save_profile(by_key)
    check("so saving both does not create two profiles",
          second["id"] == first["id"] and len(profiles._load()) == 1,
          f"{len(profiles._load())} profiles")
    check("and the second save records the method",
          profiles._load()[0].get("auth_method") == "key",
          str(profiles._load()[0].get("auth_method")))
    check("along with the key it needs",
          profiles._load()[0].get("private_key_path") == "C:/keys/lab_ed25519")

    # The transport is what the session manager dispatches on, and it has to
    # stay something HANDLERS knows about.
    from backend.connections.manager import HANDLERS

    check("the stored transport is one the manager can dispatch",
          profiles._load()[0].get("connection_type") in HANDLERS,
          str(profiles._load()[0].get("connection_type")))
    check("and 'ssh-key' is not a transport",
          "ssh-key" not in HANDLERS,
          "the picker value must never reach the handler registry")

    # No secret reaches the file by the new route either.
    profiles.save_profile({**by_key, "private_key_passphrase": "SECRET-PHRASE"})
    raw = (_temp / "profiles.json").read_text(encoding="utf-8")
    check("a key passphrase still cannot reach profiles.json",
          "SECRET-PHRASE" not in raw, raw[:200])


def test_tags() -> None:
    """
    Grouping saved connections.

    Tags rather than folders because the useful groupings overlap — a device
    is both "glasgow" and "production" and "access", and any tree forces a
    choice between them.
    """
    print(chr(10) + "-- Tags --")
    reset()

    check("whitespace and case are normalised",
          profiles.normalise_tags(" Glasgow , PRODUCTION ") == ["glasgow", "production"])
    check("duplicates collapse",
          profiles.normalise_tags(["lab", "Lab", " lab "]) == ["lab"],
          "'Production' and 'production' being two groups is a distinction "
          "nobody means to make")
    check("a comma-separated string works too",
          profiles.normalise_tags("a,b,,c") == ["a", "b", "c"])
    check("and empties produce nothing", profiles.normalise_tags(["", "  "]) == [])
    check("order is kept", profiles.normalise_tags("z,a,m") == ["z", "a", "m"],
          "sorting them would reorder what somebody typed for no reason")

    saved = profiles.save_profile({
        "hostname": "10.40.0.1", "port": 22, "username": "u",
        "connection_type": "ssh", "name": "sw1", "tags": ["Lab", "lab", "Access"],
    })
    check("tags are normalised on the way in",
          saved.get("tags") == ["lab", "access"], str(saved.get("tags")))

    profiles.save_profile({
        "hostname": "10.40.0.2", "port": 22, "username": "u",
        "connection_type": "ssh", "name": "sw2", "tags": ["lab"],
    })

    counts = {entry["tag"]: entry["count"] for entry in profiles.all_tags()}
    check("the tag list counts what carries each", counts == {"lab": 2, "access": 1},
          str(counts))

    tagged = profiles.profiles_tagged("lab")
    check("a tag selects its devices", len(tagged) == 2, str(len(tagged)))
    check("and matching is case-insensitive",
          len(profiles.profiles_tagged("LAB")) == 2)
    check("an unknown tag selects nothing", profiles.profiles_tagged("nope") == [])
    check("as does an empty one", profiles.profiles_tagged("") == [])

    profiles.set_tags(saved["id"], ["edge"])
    check("tags can be replaced",
          profiles.profiles_tagged("edge")[0]["id"] == saved["id"])
    check("and the old one is gone",
          len(profiles.profiles_tagged("lab")) == 1)

    profiles.set_tags(saved["id"], [])
    check("clearing removes the field rather than storing an empty list",
          "tags" not in profiles._load()[0] or not profiles._load()[0].get("tags"),
          str(profiles._load()[0].get("tags")))


# ---------------------------------------------------------------------------
# The estate as a spreadsheet (#535)
#
# Two of these are security properties rather than conveniences, and both fail
# silently if they regress: a password column quietly stripped leaves somebody
# believing their passwords are in ShellMate, and an un-neutralised cell is a
# formula that runs the moment the export is opened.
# ---------------------------------------------------------------------------


def test_a_password_column_is_refused_not_stripped() -> None:
    print("\n-- A CSV carrying passwords --")
    reset()

    text = ("name,hostname,username,password\n"
            "core-sw-01,10.1.1.1,admin,hunter2\n")
    refused = ""
    try:
        profiles.import_csv(text)
    except ValueError as exc:
        refused = str(exc)

    check("a password column stops the whole import",
          "password" in refused.lower(), refused or "it was accepted")
    check("and nothing was written",
          profiles._load() == [], str(profiles._load()))

    # The point of refusing rather than stripping: the caller is told, so the
    # interface can say why. A silent strip reports success.
    check("the message says what to do instead",
          "shared credential" in refused, refused)

    for header in ("Enable Password", "SSH passphrase", "api secret"):
        raised = False
        try:
            profiles.import_csv(f"name,hostname,{header}\na,10.1.1.1,x\n")
        except ValueError:
            raised = True
        check(f"'{header}' is refused too", raised)


def test_a_csv_becomes_connections() -> None:
    print("\n-- Reading an estate in --")
    reset()

    text = (
        "Name,IP Address,Port,Type,User,Groups,Platform\n"
        "core-sw-01,10.1.1.1,22,ssh,admin,site-004/core,cisco_ios\n"
        "acc-sw-01,10.1.1.2,,ssh,admin,\"site-004/access,production\",\n"
        "old-term,10.1.1.9,23,telnet,,site-004,\n"
    )

    preview = profiles.import_csv(text)
    check("the header row is recognised by its aliases",
          preview["columns"][:4] == ["name", "hostname", "port", "type"],
          str(preview["columns"]))
    check("three new, none already saved",
          (preview["new"], preview["already_saved"]) == (3, 0), str(preview))
    check("a preview writes nothing", profiles._load() == [])

    applied = profiles.import_csv(text, apply=True)
    check("applying saves them", applied["new"] == 3, str(applied))

    saved = {p["name"]: p for p in profiles._load()}
    check("the port defaults per transport",
          saved["acc-sw-01"]["port"] == 22 and saved["old-term"]["port"] == 23,
          str([saved["acc-sw-01"]["port"], saved["old-term"]["port"]]))
    check("groups arrive as tags, nesting intact",
          saved["acc-sw-01"]["tags"] == ["site-004/access", "production"],
          str(saved["acc-sw-01"].get("tags")))
    check("the platform is carried across",
          saved["core-sw-01"]["platform"] == "cisco_ios")

    again = profiles.import_csv(text, apply=True)
    check("importing the same file twice adds nothing",
          (again["new"], again["already_saved"]) == (0, 3), str(again))
    check("and there are still three connections",
          len(profiles._load()) == 3, str(len(profiles._load())))


def test_a_bad_row_is_named_and_the_rest_go_in() -> None:
    print("\n-- One bad row --")
    reset()

    text = (
        "name,hostname,port,type\n"
        "good,10.1.1.1,22,ssh\n"
        "no-address,,22,ssh\n"
        "bad-port,10.1.1.3,notaport,ssh\n"
        "console,COM3,,serial\n"
        "wrong-type,10.1.1.4,22,carrier-pigeon\n"
    )
    result = profiles.import_csv(text, apply=True)
    check("the readable row went in", result["new"] == 1, str(result))
    check("four rows were rejected", len(result["rejected"]) == 4,
          str(result["rejected"]))

    reasons = {row["line"]: row["why"] for row in result["rejected"]}
    check("each rejection names its line number",
          sorted(reasons) == [3, 4, 5, 6], str(sorted(reasons)))
    check("and says why, not just that",
          "hostname" in reasons[3] and "port number" in reasons[4]
          and "COM port" in reasons[5] and "carrier-pigeon" in reasons[6],
          str(reasons))


def test_an_unknown_credential_rejects_the_row() -> None:
    """Never silently unattached: the row would import and never connect."""
    print("\n-- Credential by name --")
    reset()
    entry = profiles.save_credential_set("Lab login", "admin", "", "plaintext")

    result = profiles.import_csv(
        "name,hostname,type,credential\n"
        "sw1,10.2.1.1,ssh,Lab login\n"
        "sw2,10.2.1.2,ssh,Nothing called this\n",
        apply=True)

    check("the named credential is attached by name",
          profiles._load()[0].get("credential_ref") == entry["id"],
          str(profiles._load()))
    check("an unknown name rejects the row rather than dropping the link",
          len(result["rejected"]) == 1
          and "shared credential" in result["rejected"][0]["why"],
          str(result["rejected"]))


def test_a_bulk_import_is_one_write() -> None:
    """`retag_many`'s rule: one load and one save however many arrive."""
    print("\n-- One load, one save --")
    reset()

    writes = []
    original = profiles._save
    profiles._save = lambda entries: (writes.append(len(entries)), original(entries))[1]
    try:
        rows = [{"name": f"sw{n}", "hostname": f"10.3.0.{n}", "port": 22,
                 "connection_type": "ssh", "username": "admin"}
                for n in range(1, 51)]
        profiles.save_many(rows)
    finally:
        profiles._save = original

    check("fifty connections cost one write", writes == [50], str(writes))
    check("and all fifty are there", len(profiles._load()) == 50)


def test_an_import_adds_groups_rather_than_replacing_them() -> None:
    print("\n-- Re-importing does not empty a group --")
    reset()
    profiles.save_many([{"name": "sw1", "hostname": "10.4.0.1", "port": 22,
                         "connection_type": "ssh", "username": "admin",
                         "tags": ["glasgow"]}])

    profiles.import_csv("name,hostname,type,username,groups\n"
                        "sw1,10.4.0.1,ssh,admin,production\n", apply=True)
    check("the group it already had survives the import",
          profiles._load()[0]["tags"] == ["glasgow", "production"],
          str(profiles._load()[0].get("tags")))


def test_the_export_cannot_be_a_formula() -> None:
    """The #513 lesson: a device name is not ours to trust."""
    print("\n-- Formula injection --")
    reset()
    profiles.save_many([
        {"name": "=cmd|'/c calc'!A1", "hostname": "10.5.0.1", "port": 22,
         "connection_type": "ssh", "username": "admin"},
        {"name": "+1234", "hostname": "10.5.0.2", "port": 22,
         "connection_type": "ssh", "username": "-admin"},
        {"name": "@SUM(A1)", "hostname": "10.5.0.3", "port": 22,
         "connection_type": "ssh", "username": "admin"},
    ])

    text = profiles.export_csv()
    rows = list(csv.reader(io.StringIO(text)))
    body = rows[1:]

    dangerous = [cell for row in body for cell in row
                 if cell[:1] in ("=", "+", "-", "@", "\t", "\r")]
    check("no exported cell begins as a formula", not dangerous, str(dangerous))
    check("the value is kept, only prefixed",
          any(cell == "'=cmd|'/c calc'!A1" for row in body for cell in row),
          str(body))
    check("a username starting with a dash is neutralised too",
          any(cell == "'-admin" for row in body for cell in row), str(body))


def test_the_export_carries_no_secret() -> None:
    print("\n-- Nothing secret leaves in the export --")
    reset()
    entry = profiles.save_credential_set("Lab login", "admin", CSV_SECRET_VALUE, "plaintext")
    saved = profiles.save_many([{"name": "sw1", "hostname": "10.6.0.1",
                                 "port": 22, "connection_type": "ssh",
                                 "username": "admin", "tags": ["glasgow"],
                                 "credential_ref": entry["id"]}])["created"][0]
    profiles.save_plaintext_credentials(saved["id"], {"password": CSV_SECRET_VALUE})

    text = profiles.export_csv()
    header = next(csv.reader(io.StringIO(text)))

    check("no column is a secret",
          not (set(header) & profiles.SECRET_FIELDS), str(header))
    check("and no stored password appears anywhere in the file",
          CSV_SECRET_VALUE not in text)
    check("the credential column is the set's name, which can be re-imported",
          "Lab login" in text, text)


def test_the_export_can_be_one_group() -> None:
    print("\n-- Exporting one group --")
    reset()
    profiles.save_many([
        {"name": "a", "hostname": "10.7.0.1", "port": 22, "connection_type": "ssh",
         "username": "admin", "tags": ["site-004"]},
        {"name": "b", "hostname": "10.7.0.2", "port": 22, "connection_type": "ssh",
         "username": "admin", "tags": ["site-004/access"]},
        {"name": "c", "hostname": "10.7.0.3", "port": 22, "connection_type": "ssh",
         "username": "admin", "tags": ["site-005"]},
    ])

    text = profiles.export_csv("site-004")
    rows = list(csv.reader(io.StringIO(text)))[1:]
    names = sorted(row[0] for row in rows)
    check("the subtree comes with the group, and nothing else does",
          names == ["a", "b"], str(names))
    # `site-004` must not swallow `site-0040`; the separator is what makes the
    # prefix match safe, the same rule profiles_tagged() uses.
    check("a longer group name that merely starts the same is left out",
          "site-005" not in text)


def test_an_export_re_imports_as_itself() -> None:
    print("\n-- Round trip --")
    reset()
    profiles.save_many([{"name": "core-sw-01", "hostname": "10.8.0.1",
                         "port": 2222, "connection_type": "ssh",
                         "username": "neteng", "tags": ["site-004/core"],
                         "platform": "cisco_ios"}])
    text = profiles.export_csv()

    result = profiles.import_csv(text, apply=True)
    check("re-importing an export adds nothing",
          (result["new"], result["already_saved"]) == (0, 1), str(result))

    reset()
    profiles.import_csv(text, apply=True)
    restored = profiles._load()[0]
    check("into an empty ShellMate it comes back whole",
          (restored["name"], restored["hostname"], restored["port"],
           restored["username"], restored["tags"], restored["platform"])
          == ("core-sw-01", "10.8.0.1", 2222, "neteng",
              ["site-004/core"], "cisco_ios"),
          str(restored))


def test_a_file_with_no_header_is_read_in_column_order() -> None:
    print("\n-- No header row --")
    reset()
    result = profiles.import_csv(
        "core-sw-01,10.9.0.1,22,ssh,admin,glasgow,,\n", apply=True)
    check("the documented column order is assumed", result["new"] == 1, str(result))
    check("and the row is read as a device, not a header",
          profiles._load()[0]["hostname"] == "10.9.0.1", str(profiles._load()))

    reset()
    semicolons = profiles.import_csv(
        "name;hostname;port;type;username\nsw1;10.9.0.2;22;ssh;admin\n", apply=True)
    check("a semicolon-separated export is read as one too",
          semicolons["new"] == 1, str(semicolons))


# ---------------------------------------------------------------------------
# Editing many at once (#537)
#
# The interesting half is what it refuses. A bulk edit that merged two
# connections would lose a credential without saying so, and one that
# accepted a password column would have somebody believing fifty devices
# had been given a password they had not.
# ---------------------------------------------------------------------------

def _estate(count: int = 3, **fields) -> list[dict]:
    reset()
    profiles.save_many([{"name": f"sw{n}", "hostname": f"10.9.0.{n}",
                         "port": 22, "connection_type": "ssh",
                         "username": "old-account", **fields}
                        for n in range(1, count + 1)])
    return profiles._load()


def test_a_bulk_edit_is_one_write() -> None:
    print("\n-- Editing fifty at once --")
    saved = _estate(50)

    writes = []
    original = profiles._save
    profiles._save = lambda entries: (writes.append(len(entries)), original(entries))[1]
    try:
        result = profiles.update_many([p["id"] for p in saved],
                                      {"username": "svc-neteng"})
    finally:
        profiles._save = original

    check("fifty connections cost one write", writes == [50], str(writes))
    check("and all fifty changed", len(result["updated"]) == 50,
          str(len(result["updated"])))
    check("nothing was skipped", not result["skipped"], str(result["skipped"]))
    check("the file says so",
          {p.get("username") for p in profiles._load()} == {"svc-neteng"},
          str({p.get("username") for p in profiles._load()}))


def test_a_bulk_edit_leaves_alone_what_it_was_not_given() -> None:
    print("\n-- Leave as they are --")
    saved = _estate(2, platform="ios", jump_host="bastion-1")

    profiles.update_many([p["id"] for p in saved], {"username": "svc-neteng"})
    after = profiles._load()
    check("a field not named is untouched",
          all(p.get("platform") == "ios" for p in after), str(after))
    check("and so is the jump host",
          all(p.get("jump_host") == "bastion-1" for p in after), str(after))

    # An empty value is the only way to say "take this off all of them".
    profiles.update_many([p["id"] for p in saved], {"jump_host": ""})
    check("an empty value clears the field",
          all("jump_host" not in p for p in profiles._load()),
          str(profiles._load()))

    try:
        profiles.update_many([p["id"] for p in saved], {})
        check("an edit that changes nothing is refused", False, "it was accepted")
    except ValueError:
        check("an edit that changes nothing is refused", True)


def test_a_bulk_edit_refuses_secrets_and_what_the_device_said() -> None:
    print("\n-- What a bulk edit will not write --")
    saved = _estate(2)
    ids = [p["id"] for p in saved]

    try:
        profiles.update_many(ids, {"password": CSV_SECRET_VALUE})
        check("a password is refused", False, "it was accepted")
    except ValueError as exc:
        check("a password is refused", True)
        check("and it says to use a shared credential instead",
              "shared credential" in str(exc), str(exc))
    check("nothing of it reached the file",
          CSV_SECRET_VALUE not in (_temp / "profiles.json").read_text(encoding="utf-8"))

    # Inventory facts are what the device said about itself (#536). A user
    # editing fifty connections is not the device.
    for field in ("version", "model", "serial", "last_connected"):
        try:
            profiles.update_many(ids, {field: "made up"})
            check(f"'{field}' is refused", False, "it was accepted")
        except ValueError:
            check(f"'{field}' is refused", True)

    try:
        profiles.update_many(ids, {"hostname": "somewhere-else"})
        check("a field outside the list is refused", False, "it was accepted")
    except ValueError:
        check("a field outside the list is refused", True)

    try:
        profiles.update_many(ids, {"port": "70000"})
        check("a port out of range is refused", False, "it was accepted")
    except ValueError:
        check("a port out of range is refused", True)


def test_a_bulk_edit_reports_a_merge_rather_than_making_one() -> None:
    """
    The risk the issue names: username and port are part of what makes two
    saved connections the same connection, so setting one username across a
    selection can push two of them onto the same identity. dedupe_existing()
    merges those deliberately and loses a credential doing it (#73); here it
    would be an accident.
    """
    print("\n-- Would merge with --")
    reset()
    profiles.save_many([
        {"name": "sw1", "hostname": "10.9.1.1", "port": 22,
         "connection_type": "ssh", "username": "admin"},
        {"name": "sw1-as-neteng", "hostname": "10.9.1.1", "port": 22,
         "connection_type": "ssh", "username": "neteng"},
        {"name": "sw2", "hostname": "10.9.1.2", "port": 22,
         "connection_type": "ssh", "username": "neteng"},
    ])
    saved = profiles._load()
    result = profiles.update_many([p["id"] for p in saved], {"username": "admin"})

    check("nothing was merged", len(profiles._load()) == 3,
          str(len(profiles._load())))
    check("the one that would have collided was skipped",
          [s["name"] for s in result["skipped"]] == ["sw1-as-neteng"],
          str(result["skipped"]))
    check("and it says what it would have merged with",
          result["skipped"][0]["why"] == "would merge with sw1",
          str(result["skipped"][0]))
    check("it keeps the username it had",
          next(p for p in profiles._load()
               if p["name"] == "sw1-as-neteng")["username"] == "neteng")
    check("the ones that could change did",
          {s["name"] for s in result["updated"]} == {"sw1", "sw2"},
          str(result["updated"]))


def test_a_bulk_edit_can_attach_and_detach_a_shared_credential() -> None:
    print("\n-- A credential across a selection --")
    saved = _estate(3)
    ids = [p["id"] for p in saved]
    shared = profiles.save_credential_set("Glasgow", "svc-neteng", "hunter2",
                                          storage="plaintext")

    profiles.update_many(ids, {"credential_ref": shared["id"]})
    listed = profiles.get_profiles()
    check("all three now reference it",
          all(p.get("credential_ref") == shared["id"] for p in listed), str(listed))
    check("and all three report a saved credential",
          all(p["has_saved_credentials"] for p in listed), str(listed))
    check("the credential itself resolves",
          profiles.load_credentials(ids[0]).get("password") == "hunter2")

    profiles.update_many(ids, {"credential_ref": ""})
    check("and one edit detaches them all",
          all("credential_ref" not in p for p in profiles._load()),
          str(profiles._load()))


def main() -> int:
    print("\n" + "=" * 52)
    print("  Connection profiles")
    print("=" * 52)

    for test in (test_identity, test_save_refuses_to_duplicate,
                 test_saving_again_is_an_edit,
                 test_existing_duplicates_are_merged,
                 test_merge_keeps_what_the_discarded_entry_knew,
                 test_orphaned_credentials_are_not_left_behind,
                 test_listing_does_not_mutate,
                 test_a_secret_still_cannot_reach_the_file,
                 test_what_is_saved_can_be_listed,
                 test_a_plaintext_credential_can_be_read_back,
                 test_changing_one_does_not_leave_the_old_copy,
                 test_encrypting_never_loses_the_password,
                 test_forgetting,
                 test_the_api_still_keeps_its_promise,
                 test_a_credential_can_belong_to_more_than_one_connection,
                 test_a_devices_own_password_wins,
                 test_deleting_a_shared_credential_detaches_what_used_it,
                 test_a_set_needs_a_name_and_holds_no_secret,
                 test_the_two_ssh_forms_are_one_device,
                 test_tags,
                 test_a_password_column_is_refused_not_stripped,
                 test_a_csv_becomes_connections,
                 test_a_bad_row_is_named_and_the_rest_go_in,
                 test_an_unknown_credential_rejects_the_row,
                 test_a_bulk_import_is_one_write,
                 test_an_import_adds_groups_rather_than_replacing_them,
                 test_the_export_cannot_be_a_formula,
                 test_the_export_carries_no_secret,
                 test_the_export_can_be_one_group,
                 test_an_export_re_imports_as_itself,
                 test_a_file_with_no_header_is_read_in_column_order,
                 test_a_bulk_edit_is_one_write,
                 test_a_bulk_edit_leaves_alone_what_it_was_not_given,
                 test_a_bulk_edit_refuses_secrets_and_what_the_device_said,
                 test_a_bulk_edit_reports_a_merge_rather_than_making_one,
                 test_a_bulk_edit_can_attach_and_detach_a_shared_credential):
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
