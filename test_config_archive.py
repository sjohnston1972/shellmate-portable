"""
test_config_archive.py — Captured configurations written out as files.

The capture itself is invisible by design: it runs on a second channel and
nothing appears in the user's terminal. That is exactly why the file end of it
has to be tested rather than observed — nobody is watching it happen.

Three properties matter more than the rest:

  - **Secrets are masked.** A running configuration carries hashes, keys and
    community strings, and this writes it to a folder the user picked, which
    may be a share and will probably be backed up.
  - **It is bounded.** A capture per login across a fleet fills a disk. Three
    limits — per device, by age, by total size — and all of them apply.
  - **It never breaks the session.** An unwritable folder is a missing copy.

    python test_config_archive.py
"""

import shutil
import sys
import tempfile
import time
from pathlib import Path

from backend import paths

_TEMP = Path(tempfile.mkdtemp(prefix="shellmate-archive-"))
paths._data_dir_cache = _TEMP

from backend import config_archive                          # noqa: E402
from backend import settings_store                          # noqa: E402

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


CONFIG = "\n".join([
    "hostname core-sw-01",
    "!",
    "username neteng password 7 09461A1D0A1B",
    "enable secret 5 $1$abcd$efghijklmnop",
    "snmp-server community s3cr3t RO",
    "!",
    "interface GigabitEthernet0/1",
    " description uplink",
    " switchport mode trunk",
])


def configure(**logging_settings) -> None:
    """Write a settings file with the given logging block."""
    base = {
        "capture_configs": True,
        "save_config_files": True,
        "redact_secrets": True,
        "config_directory": "configs",
        "config_keep_per_device": 20,
        "config_max_age_days": 365,
        "config_max_total_mb": 200,
    }
    base.update(logging_settings)
    settings_store.update_settings({"logging": base})


def reset_archive() -> None:
    shutil.rmtree(settings_store.config_directory(), ignore_errors=True)


def test_written_where_asked() -> None:
    print("\n-- Writing a capture --")
    configure()
    reset_archive()

    result = config_archive.archive("core-sw-01", CONFIG)
    check("a capture is written", result["written"], result.get("reason", ""))

    path = Path(result["path"])
    check("under a folder named for the device",
          path.parent.name == "core-sw-01", f"got {path.parent.name!r}")
    check("with a timestamped name", path.name.startswith("core-sw-01-")
          and path.suffix == ".cfg", f"got {path.name!r}")
    check("inside the configured archive directory",
          settings_store.config_directory() in path.parents,
          f"{path} is not under {settings_store.config_directory()}")


def test_secrets_are_masked() -> None:
    print("\n-- Redaction --")
    configure()
    reset_archive()

    written = Path(config_archive.archive("core-sw-01", CONFIG)["path"])
    body = written.read_text(encoding="utf-8")

    for secret in ("09461A1D0A1B", "$1$abcd$efghijklmnop", "s3cr3t"):
        check(f"{secret[:12]}… does not reach the file", secret not in body,
              "the credential was written in the clear")
    check("but the shape of the configuration survives",
          "username neteng password" in body and "interface GigabitEthernet0/1" in body)

    # Turning redaction off is the user's call, and applies here too.
    configure(redact_secrets=False)
    reset_archive()
    plain = Path(config_archive.archive("core-sw-01", CONFIG)["path"]).read_text(encoding="utf-8")
    check("redaction can be switched off", "s3cr3t" in plain)


def test_unchanged_captures_are_not_duplicated() -> None:
    print("\n-- An unchanged device --")
    configure()
    reset_archive()

    first = config_archive.archive("core-sw-01", CONFIG, changed=True)
    check("the first capture is kept", first["written"])

    again = config_archive.archive("core-sw-01", CONFIG, changed=False)
    check("an identical one is not written again", not again["written"],
          f"wrote {again.get('path')!r}")
    check("and says why", "nchanged" in again["reason"], again["reason"])

    # ...unless there is nothing there at all, or enabling the setting later
    # would produce a folder that stays empty until something changes.
    reset_archive()
    fresh = config_archive.archive("core-sw-01", CONFIG, changed=False)
    check("the first capture is written even if unchanged", fresh["written"],
          fresh.get("reason", ""))


def test_switched_off() -> None:
    print("\n-- Switched off --")
    configure(save_config_files=False)
    reset_archive()
    result = config_archive.archive("core-sw-01", CONFIG)
    check("nothing is written", not result["written"])
    check("and no folder is created",
          not settings_store.config_directory().exists(),
          "an empty folder appeared anyway")

    configure(capture_configs=False, save_config_files=True)
    check("capture off means capture off", not config_archive.capture_enabled())
    check("and files off with it", not config_archive.enabled())


def test_retention_per_device() -> None:
    print("\n-- Keeping only the newest per device --")
    configure(config_keep_per_device=3)
    reset_archive()

    folder = settings_store.config_directory() / "core-sw-01"
    folder.mkdir(parents=True, exist_ok=True)
    for index in range(6):
        path = folder / f"core-sw-01-2026010{index}-120000.cfg"
        path.write_text(f"config {index}", encoding="utf-8")
        # Distinct mtimes, oldest first, so "newest" is unambiguous.
        stamp = time.time() - (6 - index) * 3600
        import os
        os.utime(path, (stamp, stamp))

    config_archive.prune()
    left = sorted(p.name for p in folder.glob("*.cfg"))
    check("only the newest three survive", len(left) == 3, f"got {left}")
    check("and they are the newest three",
          left == ["core-sw-01-20260103-120000.cfg",
                   "core-sw-01-20260104-120000.cfg",
                   "core-sw-01-20260105-120000.cfg"],
          f"got {left}")


def test_retention_by_age_and_size() -> None:
    print("\n-- Age and total size --")
    import os

    configure(config_keep_per_device=0, config_max_age_days=30, config_max_total_mb=0)
    reset_archive()

    folder = settings_store.config_directory() / "edge-rtr"
    folder.mkdir(parents=True, exist_ok=True)

    old = folder / "edge-rtr-20200101-120000.cfg"
    old.write_text("ancient", encoding="utf-8")
    ancient = time.time() - 400 * 86400
    os.utime(old, (ancient, ancient))

    recent = folder / "edge-rtr-20260101-120000.cfg"
    recent.write_text("current", encoding="utf-8")

    config_archive.prune()
    check("anything past its age is removed", not old.exists())
    check("and anything within it is kept", recent.exists())

    # A 1 MB ceiling with four 400 KB captures in the archive: two have to go,
    # and they must be the two oldest.
    reset_archive()
    folder.mkdir(parents=True, exist_ok=True)
    configure(config_keep_per_device=0, config_max_age_days=0, config_max_total_mb=1)

    for index in range(4):
        path = folder / f"edge-rtr-2026020{index}-120000.cfg"
        path.write_text("x" * 400_000, encoding="utf-8")
        stamp = time.time() - (4 - index) * 3600
        os.utime(path, (stamp, stamp))

    removed = config_archive.prune()
    left = sorted(p.name for p in folder.glob("*.cfg"))
    check("the archive is brought under its size limit", removed == 2, f"removed {removed}")
    check("by discarding the oldest first",
          left == ["edge-rtr-20260202-120000.cfg", "edge-rtr-20260203-120000.cfg"],
          f"got {left}")

    # And with every limit at zero, nothing is touched at all.
    configure(config_keep_per_device=0, config_max_age_days=0, config_max_total_mb=0)
    before = len(list(folder.glob("*.cfg")))
    check("no limits set means nothing is removed",
          config_archive.prune() == 0 and len(list(folder.glob("*.cfg"))) == before,
          f"{before} files before")


def test_awkward_hostnames() -> None:
    print("\n-- Device names a filesystem will not take --")
    check("separators cannot escape the archive",
          "/" not in config_archive.safe_name("core/1")
          and "\\" not in config_archive.safe_name("core\\1"))
    check("traversal is neutralised",
          config_archive.safe_name("../../etc") == "etc",
          f"got {config_archive.safe_name('../../etc')!r}")
    check("an empty name still yields something",
          config_archive.safe_name("") == "device",
          f"got {config_archive.safe_name('')!r}")

    configure()
    reset_archive()
    result = config_archive.archive("core/1:vrf", CONFIG)
    check("and such a device can still be captured", result["written"],
          result.get("reason", ""))
    check("into one folder, not a nested path",
          Path(result["path"]).parent.parent == settings_store.config_directory(),
          f"got {result['path']}")


def test_failure_is_not_fatal() -> None:
    print("\n-- When the folder cannot be written --")
    # A path that cannot be created: a file standing where the folder must go.
    blocker = _TEMP / "blocked"
    blocker.write_text("not a directory", encoding="utf-8")
    configure(config_directory=str(blocker))

    result = config_archive.archive("core-sw-01", CONFIG)
    check("it reports rather than raises", not result["written"])
    check("with something a person can act on",
          "archive folder" in result["reason"], result["reason"])


def main() -> int:
    print("\n" + "=" * 52)
    print("  Configuration archive")
    print("=" * 52)

    for test in (
        test_written_where_asked,
        test_secrets_are_masked,
        test_unchanged_captures_are_not_duplicated,
        test_switched_off,
        test_retention_per_device,
        test_retention_by_age_and_size,
        test_awkward_hostnames,
        test_failure_is_not_fatal,
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
