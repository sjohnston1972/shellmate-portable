"""
feedback.py — The in-app bug and feature-request reporter (#370).

ShellMate is used by people who do not have GitHub accounts and are often on
networks that cannot reach github.com at all. So a report does not go to
GitHub directly: it goes to a small relay (see relay/ in the repository),
which holds the only credential and files the report as a labelled GitHub
issue. The portable executable carries no token, because anything inside it
can be read out of it.

Three outcomes, and the caller is told which happened — never silently:

- **sent** — the relay accepted it and a GitHub issue now exists.
- **queued** — the relay is unreachable (or not configured), so the report
  went to ``feedback-outbox.json`` in the data folder. Queued reports are
  retried at the next launch and after the next successful send.
- **refused** — the report itself was unusable (no title, wrong type). The
  bounds here mirror the relay's, so nothing valid locally dies remotely.

Nothing from any terminal session is ever attached. A report carries what the
user typed plus the platform line and whether this is a frozen build — the
same two facts the support mail template discloses, and nothing else.
"""

import json
import logging
import platform as platform_module
import threading

import httpx

from backend import paths
from backend.advanced import get as advanced

logger = logging.getLogger(__name__)

#: Sent as X-ShellMate-Key. Not a secret — anyone can read it out of the
#: executable — it exists so the relay can drop drive-by POSTs from scanners,
#: and it can be rotated in a release. The relay holds the same value.
APP_KEY = "shellmate-feedback-v1"

# "crash" joins the two a person types (#568). It is a report like any
# other from here down — the relay files it the same way and the outbox
# holds it the same way — but it is the only kind whose text ShellMate
# wrote rather than the user, which is exactly why the panel shows the
# whole thing before anything is sent.
TYPES = ("bug", "feature", "crash")
MAX_TITLE = 200
MAX_DESCRIPTION = 5000

#: The outbox is a convenience, not a database. Fifty unsent reports means
#: the relay has been unreachable for a long time; growing without bound on
#: a USB stick would be its own bug.
MAX_QUEUED = 50

SEND_TIMEOUT = 10.0

#: One writer at a time. Submits arrive from the API thread pool and the
#: startup flush at once, and interleaved read-modify-writes would drop
#: reports without an error.
_lock = threading.Lock()


def outbox_path():
    """Where unsent reports wait. In the data dir, so it travels with the exe."""
    return paths.data_dir() / "feedback-outbox.json"


def relay_url() -> str:
    return str(advanced("feedback.relay_url") or "").strip()


def build_report(kind: str, title: str, description: str) -> dict:
    """
    Validate and assemble one report. Raises ValueError when unusable.

    Length is clamped rather than refused: a description one character over
    the cap is a report worth having, and the person typing it cannot see
    the number.
    """
    kind = (kind or "").strip().lower()
    if kind not in TYPES:
        raise ValueError("A report is a 'bug', a 'feature' or a 'crash'.")

    title = (title or "").strip()
    if not title:
        raise ValueError("A report needs a title.")

    return {
        "type":        kind,
        "title":       title[:MAX_TITLE],
        "description": (description or "").strip()[:MAX_DESCRIPTION],
        "platform":    platform_module.platform(),
        "portable":    paths.is_frozen(),
    }


def as_text(report: dict) -> str:
    """The report as plain text, for the copy-to-clipboard fallback."""
    return "\n".join([
        f"ShellMate {report['type']} report: {report['title']}",
        "",
        report["description"] or "(no description)",
        "",
        f"--- {report['platform']}"
        f" · {'portable build' if report['portable'] else 'from source'}",
    ])


def _send(report: dict, url: str) -> None:
    """One POST to the relay. Raises on anything but an acceptance."""
    response = httpx.post(
        url,
        json=report,
        headers={"X-ShellMate-Key": APP_KEY},
        timeout=SEND_TIMEOUT,
    )
    response.raise_for_status()


def _load_outbox() -> list[dict]:
    try:
        data = json.loads(outbox_path().read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _save_outbox(reports: list[dict]) -> None:
    path = outbox_path()
    if not reports:
        path.unlink(missing_ok=True)
        return
    path.write_text(json.dumps(reports[-MAX_QUEUED:], indent=2),
                    encoding="utf-8")


def submit(kind: str, title: str, description: str) -> dict:
    """
    Send one report, or queue it when sending is not possible.

    Returns {"status": "sent"|"queued", "queued": <outbox size>, "text": ...}.
    The text rides along on every outcome so the interface can offer
    copy-to-clipboard without asking again.
    """
    report = build_report(kind, title, description)
    url = relay_url()

    with _lock:
        if url:
            try:
                _send(report, url)
                logger.info("Feedback sent: %s '%s'",
                            report["type"], report["title"])
                flushed = _flush_locked(url)
                return {"status": "sent", "queued": flushed,
                        "text": as_text(report)}
            except Exception as exc:
                logger.warning("Feedback relay unreachable, queuing: %s", exc)

        queue = _load_outbox()
        queue.append(report)
        _save_outbox(queue)
        return {"status": "queued", "queued": len(queue),
                "text": as_text(report)}


def flush() -> int:
    """Retry queued reports. Returns how many remain. Called at startup."""
    url = relay_url()
    if not url:
        return len(_load_outbox())
    with _lock:
        return _flush_locked(url)


def _flush_locked(url: str) -> int:
    """The flush itself; the caller holds the lock. Stops at the first
    failure — if one report cannot be sent, the rest cannot either, and
    fifty timeouts in a row would stall startup for eight minutes."""
    queue = _load_outbox()
    remaining = list(queue)
    for report in queue:
        try:
            _send(report, url)
            remaining.remove(report)
        except Exception:
            break
    if len(remaining) != len(queue):
        logger.info("Feedback outbox: sent %d queued report(s)",
                    len(queue) - len(remaining))
        _save_outbox(remaining)
    return len(remaining)
