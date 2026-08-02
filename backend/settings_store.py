"""
settings_store.py — Application settings persistence for ShellMate.
Settings are stored in settings.json in the portable data directory
(see backend/paths.py).

Also provides effective-config helpers — settings.json overrides .env values
for API keys, model URLs, and the Chroma DB URL.
"""
import json
import logging
from pathlib import Path

from backend import config as env_config
from backend import paths
from backend.vault import VaultError, vault

logger = logging.getLogger(__name__)

DEFAULT_SETTINGS: dict = {
    "terminal": {
        "font_family": "JetBrains Mono, Fira Code, Consolas, monospace",
        "font_size": 14,
        "line_height": 1.2,
        "cursor_style": "block",
        "cursor_blink": True,
        # A bar cursor at the default width disappears against a busy screen.
        # 0 means "leave it to xterm.js", which is not the same as 1.
        "cursor_width": 0,
        # Empty means "follow the colour scheme". An override here is the same
        # arrangement foreground and background already have.
        "cursor_colour": "",
        "selection_colour": "",
        # A real readability lever on condensed monospace faces, and the one
        # people reach for before changing the font.
        "letter_spacing": 0,
        # Some faces are unreadably light at 13px. Bold is separate because
        # network output leans on it for headings and status.
        "font_weight": "normal",
        "font_weight_bold": "bold",
        # Genuinely contentious. On, bold text is also brightened, which is
        # the traditional terminal behaviour and what most schemes assume.
        "draw_bold_in_bright": True,
        # Configuration output aligned to 8 wraps differently at 4.
        "tab_stop_width": 8,
        # An accessibility gap rather than a preference: xterm.js maintains a
        # live region for screen readers only when this is on, and it costs
        # enough that it is off by default.
        "screen_reader_mode": False,
        "scrollback_lines": 5000,
        "right_click_paste": True,
        "copy_on_select": False,
        # Expand short aliases ("ints") into the right command for whatever
        # platform the tab is connected to.
        "expand_aliases": True,
        # Send the platform's paging-off command on connect, so nobody types
        # "terminal length 0" a hundred times a week.
        "auto_paging_off": True,
        # Keep a session from being idled out by the *device* (#137).
        #
        # Off by default, and it must stay that way: this is the one feature
        # that types into a live session on a timer. `ssh.keepalive_seconds`
        # already handles the network half — a firewall or jump host dropping
        # an idle TCP connection — but an SSH keepalive never reaches the
        # shell, so it does nothing about `exec-timeout`, which is what
        # actually closes the session on IOS. Only input the device sees
        # resets that.
        "keep_alive": False,
        # How long a session must be silent before a nudge. Well under a
        # typical `exec-timeout 5`, and long enough that an active session
        # never triggers it.
        "keep_alive_seconds": 120,
    },
    # Regex rules that colour terminal output. Applied to plain text only, so
    # colour a device sends itself is never disturbed.
    "highlight": {
        "enabled": True,
        "rules": [
            {"pattern": r"\b(down|err-disabled|failed|failure|denied|unreachable)\b",
             "colour": "red", "ignore_case": True},
            {"pattern": r"\b(error|errors|CRC|drop|drops|discard|discards)\b",
             "colour": "orange", "ignore_case": True},
            {"pattern": r"\b(up|connected|established|active|success|ok)\b",
             "colour": "green", "ignore_case": True},
            {"pattern": r"\b(warning|notice|shutdown|disabled)\b",
             "colour": "yellow", "ignore_case": True},
        ],
    },
    "logging": {
        "enabled": False,
        "directory": "logs",
        # Mask credentials in written logs. On by default: a session log is
        # meant to be handed to someone else, and devices echo.
        "redact_secrets": True,

        # --- Configuration capture -------------------------------------
        #
        # On connect, fetch the device's running configuration on a second
        # channel — invisibly, never in the user's own session — and store it.
        # On by default because it is what the drift check on connect is built
        # from, and that has always run.
        "capture_configs": True,
        # Keeping a copy as a *file* is the part that writes to somewhere the
        # user chose, so it waits to be asked for.
        "save_config_files": False,
        # Separate from the session-log directory: these are artefacts, not a
        # transcript, and mixing them makes both harder to search.
        "config_directory": "configs",
        # Retention. A capture per login on a fleet of switches is unbounded
        # otherwise, and the folder may well be a network share.
        "config_keep_per_device": 20,
        "config_max_age_days": 365,
        "config_max_total_mb": 200,
        # Offer the diff when the configuration has changed since last time.
        "diff_on_connect": True,
    },
    "appearance": {
        "color_scheme": "deep_space",
    },
    # The application around the terminal.
    #
    # Everything here used to live in the browser's localStorage, which quietly
    # broke a promise the manual makes: move ShellMate-Data and your setup
    # moves with it. It did travel under the native window, whose storage sits
    # inside the data folder — but opening http://localhost:8765 in a browser
    # gave a different set of preferences, and nothing reconciled the two.
    "interface": {
        # "dark" | "light" | "system"
        "theme": "dark",
        # Which split layout to open with. See frontend/js/layout.js.
        "default_layout": "single",
        # Fraction of the window the chat pane takes. Dragging the divider
        # used to be forgotten the moment the page reloaded.
        "chat_pane_fraction": 0.3333,
        # Saved chat quick-buttons and the pop-out window's geometry.
        "quick_buttons": [],
        "chat_popout": None,
        # Closing a tab kills the session on the other end of it. Worth a
        # question when that session is still connected.
        "confirm_close_tab": True,
        # Quit is the one action that really does drop every device, which is
        # precisely what closing the window was designed not to do.
        "confirm_quit": True,
        # "comfortable" | "compact"
        "density": "comfortable",
        "max_tab_label_px": 160,
        "show_connection_dot": True,
        # Every sidebar item is an icon with a tooltip. The prompt editor was
        # reported as deleted when it moved behind a tuner glyph, so this is
        # one setting away rather than a redesign — off by default, because
        # the rail is deliberately narrow and most people learn the icons.
        "sidebar_labels": False,
        # Which entries the tab right-click menu offers. Every one defaults to
        # on, so an existing installation loses nothing — an upgrade that
        # silently removed menu items people use would be the worst possible
        # outcome of making them configurable. The keys are declared by
        # TAB_MENU_GROUPS in tabs.js; absent means on.
        "tab_menu": {},
        # Per-device terminal colour schemes, keyed "address:port" (#139).
        # Against the address rather than the session, because a session id
        # does not survive a reconnect and the thing being marked is the
        # device — and because an ad-hoc connection with no saved profile
        # should be markable too.
        "tab_schemes": {},
        # Reopen the tabs that were open at quit. Off, and it stays off on
        # upgrade: this reconnects to devices nobody asked to connect to,
        # which is the same objection that keeps auto-reconnect off. It only
        # restores connections whose credentials the server already holds,
        # and names the ones it could not.
        "restore_tabs": False,
        # What the open tabs were, so the above has something to restore.
        # Written on every change rather than at quit — a list that is only
        # correct after a clean shutdown fails exactly when it is wanted.
        "open_tabs": [],
        # "welcome" | "last" | "profile". The welcome screen is a poor answer
        # for somebody who works on one device all day and a good one for
        # somebody who does not, so it is a choice rather than a change.
        "new_tab_opens": "welcome",
        "new_tab_profile": "",
        # "manual" | "name" | "device" | "opened" | "tag". Manual is where
        # tabs have always been and stays the default: people put tabs where
        # they want them and expect them to stay there. Grouping earns its
        # keep at twenty tabs across three estates, which is also where the
        # tags already recorded in the profiles start to matter.
        "tab_order": "manual",
        # How a side panel arrives. "slide" | "fade" | "scale" | "none".
        # Slide by default because it says which edge the panel came from and
        # where Escape will send it back to. "none" is a real answer, not a
        # joke one: over Remote Desktop an animation that drops frames is
        # worse than no animation.
        "panel_transition": "slide",
        # Per-panel widths, keyed by element id, set by dragging the handle.
        # Alongside chat_pane_fraction for the same reason: a layout
        # preference should travel with the data folder rather than live in
        # one browser's local storage.
        "panel_widths": {},
    },
    # Remembered so it opens where it was left rather than at a fixed size in
    # the middle of whichever monitor Windows picks.
    "window": {
        "width": 0,          # 0 means "use the default"
        "height": 0,
        "x": None,
        "y": None,
        "start_minimised": False,
    },
    # How loudly to say that something is about to happen to a device — a
    # pending reload, a commit waiting to be confirmed. The countdown itself is
    # not switchable: it is the information, not the interruption.
    "alerts": {
        # The tab pulses over the last five minutes.
        "flash_tab": True,
        # A short synthesised tone at each threshold. No audio file is fetched.
        "sound": True,
        # A toast, which is what reaches someone looking at another tab.
        "popup": True,
        # Honours prefers-reduced-motion as well; this forces it on.
        "reduce_motion": False,
    },
    # Defaults for a serial console, so the same values are not re-entered on
    # every connection. Cisco console is 9600 8-N-1 with no flow control.
    "serial": {
        "baud_rate": 9600,
        "data_bits": 8,
        "parity": "N",
        "stop_bits": 1,
        "flow_control": "none",
    },
    # User-overridable API keys / endpoints. Empty string means "fall back
    # to whatever .env provides". Keys persisted here override .env.
    "providers": {
        "anthropic_api_key": "",
        "openai_api_key": "",
        "xai_api_key": "",
        "deepseek_api_key": "",
        "ollama_host": "",
        "chroma_url": "",
        "chroma_collection": "design_guidelines",
    },
    # Stockton: the granular settings, keyed "category.name". Empty means
    # every one of them is at its default — see backend/advanced.py, which
    # owns the defaults themselves so a constant and its description cannot
    # disagree.
    "advanced": {},
    "ai": {
        # "learn" | "tshoot" — controls which system-prompt persona is used.
        # Now a starting value rather than only a record of the last toggle:
        # somebody who always wants Learn should be able to say so once.
        "mode": "tshoot",
        # "backend:model", as the picker in the chat header spells it. Held
        # here rather than in the page, so it survives a reload and travels
        # with the data folder — which is why the quick buttons moved out of
        # localStorage, and this never followed.
        "default_model": "",
        # The assistant is optional, and off until asked for. A fresh install
        # otherwise opens with a third of the window given to a pane that
        # cannot answer anything until a provider is configured — and on a
        # locked-down network there may be no provider to configure. The
        # terminal has to stand on its own, which is the whole reason the
        # panel is optional. The assistant icon in the sidebar turns it on.
        "panel_enabled": False,
    },
}

# Defaults that have changed since release, and what they used to be.
#
# A default is only a default for an installation that has never been
# configured. Deep-merging a new default over an existing settings.json would
# reach into working setups and change them — someone who has been using the
# assistant for months would find it gone after an update, with no action of
# theirs to explain it. So an existing file keeps the old value, written in
# explicitly, and only a genuinely first run sees the new one.
LEGACY_DEFAULTS: dict[tuple[str, str], object] = {
    ("ai", "panel_enabled"): True,
}

# Which env-var name backs each provider field, for the "preconfigured by env"
# indicator the settings UI shows.
ENV_BACKED_FIELDS: dict = {
    "anthropic_api_key": "ANTHROPIC_API_KEY",
    "openai_api_key":    "OPENAI_API_KEY",
    "xai_api_key":       "XAI_API_KEY",
    "deepseek_api_key":  "DEEPSEEK_API_KEY",
    "ollama_host":       "OLLAMA_HOST",
    "chroma_url":        "CHROMA_URL",
    "chroma_collection": "CHROMA_COLLECTION",
}

SECRET_FIELDS = {
    "anthropic_api_key", "openai_api_key", "xai_api_key", "deepseek_api_key",
}


def get_settings() -> dict:
    """
    Return raw stored settings deep-merged over the defaults.

    A settings file that exists but predates a setting keeps that setting's
    old default — see :data:`LEGACY_DEFAULTS`. Only a first run, where there
    is no file at all, gets the current one.
    """
    settings_file = paths.settings_file()
    if not settings_file.exists():
        return _deep_merge(DEFAULT_SETTINGS, {})
    try:
        stored = json.loads(settings_file.read_text(encoding="utf-8"))
        return _deep_merge(DEFAULT_SETTINGS, _honour_legacy_defaults(stored))
    except Exception:
        return _deep_merge(DEFAULT_SETTINGS, {})


def _honour_legacy_defaults(stored: dict) -> dict:
    """
    Fill in what an older settings file could not have said.

    A missing key in an existing file means "this predates the setting", which
    is not the same as "the user wants whatever the default is now".
    """
    if not isinstance(stored, dict):
        return {}

    patched = dict(stored)
    for (section, key), legacy in LEGACY_DEFAULTS.items():
        values = patched.get(section)
        if not isinstance(values, dict):
            # The whole section is missing, so the file certainly predates it.
            patched[section] = {**(values if isinstance(values, dict) else {}),
                                key: legacy}
        elif key not in values:
            patched[section] = {**values, key: legacy}
    return patched


def get_settings_for_ui() -> dict:
    """
    Return settings shaped for the frontend:
      - secret API keys are masked (replaced by 8 dots) but a flag tells the UI
        whether a value is actually set
      - includes an "env_preconfigured" map listing which provider fields have
        an env var backing them so the UI can render the placeholder text
    """
    s = get_settings()
    providers = dict(s.get("providers", {}))
    out_providers: dict = {}
    has_value: dict = {}
    for k, v in providers.items():
        # A secret's real home is the vault, so "is it set?" has to be asked
        # there. settings.json is only consulted as a fallback for a key that
        # predates the vault and has not been migrated yet.
        stored = vault.get(k) if k in SECRET_FIELDS else ""
        effective = stored or v

        if k in SECRET_FIELDS and effective:
            out_providers[k] = "•" * 8
        else:
            out_providers[k] = v or ""
        has_value[k] = bool(effective)

    env_preconfigured: dict = {}
    for field, env_name in ENV_BACKED_FIELDS.items():
        env_preconfigured[field] = bool(getattr(env_config, env_name, "") if hasattr(env_config, env_name) else "")
        # CHROMA_URL/CHROMA_COLLECTION read directly from os.getenv since they
        # weren't in config.py originally
        if field in ("chroma_url", "chroma_collection"):
            import os
            env_preconfigured[field] = bool(os.getenv(env_name, ""))

    s_out = dict(s)
    s_out["providers"] = out_providers
    s_out["providers_has_value"] = has_value
    s_out["env_preconfigured"] = env_preconfigured
    return s_out


def update_settings(partial: dict) -> dict:
    """
    Persist a partial settings update.

    Secrets are diverted into the encrypted vault and never reach
    settings.json. Everything else is merged and written as before.

    A secret arriving as the masked placeholder ("••••••••") means the user
    did not touch that field, so it is dropped rather than saved — otherwise
    opening the settings panel and pressing Save would overwrite every stored
    key with a row of dots.
    """
    incoming = dict(partial)
    secrets = _extract_secrets(incoming)

    if secrets:
        try:
            vault.set_many(secrets)
        except VaultError as exc:
            raise VaultError(f"Could not save to the vault: {exc}") from exc

    current = get_settings()
    merged = _deep_merge(current, incoming)

    # "advanced" is replaced wholesale rather than merged. A deep merge can add
    # a key and change one, but never remove one — so resetting a setting back
    # to its default would leave the old value in the file, and it would come
    # straight back on the next read.
    if isinstance(incoming.get("advanced"), dict):
        merged["advanced"] = dict(incoming["advanced"])

    # Belt and braces: even if a secret slipped through the extraction above,
    # it must not be written to disk in the clear.
    for field in SECRET_FIELDS:
        if field in merged.get("providers", {}):
            merged["providers"][field] = ""

    settings_file = paths.settings_file()
    settings_file.parent.mkdir(parents=True, exist_ok=True)
    settings_file.write_text(json.dumps(merged, indent=2), encoding="utf-8")
    return get_settings_for_ui()


def log_directory() -> Path:
    """
    Resolve the configured session-log directory to an absolute path.

    An absolute path in settings is honoured as-is, so a user can log straight
    to a network share.  A relative one resolves against the portable data
    directory — never the working directory, which for a double-clicked
    executable is wherever Explorer happened to leave us.
    """
    configured = (get_settings().get("logging", {}) or {}).get("directory", "logs")
    candidate = Path(configured)
    return candidate if candidate.is_absolute() else paths.data_dir() / candidate


def config_directory() -> Path:
    """
    Resolve the configuration-archive directory to an absolute path.

    Deliberately not under the session-log directory. A captured configuration
    is an artefact — a thing as it was at a moment — while a session log is a
    transcript of what someone did. Filed together, neither is easy to find,
    and the retention rules that make sense for one are wrong for the other.

    Absolute paths are honoured as-is, so captures can go straight to a share.
    A relative one resolves against the portable data directory, never the
    working directory — which for a double-clicked executable is wherever
    Explorer happened to leave us.
    """
    configured = (get_settings().get("logging", {}) or {}).get("config_directory", "configs")
    candidate = Path(configured or "configs")
    return candidate if candidate.is_absolute() else paths.data_dir() / candidate


def get_effective(field: str, env_fallback: str = "") -> str:
    """
    Return the active value for a provider field.

    Order of precedence:

    1. The encrypted vault — where every secret now lives.
    2. settings.json — non-secret fields (Ollama host, Chroma URL) only, plus
       any plaintext secret left behind by a version predating the vault and
       not yet migrated.
    3. The matching .env variable.

    Every AI client resolves its credentials through here, so this one function
    is what makes the vault apply everywhere.
    """
    if field in SECRET_FIELDS:
        stored = vault.get(field)
        if stored:
            return stored

    settings = get_settings()
    value = (settings.get("providers", {}) or {}).get(field, "") or ""
    return value or env_fallback


# ---------------------------------------------------------------------------
# Migration off plaintext storage
# ---------------------------------------------------------------------------


def migrate_plaintext_secrets() -> list[str]:
    """
    Move any plaintext API keys out of settings.json and into the vault.

    Versions before the vault wrote provider keys straight into settings.json.
    Leaving them there would mean the vault protects new keys while the old
    ones stay readable next to it, which is worse than either option alone.

    Skipped silently when the vault is locked — the user gets a prompt first,
    and migration runs on the next write instead.

    Returns:
        Names of the fields moved, for the startup log. Never their values.
    """
    settings = get_settings()
    providers = settings.get("providers", {}) or {}

    plaintext = {
        field: providers.get(field, "")
        for field in SECRET_FIELDS
        if providers.get(field)
    }
    if not plaintext:
        return []

    try:
        vault.set_many(plaintext)
    except VaultError as exc:
        logger.warning("Could not migrate plaintext keys into the vault: %s", exc)
        return []

    # Only blank them in settings.json once the vault write has succeeded, so a
    # failure here can never destroy the only copy.
    cleared = {field: "" for field in plaintext}
    current = get_settings()
    current.setdefault("providers", {}).update(cleared)
    settings_file = paths.settings_file()
    settings_file.parent.mkdir(parents=True, exist_ok=True)
    settings_file.write_text(json.dumps(current, indent=2), encoding="utf-8")

    return sorted(plaintext)


def _extract_secrets(partial: dict) -> dict[str, str]:
    """
    Pull secret provider fields out of *partial*, mutating it in place.

    Returns the secrets the user actually changed, ready for the vault.
    Values that are the masked placeholder are treated as "unchanged" and
    excluded from both the return value and the settings written to disk.
    """
    providers = partial.get("providers")
    if not isinstance(providers, dict):
        return {}

    secrets: dict[str, str] = {}
    for field in list(providers):
        if field not in SECRET_FIELDS:
            continue
        value = providers.pop(field)
        if not isinstance(value, str):
            continue
        # Unchanged: the UI echoed back the mask it was given.
        if value and set(value) <= {"•"}:
            continue
        secrets[field] = value

    return secrets


def _deep_merge(base: dict, override: dict) -> dict:
    result = dict(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result
