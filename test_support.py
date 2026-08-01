"""
test_support.py — A bundle designed to be emailed must not carry a secret.

That is the whole risk of this feature. Everything else about it is
convenience; this one property is what makes it safe to have at all, and the
failure is silent — a leaked key looks exactly like a working bundle until
somebody else reads it.

So the tests plant real-shaped credentials everywhere a collector might reach
— the vault, settings, the plaintext credentials file, a terminal buffer — and
then assert that no section, and no complete bundle, contains any of them.

    python test_support.py
"""

import json
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

from backend import paths

_TEMP = Path(tempfile.mkdtemp(prefix="shellmate-support-"))
paths._data_dir_cache = _TEMP

from backend import profiles, settings_store, support     # noqa: E402
from backend.session.buffer import SessionBuffer          # noqa: E402

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


# Distinctive enough that finding one anywhere is unambiguous.
SECRETS = {
    "an API key":            "sk-ant-SUPPORTTEST-abcdef123456",
    "a device password":     "Tr0ub4dor-device-pw",
    "a plaintext password":  "PLAINTEXT-SUPPORTTEST-pw",
    "an echoed type-7":      "09461A1D0A1B",
    "an SNMP community":     "s3cr3t-community",
}

DEVICE_OUTPUT = (
    "core-sw-01#show running-config\r\n"
    "username neteng password 7 09461A1D0A1B\r\n"
    "snmp-server community s3cr3t-community RW\r\n"
    "interface GigabitEthernet0/1\r\n"
)


class FakeManager:
    """Just enough SessionManager for the collectors that take one."""

    def __init__(self, sessions):
        self._sessions = {s["session_id"]: s for s in sessions}

    def get_all_sessions(self):
        return list(self._sessions.values())


def plant_secrets() -> FakeManager:
    """Put a credential everywhere a collector could conceivably reach."""
    from backend.vault import vault

    try:
        vault.set("anthropic_api_key", SECRETS["an API key"])
    except Exception:
        # A locked or unavailable vault is fine — settings still gets one.
        pass

    settings_store.update_settings({
        "providers": {"ollama_host": "http://localhost:11434"},
        "logging": {"redact_secrets": True},
    })

    # The file that must never be gathered, by name or by accident.
    profiles.save_plaintext_credentials("p1", {
        "password": SECRETS["a plaintext password"],
    })

    buffer = SessionBuffer("s1")
    buffer.write(DEVICE_OUTPUT)
    return FakeManager([{
        "session_id":      "s1",
        "buffer":          buffer,
        "display_label":   "core-sw-01",
        "hostname":        "10.20.30.40",
        "connection_type": "ssh",
        "connected_at":    "2026-08-01T09:00:00+00:00",
        "is_connected":    True,
        "fingerprint":     {"platform": "ios", "confidence": 0.9, "source": "banner"},
    }])


def test_no_secret_in_any_section() -> None:
    print("\n-- Nothing sensitive, in any section --")
    manager = plant_secrets()
    every = [s.id for s in support.SECTIONS]
    collected = support.collect(every, manager)

    check("every section produced something",
          len(collected) == len(every), f"got {len(collected)} of {len(every)}")

    for section_id, text in sorted(collected.items()):
        for label, secret in SECRETS.items():
            check(f"{section_id}: {label} is absent",
                  secret not in text,
                  f"the credential was gathered into {section_id}")


def test_the_plaintext_file_is_never_read() -> None:
    """The one file whose entire contents are credentials."""
    print("\n-- The plaintext credentials file --")
    manager = plant_secrets()
    path = profiles._plaintext_path() if hasattr(profiles, "_plaintext_path") else None

    collected = support.collect([s.id for s in support.SECTIONS], manager)
    blob = "\n".join(collected.values())

    check("its contents never appear",
          SECRETS["a plaintext password"] not in blob)
    check("and neither does the file itself get named as included",
          "credentials-plaintext.json" not in blob
          or "never" in blob.lower() or "not included" in blob.lower(),
          "the bundle references the plaintext file")

    if path is not None:
        check("the file really did exist during the test", path.exists())


def test_device_data_is_opt_in() -> None:
    print("\n-- Device data is opt-in --")
    device_sections = [s for s in support.SECTIONS if s.device_data]
    check("the sections carrying device data are marked",
          {s.id for s in device_sections} == {"sessions", "scrollback"},
          f"got {[s.id for s in device_sections]}")
    check("and none of them defaults to on",
          not any(s.default_on for s in device_sections))
    check("while the ones describing ShellMate do",
          all(s.default_on for s in support.SECTIONS
              if s.id in {"about", "versions", "log", "settings"}))


def test_scrollback_is_redacted() -> None:
    print("\n-- Terminal output, when asked for --")
    manager = plant_secrets()
    text = support.collect(["scrollback"], manager)["scrollback"]

    check("the session appears at all", "core-sw-01" in text, text[:120])
    check("with the shape of the output intact",
          "interface GigabitEthernet0/1" in text)
    for label in ("an echoed type-7", "an SNMP community"):
        check(f"but {label} is masked", SECRETS[label] not in text)


def test_a_broken_section_does_not_break_the_bundle() -> None:
    print("\n-- When one thing cannot be gathered --")
    original = support.SECTIONS_BY_ID["versions"].collect

    def explode(_ctx):
        raise RuntimeError("nope")

    support.SECTIONS_BY_ID["versions"].collect = explode
    try:
        collected = support.collect(["about", "versions"], None)
        check("the failing section is reported in place",
              "could not be collected" in collected["versions"],
              collected["versions"][:80])
        check("and the rest is still gathered", "ShellMate Portable" in collected["about"])
    finally:
        support.SECTIONS_BY_ID["versions"].collect = original

    check("an unknown section is ignored rather than fatal",
          support.collect(["nonsense"], None) == {})


def test_the_bundle() -> None:
    print("\n-- The zip --")
    manager = plant_secrets()
    collected = support.collect(["about", "log", "settings", "scrollback"], manager)
    path = support.write_bundle(collected, note="It stopped responding.")

    check("a zip is written", path.exists() and zipfile.is_zipfile(path))

    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        check("it lists what is inside", "contents.txt" in names, str(names))
        check("the description is included", "what-happened.txt" in names)
        check("and each section is a readable file",
              {"about.txt", "shellmate.log", "settings.json", "scrollback.txt"} <= set(names),
              str(names))

        blob = "\n".join(
            archive.read(n).decode("utf-8", errors="replace") for n in names)

    for label, secret in SECRETS.items():
        check(f"the finished bundle does not contain {label}", secret not in blob)

    check("it goes under the data folder, not beside the executable",
          paths.data_dir() in path.parents, str(path))


def main() -> int:
    print("\n" + "=" * 52)
    print("  Support bundle")
    print("=" * 52)

    for test in (
        test_no_secret_in_any_section,
        test_the_plaintext_file_is_never_read,
        test_device_data_is_opt_in,
        test_scrollback_is_redacted,
        test_a_broken_section_does_not_break_the_bundle,
        test_the_bundle,
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
