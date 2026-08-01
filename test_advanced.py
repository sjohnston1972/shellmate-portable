"""
test_advanced.py — Stockton, and the promise that it cannot break anything.

Sixty-odd values that were constants in the source are now editable. The
admission test for putting one here was that the worst outcome is *degraded*,
never broken — so these tests attack the bounds rather than the happy path.

The registry is also its own worst enemy: a setting whose default sits outside
its own range would be clamped away the first time anything read it, and the
application would behave as though the default were something else entirely.
That is checked first, for every entry, because it is the failure that would
be hardest to notice.

    python test_advanced.py
"""

import json
import shutil
import sys
import tempfile
from pathlib import Path

from backend import paths

_TEMP = Path(tempfile.mkdtemp(prefix="shellmate-advanced-"))
paths._data_dir_cache = _TEMP

from backend import advanced, settings_store               # noqa: E402

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


def fresh() -> None:
    advanced.reset()


def test_the_registry_is_coherent() -> None:
    """Every declaration has to make sense on its own terms."""
    print("\n-- The registry --")
    fresh()

    check("there are enough settings to be worth a panel",
          len(advanced.SETTINGS) >= 40, f"got {len(advanced.SETTINGS)}")

    keys = [s.key for s in advanced.SETTINGS]
    check("keys are unique", len(keys) == len(set(keys)))
    check("every key is category-qualified",
          all("." in k for k in keys),
          str([k for k in keys if "." not in k]))

    unknown = {s.category for s in advanced.SETTINGS} - set(advanced.CATEGORIES)
    check("every category has a heading", not unknown, f"missing {sorted(unknown)}")

    for setting in advanced.SETTINGS:
        # The one that would be hardest to spot: a default outside its own
        # bounds is silently replaced the first time anything reads it.
        check(f"{setting.key}: the default survives its own clamp",
              setting.clamp(setting.default) == setting.default,
              f"default {setting.default!r} clamps to "
              f"{setting.clamp(setting.default)!r}")

    for setting in advanced.SETTINGS:
        if setting.kind in ("int", "float"):
            check(f"{setting.key}: numeric settings are bounded",
                  setting.minimum is not None and setting.maximum is not None,
                  "an unbounded number can be set to anything")
        if setting.kind == "choice":
            check(f"{setting.key}: the default is one of the choices",
                  setting.default in setting.choices,
                  f"{setting.default!r} not in {setting.choices}")
        check(f"{setting.key}: it explains itself", bool(setting.summary))


def test_nothing_can_be_set_out_of_range() -> None:
    """The bound is enforced here, not by the browser."""
    print("\n-- Values that would break something --")
    fresh()

    hostile = {
        "ssh.connect_timeout":      -5,
        "ssh.read_timeout":         0,
        "history.buffer_lines":     0,
        "alerts.max_toasts":        99999,
        "identify.act_threshold":   0.0,
        "broadcast.max_seconds":    0,
        "ai.max_tokens":            -1,
    }
    advanced.update(hostile)

    for key in hostile:
        setting = advanced.SETTINGS_BY_KEY[key]
        value = advanced.get(key)
        check(f"{key} stays within {setting.minimum}–{setting.maximum}",
              setting.minimum <= value <= setting.maximum,
              f"got {value}")

    check("a zero confidence threshold is refused",
          advanced.get("identify.act_threshold") >= 0.4,
          "an unidentified device could be acted on")


def test_garbage_does_not_raise() -> None:
    """settings.json is a text file people are encouraged to edit."""
    print("\n-- A file edited by hand, badly --")
    fresh()

    advanced.update({
        "ssh.connect_timeout":     "not a number",
        "history.record":          "yes please",
        "ssh.host_key_policy":     "whatever",
        "terminal.renderer":       None,
        "nonexistent.setting":     42,
    })

    check("an unparseable number falls back to the default",
          advanced.get("ssh.connect_timeout")
          == advanced.SETTINGS_BY_KEY["ssh.connect_timeout"].default)
    check("an invalid choice falls back to the default",
          advanced.get("ssh.host_key_policy") == "auto-add",
          f"got {advanced.get('ssh.host_key_policy')!r}")
    check("and so does a null one",
          advanced.get("terminal.renderer") == "canvas")
    check("an unknown key is dropped rather than stored",
          "nonexistent.setting" not in (
              settings_store.get_settings().get("advanced") or {}))
    check("reading every value still works",
          len(advanced.all_values()) == len(advanced.SETTINGS))

    try:
        advanced.get("no.such.thing")
        check("an unknown key raises rather than returning None", False,
              "it returned a value")
    except KeyError:
        check("an unknown key raises rather than returning None", True)


def test_reset_at_three_granularities() -> None:
    print("\n-- Getting back --")
    fresh()
    advanced.update({
        "ssh.connect_timeout": 60,
        "ssh.keepalive_seconds": 30,
        "alerts.max_toasts": 7,
    })

    advanced.reset(key="ssh.connect_timeout")
    check("one setting resets", advanced.get("ssh.connect_timeout") == 15)
    check("and leaves its neighbours alone",
          advanced.get("ssh.keepalive_seconds") == 30)

    advanced.reset(category="ssh")
    check("a category resets", advanced.get("ssh.keepalive_seconds") == 0)
    check("and leaves other categories alone", advanced.get("alerts.max_toasts") == 7)

    advanced.reset()
    check("everything resets", advanced.get("alerts.max_toasts") == 3)
    check("and the stored block is emptied",
          not (settings_store.get_settings().get("advanced") or {}))


def test_reset_actually_removes_from_the_file() -> None:
    """
    The trap a deep merge sets.

    update_settings deep-merges, which can add a key and change one but never
    remove one. A reset written that way would leave the old value in
    settings.json and it would come straight back on the next read.
    """
    print("\n-- Reset really removes --")
    fresh()
    advanced.update({"alerts.max_toasts": 9})

    stored = json.loads(paths.settings_file().read_text(encoding="utf-8"))
    check("the change is written to disk",
          stored["advanced"].get("alerts.max_toasts") == 9, str(stored.get("advanced")))

    advanced.reset(key="alerts.max_toasts")
    stored = json.loads(paths.settings_file().read_text(encoding="utf-8"))
    check("and resetting takes it out of the file again",
          "alerts.max_toasts" not in (stored.get("advanced") or {}),
          f"still there: {stored.get('advanced')}")
    check("so it does not come back on the next read",
          advanced.get("alerts.max_toasts") == 3)


def test_the_settings_reach_the_code() -> None:
    """A registry nothing reads would be an elaborate no-op."""
    print("\n-- The code actually reads them --")
    fresh()

    from backend.fingerprint import Fingerprint

    weak = Fingerprint(platform="ios", confidence=0.5)
    check("a 0.5 guess is not acted on by default", not weak.certain_enough_to_act)
    advanced.update({"identify.act_threshold": 0.45})
    check("lowering the threshold changes that", weak.certain_enough_to_act)
    advanced.reset(key="identify.act_threshold")

    import backend.connections.ssh_handler as ssh

    advanced.update({"ssh.host_key_policy": "reject"})
    check("the host key policy is honoured",
          type(ssh._host_key_policy()).__name__ == "RejectPolicy")
    advanced.reset(key="ssh.host_key_policy")

    advanced.update({"ssh.kex_algorithms": "diffie-hellman-group1-sha1"})
    overrides = ssh._algorithm_overrides()
    check("naming a legacy key exchange disables the others",
          bool(overrides.get("disabled_algorithms", {}).get("kex")))
    check("and does not disable the one asked for",
          "diffie-hellman-group1-sha1"
          not in overrides["disabled_algorithms"]["kex"])
    advanced.reset()

    check("with nothing set, paramiko's defaults are untouched",
          ssh._algorithm_overrides() == {})


def test_what_is_deliberately_absent() -> None:
    """The exclusions are part of the deliverable, not an omission."""
    print("\n-- Deliberately not here --")
    keys = {s.key for s in advanced.SETTINGS}

    for forbidden in ("vault.scrypt_n", "vault.scrypt_r", "broadcast.confirm",
                      "server.bind_host", "pipeline.max_alias_line"):
        check(f"{forbidden} is not exposed", forbidden not in keys,
              "a setting that fails the admission test is present")

    check("and the reasons are recorded for the panel",
          len(advanced.NOT_EXPOSED) >= 4)
    check("each with an explanation",
          all(label and why for label, why in advanced.NOT_EXPOSED))


def test_describe_is_renderable() -> None:
    print("\n-- What the panel gets --")
    fresh()
    advanced.update({"alerts.max_toasts": 6})
    described = advanced.describe()

    check("every setting is described", len(described["settings"]) == len(advanced.SETTINGS))
    entry = next(s for s in described["settings"] if s["key"] == "alerts.max_toasts")
    check("a changed setting is flagged", entry["modified"])
    check("with its default alongside", entry["default"] == 3 and entry["value"] == 6)

    untouched = next(s for s in described["settings"] if s["key"] == "ai.temperature")
    check("and an untouched one is not", not untouched["modified"])

    check("bounds are included so the panel can show them",
          entry["min"] == 1 and entry["max"] == 10)
    check("restart-required settings say so",
          any(s["restart"] for s in described["settings"]))


def main() -> int:
    print("\n" + "=" * 52)
    print("  Stockton — advanced settings")
    print("=" * 52)

    for test in (
        test_the_registry_is_coherent,
        test_nothing_can_be_set_out_of_range,
        test_garbage_does_not_raise,
        test_reset_at_three_granularities,
        test_reset_actually_removes_from_the_file,
        test_the_settings_reach_the_code,
        test_what_is_deliberately_absent,
        test_describe_is_renderable,
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
