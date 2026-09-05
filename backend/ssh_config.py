"""
ssh_config.py — What OpenSSH already knows about a host (#527).

Anybody arriving from OpenSSH, Termius or VS Code has years of `Host`
aliases in `~/.ssh/config`, with the hostname, the port, the account and
often a bastion already worked out. Retyping fifty of them is exactly why
people keep two tools open.

Two uses, and they are deliberately different in strength:

- **Filling blanks in the connection dialog.** A host typed into the
  dialog that matches a stanza has its empty fields filled from it, and
  nothing else. What was filled is named on screen and can be typed over
  before connecting.
- **Importing stanzas as profiles.** One profile per concrete `Host`, so
  the estate arrives in one action rather than fifty dialogs.

The fill happens in the dialog rather than inside `ssh_handler.connect()`,
which is where the issue sketched it. Nothing above the transport layer
has a channel to report on at that point, so a fill applied there would be
invisible at exactly the moment it changed where the connection was going
— and "the address you typed is not the address this dialled" is not
something to discover from a device that answers wrongly. In the dialog it
is visible by construction and can be undone before anything is sent.

The parsing is paramiko's own `SSHConfig`, not a reimplementation, and the
effective values come from `lookup()` — so `Host *` defaults, `Include`
and pattern matching behave the way OpenSSH behaves rather than the way a
second parser guessed.

**What cannot be expressed is reported, never half-imported.** A stanza
with a `ProxyCommand` describes a connection ShellMate cannot make: it has
no shell to run the command in, and importing it minus its proxy would
produce a profile that looks right, connects to the wrong thing or nothing
at all, and gives no hint why. So such a stanza is listed with its reason
and left out. The same for a pattern — `Host *.example.net` is not a
device and a profile named after a wildcard is a profile that dials
nothing.
"""

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

#: The tag imported profiles are grouped under, so an import can be seen,
#: reviewed and removed as one thing rather than fifty scattered rows.
IMPORT_TAG = "ssh-config"

#: Directives ShellMate cannot honour, and what to say about each. The
#: message names the consequence rather than the directive: "ProxyCommand
#: is unsupported" tells somebody nothing about what would happen if it
#: were ignored, which is the thing they need to weigh.
UNSUPPORTED = {
    "proxycommand":
        "ProxyCommand runs a program to reach the device, and ShellMate has "
        "no shell to run it in. Imported without it, the profile would dial "
        "the address directly — which is either the wrong machine or none.",
    "remotecommand":
        "RemoteCommand runs something instead of a shell. ShellMate opens an "
        "interactive session, so the profile would behave differently from "
        "the same alias in OpenSSH.",
    "localcommand":
        "LocalCommand runs a program on this machine when the session opens. "
        "ShellMate does not run it, and a profile that silently skipped it "
        "would be a different connection wearing the same name.",
}


def config_path() -> Path:
    """
    Where OpenSSH keeps it. The same place on Windows as everywhere else.

    Windows OpenSSH reads `%USERPROFILE%\\.ssh\\config`, which is what
    `Path.home()` resolves to — so there is nothing platform-specific here,
    only the expectation that there might be no file at all.
    """
    return Path.home() / ".ssh" / "config"


def available() -> bool:
    try:
        return config_path().is_file()
    except OSError:                                       # pragma: no cover
        return False


def _parsed():
    """paramiko's own parse of the file, or None if there is nothing to read."""
    path = config_path()
    if not path.is_file():
        return None
    try:
        import paramiko

        parsed = paramiko.SSHConfig()
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            parsed.parse(handle)
        return parsed
    except Exception as exc:
        logger.warning("Could not read %s: %s", path, exc)
        return None


#: A pattern rather than a host. `*`, `?` and `!` are OpenSSH's wildcards.
_PATTERN = re.compile(r"[*?!]")

#: The `Host` lines themselves. `Match` lines deliberately do not match.
_HOST_LINE = re.compile(r"(?mi)^\s*Host\s+(.+?)\s*$")


def _host_patterns() -> list[str]:
    """
    Every name a `Host` line declares, read from the file itself.

    paramiko has `get_hostnames()` for this and it cannot be used: in 5.0.0
    it raises `KeyError: 'host'` on any file containing a `Match` block,
    because it walks every parsed entry expecting a "host" key that a Match
    entry does not have. Match blocks are ordinary in a real config, so
    that is not an edge case — it is "the listing fails for exactly the
    people who have the most in their file".

    `lookup()` is unaffected and is still what produces the values; only
    the list of names comes from here.
    """
    try:
        text = config_path().read_text(encoding="utf-8", errors="replace")
    except OSError:                                       # pragma: no cover
        return []
    names: list[str] = []
    for line in _HOST_LINE.findall(text):
        for token in line.split():
            if token not in names:
                names.append(token)
    return names


def _caveats() -> list[str]:
    """
    Things true of the whole file rather than of one stanza.

    `Match` blocks are the one that matters: their conditions depend on the
    user, the local network or the output of a command, so what OpenSSH
    would apply is not knowable from here. Saying nothing would leave
    somebody with a profile that works from the office and not from home,
    with nothing connecting the two.
    """
    out = []
    try:
        text = config_path().read_text(encoding="utf-8", errors="replace")
    except OSError:                                       # pragma: no cover
        return out
    if re.search(r"(?mi)^\s*Match\s+\S", text):
        out.append(
            "This file has Match blocks. Their conditions can depend on the "
            "user, the network or a command's output, so ShellMate cannot "
            "say which of them OpenSSH would apply — anything they set is "
            "not reflected here.")
    return out


def _entry(name: str, values: dict) -> dict:
    """One stanza, as ShellMate would express it, with what it cannot."""
    refusals = [reason for key, reason in UNSUPPORTED.items() if values.get(key)]

    identity = ""
    files = values.get("identityfile") or []
    if files:
        # OpenSSH allows several; ShellMate holds one. Taking the first and
        # saying so beats silently dropping the rest.
        identity = str(files[0])
        if len(files) > 1:
            refusals.append(
                f"{len(files)} identity files are listed and ShellMate holds "
                f"one. The first, {identity}, is the one that would be used.")

    jump_host, jump_port, jump_user = "", 22, ""
    proxy = (values.get("proxyjump") or "").strip()
    if proxy:
        if "," in proxy:
            refusals.append(
                "ProxyJump names more than one hop. ShellMate goes through "
                "one bastion, so the chain cannot be reproduced.")
        else:
            jump_user, jump_host, jump_port = _hop(proxy)

    return {
        "name": name,
        "hostname": str(values.get("hostname") or name),
        "port": _int(values.get("port"), 22),
        "username": str(values.get("user") or ""),
        "private_key_path": identity,
        "jump_host": jump_host,
        "jump_port": jump_port,
        "jump_username": jump_user,
        "refusals": refusals,
    }


def _hop(spec: str) -> tuple[str, str, int]:
    """`user@host:port` as OpenSSH writes it, each part optional."""
    user = ""
    if "@" in spec:
        user, spec = spec.split("@", 1)
    port = 22
    if ":" in spec:
        spec, _, tail = spec.rpartition(":")
        port = _int(tail, 22)
    return user, spec, port


def _int(value, fallback: int) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return fallback


def stanzas() -> dict:
    """
    Every concrete `Host` in the file, and what cannot be brought across.

    Patterns are left out rather than listed as refusals: `Host *` is not a
    host somebody failed to import, it is how defaults are written, and
    reporting it as a problem would put a permanent complaint in front of
    every correctly-written config file.
    """
    parsed = _parsed()
    if parsed is None:
        return {"path": str(config_path()), "present": False,
                "hosts": [], "caveats": []}

    hosts = []
    for name in sorted(_host_patterns()):
        if _PATTERN.search(name):
            continue
        try:
            hosts.append(_entry(name, parsed.lookup(name)))
        except Exception as exc:                          # pragma: no cover
            logger.warning("Could not read the stanza for %s: %s", name, exc)
    return {"path": str(config_path()), "present": True,
            "hosts": hosts, "caveats": _caveats()}


def match(host: str) -> dict | None:
    """
    What the file says about one typed host, or None if it says nothing.

    A stanza carrying anything ShellMate cannot express comes back with its
    `refusals` filled in and the caller offers nothing: filling the address
    while skipping the ProxyCommand would build exactly the wrong
    connection out of the right file.
    """
    typed = (host or "").strip()
    if not typed:
        return None

    parsed = _parsed()
    if parsed is None:
        return None
    try:
        values = parsed.lookup(typed)
    except Exception:                                     # pragma: no cover
        return None

    # `lookup` always answers, inventing an entry out of nothing when the
    # file says nothing — so "does this file actually mention this host"
    # has to be asked separately, or every hostname typed would report
    # itself as coming from a config that never named it.
    if not _mentioned(parsed, typed):
        return None
    return _entry(typed, values)


def _mentioned(parsed, host: str) -> bool:
    """Whether the file names this host at all, pattern or literal."""
    import fnmatch

    for name in _host_patterns():
        if name == host or (_PATTERN.search(name)
                            and not name.startswith("!")
                            and fnmatch.fnmatch(host, name)):
            # `Host *` alone is a default, not a mention: matching on it
            # would report every connection as coming from the config.
            if name != "*":
                return True
    return False


def importable() -> dict:
    """What an import would do, before it does it."""
    found = stanzas()
    found["ready"] = [h for h in found["hosts"] if not h["refusals"]]
    found["blocked"] = [h for h in found["hosts"] if h["refusals"]]
    return found


def import_profiles(names: list | None = None, tag: str = IMPORT_TAG) -> dict:
    """
    Create one profile per importable stanza.

    A stanza with anything ShellMate cannot express is skipped and returned
    in `skipped` with its reason. Half-importing it would produce a profile
    that looks complete and connects to the wrong place — the failure this
    whole module is arranged to avoid.
    """
    from backend import profiles as profiles_module

    found = importable()
    wanted = set(names or [])
    made, skipped, already = [], [], []

    for host in found["ready"]:
        if wanted and host["name"] not in wanted:
            continue
        fields = {
            "name": host["name"],
            "hostname": host["hostname"],
            "port": host["port"],
            "username": host["username"],
            "connection_type": "ssh",
            "private_key_path": host["private_key_path"],
            "jump_host": host["jump_host"],
            "jump_port": host["jump_port"],
            "jump_username": host["jump_username"],
            "tags": [tag],
        }
        saved = profiles_module.save_profile(fields)
        (already if saved.get("already_saved") else made).append(saved["name"])

    for host in found["blocked"]:
        if wanted and host["name"] not in wanted:
            continue
        skipped.append({"name": host["name"], "why": host["refusals"]})

    logger.info("Imported %d host(s) from %s, skipped %d",
                len(made), config_path(), len(skipped))
    return {"imported": made, "already": already, "skipped": skipped,
            "caveats": found["caveats"], "tag": tag}
