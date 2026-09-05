"""
compliance.py — Did the standard land everywhere? (#543)

`config_push.preview` can already say, of one device, which lines of a
block are present and which are missing — without sending anything, from
the configuration ShellMate already holds. Nothing asked that question of
a *group*, and "these fourteen switches are missing the new AAA lines" is
the change-management question at two hundred sites.

It needs no login. The scheduled backups keep every device's latest
snapshot fresh, so this reads what is already stored and answers
immediately. That is the whole appeal, and it is also the thing that makes
the answer subtle:

**Snapshot age is part of the answer, not metadata about it.** "Compliant"
against a capture from six weeks ago is a statement about six weeks ago. A
row that reports compliance without saying how old the evidence is invites
exactly the wrong conclusion, so the age travels with every verdict and a
device with no snapshot at all is its own state rather than being rounded
to either compliant or not.

**Three states, not two.** `compliant`, `missing`, and `never-captured`.
Folding the third into "not compliant" would send somebody to fix a device
that may be perfectly configured; folding it into "compliant" is worse.

**The limit is stated, not hidden.** Lines are matched as a set with
indentation stripped, so section context is ignored — `description uplink`
under the wrong interface counts as present. That is right for the flat
blocks this is for (AAA, logging, NTP, SNMP) and wrong for anything whose
meaning depends on the line above it. Every result carries `limits` saying
so, and the panel prints it. A check that overstates what it verified is
worse than no check.
"""

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

#: How old a snapshot can be before the verdict is flagged as stale. A
#: nightly backup makes everything a day old at worst; a week means the
#: schedule is not running, which is itself the finding.
STALE_AFTER_DAYS = 7

LIMITS = (
    "Lines are compared as a set, with indentation ignored, so a line that "
    "appears under a different parent still counts as present. This suits "
    "flat blocks — AAA, logging, NTP, SNMP — and not configuration whose "
    "meaning depends on the section it sits in."
)


def _platform_of(profile: dict) -> str:
    """
    What ShellMate believes this device is.

    The remembered observation first, then whatever was set by hand. A
    platform nobody has established is "", which callers treat as "any
    snippet applies" rather than as a reason to skip the device.
    """
    return (str(profile.get("last_seen_platform") or "").strip()
            or str(profile.get("platform") or "").strip())


def _snippet_lines(snippet: Any) -> list[str]:
    """The commands of a snippet, however it arrived."""
    if snippet is None:
        return []
    if isinstance(snippet, dict):
        commands = snippet.get("commands") or []
    else:
        commands = getattr(snippet, "commands", None) or []
    return [str(c) for c in commands if str(c).strip()]


def check_device(profile: dict, snippet: Any, latest_snapshot,
                 must_not_have: Any = None) -> dict:
    """
    One device's verdict.

    Args:
        profile:         The connection profile.
        snippet:         The golden block — a Snippet, a dict, or None.
        latest_snapshot: ``hostname -> snapshot row or None``. Injected so a
                         test needs no database and the caller can read the
                         whole group in one pass if it ever wants to.
        must_not_have:   An optional block that should *not* be present.

    Returns:
        A row for the table: name, hostname, platform, state, the missing
        and unexpected lines, and how old the evidence is.
    """
    from backend import config_push

    name = (profile.get("name") or profile.get("hostname")
            or profile.get("id", "?"))
    hostname = (str(profile.get("hostname") or "").strip()
                or str(profile.get("name") or "").strip())
    platform = _platform_of(profile)

    row: dict = {
        "name": name, "hostname": hostname, "platform": platform,
        "state": "never-captured", "missing": [], "unexpected": [],
        "present": 0, "captured_at": None, "age_days": None, "stale": False,
    }

    if not hostname:
        row["state"] = "no-device-name"
        return row

    snapshot = latest_snapshot(hostname)
    if not snapshot or not (snapshot.get("content") or "").strip():
        # Deliberately not "not compliant". A device nobody has captured
        # may be perfectly configured, and sending somebody to fix it is
        # the wrong instruction; so is calling it compliant.
        return row

    content = snapshot.get("content") or ""
    captured_at = snapshot.get("captured_at")
    row["captured_at"] = captured_at
    if captured_at:
        age = max(0.0, (time.time() - float(captured_at)) / 86400.0)
        row["age_days"] = round(age, 1)
        row["stale"] = age > STALE_AFTER_DAYS

    wanted = _snippet_lines(snippet)
    if not wanted:
        row["state"] = "no-snippet"
        return row

    result = config_push.check(content, wanted, platform)
    row["missing"] = [line["text"] for line in result["lines"]
                      if line["status"] == "add"]
    row["present"] = result["counts"]["present"]

    forbidden = _snippet_lines(must_not_have)
    if forbidden:
        # The same call. Anything that comes back present is a line that
        # should not be there — a second mode would be a second thing to
        # keep correct for no gain.
        against = config_push.check(content, forbidden, platform)
        row["unexpected"] = [line["text"] for line in against["lines"]
                             if line["status"] == "present"]

    row["state"] = ("compliant" if not row["missing"] and not row["unexpected"]
                    else "missing")
    return row


def check_group(key: str, profiles: list[dict], snippet_for, latest_snapshot,
                must_not_have_for=None) -> dict:
    """
    Ask the same question of every device in a group.

    Args:
        key:               The group.
        profiles:          Its connections, nested ones included.
        snippet_for:       ``platform -> snippet``. A callable rather than a
                           single snippet because a mixed group needs one
                           block per platform — the AAA lines for IOS are
                           not the AAA lines for NX-OS, and running the IOS
                           block against a firewall would report every line
                           missing and be worse than useless.
        latest_snapshot:   ``hostname -> snapshot row or None``.
        must_not_have_for: ``platform -> snippet``, or None.

    Returns:
        The table, its counts, when it ran, and the limits it was subject
        to. Never raises: one unreadable profile must not lose the other
        199 answers.
    """
    started = time.time()
    rows: list[dict] = []

    for profile in profiles:
        try:
            platform = _platform_of(profile)
            row = check_device(
                profile,
                snippet_for(platform) if snippet_for else None,
                latest_snapshot,
                must_not_have_for(platform) if must_not_have_for else None)
        except Exception as exc:                          # pragma: no cover
            logger.warning("Compliance check failed for %r: %s",
                           profile.get("name"), exc)
            row = {"name": profile.get("name") or "?", "hostname": "",
                   "platform": "", "state": "error", "missing": [],
                   "unexpected": [], "present": 0, "captured_at": None,
                   "age_days": None, "stale": False, "why": str(exc)[:200]}
        rows.append(row)

    # Worst first: a table of two hundred devices is read from the top, and
    # the compliant ones are the rows nobody needs to see.
    order = {"missing": 0, "never-captured": 1, "no-device-name": 2,
             "no-snippet": 3, "error": 4, "compliant": 5}
    rows.sort(key=lambda r: (order.get(r["state"], 9), r["name"].lower()))

    counts: dict[str, int] = {}
    for row in rows:
        counts[row["state"]] = counts.get(row["state"], 0) + 1

    return {
        "group": key,
        "at": started,
        "took_s": round(time.time() - started, 1),
        "devices": rows,
        "counts": counts,
        "checked": len(rows),
        # Carried with the result rather than left to the panel to
        # remember: a result forwarded, exported or read from the digest
        # has to arrive with the caveat attached to it.
        "limits": LIMITS,
        "stale_after_days": STALE_AFTER_DAYS,
    }


def summary_line(report: dict) -> str:
    """
    One sentence, for the digest and the group card.

    Names the two numbers somebody acts on and stays quiet about the rest.
    "14 of 60 missing lines" is a morning's work; "46 compliant" is not
    news, and a summary that leads with it gets skimmed past.
    """
    counts = report.get("counts") or {}
    missing = counts.get("missing", 0)
    never = counts.get("never-captured", 0)
    checked = report.get("checked", 0)

    if not checked:
        return "Nothing to check."
    if not missing and not never:
        return f"All {checked} device(s) have the whole block."

    bits = []
    if missing:
        bits.append(f"{missing} of {checked} missing lines")
    if never:
        bits.append(f"{never} never captured")
    return ", ".join(bits) + "."
