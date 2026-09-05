"""
backup_webhook.py — Telling something other than ShellMate what the night found (#539).

The in-app digest answers "what happened overnight" for somebody sitting in
front of ShellMate. Most mornings nobody is: the person who needs to know
that core-2 changed is in Teams, or the team's channel, or a ticketing
system watching a URL.

Three rules, and the first is the whole design:

**Silence is the feature.** A clean night sends nothing at all. A webhook
that fires every morning whether or not anything happened is one whose
messages get filtered into a folder, and then the morning something did
happen looks exactly like every other morning. Exactly the reasoning the
digest itself is built on, and the two must not diverge.

**One source of numbers.** The body is built from the same `scheduler.digest`
the panel renders. Computing "2 changed, 1 failed" a second time here would
be a second chance to be wrong, and a webhook that disagrees with the screen
is worse than no webhook.

**Never the configuration.** Counts and device names, never diff text —
unless a separate setting says so, and then redacted like everything else.
A backup digest that posts a running config into a chat channel has moved
somebody's estate somewhere with a very different access model.

The URL is a bearer secret: anyone holding it can post into the channel. It
goes in the vault beside the API keys, is diverted out of settings.json by
`update_settings`, and is masked in the support bundle.
"""
from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from backend.advanced import get as advanced

logger = logging.getLogger(__name__)

#: Where the URL lives in the vault.
VAULT_KEY = "backup_webhook_url"

#: The shapes that can be sent.
#:
#: Generic JSON is the one that keeps working. Teams' incoming-webhook
#: format changed under Workflows and the old MessageCard is on the way
#: out, so the card is offered as a convenience and the JSON is the
#: contract — anything that can read a POST body can consume it.
FORMATS = ("json", "teams", "slack")


def _settings() -> dict:
    from backend.settings_store import get_settings

    return dict((get_settings().get("backups") or {}))


def url() -> str:
    """The configured URL, from the vault. Empty when there is none."""
    from backend.vault import vault

    try:
        return str(vault.get(VAULT_KEY, "") or "").strip()
    except Exception:
        # A locked vault degrades to "no value" rather than raising, the
        # same as every other secret — a forgotten master password must not
        # stop a backup running.
        return ""


def is_configured() -> bool:
    """Whether anything would be sent at all."""
    return bool(_settings().get("webhook_enabled")) and bool(url())


def _link() -> str:
    """
    Where to go and look, when ShellMate is reachable at a known address.

    The issue asked for a ShellMate link in the body. There is nothing
    honest to put there by default: ShellMate binds to loopback on a port
    chosen at startup, so `http://localhost:8765` posted into a team channel
    is a link that works for exactly one person and misleads everybody else
    into thinking it is broken. So it comes from `backups.webhook_link`,
    empty unless a deployment that really is reachable at an address has
    been given one.
    """
    return str(advanced("backups.webhook_link") or "").strip()


# ---------------------------------------------------------------------------
# The bodies
# ---------------------------------------------------------------------------

def _summary(report: dict) -> str:
    """The one sentence, from the same place the toast gets it."""
    from backend import scheduler

    return scheduler.digest_line(report) or "A scheduled backup finished."


def build_json(report: dict, include_diff: bool = False) -> dict:
    """
    The generic body: counts, names, and where to look.

    Named fields rather than a rendered string, because the point of the
    generic shape is that something else can act on it — filter on
    `failed`, route on `group`, open a ticket when `changed` is non-empty.
    """
    groups = []
    for entry in report.get("groups") or []:
        groups.append({
            "group":         entry.get("name") or entry.get("group") or "",
            "at":            entry.get("at"),
            "changed":       list(entry.get("changed") or []),
            "failed":        list(entry.get("failed") or []),
            "skipped":       list(entry.get("skipped") or []),
            "missed":        int(entry.get("missed") or 0),
            "non_compliant": int(entry.get("non_compliant") or 0),
            "unverifiable":  int(entry.get("unverifiable") or 0),
        })

    body: dict[str, Any] = {
        "source":  "shellmate",
        "event":   "backup",
        "summary": _summary(report),
        "changed": int(report.get("changed") or 0),
        "failed":  int(report.get("failed") or 0),
        "missed":  int(report.get("missed") or 0),
        "non_compliant": int(report.get("non_compliant") or 0),
        "unverifiable":  int(report.get("unverifiable") or 0),
        "groups":  groups,
    }
    link = _link()
    if link:
        body["url"] = link
    if include_diff:
        # Only when asked for, and only redacted. The setting exists
        # because some teams genuinely want the change in the channel; the
        # redaction is not negotiable either way.
        body["diffs"] = _diffs(report)
    return body


def _diffs(report: dict) -> list[dict]:
    """
    What changed, redacted, and only for the devices that changed.

    Through `outbound.redact_text` like everything else that leaves the
    machine. A running configuration carries hashes, keys and community
    strings, and a chat channel is a very different access model from the
    archive folder it came from.
    """
    from backend.configs import diff_snapshots
    from backend.store import store
    from backend.session.outbound import redact_text

    out: list[dict] = []
    cap = int(advanced("backups.webhook_diff_lines"))
    for entry in report.get("groups") or []:
        for device in (entry.get("changed") or []):
            try:
                # The two newest stored snapshots: the run that just
                # finished, and the one it differs from. Read back from the
                # store rather than recomputed against the device —
                # `drift_report` opens a channel, and by the time this runs
                # the session it would have used has been closed.
                recent = store.list_snapshots(device, 2)
                if len(recent) < 2:
                    continue
                new_snap = store.get_snapshot(recent[0].get("id"))
                old_snap = store.get_snapshot(recent[1].get("id"))
                if not new_snap or not old_snap:
                    continue
                diff = (diff_snapshots(old_snap, new_snap) or {}).get("diff", "")
            except Exception as exc:
                logger.debug("No diff for %s: %s", device, exc)
                continue
            lines = redact_text(diff or "").splitlines()[:cap]
            if lines:
                out.append({"device": device, "diff": "\n".join(lines)})
    return out


def build_card(report: dict, style: str) -> dict:
    """
    A chat-service card. One shape per service, both deliberately plain.

    No colours, no images, no actions. A card that renders on the service
    the day it was written and degrades to an empty bubble after a platform
    change is worse than a line of text that always arrives — which is why
    the generic JSON, not this, is the thing the tests treat as the
    contract.
    """
    text = _summary(report)
    link = _link()
    if link:
        text = f"{text} — {link}"

    if style == "slack":
        return {"text": f"ShellMate: {text}"}

    # Teams. `text` on its own is understood by the classic incoming
    # webhook and by a Workflows "Post to channel" step, which is the
    # widest thing that still works after the connector deprecation.
    return {"title": "ShellMate backup", "text": text}


def build(report: dict) -> dict:
    """The body for the configured format."""
    settings = _settings()
    style = str(settings.get("webhook_format") or "json").lower()
    if style not in FORMATS:
        style = "json"
    if style == "json":
        return build_json(report, bool(settings.get("webhook_include_diff")))
    return build_card(report, style)


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------

def send(report: dict) -> dict:
    """
    Post one digest, or say why nothing was sent.

    Never raises. It is called at the end of a scheduled backup, and a
    webhook failure must not turn a successful night's captures into a
    failed one — the configurations are already stored, which is the part
    that mattered.

    Returns ``{"sent": bool, "reason": str, "status": int}``. ``reason`` is
    the empty string on success and a short phrase otherwise, because the
    settings panel's Test button needs to say which of "not configured",
    "nothing to report" and "the server refused it" happened.
    """
    if not is_configured():
        return {"sent": False, "reason": "not configured", "status": 0}

    if not report.get("anything"):
        # The rule. A clean night is not news, and a webhook that fires
        # anyway is one whose messages get filtered into a folder.
        return {"sent": False, "reason": "nothing to report", "status": 0}

    target = url()
    try:
        response = httpx.post(
            target, json=build(report),
            timeout=float(advanced("backups.webhook_timeout")),
        )
        response.raise_for_status()
        logger.info("Backup digest posted to the webhook (%s)",
                    response.status_code)
        return {"sent": True, "reason": "", "status": response.status_code}
    except httpx.HTTPStatusError as exc:
        # The status, not the body. A webhook's error body routinely
        # contains the URL it was posted to, which is the secret.
        logger.warning("The backup webhook refused the digest: HTTP %s",
                       exc.response.status_code)
        return {"sent": False, "reason": f"HTTP {exc.response.status_code}",
                "status": exc.response.status_code}
    except Exception as exc:
        logger.warning("The backup webhook could not be reached: %s",
                       type(exc).__name__)
        return {"sent": False,
                "reason": f"could not be reached ({type(exc).__name__})",
                "status": 0}


def notify_after_run() -> dict:
    """
    Called when a scheduled backup finishes.

    Reads the digest rather than being handed the run's result, so the
    numbers it sends are the numbers the panel shows — including the
    compliance figures, which are attached to the group after the backup
    result is written. Being handed the result would send a message that
    disagreed with the screen for exactly the runs where it mattered most.
    """
    from backend import scheduler

    try:
        # `include_seen` — the seen marker is about somebody having *read*
        # the panel, and whether a person has looked at a screen says
        # nothing about whether a channel has been told.
        return send(scheduler.digest(include_seen=True))
    except Exception as exc:
        logger.warning("Could not post the backup digest: %s", exc)
        return {"sent": False, "reason": str(exc), "status": 0}


def preview(report: dict | None = None) -> str:
    """
    The body that would be sent, as text, for the settings panel.

    A webhook is the one integration where nobody can see what arrived
    until it has already arrived somewhere they may not control. Showing
    the body first is cheap; the alternative is finding out by posting a
    running configuration into a company channel.
    """
    from backend import scheduler

    if report is None:
        report = scheduler.digest(include_seen=True)
    return json.dumps(build(report), indent=2)
