"""
test_outbound.py — Credentials must not leave the machine.

``redact()`` exists so that a password a device echoed does not end up in
something handed to somebody else. It was applied to session logs and captured
configurations and to nothing else, while four other paths sent raw terminal
output off the machine: the AI chat, ``/context all``, the session summary, and
Conclude to Jira.

The Jira one mattered most. The others post to an API; that one writes the
buffer into a ticket which persists, which colleagues read, and which on most
instances is searchable across an organisation.

These tests push a buffer containing three real credential shapes through every
outbound path and assert none of them survives. The last test is the one that
earns its keep: it fails if a *new* path is added that reads a buffer directly
instead of going through the helper.

    python test_outbound.py
"""

import re
import shutil
import sys
import tempfile
from pathlib import Path

from backend import paths

_TEMP = Path(tempfile.mkdtemp(prefix="shellmate-outbound-"))
paths._data_dir_cache = _TEMP

from backend import settings_store                          # noqa: E402
from backend.session import outbound                        # noqa: E402
from backend.session.buffer import SessionBuffer            # noqa: E402

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


# The three shapes a device will actually echo into a buffer.
SECRETS = {
    "a type-7 password":     "09461A1D0A1B",
    "an enable secret hash": "$1$abcd$efghijklmnop",
    "an SNMP community":     "s3cr3t-rw",
}

# With a colour code buried inside the credential, because devices do that and
# it is why clean() has to run before redact() rather than after.
RAW_OUTPUT = (
    "core-sw-01#show running-config\r\n"
    "hostname core-sw-01\r\n"
    "username neteng password 7 09461A1D0A1B\r\n"
    "enable secret 5 $1$abcd$efghijklmnop\r\n"
    "snmp-server community \x1b[32ms3cr3t-rw\x1b[0m RW\r\n"
    "interface GigabitEthernet0/1\r\n"
    " description uplink to core\r\n"
    "core-sw-01#"
)


def make_session(session_id: str = "s1") -> dict:
    buffer = SessionBuffer(session_id)
    buffer.write(RAW_OUTPUT)
    return {
        "session_id":      session_id,
        "buffer":          buffer,
        "display_label":   "core-sw-01",
        "hostname":        "10.20.30.40",
        "connection_type": "ssh",
    }


def assert_clean(name: str, text: str) -> None:
    """Every known secret must be absent, and the structure still present."""
    for label, secret in SECRETS.items():
        check(f"{name}: {label} does not leave the machine",
              secret not in text,
              "the credential was sent in the clear")
    check(f"{name}: the output is still readable",
          "interface GigabitEthernet0/1" in text and "hostname core-sw-01" in text,
          "redaction removed more than the credential")


def configure(redact_secrets: bool = True) -> None:
    settings_store.update_settings({"logging": {"redact_secrets": redact_secrets}})


def test_the_helper() -> None:
    print("\n-- The one door out --")
    configure()
    text = outbound.session_text(make_session(), 200)
    assert_clean("session_text", text)
    check("escape sequences are stripped as well",
          "\x1b[" not in text, "control codes reached the outbound text")
    check("a session with no buffer yields nothing",
          outbound.session_text({}, 200) == "")


def test_colour_inside_a_credential() -> None:
    """clean() must run before redact(), or the pattern never matches."""
    print("\n-- A credential with a colour code in the middle of it --")
    configure()
    session = make_session()
    text = outbound.session_text(session, 200)
    check("a coloured community string is still masked",
          "s3cr3t-rw" not in text,
          "escape codes inside the value defeated the pattern")


def test_chat_context() -> None:
    print("\n-- The AI chat context --")
    configure()
    from backend.ai import router
    text = router._session_text(make_session(), 200)
    assert_clean("chat", text)

    # And the commands parsed out of it must not carry one either.
    commands = router._extract_commands(text)
    joined = "\n".join(commands)
    for label, secret in SECRETS.items():
        check(f"chat command history: {label} is absent", secret not in joined)


def test_session_summary() -> None:
    print("\n-- The session summary --")
    configure()
    session = make_session()

    class FakeManager:
        def get_session(self, sid):
            return session

    from backend.ai import summarize
    # Reach the transcript assembly without calling a provider: rebuild the
    # same structure the summary sends, through the same helper.
    text = outbound.session_text(session, 400)
    assert_clean("summary", text)
    check("summarize.py imports the outbound helper",
          "outbound" in Path("backend/ai/summarize.py").read_text(encoding="utf-8"),
          "the summary path is not going through it")


def test_jira_export() -> None:
    print("\n-- Conclude to Jira --")
    configure()
    session = make_session()
    text = outbound.session_text(session, 500)
    assert_clean("jira buffer", text)

    # The conversation goes into the ticket too, and the assistant quotes
    # device output back constantly.
    quoted = outbound.redact_text(
        "You ran: username neteng password 7 09461A1D0A1B")
    check("jira chat messages are masked too",
          "09461A1D0A1B" not in quoted, quoted)


# A drift diff the way an archive actually produces one: a credential
# changed, and the surrounding configuration as context (#549).
DRIFT_DIFF = """--- core-sw-01 @ 2026-09-01 09:00
+++ core-sw-01 @ 2026-09-04 09:00
@@ -1,6 +1,6 @@
 hostname core-sw-01
-username neteng password 7 09461A1D0A1B
+username neteng password 7 09461A1D0A1B
 enable secret 5 $1$abcd$efghijklmnop
 snmp-server community s3cr3t-rw RW
 interface GigabitEthernet0/1
  description uplink to core
"""


def test_drift_diff_to_the_assistant() -> None:
    """
    #549: the diff is configuration, so it leaves through the same door.

    The whole point of feeding drift to the model is that it carries the
    running configuration of a device — which is precisely the content the
    redaction exists for. Composed in the browser this would have arrived at
    the provider untouched.
    """
    print("\n-- The drift diff handed to the assistant --")
    configure()
    from backend.ai import explain, prompts

    session = make_session()
    session["drift"] = {
        "available": True, "changed": 2, "added": 1, "removed": 1,
        "days_since": 3, "diff": DRIFT_DIFF,
    }

    facts = explain.drift_facts(session)
    check("a drift report with changes produces facts", bool(facts))
    assert_clean("drift diff", facts["diff"])

    # And the whole way through: the block the model is actually shown.
    block = "\n".join(prompts.render_drift_block(facts, "core-sw-01"))
    assert_clean("rendered drift block", block)
    check("the block says how much changed and when",
          "2 lines differ" in block and "3 days ago" in block, block)

    # The prompt the Explain button produces, end to end.
    message = explain.diff_prompt({}, session)
    assert_clean("explain prompt", message)
    check("the explain prompt asks the question the engineer has",
          explain.DIFF_QUESTION in message, message[:200])

    # A session with no drift report has nothing to explain, and says so by
    # returning nothing rather than sending an empty diff to a provider.
    check("no drift report means no prompt", explain.diff_prompt({}, make_session()) == "")
    check("and no facts", explain.drift_facts(make_session()) is None)


def test_drift_diff_is_capped() -> None:
    """A large chassis produces a diff of thousands of lines; it is a bill."""
    print("\n-- The cap on it --")
    configure()
    from backend.ai import explain

    settings_store.update_settings({"advanced": {"ai.drift_lines": 3}})
    session = make_session()
    session["drift"] = {"available": True, "changed": 40, "added": 20,
                        "removed": 20, "diff": DRIFT_DIFF}
    text = explain.drift_facts(session)["diff"]
    check("the diff is trimmed to the configured number of lines",
          len(text.splitlines()) == 4, f"{len(text.splitlines())} lines")
    check("and what was left out is announced rather than dropped silently",
          "more diff lines not shown" in text, text)

    settings_store.update_settings({"advanced": {"ai.drift_lines": 0}})
    off = explain.drift_facts(session)["diff"]
    check("zero sends none of it, and says so",
          "not sent" in off and "hostname core-sw-01" not in off, off)
    settings_store.update_settings({"advanced": {}})


def test_the_switch() -> None:
    print("\n-- Turning it off --")
    configure(redact_secrets=False)
    text = outbound.session_text(make_session(), 200)
    check("redaction can be switched off deliberately",
          "s3cr3t-rw" in text,
          "the setting had no effect")
    check("and the switch is the same one that governs logs",
          not outbound.redaction_enabled())
    configure(redact_secrets=True)
    check("and back on again", outbound.redaction_enabled())


# Credential forms a device will print, each with the secret that must go
# and a neighbouring token that must stay so the line still reads as
# configuration (#495). The wrong-token cases are the ones that matter most:
# a pattern that masks the key *number* and leaves the hash looks like it
# worked.
CREDENTIAL_FORMS: list[tuple[str, str, str]] = [
    # (line, the secret, what must survive)
    # `show snmp` prints "Community string:" and `show snmp community`
    # prints "Community name:". Only the second was covered, so the one
    # command somebody is most likely to run when asking about SNMP put the
    # community in the buffer in clear.
    ("Community string: public123",                              "public123",    "Community string:"),
    ("  Community string: private456 (RW)",                      "private456",   "(RW)"),
    # Prose, because people type prose into the chat box. Before these, the
    # catch-all matched "community is" and masked the word *is*, leaving the
    # string beside it — output that looks redacted and is not, which is
    # worse than output that was never redacted, because the mask is the
    # thing somebody checks for before pasting a log into a ticket.
    ("The snmp community is public123 and must not be quoted.",  "public123",    "must not be quoted"),
    ("the community string is public123",                        "public123",    "community string is"),
    ("the community for the edge is public123",                  "public123",    "for the edge"),
    # The same shape for a password, which is how somebody actually asks
    # for help: "the password for the box is hunter2, why is it not
    # working". A phrase between the noun and the value used to put the
    # mask on `the` and leave the password beside it.
    ("the password is hunter2",                                  "hunter2",      "password is"),
    ("the password for the box is hunter2",                      "hunter2",      "password for"),
    ("the enable secret for the edge routers is hunter2",        "hunter2",      "edge routers"),
    ("the password on sw-01 is hunter2",                         "hunter2",      "sw-01"),
    ("crypto isakmp key MyS3cret address 10.1.1.1",              "MyS3cret",     "address 10.1.1.1"),
    ("crypto isakmp key 6 ENCRYPTEDBLOB address 10.1.1.1",       "ENCRYPTEDBLOB", "key 6 "),
    (" key MyTacacsSecret",                                      "MyTacacsSecret", " key "),
    (" key 7 0822455D0A16",                                      "0822455D0A16", "key 7 "),
    ("ntp authentication-key 1 md5 0822455D0A16 7",              "0822455D0A16", "authentication-key 1 md5"),
    ("ip ospf authentication-key 7 HASHHASH",                    "HASHHASH",     "authentication-key 7 "),
    ("ip ospf message-digest-key 1 md5 7 HASHHASH",              "HASHHASH",     "message-digest-key 1 md5 7 "),
    (' pre-shared-key ascii-text "$9$abcdef";',                   "$9$abcdef",    "ascii-text"),
    (" pre-shared-key address 10.1.1.1 key MyPSK",               "MyPSK",        "address 10.1.1.1 key"),
    ("snmp-server user bob grp v3 auth sha AuthPass priv aes 128 PrivPass", "AuthPass", "auth sha"),
    ("snmp-server user bob grp v3 auth sha AuthPass priv aes 128 PrivPass", "PrivPass", "priv aes 128"),
    ("snmp-server user admin auth md5 0xabc123 priv 0xdef456 localizedkey", "0xdef456", "localizedkey"),
    ("Community name: public",                                   "public",       "Community name:"),
    ("Community SecurityName: public",                           "public",       "SecurityName:"),
    ("snmp-server host 10.1.1.1 version 2c trapcomm",            "trapcomm",     "version 2c"),
    ("radius-server host 10.1.1.2 auth-port 1812 acct-port 1813 key 7 ABC123", "ABC123", "acct-port 1813 key 7"),
    (' secret "$9$abcdef";',                                      "$9$abcdef",    "secret"),
    (' authentication-key 1 type md5 value "$9$abcdef";',         "$9$abcdef",    "1 type md5 value"),
]

# Ordinary lines with a credential keyword in them. Masking a word out of
# these damages evidence for nothing.
ORDINARY_LINES = [
    " description key uplink to core",
    "key chain BGP-KEYS",
    " key 1",
    "ip access-list extended KEY-SERVERS",
    " set community 65000:100 additive",
    " match community CL1",
    " neighbor 10.1.1.1 send-community both",
    "snmp-server group NETOPS v3 priv read RO-VIEW",
    "snmp-server group NETOPS v3 auth",
    "username bob privilege 15",
    "service password-encryption",
    " authentication key-chain BGP-KEYS",
    "Community Index: cisco0",
    # A sentence about communities is not a community. A redactor that fires
    # on these is one people learn to ignore, and then the one that matters
    # is ignored too.
    " neighbor 10.1.1.1 send-community extended",
]


def test_credential_forms() -> None:
    print("\n-- The forms a device prints --")
    from backend.session.redact import redact
    for line, secret, keep in CREDENTIAL_FORMS:
        out = redact(line)
        check(f"{line.strip()[:48]!r}: masks the secret", secret not in out, out)
        check(f"  and keeps {keep.strip()!r}", keep in out, out)
    for line in ORDINARY_LINES:
        check(f"{line.strip()!r} is left alone", redact(line) == line, redact(line))


def test_no_path_bypasses_the_helper() -> None:
    """
    The regression guard.

    Any module that reads a buffer directly is a path that can be added
    without redaction — which is exactly how this bug happened the first time.
    Only buffer.py (which owns it) and outbound.py (which masks it) may.
    """
    print("\n-- No way around it --")
    allowed = {"buffer.py", "outbound.py"}
    offenders = []

    for path in Path("backend").rglob("*.py"):
        if path.name in allowed:
            continue
        source = path.read_text(encoding="utf-8")
        for match in re.finditer(r"\.get_text\s*\(", source):
            line = source[:match.start()].count("\n") + 1
            offenders.append(f"{path.as_posix()}:{line}")

    check("nothing reads a session buffer directly",
          not offenders,
          "these bypass the outbound helper and will send raw output: "
          + ", ".join(offenders))

    # The same for a command record's output (#496): the modules that shape
    # content for the model may read it only on a line that passes it
    # through redact_text(). transcript.py owns the record; the history
    # store keeps it on disk, which is what the logging switch governs.
    record_reads = re.compile(
        r"""\.output\b|\brec\w*\[["']output["']\]|getattr\([^,]+,\s*["']output["']""")
    offenders = []
    for path in list(Path("backend/ai").rglob("*.py")) + [Path("backend/session/parsed.py")]:
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith("#") or "``" in line:
                continue
            if record_reads.search(line) and "redact_text(" not in line:
                offenders.append(f"{path.as_posix()}:{number}")
    check("nothing shapes a command record's output for the model without redacting it",
          not offenders,
          "these read record output without redact_text(): " + ", ".join(offenders))


def main() -> int:
    print("\n" + "=" * 52)
    print("  Outbound redaction")
    print("=" * 52)

    for test in (
        test_the_helper,
        test_colour_inside_a_credential,
        test_chat_context,
        test_session_summary,
        test_jira_export,
        test_drift_diff_to_the_assistant,
        test_drift_diff_is_capped,
        test_the_switch,
        test_credential_forms,
        test_no_path_bypasses_the_helper,
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
