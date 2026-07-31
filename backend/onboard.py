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

from backend.fingerprint import Fingerprint, identify
from backend.platforms import get_profile

logger = logging.getLogger(__name__)

# How long to keep collecting banner text before identifying the device.
# Long enough for a login banner to finish printing, short enough that the
# session is configured before anyone starts typing.
IDENTIFY_AFTER = 1.0

# Give up waiting for a prompt after this long and identify from the banner
# alone. Some devices sit behind a terminal server that never shows one.
IDENTIFY_DEADLINE = 8.0


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

    def run(self, prompt: str = "") -> dict:
        """
        Identify the device and decide what to send it.

        Returns a summary for the frontend, including the command that should
        be sent (empty when nothing should be).
        """
        self._done = True
        self.fingerprint = identify(banner=self._banner, prompt=prompt)

        profile = get_profile(self.fingerprint.platform)
        if self.fingerprint.certain_enough_to_act and profile.paging_off:
            self.paging_command = profile.paging_off
        else:
            self.paging_command = ""

        logger.info(
            "Identified device as %s (%s, confidence %.2f via %s)%s",
            profile.name, self.fingerprint.version or "version unknown",
            self.fingerprint.confidence, self.fingerprint.source,
            f"; sending '{self.paging_command}'" if self.paging_command else
            "; sending nothing",
        )

        summary = self.fingerprint.as_dict()
        summary["profile_name"] = profile.name
        summary["paging_command"] = self.paging_command
        return summary
