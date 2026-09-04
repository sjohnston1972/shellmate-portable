"""
test_parsed.py — Show output as rows, and only when that is honest.

The parser is optional, local and silent: it must never raise into a chat,
never invent rows for a command it has no template for, and never replace
the raw text. These check the seams — the platform map, the "no template"
path, the rendering, and that the tables reach the prompt and the
auto-analysis message in the right shape.

    python test_parsed.py
"""

import shutil
import sys
import tempfile
from pathlib import Path

from backend import paths

_TEMP = Path(tempfile.mkdtemp(prefix="shellmate-parsed-"))
paths._data_dir_cache = _TEMP

from backend.session import parsed                              # noqa: E402
from backend.session.transcript import CommandRecord            # noqa: E402

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


BRIEF = """Interface              IP-Address      OK? Method Status                Protocol
GigabitEthernet0/1     10.1.1.1        YES NVRAM  up                    up
GigabitEthernet0/2     unassigned      YES unset  administratively down down
"""


XR_BRIEF = """Interface                      IP-Address      Status                Protocol Vrf-Name
MgmtEth0/RSP0/CPU0/0           10.0.0.1        Up                    Up       default
TenGigE0/0/0/0                 10.1.1.1        Up                    Up       default
TenGigE0/0/0/1                 unassigned      Shutdown              Down     default
"""


def _ntc_names() -> set:
    """Every platform name the installed ntc-templates index knows."""
    import os

    import ntc_templates
    index = os.path.join(os.path.dirname(ntc_templates.__file__), "templates", "index")
    names = set()
    with open(index, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("Template,"):
                continue
            parts = [part.strip() for part in line.split(",")]
            if len(parts) >= 3:
                names.add(parts[2])
    return names


def test_parsing() -> None:
    print("\n-- Parsing --")
    check("ntc-templates is installed", parsed.available())
    rows = parsed.parse("ios", "show ip interface brief", BRIEF)
    check("IOS interface brief parses to rows", isinstance(rows, list) and len(rows) == 2, str(rows))
    check("  with the columns a person would expect",
          rows and rows[1].get("interface") == "GigabitEthernet0/2"
          and "down" in rows[1].get("status", ""), str(rows))
    check("a command with no template is None, not an error",
          parsed.parse("ios", "show nothing at all", "text") is None)
    check("an unknown platform is None", parsed.parse("mystery", "show version", "x") is None)
    check("empty output is None", parsed.parse("ios", "show version", "") is None)
    check("every ShellMate platform maps to an ntc name",
          set(parsed.NTC_PLATFORMS) >= {"ios", "nxos", "asa", "junos", "panos", "arista",
                                        "linux", "iosxr", "fortios", "routeros",
                                        "huawei", "aoscx"})
    check("and every name it maps to is one ntc-templates actually has",
          _ntc_names() >= set(parsed.NTC_PLATFORMS.values()),
          f"unknown: {sorted(set(parsed.NTC_PLATFORMS.values()) - _ntc_names())}")

    # Huawei is the one where the obvious name is the wrong one: the bare
    # `huawei` prefix belongs to the OLT templates, and VRP is `huawei_vrp`.
    xr = parsed.parse("iosxr", "show ip interface brief", XR_BRIEF)
    check("an IOS-XR interface brief parses through the cisco_xr templates",
          isinstance(xr, list) and any(r.get("interface", "").startswith("TenGigE")
                                       for r in xr), str(xr))


def test_rendering() -> None:
    print("\n-- Rendering --")
    rows = parsed.parse("ios", "show ip interface brief", BRIEF) or []
    text = parsed.render("show ip interface brief", rows)
    check("names the command and the row count", "show ip interface brief — 2 rows" in text, text)
    check("columns are headed", "interface" in text.splitlines()[1], text)
    check("rows are there", "GigabitEthernet0/2" in text, text)
    many = [{"a": str(i), "b": ""} for i in range(100)]
    cut = parsed.render("show many", many, max_rows=10)
    check("long tables are cut with a note", "90 more rows not shown" in cut, cut)
    check("empty columns are dropped", " b" not in cut.splitlines()[1], cut)
    check("zero rows is stated, not blank", "0 rows" in parsed.render("show arp", []))


def test_tables_for_records() -> None:
    print("\n-- From recent records --")
    records = [
        CommandRecord(command="show made-up thing", output="% Invalid input"),
        CommandRecord(command="show ip interface brief", output=BRIEF),
        CommandRecord(command="conf t", output=""),
    ]
    tables = parsed.tables_for("ios", records)
    check("only the commands with a template become tables",
          len(tables) == 1 and "show ip interface brief" in tables[0], str(tables))
    check("no platform, no tables", parsed.tables_for("", records) == [])
    check("a cap on how many", len(parsed.tables_for("ios", [records[1]] * 6, limit=2)) == 2)


SNMP = """Community name: s3cr3t-rw
Community Index: cisco0
Community SecurityName: s3cr3t-rw
storage-type: nonvolatile        active
"""


def configure(redact_secrets: bool) -> None:
    from backend import settings_store
    settings_store.update_settings({"logging": {"redact_secrets": redact_secrets}})


def test_rows_are_redacted() -> None:
    """A table is another way out of the machine, so it goes through the same door (#496)."""
    print("\n-- Through redaction --")
    from backend.session import outbound
    record = CommandRecord(command="show snmp community", output=SNMP)
    check("the fixture parses at all", parsed.parse("ios", "show snmp community", SNMP), "no template?")

    configure(redact_secrets=True)
    check("redaction is on for the test", outbound.redaction_enabled())
    tables = parsed.tables_for("ios", [record])
    check("a community string does not reach the table",
          tables and "s3cr3t-rw" not in tables[0], str(tables))
    check("  and the mask is there in its place", tables and "********" in tables[0], str(tables))

    configure(redact_secrets=False)
    tables = parsed.tables_for("ios", [record])
    check("with redaction off the value is shown, and the memo did not serve the masked one",
          tables and "s3cr3t-rw" in tables[0], str(tables))
    configure(redact_secrets=True)


def test_tables_are_memoised() -> None:
    print("\n-- Parsed once --")
    configure(redact_secrets=True)
    record = CommandRecord(command="show ip interface brief", output=BRIEF)
    calls = []
    real = parsed.parse

    def counting(platform_id, command, output):
        calls.append(command)
        return real(platform_id, command, output)

    parsed.parse = counting
    try:
        first = parsed.tables_for("ios", [record])
        second = parsed.tables_for("ios", [record])
        check("the same record is parsed once across two questions",
              len(calls) == 1 and first == second, str(calls))
        check("a record with no template is remembered as such too",
              parsed.tables_for("ios", [CommandRecord(command="conf t", output="x")] * 3) == []
              and len(calls) == 2, str(calls))
        parsed.tables_for("nxos", [record])
        check("a different platform is parsed again", len(calls) == 3, str(calls))
    finally:
        parsed.parse = real


def test_reaches_the_prompt() -> None:
    print("\n-- In the prompt --")
    from backend.ai.prompts import build_context_prompt
    prompt = build_context_prompt([], "raw", "sw1", [], parsed_tables=["--- Parsed: show x — 1 row ---\n  a"])
    check("tables sit after the raw output and say the raw text wins",
          "Structured view" in prompt and prompt.index("raw") < prompt.index("Structured view")
          and "authoritative" in prompt, prompt)
    check("no tables, no section", "Structured view" not in build_context_prompt([], "raw", "sw1", []))

    inv = build_context_prompt([], "raw", "sw1", [], investigation={"step": 7, "max": 8})
    check("an investigation states its budget", "7 of a budget of 8" in inv and "One step left" in inv, inv)
    spent = build_context_prompt([], "raw", "sw1", [], investigation={"step": 8, "max": 8})
    check("a spent budget says to conclude", "conclude now" in spent, spent)

    from backend.app import _auto_analysis_prompt
    msg = _auto_analysis_prompt({"command": "show ip interface brief", "output": BRIEF}, "ios")
    check("auto-analysis carries the parsed rows too",
          "Parsed: show ip interface brief" in msg and "GigabitEthernet0/2" in msg, msg[:300])
    plain = _auto_analysis_prompt({"command": "show ip interface brief", "output": BRIEF}, "")
    check("  but not without a platform", "Parsed:" not in plain)


def main() -> int:
    print("=" * 52)
    print("  Structured output")
    print("=" * 52)
    for test in (test_parsing, test_rendering, test_tables_for_records,
                 test_rows_are_redacted, test_tables_are_memoised, test_reaches_the_prompt):
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
