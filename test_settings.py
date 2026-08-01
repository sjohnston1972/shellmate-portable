"""
test_settings.py — Settings persistence, and the promise not to change a setup.

The interesting case is not reading a value back; it is what happens when a
*default changes* after people are already using the application. Deep-merging
the new default over an existing settings.json reaches into working setups and
alters them: someone who has used the assistant panel daily for months would
find it gone after an update, having done nothing to cause it and with nothing
on screen to explain it.

So an existing file keeps the old default, written in explicitly, and only a
genuinely first run — no file at all — sees the new one. These tests hold that
line, including for the ordinary case where saving anything at all must not
disturb the rest.

    python test_settings.py
"""

import json
import shutil
import sys
import tempfile
from pathlib import Path

from backend import paths

_TEMP = Path(tempfile.mkdtemp(prefix="shellmate-settings-"))
paths._data_dir_cache = _TEMP

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


def write_settings(document: dict) -> None:
    paths.settings_file().parent.mkdir(parents=True, exist_ok=True)
    paths.settings_file().write_text(json.dumps(document, indent=2), encoding="utf-8")


def clear_settings() -> None:
    paths.settings_file().unlink(missing_ok=True)


def test_first_run_defaults() -> None:
    print("\n-- A first run --")
    clear_settings()
    settings = settings_store.get_settings()
    check("the assistant panel starts hidden",
          settings["ai"]["panel_enabled"] is False,
          f"got {settings['ai']['panel_enabled']!r}")
    check("and nothing was written just by reading",
          not paths.settings_file().exists())


def test_existing_install_keeps_its_behaviour() -> None:
    print("\n-- An installation that predates the setting --")

    # A settings.json written before ai.panel_enabled existed at all.
    write_settings({"terminal": {"font_size": 16}})
    settings = settings_store.get_settings()
    check("keeps the assistant panel it has always had",
          settings["ai"]["panel_enabled"] is True,
          f"got {settings['ai']['panel_enabled']!r}")
    check("and its own settings are untouched",
          settings["terminal"]["font_size"] == 16,
          f"got {settings['terminal']['font_size']!r}")

    # The whole ai section missing is the same situation.
    write_settings({"ai": {"mode": "learn"}})
    settings = settings_store.get_settings()
    check("a partial ai section is treated the same way",
          settings["ai"]["panel_enabled"] is True,
          f"got {settings['ai']['panel_enabled']!r}")
    check("without losing what it did say",
          settings["ai"]["mode"] == "learn",
          f"got {settings['ai']['mode']!r}")


def test_an_explicit_choice_wins() -> None:
    print("\n-- An explicit choice --")
    write_settings({"ai": {"panel_enabled": False}})
    check("turning it off stays off",
          settings_store.get_settings()["ai"]["panel_enabled"] is False)

    write_settings({"ai": {"panel_enabled": True}})
    check("turning it on stays on",
          settings_store.get_settings()["ai"]["panel_enabled"] is True)


def test_saving_preserves_everything_else() -> None:
    print("\n-- Saving one setting --")
    clear_settings()
    settings_store.update_settings({"terminal": {"font_size": 18}})
    settings_store.update_settings({"ai": {"panel_enabled": True}})

    settings = settings_store.get_settings()
    check("the earlier save survives the later one",
          settings["terminal"]["font_size"] == 18,
          f"got {settings['terminal']['font_size']!r}")
    check("and the later one took effect",
          settings["ai"]["panel_enabled"] is True)

    stored = json.loads(paths.settings_file().read_text(encoding="utf-8"))
    check("the choice is written down, not merely inferred",
          stored.get("ai", {}).get("panel_enabled") is True,
          f"settings.json holds {stored.get('ai')!r}")


def test_unreadable_file_falls_back() -> None:
    print("\n-- A corrupt settings file --")
    paths.settings_file().write_text("{ this is not json", encoding="utf-8")
    settings = settings_store.get_settings()
    check("defaults are used rather than raising",
          settings["ai"]["panel_enabled"] is False,
          f"got {settings['ai']['panel_enabled']!r}")
    check("and the rest of the defaults are intact",
          settings["terminal"]["scrollback_lines"] == 5000)


def main() -> int:
    print("\n" + "=" * 52)
    print("  Settings")
    print("=" * 52)

    for test in (
        test_first_run_defaults,
        test_existing_install_keeps_its_behaviour,
        test_an_explicit_choice_wins,
        test_saving_preserves_everything_else,
        test_unreadable_file_falls_back,
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
