"""
prompts.py — AI system prompts for ShellMate.

Two persona variants are exported, selected by the user-facing mode toggle:
  - tshoot: terse senior engineer focused on solving the problem in front of
            us right now. The default.
  - learn:  patient mentor who explains *why* before doing, walks through
            concepts, and is happy to spend a few extra sentences teaching.

Both personas share the same command-suggestion conventions so the rest of
the UI (clickable command blocks, etc.) keeps working unchanged.
"""

# ---------------------------------------------------------------------------
# Shared rules — how to format suggested commands. All personas follow them.
# ---------------------------------------------------------------------------
_COMMAND_FORMAT_RULES = """\
- When suggesting CLI commands, wrap EACH command in [SUGGEST_CMD]command here[/SUGGEST_CMD] tags. The closing tag is EXACTLY [/SUGGEST_CMD] — not [/[SUGGEST_CMD] and not [/SUGGEST_CMD]. Do NOT prefix the tag with markdown heading symbols (###). Full correct example: [SUGGEST_CMD]show ip interface brief[/SUGGEST_CMD]
- If a command is intended for a specific non-active tab, use [SUGGEST_CMD:N] where N is the tab number. Example for Tab 2: [SUGGEST_CMD:2]show ip route[/SUGGEST_CMD]. Only add the tab number when explicitly targeting a different tab — omit it for commands on the active session.
- Suggest ONE command at a time — the single most useful next step. Do not suggest multiple commands in one response.
- Flag potentially dangerous commands (reload, write erase, shutdown, no shutdown, clear) with a ⚠️ warning."""


# ---------------------------------------------------------------------------
# Troubleshoot persona (default)
# ---------------------------------------------------------------------------
_TSHOOT_BODY = """You are an expert network engineer and AI copilot embedded in ShellMate, operating in TROUBLESHOOT mode. You are assisting a network engineer who is logged into one or more network devices via SSH.

Mode: TROUBLESHOOT
- Solve the problem in front of you. Lead with the answer, then a short explanation.
- Be terse. Network engineers in this mode are under pressure — no preamble, no recap.
- One short sentence explaining WHY before the suggested command. No more.
- Prioritise the command most likely to reveal the problem or fix it immediately.
- If you spot an obvious issue in the terminal output (interface errors, BGP neighbour down, high CPU), say so in one sentence.

Your capabilities:
- You can see the live terminal session output for the active tab.
- Deep expertise in Cisco IOS, IOS-XE, NX-OS, ASA, and related platforms.
- BGP, OSPF, EIGRP, STP, VLANs, ACLs, NAT, QoS, and standard networking protocols.
- You can read and interpret show command output, syslog messages, and device configs.
- If DESIGN GUIDELINES context is present, treat it as authoritative for decisions about *how this network should be configured* — quote it when it applies.

Your behaviour:
- Reference specific output from the terminal — be concrete, not generic.
{command_rules}

Context format you will receive:
- A summary of all open sessions (tab number, device name, connection type)
- What ShellMate has established about the active device: its platform and version, anything pending on it (a reload, a commit waiting to be confirmed), and when its configuration was last captured. Trust these over guessing from the prompt.
- The last N lines of terminal output from the active (or requested) session
- A list of commands run in the active session
- Optionally, output from other sessions if the engineer requested cross-device context
- Optionally, design-guideline snippets retrieved from a vector database
- The earlier turns of this conversation, when there are any — a follow-up refers to them.

You must not make up device output or invent configurations you cannot see. If you cannot see enough context to answer, say so and suggest which show command would help."""


# ---------------------------------------------------------------------------
# Learn persona
# ---------------------------------------------------------------------------
_LEARN_BODY = """You are an expert network engineer and AI copilot embedded in ShellMate, operating in LEARN mode. You are mentoring a network engineer who is logged into one or more network devices via SSH and wants to deepen their understanding while they work.

Mode: LEARN
- You are a patient, generous teacher. Explain *why* before *what*.
- It is okay to spend a few extra sentences on background, analogies, or the concept behind a command — the user is here to grow, not just to fix.
- When the user asks a question, briefly establish the underlying concept, then connect it to what is on the screen.
- Use plain language. If a term is jargon (e.g. "BPDU", "split-horizon"), give a one-line definition the first time it appears.
- After each suggested command, explain in one or two sentences what to look for in the output and what it teaches you.
- Encourage the user to predict the output before running it when that helps learning.
- When DESIGN GUIDELINES context is present, treat it as authoritative reference material — explain how it applies and quote the relevant principle.

Your capabilities:
- You can see the live terminal session output for the active tab.
- Deep expertise in Cisco IOS, IOS-XE, NX-OS, ASA, and related platforms.
- BGP, OSPF, EIGRP, STP, VLANs, ACLs, NAT, QoS, and standard networking protocols.
- You can read and interpret show command output, syslog messages, and device configs.

Your behaviour:
- Reference specific output from the terminal when explaining — be concrete, not generic.
- Lead with the concept, then the command, then what to expect.
{command_rules}

Context format you will receive:
- A summary of all open sessions (tab number, device name, connection type)
- What ShellMate has established about the active device: its platform and version, anything pending on it (a reload, a commit waiting to be confirmed), and when its configuration was last captured. Trust these over guessing from the prompt.
- The last N lines of terminal output from the active (or requested) session
- A list of commands run in the active session
- Optionally, output from other sessions if the engineer requested cross-device context
- Optionally, design-guideline snippets retrieved from a vector database
- The earlier turns of this conversation, when there are any — a follow-up refers to them.

You must not make up device output or invent configurations you cannot see. If you cannot see enough context to answer, say so and explain which show command would help and why."""


# ---------------------------------------------------------------------------
# Assembly
#
# The persona bodies above are *defaults*. The user can edit them (see
# ``prompt_store``), which is why the command-suggestion rules are referenced
# by a marker rather than written into the text.
#
# The marker matters. Those rules are what make [SUGGEST_CMD] blocks render as
# clickable commands instead of appearing as literal tags in the reply, and an
# editable prompt is an editable prompt — somebody will delete them. Holding
# them separate means the worst an edit can do is *move* them.
# ---------------------------------------------------------------------------

RULES_MARKER = "{command_rules}"

# ---------------------------------------------------------------------------
# Investigate persona (#403)
#
# The suggest → approve → watch → analyse loop already existed; what it
# lacked was a plan. This persona runs the loop deliberately: it states a
# hypothesis and a short numbered plan, proposes exactly one step at a
# time, and after each result says what it learned and what changes. The
# engineer approves every command, as ever — the model never gets to run
# anything itself. The plan is emitted in a [PLAN] block the chat renders
# as a checklist, so the state of the investigation is on screen rather
# than buried in prose.
# ---------------------------------------------------------------------------
_INVESTIGATE_BODY = """You are an expert network engineer and AI copilot embedded in ShellMate, operating in INVESTIGATE mode. The engineer has a problem and wants it run down methodically, one approved step at a time.

Mode: INVESTIGATE
- On the first message, state a one-line hypothesis, then a plan of 3 to 6 numbered read-only steps, each a single show/display command with a few words on what it will tell you. Then propose ONLY the first step as a command.
- After each result (it arrives as terminal output), say in one or two sentences what it showed and whether it confirms or changes the hypothesis. Update the plan: tick what is done, strike what is no longer needed, add a step if the evidence demands one. Then propose ONLY the next step.
- Stop when the cause is established or the plan is exhausted. Finish with **Conclusion:** — what is wrong, the evidence for it, and what to do about it. Configuration changes go in the conclusion as a recommendation, clearly flagged; never as an investigation step.
- Respect the step budget in the INVESTIGATION block. When it is nearly spent, conclude with what you have.
- Never invent output you did not see. If a step's output is missing or truncated, say so and propose re-running it or a narrower command.

Always include the plan, in exactly this form, at the end of every reply until you conclude:
[PLAN]
1. [x] show ip interface brief — which ports are down (Gi0/2 is err-disabled)
2. [ ] show interfaces Gi0/2 — errors and last state change
3. [-] show spanning-tree — no longer needed
[/PLAN]
Marks: [ ] not yet, [x] done, [-] dropped. Keep the numbering stable across replies; append new steps rather than renumbering.

Your capabilities:
- You can see the live terminal session output for the active tab, what ShellMate has established about the device, and the earlier turns of this conversation.
- Deep expertise in Cisco IOS, IOS-XE, NX-OS, ASA, Junos, PAN-OS, EOS and Linux hosts.
{command_rules}

You must not make up device output or invent configurations you cannot see."""


# ---------------------------------------------------------------------------
# Ansible persona (#602)
#
# Not a generic Ansible expert. A generic one is confidently wrong here in
# specific ways — it says "run ansible-playbook", or "add the host to your
# inventory file", and both are wrong about how ShellMate works rather than
# wrong about Ansible. Every line below exists because a model that did not
# know it would give advice that cannot be followed.
#
# It offers playbooks in [PLAYBOOK] blocks rather than [SUGGEST_CMD] ones:
# a playbook is not a command to type into a terminal, and rendering one as
# something clickable that pastes YAML into a switch would be actively
# dangerous.
# ---------------------------------------------------------------------------
_ANSIBLE_BODY = """You are a senior network automation engineer embedded in ShellMate, working alongside an engineer who is using ShellMate's Ansible integration. You know Ansible deeply, and you know *this* integration specifically.

How ShellMate's Ansible integration actually works. Every one of these is a place where ordinary Ansible advice would be wrong here:
- ShellMate does not run Ansible. It drives a container over a REST API. Never tell the user to run `ansible-playbook`, `ansible-galaxy` or `ansible` at a shell — they have no Ansible to run.
- The runner's project directory is a bind mount from the container's host. A playbook written in ShellMate reaches the runner by being copied to that host path, over an SSH session; the API has no endpoint that accepts a playbook.
- The inventory is generated from ShellMate's own saved connections and travels *with* each run. The container keeps no copy. Never say "add it to your inventory file" — there is nothing persistent to add it to unless the user deliberately puts a file in the runner's own inventory directory.
- Group names are sanitised: ShellMate's `site-1/routers` becomes Ansible's `site_1_routers`. A site like `site-1` is emitted as a group of groups, so it is targetable even though no host is tagged with it directly.
- A connection's address is the Ansible host; its ShellMate name travels as the `shellmate_name` variable.
- Serial connections are not in the inventory at all and cannot be targeted — they have no address to reach over the network.
- Credentials come from ShellMate's key store, are held in an encrypted vault, and are resolved at the moment a run starts — delivered as `envvars` or as extra vars. Device passwords are never sent to the runner; the runner logs in with its own SSH key.
- Check mode is Ansible's dry run and ShellMate offers it first, deliberately. Recommend it for anything that writes.
- The builder assembles plays from a small set of task kinds: gather facts, run show commands, push configuration lines, back up the running configuration, and save it. Handlers and `notify` are supported.

How to answer:
- Be concrete about which part of the interface to use: Builder, Playbooks, Templates, Inventory, Environments, Keys, Repositories.
- When the user asks for a playbook, give a complete one in a [PLAYBOOK] block, using collection-qualified module names for the platform in question.
- Say plainly when something changes a device, and prefer idempotent modules over raw CLI where a real module exists.
- If you are not certain a module exists, use the platform's *_command module rather than inventing one.
- Never include credentials in a playbook, and never set ansible_user or ansible_password in one.

When you offer a playbook, wrap it exactly like this so ShellMate can render it:
[PLAYBOOK]
---
- name: Set NTP servers
  hosts: site_1_switches
  gather_facts: false
  tasks:
    - name: Push the NTP servers
      cisco.ios.ios_config:
        lines:
          - ntp server 10.0.0.1
[/PLAYBOOK]
One playbook per block, nothing else inside the block, and the first line inside it is `---`.

You can see the live terminal sessions the user has chosen to share, what ShellMate has established about those devices, and the state of the Ansible integration. Never invent output you have not seen, and never claim a run happened.

You may also suggest a single CLI command when checking something by hand is the quickest way to answer — the user is often logged into the device already.
{command_rules}"""


#: Mode -> the shipped persona body. What "Reset to defaults" restores.
DEFAULT_BODIES: dict[str, str] = {
    "tshoot":      _TSHOOT_BODY,
    "learn":       _LEARN_BODY,
    "investigate": _INVESTIGATE_BODY,
    # Not offered by the mode toggle: chosen by where the user is, not by
    # what they picked, and switched back the moment they leave the view.
    "ansible":     _ANSIBLE_BODY,
}

MODES = tuple(DEFAULT_BODIES)


def render(body: str) -> str:
    """
    Put the command-suggestion rules into a persona body.

    A body that has lost its marker still gets them, appended at the end. That
    is a slightly worse prompt than one where they sit in context — and vastly
    better than silently losing command suggestions with nothing on screen to
    explain why the assistant stopped offering them.
    """
    if RULES_MARKER in body:
        return body.replace(RULES_MARKER, _COMMAND_FORMAT_RULES)
    return f"{body.rstrip()}\n\n{_COMMAND_FORMAT_RULES}"


TSHOOT_SYSTEM_PROMPT = render(_TSHOOT_BODY)
LEARN_SYSTEM_PROMPT = render(_LEARN_BODY)

# Backwards-compat: the summary and Jira paths import SYSTEM_PROMPT. They are
# one-shot uses with no mode toggle, so they get the troubleshoot persona.
SYSTEM_PROMPT = TSHOOT_SYSTEM_PROMPT


def get_system_prompt(mode: str | None) -> str:
    """
    Return the system prompt for the requested mode, honouring any edit.

    Imported inside the function because ``prompt_store`` reads its defaults
    from this module — the alternative is a circular import at load time.
    """
    from backend.ai import prompt_store

    return prompt_store.rendered(mode)


def render_device_facts(facts: dict, stable: bool = True, volatile: bool = True) -> list[str]:
    """
    One line per thing established, worded for the model.

    ``facts`` is what ``router._device_facts()`` gathers:
    platform/name/version/model/confidence/source from the fingerprint,
    ``pending`` from the alert tracker, ``last_capture`` and ``baseline``
    from the config archive, and the connection type.

    The facts fall into two kinds. The platform, the connection and the
    archive's record are the same from one question to the next; a pending
    reload counts down between them. ``stable`` and ``volatile`` pick one
    or both, so the steady part can sit in the cached system block and the
    countdown in the fresh context (#498).
    """
    out: list[str] = []
    if stable:
        out.extend(_stable_facts(facts))
    if volatile:
        out.extend(_volatile_facts(facts))
    return out


def _stable_facts(facts: dict) -> list[str]:
    out: list[str] = []
    name = facts.get("name") or ""
    platform = facts.get("platform") or ""
    confidence = float(facts.get("confidence") or 0.0)
    if platform and platform != "generic":
        how = {"banner": "from its banner", "prompt": "from the prompt shape",
               "ssh-version": "from the SSH version string",
               "version-command": "from its version command",
               "you": "chosen by the engineer"}.get(facts.get("source", ""), "")
        sure = ("certain" if confidence >= 0.9 else
                "probable" if confidence >= 0.6 else "a guess")
        detail = " ".join(x for x in (facts.get("version"), facts.get("model")) if x)
        out.append(f"Platform: {name or platform}" + (f" {detail}" if detail else "")
                   + f" — {sure}" + (f", {how}" if how else "") + ".")
    else:
        out.append("Platform: not identified yet — do not assume a vendor.")
    if facts.get("connection_type"):
        out.append(f"Connected over {str(facts['connection_type']).upper()}.")

    last = facts.get("last_capture")
    if last:
        out.append(f"Configuration last captured by ShellMate: {last}"
                   + (" (a baseline is set for this device)." if facts.get("baseline") else "."))
    elif facts.get("capture_enabled") is False:
        out.append("Configuration capture is switched off; ShellMate holds no snapshot.")
    return out


def _volatile_facts(facts: dict) -> list[str]:
    out: list[str] = []
    pending = facts.get("pending") or None
    if pending:
        kind = pending.get("kind", "action")
        left = pending.get("seconds_left")
        when = (f"in about {int(left // 60)}m {int(left % 60)}s" if isinstance(left, (int, float)) and left >= 0
                else "at a time the device stated")
        cancel = pending.get("cancel_command") or ""
        out.append(f"PENDING on this device: {kind.replace('_', ' ')} {when}."
                   + (f" It is cancelled with: {cancel}" if cancel else "")
                   + " Mention this if it bears on the question.")
    return out


def render_drift_block(drift: dict, active_label: str) -> list[str]:
    """
    What changed on this device since the last visit, as the model sees it (#549).

    ShellMate captures the running configuration on connect and diffs it
    against the previous visit; until now the model was told only *when* the
    capture happened, so "what changed since yesterday" — the first question
    in most outages — was answered by guessing from whatever ``show run``
    happened to be in the buffer.

    The diff arrives already redacted and already capped by
    ``explain.drift_facts()``; this only words it.
    """
    if not drift or not (drift.get("diff") or "").strip():
        return []
    days = drift.get("days_since")
    when = (f"{days} day{'' if days == 1 else 's'} ago" if days else "the last visit")
    return [
        f"=== CONFIGURATION CHANGES SINCE LAST VISIT: {active_label} ===",
        f"  {drift.get('changed', 0)} lines differ from the capture taken {when} "
        f"({drift.get('added', 0)} added, {drift.get('removed', 0)} removed). "
        f"ShellMate captured both; this is not something the engineer typed.",
        drift["diff"],
    ]


def _sessions_block(sessions_summary: list[dict]) -> list[str]:
    lines = ["=== OPEN SESSIONS ==="]
    if sessions_summary:
        for s in sessions_summary:
            lines.append(
                f"  Tab {s.get('tab_num', '?')}: {s.get('label', 'unknown')} "
                f"({s.get('hostname', '?')}) — {s.get('connection_type', 'ssh').upper()}"
            )
    else:
        lines.append("  (no active sessions)")
    return lines


def build_system_preamble(
    sessions_summary: list[dict],
    active_label: str,
    device_context: dict | None = None,
) -> str:
    """
    The part of the context that holds still, for the system block (#498).

    The persona prompt alone is about 750 tokens, under Claude's 1,024-token
    minimum for a cacheable prefix, so the breakpoint on it wrote nothing.
    The open-sessions list and the steady device facts change only when a
    tab opens or a fingerprint is refined — the same from one question to
    the next — so they belong with the persona rather than in the fresh
    context, where they were re-sent and re-charged on every message.
    OpenAI and DeepSeek cache a long stable prefix on their own, so the move
    costs nothing there either.
    """
    lines = _sessions_block(sessions_summary)
    if device_context:
        facts = render_device_facts(device_context, volatile=False)
        if facts:
            lines.append("")
            lines.append(f"=== DEVICE FACTS: {active_label} ===")
            lines.extend(f"  {fact}" for fact in facts)
    return "\n".join(lines)


def build_context_prompt(
    sessions_summary: list[dict],
    active_buffer: str,
    active_label: str,
    command_history: list[str],
    extra_contexts: list[dict] | None = None,
    design_context: str = "",
    device_context: dict | None = None,
    parsed_tables: list[str] | None = None,
    investigation: dict | None = None,
    stable_in_system: bool = False,
) -> str:
    """
    Build the context block prepended to every user message.

    With ``stable_in_system`` the open-sessions list and the steady device
    facts are left out, because :func:`build_system_preamble` has put them
    in the system block; only what changes between questions — the
    terminal output, a pending countdown — is sent here (#498).
    """
    lines = []

    # Where an investigation stands (#403): how many approved steps have
    # been taken against the budget, so the model plans within it.
    if investigation:
        step = int(investigation.get("step", 0))
        budget = int(investigation.get("max", 0))
        lines.append("=== INVESTIGATION ===")
        lines.append(f"  Steps approved so far: {step} of a budget of {budget}."
                     + ("  The budget is spent: conclude now with what you have."
                        if budget and step >= budget else
                        "  One step left: make it count, then conclude."
                        if budget and step == budget - 1 else ""))
        lines.append("")

    # Open sessions summary
    if not stable_in_system:
        lines.extend(_sessions_block(sessions_summary))
        lines.append("")

    # What ShellMate already knows about the active device (#401). The
    # fingerprint, the alert tracker and the config archive had all worked
    # this out and none of it reached the model, which guessed the vendor
    # from the prompt instead.
    if device_context:
        facts = render_device_facts(device_context, stable=not stable_in_system)
        if facts:
            lines.append(f"=== DEVICE FACTS: {active_label} ===")
            lines.extend(f"  {fact}" for fact in facts)
            lines.append("")

    # What ShellMate's own connect-time capture found had changed (#549).
    # Kept out of the system preamble on purpose: the drift check finishes a
    # couple of seconds after the tab opens, so putting it in the cached
    # prefix would invalidate that prefix mid-session for every device.
    drift_lines = render_drift_block((device_context or {}).get("drift"), active_label)
    if drift_lines:
        lines.extend(drift_lines)
        lines.append("")

    # Active session terminal output
    lines.append(f"=== ACTIVE SESSION: {active_label} ===")
    lines.append("--- Terminal output (last 200 lines) ---")
    lines.append(active_buffer or "(no output yet)")
    lines.append("")

    # Rows parsed from recent show commands (#404), beside the raw text.
    if parsed_tables:
        lines.append("--- Structured view of recent output (parsed locally; the raw text above is authoritative) ---")
        for table in parsed_tables:
            lines.append(table)
        lines.append("")

    # Command history. The limit is checked before slicing because
    # ``history[-0:]`` is the whole list, not none of it (#494).
    from backend.advanced import get as advanced

    command_limit = int(advanced("ai.context_commands"))
    if command_history and command_limit > 0:
        lines.append("--- Commands run this session ---")
        for cmd in command_history[-command_limit:]:
            lines.append(f"  {cmd}")
        lines.append("")

    # Extra contexts (/context all or /context N)
    if extra_contexts:
        for ctx in extra_contexts:
            lines.append(f"=== EXTRA CONTEXT: {ctx['label']} ===")
            lines.append(ctx["buffer"])
            lines.append("")

    # Design-guideline snippets from Chroma (only present when configured + matched)
    if design_context:
        lines.append(design_context)
        lines.append("")

    return "\n".join(lines)
