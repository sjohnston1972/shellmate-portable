"""
advanced.py — Stockton. The settings that were constants in the source.

Roughly sixty values govern how ShellMate behaves — timeouts, thresholds,
buffer sizes, retry counts — and every one of them used to be a literal in a
module, reachable only by rebuilding.  Some people want to take the lid off,
and most of these are things somebody has a genuine reason to change: an old
switch that needs a legacy cipher, a satellite link that needs a longer
timeout, a fleet of two hundred devices that needs a concurrency cap.

**A registry, not a settings file with a form beside it.**  Each entry below
*is* the default the code reads.  ``backend/connections/ssh_handler.py`` asks
``advanced("ssh.connect_timeout")`` instead of holding its own constant, and
the panel renders itself from the same declaration.  Sixty hand-written HTML
rows against sixty constants would drift — silently, because the label would
go on describing something the code no longer does.

**Every value is bounded, and the bound is enforced here.**  Not by the
input's ``min`` attribute, which is a suggestion to a browser.  A setting must
never be able to stop ShellMate starting, drop a session mid-reload, or send
something wrong to a device: the worst outcome available is *degraded*.

**Some things are deliberately absent.**  The scrypt parameters, the broadcast
confirmation, the alias-line guard, the loopback binding.  Each fails that
last test, and the reasons are recorded in :data:`NOT_EXPOSED` so the panel can
say why rather than leaving somebody hunting for them in settings.json.
"""

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Setting:
    """One advanced value: its default, its bounds, and why anyone would."""

    key: str
    label: str
    default: Any
    #: "int" | "float" | "bool" | "text" | "choice" | "algorithms"
    kind: str
    summary: str
    #: The trade-off. Rendered as the row's tooltip.
    tip: str = ""
    minimum: float | None = None
    maximum: float | None = None
    unit: str = ""
    choices: tuple[str, ...] = ()
    #: For "algorithms": the name of the paramiko list to offer, and which of
    #: its entries are legacy. Enumerated at render time rather than written
    #: out here, so the picker cannot list something paramiko will not accept.
    algorithms: str = ""
    #: True when the value is read once at startup.
    restart: bool = False

    @property
    def category(self) -> str:
        return self.key.split(".", 1)[0]

    def clamp(self, value: Any) -> Any:
        """
        Coerce a value into something safe, or fall back to the default.

        Never raises. A settings file edited by hand — which is the whole
        point of the data folder — must not be able to stop the application
        reading its own configuration.
        """
        try:
            if self.kind == "bool":
                return bool(value)
            if self.kind == "algorithms":
                # Stored as a comma-separated string so an existing
                # settings.json keeps working and the value stays editable by
                # hand. Anything paramiko does not offer is dropped rather than
                # kept: a name that cannot match is indistinguishable from a
                # typo, and silently disables everything else.
                offered = set(available_algorithms(self.algorithms))
                if isinstance(value, (list, tuple, set)):
                    wanted = [str(v).strip() for v in value]
                else:
                    wanted = [p.strip() for p in str(value or "").split(",")]
                return ",".join(w for w in wanted if w and w in offered)
            if self.kind == "choice":
                text = str(value)
                return text if text in self.choices else self.default
            if self.kind == "text":
                return str(value)
            number = int(value) if self.kind == "int" else float(value)
        except (TypeError, ValueError):
            return self.default

        if self.minimum is not None:
            number = max(self.minimum, number)
        if self.maximum is not None:
            number = min(self.maximum, number)
        return int(number) if self.kind == "int" else float(number)



# ---------------------------------------------------------------------------
# The algorithm lists
#
# Read from paramiko rather than written out here. A second copy would fall out
# of step with whatever the installed version actually negotiates, and the whole
# point of these settings is reaching a device that the defaults will not — a
# picker offering a name paramiko does not know would be worse than the text
# field it replaces.
# ---------------------------------------------------------------------------

#: Which paramiko Transport list backs each of the four negotiated sets.
_ALGORITHM_LISTS = {
    "kex":     "_preferred_kex",
    "ciphers": "_preferred_ciphers",
    "macs":    "_preferred_macs",
    "keys":    "_preferred_keys",
}

#: Algorithms that exist for devices too old to offer anything better. Marked
#: rather than hidden — they are the entire reason these settings exist, so
#: this is a label, not a warning.
LEGACY_ALGORITHMS = frozenset({
    "diffie-hellman-group1-sha1",
    "diffie-hellman-group14-sha1",
    "diffie-hellman-group-exchange-sha1",
    "3des-cbc",
    "aes128-cbc", "aes192-cbc", "aes256-cbc",
    "hmac-sha1", "hmac-sha1-96", "hmac-md5", "hmac-md5-96",
    "ssh-rsa", "ssh-dss",
})


def available_algorithms(which: str) -> list[str]:
    """
    What paramiko will negotiate, in its own order of preference.

    That order is meaningful, so it is preserved rather than sorted —
    alphabetical would put the weakest first.

    Returns an empty list if the attribute cannot be read. These are paramiko
    internals: stable for years, but a picker with nothing in it is a better
    failure than an exception, and the interface falls back to a plain text
    field when the list is empty.
    """
    attribute = _ALGORITHM_LISTS.get(which)
    if not attribute:
        return []
    try:
        import paramiko

        return list(getattr(paramiko.Transport, attribute))
    except Exception as exc:                              # pragma: no cover
        logger.info("Could not read paramiko's %s list: %s", which, exc)
        return []


CATEGORIES = {
    "identify":  "Device identification",
    "ssh":       "SSH, telnet and serial",
    "terminal":  "Terminal and rendering",
    "history":   "Recording and history",
    "capture":   "Configuration capture",
    "alerts":    "Alerts",
    "broadcast": "Broadcast",
    "ai":        "AI assistant",
    "files":     "Files and interface",
    "diag":      "Diagnostics",
}


SETTINGS: tuple[Setting, ...] = (
    # --- Device identification ---------------------------------------------
    Setting("identify.act_threshold", "Confidence needed to act", 0.6, "float",
            "How sure ShellMate must be before it sends a device anything.",
            "A single-vendor estate can safely act on weaker evidence — every "
            "device really is the platform the prompt suggests.||Floored at 0.4 "
            "on purpose. At zero, an *unidentified* device would be acted on, "
            "which is the one thing the generic profile exists to prevent.",
            minimum=0.4, maximum=0.95),
    Setting("identify.wait_seconds", "Wait before identifying", 1.0, "float",
            "How long to collect banner text before deciding what the device is.",
            "A device with a long login banner needs longer before the guess is "
            "worth making.", minimum=0.2, maximum=10, unit="s"),
    Setting("identify.deadline_seconds", "Give up waiting for a prompt", 8.0, "float",
            "Identify from the banner alone after this long.",
            "Terminal servers frequently never present a prompt ShellMate "
            "recognises. Without this it would wait forever.",
            minimum=2, maximum=60, unit="s"),
    Setting("identify.banner_bytes", "Banner text to collect", 8192, "int",
            "How much output to keep while working out what the device is.",
            "A very long legal banner can push the version string out of the "
            "window, leaving a device identifiable only by its prompt.",
            minimum=1024, maximum=65536, unit="bytes"),

    # --- Connections -------------------------------------------------------
    Setting("ssh.connect_timeout", "SSH connect timeout", 15, "int",
            "How long to wait for a device to answer before giving up.",
            "Raise it for a satellite link or a device behind a slow terminal "
            "server. Lower it to fail fast on a subnet you expect to be down.",
            minimum=3, maximum=120, unit="s"),
    Setting("ssh.keepalive_seconds", "SSH keepalive", 0, "int",
            "Send a keepalive this often. Zero switches it off.",
            "The most useful setting here. Firewalls and jump hosts idle a "
            "session out mid-change, and the first you know is a dead prompt. "
            "30 to 60 seconds suits most of them.",
            minimum=0, maximum=300, unit="s"),
    Setting("ssh.read_timeout", "Read poll interval", 0.5, "float",
            "How long each read waits before checking whether to stop.",
            "Trades responsiveness against idle CPU across many open tabs. "
            "Lower feels marginally snappier and costs more.",
            minimum=0.05, maximum=5, unit="s"),
    Setting("ssh.kex_algorithms", "Key exchange algorithms", "", "algorithms",
            "Nothing ticked offers paramiko's own set, which is what you want "
            "unless a device refuses it.",
            "Legacy kit needs `diffie-hellman-group1-sha1`, which modern "
            "paramiko will not offer by default — so those devices are "
            "otherwise unreachable with no way to fix it.||Ticking anything at "
            "all offers *only* what is ticked. Clear it to recover.",
            algorithms="kex"),
    Setting("ssh.ciphers", "Ciphers", "", "algorithms",
            "Nothing ticked offers paramiko's own set.",
            "Same reason as the key exchange list: very old devices offer only "
            "ciphers that have since been dropped from the defaults.",
            algorithms="ciphers"),
    Setting("ssh.macs", "Message authentication codes", "", "algorithms",
            "Nothing ticked offers paramiko's own set.",
            "A device old enough to need a legacy key exchange frequently needs "
            "`hmac-sha1` as well — fixing one and not the other leaves the same "
            "device unreachable for a different reason.",
            algorithms="macs"),
    Setting("ssh.host_key_algorithms", "Host key algorithms", "", "algorithms",
            "Nothing ticked offers paramiko's own set.",
            "The last of the four negotiated lists. `ssh-rsa` is the one older "
            "kit tends to need.",
            algorithms="keys"),
    Setting("ssh.host_key_policy", "Unknown host keys", "auto-add", "choice",
            "What to do when a device's key has not been seen before.",
            "`auto-add` accepts and remembers it, which is what makes first "
            "connections work.||`reject` is the right answer on a managed "
            "estate where every device's key is already known.",
            choices=("auto-add", "warn", "reject")),
    Setting("ssh.look_for_keys", "Try keys in ~/.ssh", True, "bool",
            "Let paramiko offer keys it finds in the usual place.",
            "Off, only a key you name explicitly is used — which makes an "
            "authentication failure mean what it says."),
    Setting("ssh.telnet_autologin_deadline", "Telnet auto-login window", 30, "int",
            "Stop answering login prompts after this long. Zero disables it.",
            "The guard exists so that a prompt-shaped line arriving an hour "
            "into a session cannot type a password into a live device.",
            minimum=0, maximum=120, unit="s"),
    Setting("ssh.serial_wake_on_connect", "Send a carriage return on connect", True, "bool",
            "Nudges a serial console into printing its prompt.",
            "Most devices need it to say anything at all. A few react badly."),

    # --- Terminal ----------------------------------------------------------
    Setting("terminal.renderer", "Renderer", "canvas", "choice",
            "How xterm.js draws the terminal.",
            "`canvas` suits almost everything. `dom` for a VM with no GPU, "
            "where canvas can be slower. `webgl` for a very busy session.",
            choices=("dom", "canvas", "webgl"), restart=True),
    Setting("terminal.min_contrast", "Minimum contrast ratio", 1.0, "float",
            "Force readability when a device sends unreadable colours.",
            "Some devices print dark blue on black. 1 leaves colours exactly as "
            "sent; 4.5 is the accessibility threshold; 21 forces black or white.",
            minimum=1, maximum=21),
    Setting("terminal.word_separators", "Word separators", " ()[]{}',\"`", "text",
            "Characters that end a word when you double-click.",
            "Remove `/` and `.` and a double-click selects `Gi0/1` or "
            "`10.1.1.1/24` whole, which is usually what you wanted."),
    Setting("terminal.scroll_sensitivity", "Scroll sensitivity", 1, "int",
            "Lines per notch of the wheel.",
            "", minimum=1, maximum=10, unit="lines"),
    Setting("terminal.paste_confirm_lines", "Confirm a paste of", 1, "int",
            "Ask before pasting this many lines or more.",
            "Every multi-line paste asks today. Somebody pasting short "
            "configuration blocks all day may want 5.",
            minimum=1, maximum=1000, unit="lines"),
    Setting("terminal.paste_chunk_bytes", "Paste chunk size", 0, "int",
            "Break a large paste into pieces. Zero sends it in one go.",
            "Some devices drop characters when a paste arrives faster than "
            "their input buffer drains.",
            minimum=0, maximum=65536, unit="bytes"),
    Setting("terminal.paste_chunk_delay", "Delay between paste chunks", 0, "int",
            "Wait this long between pieces.",
            "Only does anything when a chunk size is set.",
            minimum=0, maximum=2000, unit="ms"),

    # --- History -----------------------------------------------------------
    Setting("history.record", "Record sessions", True, "bool",
            "Write commands and output to the history database.",
            "Off, nothing is recorded at all — no search, no replay, and no "
            "drift check. What is already stored is kept."),
    Setting("history.max_output_chars", "Output stored per command", 262144, "int",
            "Anything beyond this is truncated in the record.",
            "A full `show tech-support` is larger than this and is cut short "
            "today. Raising it makes the database grow faster.",
            minimum=4096, maximum=8388608, unit="bytes"),
    Setting("history.buffer_lines", "Scrollback kept per session", 5000, "int",
            "Lines held in memory, which is what the assistant can reach.",
            "Separate from the terminal's own scrollback. Across a dozen tabs "
            "this is the setting that costs memory.",
            minimum=100, maximum=500000, unit="lines"),
    Setting("history.retention_days", "Discard history after", 0, "int",
            "Zero keeps it forever.",
            "Nothing prunes the database today. Worth setting if ShellMate "
            "lives on a stick with limited room.",
            minimum=0, maximum=3650, unit="days"),
    Setting("history.prompt_pattern", "Prompt pattern override", "", "text",
            "A regular expression replacing the built-in prompt detector.",
            "The deepest setting here. One pattern drives the whole transcript "
            "layer — which text is a command, and where its output ends.||An "
            "invalid expression falls back to the built-in with a log line, "
            "never an unusable session. Blank restores it.",
            restart=True),

    # --- Capture -----------------------------------------------------------
    Setting("capture.timeout", "Capture timeout", 60, "int",
            "How long to wait for a configuration to finish printing.",
            "A large chassis over a slow WAN genuinely takes this long.",
            minimum=5, maximum=600, unit="s"),
    Setting("capture.idle_settle", "Quiet period before finishing", 1.5, "float",
            "How long the device must be silent before the capture is done.",
            "Long enough to survive a pause mid-transfer, short enough not to "
            "feel stuck. Raise it on a link that stutters.",
            minimum=0.2, maximum=10, unit="s"),
    Setting("capture.start_delay", "Delay before the check starts", 2500, "int",
            "Wait after connecting before asking for the configuration.",
            "A device still printing its banner will not answer cleanly.",
            minimum=0, maximum=60000, unit="ms"),
    Setting("capture.diff_context", "Diff context lines", 3, "int",
            "Unchanged lines shown either side of a change.",
            "More context makes a change easier to place and the diff longer.",
            minimum=0, maximum=20, unit="lines"),

    # --- Alerts ------------------------------------------------------------
    Setting("alerts.thresholds", "Escalation thresholds", "600,300,60,10", "text",
            "Seconds remaining at which each warning fires, largest first.",
            "Somebody who wants a nudge half an hour out would add 1800.||A "
            "malformed list falls back to the default rather than leaving a "
            "pending reload unannounced."),
    Setting("alerts.flash_within", "Flash the tab within", 300, "int",
            "Only pulse the tab inside this window.",
            "A tab that pulses for ten minutes is one people stop seeing.",
            minimum=0, maximum=3600, unit="s"),
    Setting("alerts.stale_hours", "Forget a pending action after", 6, "int",
            "Drop an alert nothing has been heard about for this long.",
            "Something was missed — the device rebooted and the socket "
            "survived, or the cancellation scrolled past. A countdown stuck at "
            "zero all afternoon is worse than none.",
            minimum=1, maximum=168, unit="hours"),
    Setting("alerts.max_toasts", "Toasts on screen at once", 3, "int",
            "Older ones are removed as new arrive.",
            "A stack that grows without limit stops being readable and starts "
            "being a wall.", minimum=1, maximum=10),
    Setting("alerts.toast_seconds", "How long a toast stays", 12, "int",
            "For ordinary alerts. The final warning stays until dismissed.",
            "", minimum=1, maximum=600, unit="s"),
    Setting("alerts.tone_volume", "Tone volume", 0.2, "float",
            "How loud the warning tone is.",
            "Audible over a noisy comms room, or barely there in an office.",
            minimum=0, maximum=1),
    Setting("alerts.commit_confirm_minutes", "Junos commit-confirm default", 10, "int",
            "Assumed when `commit confirmed` is given with no number.",
            "Ten is the Junos default. Change it if your estate differs.",
            minimum=1, maximum=120, unit="min"),

    # --- Broadcast ---------------------------------------------------------
    Setting("broadcast.max_seconds", "Whole-run timeout", 180, "int",
            "Abandon a broadcast still running after this long.",
            "A long sequence across a fleet legitimately takes minutes. The cap "
            "stops a mistyped wait holding the connection open indefinitely.",
            minimum=10, maximum=3600, unit="s"),
    Setting("broadcast.default_wait", "Default wait between commands", 500, "int",
            "The starting value in the panel. Snippets carry their own.",
            "", minimum=0, maximum=60000, unit="ms"),
    Setting("broadcast.concurrency", "Devices at once", 0, "int",
            "Zero means no limit.",
            "Two hundred devices at once will bury a jump host, and every one "
            "of them is a separate SSH channel.",
            minimum=0, maximum=500),
    Setting("broadcast.stop_on_error", "Stop a device on first failure", True, "bool",
            "Abandon the rest of the sequence on that device.",
            "The rest of a sequence rarely makes sense once a step has failed, "
            "and continuing is how half-applied changes happen.||Off is "
            "occasionally what you want for a run of read-only commands."),

    # --- AI ----------------------------------------------------------------
    Setting("ai.temperature", "Temperature", 0.2, "float",
            "How much the model varies its answers.",
            "Troubleshooting wants a consistent answer, not a creative one. "
            "Higher is worth trying when you want alternatives rather than the "
            "single most likely reply.", minimum=0, maximum=2),
    Setting("ai.max_tokens", "Maximum response length", 2048, "int",
            "The ceiling on one reply.",
            "Raise it if answers are being cut off mid-sentence. Every token "
            "costs, on a metered provider.",
            minimum=256, maximum=32768, unit="tokens"),
    Setting("ai.context_lines", "Terminal lines sent as context", 200, "int",
            "Recent output given to the assistant with each question.",
            "More context means better answers and more tokens. Zero sends "
            "none, which makes the assistant a general-purpose one.",
            minimum=0, maximum=5000, unit="lines"),
    Setting("ai.context_commands", "Commands of history sent", 30, "int",
            "How many recent commands are listed alongside the output.",
            "", minimum=0, maximum=500),
    Setting("ai.request_timeout", "Request timeout", 120, "int",
            "How long to wait for a provider to finish answering.",
            "A long reply from a slow local model can legitimately take a "
            "while.", minimum=5, maximum=600, unit="s"),
    Setting("ai.redact_context", "Mask secrets in what is sent", True, "bool",
            "Apply the same redaction that covers session logs.",
            "Terminal output going to a third-party API is at least as "
            "deserving of it as a log file.||Turning it off is reasonable when "
            "the model is Ollama on your own machine and nothing is leaving. "
            "It is not reasonable with a cloud provider."),

    # --- Files and interface ------------------------------------------------
    Setting("files.max_upload_mb", "Maximum SFTP upload", 2048, "int",
            "Refuse anything larger.",
            "Device images are large; the cap exists so a mis-selected file "
            "cannot fill a switch's flash.",
            minimum=1, maximum=65536, unit="MB"),
    Setting("files.show_hidden", "Show hidden files when browsing", False, "bool",
            "Dot-files in the local file picker.",
            "`~/.ssh` is hidden on every platform, which is exactly where keys "
            "live."),
    Setting("files.note_seconds", "How long a device note stays", 3.5, "float",
            "The line in the corner saying what was sent on connect.",
            "Long enough to read is the only requirement, and reading speed "
            "differs.", minimum=0.5, maximum=60, unit="s"),
    Setting("files.drift_banner_seconds", "How long an unchanged-config note stays", 8, "int",
            "A changed configuration always waits to be dismissed.",
            "", minimum=1, maximum=120, unit="s"),

    # --- Diagnostics --------------------------------------------------------
    Setting("diag.log_level", "Log level", "INFO", "choice",
            "How much ShellMate writes to its own log.",
            "`DEBUG` is what support will ask for. It is noisy and the log is "
            "rewritten each launch, so it costs nothing to leave on for one "
            "run.", choices=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"),
            restart=True),
    Setting("diag.http_access_log", "Log every HTTP request", False, "bool",
            "Records each call the interface makes to its own API.",
            "Useful when something in the interface is not reaching the "
            "backend. Very noisy otherwise.", restart=True),
    Setting("diag.port_scan_attempts", "Ports to try if 8765 is busy", 20, "int",
            "ShellMate walks upward from its preferred port.",
            "", minimum=1, maximum=100, restart=True),
)

SETTINGS_BY_KEY = {s.key: s for s in SETTINGS}


#: What was left out, and why. Shown in the panel so nobody goes looking.
NOT_EXPOSED: tuple[tuple[str, str], ...] = (
    ("Vault key-derivation parameters",
     "Lowering them weakens every stored credential, and changing them at all "
     "makes an existing vault unreadable. No good outcome, several bad ones."),
    ("The broadcast confirmation",
     "It is the whole safety model of that feature — every command against "
     "every device, named before anything is sent. A tool that can broadcast "
     "without showing you what and where first is a different tool."),
    ("The alias-line length guard",
     "It stops a pasted block being mistaken for an alias and rewritten. "
     "Nothing to gain by changing it."),
    ("Binding beyond 127.0.0.1",
     "There is no authentication, precisely because nothing but this machine "
     "can reach the port. The two go together."),
    ("Redaction in session logs",
     "Already an ordinary setting under Session Logging. A second switch for "
     "the same promise is a promise nobody can rely on."),
)


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def get(key: str) -> Any:
    """
    The effective value for one setting.

    This is what the rest of the codebase calls instead of holding a constant.
    Unknown keys raise, because a typo here would silently return None and
    produce a timeout of zero somewhere.
    """
    setting = SETTINGS_BY_KEY.get(key)
    if setting is None:
        raise KeyError(f"'{key}' is not an advanced setting.")

    from backend.settings_store import get_settings

    stored = (get_settings().get("advanced") or {})
    if key not in stored:
        return setting.default
    return setting.clamp(stored[key])


def all_values() -> dict[str, Any]:
    """Every setting's effective value."""
    return {s.key: get(s.key) for s in SETTINGS}


def describe() -> dict:
    """The registry, for the panel. Renders itself from this."""
    from backend.settings_store import get_settings

    stored = (get_settings().get("advanced") or {})
    return {
        "categories": CATEGORIES,
        "not_exposed": [{"label": label, "why": why} for label, why in NOT_EXPOSED],
        "settings": [
            {
                "key":      s.key,
                "category": s.category,
                "label":    s.label,
                "kind":     s.kind,
                "summary":  s.summary,
                "tip":      s.tip,
                "default":  s.default,
                "value":    get(s.key),
                "modified": s.key in stored and s.clamp(stored[s.key]) != s.default,
                "min":      s.minimum,
                "max":      s.maximum,
                "unit":     s.unit,
                "choices":  list(s.choices),
                "algorithms": (
                    [{"name": name, "legacy": name in LEGACY_ALGORITHMS}
                     for name in available_algorithms(s.algorithms)]
                    if s.kind == "algorithms" else []
                ),
                "restart":  s.restart,
            }
            for s in SETTINGS
        ],
    }


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def update(values: dict) -> dict:
    """
    Store a partial update, clamped.

    Clamping happens *here* rather than trusting the browser, because the
    browser is not the only caller — the API is documented and scriptable, and
    settings.json is a text file people are encouraged to edit.

    Unknown keys are dropped rather than stored: a typo left in the file would
    otherwise look like a setting that simply does nothing.
    """
    from backend.settings_store import get_settings, update_settings

    current = dict(get_settings().get("advanced") or {})
    for key, value in (values or {}).items():
        setting = SETTINGS_BY_KEY.get(key)
        if setting is None:
            logger.info("Ignoring unknown advanced setting '%s'", key)
            continue
        current[key] = setting.clamp(value)

    update_settings({"advanced": current})
    return describe()


def reset(key: str | None = None, category: str | None = None) -> dict:
    """
    Put things back.

    One setting, one category, or all of them. This is the way out of any
    change made here, which is why it exists at three granularities — having
    to undo sixty rows by hand to recover from one is not a way back.
    """
    from backend.settings_store import get_settings, update_settings

    current = dict(get_settings().get("advanced") or {})

    if key is not None:
        current.pop(key, None)
    elif category is not None:
        for candidate in [k for k in current if k.split(".", 1)[0] == category]:
            current.pop(candidate, None)
    else:
        current = {}

    update_settings({"advanced": current})
    return describe()
