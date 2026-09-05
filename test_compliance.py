"""
test_compliance.py — Did the standard land everywhere? (#543)

The check reads stored snapshots rather than logging in, which is what
makes it instant and also what makes its answer subtle. Three things are
easy to get wrong here and each one sends somebody to the wrong place:

**Three states, not two.** `compliant`, `missing` and `never-captured`.
Folding the third into "not compliant" sends an engineer to fix a device
that may be perfectly configured; folding it into "compliant" is worse
still, because it reports a device as verified that nobody has looked at.

**The age of the evidence is part of the verdict.** "Compliant" against a
capture from six weeks ago is a statement about six weeks ago, and a row
that omits that invites exactly the wrong conclusion.

**A mixed group needs a block per platform.** Running the IOS AAA lines
against a firewall reports every line missing, which is not a finding — it
is the check being asked the wrong question.

    python test_compliance.py
"""

import shutil
import sys
import tempfile
import time
from pathlib import Path

from backend import paths

_TEMP = Path(tempfile.mkdtemp(prefix="shellmate-compliance-"))
paths._data_dir_cache = _TEMP

from backend import compliance                             # noqa: E402

passed = 0
failed: list[str] = []

GOLDEN = ["aaa new-model",
          "aaa authentication login default group tacacs+ local",
          "ntp server 10.0.0.1",
          "logging host 10.0.0.9"]

COMPLIANT = ("hostname good-sw-01\n"
             "aaa new-model\n"
             "aaa authentication login default group tacacs+ local\n"
             "ntp server 10.0.0.1\n"
             "logging host 10.0.0.9\n")

PARTIAL = ("hostname half-sw-02\n"
           "aaa new-model\n"
           "ntp server 10.0.0.1\n")

WITH_TELNET = COMPLIANT + "transport input telnet\n"


def check(name: str, condition: bool, detail: str = "") -> None:
    global passed
    if condition:
        passed += 1
        print(f"  OK  {name}")
    else:
        failed.append(f"{name}: {detail}")
        print(f"  FAIL {name}\n       {detail}")


def snapshots(mapping, age_days=0.5):
    """A `latest_snapshot(hostname)` over a dict of {hostname: text}."""
    def latest(hostname):
        text = mapping.get(hostname)
        if text is None:
            return None
        return {"hostname": hostname, "content": text,
                "captured_at": time.time() - age_days * 86400}
    return latest


def profile(name, hostname, platform="cisco_ios"):
    return {"id": f"p-{name}", "name": name, "hostname": hostname,
            "connection_type": "ssh", "platform": platform}


def snippet(commands, name="Golden"):
    return {"id": "s1", "name": name, "commands": commands}


# ---------------------------------------------------------------------------

def test_the_three_states() -> None:
    """The whole reason this is not a boolean."""
    print("\n-- Compliant, missing, never captured --")

    profiles = [profile("good", "good-sw-01"), profile("half", "half-sw-02"),
                profile("unseen", "unseen-sw-03")]
    latest = snapshots({"good-sw-01": COMPLIANT, "half-sw-02": PARTIAL})

    report = compliance.check_group(
        "site", profiles, lambda p: snippet(GOLDEN), latest)
    rows = {r["name"]: r for r in report["devices"]}

    check("the compliant one is compliant",
          rows["good"]["state"] == "compliant", str(rows["good"]))
    check("and has nothing missing", rows["good"]["missing"] == [])

    check("the partial one is missing", rows["half"]["state"] == "missing")
    check("and names exactly which lines",
          rows["half"]["missing"] == [
              "aaa authentication login default group tacacs+ local",
              "logging host 10.0.0.9"],
          str(rows["half"]["missing"]))
    check("while counting what it does have",
          rows["half"]["present"] == 2, str(rows["half"]["present"]))

    check("the uncaptured one is its own state",
          rows["unseen"]["state"] == "never-captured",
          "rounding this to compliant reports a device nobody has looked at "
          "as verified; rounding it to missing sends somebody to fix a "
          "device that may be fine")
    check("and claims nothing about its lines",
          rows["unseen"]["missing"] == [] and rows["unseen"]["present"] == 0,
          str(rows["unseen"]))

    check("the counts add up",
          report["counts"] == {"compliant": 1, "missing": 1,
                               "never-captured": 1},
          str(report["counts"]))
    check("worst first, so the rows nobody needs are last",
          [r["state"] for r in report["devices"]]
          == ["missing", "never-captured", "compliant"],
          str([r["state"] for r in report["devices"]]))


def test_the_age_of_the_evidence() -> None:
    print("\n-- How old is this answer --")

    profiles = [profile("good", "good-sw-01")]

    fresh = compliance.check_group(
        "site", profiles, lambda p: snippet(GOLDEN),
        snapshots({"good-sw-01": COMPLIANT}, age_days=0.5))
    row = fresh["devices"][0]
    check("a fresh verdict carries its age",
          row["age_days"] is not None and row["age_days"] < 1,
          str(row["age_days"]))
    check("and is not flagged stale", row["stale"] is False)

    old = compliance.check_group(
        "site", profiles, lambda p: snippet(GOLDEN),
        snapshots({"good-sw-01": COMPLIANT}, age_days=40))
    row = old["devices"][0]
    check("an old one is still compliant — against old evidence",
          row["state"] == "compliant")
    check("but is flagged stale", row["stale"] is True,
          "compliant against a six-week-old capture is a statement about "
          "six weeks ago")
    check("and the report says where the line is",
          old["stale_after_days"] == compliance.STALE_AFTER_DAYS)


def test_lines_that_should_not_be_there() -> None:
    """
    The must-not-have list, which is the same call inverted.

    Anything from that block coming back "present" is a line that should
    not exist — no second mode, no second thing to keep correct.
    """
    print("\n-- Unexpected lines --")

    profiles = [profile("clean", "good-sw-01"), profile("telnet", "bad-sw-04")]
    latest = snapshots({"good-sw-01": COMPLIANT, "bad-sw-04": WITH_TELNET})
    forbidden = snippet(["transport input telnet"], name="Forbidden")

    report = compliance.check_group(
        "site", profiles, lambda p: snippet(GOLDEN), latest,
        lambda p: forbidden)
    rows = {r["name"]: r for r in report["devices"]}

    check("the clean device has nothing unexpected",
          rows["clean"]["unexpected"] == [] and rows["clean"]["state"] == "compliant")
    check("the one with telnet is flagged",
          rows["telnet"]["unexpected"] == ["transport input telnet"],
          str(rows["telnet"]))
    check("and is not compliant despite having every golden line",
          rows["telnet"]["state"] == "missing" and rows["telnet"]["missing"] == [],
          "having the whole block is not the same as being clean")


def test_a_mixed_group_gets_a_block_per_platform() -> None:
    """
    Running the IOS block against a firewall is not a finding.

    It reports every line missing, which reads as a badly misconfigured
    device when the truth is that the check was asked the wrong question.
    """
    print("\n-- A mixed estate --")

    ios = snippet(["ntp server 10.0.0.1"], name="IOS NTP")
    asa = snippet(["ntp server 10.0.0.1"], name="ASA NTP")
    blocks = {"cisco_ios": ios, "cisco_asa": asa}

    profiles = [profile("sw", "mix-sw-01", "cisco_ios"),
                profile("fw", "mix-fw-02", "cisco_asa"),
                profile("odd", "mix-x-03", "mikrotik_routeros")]
    latest = snapshots({
        "mix-sw-01": "ntp server 10.0.0.1\n",
        "mix-fw-02": "ntp server 10.0.0.1\n",
        "mix-x-03": "system ntp client set primary-ntp=10.0.0.1\n",
    })

    report = compliance.check_group(
        "mixed", profiles, lambda p: blocks.get(p), latest)
    rows = {r["name"]: r for r in report["devices"]}

    check("each platform is checked against its own block",
          rows["sw"]["state"] == "compliant" and rows["fw"]["state"] == "compliant",
          str(rows))
    check("a platform with no block is said so, not checked against another",
          rows["odd"]["state"] == "no-snippet",
          "reporting every line missing would read as a misconfigured device "
          "when the check was asked the wrong question")
    check("and it claims nothing about that device's lines",
          rows["odd"]["missing"] == [], str(rows["odd"]))


def test_the_limit_is_carried_with_the_result() -> None:
    """
    Section context is ignored, and the result has to say so.

    A `description uplink` under the wrong interface counts as present.
    That is right for the flat blocks this is for and wrong otherwise, and
    a check that overstates what it verified is worse than no check.
    """
    print("\n-- What it cannot see --")

    profiles = [profile("sw", "ctx-sw-01")]
    # The line is present, but under the wrong interface.
    latest = snapshots({"ctx-sw-01":
                        "interface Gi1/0/2\n description uplink to core\n"})
    report = compliance.check_group(
        "ctx", profiles, lambda p: snippet(["description uplink to core"]),
        latest)

    check("it reports the line as present, as designed",
          report["devices"][0]["state"] == "compliant")
    check("and the result carries the caveat",
          "compared as a set" in report["limits"], report["limits"][:120])
    check("which travels with an exported or forwarded result",
          "limits" in report,
          "a caveat the panel remembers is a caveat a forwarded result loses")


def test_a_device_with_no_name_and_an_empty_snapshot() -> None:
    print("\n-- Edges --")

    profiles = [
        {"id": "p-blank", "name": "", "hostname": "", "connection_type": "ssh"},
        profile("empty", "empty-sw-05"),
    ]
    latest = snapshots({"empty-sw-05": "   \n\n"})
    report = compliance.check_group(
        "edges", profiles, lambda p: snippet(GOLDEN), latest)
    rows = {r["state"] for r in report["devices"]}

    check("a profile with nothing to key on says so",
          "no-device-name" in rows, str(report["devices"]))
    check("a snapshot with no content is never-captured, not compliant",
          "never-captured" in rows,
          "an empty capture proves nothing and must not read as a pass")


def test_the_summary_leads_with_what_to_act_on() -> None:
    print("\n-- One sentence --")

    clean = {"counts": {"compliant": 12}, "checked": 12}
    check("a clean run says so plainly",
          "All 12" in compliance.summary_line(clean),
          compliance.summary_line(clean))

    mixed = {"counts": {"compliant": 46, "missing": 14, "never-captured": 2},
             "checked": 62}
    line = compliance.summary_line(mixed)
    check("a mixed one leads with the number somebody acts on",
          line.startswith("14 of 62"), line)
    check("and mentions the uncaptured ones separately",
          "2 never captured" in line, line)
    check("without leading with the good news",
          "46" not in line,
          "a summary that leads with 46 compliant gets skimmed past")

    check("nothing to check is not a pass",
          compliance.summary_line({"counts": {}, "checked": 0})
          == "Nothing to check.")


def main() -> int:
    print("=" * 52)
    print("  Compliance — did the standard land everywhere")
    print("=" * 52)

    for test in (
        test_the_three_states,
        test_the_age_of_the_evidence,
        test_lines_that_should_not_be_there,
        test_a_mixed_group_gets_a_block_per_platform,
        test_the_limit_is_carried_with_the_result,
        test_a_device_with_no_name_and_an_empty_snapshot,
        test_the_summary_leads_with_what_to_act_on,
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
