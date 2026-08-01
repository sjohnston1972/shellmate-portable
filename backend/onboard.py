"""
onboard.py — What happens in the first few seconds of a session.

Identify the device, then act on it: turn paging off so a ``show run`` does not
stop every 24 lines, and switch the session's aliases and guardrails to the
right platform.

Deliberate choices here, because this code types into someone's live session:

**Nothing is sent to an unidentified device.**  Guessing a paging command and
having it land as a syntax error is untidy; having it land as something
*meaningful* on an unrecognised platform is dangerous.  The generic profile
sends nothing at all.

**The command is visible.**  It is echoed into the terminal exactly as if the
user had typed it, and the UI says what was sent.  A tool that silently injects
commands into a session an engineer is about to make changes in — and may later
have to account for — is not one worth trusting.

**Once only, and never mid-command.**  Onboarding waits for the device to
reach a prompt, so the paging command cannot land in the middle of something
the user is already typing.
"""

import logging
import time
from dataclasses import dataclass, field

from backend.fingerprint import ACT_THRESHOLD, Fingerprint, identify
from backend.platforms import GENERIC, get_profile

logger = logging.getLogger(__name__)

# How long to keep collecting banner text before identifying the device.
# Long enough for a login banner to finish printing, short enough that the
# session is configured before anyone starts typing.
IDENTIFY_AFTER = 1.0

# Give up waiting for a prompt after this long and identify from the banner
# alone. Some devices sit behind a terminal server that never shows one.
IDENTIFY_DEADLINE = 8.0


def summarise(fingerprint: Fingerprint, auto_paging: bool = True) -> dict:
    """
    Describe what was identified and what follows from it.

    The important half is ``paging_skipped``.  Paging-off is gated on
    confidence, and the gate fires far more often than people expect: a device
    whose banner is a legal warning rather than a version string is identified
    from its prompt alone, scores below :data:`ACT_THRESHOLD`, and is
    deliberately sent nothing.  That behaviour is right — guessing a paging
    command at a device we are unsure of is what the generic profile exists to
    prevent — but saying only *"identified Cisco IOS"* while paging stays on
    reads as though everything worked.  So the reason travels with the result
    and the interface states it.

    ``paging_command`` is what will actually be sent, and is empty whenever
    nothing will be.  Both gates — confidence and the user's setting — are
    resolved here, so the interface has one thing to believe rather than
    re-deriving the decision and getting it subtly wrong.

    Args:
        fingerprint: What the device was identified as.
        auto_paging: The user's "turn paging off on connect" setting.

    Returns:
        A summary for the frontend and the session record.
    """
    profile = get_profile(fingerprint.platform)

    if not profile.paging_off:
        # A shell, or a platform someone has deliberately blanked.
        skipped = "no-command" if fingerprint.platform != GENERIC else "unidentified"
    elif fingerprint.platform == GENERIC:
        skipped = "unidentified"
    elif not fingerprint.certain_enough_to_act:
        skipped = "unconfident"
    elif not auto_paging:
        skipped = "off"
    else:
        skipped = ""

    summary = fingerprint.as_dict()
    summary["profile_name"] = profile.name
    summary["paging_command"] = profile.paging_off if not skipped else ""
    # What *would* be sent, so the interface can name the command it is
    # declining to send rather than describing the omission in the abstract.
    summary["paging_available"] = profile.paging_off
    summary["paging_skipped"] = skipped
    summary["confident"] = fingerprint.certain_enough_to_act
    summary["act_threshold"] = ACT_THRESHOLD
    return summary


def as_chosen(platform_id: str) -> Fingerprint:
    """
    Build a fingerprint for a platform the user named themselves.

    The escape hatch for the two cases automatic identification cannot win:
    a device whose banner is a legal warning instead of a version string, and
    anything reached through a terminal server, where the banner belongs to
    the terminal server rather than to the device.  Both are identified from
    the prompt alone, score below the acting threshold, and are correctly sent
    nothing — leaving a setting that is on and does nothing.

    Told directly, there is nothing left to be unsure about, so this carries
    full confidence.  It is the one source that is not a guess.

    Raises:
        ValueError: No such platform.
    """
    from backend.platforms import load_profiles

    profile = load_profiles().get(platform_id)
    if profile is None:
        raise ValueError(f"'{platform_id}' is not a platform ShellMate knows about.")

    return Fingerprint(
        platform=profile.id,
        name=profile.name,
        confidence=1.0,
        source="you",
    )


@dataclass
class Onboarder:
    """Runs the once-per-session identify-and-configure routine."""

    started_at: float = field(default_factory=time.monotonic)
    _banner: str = field(default="", init=False)
    _done: bool = field(default=False, init=False)

    fingerprint: Fingerprint | None = field(default=None, init=False)
    paging_command: str = field(default="", init=False)

    def observe(self, text: str) -> None:
        """Accumulate output for identification. Bounded to the first few KB."""
        if self._done or len(self._banner) > 8192:
            return
        self._banner += text

    @property
    def done(self) -> bool:
        return self._done

    def stand_down(self) -> None:
        """
        Stop onboarding without running it.

        Used when the user has identified the device themselves: automatic
        identification finishing a second later and overwriting their answer
        with a lower-confidence guess would be its own small betrayal.
        """
        self._done = True

    def ready(self, at_prompt: bool) -> bool:
        """
        Whether it is time to identify the device.

        Waits for a prompt so nothing is sent mid-command, but does not wait
        forever — a device that never presents a recognisable prompt still
        deserves to be identified from its banner.
        """
        if self._done:
            return False
        elapsed = time.monotonic() - self.started_at
        if at_prompt and elapsed >= IDENTIFY_AFTER:
            return True
        return elapsed >= IDENTIFY_DEADLINE

    def run(self, prompt: str = "", auto_paging: bool = True) -> dict:
        """
        Identify the device and decide what to send it.

        Args:
            prompt: The most recent prompt, if the device has shown one.
            auto_paging: The user's "turn paging off on connect" setting.

        Returns:
            A summary for the frontend, including the command that should be
            sent (empty when nothing should be) and why, when nothing is.
        """
        self._done = True
        self.fingerprint = identify(banner=self._banner, prompt=prompt)

        summary = summarise(self.fingerprint, auto_paging)
        self.paging_command = summary["paging_command"]

        logger.info(
            "Identified device as %s (%s, confidence %.2f via %s)%s",
            summary["profile_name"], self.fingerprint.version or "version unknown",
            self.fingerprint.confidence, self.fingerprint.source,
            f"; sending '{self.paging_command}'" if self.paging_command else
            f"; sending nothing ({summary['paging_skipped']})",
        )
        return summary
