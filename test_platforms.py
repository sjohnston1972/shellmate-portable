"""
test_platforms.py — Tests for device fingerprinting and platform profiles.

Fed the banners real devices actually print. The failure that matters here is
a confident *wrong* answer: identifying a firewall as a switch means sending
it a paging command it does not understand, so the tests check both what is
identified and how sure it claims to be.

    python test_platforms.py
"""

import json
import shutil
import sys
import tempfile
from pathlib import Path

from backend import paths

_TEMP = Path(tempfile.mkdtemp(prefix="shellmate-platforms-"))
paths._data_dir_cache = _TEMP

from backend import platforms as platforms_module          # noqa: E402
from backend.fingerprint import (                          # noqa: E402
    identify, refine_with_version_output,
)
from backend.onboard import as_chosen, summarise            # noqa: E402
from backend.platforms import (                            # noqa: E402
    GENERIC, get_profile, load_profiles, resolve_alias,
)

passed = 0
failed: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    global passed
    if condition:
        passed += 1
        print(f"  OK  {name}")
    else:
        failed.append(f"{name}: {detail}")
        print(f"  FAIL {name}\n       {detail}")


# Real banner shapes, trimmed.
BANNERS = {
    "ios": (
        "Cisco IOS Software, C2960X Software (C2960X-UNIVERSALK9-M), "
        "Version 15.2(7)E3, RELEASE SOFTWARE (fc2)\r\n"
        "Technical Support: http://www.cisco.com/techsupport\r\n"
        "cisco WS-C2960X-48FPD-L (APM86XXX) processor with 524288K bytes of memory.\r\n"
    ),
    "nxos": (
        "Cisco Nexus Operating System (NX-OS) Software\r\n"
        "TAC support: http://www.cisco.com/tac\r\n"
        "Software\r\n  NXOS: version 9.3(5)\r\n"
        "Hardware\r\n  cisco Nexus9000 C93180YC-EX chassis\r\n"
    ),
    "asa": (
        "Cisco Adaptive Security Appliance Software Version 9.12(4)\r\n"
        "Firepower Extensible Operating System Version 2.6(1.192)\r\n"
        "Model: ASA5516\r\n"
    ),
    "junos": (
        "Hostname: srx-edge\r\nModel: srx345\r\n"
        "Junos: 20.4R3-S2.6\r\n"
        "JUNOS Software Release [20.4R3-S2.6]\r\n"
    ),
    "panos": (
        "hostname: PA-VM\r\nmodel: PA-VM\r\n"
        "sw-version: 10.1.6\r\nfamily: vm\r\n"
    ),
    "arista": (
        "Arista DCS-7050SX-64-R\r\n"
        "Hardware version: 01.31\r\n"
        "Software image version: 4.24.5M\r\n"
    ),
    # The platform pack (#524).
    "iosxr": (
        "Cisco IOS XR Software, Version 7.3.2\r\n"
        "Copyright (c) 2013-2021 by Cisco Systems, Inc.\r\n"
        "cisco ASR9K Series (Intel 686 F6M14S4) processor\r\n"
    ),
    "fortios": (
        "Version: FortiGate-100F v7.2.5,build1517,230606 (GA.F)\r\n"
        "Serial-Number: FGT100FTK20001234\r\n"
        "Hostname: FGT-01\r\n"
    ),
    "routeros": (
        "MikroTik RouterOS 7.11.2 (c) 1999-2023  https://www.mikrotik.com/\r\n"
        "board-name: hEX-S\r\n"
    ),
    "huawei": (
        "Huawei Versatile Routing Platform Software\r\n"
        "VRP (R) software, Version 5.170 (S5720 V200R019C10SPC500)\r\n"
        "HUAWEI S5720-28X-LI-24S-AC Routing Switch uptime is 3 days\r\n"
    ),
    "aoscx": (
        "ArubaOS-CX\r\n"
        "(c) Copyright 2017-2023 Hewlett Packard Enterprise Development LP\r\n"
        "Version      : FL.10.09.1010\r\n"
        "Build Date   : 2023-01-01\r\n"
    ),
}


def test_banner_identification() -> None:
    print("\n-- Identification from a login banner --")
    for expected, banner in BANNERS.items():
        result = identify(banner=banner)
        check(f"identifies {expected}", result.platform == expected,
              f"got {result.platform} (confidence {result.confidence})")
        check(f"  {expected} confidence is high enough to act",
              result.certain_enough_to_act,
              f"confidence {result.confidence}, source {result.source}")


def test_version_and_model_extraction() -> None:
    print("\n-- Version and model --")
    expectations = [
        ("ios", "15.2(7)E3", "WS-C2960X-48FPD-L"),
        ("nxos", "9.3(5)", None),
        ("asa", "9.12(4)", "ASA5516"),
        ("junos", "20.4R3-S2.6", "srx345"),
        ("panos", "10.1.6", "PA-VM"),
        ("arista", "4.24.5M", None),
        ("iosxr", "7.3.2", "ASR9K"),
        ("fortios", "7.2.5", "FortiGate-100F"),
        ("routeros", "7.11.2", "hEX-S"),
        ("huawei", "5.170", "S5720-28X-LI-24S-AC"),
        ("aoscx", "FL.10.09.1010", None),
    ]
    for platform, version, model in expectations:
        result = identify(banner=BANNERS[platform])
        check(f"{platform} version extracted", result.version == version,
              f"got {result.version!r}, want {version!r}")
        if model:
            check(f"{platform} model extracted", result.model == model,
                  f"got {result.model!r}, want {model!r}")


def test_nxos_not_mistaken_for_ios() -> None:
    """An NX-OS banner also contains the word 'cisco'."""
    print("\n-- Specific signatures beat generic ones --")
    result = identify(banner=BANNERS["nxos"])
    check("NX-OS is not identified as IOS", result.platform == "nxos",
          f"got {result.platform}")
    result = identify(banner=BANNERS["asa"])
    check("ASA is not identified as IOS", result.platform == "asa",
          f"got {result.platform}")


def test_prompt_fallback() -> None:
    print("\n-- Falling back to the prompt --")
    cases = [
        ("neteng@jump:~$", "linux"),
        ("admin@PA-VM(active)>", "panos"),
        ("neteng@srx-edge>", "junos"),
        ("ASA-FW/pri/act#", "asa"),
        ("switch01(config-if)#", "ios"),
        ("RP/0/RSP0/CPU0:edge-xr#", "iosxr"),
        ("[admin@MikroTik] >", "routeros"),
        ("<core-sw1>", "huawei"),
        ("[core-sw1]", "huawei"),
        ("FGT-01 (global) #", "fortios"),
    ]
    for prompt, expected in cases:
        result = identify(banner="", prompt=prompt)
        check(f"prompt {prompt} suggests {expected}", result.platform == expected,
              f"got {result.platform}")

    # A bare Cisco-ish prompt is barely evidence and must not trigger action.
    result = identify(banner="", prompt="switch01#")
    check("a bare prompt is too weak to act on", not result.certain_enough_to_act,
          f"confidence {result.confidence}")


def test_unknown_device() -> None:
    print("\n-- Unidentifiable devices --")
    result = identify(banner="Authorised access only. All activity is logged.\r\n")
    check("a legal banner identifies nothing", result.platform == GENERIC,
          f"got {result.platform}")
    check("and is not acted upon", not result.certain_enough_to_act)
    check("the generic profile sends no paging command",
          get_profile(GENERIC).paging_off == "",
          f"got {get_profile(GENERIC).paging_off!r}")


def test_onboarding_explains_itself() -> None:
    """
    Every reason for sending nothing is distinguishable.

    The bug this guards (#47): paging-off fires only above the confidence
    threshold, which a prompt-only identification almost never clears, and the
    interface said "identified Cisco IOS" and stopped — reading as success
    while paging was still on. The summary now carries *why*, and there is one
    answer rather than the interface re-deriving the decision itself.
    """
    print("\n-- Onboarding says what it did not do --")

    confident = summarise(identify(banner=BANNERS["ios"]))
    check("a banner match is acted on",
          confident["paging_command"] == "terminal length 0"
          and confident["paging_skipped"] == "",
          f"got {confident['paging_command']!r} / {confident['paging_skipped']!r}")

    unsure = summarise(identify(banner="", prompt="switch01(config)#"))
    check("a prompt-only guess sends nothing", unsure["paging_command"] == "",
          f"got {unsure['paging_command']!r}")
    check("and says it was not confident enough",
          unsure["paging_skipped"] == "unconfident",
          f"got {unsure['paging_skipped']!r}")
    check("while still naming the command it declined to send",
          unsure["paging_available"] == "terminal length 0",
          f"got {unsure['paging_available']!r}")

    unknown = summarise(identify(banner="Authorised access only.\r\n"))
    check("an unidentified device is distinguishable from an unsure one",
          unknown["paging_skipped"] == "unidentified",
          f"got {unknown['paging_skipped']!r}")

    off = summarise(identify(banner=BANNERS["ios"]), auto_paging=False)
    check("the setting being off is its own reason",
          off["paging_skipped"] == "off" and off["paging_command"] == "",
          f"got {off['paging_skipped']!r} / {off['paging_command']!r}")

    shell = summarise(identify(banner="", prompt="neteng@jump:~$"))
    check("a shell is identified confidently", shell["confident"],
          f"confidence {shell['confidence']}")
    check("but has no paging command to send",
          shell["paging_skipped"] == "no-command",
          f"got {shell['paging_skipped']!r}")

    chosen = summarise(as_chosen("asa"))
    check("naming the platform yourself is acted on",
          chosen["paging_command"] == "terminal pager 0",
          f"got {chosen['paging_command']!r}")
    check("and is recorded as coming from you", chosen["source"] == "you",
          f"got {chosen['source']!r}")

    try:
        as_chosen("not-a-platform")
        check("an unknown platform is rejected", False, "no error raised")
    except ValueError:
        check("an unknown platform is rejected", True)


def test_refinement() -> None:
    print("\n-- Refining with version output --")
    weak = identify(banner="", prompt="switch01#")
    check("starts as a weak IOS guess",
          weak.platform == "ios" and not weak.certain_enough_to_act,
          f"got {weak.platform} at {weak.confidence}")

    refined = refine_with_version_output(weak, BANNERS["nxos"])
    check("version output corrects the platform", refined.platform == "nxos",
          f"got {refined.platform}")
    check("and raises confidence enough to act", refined.certain_enough_to_act)
    check("source is recorded", refined.source == "version-command",
          f"got {refined.source}")

    # Useless output must not destroy what we already knew.
    kept = refine_with_version_output(
        identify(banner=BANNERS["ios"]), "% Invalid input detected")
    check("unhelpful output leaves the fingerprint alone", kept.platform == "ios",
          f"got {kept.platform}")


def test_profiles_and_aliases() -> None:
    print("\n-- Profiles and aliases --")
    check("every platform defines a name",
          all(p.name for p in load_profiles().values()))
    check("a generic profile always exists", GENERIC in load_profiles())

    check("ints expands per platform on IOS",
          resolve_alias("ios", "ints") == "show ip interface brief",
          f"got {resolve_alias('ios', 'ints')}")
    check("ints expands differently on Junos",
          resolve_alias("junos", "ints") == "show interfaces terse",
          f"got {resolve_alias('junos', 'ints')}")
    check("ints expands differently on PAN-OS",
          resolve_alias("panos", "ints") == "show interface all",
          f"got {resolve_alias('panos', 'ints')}")

    check("aliases are case-insensitive", resolve_alias("ios", "INTS") is not None)
    check("an unknown alias expands to nothing",
          resolve_alias("ios", "notanalias") is None)
    # Rewriting the middle of a real command would be worse than not helping.
    check("a full command is never rewritten",
          resolve_alias("ios", "show ints") is None,
          f"got {resolve_alias('ios', 'show ints')}")
    check("the generic profile has no aliases",
          resolve_alias(GENERIC, "ints") is None)

    check("paging-off differs across platforms",
          get_profile("ios").paging_off == "terminal length 0"
          and get_profile("junos").paging_off == "set cli screen-length 0"
          and get_profile("panos").paging_off == "set cli pager off"
          and get_profile("asa").paging_off == "terminal pager 0")


def test_profiles_are_editable() -> None:
    print("\n-- User-editable platform file --")
    directory = Path(tempfile.mkdtemp(prefix="shellmate-plat-"))
    paths._data_dir_cache = directory
    platforms_module._cache = None
    try:
        load_profiles(refresh=True)
        path = platforms_module.profiles_path()
        check("defaults are written out on first run", path.exists())

        document = json.loads(path.read_text(encoding="utf-8"))
        check("the file lists the platforms", "ios" in document["platforms"])

        # Correct an alias and add a whole new platform.
        document["platforms"]["ios"]["aliases"]["ints"] = "show interfaces status"
        document["platforms"]["vyos"] = {
            "id": "vyos", "name": "VyOS", "paging_off": "set terminal length 0",
            "signatures": ["vyos"], "aliases": {"ints": "show interfaces"},
        }
        path.write_text(json.dumps(document), encoding="utf-8")

        platforms_module._cache = None
        load_profiles(refresh=True)

        check("an edited alias is honoured",
              resolve_alias("ios", "ints") == "show interfaces status",
              f"got {resolve_alias('ios', 'ints')}")
        check("a user-added platform is loaded",
              get_profile("vyos").name == "VyOS", f"got {get_profile('vyos').name}")
        check("and it can be fingerprinted",
              identify(banner="Version: VyOS 1.4").platform == "vyos",
              f"got {identify(banner='Version: VyOS 1.4').platform}")

        # A corrupt file must not leave ShellMate unable to identify anything.
        path.write_text("{ this is not json", encoding="utf-8")
        platforms_module._cache = None
        load_profiles(refresh=True)
        check("a corrupt file falls back to the built-ins",
              resolve_alias("ios", "ints") == "show ip interface brief",
              f"got {resolve_alias('ios', 'ints')}")
    finally:
        platforms_module._cache = None
        paths._data_dir_cache = _TEMP
        shutil.rmtree(directory, ignore_errors=True)


def test_the_platform_pack() -> None:
    """
    What the five newest profiles will and will not send (#524).

    The paging command is the only thing ShellMate types into a session
    unasked, so it is the one field where "we are not sure" has to mean an
    empty string rather than a plausible guess. FortiOS pages through a
    configuration change shared by every administrator on the box, and
    RouterOS answers paging per command rather than per session; neither has
    a per-session command, so neither gets one.
    """
    print("\n-- The platform pack --")

    sends = {
        "iosxr":  "terminal length 0",
        "huawei": "screen-length 0 temporary",
        "aoscx":  "no page",
    }
    for platform, command in sends.items():
        check(f"{platform} turns paging off with {command!r}",
              get_profile(platform).paging_off == command,
              f"got {get_profile(platform).paging_off!r}")

    for platform in ("fortios", "routeros"):
        check(f"{platform} sends nothing, because it has nothing to send",
              get_profile(platform).paging_off == "",
              f"got {get_profile(platform).paging_off!r}")
        check(f"  and {platform} is still identified and still useful",
              bool(get_profile(platform).show_run
                   and get_profile(platform).version_command))

    # An XR banner contains "Cisco IOS", and the IOS version pattern matches
    # it. Getting this wrong means a confident, wrong answer above the gate.
    xr = identify(banner=BANNERS["iosxr"])
    check("an IOS-XR banner is not read as IOS", xr.platform == "iosxr",
          f"got {xr.platform} at {xr.confidence}")
    ios = identify(banner=BANNERS["ios"])
    check("and an IOS banner is still IOS", ios.platform == "ios",
          f"got {ios.platform}")

    # A prompt names the platform for XR and RouterOS, and only the family
    # for Huawei — HP Comware prints <host> and [host] identically and wants
    # a different paging command.
    check("an XR node id is enough to act on",
          identify(banner="", prompt="RP/0/RSP0/CPU0:edge-xr#").certain_enough_to_act)
    check("a Huawei prompt alone is not",
          not identify(banner="", prompt="<core-sw1>").certain_enough_to_act)

    check("ints expands on IOS-XR",
          resolve_alias("iosxr", "ints") == "show ipv4 interface brief",
          f"got {resolve_alias('iosxr', 'ints')}")
    check("ints expands on Huawei VRP",
          resolve_alias("huawei", "ints") == "display interface brief",
          f"got {resolve_alias('huawei', 'ints')}")
    check("ints expands on RouterOS",
          resolve_alias("routeros", "ints") == "/interface print",
          f"got {resolve_alias('routeros', 'ints')}")
    check("ints expands on FortiOS",
          resolve_alias("fortios", "ints") == "get system interface",
          f"got {resolve_alias('fortios', 'ints')}")
    check("ints expands on AOS-CX",
          resolve_alias("aoscx", "ints") == "show interface brief",
          f"got {resolve_alias('aoscx', 'ints')}")

    from backend.platforms import matches_dangerous
    check("a Huawei reboot is held", matches_dangerous("huawei", "reboot") == "reboot")
    check("a RouterOS reset is held",
          matches_dangerous("routeros", "/system reset-configuration")
          == "/system reset-configuration")
    check("a FortiOS factory reset is held",
          matches_dangerous("fortios", "execute factoryreset") == "execute factoryreset")
    check("an ordinary Huawei display is not",
          matches_dangerous("huawei", "display interface brief") == "")

    # Blank config commands mean "ShellMate will not push here", which is the
    # documented meaning and not an oversight: XR commits in two stages,
    # FortiOS applies as you type, and RouterOS has no configuration mode.
    for platform in ("iosxr", "fortios", "routeros"):
        check(f"{platform} is not pushed to", get_profile(platform).config_enter == "",
              f"got {get_profile(platform).config_enter!r}")
    check("Huawei enters and leaves configuration mode",
          get_profile("huawei").config_enter == "system-view"
          and get_profile("huawei").config_exit == "return")
    check("  but does not offer Save, which asks a question nobody can answer",
          get_profile("huawei").save_command == "",
          f"got {get_profile('huawei').save_command!r}")
    check("AOS-CX is Cisco-shaped all the way through",
          get_profile("aoscx").config_enter == "configure terminal"
          and get_profile("aoscx").save_command == "write memory")

    from backend.session.parsed import NTC_PLATFORMS
    check("every new platform maps to an ntc-templates name",
          {"iosxr", "fortios", "routeros", "huawei", "aoscx"} <= set(NTC_PLATFORMS),
          f"got {sorted(NTC_PLATFORMS)}")


def test_new_platforms_reach_an_existing_install() -> None:
    """
    A platforms.json written before the pack must gain it, not block it.

    Profiles are data, so an installation that has ever opened the editor
    has a complete file on disk. If that file simply won, the five new
    platforms would reach nobody who had used ShellMate before — which is
    the same trap `_merge_profile` was written for on aliases.
    """
    print("\n-- New platforms on an existing installation --")
    directory = Path(tempfile.mkdtemp(prefix="shellmate-pack-"))
    paths._data_dir_cache = directory
    platforms_module._cache = None
    try:
        load_profiles(refresh=True)
        path = platforms_module.profiles_path()
        document = json.loads(path.read_text(encoding="utf-8"))

        # Rewind the file to what an earlier version wrote: the old seven,
        # with one edit of the user's own.
        for new in ("iosxr", "fortios", "routeros", "huawei", "aoscx"):
            document["platforms"].pop(new, None)
        document["platforms"]["ios"]["paging_off"] = "terminal length 0 exec"
        path.write_text(json.dumps(document), encoding="utf-8")

        platforms_module._cache = None
        load_profiles(refresh=True)

        check("a file that predates the pack still loads",
              get_profile("ios").name == "Cisco IOS / IOS-XE")
        check("and the user's own edit survives",
              get_profile("ios").paging_off == "terminal length 0 exec",
              f"got {get_profile('ios').paging_off!r}")
        for new in ("iosxr", "fortios", "routeros", "huawei", "aoscx"):
            check(f"  {new} arrives from the built-ins",
                  get_profile(new).id == new, f"got {get_profile(new).id!r}")
        check("and it is fingerprintable straight away",
              identify(banner=BANNERS["huawei"]).platform == "huawei")
    finally:
        platforms_module._cache = None
        paths._data_dir_cache = _TEMP
        shutil.rmtree(directory, ignore_errors=True)


def test_outbound_pipeline() -> None:
    """Keystrokes must reach the device unchanged unless an alias fires."""
    print("\n-- Outbound pipeline --")
    from backend.pipeline import CTRL_U, OutboundPipeline

    def typed(pipeline, text):
        """Feed text one character at a time, as a real session does."""
        return "".join(pipeline.process(ch) for ch in text)

    pipe = OutboundPipeline()
    pipe.platform = "ios"

    check("ordinary typing passes through untouched",
          typed(pipe, "show version\r") == "show version\r",
          f"got {typed(OutboundPipeline(), 'show version' + chr(13))!r}")

    # Keystrokes still go out as they are typed — otherwise the user sees
    # nothing on screen while typing. The substitution happens at Enter:
    # Ctrl-U wipes what the device echoed, then the real command follows.
    pipe = OutboundPipeline(); pipe.platform = "ios"
    result = typed(pipe, "ints\r")
    check("an alias is expanded at Enter",
          result == "ints" + CTRL_U + "show ip interface brief\r", f"got {result!r}")
    check("keystrokes are echoed as typed before the substitution",
          result.startswith("ints"), f"got {result!r}")
    check("the echoed text is cleared before the real command",
          result.index(CTRL_U) < result.index("show ip"), f"got {result!r}")
    check("the substitution is reported",
          pipe.last_expansion == ("ints", "show ip interface brief"),
          f"got {pipe.last_expansion}")

    pipe = OutboundPipeline(); pipe.platform = "junos"
    check("the same alias differs by platform",
          typed(pipe, "ints\r") == "ints" + CTRL_U + "show interfaces terse\r",
          f"got {typed(OutboundPipeline(), 'x')!r}")

    # Backspacing away an alias must leave nothing to expand.
    pipe = OutboundPipeline(); pipe.platform = "ios"
    result = typed(pipe, "ints\b\b\b\bshow ip route\r")
    check("backspaces are tracked, so no stale expansion fires",
          result.endswith("show ip route\r") and CTRL_U not in result,
          f"got {result!r}")

    # Ctrl-U clears the line on the device; our view must clear too.
    pipe = OutboundPipeline(); pipe.platform = "ios"
    result = typed(pipe, "ints" + CTRL_U + "show clock\r")
    check("Ctrl-U resets the tracked line",
          result.endswith("show clock\r") and result.count(CTRL_U) == 1,
          f"got {result!r}")

    pipe = OutboundPipeline(); pipe.platform = "ios"
    pipe.expand_aliases = False
    check("expansion can be switched off",
          typed(pipe, "ints\r") == "ints\r")

    pipe = OutboundPipeline()          # no platform identified
    check("nothing is expanded before the device is identified",
          typed(pipe, "ints\r") == "ints\r")

    # A long line is a paste, not typing.
    pipe = OutboundPipeline(); pipe.platform = "ios"
    long_line = "x" * 100
    check("over-long lines are never treated as aliases",
          typed(pipe, long_line + "\r") == long_line + "\r")

    # Arrow keys move the cursor in ways we cannot track from this side.
    pipe = OutboundPipeline(); pipe.platform = "ios"
    result = typed(pipe, "ints\x1b[Dx\r")
    check("an escape sequence abandons the assembled line",
          CTRL_U not in result, f"got {result!r}")


def test_dangerous_command_matching() -> None:
    """
    Which typed lines count as destructive.

    `dangerous_commands` was defined for all nine platforms, documented as
    feeding the guardrails, editable in the interface — and read by nothing.
    The consequence was an inverted risk: a `write erase` *suggested by the
    assistant* got a confirmation, and the same command typed by hand went
    straight to the device. The second is how the accident happens.
    """
    print("\n-- What counts as destructive --")
    from backend.platforms import GENERIC, matches_dangerous

    check("a bare reload matches", matches_dangerous("ios", "reload") == "reload")
    check("and one with arguments",
          matches_dangerous("ios", "reload in 10") == "reload")
    check("case does not matter",
          matches_dangerous("ios", "WRITE ERASE") == "write erase")
    check("nor does doubled whitespace",
          matches_dangerous("ios", "write  erase") == "write erase")

    # The one that decides whether this is usable. A guardrail that fires on
    # `show reload-reason` gets switched off in week two.
    check("a command that merely starts with the same letters does not match",
          matches_dangerous("ios", "show reload-reason") == "",
          "prefix matching has to respect word boundaries")
    check("nor does an ordinary show command",
          matches_dangerous("ios", "show ip interface brief") == "")
    check("an empty line matches nothing", matches_dangerous("ios", "   ") == "")
    check("an unknown platform matches nothing rather than raising",
          matches_dangerous("no-such-platform", "reload") == "")

    check("the generic profile still catches the universal ones",
          bool(matches_dangerous(GENERIC, "reload")),
          "a device below the confidence gate has only this list")


def test_the_guardrail_holds_before_it_sends() -> None:
    """
    Nothing reaches the device until the answer comes back.

    That is the whole guarantee. The line is cleared from the device's input
    with CTRL_U — the same trick alias expansion already uses — and the
    terminator is withheld, so an unanswered prompt leaves the device exactly
    as it was.
    """
    print("\n-- Holding a destructive command --")
    from backend import advanced
    from backend.pipeline import CTRL_U, OutboundPipeline

    def make(platform: str) -> OutboundPipeline:
        pipeline = OutboundPipeline()
        pipeline.platform = platform
        return pipeline

    pipeline = make("ios")

    sent = pipeline.process("show ip interface brief\r")
    check("an ordinary command goes straight through",
          sent.endswith("\r") and not pipeline.held_command, repr(sent))

    sent = pipeline.process("reload\r")
    check("a destructive one is held", pipeline.held_command == "reload")
    check("and no carriage return reaches the device",
          sent.endswith(CTRL_U) and "\r" not in sent[len("reload"):],
          repr(sent))

    check("confirming releases it with its terminator",
          pipeline.release() == "reload\r")
    check("and it is not held twice", pipeline.held_command == "")

    pipeline.process("write erase\r")
    check("cancelling reports what was dropped",
          pipeline.drop() == "write erase")
    check("and forgets it", pipeline.held_command == "")

    # Below the confidence gate there is no platform list.
    unknown = make("")
    unknown.process("reload\r")
    check("an unidentified device falls back to the generic list",
          unknown.held_command == "reload",
          "guarding nothing there is a choice, not a default")
    unknown.drop()

    advanced.update({"terminal.confirm_dangerous_scope": "identified-only"})
    strict = make("")
    sent = strict.process("reload\r")
    check("unless that has been switched off deliberately",
          not strict.held_command and sent.endswith("\r"), repr(sent))
    advanced.reset(key="terminal.confirm_dangerous_scope")

    advanced.update({"terminal.confirm_dangerous": False})
    off = make("ios")
    sent = off.process("reload\r")
    check("and the switch turns the whole thing off",
          not off.held_command and sent.endswith("\r"), repr(sent))
    advanced.reset(key="terminal.confirm_dangerous")


def test_a_platform_you_set_is_remembered() -> None:
    """
    Telling ShellMate what a device is used to last as long as the tab.

    Which landed hardest on exactly the devices `as_chosen()` exists for — a
    legal-warning banner and anything behind a terminal server are the ones
    automatic identification will never settle, so the escape hatch had to be
    used again on every single reconnect.

    It is also the strongest value ShellMate holds: confidence 1.0, source
    "you", and its own docstring calls it the one source that is not a guess.
    Discarding it on disconnect while keeping every guess in the database was
    the wrong way round.
    """
    print(chr(10) + "-- Remembering what you said a device is --")
    from backend.onboard import Onboarder

    def onboard(banner: str, prompt: str, remembered: str = "") -> dict:
        o = Onboarder()
        o.observe(banner)
        return o.run(prompt=prompt, remembered=remembered)

    legal = "Authorised access only. All activity is logged." + chr(13) + chr(10)

    # Without it: identified from the prompt, below the gate, nothing sent.
    blind = onboard(legal, "switch01#")
    check("a legal banner alone sends nothing",
          blind["paging_command"] == "" and blind["paging_skipped"] == "unconfident",
          str(blind["paging_skipped"]))

    remembered = onboard(legal, "switch01#", "ios")
    check("a remembered platform is used", remembered["remembered"] is True)
    check("and the paging command goes out",
          remembered["paging_command"] == "terminal length 0",
          str(remembered["paging_command"]))
    check("so aliases have a platform to resolve against",
          remembered["platform"] == "ios")

    # A confident banner is evidence about the device as it is *now*.
    replaced = onboard(BANNERS["asa"], "fw#", "ios")
    check("a confident banner beats a remembered value",
          replaced["platform"] == "asa",
          "a device answering as an ASA has probably been replaced")
    check("and the disagreement is reported rather than silent",
          replaced["remembered_overridden"] == "ios",
          "silently preferring either would leave somebody unable to explain "
          "why a command went out")

    # platforms.json is a text file, and a profile can travel between
    # installations.
    unknown = onboard(legal, "switch01#", "no-such-platform")
    check("an unknown remembered platform falls back rather than raising",
          unknown["remembered"] is False)
    check("and identification carries on normally",
          unknown["paging_skipped"] == "unconfident",
          str(unknown["paging_skipped"]))

    check("nothing changes when there is nothing remembered",
          onboard(legal, "switch01#")["remembered"] is False)


def test_the_profile_carries_it() -> None:
    """The write and the read, against a temporary profiles.json."""
    print(chr(10) + "-- Where it is kept --")
    import json as _json
    import tempfile
    from pathlib import Path as _Path

    from backend import paths as _paths, profiles as _profiles

    original_file = _paths.profiles_file
    temp = _Path(tempfile.mkdtemp(prefix="remember-"))
    _paths.profiles_file = lambda: temp / "profiles.json"

    try:
        _profiles._save([{
            "id": "p1", "hostname": "10.60.0.1", "port": 22,
            "username": "neteng", "connection_type": "ssh", "name": "10.60.0.1",
        }])

        check("nothing is remembered to begin with",
              _profiles.remembered_platform("10.60.0.1", 22, "neteng") == "")

        check("setting it reports a change",
              _profiles.remember_platform("10.60.0.1", 22, "neteng", "asa") is True)
        check("and it reads back",
              _profiles.remembered_platform("10.60.0.1", 22, "neteng") == "asa")

        check("a different device is unaffected",
              _profiles.remembered_platform("10.60.0.2", 22, "neteng") == "")

        # Matched by target, not by name — the name is rewritten the moment
        # the device says what it is called.
        _profiles._save([{**_profiles._load()[0], "name": "core-fw-01"}])
        check("a renamed profile is still found",
              _profiles.remembered_platform("10.60.0.1", 22, "neteng") == "asa",
              "matching on a field that changes by itself would stop finding "
              "the profile it just renamed")

        check("clearing it works",
              _profiles.remember_platform("10.60.0.1", 22, "neteng", "") is True)
        check("and it is gone from the file",
              "platform" not in _profiles._load()[0],
              str(_profiles._load()[0]))

        check("an unknown target changes nothing",
              _profiles.remember_platform("10.99.9.9", 22, "u", "ios") is False)
    finally:
        _paths.profiles_file = original_file


def main() -> int:
    print("=" * 52)
    print("  Device fingerprinting and platform profiles")
    print("=" * 52)

    for test in (
        test_banner_identification,
        test_version_and_model_extraction,
        test_nxos_not_mistaken_for_ios,
        test_prompt_fallback,
        test_onboarding_explains_itself,
        test_unknown_device,
        test_refinement,
        test_profiles_and_aliases,
        test_profiles_are_editable,
        test_the_platform_pack,
        test_new_platforms_reach_an_existing_install,
        test_outbound_pipeline,
        test_dangerous_command_matching,
        test_the_guardrail_holds_before_it_sends,
        test_a_platform_you_set_is_remembered,
        test_the_profile_carries_it,
    ):
        try:
            test()
        except Exception as exc:
            failed.append(f"{test.__name__}: raised {type(exc).__name__}: {exc}")
            print(f"  FAIL {test.__name__} raised {type(exc).__name__}: {exc}")

    shutil.rmtree(_TEMP, ignore_errors=True)

    print("\n" + "=" * 52)
    print(f"  {passed} passed  |  {len(failed)} failed")
    print("=" * 52)
    if failed:
        print("\nFAILURES:")
        for item in failed:
            print(f"  {item}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
