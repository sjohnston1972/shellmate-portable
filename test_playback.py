"""
test_playback.py — A session as a page that replays itself (#574).

This file is *built to be sent* — mailed to a vendor, put on a share, opened
by somebody who does not have ShellMate. That makes it the most exposed
thing ShellMate writes, and the tests are shaped by the three ways it could
betray that.

**It could carry a secret.** Same rule as every report: redacted on the way
in, and asserted here rather than assumed.

**Device output could break out of the script tag.** The commands go into a
`<script>` as JSON, and JSON has no opinion about the document it is
embedded in — the characters that spell a closing script tag are ordinary
text to it. So are U+2028 and U+2029, which JSON treats as whitespace and
JavaScript treats as line terminators, making a page that parses
differently from the JSON it was built from. Both are escaped; both are
tested with output that actually contains them.

**It could need the network.** A page that fetches renders as a blank box
on the air-gapped machine where somebody most needs to watch it.

    python test_playback.py
"""

import json
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

from backend import paths

_TEMP = Path(tempfile.mkdtemp(prefix="shellmate-playback-"))
paths._data_dir_cache = _TEMP

from backend import playback, settings_store                # noqa: E402

passed = 0
failed: list[str] = []

SECRET_LINE = "username admin password 7 070C285F4D06485744"
LS = chr(0x2028)
PS = chr(0x2029)


def check(name: str, condition: bool, detail: str = "") -> None:
    global passed
    if condition:
        passed += 1
        print(f"  OK  {name}")
    else:
        failed.append(f"{name}: {detail}")
        print(f"  FAIL {name}\n       {detail}")


def a_session() -> dict:
    now = time.time()
    return {
        "id": "play-1",
        "label": "core-sw-01",
        "hostname": "core-sw-01",
        "connection_type": "ssh",
        "username": "steven",
        "target": "10.0.0.1:22",
        "started_at": now - 900,
        "ended_at": now,
        "notes": "Chasing the uplink flap.",
        "commands": [
            {"prompt": "core-sw-01#", "command": "show run | inc username",
             "output": SECRET_LINE, "ran_at": now - 800, "duration_ms": 210},
            {"prompt": "core-sw-01#", "command": "show banner motd",
             # Everything that could end the script block or the string
             # literal it lives in, in one banner.
             "output": ("</script><script>alert(1)</script>\n"
                        "Tom & Jerry < 5 > 3\n"
                        + LS + PS + "after the separators\n"),
             "ran_at": now - 600, "duration_ms": 40},
        ],
    }


def setup_redaction(on: bool) -> None:
    settings_store.update_settings({"logging": {"redact_secrets": on}})


# ---------------------------------------------------------------------------

def test_the_payload_cannot_escape_its_script_tag() -> None:
    """The one that would turn a session into markup on somebody's screen."""
    print("\n-- The payload --")
    setup_redaction(True)
    hostile = "</script><b>x</b>&amp;" + LS + PS + "tail"
    out = playback._payload([{
        "command": "show run", "output": hostile,
        "ran_at": 1.0, "duration_ms": 2,
    }])

    check("no closing script tag survives", "</script>" not in out)
    check("no raw angle bracket at all",
          "<" not in out and ">" not in out,
          "a device could spell any tag it liked")
    check("no raw ampersand", "&" not in out)
    check("neither line separator survives",
          LS not in out and PS not in out,
          "JavaScript would treat these as statement terminators")
    check("and it still round-trips to exactly the original",
          json.loads(out)[0]["output"] == hostile,
          "the escaping changed the content it was protecting")


def test_the_page_is_self_contained() -> None:
    print("\n-- Self-contained --")
    setup_redaction(True)
    title, page = playback.build(a_session())

    check("the title names the device", "core-sw-01" in title, title)
    # Not "no URL appears anywhere": xterm.js carries two attribution URLs
    # in its own licence header, and a comment is not a request. What has to
    # be true is that nothing in the document *fetches* — which is a
    # property of elements and CSS, not of the characters h-t-t-p.
    check("no element references anything external",
          " src=" not in page and " href=" not in page,
          "an inlined page must not link out")
    check("no stylesheet import or remote font",
          "@import" not in page and "url(http" not in page)
    check("nothing calls fetch or opens a request at load",
          "fetch(" not in page and "XMLHttpRequest" not in page,
          "a page that asks the network renders blank where it matters")
    check("xterm.js is embedded", "Terminal" in page and len(page) > 200_000,
          f"page is {len(page):,} bytes — the emulator is missing")
    check("the xterm stylesheet is embedded", ".xterm" in page)
    check("it declares itself a document", page.startswith("<!doctype html>"))


def test_the_controls_are_there() -> None:
    """The issue asks for the same controls, so it is the same session."""
    print("\n-- The controls --")
    setup_redaction(True)
    page = playback.build(a_session())[1]

    for control in ("play", "pause", "stop", "speed", "seek"):
        check(f"there is a {control} control", f'id="{control}"' in page)
    check("the gap cap matches the application's 60 seconds",
          "MAX_GAP" not in page and ", 60)" in page,
          "the placeholder was not substituted, or the cap changed")


def test_a_secret_does_not_reach_the_page() -> None:
    print("\n-- Redaction --")
    setup_redaction(True)
    page = playback.build(a_session())[1]
    check("the type-7 password is not in the playback",
          "070C285F4D06485744" not in page)
    check("the command that found it still is",
          "show run | inc username" in page)

    text = playback.transcript(a_session())
    check("nor in the transcript", "070C285F4D06485744" not in text)
    check("the line survives, masked rather than removed",
          "username admin password" in text)


def test_the_transcript_is_actually_plain() -> None:
    """
    Its whole reason to exist is being paste-able.

    Markdown would arrive in a vendor's mail client as literal asterisks and
    backticks, which is worse than no formatting at all.
    """
    print("\n-- The transcript --")
    setup_redaction(True)
    text = playback.transcript(a_session())

    check("no code fences", "```" not in text)
    # Not "no ** anywhere", and not a lazy pair either — the redaction mask
    # is a run of eight asterisks, which a pattern allowing asterisks in the
    # middle happily matches as bold. Flagging that would be testing the
    # masking rather than the formatting. Bold needs non-asterisk content.
    check("no bold-wrapped text",
          not re.search(r"\*\*[^*]+\*\*", text),
          "Markdown bold would arrive as literal asterisks in a mail client")
    check("no markdown list-and-label rows",
          not re.search(r"^- \*\*", text, re.M))
    check("no markdown headings",
          not re.search(r"^#{1,6} ", text, re.M))
    check("the device is named", "core-sw-01" in text)
    check("the notes are there", "Chasing the uplink flap" in text)
    check("the commands are there",
          "show run | inc username" in text and "show banner motd" in text)
    check("it says where it came from",
          "Exported from ShellMate Portable" in text)

    empty = dict(a_session(), commands=[])
    check("a session with no commands says so",
          "No commands were recorded" in playback.transcript(empty))


def test_an_empty_session_still_produces_a_page() -> None:
    """
    A session with nothing in it is a page that says so, not a broken one.

    The player would otherwise set the seek bar's max to -1 and divide by an
    empty span, which fails silently as a page that does nothing when Play
    is pressed.
    """
    print("\n-- Nothing to replay --")
    setup_redaction(True)
    page = playback.build(dict(a_session(), commands=[]))[1]
    check("the page is still produced", page.startswith("<!doctype html>"))
    check("and it says there is nothing to play",
          "recorded no commands" in page)


def test_writing_the_files() -> None:
    print("\n-- Writing --")
    setup_redaction(True)
    session = a_session()

    page_path = playback.write(session, "html")
    text_path = playback.write(session, "txt")

    check("the playback exists", page_path.exists())
    check("the transcript exists", text_path.exists())
    check("both are in the reports folder",
          page_path.parent == paths.reports_dir()
          and text_path.parent == paths.reports_dir())
    check("the names say what they are",
          "playback" in page_path.name and "transcript" in text_path.name,
          f"{page_path.name} / {text_path.name}")
    check("neither carries the secret",
          "070C285F4D06485744" not in page_path.read_text(encoding="utf-8")
          and "070C285F4D06485744" not in text_path.read_text(encoding="utf-8"))

    hostile = dict(session, label="../../etc/pass wd:1")
    escaped = playback.write(hostile, "txt")
    check("a hostile device name cannot escape the folder",
          escaped.parent == paths.reports_dir(), str(escaped))

    bad = False
    try:
        playback.write(session, "pdf")
    except ValueError:
        bad = True
    check("an unknown format is refused rather than guessed", bad)


def test_the_routes() -> None:
    print("\n-- The routes --")
    setup_redaction(True)

    from types import SimpleNamespace

    from fastapi.testclient import TestClient

    from backend import store as store_module
    from backend.app import app

    store = store_module.store
    session_id = "playback-route-1"
    store.start_session(session_id, {
        "label": "core-sw-01", "hostname": "core-sw-01",
        "connection_type": "ssh", "username": "steven",
    })
    store.add_command(session_id, SimpleNamespace(
        command="show version", output="Cisco IOS Software",
        prompt="core-sw-01#", started_at=time.time(), duration_ms=90))
    store.end_session(session_id)
    store.flush()

    client = TestClient(app, base_url="http://127.0.0.1")

    res = client.post("/api/reports", json={"kind": "playback",
                                            "session_id": session_id})
    check("a playback is written", res.status_code == 200,
          f"HTTP {res.status_code}: {res.text[:160]}")
    if res.status_code == 200:
        body = res.json()
        check("it is an html file", body["name"].endswith(".html"), body["name"])
        check("and a large one, because the emulator is inside it",
              body["bytes"] > 200_000, str(body["bytes"]))

    res = client.post("/api/reports", json={"kind": "transcript",
                                            "session_id": session_id})
    check("a transcript is written", res.status_code == 200,
          f"HTTP {res.status_code}: {res.text[:160]}")
    if res.status_code == 200:
        check("it is a txt file", res.json()["name"].endswith(".txt"))

    res = client.post("/api/reports", json={"kind": "playback",
                                            "session_id": "no-such-session"})
    check("an unknown session is a 404", res.status_code == 404,
          f"HTTP {res.status_code}")

    store.delete_session(session_id)


def main() -> int:
    print("=" * 52)
    print("  Playback — a session somebody else can watch")
    print("=" * 52)

    for test in (
        test_the_payload_cannot_escape_its_script_tag,
        test_the_page_is_self_contained,
        test_the_controls_are_there,
        test_a_secret_does_not_reach_the_page,
        test_the_transcript_is_actually_plain,
        test_an_empty_session_still_produces_a_page,
        test_writing_the_files,
        test_the_routes,
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
