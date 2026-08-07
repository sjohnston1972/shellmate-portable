"""
alerts.py — Things that will happen to a device unless somebody intervenes.

`reload in 10` is the command that most needs a clock beside it, and a
terminal that says nothing about it is leaving the most consequential piece of
state in the session invisible. Junos `commit confirmed` is the same shape:
quietly rolls itself back if you get distracted.

This module tracks those, per session, and nothing else. It is the *producer*
of alerts, not the alert system — the interface decides what a pending reboot
sounds like and looks like.

Two sources, because they answer different questions:

**What was typed.** Known the instant Enter is pressed, and still true when
the device's reply has scrolled off the top. But `reload in 10` tells you the
intent, not the moment — the device rounds, and the clocks differ.

**What the device said.** `*** --- SHUTDOWN in 0:09:58 ---` is authoritative,
and IOS repeats it as the deadline nears, so the countdown re-synchronises
with the device rather than drifting against it.

The typed command opens the alert; the device's answer refines it. Where only
the first is available the alert says a reload was *requested* and shows no
countdown, because a confident wrong timer is worse than an honest vague one.
"""

import logging
import re
import time
from dataclasses import dataclass, field

from backend import platforms as platforms_module

from backend.advanced import get as advanced

logger = logging.getLogger(__name__)

RELOAD = "reload"
COMMIT_CONFIRM = "commit-confirm"

# Junos applies this when `commit confirmed` is given with no number.
DEFAULT_COMMIT_CONFIRM_MINUTES = 10

# A pending action older than this is dropped. Something was missed — the
# device rebooted and the socket survived, or the cancel scrolled past — and a
# countdown stuck at zero for the rest of the afternoon is worse than none.
STALE_AFTER_SECONDS = 6 * 3600


@dataclass
class PendingAction:
    """Something scheduled on a device that has not happened yet."""

    kind: str                       # RELOAD | COMMIT_CONFIRM
    source: str                     # the command or output that revealed it
    requested_at: float
    # Wall-clock epoch seconds. None when the device has not told us and the
    # typed command did not say — see the module docstring.
    deadline: float | None = None
    # True once a deadline is known well enough to count down from.
    confident: bool = False
    # True when the deadline came from the device rather than from reading the
    # typed command. Kept apart from `confident` because they answer different
    # questions: whether to show a countdown, and whether a fresh estimate of
    # our own is allowed to overwrite it.
    authoritative: bool = False
    # True while the typed command has not yet been answered by the device's
    # own scheduling banner (#248). Tracked — so a cancel still clears it —
    # but not counted down from: `reload in 10` can still be aborted at the
    # device's [confirm] prompt, and a countdown for a reload that never
    # started is a false alarm with a deadline.
    awaiting_confirmation: bool = False

    def seconds_left(self) -> float | None:
        if self.deadline is None:
            return None
        return self.deadline - time.time()

    def as_dict(self) -> dict:
        # While awaiting confirmation the deadline is only our reading of the
        # typed command — good enough to hold, not good enough to show (#248).
        masked = self.awaiting_confirmation
        return {
            "kind": self.kind,
            "source": self.source,
            "requested_at": self.requested_at,
            # Milliseconds, because the browser's clock works in them.
            "deadline_ms": (None if (self.deadline is None or masked)
                            else int(self.deadline * 1000)),
            "confident": self.confident and not masked,
            "authoritative": self.authoritative,
            "awaiting_confirmation": masked,
        }


# ---------------------------------------------------------------------------
# Reading a duration out of a match
# ---------------------------------------------------------------------------


def _groups(match: re.Match) -> dict:
    try:
        return match.groupdict()
    except Exception:
        return {}


def _seconds_from(match: re.Match) -> float | None:
    """
    Turn a pattern's named groups into seconds from now.

    Returns None when the match carried no timing at all, which is a real
    outcome: `reload` on its own means *now*, and `commit confirmed` with no
    number means the platform default. Both are handled by the caller, which
    knows which pattern matched.
    """
    groups = _groups(match)

    absolute = groups.get("at")
    if absolute:
        return _seconds_until_clock_time(absolute)

    parts = {k: groups.get(k) for k in ("h", "m", "s")}
    if not any(parts.values()):
        return None

    total = 0.0
    total += int(parts["h"] or 0) * 3600
    total += int(parts["m"] or 0) * 60
    total += int(parts["s"] or 0)
    return total


def _seconds_until_clock_time(text: str) -> float | None:
    """
    Seconds until the next occurrence of an HH:MM on the wall clock.

    Uses this machine's clock, which is not the device's. Close enough for a
    countdown and honest about it: the alert says what was scheduled, and the
    device's own banner overwrites this estimate the moment it arrives.
    """
    try:
        hours, minutes = (int(part) for part in text.split(":", 1))
    except ValueError:
        return None
    if not (0 <= hours < 24 and 0 <= minutes < 60):
        return None

    now = time.localtime()
    delta = (hours - now.tm_hour) * 3600 + (minutes - now.tm_min) * 60 - now.tm_sec
    if delta <= 0:
        delta += 24 * 3600          # it means tomorrow
    return float(delta)


def _search(patterns: list[str], text: str) -> re.Match | None:
    """
    Find the most useful match, not merely the first.

    Devices echo. A chunk of output that says "commit confirmed will be
    automatically rolled back in 2 minutes" matches both the pattern for the
    typed command and the one carrying the timing, and taking whichever came
    first in the list would throw away the only number in the sentence.

    So every pattern is tried and one that yields a duration wins. A match
    without timing is still returned if it is all there is — it means
    something is pending, which is worth saying even without a countdown.
    """
    fallback = None
    for pattern in patterns:
        try:
            match = re.search(pattern, text, re.IGNORECASE)
        except re.error as exc:
            # A user-edited platforms.json can hold a broken pattern. Skipping
            # it keeps the rest working; failing here would take the session's
            # whole output path down with it.
            logger.warning("Bad alert pattern %r: %s", pattern, exc)
            continue
        if not match:
            continue
        if _seconds_from(match) is not None:
            return match
        if fallback is None:
            fallback = match
    return fallback


# ---------------------------------------------------------------------------
# The tracker
# ---------------------------------------------------------------------------


@dataclass
class AlertTracker:
    """
    What is pending on one session.

    One per session, owned by that session's reader, so no locking.
    """

    platform: str = ""
    pending: PendingAction | None = field(default=None)

    def _profile(self):
        return platforms_module.get_profile(self.platform) if self.platform else None

    # -- inputs ------------------------------------------------------------

    def observe_command(self, command: str) -> bool:
        """
        Look at a command on its way to the device.

        Returns True when the pending state changed and the interface should
        be told.
        """
        if not command or not command.strip():
            return False
        return self._observe(command.strip(), from_device=False)

    def observe_output(self, text: str) -> bool:
        """Look at what the device said. Same contract as observe_command."""
        if not text or not text.strip():
            return False
        return self._observe(text, from_device=True)

    def _observe(self, text: str, from_device: bool) -> bool:
        profile = self._profile()
        if profile is None:
            return False

        # Cancellations first. Someone typing `reload cancel` while a pattern
        # elsewhere would also match must end up with nothing pending.
        if self.pending is not None:
            cancels = (profile.reload_cancel_patterns
                       if self.pending.kind == RELOAD
                       else profile.commit_cancel_patterns)
            if _search(cancels, text):
                logger.info("Pending %s cancelled by %r", self.pending.kind, text[:60])
                self.pending = None
                return True

        changed = self._match_reload(profile, text, from_device)
        changed = self._match_commit_confirm(profile, text, from_device) or changed
        return changed

    def _match_reload(self, profile, text: str, from_device: bool) -> bool:
        announce = getattr(profile, "reload_announce_patterns", None) or []

        if from_device:
            # Where the platform has announcement patterns, only those count
            # as the device's word (#248) — the echoed command text also
            # arrives as output, and an echo is not a confirmation. A
            # platform with no announcement patterns keeps the old behaviour.
            match = (_search(announce, text) if announce
                     else _search(profile.reload_patterns, text))
        else:
            match = _search(profile.reload_patterns, text)
        if not match:
            return False

        seconds = _seconds_from(match)
        # A platform that announces its reloads gets no countdown from the
        # typed command alone (#248): the pending is tracked, badged, and
        # armed only when the announcement lands.
        awaiting = (not from_device) and bool(announce)
        return self._record(RELOAD, match.group(0).strip(), seconds,
                            from_device, awaiting=awaiting)

    def _match_commit_confirm(self, profile, text: str, from_device: bool) -> bool:
        match = _search(profile.commit_confirm_patterns, text)
        if not match:
            return False

        seconds = _seconds_from(match)
        if seconds is None and not from_device:
            # "commit confirmed" with no number: the platform's own default.
            seconds = advanced("alerts.commit_confirm_minutes") * 60
        return self._record(COMMIT_CONFIRM, match.group(0).strip(), seconds, from_device)

    def _record(self, kind: str, source: str, seconds: float | None,
                from_device: bool, awaiting: bool = False) -> bool:
        now = time.time()
        deadline = None if seconds is None else now + seconds

        existing = self.pending
        if existing is not None and existing.kind == kind:
            if deadline is None:
                return False
            # The device's own answer re-synchronises a countdown that has
            # drifted, and our estimate must not overwrite it. A *fresh* typed
            # command is a different matter: someone who has just typed
            # `commit confirmed 3` has changed the deadline, and the old one is
            # simply wrong.
            if not from_device and existing.authoritative:
                return False
            # IOS repeats its banner every minute. Only report a change worth
            # reporting, or the interface is redrawn for nothing. A device
            # answer that *confirms* an awaited command is always worth it,
            # whatever the deadline delta — it is the arming moment (#248).
            if (abs((existing.deadline or 0) - deadline) < 2
                    and existing.authoritative == from_device
                    and not (from_device and existing.awaiting_confirmation)):
                return False

            existing.deadline = deadline
            existing.confident = True
            existing.authoritative = existing.authoritative or from_device
            if from_device:
                existing.awaiting_confirmation = False
            existing.source = source or existing.source
            return True

        self.pending = PendingAction(
            kind=kind,
            source=source,
            requested_at=now,
            deadline=deadline,
            confident=deadline is not None,
            authoritative=from_device and deadline is not None,
            awaiting_confirmation=awaiting,
        )
        logger.info(
            "Pending %s on this session: %s%s",
            kind, "in %.0fs" % seconds if seconds is not None else "time unknown",
            " (awaiting the device's confirmation)" if awaiting else "",
        )
        return True

    # -- outputs -----------------------------------------------------------

    def expire(self) -> bool:
        """
        Drop a pending action that has come and gone.

        Called from the read loop. Returns True when something was cleared.
        """
        if self.pending is None:
            return False

        age = time.time() - self.pending.requested_at
        left = self.pending.seconds_left()

        # Past its deadline by a clear margin: it either happened, or it was
        # cancelled somewhere we did not see.
        if left is not None and left < -60:
            logger.info("Pending %s has passed; clearing", self.pending.kind)
            self.pending = None
            return True

        if age > advanced("alerts.stale_hours") * 3600:
            logger.info("Pending %s is stale; clearing", self.pending.kind)
            self.pending = None
            return True

        return False

    def clear(self) -> bool:
        """Forget anything pending, e.g. on reconnect."""
        if self.pending is None:
            return False
        self.pending = None
        return True

    def retract(self, max_age_seconds: float = 30.0) -> bool:
        """
        Drop a pending that only ever came from a freshly typed command.

        The tracker arms at Enter, before the guardrail question is answered —
        so declining "send `reload in 10`?" used to leave a live countdown for
        a command the device never received (#248). Bounded by age and by
        authority: anything the device itself has confirmed is not ours to
        retract, and neither is something typed minutes ago.
        """
        pending = self.pending
        if (pending is not None and not pending.authoritative
                and time.time() - pending.requested_at <= max_age_seconds):
            logger.info("Pending %s retracted — the command was not sent",
                        pending.kind)
            self.pending = None
            return True
        return False

    def payload(self) -> dict:
        """What to send to the interface."""
        if self.pending is None:
            return {"type": "pending_action", "pending": None}

        pending = self.pending.as_dict()
        # The way out, alongside the warning (#292). Sent with the pending
        # rather than looked up in the browser, because the platform is
        # already resolved here and the interface has no business deciding
        # what to type at a device.
        if self.pending.kind == RELOAD:
            profile = self._profile()
            pending["cancel_command"] = getattr(
                profile, "reload_cancel_command", "") if profile else ""
        return {"type": "pending_action", "pending": pending}
