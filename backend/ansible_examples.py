"""
ansible_examples.py — Templates worth starting from (#590).

An empty template library gives no sense of what a template is *for*, and
the first one anybody writes from a blank editor is the hardest one they
will ever write. So a handful ship — real jobs a network engineer does
repeatedly, not toys.

Two decisions worth stating, because they are the ones that make this
useful rather than clutter:

- **Seeded once, then they are yours.** They are written into the library
  on first run and never again, so editing or deleting one sticks. A set
  that reappeared after being deleted would be the software arguing with
  somebody about their own data.
- **The badge is derived, so a description must not argue with it.** Whether
  a template writes is read off its body, and the scan is conservative: it
  marks by module, not by how the module is used. So "collect running
  configurations" comes back as *writes*, because `ios_config` can write —
  even though with only `backup` set it changes nothing. The description
  says that rather than claiming read-only and being contradicted by the
  badge sitting next to it. A shipped example whose own text disagrees with
  ShellMate's own verdict would teach exactly the wrong lesson about which
  to trust.

Every body is quoted the way a real configuration line needs. Colons,
hashes and leading digits all mean something to YAML and nothing to the
person typing `description 100% uplink: to core`, which is the single most
likely way a hand-written first template fails.
"""

import logging

logger = logging.getLogger(__name__)

#: Written into the library on first run. `id` is fixed so a reseed cannot
#: duplicate one, and so a later version of ShellMate can recognise the
#: shipped copy rather than treating an edited one as new.
EXAMPLES: list[dict] = [
    {
        "id": "example-collect-configs",
        "name": "Collect running configurations",
        "description": "Fetch the running configuration from every device in "
                       "the group and keep it on the runner. Marked as "
                       "writing because ios_config is a module that can "
                       "write; with only backup set it changes nothing.",
        "platform": "ios",
        "body": "- name: Collect running configurations\n"
                "  hosts: {{ target }}\n"
                "  gather_facts: false\n"
                "  tasks:\n"
                "    - name: Back up the running configuration\n"
                "      cisco.ios.ios_config:\n"
                "        backup: true\n",
        "variables": [
            {"name": "target", "label": "Devices",
             "help": "A group, or an Ansible host pattern.",
             "required": True},
        ],
    },
    {
        "id": "example-show-commands",
        "name": "Run show commands",
        "description": "Run one or more read-only commands across a group and "
                       "print what came back. The quickest way to answer "
                       "\"is this the same everywhere\".",
        "platform": "ios",
        "body": "- name: Run show commands\n"
                "  hosts: {{ target }}\n"
                "  gather_facts: false\n"
                "  tasks:\n"
                "    - name: Ask the device\n"
                "      cisco.ios.ios_command:\n"
                "        commands:\n"
                "          - {{ command }}\n"
                "      register: shellmate_output\n"
                "    - name: Show what came back\n"
                "      ansible.builtin.debug:\n"
                "        var: shellmate_output.stdout_lines\n",
        "variables": [
            {"name": "target", "label": "Devices", "required": True},
            {"name": "command", "label": "Command",
             "default": "show version", "required": True,
             "help": "One command. For several, edit the playbook after "
                     "filling this in."},
        ],
    },
    {
        "id": "example-ntp",
        "name": "Set NTP servers",
        "description": "Point a group at your NTP servers. Idempotent — "
                       "running it twice changes nothing the second time.",
        "platform": "ios",
        "body": "- name: Set NTP servers\n"
                "  hosts: {{ target }}\n"
                "  gather_facts: false\n"
                "  tasks:\n"
                "    - name: Configure NTP\n"
                "      cisco.ios.ios_config:\n"
                "        lines:\n"
                "          - ntp server {{ primary }}\n"
                "          - ntp server {{ secondary }}\n"
                "        save_when: modified\n",
        "variables": [
            {"name": "target", "label": "Devices", "required": True},
            {"name": "primary", "label": "Primary NTP server",
             "default": "10.0.0.1", "required": True},
            {"name": "secondary", "label": "Secondary NTP server",
             "default": "10.0.0.2", "required": True},
        ],
    },
    {
        "id": "example-syslog",
        "name": "Set syslog servers",
        "description": "Send logging to your collector. Worth running across "
                       "a whole site after somebody builds a switch by hand.",
        "platform": "ios",
        "body": "- name: Set syslog servers\n"
                "  hosts: {{ target }}\n"
                "  gather_facts: false\n"
                "  tasks:\n"
                "    - name: Configure logging\n"
                "      cisco.ios.ios_config:\n"
                "        lines:\n"
                "          - logging host {{ collector }}\n"
                "          - logging trap {{ level }}\n"
                "        save_when: modified\n",
        "variables": [
            {"name": "target", "label": "Devices", "required": True},
            {"name": "collector", "label": "Syslog server",
             "default": "10.0.0.5", "required": True},
            {"name": "level", "label": "Trap level", "default": "informational",
             "choices": ["emergencies", "alerts", "critical", "errors",
                         "warnings", "notifications", "informational",
                         "debugging"]},
        ],
    },
    {
        "id": "example-interface-state",
        "name": "Shut or unshut an interface",
        "description": "Take a port down or bring it back, and leave a "
                       "description saying why. The description is required "
                       "on purpose — an interface shut with no reason is a "
                       "ticket somebody opens six months later.",
        "platform": "ios",
        "body": "- name: {{ state }} an interface\n"
                "  hosts: {{ target }}\n"
                "  gather_facts: false\n"
                "  tasks:\n"
                "    - name: Set the state of {{ interface }}\n"
                "      cisco.ios.ios_config:\n"
                "        parents: interface {{ interface }}\n"
                "        lines:\n"
                "          - description {{ reason }}\n"
                "          - {{ state }}\n"
                "        save_when: modified\n",
        "variables": [
            {"name": "target", "label": "Device", "required": True,
             "help": "One device, usually — a port number means different "
                     "things on different switches."},
            {"name": "interface", "label": "Interface",
             "help": "For example GigabitEthernet1/0/4.", "required": True},
            {"name": "state", "label": "State", "default": "shutdown",
             "choices": ["shutdown", "no shutdown"]},
            {"name": "reason", "label": "Why", "required": True,
             "help": "Goes onto the interface description, so whoever finds "
                     "it later knows what happened."},
        ],
    },
    {
        "id": "example-vlan-on-trunk",
        "name": "Allow a VLAN on a trunk",
        "description": "Add a VLAN to a trunk's allowed list. Uses `add` "
                       "rather than replacing the list, because replacing it "
                       "is how an uplink loses every other VLAN at once.",
        "platform": "ios",
        "body": "- name: Allow a VLAN on a trunk\n"
                "  hosts: {{ target }}\n"
                "  gather_facts: false\n"
                "  tasks:\n"
                "    - name: Add VLAN {{ vlan }} to {{ interface }}\n"
                "      cisco.ios.ios_config:\n"
                "        parents: interface {{ interface }}\n"
                "        lines:\n"
                "          - switchport trunk allowed vlan add {{ vlan }}\n"
                "        save_when: modified\n",
        "variables": [
            {"name": "target", "label": "Devices", "required": True},
            {"name": "interface", "label": "Trunk interface", "required": True},
            {"name": "vlan", "label": "VLAN id", "required": True},
        ],
    },
    {
        "id": "example-banner",
        "name": "Set the login banner",
        "description": "Push a banner to every device. A worked example of "
                       "text that YAML would otherwise mangle — the body is "
                       "quoted so a banner containing a colon or a hash "
                       "survives.",
        "platform": "ios",
        "body": "- name: Set the login banner\n"
                "  hosts: {{ target }}\n"
                "  gather_facts: false\n"
                "  tasks:\n"
                "    - name: Configure the banner\n"
                "      cisco.ios.ios_banner:\n"
                "        banner: login\n"
                "        text: \"{{ message }}\"\n"
                "        state: present\n",
        "variables": [
            {"name": "target", "label": "Devices", "required": True},
            {"name": "message", "label": "Banner text", "required": True,
             "default": "Authorised access only. Activity is logged."},
        ],
    },
]


def seed_if_empty() -> int:
    """
    Write the examples on first run, and never again.

    Only when the library is completely empty, so somebody who has deleted
    them does not get them back. A set that reappears after being deleted
    is the software arguing with a person about their own data.

    Never raises. Examples failing to load is not a reason for the Ansible
    view to be unavailable.
    """
    from backend import ansible_library

    try:
        if ansible_library.templates():
            return 0
    except Exception:                                     # pragma: no cover
        return 0

    written = 0
    for example in EXAMPLES:
        try:
            ansible_library.save_template(dict(example))
            written += 1
        except Exception as exc:                          # pragma: no cover
            # One bad example must not cost the rest. It also means a
            # template that stops validating after a change to the library
            # shows up here rather than taking the seeding down with it.
            logger.warning("Example template %r was refused: %s",
                           example.get("name"), exc)
    if written:
        logger.info("Seeded %d example Ansible templates", written)
    return written
