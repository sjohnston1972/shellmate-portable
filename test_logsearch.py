"""
test_logsearch.py — Searching inside the session logs (#576).

The Logs panel could list files and open one. This searches across all of
them, which introduces three failures that listing never had, and each one
fails quietly rather than loudly:

**An unbounded read.** A log folder nobody has pruned holds gigabytes. A
search with no bound holds the request open reading all of it and the panel
looks hung — so there is a byte bound and a hit bound, and both have to be
*reported*, because a search that stopped early without saying so makes
"no matches" indistinguishable from "I did not look".

**A half-written pattern.** People compose regular expressions in the box,
so an invalid one is the field's normal state, not a fault. It has to come
back as a sentence.

**An off-by-one on the date range.** "Until the 5th" that excludes the 5th
silently omits the day somebody is looking for, and the search looks like
it found nothing.

    python test_logsearch.py
"""

import shutil
import sys
import tempfile
import time
from pathlib import Path

from backend import paths

_TEMP = Path(tempfile.mkdtemp(prefix="shellmate-logsearch-"))
paths._data_dir_cache = _TEMP

from backend import advanced, logsearch, settings_store       # noqa: E402

passed = 0
failed: list[str] = []

LOGS = _TEMP / "logs"


def check(name: str, condition: bool, detail: str = "") -> None:
    global passed
    if condition:
        passed += 1
        print(f"  OK  {name}")
    else:
        failed.append(f"{name}: {detail}")
        print(f"  FAIL {name}\n       {detail}")


def write_log(name: str, text: str, days_ago: float = 0) -> Path:
    LOGS.mkdir(parents=True, exist_ok=True)
    path = LOGS / name
    path.write_text(text, encoding="utf-8")
    if days_ago:
        when = time.time() - days_ago * 86400
        import os

        os.utime(path, (when, when))
    return path


def seed() -> None:
    shutil.rmtree(LOGS, ignore_errors=True)
    write_log("core-sw-01.log",
              "interface GigabitEthernet1/0/1\n"
              "  description UPLINK to core\n"
              "  shutdown\n"
              "line 4 mentions Uplink again\n", days_ago=0)
    write_log("edge-fw-02.log",
              "no uplink here at all\n"
              "just a firewall log\n", days_ago=3)
    write_log("old-rtr-03.log",
              "UPLINK\nUPLINK\nUPLINK\n", days_ago=30)


# ---------------------------------------------------------------------------

def test_it_finds_lines_not_just_files() -> None:
    """
    A search that only names files sends somebody back to reading one at a
    time, which is where they started.
    """
    print("\n-- Finding --")
    seed()
    out = logsearch.search(LOGS, "uplink")

    check("three files matched", len(out["files"]) == 3,
          str([f["filename"] for f in out["files"]]))
    check("the total is every occurrence", out["hits"] == 6, str(out["hits"]))

    top = out["files"][0]
    check("the file with the most hits is first",
          top["filename"] == "old-rtr-03.log", top["filename"])
    check("each match carries its line number and its text",
          top["matches"][0]["line"] == 1 and "UPLINK" in top["matches"][0]["text"],
          str(top["matches"][0]))

    core = next(f for f in out["files"] if f["filename"] == "core-sw-01.log")
    check("line numbers are the file's own",
          [m["line"] for m in core["matches"]] == [2, 4],
          str([m["line"] for m in core["matches"]]))


def test_the_switches() -> None:
    print("\n-- Case, regex and whole word --")
    seed()

    check("case-insensitive by default",
          logsearch.search(LOGS, "uplink")["hits"] == 6)
    check("matching case narrows it",
          logsearch.search(LOGS, "uplink", case=True)["hits"] == 1,
          str(logsearch.search(LOGS, "uplink", case=True)["hits"]))

    check("a literal query is not read as a pattern",
          logsearch.search(LOGS, "1/0/1")["hits"] == 1,
          "the slashes and dot were treated as regex")

    out = logsearch.search(LOGS, r"Gigabit\w+", regex=True)
    check("a regular expression matches", out["hits"] == 1, str(out["hits"]))

    check("whole word does not match inside a word",
          logsearch.search(LOGS, "link", whole_word=True)["hits"] == 0,
          "'link' matched inside 'uplink'")
    check("but does match the word itself",
          logsearch.search(LOGS, "uplink", whole_word=True)["hits"] == 6)


def test_a_bad_pattern_is_a_sentence() -> None:
    print("\n-- A pattern being typed --")
    seed()

    for query in ("[unclosed", "a{2,1}", "(?P<"):
        raised = ""
        try:
            logsearch.search(LOGS, query, regex=True)
        except logsearch.SearchError as exc:
            raised = str(exc)
        check(f"{query!r} is refused with a reason",
              bool(raised) and "valid pattern" in raised, raised or "no error")

    # An empty query is not an error. "Show me the logs from that Tuesday"
    # is a whole question, and substituting a match-everything pattern
    # would report every line of every file as a hit — a number nobody
    # asked for, arriving where a result goes.
    out = logsearch.search(LOGS, "   ")
    check("an empty query lists rather than refusing",
          len(out["files"]) == 3, str(out))
    check("and reports no hits, because nothing was searched for",
          out["hits"] == 0 and all(not f["matches"] for f in out["files"]),
          str(out["hits"]))
    check("the listing is newest first",
          [f["filename"] for f in out["files"]]
          == ["core-sw-01.log", "edge-fw-02.log", "old-rtr-03.log"],
          str([f["filename"] for f in out["files"]]))


def test_the_date_range() -> None:
    print("\n-- When --")
    seed()
    from datetime import date, timedelta

    today = date.today()
    yesterday = (today - timedelta(days=1)).isoformat()
    week_ago = (today - timedelta(days=7)).isoformat()

    out = logsearch.search(LOGS, "uplink", since=yesterday)
    check("since excludes anything older",
          [f["filename"] for f in out["files"]] == ["core-sw-01.log"],
          str([f["filename"] for f in out["files"]]))

    out = logsearch.search(LOGS, "uplink", since=week_ago)
    check("a wider window takes more in", len(out["files"]) == 2,
          str([f["filename"] for f in out["files"]]))

    # The off-by-one: "until today" must include today's file, which was
    # modified at some hour of today rather than at midnight.
    out = logsearch.search(LOGS, "uplink", until=today.isoformat())
    check("until includes the whole of the named day",
          any(f["filename"] == "core-sw-01.log" for f in out["files"]),
          "today's log was excluded from a range ending today")

    bad = ""
    try:
        logsearch.search(LOGS, "uplink", since="5th September")
    except logsearch.SearchError as exc:
        bad = str(exc)
    check("a malformed date says the format", "YYYY-MM-DD" in bad, bad)

    backwards = ""
    try:
        logsearch.search(LOGS, "uplink", since=today.isoformat(),
                         until=week_ago)
    except logsearch.SearchError as exc:
        backwards = str(exc)
    check("a range that runs backwards is refused",
          "after its end" in backwards, backwards)


def test_the_bounds_are_reported() -> None:
    """
    A search that stopped early without saying so makes "no matches" and
    "I did not look" the same answer.
    """
    print("\n-- Bounds --")
    shutil.rmtree(LOGS, ignore_errors=True)
    # Well past the byte bound set below. 20,000 lines is about 240 KB;
    # the bound's own floor is 100 KB, so a smaller fixture would never
    # reach it and this would pass by not testing anything.
    write_log("huge.log", ("filler line" + chr(10)) * 20_000
              + "NEEDLE at the end" + chr(10))

    settings_store.update_settings({"advanced": {"logs.search_max_bytes": 100_000}})
    out = logsearch.search(LOGS, "filler")
    check("the byte bound stops the read",
          out["files"][0]["truncated"] is True,
          "a bounded read did not report itself")
    check("and the search says so at the top level",
          out["truncated"] is True)
    check("the needle past the bound is genuinely not found",
          logsearch.search(LOGS, "NEEDLE")["hits"] == 0,
          "the bound was not applied")

    settings_store.update_settings({"advanced": {"logs.search_max_bytes": 200_000_000,
                                                 "logs.search_max_hits": 25}})
    out = logsearch.search(LOGS, "filler")
    hit_file = out["files"][0]
    check("the hit bound caps the lines carried back",
          len(hit_file["matches"]) == 25, str(len(hit_file["matches"])))
    check("but the count is still the true one",
          hit_file["hits"] == 20_000, str(hit_file["hits"]))
    check("and the capping is reported", hit_file["capped"] is True)

    settings_store.update_settings({"advanced": {}})


def test_a_line_with_no_newline_in_a_mile_of_it() -> None:
    """
    A device streaming a progress bar writes a log with almost no newlines.

    Reading that line by line is one enormous string in memory; the chunked
    read is what stops it, and the long match still has to be cut for a list.
    """
    print("\n-- One very long line --")
    shutil.rmtree(LOGS, ignore_errors=True)
    write_log("progress.log", "x" * 900_000 + "NEEDLE" + "y" * 900_000)

    out = logsearch.search(LOGS, "NEEDLE")
    check("it is still found", out["hits"] == 1, str(out["hits"]))
    check("the line shown is cut to something a list can hold",
          len(out["files"][0]["matches"][0]["text"]) <= logsearch.MAX_LINE + 2,
          str(len(out["files"][0]["matches"][0]["text"])))


def test_an_unreadable_file_does_not_take_the_search_with_it() -> None:
    print("\n-- One bad file --")
    shutil.rmtree(LOGS, ignore_errors=True)
    write_log("good.log", "uplink is here\n")
    # Not valid UTF-8. errors="replace" means this is read rather than
    # raising — which is the behaviour we want, since a log with one bad
    # byte still has every other line worth searching.
    (LOGS / "binary.log").write_bytes(b"\xff\xfe uplink \x00 raw\n")

    out = logsearch.search(LOGS, "uplink")
    check("the good file is still searched",
          any(f["filename"] == "good.log" for f in out["files"]),
          str([f["filename"] for f in out["files"]]))
    check("and the undecodable one is read rather than skipped",
          any(f["filename"] == "binary.log" for f in out["files"]),
          "one bad byte should not lose every line in the file")


def test_no_logs_at_all() -> None:
    print("\n-- Nothing to search --")
    shutil.rmtree(LOGS, ignore_errors=True)
    out = logsearch.search(LOGS, "anything")
    check("an absent folder is an empty result, not an error",
          out["files"] == [] and out["hits"] == 0, str(out))


def test_the_route() -> None:
    print("\n-- The route --")
    seed()

    from fastapi.testclient import TestClient

    from backend.app import app

    client = TestClient(app, base_url="http://127.0.0.1")

    res = client.get("/api/logs/search", params={"q": "uplink"})
    check("the route resolves rather than being read as a filename",
          res.status_code == 200,
          f"HTTP {res.status_code}: {res.text[:140]}")
    if res.status_code == 200:
        check("and it returns the files", len(res.json()["files"]) == 3,
              str(res.json().get("files")))

    res = client.get("/api/logs/search",
                     params={"q": "[unclosed", "regex": "true"})
    check("a bad pattern is a 400 with the reason, not a 500",
          res.status_code == 400, f"HTTP {res.status_code}")
    check("and the reason is readable",
          "valid pattern" in res.json().get("detail", ""),
          res.text[:140])

    from datetime import date, timedelta

    res = client.get("/api/logs/search", params={
        "q": "", "since": (date.today() - timedelta(days=1)).isoformat()})
    check("dates alone filter the listing rather than erroring",
          res.status_code == 200
          and [f["filename"] for f in res.json()["files"]] == ["core-sw-01.log"],
          f"HTTP {res.status_code}: {res.text[:140]}")


def main() -> int:
    print("=" * 52)
    print("  Log search — finding the session you half remember")
    print("=" * 52)

    for test in (
        test_it_finds_lines_not_just_files,
        test_the_switches,
        test_a_bad_pattern_is_a_sentence,
        test_the_date_range,
        test_the_bounds_are_reported,
        test_a_line_with_no_newline_in_a_mile_of_it,
        test_an_unreadable_file_does_not_take_the_search_with_it,
        test_no_logs_at_all,
        test_the_route,
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
