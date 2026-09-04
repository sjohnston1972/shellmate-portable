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

The same three rules govern :class:`OnConnectScript` at the foot of this
module (#532) — the lines a saved connection sends itself once the paging
command has gone out. Same file because it is the same decision made twice:
what may be typed into somebody's session before they have touched it.
"""

import logging
import re
import time
from dataclasses import dataclass, field

from backend.fingerprint import Fingerprint, identify
from backend.platforms import GENERIC, get_profile
from backend.session.ansi import strip_ansi

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
    # The *configured* threshold (#326), not the constant: quoting 0.6 to
    # someone who raised it in Stockton is the label-drift CLAUDE.md warns
    # about — an explanation citing a number the gate is not using.
    from backend.advanced import get as advanced
    summary["act_threshold"] = advanced("identify.act_threshold")

    # How many aliases the identification just brought into force.
    #
    # Identifying the platform is what makes alias expansion work at all —
    # pipeline.platform is set from this same summary, and expansion is keyed
    # on it — so the two are one event to the user even though they are two
    # things in the code. The count only ever appeared in the branches where
    # *nothing* was sent ("nothing to send, aliases are active"), which is the
    # rarest case; the ordinary one announced the paging command and said
    # nothing about the aliases it had just switched on.
    #
    # Note this is what the *platform* offers, not what the user's setting
    # allows. `terminal.expand_aliases` can be off, and only the frontend
    # knows that — so it decides whether to mention them, and this stays a
    # plain fact about the profile.
    summary["alias_count"] = len(profile.aliases)
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
        from backend.advanced import get as advanced

        if self._done or len(self._banner) > advanced("identify.banner_bytes"):
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
        from backend.advanced import get as advanced

        elapsed = time.monotonic() - self.started_at
        if at_prompt and elapsed >= advanced("identify.wait_seconds"):
            return True
        return elapsed >= advanced("identify.deadline_seconds")

    def run(self, prompt: str = "", auto_paging: bool = True,
            remembered: str = "") -> dict:
        """
        Identify the device and decide what to send it.

        Args:
            prompt: The most recent prompt, if the device has shown one.
            auto_paging: The user's "turn paging off on connect" setting.
            remembered: What the user last said this device is, if anything.

        Returns:
            A summary for the frontend, including the command that should be
            sent (empty when nothing should be) and why, when nothing is.
        """
        self._done = True
        self.fingerprint = identify(banner=self._banner, prompt=prompt)

        # A remembered answer beats a guess and loses to direct evidence.
        #
        # It beats a prompt-shape guess because that is the whole point: the
        # devices somebody bothers to identify by hand are the ones a prompt
        # can never settle, and a remembered value carries confidence 1.0
        # precisely because it was not inferred.
        #
        # It loses to a confident banner because a device that used to answer
        # as an ASA and now announces itself as IOS has most likely been
        # replaced, and the banner is evidence about the device as it is now.
        # Either way the summary says which happened — silently preferring one
        # is the thing that would leave somebody unable to explain why a
        # command went out.
        remembered_used = False
        overridden = ""
        if remembered:
            banner_is_direct = (self.fingerprint.source == "banner"
                                and self.fingerprint.certain_enough_to_act)
            if banner_is_direct and self.fingerprint.platform != remembered:
                overridden = remembered
            elif not banner_is_direct:
                try:
                    self.fingerprint = as_chosen(remembered)
                    remembered_used = True
                except ValueError:
                    # platforms.json edited, or the profile carried to an
                    # installation without that platform. Falling back to
                    # automatic identification is the right failure.
                    logger.info("Remembered platform %r is not one this "
                                "installation knows; identifying normally",
                                remembered)

        summary = summarise(self.fingerprint, auto_paging)
        summary["remembered"] = remembered_used
        summary["remembered_overridden"] = overridden
        self.paging_command = summary["paging_command"]

        logger.info(
            "Identified device as %s (%s, confidence %.2f via %s)%s",
            summary["profile_name"], self.fingerprint.version or "version unknown",
            self.fingerprint.confidence, self.fingerprint.source,
            f"; sending '{self.paging_command}'" if self.paging_command else
            f"; sending nothing ({summary['paging_skipped']})",
        )
        return summary


# ---------------------------------------------------------------------------
# The on-connect script (#532)
#
# The first thirty seconds on every device are the same thirty seconds:
# `enable`, `terminal monitor`, a screen width, entering a VDOM or a context.
# SecureCRT calls it a logon script and Netmiko calls it session_preparation();
# both exist because people were typing the same four lines forty times a day.
#
# It is also the honest answer for the platforms where paging-off cannot be a
# profile default — FortiOS and MikroTik set it per user or per context, so
# the platform profile has nothing correct to send and the connection does.
#
# Everything here obeys the two rules onboarding obeys, for the same reasons:
#
# **Nothing is sent silently.** Every line is typed into the session where the
# user can see it and the device echoes it, and the script is announced as a
# whole — what will be sent, what was, and anything that was not.
#
# **Nothing is guessed.** A line goes out only when the device is idle at a
# bare prompt with nothing half-typed, one line per prompt, and through the
# outbound pipeline — so the dangerous-command guardrail still holds a
# `reload` written into a script exactly as it holds one that was typed.
# ---------------------------------------------------------------------------

#: Give up on the rest of a script after this long with no prompt. A device
#: that stops answering mid-script is a device the rest of the script must not
#: be fired at the moment it comes back — thirty seconds later, into whatever
#: the user has started doing.
ON_CONNECT_DEADLINE = 20.0

#: How long to wait for the `Password:` an `enable` usually produces. Bounded
#: and answered once, exactly as telnet auto-login is: a prompt regex that
#: stays armed will eventually match ordinary output and type a password into
#: a live device.
ENABLE_ANSWER_DEADLINE = 8.0

#: The line that means "and then the enable password".
_ENABLE_RE = re.compile(r"^(?:enable|en)(?:\s+\d+)?$", re.IGNORECASE)

#: What a device asks when it wants that password. Anchored to the end of the
#: output, because it is only ever tested against the tail of what has just
#: arrived.
_PASSWORD_RE = re.compile(r"pass(?:word|code|phrase)\s*:\s*$", re.IGNORECASE)


@dataclass
class OnConnectScript:
    """
    A saved connection's own lines, sent once, after onboarding.

    Owns only the decision of *what to send next*; the session's read loop
    owns the sending, because that is where the pipeline and the socket are.
    """

    lines: list[str] = field(default_factory=list)
    started_at: float = field(default_factory=time.monotonic)

    sent: list[str] = field(default_factory=list, init=False)
    _index: int = field(default=0, init=False)
    _done: bool = field(default=False, init=False)
    _reason: str = field(default="", init=False)
    _last_progress: float = field(default_factory=time.monotonic, init=False)

    # Waiting on the `Password:` an `enable` produces, and the tail of output
    # to test against. Both cleared the moment a prompt appears instead.
    _awaiting_password: bool = field(default=False, init=False)
    _asked_at: float = field(default=0.0, init=False)
    _tail: str = field(default="", init=False)

    # Whether the device has said anything since the last line went out.
    #
    # Without this the script races its own output. `idle_at_prompt` is only
    # recomputed when output arrives, so in the half-second after a line is
    # sent it still describes the prompt *before* it — and the next idle tick
    # would fire the next line into a device that has not answered the first,
    # which is the whole thing this feature must not do. True to begin with,
    # because the first line is sent at a prompt the device printed itself.
    _saw_output: bool = field(default=True, init=False)

    @property
    def done(self) -> bool:
        return self._done

    @property
    def remaining(self) -> list[str]:
        return list(self.lines[self._index:])

    @property
    def awaiting_password(self) -> bool:
        """True while an `enable` is waiting for its password prompt."""
        return self._awaiting_password

    def finish(self, reason: str = "") -> None:
        """Stop, with a reason the interface can state."""
        if not self._done:
            self._done = True
            self._reason = reason

    def next_line(self, at_prompt: bool, now: float | None = None) -> str | None:
        """
        The next line to send, or None if it is not time.

        Args:
            at_prompt: The device is idle at a bare prompt and nothing is
                half-typed — the caller resolves that, because it is the only
                thing holding both the transcript and the pipeline.
        """
        if self._done:
            return None
        clock = time.monotonic() if now is None else now

        # Nothing is believed about the device until it has spoken since
        # the last line. See _saw_output.
        settled = at_prompt and self._saw_output

        if settled:
            # A prompt after `enable` means the device did not ask for a
            # password — the session was privileged already. Carry on rather
            # than sitting out the deadline.
            self._awaiting_password = False

        if self._awaiting_password:
            if clock - self._asked_at > ENABLE_ANSWER_DEADLINE:
                self._awaiting_password = False
                self.finish("no-password-prompt")
            return None

        if self._index >= len(self.lines):
            self.finish("")
            return None

        if not settled:
            if clock - self._last_progress > ON_CONNECT_DEADLINE:
                self.finish("no-prompt")
            return None

        line = self.lines[self._index]
        self._index += 1
        self._last_progress = clock
        self._saw_output = False
        self.sent.append(line)
        if _ENABLE_RE.match(line):
            self._awaiting_password = True
            self._asked_at = clock
            self._tail = ""
        return line

    def observe(self, text: str) -> bool:
        """
        Feed device output. True when an enable password should be sent now.

        Every chunk goes through here while the script runs, because the
        arrival of *anything* is what says the device has answered the last
        line. The password half is only consulted while an `enable` is
        outstanding, and it disarms itself the moment it says yes — one
        answer per script, at a prompt we went looking for, rather than a
        pattern left running for the life of the session.
        """
        if self._done:
            return False
        if text:
            self._saw_output = True
        if not self._awaiting_password:
            return False
        self._tail = (self._tail + strip_ansi(text or ""))[-200:]
        if not _PASSWORD_RE.search(self._tail.rstrip("\r\n")):
            return False
        self._awaiting_password = False
        self._tail = ""
        self._last_progress = time.monotonic()
        return True

    def wait_for_device(self) -> None:
        """
        Hold everything until the device has spoken again.

        Told rather than inferred, because the script does not do its own
        sending. Anything typed into the session from outside this class
        leaves `idle_at_prompt` describing the prompt *before* it for as long
        as the device takes to answer — so the paging command at the end of
        onboarding, and the enable password, both have to say so, or the
        first line of the script goes out on top of them.
        """
        self._saw_output = False
        self._last_progress = time.monotonic()

    def answered(self) -> None:
        """The caller has typed the enable password."""
        self.wait_for_device()

    def summary(self) -> dict:
        """
        What was sent, what was not, and why — for the interface and the log.

        The skipped half is the half that matters, exactly as it is for the
        paging command: a script that stopped after two of five lines because
        the device never came back to a prompt reads as complete success
        unless somebody says otherwise, and the user is the one who has to
        account for the session afterwards.
        """
        return {
            "sent":    list(self.sent),
            "skipped": self.remaining,
            "reason":  self._reason,
        }
