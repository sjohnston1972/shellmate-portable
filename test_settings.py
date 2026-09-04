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


def test_a_relative_path_means_the_data_folder() -> None:
    """
    Where the file picker opens when a field holds `logs` or `configs`.

    Those names are stored relative on purpose — it is what lets a ShellMate
    folder be copied to another machine and still work — and ``settings_store``
    resolves them against the data directory. The browse endpoint resolved them
    against the *process working directory*, which for a double-clicked
    executable is wherever Explorer happened to leave us. So "browse from
    configs" opened somewhere unrelated, then fell back to the home directory,
    discarding the only thing the caller actually knew.
    """
    print("\n-- What a relative path in a settings field means --")
    from backend.app import _nearest_existing, _resolve_local

    data = paths.data_dir()

    check("a relative name resolves against the data folder",
          _resolve_local("configs") == data / "configs",
          f"got {_resolve_local('configs')}")
    check("and matches what the application itself uses",
          _resolve_local("logs") == settings_store.log_directory(),
          f"{_resolve_local('logs')} != {settings_store.log_directory()}")
    check("an absolute path is used exactly as given",
          _resolve_local("D:/captures") == Path("D:/captures"))

    unc = _resolve_local("//nas/configs")
    check("including a path to a share", unc.is_absolute(), str(unc))

    missing = data / "definitely-not-here" / "nor-here"
    check("a folder that does not exist yet falls back to the nearest that does",
          _nearest_existing(missing) == data,
          f"got {_nearest_existing(missing)}")


def test_browsing_names_the_folder_that_is_missing() -> None:
    """
    Browsing from `configs` before anything has been captured.

    Falling back silently is what happened before, and it throws away the one
    piece of information the caller had: which folder they were after.
    """
    print("\n-- Browsing somewhere that does not exist yet --")
    from fastapi.testclient import TestClient

    from backend.app import app

    client = TestClient(app, base_url="http://127.0.0.1")
    target = "a-folder-that-is-not-there"

    body = client.get(f"/api/local/browse?path={target}").json()
    check("it lists the folder above rather than the home directory",
          body["path"] == str(paths.data_dir().resolve()), body["path"])
    check("and names the part that is missing",
          body.get("missing", "").endswith(target), repr(body.get("missing")))

    (paths.data_dir() / "logs").mkdir(parents=True, exist_ok=True)
    body = client.get("/api/local/browse?path=logs").json()
    check("a folder that does exist reports nothing missing",
          not body.get("missing"), repr(body.get("missing")))
    check("and is what gets listed",
          body["path"] == str((paths.data_dir() / "logs").resolve()), body["path"])


def test_creating_a_missing_folder() -> None:
    """
    The offer attached to that message.

    One level, never a tree: parents=True would turn a mistyped path into a
    row of empty folders somebody then has to find and remove.
    """
    print("\n-- Creating it --")
    from fastapi.testclient import TestClient

    from backend.app import app

    client = TestClient(app, base_url="http://127.0.0.1")
    made = paths.data_dir() / "created-by-a-test"
    shutil.rmtree(made, ignore_errors=True)

    try:
        first = client.post("/api/local/mkdir", json={"path": "created-by-a-test"})
        check("it is created",
              first.status_code == 200 and first.json()["created"] is True,
              first.text)
        check("where the application would look for it", made.is_dir())

        again = client.post("/api/local/mkdir", json={"path": "created-by-a-test"})
        check("asking twice is not an error",
              again.status_code == 200 and again.json()["created"] is False,
              again.text)

        deep = client.post("/api/local/mkdir", json={"path": "no-such/deeper/still"})
        check("a whole missing tree is refused rather than built",
              deep.status_code == 400, deep.text)
        check("and nothing is left behind",
              not (paths.data_dir() / "no-such").exists())
    finally:
        shutil.rmtree(made, ignore_errors=True)


def test_every_tab_menu_entry_has_its_own_setting() -> None:
    """
    One row in Settings per tab-menu entry, and no two writing to one key.

    The Settings rows are generated from `tabMenuItems()`, which is the right
    shape — a new menu entry appears there without anything being added by
    hand. What that pattern cannot guarantee on its own is that the keys are
    unique, and two entries once shared `keep_alive`. That renders two
    checkboxes carrying the same `data-menu-setting`, and `_collectTabMenu()`
    walks them in DOM order writing into one key — so the first was silently
    overwritten by the second and could not be turned off on its own (#209).

    Read out of the source, for the same reason test_icons.py reads the font:
    the failure is invisible at runtime, and the list is the only place the
    truth lives.
    """
    print("\n-- Tab menu entries reach Settings --")
    import re

    source = (Path(__file__).parent / "frontend" / "js" / "tabs.js") \
        .read_text(encoding="utf-8")

    block = re.search(r"const TAB_MENU_GROUPS = \[(.*?)\n  \];", source, re.S)
    check("the menu definition is findable", block is not None,
          "TAB_MENU_GROUPS moved or was renamed — this test is now blind")
    if not block:
        return

    sections = re.search(r"const TAB_MENU_SECTIONS = \[(.*?)\n  \];", source, re.S)
    section_text = sections.group(1) if sections else ""

    # The two lists are counted separately on purpose: a SECTIONS entry is a
    # whole block of the menu — "Move to pane" — and carries a setting without
    # an action, because there is no single thing to invoke.
    row_settings = re.findall(r"setting:\s*'([a-z0-9_]+)'", block.group(1))
    section_settings = re.findall(r"setting:\s*'([a-z0-9_]+)'", section_text)
    settings = row_settings + section_settings

    check("entries were found to check", len(settings) > 8,
          f"only {len(settings)} — the pattern is probably not matching")

    duplicates = sorted({s for s in settings if settings.count(s) > 1})
    check("no two entries share a setting", not duplicates,
          f"{', '.join(duplicates)} appears twice — the later checkbox "
          f"overwrites the earlier one and the first row does nothing")

    # Every row has a setting, or it renders in the menu with no way to turn
    # it off.
    actions = re.findall(r"action:\s*'([a-z0-9-]+)'", block.group(1))
    check("every menu row carries a setting",
          len(actions) == len(row_settings),
          f"{len(actions)} actions but {len(row_settings)} settings — one has "
          f"no Settings row and cannot be switched off")

    check("every section carries a setting too", section_settings,
          "a section with no setting cannot be hidden from Settings")

    # And the handler exists, or the row is a button that does nothing.
    handled = set(re.findall(r"case '([a-z0-9-]+)':", source))
    unhandled = sorted(a for a in actions if a not in handled and a != 'pane')
    check("every entry has a handler", not unhandled,
          f"no case for: {', '.join(unhandled)}")


def test_logging_can_be_decided_per_session() -> None:
    """
    One tab's own answer, beside the global switch (#534).

    Logging was one switch for every open session, so "start logging this
    one now" — ten minutes into a change on the core — meant starting a file
    for eleven other tabs as well. The override is resolved in the same place
    the global is, because two places deciding one thing is how a tab ends up
    saying "logging" while nothing is being written.
    """
    print("\n-- Logging, per session --")
    from backend.app import _open_session_log

    before = settings_store.get_settings().get("logging", {}).get("enabled")
    try:
        settings_store.update_settings({"logging": {"enabled": False}})

        session: dict = {"hostname": "core-sw"}
        _open_session_log(session, "abcd1234efgh")
        check("with the setting off and no override, nothing is written",
              session["_log_path"] is None, repr(session.get("_log_path")))

        session["log_override"] = True
        _open_session_log(session, "abcd1234efgh")
        path = session["_log_path"]
        check("this tab can say yes on its own",
              path is not None and path.name == "abcd1234-core-sw.log",
              repr(path))
        check("into the log folder, like any other session log",
              path is not None and path.parent == settings_store.log_directory(),
              repr(path))

        settings_store.update_settings({"logging": {"enabled": True}})
        session["log_override"] = False
        _open_session_log(session, "abcd1234efgh")
        check("and it can say no while the setting says yes",
              session["_log_path"] is None, repr(session.get("_log_path")))

        session["log_override"] = None
        _open_session_log(session, "abcd1234efgh")
        check("no answer of its own follows the setting",
              session["_log_path"] is not None)
    finally:
        settings_store.update_settings({"logging": {"enabled": bool(before)}})


def test_saving_a_scrollback() -> None:
    """
    What is already on screen, written to a file (#534).

    The half that matters is the redaction. This writes device output to a
    file whose whole purpose is to be handed to somebody else, so it goes
    through the one switch that covers session logs and captured
    configurations — and it is done on the server, because the browser is
    holding the raw text and must not be the thing that saves it.
    """
    print("\n-- Saving a scrollback --")
    from fastapi.testclient import TestClient

    from backend.app import app

    client = TestClient(app, base_url="http://127.0.0.1")
    typed = "core-sw#show run\n username neteng password 7 05080F1C2243\n"

    before = settings_store.get_settings().get("logging", {}).get("redact_secrets")
    try:
        settings_store.update_settings({"logging": {"redact_secrets": True}})
        # No native window in a test, so the prepared text comes back for the
        # browser to save rather than a folder dialog being raised.
        answer = client.post("/api/scrollback/save",
                             json={"filename": 'core sw:1', "text": typed})
        check("it answers", answer.status_code == 200, answer.text)
        body = answer.json()
        check("with a name a filesystem will accept",
              body.get("filename") == "core sw-1.log", repr(body.get("filename")))
        check("and the secret masked",
              "05080F1C2243" not in body.get("text", ""), body.get("text"))
        check("while the line itself is kept",
              "username neteng" in body.get("text", ""), body.get("text"))

        settings_store.update_settings({"logging": {"redact_secrets": False}})
        raw = client.post("/api/scrollback/save",
                          json={"filename": "core-sw", "text": typed}).json()
        check("switched off, it is the text as it was",
              "05080F1C2243" in raw.get("text", ""), raw.get("text"))
    finally:
        settings_store.update_settings(
            {"logging": {"redact_secrets": True if before is None else bool(before)}})


def test_the_ansible_token_lives_in_the_vault() -> None:
    """
    The runner's bearer token behaves like an API key, not like a setting.

    Three things have to hold at once, and each has bitten a secret field
    somewhere before: it never reaches settings.json, the page is never
    handed the real value back, and a save that did not touch it leaves it
    alone. The last one is what makes the field usable — otherwise changing
    a timeout means retyping a token nobody can read.
    """
    from backend import ansible, settings_store

    settings_store.update_settings({"ansible": {"runner_url": "https://runner:8081",
                                                "token": "s3cret-token"}})

    on_disk = (paths.data_dir() / "settings.json").read_text(encoding="utf-8")
    check("the token is not written to settings.json",
          "s3cret-token" not in on_disk,
          "the file carries the token in plain text")

    shown = settings_store.get_settings_for_ui()["ansible"]
    check("the page is told a token is stored", shown["has_token"] is True,
          str(shown))
    check("but never given it",
          "s3cret-token" not in str(shown) and shown["token"] != "s3cret-token",
          str(shown))

    check("the runner client can still read it",
          ansible.config().token == "s3cret-token",
          "the token went into the vault and never came back out")

    settings_store.update_settings({"ansible": {"runner_url": "https://runner:8081",
                                               "timeout": 45}})
    check("a save that did not mention the token keeps it",
          ansible.config().token == "s3cret-token",
          "changing a timeout wiped the token")

    settings_store.update_settings({"ansible": {"token": ""}})
    check("clearing it removes it rather than leaving it quietly in place",
          not ansible.config().token,
          "an emptied field left the old token stored")


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
        test_a_relative_path_means_the_data_folder,
        test_browsing_names_the_folder_that_is_missing,
        test_creating_a_missing_folder,
        test_every_tab_menu_entry_has_its_own_setting,
        test_logging_can_be_decided_per_session,
        test_saving_a_scrollback,
        test_the_ansible_token_lives_in_the_vault,
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
