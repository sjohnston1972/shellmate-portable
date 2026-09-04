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
        "diag.log_level":          None,
        "nonexistent.setting":     42,
    })

    check("an unparseable number falls back to the default",
          advanced.get("ssh.connect_timeout")
          == advanced.SETTINGS_BY_KEY["ssh.connect_timeout"].default)
    check("an invalid choice falls back to the default",
          advanced.get("ssh.host_key_policy") == "auto-add",
          f"got {advanced.get('ssh.host_key_policy')!r}")
    check("and so does a null one",
          advanced.get("diag.log_level") == "INFO",
          f"got {advanced.get('diag.log_level')!r}")
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


def _a_legacy_algorithm() -> tuple[str, str, str]:
    """
    A setting, its group, and one legacy algorithm paramiko really offers.

    Discovered rather than named. paramiko 5.0 dropped every SHA-1 key
    exchange, so a test that hardcoded `diffie-hellman-group1-sha1` failed
    for a change in a dependency while the code under test was correct —
    and a test that cries wolf gets muted.
    """
    for setting, group in (("ssh.kex_algorithms", "kex"),
                           ("ssh.ciphers", "ciphers"),
                           ("ssh.macs", "macs"),
                           ("ssh.host_key_algorithms", "keys")):
        for name in advanced.available_algorithms(group):
            if name in advanced.LEGACY_ALGORITHMS:
                return setting, group, name
    return "ssh.kex_algorithms", "kex", ""


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

    # The policy is no longer a paramiko object handed over wholesale (#528):
    # ShellMate makes the decision itself, so the setting is checked by the
    # decision refusing an unknown key rather than by the class it returns.
    import paramiko

    from backend.connections.base import ConnectionError_

    advanced.update({"ssh.host_key_policy": "reject"})
    refused = None
    try:
        ssh._verify_host_key("no-such-host.invalid", 22, paramiko.ECDSAKey.generate())
    except ConnectionError_ as exc:
        refused = exc
    check("the host key policy is honoured", refused is not None
          and "reject unknown keys" in str(refused), repr(refused))
    advanced.reset(key="ssh.host_key_policy")

    # The algorithm is taken from what paramiko actually offers rather than
    # named here. paramiko 5.0 removed every SHA-1 key exchange, so a
    # hardcoded `diffie-hellman-group1-sha1` made this test fail for a
    # change in a dependency while the code under test was correct.
    setting, group, legacy_name = _a_legacy_algorithm()
    if legacy_name:
        advanced.update({setting: legacy_name})
        overrides = ssh._algorithm_overrides()
        check(f"naming a legacy {group} entry disables the others",
              bool(overrides.get("disabled_algorithms", {}).get(group)),
              f"chose {legacy_name}")
        check("and does not disable the one asked for",
              legacy_name not in overrides["disabled_algorithms"][group])
        advanced.reset()
    else:
        check("there is a legacy algorithm to restrict to", False,
              "paramiko offers no legacy algorithm in any group, so the "
              "settings that exist to reach old kit cannot be exercised — "
              "which is worth knowing rather than skipping")

    check("with nothing set, paramiko's defaults are untouched",
          ssh._algorithm_overrides() == {})


def test_algorithm_settings() -> None:
    """
    The four negotiated SSH lists, which are pickers rather than text fields.

    These are the settings that exist to reach a device the defaults will not,
    so a value that cannot match is the failure that matters: it disables
    everything else while enabling nothing, and the device stays unreachable
    for a new reason.
    """
    print("\n-- SSH algorithm lists --")
    fresh()

    keys = [s.key for s in advanced.SETTINGS if s.kind == "algorithms"]
    check("all four negotiated lists are exposed",
          set(keys) == {"ssh.kex_algorithms", "ssh.ciphers", "ssh.macs",
                        "ssh.host_key_algorithms"},
          f"got {sorted(keys)}")

    for group in ("kex", "ciphers", "macs", "keys"):
        offered = advanced.available_algorithms(group)
        check(f"{group}: paramiko's list is readable", len(offered) >= 5,
              f"got {len(offered)}")
        check(f"{group}: in preference order, not sorted",
              offered != sorted(offered) or len(offered) < 2,
              "the list came back alphabetical, which puts the weakest first")

    check("an unknown group yields nothing rather than raising",
          advanced.available_algorithms("nonsense") == [])

    # The legacy entries are the reason these settings exist, so they must be
    # present and flagged rather than filtered out. Which entry is legacy
    # depends on the paramiko in use, so it is discovered.
    setting, group, legacy_name = _a_legacy_algorithm()
    check("a legacy algorithm is still offered somewhere",
          bool(legacy_name),
          "paramiko offers none in any group; the settings that exist to "
          "reach old kit have nothing to reach it with")
    described = next(s for s in advanced.describe()["settings"]
                     if s["key"] == setting)
    legacy = [a["name"] for a in described["algorithms"] if a["legacy"]]
    check("and it is marked as legacy", legacy_name in legacy,
          f"{legacy_name} not in {legacy}")
    modern = next((a["name"] for a in described["algorithms"]
                   if not a["legacy"]), "")
    check("while a modern one is not", modern and modern not in legacy,
          f"{modern!r} was flagged legacy")

    # A name paramiko does not offer is dropped, not stored.
    advanced.update({setting: f"{legacy_name}, not-a-real-algorithm, "})
    check("a typo is dropped rather than kept",
          advanced.get(setting) == legacy_name,
          f"got {advanced.get('ssh.kex_algorithms')!r}")

    advanced.update({"ssh.ciphers": ["aes256-ctr", "aes128-cbc"]})
    check("a list is accepted as well as a string",
          advanced.get("ssh.ciphers") == "aes256-ctr,aes128-cbc",
          f"got {advanced.get('ssh.ciphers')!r}")

    advanced.update({"ssh.macs": "nothing-valid-at-all"})
    check("a value with nothing valid in it becomes blank",
          advanced.get("ssh.macs") == "",
          f"got {advanced.get('ssh.macs')!r} — a non-empty value that matches "
          f"nothing would disable every algorithm")


def test_algorithms_reach_paramiko() -> None:
    """paramiko takes the inverse: naming what you want disables the rest."""
    print("\n-- What paramiko is actually told --")
    fresh()
    import backend.connections.ssh_handler as ssh

    check("nothing chosen restricts nothing", ssh._algorithm_overrides() == {})

    # Two groups, both discovered: which algorithms exist is paramiko's
    # business and changes between versions, while "naming one restricts
    # its group and leaves the others alone" is ShellMate's and does not.
    picked = {}
    for setting, group in (("ssh.kex_algorithms", "kex"),
                           ("ssh.ciphers", "ciphers"),
                           ("ssh.macs", "macs"),
                           ("ssh.host_key_algorithms", "keys")):
        have = advanced.available_algorithms(group)
        if len(have) > 1:
            picked[group] = (setting, have[-1])
        if len(picked) == 2:
            break
    check("there are two groups to restrict", len(picked) == 2,
          f"paramiko offers usable lists for {sorted(picked)} only")

    advanced.update({setting: name for setting, name in picked.values()})
    disabled = ssh._algorithm_overrides().get("disabled_algorithms", {})

    check("only the groups chosen are restricted",
          set(disabled) == set(picked), f"got {sorted(disabled)}")
    for group, (_setting, wanted) in picked.items():
        check(f"{group}: the chosen algorithm is not disabled",
              wanted not in disabled[group])
        check(f"{group}: everything else is",
              len(disabled[group]) == len(advanced.available_algorithms(group)) - 1,
              f"disabled {len(disabled[group])} of "
              f"{len(advanced.available_algorithms(group))}")

    advanced.reset()
    check("and resetting hands paramiko its defaults back",
          ssh._algorithm_overrides() == {})


def test_every_setting_is_actually_read() -> None:
    """
    A setting nothing reads is worse than no setting at all.

    The rest of this file checks the registry is coherent — defaults inside
    their ranges, categories with headings, values clamped. None of that
    notices a setting that is declared, stored, reported as modified, reset by
    the reset button, and read by nothing.

    That is exactly what happened: 35 of 57 were inert. The failure is silent,
    because the value really is saved and there is nothing to see.

    Crude on purpose. It looks for the key as a string anywhere outside
    advanced.py, which cannot prove the value is used *well* — only that
    something asked for it.
    """
    print(chr(10) + "-- Every setting reaches the code --")

    root = Path(__file__).parent
    sources = []
    for pattern in ("backend/**/*.py", "frontend/js/*.js", "run.py"):
        sources.extend(root.glob(pattern))

    blob = ""
    for source in sources:
        if source.name == "advanced.py":
            continue
        try:
            blob += source.read_text(encoding="utf-8")
        except OSError:
            continue

    unread = [s.key for s in advanced.SETTINGS if s.key not in blob]
    check("nothing is declared and then ignored", not unread,
          f"{len(unread)} setting(s) nothing reads: {', '.join(sorted(unread))}")


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


def test_a_setting_that_says_it_does_something_does_it() -> None:
    """
    Four settings that were declared, exposed, and read by nothing.

    This is the failure mode the registry was built to prevent — the
    declaration *is* the default the code reads, so a setting and its
    behaviour cannot drift. These drifted anyway, by being read in one place
    and ignored in another, or by being read into a variable nobody used.

    Each is checked against the code that has to honour it rather than against
    the registry, because the registry was never the half that was wrong.
    """
    print(chr(10) + "-- Settings that have to mean something --")
    from pathlib import Path as _Path

    root = _Path(__file__).parent

    store = (root / "backend" / "store.py").read_text(encoding="utf-8")
    # history.record: start_session() checked it and add_command() did not, so
    # switching recording off skipped the session row and stored every command.
    add_command = store[store.index("def add_command"):store.index("def search")]
    check("history.record is checked before a command is stored",
          "_recording_enabled()" in add_command,
          "recording off still wrote every command and its output")

    # history.retention_days: a reader with no caller.
    check("history.retention_days is actually applied",
          "_retention_days()" in store and store.count("_retention_days") >= 2,
          "declared, given a reader, and never called — so 'Discard history "
          "after' discarded nothing")
    check("and there is something that deletes",
          "DELETE FROM commands WHERE ran_at" in store)

    terminal = (root / "frontend" / "js" / "terminal.js").read_text(encoding="utf-8")
    mouseup = terminal[terminal.index("addEventListener('mouseup'"):]
    check("copy_on_select is checked where the copying happens",
          "copy_on_select" in mouseup[:900],
          "xterm was told the setting and this handler copied anyway")

    check("paste chunking cannot be scheduled with a zero delay",
          "Math.max(A('terminal.paste_chunk_delay'" in terminal,
          "index * 0 fires every chunk at once, which is the one outcome "
          "chunking exists to prevent")
    check("and chunks are split on bytes, as the setting is named",
          "TextEncoder" in terminal,
          "splitting by character sent up to 3x the intended bytes per chunk")

    app = (root / "backend" / "app.py").read_text(encoding="utf-8")
    check("the broadcast timeout message quotes the configured limit",
          "BROADCAST_MAX_SECONDS" not in app,
          "a sequence abandoned at 600s reported 180")


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
        test_algorithm_settings,
        test_algorithms_reach_paramiko,
        test_every_setting_is_actually_read,
        test_what_is_deliberately_absent,
        test_describe_is_renderable,
        test_a_setting_that_says_it_does_something_does_it,
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
