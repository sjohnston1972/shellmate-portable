"""
test_transcript.py — Tests for ANSI cleaning and command segmentation.

Everything here is fed the shapes real devices actually emit: colour codes
mid-word, ``--More--`` erased with backspaces, ``Building configuration...``
redrawn with a carriage return, and prompts from five different vendors.

These are the failure modes that do not announce themselves. A missed escape
sequence makes a stored transcript unsearchable; a false prompt slices real
output in half and files configuration lines under the wrong command.

    python test_transcript.py
"""

import sys

from backend.session.ansi import apply_backspace, apply_carriage_returns, clean, strip_ansi
from backend.session.transcript import (
    TranscriptParser, detect_hostname, match_prompt,
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


# ---------------------------------------------------------------------------
# ANSI cleaning
# ---------------------------------------------------------------------------


def test_ansi_stripping() -> None:
    print("\n-- Escape sequences --")

    coloured = "\x1b[32mGigabitEthernet0/1\x1b[0m is up"
    check("colour codes removed from mid-word",
          strip_ansi(coloured) == "GigabitEthernet0/1 is up",
          f"got {strip_ansi(coloured)!r}")

    check("cursor movement removed",
          strip_ansi("\x1b[2J\x1b[Hswitch01#") == "switch01#")

    check("window-title OSC removed",
          strip_ansi("\x1b]0;terminal server\x07switch01#") == "switch01#",
          f"got {strip_ansi(chr(27) + ']0;terminal server' + chr(7) + 'switch01#')!r}")

    check("charset selection removed",
          strip_ansi("\x1b(Bplain text") == "plain text")

    check("plain text is untouched",
          strip_ansi("interface Gi0/1") == "interface Gi0/1")


def test_backspace() -> None:
    print("\n-- Backspace --")

    # How a Cisco device erases --More-- when you press space.
    more = "--More--" + "\b" * 8 + " " * 8 + "\b" * 8 + "hostname switch01"
    check("--More-- erased by backspaces",
          apply_backspace(more).strip() == "hostname switch01",
          f"got {apply_backspace(more)!r}")

    check("backspace deletes one character",
          apply_backspace("abcX\b") == "abc")

    check("backspace at line start is ignored",
          apply_backspace("line1\n\babc") == "line1\nabc",
          f"got {apply_backspace('line1' + chr(10) + chr(8) + 'abc')!r}")

    check("text without backspace is untouched",
          apply_backspace("no backspaces here") == "no backspaces here")


def test_carriage_returns() -> None:
    print("\n-- Carriage returns --")

    # Overwriting is character by character from column zero, not a wholesale
    # line replacement: a real terminal leaves whatever the new text does not
    # reach. Storing what the engineer actually saw means reproducing that.
    check("shorter overwrite leaves the tail",
          apply_carriage_returns("Building configuration...\r[OK]") == "[OK]ding configuration...",
          f"got {apply_carriage_returns('Building configuration...' + chr(13) + '[OK]')!r}")

    check("longer overwrite replaces entirely",
          apply_carriage_returns("5%\r100% done") == "100% done",
          f"got {apply_carriage_returns('5%' + chr(13) + '100% done')!r}")

    check("a device clearing with spaces leaves a blank line",
          apply_carriage_returns("--More--\r        ").strip() == "",
          f"got {apply_carriage_returns('--More--' + chr(13) + ' ' * 8)!r}")

    check("CRLF is not treated as an overwrite",
          clean("line one\r\nline two") == "line one\nline two",
          f"got {clean('line one' + chr(13) + chr(10) + 'line two')!r}")


def test_cursor_movement() -> None:
    """
    Devices redraw their input line; the movement must be applied, not stripped.

    Regression test for a command recorded from a live router as
    'Tunnel7Tunnel7Tunnel7S3-R2#Tunnel7Tunnel7Tunnel7'. Stripping the cursor
    escapes concatenated each redraw instead of overwriting.
    """
    print("\n-- Cursor movement and erase-in-line --")

    esc = "\x1b"
    cases = [
        ("the redraw seen on a real router",
         f"S3-R2#Tunnel7{esc}[7DTunnel7{esc}[7DTunnel7", "S3-R2#Tunnel7"),
        ("tab completion replacing the tail",
         f"sw#sh ip int br{esc}[6D{esc}[Kinterface brief", "sw#sh ip interface brief"),
        ("erase to end of line",
         f"sw#show running-config{esc}[14D{esc}[Kversion", "sw#show version"),
        ("cursor forward returns to where it was",
         f"sw#show{esc}[3D{esc}[3C version", "sw#show version"),
        ("cursor to an absolute column",
         f"abcdef{esc}[3GXY", "abXYef"),
        # ECMA-48: erase-all clears the line but does not move the cursor,
        # so the following text lands where the cursor already was.
        ("erase the whole line leaves the cursor put",
         f"rubbish{esc}[2Kclean", "       clean"),
        ("erase-all followed by a column reset starts over",
         f"rubbish{esc}[2K{esc}[1Gclean", "clean  "),
        ("text with no movement is untouched",
         "sw#show version", "sw#show version"),
    ]
    for name, raw, want in cases:
        got = clean(raw)
        check(name, got == want, f"got {got!r}, want {want!r}")

    parser = TranscriptParser()
    records = parser.feed(f"S3-R2#Tunnel7{esc}[7DTunnel7{esc}[7DTunnel7\r\nS3-R2#")
    check("the command is recorded as what was left on screen",
          bool(records) and records[0].command == "Tunnel7",
          f"got {[r.command for r in records]}")


def test_clean_combined() -> None:
    print("\n-- Combined cleaning --")

    raw = ("\x1b[32mswitch01\x1b[0m#show run\r\n"
           "Building configuration...\r\n"
           "--More--" + "\b" * 8 + " " * 8 + "\b" * 8 +
           "hostname switch01\r\n")
    result = clean(raw)

    check("no escape sequences survive", "\x1b" not in result)
    check("no control characters survive",
          not any(ord(c) < 32 and c != "\n" for c in result), f"got {result!r}")
    check("the command line is intact",
          "switch01#show run" in result, f"got {result!r}")
    check("the config line is intact",
          "hostname switch01" in result, f"got {result!r}")
    check("--More-- is gone", "--More--" not in result, f"got {result!r}")


# ---------------------------------------------------------------------------
# Prompt recognition
# ---------------------------------------------------------------------------


def test_prompt_matching() -> None:
    print("\n-- Prompt recognition across vendors --")

    should_match = [
        ("switch01#",                    "IOS enable"),
        ("switch01>",                    "IOS user"),
        ("switch01(config)#",            "IOS config"),
        ("switch01(config-if)#",         "IOS interface config"),
        ("ASA-FW/pri/act#",              "ASA failover context"),
        ("neteng@srx-edge>",             "Junos operational"),
        ("neteng@srx-edge#",             "Junos config"),
        ("admin@PA-VM(active)>",         "PAN-OS"),
        ("neteng@jump:~$",               "Linux jump host"),
        ("nxos-leaf-01#",                "NX-OS"),
    ]
    for line, label in should_match:
        check(f"recognises {label}: {line}", match_prompt(line) is not None)

    should_not_match = [
        ("  ip address 10.0.0.1 255.255.255.0", "a config line"),
        ("!", "an IOS comment marker"),
        ("---------------------------", "a separator rule"),
        ("Password: ", "a credential prompt"),
        ("% Invalid input detected at '^' marker.", "an error message"),
        ("  1: GigabitEthernet0/1", "numbered output"),
    ]
    for line, label in should_not_match:
        check(f"rejects {label}", match_prompt(line) is None,
              f"matched: {match_prompt(line)}")

    # A long line containing '#' is configuration, not a prompt.
    long_line = "x" * 600 + "#"
    check("rejects an over-long line", match_prompt(long_line) is None)


def test_command_extraction() -> None:
    print("\n-- Command extraction from a prompt line --")

    found = match_prompt("switch01#show ip interface brief")
    check("splits prompt from command",
          found == ("switch01#", "show ip interface brief"), f"got {found}")

    found = match_prompt("switch01# show version")
    check("tolerates a space after the prompt",
          found is not None and found[1].strip() == "show version", f"got {found}")

    found = match_prompt("switch01#")
    check("bare prompt yields no command",
          found is not None and found[1] == "", f"got {found}")


def test_hostname_detection() -> None:
    print("\n-- Hostname detection --")

    cases = [
        ("switch01#show ver\r\nswitch01#",        "switch01",  "IOS"),
        ("neteng@srx-edge> show chassis\r\nneteng@srx-edge>", "srx-edge", "Junos user@host"),
        ("core-sw(config-if)#\r\n",               "core-sw",   "strips config mode"),
        ("ASA-FW/pri/act#\r\n",                   "ASA-FW",    "strips failover context"),
        ("neteng@jump:~$ ls\r\nneteng@jump:~$",   "jump",      "Linux"),
    ]
    for text, expected, label in cases:
        got = detect_hostname(text)
        check(f"detects hostname ({label})", got == expected, f"got {got!r}, want {expected!r}")

    check("returns None when there is no prompt",
          detect_hostname("just some output\r\nmore output") is None)


# ---------------------------------------------------------------------------
# Segmentation
# ---------------------------------------------------------------------------


def test_segmentation() -> None:
    print("\n-- Command segmentation --")

    parser = TranscriptParser()
    stream = (
        "\r\nswitch01#show ip interface brief\r\n"
        "Interface              IP-Address      OK? Method Status\r\n"
        "GigabitEthernet0/1     10.1.1.1        YES NVRAM  up\r\n"
        "GigabitEthernet0/2     unassigned      YES NVRAM  down\r\n"
        "switch01#"
    )
    records = parser.feed(stream)

    check("one command captured", len(records) == 1, f"got {len(records)}: {records}")
    if records:
        record = records[0]
        check("command text is correct",
              record.command == "show ip interface brief", f"got {record.command!r}")
        check("output captured in full",
              "GigabitEthernet0/1" in record.output and "GigabitEthernet0/2" in record.output,
              f"got {record.output!r}")
        check("the prompt is not inside the output",
              "switch01#" not in record.output, f"got {record.output!r}")
        check("prompt recorded alongside", record.prompt == "switch01#", f"got {record.prompt!r}")


def test_multiple_commands() -> None:
    print("\n-- Several commands in sequence --")

    parser = TranscriptParser()
    records = parser.feed(
        "switch01#show clock\r\n"
        "09:41:22.123 UTC Thu Jul 31 2026\r\n"
        "switch01#configure terminal\r\n"
        "Enter configuration commands, one per line.\r\n"
        "switch01(config)#interface Gi0/2\r\n"
        "switch01(config-if)#no shutdown\r\n"
        "switch01(config-if)#"
    )

    commands = [r.command for r in records]
    check("all four commands captured",
          commands == ["show clock", "configure terminal", "interface Gi0/2", "no shutdown"],
          f"got {commands}")
    if len(records) >= 1:
        check("first command's output is its own",
              "09:41:22" in records[0].output, f"got {records[0].output!r}")
    if len(records) >= 4:
        check("config-mode prompt tracked",
              records[3].prompt == "switch01(config-if)#", f"got {records[3].prompt!r}")


def test_split_across_chunks() -> None:
    print("\n-- Stream split across chunks --")

    parser = TranscriptParser()
    # Deliberately split mid-word and mid-line, as TCP will.
    chunks = ["switch01#show ver", "sion\r\nCisco IOS Software, Ver",
              "sion 15.2\r\n", "switch01#"]

    records: list = []
    for chunk in chunks:
        records.extend(parser.feed(chunk))

    check("command reassembled across chunks",
          len(records) == 1 and records[0].command == "show version",
          f"got {[r.command for r in records]}")
    if records:
        check("output reassembled across chunks",
              "Version 15.2" in records[0].output, f"got {records[0].output!r}")


def test_erase_arriving_in_its_own_chunk() -> None:
    """
    An erase must delete what came before it, even chunks later (#272).

    ShellMate sends Ctrl-U before an expanded alias, so the expansion
    replaces the alias the device has already echoed. The device answers
    with backspaces, spaces and backspaces — and that answer arrives as its
    own chunk, after the keystroke echoes it is meant to erase.

    Cleaning each chunk in isolation found nothing to delete in front of
    those backspaces and dropped them, so real sessions recorded
    "arpshow ip arp": the alias glued to its own expansion. The command was
    then unfindable by either the alias or the command, which is half of why
    history search looked broken.

    The bytes below are taken from a session log, not invented.
    """
    print("\n-- An erase split from what it erases --")

    def replay(chunks: list[str]) -> list[str]:
        parser = TranscriptParser()
        records: list = []
        for chunk in chunks:
            records.extend(parser.feed(chunk))
        return [r.command for r in records]

    # "arp" typed a keystroke at a time, then the erase and the expansion.
    alias = replay(["S3-R1#", "a", "r", "p",
                    "\b\b\b   \b\b\bshow ip arp", "\r\n",
                    "Protocol  Address\r\n", "S3-R1#"])
    check("an expanded alias is recorded as the command that ran",
          alias == ["show ip arp"], f"got {alias}")

    # Typed "inmt", corrected to "ints", which expanded.
    corrected = replay(["S3-R1#", "i", "n", "m", "t", "\b \b\b \b", "t", "s",
                        "\b\b\b\b    \b\b\b\bshow ip interface brief", "\r\n",
                        "Interface  IP-Address\r\n", "S3-R1#"])
    check("a corrected typo leaves no trace in the record",
          corrected == ["show ip interface brief"], f"got {corrected}")

    # And a command typed straight through is unaffected.
    plain = replay(["S3-R1#show ", "vrf\r\n", "  Name\r\n", "S3-R1#"])
    check("an ordinary command still reads as itself",
          plain == ["show vrf"], f"got {plain}")


def test_flush_captures_last_command() -> None:
    print("\n-- Flush at end of session --")

    parser = TranscriptParser()
    parser.feed("switch01#reload\r\nProceed with reload? [confirm]")
    check("command still pending before flush",
          parser.pending is not None and parser.pending.command == "reload")

    final = parser.flush()
    check("flush returns the in-flight command",
          final is not None and final.command == "reload", f"got {final}")
    check("its partial output is kept",
          final is not None and "Proceed with reload" in final.output, f"got {final}")


def test_output_not_split_by_hashes() -> None:
    print("\n-- Configuration output is not mistaken for prompts --")

    parser = TranscriptParser()
    records = parser.feed(
        "switch01#show running-config\r\n"
        "Building configuration...\r\n"
        "!\r\n"
        "version 15.2\r\n"
        "!\r\n"
        "hostname switch01\r\n"
        "!\r\n"
        "interface GigabitEthernet0/1\r\n"
        " ip address 10.1.1.1 255.255.255.0\r\n"
        "!\r\n"
        "end\r\n"
        "switch01#"
    )

    check("config captured as one record", len(records) == 1, f"got {len(records)}")
    if records:
        output = records[0].output
        check("whole config body retained",
              "version 15.2" in output and "end" in output and "ip address" in output,
              f"got {output!r}")
        check("comment markers did not split it", output.count("!") >= 4, f"got {output!r}")


def test_junos_edit_banner() -> None:
    print("\n-- Junos edit banner --")

    parser = TranscriptParser()
    records = parser.feed(
        "neteng@srx-edge# set interfaces ge-0/0/0 description uplink\r\n"
        "\r\n"
        "[edit]\r\n"
        "neteng@srx-edge# "
    )
    check("command captured", len(records) == 1, f"got {[r.command for r in records]}")
    if records:
        check("[edit] banner excluded from output",
              "[edit]" not in records[0].output, f"got {records[0].output!r}")


def main() -> int:
    print("=" * 52)
    print("  Transcript and ANSI tests")
    print("=" * 52)

    for test in (
        test_ansi_stripping,
        test_backspace,
        test_carriage_returns,
        test_cursor_movement,
        test_clean_combined,
        test_prompt_matching,
        test_command_extraction,
        test_hostname_detection,
        test_segmentation,
        test_multiple_commands,
        test_split_across_chunks,
        test_erase_arriving_in_its_own_chunk,
        test_flush_captures_last_command,
        test_output_not_split_by_hashes,
        test_junos_edit_banner,
    ):
        try:
            test()
        except Exception as exc:
            failed.append(f"{test.__name__}: raised {type(exc).__name__}: {exc}")
            print(f"  FAIL {test.__name__} raised {type(exc).__name__}: {exc}")

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
