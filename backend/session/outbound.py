"""
session/outbound.py — The one door out for terminal content.

Everything that sends a session's output off this machine goes through here:
the AI chat, the session summary, and the Jira export.  There is a single
function because there was previously no single place, and the consequence was
that ``redact()`` — the whole point of which is keeping credentials out of
things handed to other people — was applied to session logs and captured
configurations and to nothing else.

Devices echo.  ``show run`` on a device with unencrypted secrets in its
configuration puts password hashes, pre-shared keys and community strings
straight into the buffer.  That buffer was reaching third-party APIs, and — via
*Conclude to Jira* — being written into tickets that persist, that colleagues
read, and that on most instances are searchable across an organisation.  A
ticket is more "handed to someone else" than a log file on disk ever is.

Two things this deliberately does **not** touch:

**What is on screen.**  The terminal always shows the truth.  Hiding a
credential from the person who typed it would be worse than useless, and they
may have to account for that session afterwards.

**The buffer itself.**  It is the record of what the device said, and what the
history database and the evidence pack read from.  Masking happens here, at
the point of sending, on a copy.

Be honest about the limit: this is pattern matching, so a credential in a form
``redact()`` does not recognise goes through untouched.  It reduces exposure
rather than guaranteeing its absence.
"""

import logging

from backend.session.ansi import clean
from backend.session.redact import redact
from backend.settings_store import get_settings

logger = logging.getLogger(__name__)


def redaction_enabled() -> bool:
    """
    Whether outbound terminal content should be masked.

    Shares ``logging.redact_secrets`` with session logs and captured
    configurations rather than having a switch of its own. The setting is
    presented to users as a property of ShellMate — "obscure passwords and
    secrets" — not of one output format, and two switches for one promise is a
    promise nobody can rely on.
    """
    from backend.advanced import get as advanced

    # Two switches, and both have to allow it. The logging one is the promise
    # made to everybody; the Stockton one exists so somebody running a local
    # model on their own machine can deliberately opt out of masking on the
    # way to it. Neither alone can turn masking on where the other says no.
    if not advanced("ai.redact_context"):
        return False
    return bool((get_settings().get("logging", {}) or {}).get("redact_secrets", True))


def session_text(session: dict, lines: int = 200) -> str:
    """
    Return a session's recent output, ready to leave the machine.

    Two passes, in this order:

    ``clean()`` removes escape sequences, backspaces and pager artefacts.
    Sending the raw stream wastes tokens on invisible control codes and, worse,
    splits words the reader needs — a coloured interface name arrives with
    escape codes buried inside it. It also has to run *first*, because a
    credential with a colour code in the middle of it would not match any
    pattern below.

    ``redact()`` masks what the patterns recognise.

    Args:
        session: The session dict from SessionManager.
        lines:   How many lines of recent output to take.

    Returns:
        Text safe to send, or "" if the session has no buffer.
    """
    buffer = session.get("buffer")
    if buffer is None:
        return ""

    text = clean(buffer.get_text(lines))
    return redact(text) if redaction_enabled() else text


def redact_text(text: str) -> str:
    """
    Mask an already-extracted string.

    For content that did not come from a session buffer — chat messages
    quoted back into a Jira ticket, for instance — where the caller has the
    text but not the session.
    """
    return redact(text) if redaction_enabled() else text
