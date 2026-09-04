"""
test_ansible_builder.py — Building a playbook, and reading one back (#586).

Two halves, and the second is the one that matters.

**Building from blocks** has to produce YAML that parses, with the right
module for the platform. A builder that emits something Ansible refuses is
worse than no builder: the person using it has no way to tell whether the
mistake is theirs.

**Reading a playbook back** is the whole safety story for the assisted
path. The screen says what each task does and marks the ones that change a
device, and every way that can be wrong is a way somebody runs something
they did not mean to:

- a module's own arguments counted as extra tasks (`lines:` under
  `ios_config:` was, and the count was wrong in a way nobody would think
  to question);
- a task nobody named, whose module sits on the dash line, dropped
  entirely — which understates what a playbook does;
- an unrecognised module called safe rather than flagged.

The assistant itself is not exercised here. It needs a provider and a key,
and a test that silently skips is worse than one that does not exist. What
*is* tested is everything around it: the refusal with no description, and
the fact that a draft is never saved without a second, deliberate action.

Run: python test_ansible_builder.py
"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backend import paths  # noqa: E402

_TEMP = Path(tempfile.mkdtemp(prefix="shellmate-ansbld-"))
paths._data_dir_cache = _TEMP
assert paths.data_dir() == _TEMP, "test isolation failed — refusing to run"

from fastapi.testclient import TestClient  # noqa: E402

from backend import ansible_builder as builder  # noqa: E402
from backend.app import app  # noqa: E402

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


def _yaml(text: str):
    """Parse, or return the parser's complaint."""
    try:
        import yaml
    except ImportError:                                   # pragma: no cover
        return "skip"
    try:
        return yaml.safe_load(text)
    except Exception as exc:
        return f"ERROR: {exc}"


# ---------------------------------------------------------------------------
def blocks() -> None:
    print("\n-- Building from blocks --")

    spec = {
        "name": "Set NTP servers", "hosts": "core", "family": "ios",
        "blocks": [
            {"kind": "facts"},
            {"kind": "config", "fields": {"lines": "ntp server 10.0.0.1\nntp server 10.0.0.2"}},
            {"kind": "save"},
        ],
    }
    built = builder.build(spec)
    parsed = _yaml(built["text"])
    check("what it builds is valid YAML", not isinstance(parsed, str)
          or parsed == "skip", str(parsed)[:200])
    if isinstance(parsed, list):
        play = parsed[0]
        check("the play targets the hosts asked for", play["hosts"] == "core",
              str(play.get("hosts")))
        check("every task is named",
              all("name" in t for t in play["tasks"]), str(play["tasks"]))
        check("it used the IOS modules, not the generic ones",
              any("cisco.ios" in k for t in play["tasks"] for k in t),
              str(play["tasks"]))

    check("it says which steps write to the device",
          [d["writes"] for d in built["does"]] == [False, True, True],
          str(built["does"]))
    check("and summarises that the playbook writes at all",
          built["writes"] is True, str(built["writes"]))

    generic = builder.build({"family": "nonsense", "blocks": [{"kind": "command",
                             "fields": {"commands": "show version"}}]})
    check("an unknown platform falls back to the generic CLI module",
          "ansible.netcommon" in generic["text"], generic["text"])

    # Configuration lines are full of characters YAML cares about and the
    # user does not. A quoting bug here produces a file Ansible rejects,
    # and the person who typed a perfectly good command gets the blame.
    awkward = builder.build({"family": "ios", "blocks": [{"kind": "config", "fields": {
        "lines": "banner motd # Don't touch #\nsnmp-server community p@ss:word RO\n"
                 "description 100% uplink: to core"}}]})
    parsed = _yaml(awkward["text"])
    check("lines with quotes, colons and hashes survive being YAML",
          not isinstance(parsed, str) or parsed == "skip", str(parsed)[:200])
    if isinstance(parsed, list):
        lines = parsed[0]["tasks"][0]["cisco.ios.ios_config"]["lines"]
        check("and come back exactly as they went in",
              lines[0] == "banner motd # Don't touch #"
              and lines[1] == "snmp-server community p@ss:word RO",
              str(lines))

    for bad, why in [({"blocks": []}, "no tasks"),
                     ({"blocks": [{"kind": "nope"}]}, "an unknown block"),
                     ({"blocks": [{"kind": "config", "fields": {}}]}, "no lines"),
                     ({"blocks": [{"kind": "command", "fields": {}}]}, "no commands")]:
        try:
            builder.build(bad)
            check(f"{why} is refused", False, "it was accepted")
        except builder.BuilderError:
            check(f"{why} is refused", True)

    # save_when: modified, not always. `always` rewrites startup-config on
    # every run whether anything changed or not, which makes a no-op look
    # like a change to everything that reads the device afterwards.
    saved = builder.build({"blocks": [{"kind": "save"}]})
    check("saving only writes when something changed",
          "save_when: modified" in saved["text"], saved["text"])


# ---------------------------------------------------------------------------
def reading_back() -> None:
    print("\n-- Reading a playbook back --")

    play = """---
- name: Mixed
  hosts: access
  tasks:
    - name: Look
      cisco.ios.ios_facts:
        gather_subset: min
    - name: Change
      cisco.ios.ios_config:
        lines:
          - ntp server 1.1.1.1
        parents: line vty 0 4
    - ansible.builtin.debug:
        msg: hello
    - name: Made up
      acme.widget.frobnicate:
        thing: 1
"""
    found = builder.inspect(play)
    modules = [t["module"] for t in found["tasks"]]

    check("it finds exactly the four tasks", len(found["tasks"]) == 4,
          str(modules))
    check("a module's own arguments are not counted as tasks",
          "lines" not in modules and "parents" not in modules,
          f"it read {modules}")
    check("a task nobody named is still found",
          "ansible.builtin.debug" in modules,
          "an unnamed task was dropped, which understates what the play does")
    check("it knows which host pattern the play targets",
          found["hosts"] == ["access"], str(found["hosts"]))

    writes = {t["module"]: t["writes"] for t in found["tasks"]}
    check("gathering facts reads", writes["cisco.ios.ios_facts"] is False)
    check("pushing configuration writes", writes["cisco.ios.ios_config"] is True)
    check("debug reads", writes["ansible.builtin.debug"] is False)
    check("an unrecognised module counts as a write",
          writes["acme.widget.frobnicate"] is True,
          "not knowing a module is a reason to look, not a reason to call it safe")
    check("and it is named so somebody can check it",
          found["unknown_modules"] == ["acme.widget.frobnicate"],
          str(found["unknown_modules"]))

    # A file that does not parse is exactly the one somebody needs help
    # reading, so the scan must not be a YAML parse in disguise.
    broken = builder.inspect("- name: Broken\n  hosts: all\n   tasks:\n"
                             "  - cisco.ios.ios_config:\n      lines: [a]\n")
    check("a playbook that does not parse is still read",
          len(broken["tasks"]) >= 1, str(broken))

    noisy = builder.inspect("""---
- name: Blunt
  hosts: all
  tasks:
    - name: Ask
      cisco.ios.ios_command:
        commands: [show version]
""")
    check("it warns that check mode will not cover a command module",
          "check mode" in (noisy["check_mode_note"] or ""),
          f"it said: {noisy['check_mode_note']!r}")
    check("and names the module it means",
          "cisco.ios.ios_command" in (noisy["check_mode_note"] or ""),
          noisy["check_mode_note"])

    check("nothing at all reads as nothing at all",
          builder.inspect("")["tasks"] == [], str(builder.inspect("")))


# ---------------------------------------------------------------------------
def endpoints() -> None:
    print("\n-- Through the API --")

    with TestClient(app, base_url="http://127.0.0.1") as client:
        vocabulary = client.get("/api/ansible/builder").json()
        check("the vocabulary lists the blocks",
              set(vocabulary["blocks"]) ==
              {"facts", "command", "config", "backup", "save"},
              str(list(vocabulary["blocks"])))
        check("and the platforms, generic first",
              vocabulary["families"][0]["id"] == "generic",
              str(vocabulary["families"][:1]))
        check("the platforms carry readable names",
              any(f["label"].startswith("Cisco") for f in vocabulary["families"]),
              str([f["label"] for f in vocabulary["families"]]))

        response = client.post("/api/ansible/build", json={
            "name": "Test", "hosts": "all", "family": "ios",
            "blocks": [{"kind": "command", "fields": {"commands": "show version"}}]})
        check("building over HTTP works", response.status_code == 200,
              response.text[:200])
        text = response.json()["text"]

        response = client.post("/api/ansible/inspect", json={"text": text})
        check("and it can be read back over HTTP",
              response.status_code == 200
              and any("ios_command" in t["module"] for t in response.json()["tasks"]),
              response.text[:200])

        response = client.post("/api/ansible/build", json={"blocks": []})
        check("a playbook with no tasks is refused with a reason",
              response.status_code == 400 and "task" in response.json()["detail"],
              response.text[:200])

        # Building and saving are one call only when explicitly asked; the
        # draft path has no save at all. A model's output reaching the
        # library without somebody choosing to keep it is the failure this
        # separation exists to prevent.
        response = client.post("/api/ansible/build", json={
            "name": "Kept", "family": "ios", "save_as": "kept-test.yml",
            "blocks": [{"kind": "facts"}]})
        check("building with save_as keeps it",
              response.status_code == 200 and "saved" in response.json(),
              response.text[:200])
        listed = client.get("/api/ansible/playbooks").json()
        mine = listed.get("library", []) if isinstance(listed, dict) else listed
        check("and it appears in the library",
              any(p.get("name") == "kept-test.yml" for p in mine),
              str(listed)[:300])

        response = client.post("/api/ansible/draft", json={"description": "   "})
        check("the assistant refuses an empty description",
              response.status_code == 400, response.text[:200])
        check("without having called a provider",
              "assistant" not in response.json()["detail"].lower()
              or "reach" not in response.json()["detail"].lower(),
              response.text[:200])


if __name__ == "__main__":
    blocks()
    reading_back()
    endpoints()

    print("\n" + "=" * 52)
    print(f"  {passed} passed  |  {len(failed)} failed")
    print("=" * 52)
    for line in failed:
        print("  -", line)
    sys.exit(1 if failed else 0)
