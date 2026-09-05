"""
test_report.py — The reports somebody else reads (#540, #574).

A report leaves the machine. That single fact is what these tests attack:
everything here is about what must not survive the trip, and what must not
be able to break the document on the way.

Three failure shapes, in the order they would hurt:

**A secret in the file.** The whole point of ``outbound.redact_text`` is
that a running configuration carries hashes, keys and community strings,
and a report is far more "handed to someone else" than a log on disk. A
report that leaks is worse than no report, because it was written to be
forwarded.

**Device output escaping into the markup.** Device output contains angle
brackets, ampersands, backticks and lines beginning with a hash. Every one
of those is HTML or Markdown syntax, and a banner containing three
backticks that closes a code fence early does not fail — it silently
reformats the rest of the report as prose.

**A reference to something not in the file.** The HTML is opened from a
folder, on a machine that may be air-gapped. Anything fetched renders as
unstyled text exactly when it matters.

    python test_report.py
"""

import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

from backend import paths

_TEMP = Path(tempfile.mkdtemp(prefix="shellmate-report-"))
paths._data_dir_cache = _TEMP

from backend import report                                 # noqa: E402
from backend import settings_store                         # noqa: E402

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


# The secret is a real Cisco type-7 line, because that is what redact() is
# written to recognise and a made-up token would test the test rather than
# the redaction.
SECRET_LINE = "username admin password 7 070C285F4D06485744"
COMMUNITY = "snmp-server community S3cr3tC0mmun1ty RO"


def a_session() -> dict:
    """A stored session with everything a report reads."""
    now = time.time()
    return {
        "id": "sess-1",
        "label": "core-sw-01",
        "hostname": "core-sw-01",
        "connection_type": "ssh",
        "username": "steven",
        "target": "10.0.0.1:22",
        "started_at": now - 600,
        "ended_at": now,
        "notes": "Replacing the uplink SFP on Gi1/0/49.",
        "commands": [
            {
                "sequence": 1,
                "command": "show running-config | include username",
                "output": SECRET_LINE + "\n" + COMMUNITY,
                "ran_at": now - 500,
                "duration_ms": 240,
            },
            {
                "sequence": 2,
                # Every hostile character in one banner: markup, entities,
                # a fence, and a Markdown heading at the start of a line.
                "command": "show banner motd",
                "output": (
                    "<script>alert('x')</script>\n"
                    "Tom & Jerry < 5 > 3\n"
                    "``` not the end of the block\n"
                    "# Not a heading\n"
                ),
                "ran_at": now - 300,
                "duration_ms": 15,
            },
        ],
    }


def setup_redaction(on: bool) -> None:
    settings_store.update_settings({"logging": {"redact_secrets": on}})


# ---------------------------------------------------------------------------

def test_a_secret_does_not_reach_the_file() -> None:
    """The first thing a report must not do."""
    print("\n-- Redaction --")
    setup_redaction(True)
    title, blocks = report.session_report(a_session())

    markdown = report.to_markdown(blocks)
    page = report.to_html(title, blocks)

    check("the type-7 password is not in the Markdown",
          "070C285F4D06485744" not in markdown,
          "the redaction did not run on command output")
    check("the type-7 password is not in the HTML",
          "070C285F4D06485744" not in page)
    check("the community string is not in the Markdown",
          "S3cr3tC0mmun1ty" not in markdown)
    check("the community string is not in the HTML",
          "S3cr3tC0mmun1ty" not in page)

    # The line itself should still be there — redacted, not deleted. A report
    # that drops the line entirely hides that a credential was configured
    # at all, which is itself the finding somebody may be reporting.
    check("the line survives, masked rather than removed",
          "username admin password" in markdown,
          "redaction removed the line instead of masking the value")


def test_the_session_report_carries_what_it_claims() -> None:
    print("\n-- The session report --")
    setup_redaction(True)
    session = a_session()
    title, blocks = report.session_report(
        session,
        chat=[{"role": "user", "text": "why is the uplink flapping?"},
              {"role": "ai", "text": "The SFP is reporting low Rx power."}],
        summary="An SFP was replaced on Gi1/0/49.",
    )
    markdown = report.to_markdown(blocks)

    check("the title names the device", "core-sw-01" in title, title)
    check("the metadata carries the operator", "steven" in markdown)
    check("the notes are included",
          "Replacing the uplink SFP" in markdown)
    check("the commands are numbered and present",
          "1. show running-config" in markdown and "2. show banner" in markdown)
    check("the assistant conversation is included",
          "low Rx power" in markdown)
    check("the summary is attributed, not stated as fact",
          "Written by the ShellMate assistant" in markdown,
          "an unattributed AI summary reads as device evidence")

    empty = dict(session, commands=[])
    _, blocks = report.session_report(empty)
    check("a session with no commands says so",
          "No commands were recorded" in report.to_markdown(blocks))


def test_device_output_cannot_escape_the_html() -> None:
    print("\n-- HTML escaping --")
    setup_redaction(True)
    title, blocks = report.session_report(a_session())
    page = report.to_html(title, blocks)

    check("a script tag arrives escaped",
          "<script>" not in page and "&lt;script&gt;" in page,
          "device output reached the page as live markup")
    check("a bare ampersand arrives escaped",
          "Tom &amp; Jerry" in page)
    check("angle brackets in output are escaped",
          "&lt; 5 &gt; 3" in page)

    # The only markup this module generates is its own bold labels, and the
    # substitution runs after escaping — so it can never promote device text.
    _, chat_blocks = report.session_report(
        a_session(), chat=[{"role": "user", "text": "<b>hello</b>"}])
    chat_page = report.to_html("t", chat_blocks)
    check("markup inside a chat message stays inert",
          "<b>hello</b>" not in chat_page and "&lt;b&gt;hello" in chat_page)


def test_backticks_in_output_cannot_break_the_fence() -> None:
    print("\n-- Markdown fencing --")
    setup_redaction(True)
    title, blocks = report.session_report(a_session())
    markdown = report.to_markdown(blocks)

    # Find the block holding the banner and confirm its fence is longer than
    # anything inside it, so the run of backticks in the output cannot close
    # it early and spill the rest of the report out as prose.
    check("a fence longer than three is used when output contains one",
          "````" in markdown,
          "the fence was not widened for output containing ```")

    # Walk it the way a Markdown parser does: a fence closes only on a line
    # of at least as many backticks as opened it, carrying nothing else. A
    # naive count of lines starting with backticks would flag the ``` inside
    # the banner — which is the very thing the widened fence makes harmless.
    depth_open = 0
    banner_inside = False
    for line in markdown.splitlines():
        run = len(line) - len(line.lstrip("`"))
        if depth_open:
            if run >= depth_open and not line[run:].strip():
                depth_open = 0
                continue
            if "not the end of the block" in line:
                banner_inside = True
        elif run >= 3:
            depth_open = run

    check("every fence is closed", depth_open == 0,
          "a code block was left open, so the rest of the report is prose")
    check("a run of backticks in output stays inside its block",
          banner_inside,
          "device output closed the fence early and spilled into the document")

    plain = report._fence("no backticks here")
    check("plain output still gets a three-backtick fence", plain == "```", plain)
    check("output with a four-run gets five",
          report._fence("a ```` b") == "`````", report._fence("a ```` b"))


def test_the_html_is_self_contained() -> None:
    print("\n-- Self-contained --")
    setup_redaction(True)
    title, blocks = report.session_report(a_session())
    page = report.to_html(title, blocks)

    check("no external reference of any kind",
          "http://" not in page and "https://" not in page,
          "the page fetches something it will not have on an air-gapped machine")
    check("no external stylesheet or script tag",
          "<link" not in page and "<script" not in page)
    check("it declares itself a document", page.startswith("<!doctype html>"))
    check("the styles are inline", "<style>" in page)


def test_the_diff_report() -> None:
    print("\n-- The diff report --")
    setup_redaction(True)
    diff = {
        "diff": ("--- core-sw-01 @ 2026-09-01\n"
                 "+++ core-sw-01 @ 2026-09-05\n"
                 "@@ -1,4 +1,4 @@\n"
                 " interface Gi1/0/49\n"
                 "-description old uplink\n"
                 "+description new uplink\n"),
        "added": 1, "removed": 1, "changed": 2,
    }
    old = {"hostname": "core-sw-01", "captured_at": time.time() - 86400}
    new = {"hostname": "core-sw-01", "captured_at": time.time()}

    title, blocks = report.diff_report(diff, old, new)
    markdown = report.to_markdown(blocks)
    page = report.to_html(title, blocks)

    check("the counts are reported", "Lines changed" in markdown and "2" in markdown)
    check("the diff is fenced as a diff", "```diff" in markdown)
    check("added lines are marked in the HTML", 'class="df-add"' in page)
    check("removed lines are marked in the HTML", 'class="df-del"' in page)
    check("the hunk header is marked", 'class="df-hunk"' in page)

    identical, _ = None, None
    _, same_blocks = report.diff_report({"diff": "", "added": 0, "removed": 0,
                                         "changed": 0}, old, new)
    check("an empty diff says the configurations match",
          "identical" in report.to_markdown(same_blocks))


def test_the_change_report_distinguishes_two_kinds_of_nothing() -> None:
    """
    "No difference" and "nothing to compare" are different facts.

    A change board reading the first when the second is true is being told
    the change had no effect, when what actually happened is that nobody
    captured a configuration to measure against.
    """
    print("\n-- The change record --")
    setup_redaction(True)
    session = a_session()
    before = {"hostname": "core-sw-01", "captured_at": time.time() - 600}
    after = {"hostname": "core-sw-01", "captured_at": time.time()}

    _, no_snapshots = report.change_report(session, None, None, None)
    text = report.to_markdown(no_snapshots)
    check("with no snapshots it says a comparison is unavailable",
          "snapshot was not captured" in text, text[-400:])

    _, no_change = report.change_report(
        session, before, after, {"diff": "", "changed": 0})
    text = report.to_markdown(no_change)
    check("with snapshots and no diff it says they are identical",
          "identical before and after" in text, text[-400:])

    _, changed = report.change_report(
        session, before, after,
        {"diff": "-old\n+new", "added": 1, "removed": 1, "changed": 2})
    text = report.to_markdown(changed)
    check("the commands typed are in the change record",
          "show banner motd" in text)
    check("the notes explain what the change was for",
          "uplink SFP" in text)


def test_long_output_is_cut_and_says_so() -> None:
    print("\n-- Truncation --")
    setup_redaction(True)
    session = a_session()
    session["commands"] = [{
        "sequence": 1, "command": "show tech-support",
        "output": "\n".join(f"line {n}" for n in range(2000)),
        "ran_at": time.time(), "duration_ms": 9000,
    }]
    markdown = report.to_markdown(report.session_report(session)[1])

    check("the output is cut", "line 1999" not in markdown)
    check("the cut is announced rather than silent",
          "more line(s) not included" in markdown,
          "a truncated report that does not say so is a wrong report")
    check("what was kept is still there", "line 10" in markdown)


def test_writing_a_file() -> None:
    print("\n-- Writing --")
    setup_redaction(True)
    title, blocks = report.session_report(a_session())

    md_path = report.write(title, blocks, "core-sw-01", "md")
    html_path = report.write(title, blocks, "core-sw-01", "html")

    check("the Markdown file exists", md_path.exists())
    check("the HTML file exists", html_path.exists())
    check("both are under the reports folder",
          md_path.parent == paths.reports_dir()
          and html_path.parent == paths.reports_dir(),
          str(md_path.parent))
    check("the filename carries the device", "core-sw-01" in md_path.name)
    check("the extensions are right",
          md_path.suffix == ".md" and html_path.suffix == ".html")
    check("the written file holds no secret",
          "070C285F4D06485744" not in md_path.read_text(encoding="utf-8"))

    # A hostname is not a filename. Path separators and colons in a device
    # label must not be able to steer the write out of the reports folder.
    hostile = report.write(title, blocks, "../../etc/pass wd:1", "md")
    check("a hostile device name cannot escape the folder",
          hostile.parent == paths.reports_dir(), str(hostile))
    check("the slug keeps only safe characters",
          re.fullmatch(r"[A-Za-z0-9._-]+", hostile.name) is not None,
          hostile.name)

    bad = False
    try:
        report.write(title, blocks, "x", "pdf")
    except ValueError:
        bad = True
    check("an unknown format is refused rather than guessed", bad)


def test_redaction_off_is_honoured() -> None:
    """
    The switch has to actually be a switch.

    Somebody running a local model on their own machine may deliberately
    turn masking off; a redactor that ignores the setting would make the
    setting a lie in the other direction.
    """
    print("\n-- The switch --")
    setup_redaction(False)
    markdown = report.to_markdown(report.session_report(a_session())[1])
    check("with redaction off the secret is present",
          "070C285F4D06485744" in markdown,
          "the setting was ignored")
    setup_redaction(True)


def main() -> int:
    print("=" * 52)
    print("  Reports — what leaves the machine")
    print("=" * 52)

    for test in (
        test_a_secret_does_not_reach_the_file,
        test_the_session_report_carries_what_it_claims,
        test_device_output_cannot_escape_the_html,
        test_backticks_in_output_cannot_break_the_fence,
        test_the_html_is_self_contained,
        test_the_diff_report,
        test_the_change_report_distinguishes_two_kinds_of_nothing,
        test_long_output_is_cut_and_says_so,
        test_writing_a_file,
        test_redaction_off_is_honoured,
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
