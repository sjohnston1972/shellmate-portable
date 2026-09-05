"""
test_chat_tables.py — Real tables and proper Markdown in the chat (#554).

Two halves.

**The renderer.** `formatText` handled code, bold and line breaks; headings,
lists and — the point — tables arrived as prose with the pipes still in
them. `markdown.js` has rendered all of that for the manual since the
manual existed, and escapes before producing any markup, which is what
makes it safe to point at model output. Reusing it is a deletion, not an
addition.

**The rows.** A 48-port interface table is the example the assistant
documentation gives of what models get wrong; read as line-wrapped prose
the engineer gets it wrong too. The model keeps the fixed-width text and
the browser gets columns — from *one* parse, because two would be two
chances to disagree about what the device said.

The property that matters most is the one a pre-existing test caught while
this was being written: the rows are redacted. They go to the browser,
which already has the unmasked terminal on screen — but the chat panel is a
different surface, and a conversation gets exported, sent to Jira and
pasted into tickets. A clean table is exactly where a masked value would
come back unmasked.

    python test_chat_tables.py
"""

import shutil
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

from backend import paths

_TEMP = Path(tempfile.mkdtemp(prefix="shellmate-chat-tables-"))
paths._data_dir_cache = _TEMP

from backend import settings_store                          # noqa: E402
from backend.session import parsed                          # noqa: E402

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


def record(command, output):
    return SimpleNamespace(command=command, output=output, prompt="sw1#",
                           started_at=time.time(), duration_ms=5)


# A real IOS interface table, which is the case the whole feature is for.
# The platform id is ShellMate's own — "ios", mapped to ntc-templates'
# name in NTC_PLATFORMS. "cisco_ios" parses nothing, silently.
BRIEF = """Interface              IP-Address      OK? Method Status                Protocol
GigabitEthernet0/1     10.0.0.1        YES manual up                    up      
GigabitEthernet0/2     unassigned      YES unset  administratively down down    
GigabitEthernet0/10    unassigned      YES unset  up                    up      
"""

SECRET_OUTPUT = """Interface              IP-Address      OK? Method Status                Protocol
GigabitEthernet0/1     10.0.0.1        YES manual up                    up      
username admin password 7 070C285F4D06485744  YES unset  up            up      
"""


# ---------------------------------------------------------------------------

def test_rows_come_back_as_columns() -> None:
    print("\n-- Columns, not prose --")

    if not parsed.available():
        check("TextFSM templates are available to test against", False,
              "ntc-templates is not installed; the rows cannot be checked")
        return

    out = parsed.rows_for("ios", [record("show ip interface brief", BRIEF)])
    check("a table comes back", len(out) == 1, str(out))
    if not out:
        return

    table = out[0]
    check("it names the command it came from",
          table["command"] == "show ip interface brief", str(table["command"]))
    check("it has columns", len(table["columns"]) >= 3, str(table["columns"]))
    check("and a row per interface", len(table["rows"]) == 3, str(table["rows"]))
    check("every row has one cell per column",
          all(len(r) == len(table["columns"]) for r in table["rows"]),
          "a ragged row puts a value under the wrong heading")
    check("the values are strings, ready for the browser",
          all(isinstance(c, str) for r in table["rows"] for c in r))


def test_the_columns_come_from_one_row_not_the_union() -> None:
    """
    Taking the union of every row's keys would put a column at the end
    whenever one row happened to carry a field the others did not — which
    is a table whose headings move depending on the data.
    """
    print("\n-- Stable columns --")

    if not parsed.available():
        check("skipped: no templates", True)
        return

    out = parsed.rows_for("ios", [record("show ip interface brief", BRIEF)])
    if not out:
        check("skipped: nothing parsed", True)
        return
    check("the column order is the first row's key order",
          out[0]["columns"] == list(
              parsed.parse("ios", "show ip interface brief", BRIEF)[0].keys()),
          str(out[0]["columns"]))


def test_the_rows_are_redacted() -> None:
    """
    The property a pre-existing test caught while this was being written.

    These go to the browser, which already has the unmasked terminal on
    screen. But the chat panel is a different surface — a conversation is
    exported, sent to Jira, pasted into a ticket — and a clean table is
    exactly where a masked value would come back unmasked.
    """
    print("\n-- Masked on the way out --")
    settings_store.update_settings({"logging": {"redact_secrets": True}})

    if not parsed.available():
        check("skipped: no templates", True)
        return

    out = parsed.rows_for("ios",
                          [record("show ip interface brief", SECRET_OUTPUT)])
    blob = str(out)
    check("no secret survives into the rows",
          "070C285F4D06485744" not in blob, blob[:200])


def test_nothing_that_does_not_parse_becomes_a_table() -> None:
    print("\n-- Only what parses --")

    out = parsed.rows_for("ios", [record("show clock", "12:04:22.123 UTC")])
    check("output with no template produces no table", out == [], str(out))

    check("no records at all is not an error",
          parsed.rows_for("ios", []) == [])
    check("nor is None",
          parsed.rows_for("ios", None) == [])


def test_the_limit_holds() -> None:
    print("\n-- Bounded --")

    if not parsed.available():
        check("skipped: no templates", True)
        return

    many = [record("show ip interface brief", BRIEF) for _ in range(10)]
    out = parsed.rows_for("ios", many, limit=3)
    check("at most the limit comes back", len(out) <= 3, str(len(out)))


def test_the_browser_half_is_wired() -> None:
    print("\n-- The browser half --")
    root = Path(__file__).parent
    chat = (root / "frontend" / "js" / "chat.js").read_text(encoding="utf-8")
    md = (root / "frontend" / "js" / "markdown.js").read_text(encoding="utf-8")

    check("formatText defers to the real renderer",
          "window.shellmateMarkdown.render(text)" in chat,
          "headings, lists and tables were arriving as prose with the pipes "
          "still in them")
    check("and falls back rather than losing a reply",
          "must" in chat and "return text" in chat,
          "a renderer that threw on one odd reply must not lose it")

    check("the renderer escapes before producing markup",
          "escapeHtml" in md and "inline(escapeHtml(" in md,
          "this is what makes it safe to point at model output")

    check("the socket branch for tables exists",
          "msg.type === 'tables'" in chat)
    check("tables attach after the text",
          "attachTables(streamingBubble)" in chat,
          "a table above the first sentence pushes the answer off a short "
          "panel")
    check("cells are set as text, never as markup",
          "td.textContent = cell" in chat,
          "an interface description is somebody else's text on somebody "
          "else's box")
    check("there is a filter and a CSV copy",
          "chat-table-filter" in chat and "copyCsv" in chat)
    check("the CSV quotes values containing commas",
          'replace(/"/g' in chat,
          "an interface description with a comma would silently become two "
          "columns wherever it was pasted")
    check("sorting is numeric where the column is numbers",
          "Number.isNaN(Number(v))" in chat,
          "sorted as text, 10 comes before 9 — wrong for exactly the "
          "columns anybody sorts")
    check("a truncated table says so",
          "spec.truncated" in chat,
          "a table quietly showing sixty of four hundred rows is one "
          "somebody will count on being complete")


def main() -> int:
    print("=" * 52)
    print("  Tables and Markdown in the chat")
    print("=" * 52)

    for test in (
        test_rows_come_back_as_columns,
        test_the_columns_come_from_one_row_not_the_union,
        test_the_rows_are_redacted,
        test_nothing_that_does_not_parse_becomes_a_table,
        test_the_limit_holds,
        test_the_browser_half_is_wired,
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
