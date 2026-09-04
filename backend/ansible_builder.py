"""
ansible_builder.py — Getting to a first playbook without writing YAML (#586).

Most people who need a playbook do not need a *hard* playbook. They need
"shut these ports", "push these NTP servers", "collect the running config
off every switch in site 4" — a play with one or two tasks, the right
module for the platform, and the boilerplate that Ansible insists on. The
distance between knowing that and having valid YAML is where the tool
either helps or does not.

Two ways across it, and the order matters:

- **Blocks.** A small vocabulary of network tasks — gather facts, run
  commands, push configuration lines, save, back up — assembled locally
  into correct YAML. No network, no API key, no waiting, and the output is
  deterministic. This is the default because a helper you can rely on
  beats a cleverer one you cannot.
- **The assistant**, when one is configured. Describe the change in a
  sentence and get a draft. Better at the awkward middle ground the blocks
  do not cover, and worse in exactly the way a language model is worse: it
  will write something plausible and wrong with no less confidence than
  something right.

So everything the assistant produces is treated as a **draft**, never as
something to run. It is parsed here before it is shown, and what comes
back says what was found — tasks, modules, hosts — and what it would do to
a device. A playbook nobody read is a playbook nobody should run, and
making the reading easy is the only part of this that reduces risk.
"""

import logging
import re
from typing import Any

from backend import platforms

logger = logging.getLogger(__name__)


class BuilderError(ValueError):
    """A request that cannot be turned into a playbook."""


# ---------------------------------------------------------------------------
# The blocks
# ---------------------------------------------------------------------------
#
# Keyed by the family ShellMate already identifies, because the module for
# "run a command" is genuinely different per platform and guessing is how a
# tool ends up sending an IOS module to a firewall. A family with no entry
# gets the generic `raw`, which works everywhere and is honest about being
# a blunt instrument.

#: Per-family module names: (facts, command, config).
_MODULES = {
    "ios":    ("cisco.ios.ios_facts", "cisco.ios.ios_command", "cisco.ios.ios_config"),
    "iosxe":  ("cisco.ios.ios_facts", "cisco.ios.ios_command", "cisco.ios.ios_config"),
    "nxos":   ("cisco.nxos.nxos_facts", "cisco.nxos.nxos_command", "cisco.nxos.nxos_config"),
    "asa":    ("cisco.asa.asa_facts", "cisco.asa.asa_command", "cisco.asa.asa_config"),
    "eos":    ("arista.eos.eos_facts", "arista.eos.eos_command", "arista.eos.eos_config"),
    "junos":  ("junipernetworks.junos.junos_facts", "junipernetworks.junos.junos_command",
               "junipernetworks.junos.junos_config"),
    "iosxr":  ("cisco.iosxr.iosxr_facts", "cisco.iosxr.iosxr_command",
               "cisco.iosxr.iosxr_config"),
}

#: The generic fallback. `raw` sends text and returns text; it needs no
#: collection and works against anything with a shell or a CLI. It is
#: offered rather than a guessed module because a wrong module fails in a
#: way that reads as the device's fault.
_GENERIC = ("ansible.builtin.setup", "ansible.netcommon.cli_command",
            "ansible.netcommon.cli_config")


def families() -> list[dict]:
    """The platforms a block can be built for, for the picker."""
    out = [{"id": "generic", "label": "Any device (generic CLI)",
            "modules": list(_GENERIC)}]
    try:
        known = platforms.load_profiles()
    except Exception:                                     # pragma: no cover
        known = {}
    # ShellMate's platform ids and Ansible's family names are close but not
    # identical (`arista` here, `eos` there), so the label is looked up by
    # either. A family with no ShellMate profile keeps its own name rather
    # than being dropped: the module set is what matters, and the picker
    # showing one fewer platform than Ansible supports would be the bug.
    aliases = {"eos": "arista", "iosxe": "ios"}
    for family, mods in sorted(_MODULES.items()):
        profile = known.get(family) or known.get(aliases.get(family, ""))
        label = getattr(profile, "name", "") or family
        out.append({"id": family, "label": label, "modules": list(mods)})
    return out


def _modules_for(family: str) -> tuple[str, str, str]:
    return _MODULES.get((family or "").lower(), _GENERIC)


#: What each block asks for. `writes` drives the warning the UI shows: a
#: block that changes a device is not the same kind of thing as one that
#: reads from it, and the difference should be visible before the run.
BLOCKS: dict[str, dict] = {
    "facts": {
        "label": "Gather facts",
        "why": "Ask the device what it is. Cheap, safe, and useful as a first task.",
        "writes": False,
        "fields": [],
    },
    "command": {
        "label": "Run show commands",
        "why": "Run one or more read-only commands and keep the output.",
        "writes": False,
        "fields": [{"name": "commands", "label": "Commands, one per line",
                    "multiline": True, "required": True,
                    "placeholder": "show version\nshow ip interface brief"}],
    },
    "config": {
        "label": "Push configuration lines",
        "why": "Send configuration. This changes the device.",
        "writes": True,
        "fields": [
            {"name": "lines", "label": "Configuration lines, one per line",
             "multiline": True, "required": True,
             "placeholder": "ntp server 10.0.0.1\nntp server 10.0.0.2"},
            {"name": "parents", "label": "Under which parent, if any",
             "placeholder": "interface GigabitEthernet1/0/4"},
        ],
    },
    "backup": {
        "label": "Back up the running configuration",
        "why": "Fetch the running configuration to the runner before anything else.",
        "writes": False,
        "fields": [],
    },
    "save": {
        "label": "Save the configuration",
        "why": "Write the running configuration to startup. Makes changes survive a reload.",
        "writes": True,
        "fields": [],
    },
}


def _yaml_scalar(text: str) -> str:
    """
    Quote a value so it survives being YAML.

    Configuration lines are full of colons, hashes and leading digits, all
    of which mean something to a YAML parser and none of which mean it
    here. Single quotes with doubling is the one form with no escape
    sequences to get wrong.
    """
    return "'" + str(text).replace("'", "''") + "'"


def _lines_of(value: Any) -> list[str]:
    if isinstance(value, list):
        items = [str(v) for v in value]
    else:
        items = str(value or "").splitlines()
    return [line.strip() for line in items if line.strip()]


def _emit_task(block: dict, mods: tuple, indent: str, out: list) -> dict:
    """
    One task, as YAML lines. Returns what it is, for the summary.

    Shared by tasks and handlers because a handler *is* a task — the only
    difference is that it runs when notified rather than in sequence, and
    duplicating the emitter to express that would guarantee the two drift.
    """
    facts_mod, command_mod, config_mod = mods
    kind = str((block or {}).get("kind") or "")
    meta = BLOCKS.get(kind)
    if meta is None:
        raise BuilderError(f"'{kind}' is not a block ShellMate can build.")
    label = str(block.get("label") or meta["label"])
    fields = block.get("fields") or {}

    out.append(f"{indent}- name: {_yaml_scalar(label)}")
    body_indent = indent + "  "

    if kind == "facts":
        out += [f"{body_indent}{facts_mod}:",
                f"{body_indent}  gather_subset: min"]

    elif kind == "command":
        commands = _lines_of(fields.get("commands"))
        if not commands:
            raise BuilderError(f"'{label}' has no commands in it.")
        out += [f"{body_indent}{command_mod}:", f"{body_indent}  commands:"]
        out += [f"{body_indent}    - {_yaml_scalar(c)}" for c in commands]
        out.append(f"{body_indent}register: shellmate_output")

    elif kind == "config":
        lines = _lines_of(fields.get("lines"))
        if not lines:
            raise BuilderError(f"'{label}' has no configuration lines in it.")
        out += [f"{body_indent}{config_mod}:", f"{body_indent}  lines:"]
        out += [f"{body_indent}    - {_yaml_scalar(line)}" for line in lines]
        parents = _lines_of(fields.get("parents"))
        if parents:
            out.append(f"{body_indent}  parents:")
            out += [f"{body_indent}    - {_yaml_scalar(x)}" for x in parents]

    elif kind == "backup":
        out += [f"{body_indent}{config_mod}:", f"{body_indent}  backup: true"]

    elif kind == "save":
        # save_when: modified rather than always. `always` rewrites
        # startup-config on every run whether or not anything changed, which
        # makes a no-op look like a change to everything that reads the
        # device afterwards.
        out += [f"{body_indent}{config_mod}:",
                f"{body_indent}  save_when: modified"]

    # What this task wakes up, if anything (#600). Handlers are the reason
    # "restart it, but only if something changed" is expressible at all; a
    # builder without them produces playbooks that restart unconditionally.
    notify = [n for n in (block.get("notify") or []) if str(n).strip()]
    if notify:
        out.append(f"{body_indent}notify:")
        out += [f"{body_indent}  - {_yaml_scalar(n)}" for n in notify]

    return {"label": label, "writes": meta["writes"], "why": meta["why"],
            "notify": notify}


def _normalise(spec: dict) -> list[dict]:
    """
    The plays to build, whichever shape the caller used.

    The canvas sends plays; the older flat form sent one list of blocks and
    meant one play. Both are accepted rather than migrating every caller at
    once — and the flat form is what the assistant's inspection tests and
    the API's own smoke checks still use.
    """
    plays = spec.get("plays")
    if plays:
        return [dict(play) for play in plays]
    return [{
        "name": spec.get("name") or "",
        "hosts": spec.get("hosts") or "all",
        "gather_facts": spec.get("gather_facts", False),
        "tasks": spec.get("blocks") or [],
        "handlers": spec.get("handlers") or [],
    }]


def build(spec: dict) -> dict:
    """
    Assemble a playbook from plays, each with its own tasks and handlers.

    Returns the YAML and what it would do, because the second is the part
    somebody should read. Nothing here talks to a network or a model: the
    same input produces the same output every time, which is what makes it
    the default path rather than the fallback.

    Hosts belong to the play, not to the file, because that is where Ansible
    puts them — a playbook that configures switches and then checks a
    firewall is two plays, and a single global target cannot say so.
    """
    plays = _normalise(spec)
    if not plays:
        raise BuilderError("A playbook needs at least one play.")

    family = str(spec.get("family") or "generic").lower()
    mods = _modules_for(family)

    body: list[str] = []
    does: list[dict] = []
    step = 0

    for index, play in enumerate(plays, start=1):
        name = str(play.get("name") or "").strip() or f"Play {index}"
        hosts = str(play.get("hosts") or "").strip() or "all"
        tasks = play.get("tasks") or []
        handlers = play.get("handlers") or []
        if not tasks:
            raise BuilderError(f"'{name}' has no tasks in it.")

        body += [
            f"- name: {_yaml_scalar(name)}",
            f"  hosts: {_yaml_scalar(hosts)}",
            f"  gather_facts: {'true' if play.get('gather_facts') else 'false'}",
            "  tasks:",
        ]
        for block in tasks:
            step += 1
            found = _emit_task(block, mods, "    ", body)
            found.update(step=step, play=name, hosts=hosts, handler=False)
            does.append(found)

        if handlers:
            body.append("  handlers:")
            for block in handlers:
                found = _emit_task(block, mods, "    ", body)
                found.update(step=0, play=name, hosts=hosts, handler=True)
                does.append(found)

    text = "---\n" + "\n".join(body) + "\n"
    return {"text": text, "does": does, "family": family,
            "plays": len(plays),
            "writes": any(d["writes"] for d in does), "source": "blocks"}


# ---------------------------------------------------------------------------
# Reading a playbook back
# ---------------------------------------------------------------------------
#
# Whatever produced it — blocks, the assistant, a paste from somewhere —
# this says what it actually contains. It is the whole safety story for the
# assisted path: a draft nobody read is a draft nobody should run, and the
# only useful thing to do about that is make reading it easy.

#: Modules that change a device. Matched on the last segment so a
#: collection-qualified name and a bare one land the same way.
_WRITING = re.compile(
    r"(?:^|\.)(?:\w+_config|\w+_command_config|config|command|shell|raw|"
    r"copy|template|lineinfile|blockinfile|file|user|\w+_user|\w+_system|"
    r"\w+_interfaces?|\w+_l2_interfaces|\w+_l3_interfaces|\w+_vlans|"
    r"\w+_static_routes|\w+_banner|\w+_logging\w*|\w+_ntp\w*|\w+_snmp\w*|"
    r"\w+_acl\w*|reboot|\w+_reboot)$")

#: Modules that only look. Checked first, because `ios_command` matches the
#: writing pattern above and is not a write.
_READING = re.compile(
    r"(?:^|\.)(?:\w+_facts|setup|gather_facts|debug|assert|\w+_command|"
    r"cli_command|ping|wait_for|uri|stat|slurp|fail|set_fact|include\w*|"
    r"import\w*|pause|meta)$")

#: `- name: Something`, or `- some.module:` for a task nobody named.
_DASH_KEY = re.compile(r"^(\s*)-\s+([a-z_][a-z0-9_]*(?:\.[a-z0-9_]+){0,3}):\s*(.*)$")
_BARE_DASH = re.compile(r"^(\s*)-\s*$")
_KEY = re.compile(r"^(\s*)([a-z_][a-z0-9_]*(?:\.[a-z0-9_]+){0,3}):\s*(.*)$")
_HOSTS = re.compile(r"^\s{0,4}hosts:\s*(.+?)\s*$")

#: Keys that appear alongside a module in a task and are not modules.
#: Without this, `lines:` under `ios_config:` is counted as its own task —
#: which it was, and the count was wrong in a way nobody would question.
_TASK_KEYWORDS = {
    "name", "when", "register", "loop", "with_items", "with_dict", "until",
    "retries", "delay", "become", "become_user", "become_method", "vars",
    "tags", "notify", "ignore_errors", "changed_when", "failed_when",
    "no_log", "delegate_to", "run_once", "check_mode", "diff", "environment",
    "block", "rescue", "always", "listen", "args", "connection", "gather_facts",
    "hosts", "tasks", "handlers", "roles", "pre_tasks", "post_tasks", "serial",
    "any_errors_fatal", "throttle", "timeout", "local_action", "loop_control",
}


def inspect(text: str) -> dict:
    """
    Say what a playbook contains, without running it.

    Deliberately a scan rather than a YAML parse. A draft that does not
    parse is exactly the one somebody needs help reading, and a parser
    would refuse the whole file over one bad indent and tell them nothing
    at all.

    The scan is indent-aware because it has to be: a task's module and the
    module's own arguments are both `key:` lines, and telling them apart by
    pattern alone counted `lines:` under `ios_config:` as a second task. A
    task list that says four when it means three is worse than no list —
    it is read once and believed.

    What it gets wrong it gets wrong conservatively: an unrecognised module
    counts as something that writes.
    """
    tasks: list[dict] = []
    hosts: list[str] = []
    unknown: list[str] = []

    pending_name = ""
    task_indent = -1        # indent of the "- " that opened the current task
    have_module = False     # the first non-keyword key in a task is its module

    def close() -> None:
        nonlocal pending_name, have_module
        pending_name = ""
        have_module = False

    for line in (text or "").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue

        found = _HOSTS.match(line)
        if found:
            hosts.append(found.group(1).strip().strip("'\""))
            continue

        dashed = _DASH_KEY.match(line)
        if dashed:
            # A new list item: whatever came before is finished. The key on
            # the dash line is either the task's name or — for a task
            # nobody named — the module itself, which is a shape drafts
            # arrive in constantly and one an earlier version dropped
            # silently, understating what a playbook would do.
            close()
            task_indent = len(dashed.group(1))
            key, rest = dashed.group(2), (dashed.group(3) or "").strip()
            if key == "name":
                pending_name = rest.strip("'\"")
            elif key not in _TASK_KEYWORDS:
                line = " " * (task_indent + 2) + key + ":" + (" " + rest if rest else "")
            else:
                continue
        elif _BARE_DASH.match(line):
            close()
            task_indent = len(_BARE_DASH.match(line).group(1))
            continue

        found = _KEY.match(line)
        if not found or task_indent < 0:
            continue
        indent, key, rest = len(found.group(1)), found.group(2), found.group(3)

        # A task's keys sit two columns in from the "-" that opened it.
        # Anything deeper belongs to the module, not to the task.
        if indent != task_indent + 2:
            continue
        if key in _TASK_KEYWORDS:
            if key == "name" and rest:
                pending_name = rest.strip().strip("'\"")
            continue
        if have_module:
            continue

        if _READING.search(key):
            writes = False
        elif _WRITING.search(key):
            writes = True
        else:
            writes = True                      # unrecognised counts as a write
            unknown.append(key)

        tasks.append({"name": pending_name or key, "module": key, "writes": writes})
        have_module = True
        pending_name = ""

    blunt = [t["module"] for t in tasks
             if t["module"].endswith(("_command", "cli_command", "raw", "shell"))]
    return {
        "tasks": tasks,
        "hosts": hosts,
        "writes": any(t["writes"] for t in tasks),
        "unknown_modules": sorted(set(unknown)),
        # Worth saying before somebody trusts a dry run: check mode is only
        # as honest as the modules in the play, and these ones opt out.
        "check_mode_note": (
            "Some of these modules do not support check mode, so a dry run "
            "may skip them rather than report what they would do: "
            + ", ".join(sorted(set(blunt))) + "." if blunt else ""),
    }


# ---------------------------------------------------------------------------
# The assistant
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = (
    "You are a senior network automation engineer who writes Ansible "
    "playbooks for network devices. You write correct, minimal YAML using "
    "the collection-qualified module names for the platform you are given "
    "(cisco.ios, cisco.nxos, arista.eos, junipernetworks.junos, or "
    "ansible.netcommon for a generic CLI device).\n\n"
    "Rules you follow without exception:\n"
    "- Output ONLY the playbook. No prose before or after it, no markdown "
    "fences, no explanation. The first line is '---'.\n"
    "- One play, with an explicit 'hosts' and a 'name' on every task.\n"
    "- Never invent a module name. If you are not certain a module exists, "
    "use the platform's *_command module and put the CLI commands in it.\n"
    "- Never include credentials, and never set ansible_password or "
    "ansible_user in the playbook.\n"
    "- If the request would need something destructive (reload, write "
    "erase, shutting an uplink), still write it, but put the reason it is "
    "dangerous in the task's name so the person reading it cannot miss it.\n"
    "- Prefer idempotent modules over raw CLI where a real module exists."
)


def _prompt(request: dict) -> str:
    """The task, as the model receives it."""
    family = str(request.get("family") or "generic")
    hosts = str(request.get("hosts") or "all")
    parts = [
        f"Write an Ansible playbook for {family} devices.",
        f"The play targets the inventory group or pattern: {hosts}",
        "",
        "What it needs to do:",
        str(request.get("description") or "").strip(),
    ]
    context = str(request.get("context") or "").strip()
    if context:
        # Device output the user chose to include. Redacted on the way out
        # like every other thing that leaves this machine for an API.
        from backend.session import outbound
        parts += ["", "Relevant output from the device, for reference:",
                  outbound.redact_text(context)[:4000]]
    parts += ["", "Output the playbook only."]
    return "\n".join(parts)


def _strip_fences(text: str) -> str:
    """
    Take the playbook out of whatever the model wrapped it in.

    They are told not to use markdown fences and they use them anyway,
    often enough that stripping is cheaper than a retry — and a leading
    ```yaml makes the whole file invalid, so the failure would land on
    somebody who did nothing wrong.
    """
    cleaned = (text or "").strip()
    fenced = re.match(r"^```[a-zA-Z]*\s*\n(.*?)\n?```\s*$", cleaned, re.S)
    if fenced:
        cleaned = fenced.group(1).strip()
    if not cleaned.startswith("---"):
        start = cleaned.find("\n---")
        if start != -1:
            cleaned = cleaned[start + 1:].strip()
    return cleaned


async def draft(request: dict, backend: str, model: str | None = None) -> dict:
    """
    Ask the assistant for a playbook, and report what came back.

    The result is a draft and is labelled one everywhere it appears. The
    inspection travels with it, because the only real defence against a
    plausible wrong answer is that somebody read it, and the only way to
    make that likely is to make it easy.
    """
    description = str(request.get("description") or "").strip()
    if not description:
        raise BuilderError("Describe what the playbook should do.")

    if backend == "claude":
        from backend.ai.claude_client import stream_response
    elif backend == "xai":
        from backend.ai.xai_client import stream_response
    elif backend == "openai":
        from backend.ai.openai_client import stream_response
    elif backend == "deepseek":
        from backend.ai.deepseek_client import stream_response
    else:
        from backend.ai.ollama_client import stream_response

    chunks: list[str] = []
    async for chunk in stream_response(_prompt(request), "", model=model,
                                       system_prompt=SYSTEM_PROMPT):
        if isinstance(chunk, str):
            chunks.append(chunk)

    text = _strip_fences("".join(chunks))
    if not text:
        raise BuilderError("The assistant returned nothing. Try again, or "
                           "build it from blocks instead.")

    logger.info("Ansible playbook drafted by %s (%d characters)", backend, len(text))
    found = inspect(text)
    found.update({"text": text, "source": "assistant", "backend": backend,
                  "draft": True})
    return found
