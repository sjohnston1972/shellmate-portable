"""
platforms.py — What ShellMate knows about each kind of device.

A network estate is rarely one vendor.  The same intent — "turn paging off",
"show me the interfaces", "dump the running config" — is a different command
on IOS, NX-OS, Junos and PAN-OS, and the differences are exactly the kind of
detail that wastes an engineer's attention while they are trying to fix
something.

Each profile collects everything platform-specific in one place:

``paging_off``          Sent on connect, so nobody types ``terminal length 0``
                       for the hundredth time this week.
``show_run``           How to retrieve the running configuration.
``version_command``    How to ask what the device is.
``aliases``            Short names mapped to the real command per platform.
``dangerous_commands``  What the guardrail holds for confirmation.
``config_mode_markers`` Prompt fragments meaning "you are editing live config".

Profiles are **data**, not code.  The built-in set below is written to
``platforms.json`` in the data directory on first run, and the file is read
back in preference to the defaults.  A new platform, or a correction to an
existing one, is then a text edit rather than a rebuild — which matters when
the person who needs it is on a customer site with the executable on a stick
and no Python toolchain anywhere.
"""

import json
import logging
from dataclasses import dataclass, field, asdict

from backend import jsonfile, paths

logger = logging.getLogger(__name__)

GENERIC = "generic"


@dataclass
class PlatformProfile:
    """Everything ShellMate knows about one family of device."""

    id: str
    name: str
    paging_off: str = ""
    show_run: str = ""
    version_command: str = ""
    # Substrings that identify this platform in a banner or version output.
    # Matched case-insensitively; the longest match wins so that "nx-os" beats
    # the "cisco" that also appears in the same banner.
    signatures: list[str] = field(default_factory=list)
    aliases: dict[str, str] = field(default_factory=dict)
    dangerous_commands: list[str] = field(default_factory=list)
    config_mode_markers: list[str] = field(default_factory=list)
    comment_prefix: str = "!"

    # House rules for the assistant on this platform (#557).
    #
    # A prompt preset per platform without a fourth global prompt:
    # "Junos: prefer `| display set`, and remind about `commit
    # confirmed`"; "ASA: suggest `packet-tracer` before an ACL
    # change". A shop with a niche platform teaches it once here
    # instead of editing a prompt every colleague also uses.
    #
    # Appended to the *cached* system preamble, so on Claude it costs
    # nothing per turn — and only when the fingerprint is confident
    # enough to act on, because notes for the wrong platform are
    # worse than none. The generic profile stays silent, which is the
    # never-guess rule applied to advice rather than to commands.
    assistant_notes: str = ""

    # --- Things that will happen to the device unless someone intervenes ----
    #
    # Regular expressions, matched against both what is typed and what the
    # device says back. Named groups give the timing: ``h`` hours, ``m``
    # minutes, ``s`` seconds, ``at`` an absolute HH:MM.
    #
    # Both directions are searched because they answer different questions.
    # The typed command says a reload was *asked for* — known immediately, and
    # true even if the reply scrolls past. The device's own answer carries the
    # authoritative time, which is the only one worth counting down from.
    reload_patterns: list[str] = field(default_factory=list)
    # The device's own scheduling banner — "Reload scheduled in 5 minutes by
    # admin" (#248). Where a platform has these, a typed reload is tracked but
    # the countdown arms only when one of them lands: the command can still be
    # aborted at the device's own [confirm] prompt, and a countdown for a
    # reload that never started is a false alarm with a deadline. A platform
    # with none of these keeps the typed-command countdown.
    reload_announce_patterns: list[str] = field(default_factory=list)
    reload_cancel_patterns: list[str] = field(default_factory=list)
    # What to type to call a scheduled reload off (#292, #584). Offered by the
    # last-chance warning and by the tab's context menu, so the answer to "it
    # is about to reboot" is a button rather than remembering the command
    # under pressure.
    #
    # Blank where a platform has no such command, and both callers then say so
    # rather than offering one: this is a string typed straight at a device
    # that is counting down to a reboot, so a guess costs the seconds that
    # were left. Filled in only from a vendor's own command reference. Several
    # platforms below have one recorded here but no `reload_patterns` — the
    # command is right, ShellMate simply does not yet read a scheduled reload
    # on them, and an install that adds the patterns to platforms.json gets
    # the cancel with them.
    reload_cancel_command: str = ""
    # Junos-style "commit confirmed": rolls itself back unless confirmed. Not
    # a reboot, so it is tracked separately and worded differently.
    commit_confirm_patterns: list[str] = field(default_factory=list)
    commit_cancel_patterns: list[str] = field(default_factory=list)
    # Applying configuration (#407): how to enter and leave configuration
    # mode, and how to make it stick. Blank means ShellMate will not push
    # to this platform — a wrong command here is worse than none.
    config_enter: str = ""
    config_exit: str = ""
    save_command: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Built-in profiles
# ---------------------------------------------------------------------------

BUILTIN: dict[str, PlatformProfile] = {
    "ios": PlatformProfile(
        id="ios",
        config_enter="configure terminal",
        config_exit="end",
        save_command="write memory",
        name="Cisco IOS / IOS-XE",
        paging_off="terminal length 0",
        show_run="show running-config",
        version_command="show version",
        signatures=["cisco ios software", "ios-xe", "ios software", "cisco ios"],
        aliases={
            'acl': 'show access-lists',
            'alarms': 'show facility-alarm status',
            'arp': 'show ip arp',
            'bgp': 'show ip bgp summary',
            'cdp': 'show cdp neighbors detail',
            'clock': 'show clock',
            'counters': 'show interfaces counters',
            'cpu': 'show processes cpu sorted | exclude 0.00',
            'default': 'show ip route 0.0.0.0',
            'desc': 'show interfaces description',
            'dhcp': 'show ip dhcp binding',
            'diff': 'show archive config differences',
            'eigrp': 'show ip eigrp neighbors',
            'errors': 'show interfaces | include line protocol|error|CRC|drop',
            'flash': 'show flash:',
            'hsrp': 'show standby brief',
            'int': 'show ip interface brief',
            'ints': 'show ip interface brief',
            'inv': 'show inventory',
            'lldp': 'show lldp neighbors detail',
            'log': 'show logging',
            'mac': 'show mac address-table',
            'mem': 'show memory statistics',
            'nat': 'show ip nat translations',
            'nei': 'show cdp neighbors',
            'ntp': 'show ntp associations',
            'ospf': 'show ip ospf neighbor',
            'port': 'show interfaces status',
            'power': 'show power inline',
            'routes': 'show ip route',
            'run': 'show running-config',
            'start': 'show startup-config',
            'stp': 'show spanning-tree summary',
            'temp': 'show environment temperature',
            'trunks': 'show interfaces trunk',
            'uptime': 'show version | include uptime',
            'users': 'show users',
            'ver': 'show version',
            'vlans': 'show vlan brief',
            'vrf': 'show vrf',
            'vrrp': 'show vrrp brief',
        },
        dangerous_commands=[
            "reload", "write erase", "erase startup-config", "erase nvram:",
            "delete flash:", "format flash:", "shutdown", "no shutdown",
            "clear counters", "clear ip bgp", "clear line", "no router",
            "no interface", "no vlan", "boot system",
        ],
        config_mode_markers=["(config", "(conf-"],
        comment_prefix="!",
        reload_patterns=[
            # Typed: "reload in 90", "reload in 1:30", "reload at 23:00".
            r"^\s*reload\s+in\s+(?P<h>\d+):(?P<m>\d{1,2})\b",
            r"^\s*reload\s+in\s+(?P<m>\d+)\b",
            r"^\s*reload\s+at\s+(?P<at>\d{1,2}:\d{2})\b",
        ],
        reload_announce_patterns=[
            # The SHUTDOWN banner, and only this (#291).
            #
            # "Reload scheduled in 1 minute by steven on vty0" was in this
            # list, and it is printed *before* the [confirm] prompt — so it
            # announces what the device is being asked to do, not what it is
            # doing. Answering no, or simply walking away, leaves nothing
            # scheduled, and a countdown started from that line is counting
            # down to something that will never happen.
            #
            # The banner below is printed once the reload is genuinely armed,
            # and repeated as the time approaches.
            r"SHUTDOWN\s+in\s+(?P<h>\d+):(?P<m>\d{2}):(?P<s>\d{2})",
        ],
        reload_cancel_patterns=[
            r"^\s*reload\s+cancel\b",
            r"SHUTDOWN\s+ABORTED",
            r"Reload\s+cancelled",
        ],
        reload_cancel_command="reload cancel",
    ),
    "nxos": PlatformProfile(
        id="nxos",
        config_enter="configure terminal",
        config_exit="end",
        save_command="copy running-config startup-config",
        name="Cisco NX-OS",
        paging_off="terminal length 0",
        show_run="show running-config",
        version_command="show version",
        signatures=["nx-os", "nxos", "nexus"],
        aliases={
            'acl': 'show access-lists',
            'alarms': 'show logging last 50',
            'arp': 'show ip arp',
            'bgp': 'show ip bgp summary',
            'cdp': 'show cdp neighbors detail',
            'clock': 'show clock',
            'counters': 'show interface counters',
            'cpu': 'show system resources',
            'default': 'show ip route 0.0.0.0/0',
            'desc': 'show interface description',
            'dhcp': 'show ip dhcp binding',
            'eigrp': 'show ip eigrp neighbors',
            'errors': 'show interface counters errors',
            'flash': 'dir bootflash:',
            'hsrp': 'show hsrp brief',
            'int': 'show interface brief',
            'ints': 'show interface brief',
            'inv': 'show inventory',
            'lldp': 'show lldp neighbors detail',
            'log': 'show logging last 100',
            'mac': 'show mac address-table',
            'mem': 'show system resources',
            'nei': 'show cdp neighbors',
            'ntp': 'show ntp peer-status',
            'ospf': 'show ip ospf neighbor',
            'port': 'show interface status',
            'power': 'show environment power',
            'routes': 'show ip route',
            'run': 'show running-config',
            'start': 'show startup-config',
            'stp': 'show spanning-tree summary',
            'temp': 'show environment temperature',
            'trunks': 'show interface trunk',
            'uptime': 'show version | include uptime',
            'users': 'show users',
            'ver': 'show version',
            'vlans': 'show vlan brief',
            'vpc': 'show vpc',
            'vrf': 'show vrf',
            'vrrp': 'show vrrp',
        },
        dangerous_commands=[
            "reload", "write erase", "shutdown", "no shutdown",
            "clear counters", "delete bootflash:", "no feature",
        ],
        config_mode_markers=["(config"],
        comment_prefix="!",
        reload_patterns=[
            r"^\s*reload\s+in\s+(?P<h>\d+):(?P<m>\d{1,2})\b",
            r"^\s*reload\s+in\s+(?P<m>\d+)\b",
            r"^\s*reload\s+at\s+(?P<at>\d{1,2}:\d{2})\b",
        ],
        reload_announce_patterns=[
            # Split out for the same reason as IOS (#291): the countdown
            # arms on the device's banner, not on the command being typed.
            r"SHUTDOWN\s+in\s+(?P<h>\d+):(?P<m>\d{2}):(?P<s>\d{2})",
        ],
        reload_cancel_patterns=[
            r"^\s*reload\s+cancel\b",
            r"SHUTDOWN\s+ABORTED",
        ],
        reload_cancel_command="reload cancel",
    ),
    "asa": PlatformProfile(
        id="asa",
        config_enter="configure terminal",
        config_exit="end",
        save_command="write memory",
        name="Cisco ASA",
        paging_off="terminal pager 0",
        show_run="show running-config",
        version_command="show version",
        signatures=["adaptive security appliance", "cisco asa"],
        aliases={
            'acl': 'show access-list',
            'arp': 'show arp',
            'clock': 'show clock',
            'conns': 'show conn count',
            'cpu': 'show cpu usage',
            'failover': 'show failover',
            'ha': 'show failover',
            'int': 'show interface ip brief',
            'ints': 'show interface ip brief',
            'log': 'show logging',
            'mem': 'show memory',
            'nat': 'show nat',
            'routes': 'show route',
            'run': 'show running-config',
            'sessions': 'show conn count',
            'ver': 'show version',
            'xlate': 'show xlate',
        },
        dangerous_commands=[
            "reload", "write erase", "clear configure", "shutdown",
            "no shutdown", "failover active", "no failover",
        ],
        # The ASA schedules and cancels a reload with the same words IOS
        # does. No reload_patterns here yet, so nothing arms a countdown on
        # an ASA — see the field's own note.
        reload_cancel_command="reload cancel",
        config_mode_markers=["(config"],
        comment_prefix="!",
    ),
    "junos": PlatformProfile(
        id="junos",
        config_enter="configure",
        config_exit="commit and-quit",
        name="Juniper Junos",
        paging_off="set cli screen-length 0",
        show_run="show configuration | display set",
        version_command="show version",
        signatures=["junos", "juniper"],
        aliases={
            'alarms': 'show chassis alarms',
            'arp': 'show arp',
            'bgp': 'show bgp summary',
            'clock': 'show system uptime',
            'cpu': 'show chassis routing-engine',
            'default': 'show route 0.0.0.0/0',
            'desc': 'show interfaces descriptions',
            'dhcp': 'show dhcp server binding',
            'diff': 'show | compare',
            'disk': 'show system storage',
            'errors': 'show interfaces extensive | match error',
            'ha': 'show chassis cluster status',
            'int': 'show interfaces terse',
            'ints': 'show interfaces terse',
            'inv': 'show chassis hardware',
            'lldp': 'show lldp neighbors',
            'log': 'show log messages | last 100',
            'mac': 'show ethernet-switching table',
            'mem': 'show system memory',
            'nat': 'show security nat source summary',
            'nei': 'show lldp neighbors',
            'ntp': 'show ntp associations',
            'ospf': 'show ospf neighbor',
            'policy': 'show security policies',
            'power': 'show chassis power',
            'routes': 'show route',
            'run': 'show configuration | display set',
            'stp': 'show spanning-tree bridge',
            'temp': 'show chassis environment',
            'uptime': 'show system uptime',
            'users': 'show system users',
            'ver': 'show version',
            'vlans': 'show vlans',
            'vrf': 'show route instance',
            'vrrp': 'show vrrp summary',
        },
        dangerous_commands=[
            "request system reboot", "request system halt", "delete",
            "rollback 0", "load override", "commit", "deactivate",
            "request system zeroize",
        ],
        config_mode_markers=["[edit"],
        comment_prefix="#",
        reload_patterns=[
            r"^\s*request\s+system\s+reboot\s+in\s+(?P<m>\d+)\b",
            r"^\s*request\s+system\s+reboot\s+at\s+(?P<at>\d{1,2}:\d{2})\b",
        ],
        reload_announce_patterns=[
            # The device's own word (#248) — the typed request becomes a
            # countdown when this arrives.
            r"Shutdown\s+NOW|System\s+going\s+down\s+in\s+(?P<m>\d+)\s+minute",
        ],
        reload_cancel_patterns=[
            r"^\s*clear\s+system\s+reboot\b",
            r"Shutdown\s+cancell?ed",
        ],
        reload_cancel_command="clear system reboot",
        commit_confirm_patterns=[
            # "commit confirmed" on its own is ten minutes on Junos.
            r"^\s*commit\s+confirmed(?:\s+(?P<m>\d+))?\b",
            r"rolled?\s+back\s+in\s+(?P<m>\d+)\s+minute",
        ],
        commit_cancel_patterns=[
            # A plain commit confirms the pending one. "commit confirmed"
            # would have matched the pattern above first.
            r"^\s*commit\s*$",
            r"^\s*commit\s+and-quit\b",
            r"^\s*rollback\b",
        ],
    ),
    "panos": PlatformProfile(
        id="panos",
        config_enter="configure",
        config_exit="commit",
        save_command="exit",
        name="Palo Alto PAN-OS",
        paging_off="set cli pager off",
        show_run="show config running",
        version_command="show system info",
        signatures=["pan-os", "palo alto", "panorama"],
        aliases={
            'arp': 'show arp all',
            'bgp': 'show routing protocol bgp summary',
            'conns': 'show session info',
            'cpu': 'show system resources',
            'diff': 'show config diff',
            'disk': 'show system disk-space',
            'ha': 'show high-availability state',
            'int': 'show interface all',
            'ints': 'show interface all',
            'log': 'show log system direction equal backward',
            'nat': 'show running nat-policy',
            'policy': 'show running security-policy',
            'routes': 'show routing route',
            'run': 'show config running',
            'sessions': 'show session all',
            'uptime': 'show system info | match uptime',
            'users': 'show admins',
            'ver': 'show system info',
        },
        dangerous_commands=[
            "request restart system", "request shutdown system", "delete",
            "commit", "request system private-data-reset", "set operational-mode",
        ],
        config_mode_markers=["# "],
        comment_prefix="#",
    ),
    "arista": PlatformProfile(
        id="arista",
        config_enter="configure terminal",
        config_exit="end",
        save_command="write memory",
        name="Arista EOS",
        paging_off="terminal length 0",
        show_run="show running-config",
        version_command="show version",
        signatures=["arista", "eos"],
        aliases={
            'acl': 'show ip access-lists',
            'arp': 'show ip arp',
            'bgp': 'show ip bgp summary',
            'clock': 'show clock',
            'counters': 'show interfaces counters',
            'cpu': 'show processes top once',
            'default': 'show ip route 0.0.0.0',
            'desc': 'show interfaces description',
            'errors': 'show interfaces counters errors',
            'flash': 'dir flash:',
            'int': 'show interfaces status',
            'ints': 'show interfaces status',
            'inv': 'show inventory',
            'lldp': 'show lldp neighbors',
            'log': 'show logging last 100',
            'mac': 'show mac address-table',
            'mem': 'show version | include Memory',
            'mlag': 'show mlag',
            'nei': 'show lldp neighbors',
            'ntp': 'show ntp associations',
            'ospf': 'show ip ospf neighbor',
            'port': 'show interfaces status',
            'power': 'show power',
            'routes': 'show ip route',
            'run': 'show running-config',
            'start': 'show startup-config',
            'stp': 'show spanning-tree',
            'temp': 'show system environment temperature',
            'trunks': 'show interfaces trunk',
            'uptime': 'show version | include Uptime',
            'users': 'show users',
            'ver': 'show version',
            'vlans': 'show vlan',
            'vrf': 'show vrf',
            'vrrp': 'show vrrp',
        },
        dangerous_commands=[
            "reload", "write erase", "shutdown", "no shutdown", "delete flash:",
        ],
        # EOS keeps the IOS spelling for this, deliberately.
        reload_cancel_command="reload cancel",
        config_mode_markers=["(config"],
        comment_prefix="!",
    ),
    # -----------------------------------------------------------------
    # Beyond the Cisco / Juniper / Palo core (#524)
    #
    # A mixed estate is the norm, and a tool that identifies six vendors and
    # goes blind on the FortiGate at the edge loses its device-awareness
    # story exactly where it is most useful.
    #
    # Each profile below carries only what has been checked against the
    # vendor's own command reference. Where the honest answer is "this
    # platform has no such command", the field is blank and the comment says
    # why. A blank field costs a line of typing; a wrong one is sent to a
    # device.
    # -----------------------------------------------------------------
    "iosxr": PlatformProfile(
        id="iosxr",
        name="Cisco IOS-XR",
        # XR keeps the classic exec commands, and this one is unchanged.
        paging_off="terminal length 0",
        show_run="show running-config",
        version_command="show version",
        # "Cisco IOS XR Software" also contains "cisco ios", which the IOS
        # profile claims. The longest signature wins, so the XR ones have to
        # be longer than IOS's — and they are.
        signatures=["cisco ios xr software", "cisco ios xr", "ios-xr", "iosxr"],
        aliases={
            'acl': 'show access-lists',
            'arp': 'show arp',
            'bgp': 'show bgp summary',
            'cdp': 'show cdp neighbors detail',
            'clock': 'show clock',
            'cpu': 'show processes cpu',
            'default': 'show route 0.0.0.0/0',
            'desc': 'show interfaces description',
            'diff': 'show configuration commit changes last 1',
            'int': 'show ipv4 interface brief',
            'ints': 'show ipv4 interface brief',
            'inv': 'show inventory',
            'lldp': 'show lldp neighbors detail',
            'log': 'show logging last 100',
            'mem': 'show memory summary',
            'nei': 'show cdp neighbors',
            'ntp': 'show ntp associations',
            'ospf': 'show ospf neighbor',
            'power': 'show environment power',
            'routes': 'show route',
            'run': 'show running-config',
            'temp': 'show environment temperature',
            'uptime': 'show version | include uptime',
            'users': 'show users',
            'ver': 'show version',
            'vrf': 'show vrf all',
        },
        dangerous_commands=[
            "reload", "admin reload", "commit replace", "load",
            "clear configuration", "delete", "format", "shutdown",
            "no shutdown", "clear counters", "install remove",
            "install replace", "install rollback",
        ],
        config_mode_markers=["(config", "(admin-config"],
        comment_prefix="!",
        # No configuration push. XR commits in two stages — `commit`, then
        # `end` — and leaving configuration mode with the change uncommitted
        # asks an interactive question instead. That is two commands where
        # config_exit holds one, and the wrong half of it discards the work
        # silently. Blank means ShellMate will not push here; the lines can
        # still be pasted by hand.
        config_enter="",
        config_exit="",
        save_command="",
    ),
    "fortios": PlatformProfile(
        id="fortios",
        name="Fortinet FortiOS",
        # Deliberately blank. FortiOS has no per-session paging command:
        # paging is `config system console` / `set output standard`, a
        # configuration change that outlives the session and is shared by
        # every administrator on the box. Changing a setting on somebody
        # else's firewall to save a keypress is not a trade ShellMate makes,
        # so nothing is sent and the engineer presses space.
        paging_off="",
        # `show` from the root prompt is the non-default configuration — the
        # same thing a backup contains. `show full-configuration` includes
        # every default and runs to tens of thousands of lines.
        show_run="show",
        version_command="get system status",
        signatures=["fortigate", "fortinet", "fortios"],
        aliases={
            'arp': 'get system arp',
            'bgp': 'get router info bgp summary',
            'conns': 'diagnose sys session stat',
            'cpu': 'get system performance status',
            'ha': 'get system ha status',
            'int': 'get system interface',
            'ints': 'get system interface',
            'inv': 'get hardware status',
            'mem': 'get system performance status',
            'ospf': 'get router info ospf neighbor',
            'policy': 'show firewall policy',
            'routes': 'get router info routing-table all',
            'run': 'show',
            'sessions': 'diagnose sys session stat',
            'uptime': 'get system performance status',
            'ver': 'get system status',
        },
        dangerous_commands=[
            "execute reboot", "execute shutdown", "execute factoryreset",
            "execute factoryreset2", "execute formatlogdisk",
            "execute restore", "execute disconnect-admin-session",
            "delete", "purge", "unset",
        ],
        # No reload_cancel_command (#584). `execute reboot` on FortiOS goes
        # now: there is no scheduled reboot in the CLI to call off, and
        # therefore no cancel command either. `execute reboot cancel` looks
        # like the answer and is not one — it would be typed at a firewall.
        # The tab menu says the platform has none rather than sending it.
        #
        # FortiOS names the object being edited in the prompt itself:
        # FGT-01 (global) #, FGT-01 (port1) #.
        config_mode_markers=["("],
        comment_prefix="#",
        # No configuration push: FortiOS applies each `next` / `end` as it is
        # typed, so there is no preview-then-commit shape to hang the
        # existing enter / exit / save contract on.
        config_enter="",
        config_exit="",
        save_command="",
    ),
    "routeros": PlatformProfile(
        id="routeros",
        name="MikroTik RouterOS",
        # Deliberately blank. RouterOS pages with an interactive
        # "-- [Q quit|D dump|down]" and has no session-wide switch to turn
        # it off; the per-command answer is to append `without-paging` to the
        # command, which belongs to the command and not to the session.
        paging_off="",
        show_run="/export",
        version_command="/system resource print",
        signatures=["mikrotik", "routeros"],
        aliases={
            'arp': '/ip arp print',
            'clock': '/system clock print',
            'cpu': '/system resource print',
            'dhcp': '/ip dhcp-server lease print',
            'disk': '/system resource print',
            'int': '/interface print',
            'ints': '/interface print',
            'lldp': '/ip neighbor print',
            'log': '/log print',
            'mem': '/system resource print',
            'nat': '/ip firewall nat print',
            'nei': '/ip neighbor print',
            'ospf': '/routing ospf neighbor print',
            'policy': '/ip firewall filter print',
            'port': '/interface ethernet print',
            'routes': '/ip route print',
            'run': '/export',
            'uptime': '/system resource print',
            'users': '/user active print',
            'ver': '/system resource print',
            'vlans': '/interface vlan print',
        },
        dangerous_commands=[
            "/system reboot", "/system shutdown",
            "/system reset-configuration", "/system package downgrade",
            "/system routerboard upgrade", "/file remove", "remove",
        ],
        # RouterOS has no configuration mode: a command either takes effect
        # or it does not, so there is nothing to enter, leave or save.
        config_mode_markers=[],
        comment_prefix="#",
        config_enter="",
        config_exit="",
        save_command="",
    ),
    "huawei": PlatformProfile(
        id="huawei",
        name="Huawei VRP",
        # `temporary` is the point: it lasts for this session and is not
        # written to the configuration, so ShellMate cannot leave a device
        # changed behind it.
        paging_off="screen-length 0 temporary",
        show_run="display current-configuration",
        version_command="display version",
        signatures=[
            "huawei versatile routing platform", "vrp (r) software",
            "huawei", "vrp",
        ],
        aliases={
            'acl': 'display acl all',
            'arp': 'display arp',
            'bgp': 'display bgp peer',
            'clock': 'display clock',
            'cpu': 'display cpu-usage',
            'desc': 'display interface description',
            'dhcp': 'display ip pool',
            'int': 'display interface brief',
            'ints': 'display interface brief',
            'inv': 'display device',
            'lldp': 'display lldp neighbor brief',
            'log': 'display logbuffer',
            'mac': 'display mac-address',
            'mem': 'display memory-usage',
            'nei': 'display lldp neighbor brief',
            'ntp': 'display ntp status',
            'ospf': 'display ospf peer brief',
            'port': 'display interface brief',
            'power': 'display power',
            'routes': 'display ip routing-table',
            'run': 'display current-configuration',
            'start': 'display saved-configuration',
            'stp': 'display stp brief',
            'temp': 'display temperature',
            'trunks': 'display eth-trunk',
            'uptime': 'display version',
            'users': 'display users',
            'ver': 'display version',
            'vlans': 'display vlan',
            'vrf': 'display ip vpn-instance',
        },
        dangerous_commands=[
            "reboot", "schedule reboot", "reset saved-configuration",
            "reset counters", "shutdown", "undo shutdown", "delete",
            "format", "undo interface", "undo vlan", "startup system-software",
        ],
        # The system view is [hostname]; the user view is <hostname>.
        config_mode_markers=["["],
        comment_prefix="#",
        reload_patterns=[
            r"^\s*schedule\s+reboot\s+delay\s+(?P<h>\d+):(?P<m>\d{1,2})\b",
            r"^\s*schedule\s+reboot\s+delay\s+(?P<m>\d+)\b",
            r"^\s*schedule\s+reboot\s+at\s+(?P<at>\d{1,2}:\d{2})\b",
        ],
        reload_cancel_patterns=[
            r"^\s*undo\s+schedule\s+reboot\b",
        ],
        reload_cancel_command="undo schedule reboot",
        config_enter="system-view",
        config_exit="return",
        # Deliberately blank. `save` asks "Are you sure to continue?[Y/N]",
        # and the push sequence has no way to answer it — it would leave the
        # device sitting at a question nobody sent. Saving stays something
        # the engineer types, and sees the answer to.
        save_command="",
    ),
    "aoscx": PlatformProfile(
        id="aoscx",
        name="Aruba AOS-CX",
        # `no page` is the AOS-CX session command. The signatures below
        # deliberately do not include a bare "aruba": ArubaOS-Switch and the
        # ArubaOS controllers are different platforms with different
        # commands, and a signature that swept all three in would send this
        # to devices it is wrong for.
        paging_off="no page",
        show_run="show running-config",
        version_command="show version",
        signatures=["arubaos-cx", "aos-cx"],
        aliases={
            'arp': 'show arp',
            'bgp': 'show bgp ipv4 unicast summary',
            'clock': 'show clock',
            'cpu': 'show system resource-utilization',
            'int': 'show interface brief',
            'ints': 'show interface brief',
            'inv': 'show system',
            'lldp': 'show lldp neighbor-info',
            'log': 'show events',
            'mac': 'show mac-address-table',
            'mem': 'show system resource-utilization',
            'nei': 'show lldp neighbor-info',
            'ntp': 'show ntp status',
            'ospf': 'show ospf neighbors',
            'port': 'show interface brief',
            'power': 'show environment power-supply',
            'routes': 'show ip route',
            'run': 'show running-config',
            'start': 'show startup-config',
            'stp': 'show spanning-tree',
            'temp': 'show environment temperature',
            'uptime': 'show system',
            'ver': 'show version',
            'vlans': 'show vlan',
            'vrf': 'show vrf',
            'vsx': 'show vsx status',
        },
        dangerous_commands=[
            "boot system", "erase all zeroize", "erase all",
            "erase startup-config", "write erase", "reload", "shutdown",
            "no shutdown", "clear counters",
        ],
        config_mode_markers=["(config"],
        comment_prefix="!",
        config_enter="configure terminal",
        config_exit="end",
        save_command="write memory",
    ),
    "linux": PlatformProfile(
        id="linux",
        name="Linux / Unix shell",
        paging_off="",              # a shell has no pager to disable globally
        show_run="",
        version_command="uname -a",
        signatures=["gnu/linux", "ubuntu", "debian", "centos", "red hat"],
        aliases={
            'arp': 'ip neigh',
            'clock': 'date',
            'cpu': 'top -bn1 | head -20',
            'disk': 'df -h',
            'int': 'ip -br addr',
            'ints': 'ip -br addr',
            'listen': 'ss -tulpn',
            'log': 'journalctl -n 100 --no-pager',
            'mem': 'free -h',
            'ntp': 'timedatectl status',
            'routes': 'ip route',
            'sessions': 'ss -tunap',
            'uptime': 'uptime',
            'users': 'who',
            'ver': 'uname -a',
        },
        dangerous_commands=["rm -rf", "mkfs", "dd if=", "shutdown", "reboot", "init 0"],
        # `shutdown -r +10` is called off with `shutdown -c`, on every init
        # system that has shipped this century.
        reload_cancel_command="shutdown -c",
        config_mode_markers=[],
        comment_prefix="#",
    ),
    GENERIC: PlatformProfile(
        id=GENERIC,
        name="Unknown device",
        # Nothing is sent to a device we cannot identify. Guessing a
        # paging command and having it land as a syntax error — or worse,
        # as something meaningful on an unrecognised platform — is a poor
        # trade for saving one line of typing.
        paging_off="",
        show_run="",
        version_command="",
        signatures=[],
        aliases={},
        dangerous_commands=["reload", "reboot", "erase", "format", "shutdown"],
        config_mode_markers=["(config", "[edit"],
        comment_prefix="#",
    ),
}


# ---------------------------------------------------------------------------
# Loading and overrides
# ---------------------------------------------------------------------------

_cache: dict[str, PlatformProfile] | None = None


def profiles_path():
    """Where the editable copy of the platform definitions lives."""
    return paths.data_dir() / "platforms.json"


def load_profiles(refresh: bool = False) -> dict[str, PlatformProfile]:
    """
    Return every known platform profile.

    Writes the built-in set to disk on first run so there is something to edit,
    then prefers whatever is on disk. A malformed or partial file falls back to
    the built-ins for the affected platform rather than leaving ShellMate with
    no idea what any device is.
    """
    global _cache
    if _cache is not None and not refresh:
        return _cache

    merged = {key: PlatformProfile(**profile.as_dict()) for key, profile in BUILTIN.items()}

    path = profiles_path()
    if not path.exists():
        _write_defaults(path)
    else:
        try:
            stored = jsonfile.read(path, {}, expect=dict)
            for key, values in (stored.get("platforms") or {}).items():
                if not isinstance(values, dict):
                    continue
                base = merged.get(key)
                if base is None:
                    # A platform the user added themselves.
                    values.setdefault("id", key)
                    values.setdefault("name", key)
                    merged[key] = PlatformProfile(**_only_known_fields(values))
                else:
                    merged[key] = PlatformProfile(
                        **_merge_profile(base.as_dict(), _only_known_fields(values))
                    )
        except (OSError, json.JSONDecodeError, TypeError) as exc:
            logger.warning("Ignoring unreadable platforms.json (%s); using built-ins", exc)

    _cache = merged
    return merged


def _only_known_fields(values: dict) -> dict:
    """Drop unrecognised keys so a stray field cannot break construction."""
    allowed = set(PlatformProfile.__dataclass_fields__)
    return {k: v for k, v in values.items() if k in allowed}


def _merge_profile(base: dict, stored: dict) -> dict:
    """
    Combine a built-in profile with the user's edits to it.

    Aliases merge key by key rather than being replaced wholesale. If the
    stored file simply won, anyone who had ever opened platforms.json would be
    frozen on the alias set that existed the day they did — new built-ins
    would never reach them, which is exactly the wrong outcome for a file
    people are encouraged to edit.

    To suppress a built-in alias, map it to an empty string. That is a
    deliberate act and survives upgrades, whereas absence cannot be
    distinguished from "this predates the alias existing".
    """
    result = dict(base)

    for key, value in stored.items():
        if key == "aliases" and isinstance(value, dict):
            combined = {**base.get("aliases", {}), **value}
            result[key] = {k: v for k, v in combined.items() if v}
        else:
            result[key] = value

    return result


def _write_defaults(path) -> None:
    """Write the built-in profiles out so the user has something to edit."""
    document = {
        "_comment": (
            "Platform definitions for ShellMate Portable. Edit freely — this "
            "file is read in preference to the built-in defaults. Delete it to "
            "restore them."
        ),
        "platforms": {key: profile.as_dict() for key, profile in BUILTIN.items()},
    }
    try:
        jsonfile.write(path, document)
        logger.info("Wrote default platform definitions to %s", path)
    except OSError as exc:
        logger.warning("Could not write platform definitions: %s", exc)


def save_profile_edits(platform_id: str, values: dict) -> PlatformProfile:
    """
    Persist edits to one platform.

    Writes the whole set back so the file stays a complete, readable record
    rather than a sparse overlay someone has to mentally merge with the
    built-ins to understand.

    Raises:
        ValueError: The identifier is missing or the values are unusable.
    """
    if not platform_id:
        raise ValueError("A platform id is required.")

    profiles = load_profiles()
    base = profiles.get(platform_id)

    fields = _only_known_fields(values)
    fields.setdefault("id", platform_id)
    fields.setdefault("name", base.name if base else platform_id)

    if base is not None:
        fields = _merge_profile(base.as_dict(), fields)
    else:
        fields = {**PlatformProfile(id=platform_id, name=fields["name"]).as_dict(), **fields}

    try:
        updated = PlatformProfile(**fields)
    except TypeError as exc:
        raise ValueError(f"Those platform settings are not usable: {exc}") from exc

    profiles[platform_id] = updated
    _write_all(profiles)

    global _cache
    _cache = profiles
    return updated


def delete_platform(platform_id: str) -> bool:
    """
    Remove a platform the user added.

    Built-ins are never deleted — they would simply reappear from the defaults
    on the next load, which would look like the delete had silently failed.
    """
    if platform_id in BUILTIN:
        raise ValueError(
            f"'{platform_id}' is built in and cannot be deleted. Edit it instead, "
            f"or reset to defaults."
        )

    profiles = load_profiles()
    if platform_id not in profiles:
        return False

    del profiles[platform_id]
    _write_all(profiles)

    global _cache
    _cache = profiles
    return True


def reset_to_defaults() -> dict[str, PlatformProfile]:
    """Discard every edit and restore the built-in definitions."""
    global _cache
    _cache = None
    path = profiles_path()
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        raise ValueError(f"Could not remove {path.name}: {exc}") from exc
    return load_profiles(refresh=True)


def _write_all(profiles: dict[str, PlatformProfile]) -> None:
    """Write the full platform set to disk."""
    document = {
        "_comment": (
            "Platform definitions for ShellMate Portable. Editable here or in "
            "Settings. Delete this file to restore the built-in defaults. To "
            "suppress a built-in alias, set it to an empty string."
        ),
        "platforms": {key: profile.as_dict() for key, profile in profiles.items()},
    }
    path = profiles_path()
    try:
        jsonfile.write(path, document)
    except OSError as exc:
        raise ValueError(f"Could not write {path.name}: {exc}") from exc


def get_profile(platform_id: str) -> PlatformProfile:
    """Return one profile, falling back to the generic one."""
    profiles = load_profiles()
    return profiles.get(platform_id) or profiles[GENERIC]



def matches_dangerous(platform_id: str, command: str) -> str:
    """
    The dangerous command a typed line matches, or "".

    Prefix matching on the normalised line, so `reload in 10` matches
    `reload` and `show reload-reason` does not. Whitespace is collapsed
    because `write  erase` is the same instruction as `write erase`.

    Two limits worth stating rather than papering over:

    Abbreviations are not covered. `relo` is a valid IOS reload and matches
    nothing here — expanding every abbreviation of every command would mean
    knowing each platform's parser, and a guardrail that is wrong about what
    it catches is worse than one that is clear about it.

    Neither is anything the line assembler could not track: a command recalled
    with the up arrow arrives with nothing to match. Both are why the
    interface says this catches common mistakes rather than claiming to be a
    net.
    """
    profile = load_profiles().get(platform_id)
    if profile is None:
        return ""

    normalised = " ".join((command or "").split()).lower()
    if not normalised:
        return ""

    for dangerous in profile.dangerous_commands:
        pattern = " ".join(dangerous.split()).lower()
        if not pattern:
            continue
        if normalised == pattern or normalised.startswith(pattern + " "):
            return dangerous
    return ""


def resolve_alias(platform_id: str, command: str) -> str | None:
    """
    Expand a short alias for this platform.

    Only a bare alias on its own is expanded. "ints" becomes the platform's
    interface command; "show ints" is left alone, because the user has clearly
    typed a real command and silently rewriting the middle of it would be
    worse than not helping at all.
    """
    stripped = command.strip()
    if not stripped or " " in stripped:
        return None
    expanded = get_profile(platform_id).aliases.get(stripped.lower())
    return expanded if expanded and expanded != stripped else None
