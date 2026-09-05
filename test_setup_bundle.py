"""
test_setup_bundle.py — Taking a setup somewhere else (#563).

The manual lists every file in ShellMate-Data and says three of them are
meant to be handed to a colleague. Until now that was the whole story.

Four properties, and the first is the one the whole design follows from:

**Nothing secret is in a bundle.** Not the vault, not the plaintext
credential file, not an API key, not a device password. A bundle is a file
people mail to each other. Tested by putting a secret in every place one
could be and reading the zip back.

**A round trip reproduces the setup.** Export from one folder, import into
an empty one, compare.

**Merge does not discard your corrections.** Incoming loses ties, because
somebody importing a colleague's setup has their own fixes in these files.
Profiles merge on `identity()`, or #73's duplicates come straight back.

**The move never deletes.** Copy, then point, and the original is left
exactly where it was — the disk space is a far smaller problem than a
failed copy nobody noticed until the next launch.

    python test_setup_bundle.py
"""

import io
import json
import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

from backend import paths

_TEMP = Path(tempfile.mkdtemp(prefix="shellmate-setup-"))
paths._data_dir_cache = _TEMP

from backend import setup_bundle, settings_store              # noqa: E402
from backend.vault import vault                               # noqa: E402

passed = 0
failed: list[str] = []

ROOT = Path(__file__).parent
APP = (ROOT / "backend" / "app.py").read_text(encoding="utf-8")
PATHS = (ROOT / "backend" / "paths.py").read_text(encoding="utf-8")
PANEL = (ROOT / "frontend" / "js" / "setup.js").read_text(encoding="utf-8")
HTML = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")


def check(name: str, condition: bool, detail: str = "") -> None:
    global passed
    if condition:
        passed += 1
        print(f"  OK  {name}")
    else:
        failed.append(f"{name}: {detail}")
        print(f"  FAIL {name}\n       {detail}")


SECRET = "hunter2-do-not-ship"


def seed() -> None:
    """A setup with a secret in every place one could be."""
    settings_store.update_settings({
        "providers": {"anthropic_api_key": SECRET, "ollama_host": "http://o:11434"},
        "ansible": {"token": SECRET, "runner_url": "https://runner:5001"},
        "backups": {"webhook_url": SECRET},
        "interface": {"theme": "light"},
    })
    (_TEMP / "profiles.json").write_text(json.dumps([
        {"id": "p1", "name": "core-1", "hostname": "10.0.0.1", "port": 22,
         "username": "neteng", "connection_type": "ssh"},
        {"id": "p2", "name": "edge-1", "hostname": "10.0.0.2", "port": 22,
         "username": "neteng", "connection_type": "ssh"},
    ]), encoding="utf-8")
    (_TEMP / "groups.json").write_text(json.dumps(
        [{"id": "g1", "key": "glasgow", "name": "Glasgow"}]), encoding="utf-8")
    (_TEMP / "credential-sets.json").write_text(json.dumps(
        [{"id": "c1", "name": "Ops", "username": "ops"}]), encoding="utf-8")
    (_TEMP / "snippets.json").write_text(json.dumps(
        [{"id": "s1", "name": "ints", "commands": ["show ip int br"]}]),
        encoding="utf-8")
    vault.set("device:p1:password", SECRET)
    (_TEMP / "credentials-plaintext.json").write_text(
        json.dumps({"p1": {"password": SECRET}}), encoding="utf-8")


# ---------------------------------------------------------------------------

def test_nothing_secret_is_in_a_bundle() -> None:
    print("\n-- Nothing secret --")
    seed()

    blob, manifest = setup_bundle.export()
    archive = zipfile.ZipFile(io.BytesIO(blob))
    names = archive.namelist()
    everything = b"".join(archive.read(n) for n in names)

    check("the secret is nowhere in the zip",
          SECRET.encode() not in everything,
          "a bundle is a file people mail to each other")
    for never in setup_bundle.NEVER:
        check(f"  {never} is not in it", never not in names, str(names))
    check("the credential sets travel as names and usernames",
          b'"ops"' in archive.read("credential-sets.json"))
    check("the provider key was blanked, not the whole settings file",
          b'"ollama_host": "http://o:11434"' in archive.read("settings.json"))
    check("and so were the section secrets",
          b'"token": ""' in archive.read("settings.json")
          and b'"webhook_url": ""' in archive.read("settings.json"))

    check("there is a manifest with a checksum per file",
          all(entry.get("sha256") for entry in manifest["parts"]))
    check("and a README for whoever opens the zip by hand",
          "README.txt" in names
          and b"Saved passwords do not travel" in archive.read("README.txt"),
          "a bundle with no explanation is one somebody unpacks over their "
          "data folder by hand")
    check("the licence is not included unless asked for",
          "licence-state.json" not in names)


def test_a_round_trip() -> None:
    print("\n-- Round trip --")
    seed()
    blob, _ = setup_bundle.export()

    # Into an empty folder.
    other = Path(tempfile.mkdtemp(prefix="shellmate-setup-b-"))
    paths._data_dir_cache = other
    settings_store.invalidate()
    try:
        preview = setup_bundle.inspect(blob)
        profiles = next(p for p in preview["parts"] if p["key"] == "profiles")
        check("the preview counts", profiles["count"] == 2, str(profiles))
        check("and says none overlap, because there is nothing here",
              profiles["overlap"] == 0, str(profiles))
        check("and that the checksums hold",
              all(p["checksum_ok"] for p in preview["parts"]
                  if p["checksum_ok"] is not None))

        out = setup_bundle.apply(blob, {p["key"]: "replace"
                                        for p in preview["parts"]})
        check("everything was applied",
              {a["key"] for a in out["applied"]} >= {"profiles", "groups",
                                                      "settings", "snippets"},
              str(out))
        check("the profiles are there",
              json.loads((other / "profiles.json").read_text())[0]["name"]
              == "core-1")
        check("the theme came across",
              settings_store.get_settings()["interface"]["theme"] == "light")
        check("and no secret did",
              SECRET not in (other / "settings.json").read_text()
              and not (other / "vault.json").exists())
    finally:
        paths._data_dir_cache = _TEMP
        settings_store.invalidate()
        shutil.rmtree(other, ignore_errors=True)


def test_merge_keeps_your_corrections() -> None:
    """
    Incoming loses ties. Somebody importing a colleague's setup has their
    own fixes in these files.
    """
    print("\n-- Merge --")
    seed()
    blob, _ = setup_bundle.export()

    # Now change what is here, and add something.
    mine = json.loads((_TEMP / "profiles.json").read_text())
    mine[0]["name"] = "core-1 (corrected)"
    mine.append({"id": "p9", "name": "new-9", "hostname": "10.0.0.9",
                 "port": 22, "username": "neteng", "connection_type": "ssh"})
    (_TEMP / "profiles.json").write_text(json.dumps(mine), encoding="utf-8")

    preview = setup_bundle.inspect(blob)
    profiles = next(p for p in preview["parts"] if p["key"] == "profiles")
    check("the preview says how many are already here",
          profiles["overlap"] == 2, str(profiles))

    setup_bundle.apply(blob, {"profiles": "merge"})
    after = json.loads((_TEMP / "profiles.json").read_text())
    check("nothing was duplicated", len(after) == 3, str([p["name"] for p in after]))
    check("my correction survived",
          any(p["name"] == "core-1 (corrected)" for p in after),
          "replace is there for when they want that, and it says so")
    check("and my addition", any(p["id"] == "p9" for p in after))

    check("merge is refused for a document that is not a list",
          _raises(lambda: setup_bundle.apply(blob, {"settings": "merge"}),
                  setup_bundle.BundleError),
          "offering a merge that silently means replace is worse than not "
          "offering it")
    check("and the panel does not offer it either",
          "part.mergeable" in PANEL)


def test_what_is_refused() -> None:
    print("\n-- Refused --")

    check("not a zip",
          _raises(lambda: setup_bundle.inspect(b"hello"), setup_bundle.BundleError))

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        z.writestr("something.txt", "x")
    check("a zip with no manifest",
          _raises(lambda: setup_bundle.inspect(buffer.getvalue()),
                  setup_bundle.BundleError))

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        z.writestr("manifest.json", json.dumps({"format": 99, "parts": []}))
    check("a bundle from a newer ShellMate",
          _raises(lambda: setup_bundle.inspect(buffer.getvalue()),
                  setup_bundle.BundleError),
          "importing it here could drop settings this version does not "
          "know about")

    check("something far too large to be a bundle",
          _raises(lambda: setup_bundle.inspect(
              b"\0" * (setup_bundle.MAX_BUNDLE_BYTES + 1)),
              setup_bundle.BundleError),
          "the zip is read entirely in memory")

    check("import is refused while sessions are open",
          "Close your sessions first" in APP.split('@app.post("/api/setup/apply")')[1][:1800],
          "mirroring updater.blockers: replacing state underneath a live "
          "connection")


def test_moving_the_data_folder() -> None:
    print("\n-- Move --")
    seed()

    target = Path(tempfile.mkdtemp(prefix="shellmate-moved-")) / "ShellMate-Data"
    plan = setup_bundle.move_plan(str(target))
    check("a plan says where from and to",
          plan["ok"] and plan["from"] == str(_TEMP), str(plan))

    inside = setup_bundle.move_plan(str(_TEMP / "sub"))
    check("moving into itself is refused",
          not inside["ok"] and "into itself" in " ".join(inside["problems"]),
          "the first thing anybody tries is dragging the current folder "
          "onto the picker")
    same = setup_bundle.move_plan(str(_TEMP))
    check("and so is moving to where it already is", not same["ok"])

    # A fake app dir for the pointer, since app_dir() is the checkout.
    real_app_dir = paths.app_dir
    fake_app = Path(tempfile.mkdtemp(prefix="shellmate-app-"))
    paths.app_dir = lambda: fake_app
    try:
        class Manager:
            def get_all_sessions(self):
                return [{"display_label": "core-1"}]

        check("refused while a session is open",
              _raises(lambda: setup_bundle.move_data_dir(str(target), Manager()),
                      setup_bundle.BundleError))

        out = setup_bundle.move_data_dir(str(target), None)
        check("it copies", (target / "profiles.json").exists(), str(out))
        check("it writes the pointer",
              (fake_app / paths.DATA_DIR_POINTER).read_text(encoding="utf-8")
              .strip() == str(target.resolve()))
        check("the original is left where it was",
              (_TEMP / "profiles.json").exists(),
              "the one operation with no way back, on the folder holding "
              "everything")
        check("and the response says so",
              "left exactly as it was" in out["note"], out["note"])
        check("a restart is called for", out["restart_required"] is True)

        # The pointer is honoured on the next resolution.
        paths._data_dir_cache = None
        check("the next launch resolves to the new folder",
              paths.data_dir() == target.resolve(), str(paths.data_dir()))
        check("and does not count as the fallback",
              paths.data_dir_is_fallback() is False)

        # A pointer to somewhere unusable is ignored, with a warning.
        (fake_app / paths.DATA_DIR_POINTER).write_text(
            "Z:\\no\\such\\drive\\at\\all" if os.name == "nt" else "/proc/nope",
            encoding="utf-8")
        paths._data_dir_cache = None
        resolved = paths.data_dir()
        check("a pointer to nowhere falls back rather than refusing to start",
              resolved != Path("Z:/no/such/drive/at/all") and resolved.exists(),
              str(resolved))
    finally:
        paths.app_dir = real_app_dir
        paths._data_dir_cache = _TEMP
        settings_store.invalidate()
        shutil.rmtree(target.parent, ignore_errors=True)
        shutil.rmtree(fake_app, ignore_errors=True)

    check("the environment override is documented as winning",
          'DATA_DIR_ENV = "SHELLMATE_DATA_DIR"' in PATHS
          and "It wins over the pointer" in PATHS)
    check("and resolved in one place only",
          PATHS.count("= _override_dir()") == 1,
          "two answers to 'where is my data' is a stick somebody believes "
          "is carrying their setup and is not")


def test_the_panel_says_the_dpapi_sentence() -> None:
    print("\n-- The sentence --")

    section = HTML.split("Backup and transfer</h3>")[1].split("</section>")[0]
    check("the export says passwords are not in it",
          "not in a bundle and cannot be" in section)
    check("and that a DPAPI vault cannot be moved at all",
          "sealed to this Windows account it cannot be moved" in section,
          "somebody who exports their setup and finds their credentials "
          "gone has been failed by a missing sentence")
    check("what is never included comes from the server's list",
          "data.never" in PANEL,
          "so the promise and the code that keeps it cannot drift")

    check("the import previews before it applies",
          "inspectBundle" in PANEL and "Nothing has been changed yet" in PANEL)
    check("merge is the default for lists",
          "['merge', 'Add what is missing']" in PANEL
          and PANEL.index("['merge'") < PANEL.index("['replace'"),
          "replace as the default would quietly discard their corrections")
    check("replacements are named as replacements in the confirmation",
          "'replaces yours'" in PANEL)

    check("the move says nothing is deleted",
          "nothing is deleted" in PANEL and "data-dir.txt" in PANEL)
    check("it is loaded", 'src="/static/js/setup.js"' in HTML)

    for control in ("setup-move-target",):
        tip = HTML.split(f'for="{control}"')[1].split("</label>")[0]
        check(f"{control}'s tooltip has both halves", tip.count("||") == 1)


def _raises(fn, exc_type) -> bool:
    try:
        fn()
    except exc_type:
        return True
    except Exception:
        return False
    return False


def main() -> int:
    print("=" * 52)
    print("  Export, import and move")
    print("=" * 52)

    for test in (
        test_nothing_secret_is_in_a_bundle,
        test_a_round_trip,
        test_merge_keeps_your_corrections,
        test_what_is_refused,
        test_moving_the_data_folder,
        test_the_panel_says_the_dpapi_sentence,
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
