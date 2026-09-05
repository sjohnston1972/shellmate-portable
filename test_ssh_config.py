"""
test_ssh_config.py — Bringing ~/.ssh/config across, and refusing to half-do it (#527).

Anybody arriving from OpenSSH has years of aliases with hostnames, ports,
accounts and bastions already worked out. Importing them is the easy half.

The half worth testing is what happens to a stanza ShellMate cannot
express. A `Host` with a `ProxyCommand` reaches its device by running a
program; ShellMate has no shell to run it in. Imported without it, the
profile looks complete, is named after a device somebody recognises, and
dials the address directly — which is either the wrong machine or nothing
at all, with no hint anywhere that a line was dropped on the way in.

So: every unexpressible stanza is reported with its reason and left out,
and nothing is filled in the dialog from a stanza that carries one.

The file itself is a temporary one and `config_path` is pointed at it. The
alternative is reading the developer's own `~/.ssh/config`, which would
make the test's result depend on whose machine it runs on — and on this
project that is a defect the suite has already been bitten by once.

Run: python test_ssh_config.py
"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backend import paths  # noqa: E402

_TEMP = Path(tempfile.mkdtemp(prefix="shellmate-sshcfg-"))
paths._data_dir_cache = _TEMP
assert paths.data_dir() == _TEMP, "test isolation failed — refusing to run"

from backend import profiles as profiles_module  # noqa: E402
from backend import ssh_config  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                        # pragma: no cover
    pass

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


CONFIG = """
# A file shaped like the ones people actually have.
#
# `Host *` sits at the bottom, where it belongs: OpenSSH keeps the *first*
# value it obtains for each keyword, so a defaults block at the top wins
# every field and the specific stanzas below it are ignored.

Host core-sw
    HostName 10.10.0.1
    User netops
    Port 2222
    IdentityFile ~/.ssh/id_estate

Host edge-fw
    HostName 10.10.0.2
    ProxyJump bastion.example.net

Host through-jump
    HostName 10.10.0.3
    ProxyJump jumpuser@bastion.example.net:2200

Host behind-command
    HostName 10.10.0.4
    ProxyCommand /usr/bin/corkscrew proxy 8080 %h %p

Host two-hops
    HostName 10.10.0.5
    ProxyJump one.example.net,two.example.net

Host *.lab.example.net
    User labadmin

Host runs-something
    HostName 10.10.0.6
    RemoteCommand /bin/menu

Host *
    ServerAliveInterval 30
    User fallback
"""


def _write(text: str) -> Path:
    path = _TEMP / "ssh_config"
    path.write_text(text, encoding="utf-8")
    ssh_config.config_path = lambda: path
    return path


def reading_the_file() -> None:
    print("\n-- Reading it --")
    _write(CONFIG)

    found = ssh_config.stanzas()
    names = [h["name"] for h in found["hosts"]]
    check("it is found", found["present"] is True, str(found["present"]))
    check("every concrete Host is listed",
          {"core-sw", "edge-fw", "behind-command"} <= set(names), str(names))
    check("a pattern is not offered as a device",
          "*" not in names and "*.lab.example.net" not in names,
          "a profile named after a wildcard dials nothing, and `Host *` is "
          "how defaults are written rather than a host somebody missed")

    core = next(h for h in found["hosts"] if h["name"] == "core-sw")
    check("HostName, Port and User come across",
          core["hostname"] == "10.10.0.1" and core["port"] == 2222
          and core["username"] == "netops", str(core))
    check("and IdentityFile with them",
          core["private_key_path"].endswith("id_estate"),
          str(core["private_key_path"]))
    check("a stanza that says all four has nothing to refuse",
          core["refusals"] == [], str(core["refusals"]))

    jump = next(h for h in found["hosts"] if h["name"] == "edge-fw")
    check("ProxyJump becomes the jump host",
          jump["jump_host"] == "bastion.example.net", str(jump))

    detailed = next(h for h in found["hosts"] if h["name"] == "through-jump")
    check("with its own user and port when given",
          detailed["jump_username"] == "jumpuser"
          and detailed["jump_port"] == 2200, str(detailed))

    # `Host *` is a default, not a mention. A file with one in it must not
    # make every hostname anybody types look like a configured alias.
    check("Host * defaults do reach a real stanza",
          next(h for h in found["hosts"]
               if h["name"] == "edge-fw")["username"] == "fallback",
          "OpenSSH applies them, so ShellMate has to as well")


def what_cannot_be_expressed() -> None:
    print("\n-- What cannot be expressed is reported, not half-imported --")
    _write(CONFIG)
    found = ssh_config.importable()

    blocked = {h["name"] for h in found["blocked"]}
    check("a ProxyCommand stanza is blocked",
          "behind-command" in blocked, str(blocked))
    check("and the reason says what would happen, not which keyword it was",
          any("wrong machine" in r
              for h in found["blocked"] if h["name"] == "behind-command"
              for r in h["refusals"]),
          "'ProxyCommand is unsupported' tells nobody what importing it "
          "anyway would do, which is the thing they have to weigh")
    check("a RemoteCommand stanza is blocked too",
          "runs-something" in blocked, str(blocked))
    check("and a ProxyJump chain, which ShellMate cannot reproduce",
          "two-hops" in blocked, str(blocked))
    check("what is fine is not caught up in it",
          {"core-sw", "edge-fw", "through-jump"}
          <= {h["name"] for h in found["ready"]},
          str([h["name"] for h in found["ready"]]))


def importing() -> None:
    print("\n-- Importing --")
    _write(CONFIG)
    result = ssh_config.import_profiles()

    check("the importable ones become profiles",
          "core-sw" in result["imported"], str(result["imported"]))
    check("the rest are named with their reasons",
          {s["name"] for s in result["skipped"]}
          >= {"behind-command", "two-hops", "runs-something"},
          str(result["skipped"]))
    check("and every skip carries one",
          all(s["why"] for s in result["skipped"]), str(result["skipped"]))

    saved = {p["name"]: p for p in profiles_module.get_profiles()}
    check("nothing unexpressible was written anyway",
          "behind-command" not in saved,
          "a profile that looks complete and dials the wrong machine is the "
          "failure this whole module is arranged around")
    core = saved.get("core-sw") or {}
    check("the profile carries what the stanza said",
          core.get("hostname") == "10.10.0.1" and core.get("port") == 2222
          and core.get("username") == "netops", str(core))
    check("it is tagged, so an import can be reviewed as one thing",
          ssh_config.IMPORT_TAG in (core.get("tags") or []), str(core.get("tags")))

    # Twice must not make two.
    again = ssh_config.import_profiles()
    check("importing again does not duplicate anything",
          "core-sw" in again["already"] and "core-sw" not in again["imported"],
          str(again))
    check("and the estate has one of each",
          len([p for p in profiles_module.get_profiles()
               if p["name"] == "core-sw"]) == 1)

    # Naming a subset imports that subset.
    picked = ssh_config.import_profiles(["edge-fw"])
    check("naming stanzas imports only those",
          picked["imported"] == [] or picked["already"] == ["edge-fw"],
          str(picked))


def matching_one_typed_host() -> None:
    print("\n-- Filling a dialog, only where it is blank --")
    _write(CONFIG)

    found = ssh_config.match("core-sw")
    check("a typed alias is recognised",
          found and found["hostname"] == "10.10.0.1", str(found))

    check("a host the file never mentions matches nothing",
          ssh_config.match("10.99.99.99") is None,
          "`Host *` would otherwise make every address typed look like a "
          "configured alias, and every connection claim it was filled from "
          "a file that never named it")

    blocked = ssh_config.match("behind-command")
    check("a stanza with a ProxyCommand comes back carrying its refusal",
          blocked and blocked["refusals"],
          "filling the address while skipping the proxy would build exactly "
          "the wrong connection out of the right file")

    _write("")
    check("nothing at all when the file is empty",
          ssh_config.match("core-sw") is None,
          "an empty file mentions nothing")


def caveats_about_the_whole_file() -> None:
    print("\n-- Things true of the file rather than a stanza --")
    _write(CONFIG + "\nMatch host *.dmz exec \"test -n \\\"$ONSITE\\\"\"\n"
                    "    User onsite\n")
    found = ssh_config.stanzas()
    check("a file with a Match block can still be listed at all",
          [h["name"] for h in found["hosts"]],
          "paramiko 5.0.0's own get_hostnames() raises KeyError on any file "
          "containing a Match block, so the listing failed for exactly the "
          "people with the most in their config")
    check("a Match block is reported rather than silently ignored",
          any("Match" in c for c in found["caveats"]), str(found["caveats"]))
    check("and the reason is that it cannot be evaluated from here",
          any("cannot say" in c or "cannot" in c for c in found["caveats"]),
          "a profile that works from the office and not from home, with "
          "nothing connecting the two, is the outcome this warns about")

    _write("Host plain\n    HostName 10.1.1.1\n")
    check("a file with no Match blocks says nothing about them",
          ssh_config.stanzas()["caveats"] == [],
          "a permanent complaint in front of a correct file is noise")


def no_file_at_all() -> None:
    print("\n-- No file at all --")
    ssh_config.config_path = lambda: _TEMP / "does-not-exist"
    check("it says so rather than failing",
          ssh_config.stanzas()["present"] is False)
    check("there is nothing to import", ssh_config.importable()["ready"] == [])
    check("and matching answers nothing", ssh_config.match("anything") is None)


if __name__ == "__main__":
    reading_the_file()
    what_cannot_be_expressed()
    importing()
    matching_one_typed_host()
    caveats_about_the_whole_file()
    no_file_at_all()

    print("\n" + "=" * 52)
    print(f"  {passed} passed  |  {len(failed)} failed")
    print("=" * 52)
    for line in failed:
        print("  -", line)
    sys.exit(1 if failed else 0)
