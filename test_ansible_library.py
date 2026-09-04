"""
test_ansible_library.py — The Ansible library and the key store (#586).

The runner holds playbooks and runs them. Everything that makes an
automation *repeatable* lives in ShellMate: templates with named holes,
environments, repositories, and the credentials a run needs.

What is worth testing here is mostly refusal. A template that stores a hole
nothing describes produces a form that cannot ask for it and a run that
fails on an undefined variable three tasks in. A key store that hands back
values through its own API is not a key store. An environment that forces
check mode has to force it in one direction only, or "production, and I
mean it" is worth nothing.

Run: python test_ansible_library.py
"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backend import paths  # noqa: E402

_TEMP = Path(tempfile.mkdtemp(prefix="shellmate-anslib-"))
paths._data_dir_cache = _TEMP
assert paths.data_dir() == _TEMP, "test isolation failed — refusing to run"

from backend import ansible_keys, ansible_library  # noqa: E402

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


def refuses(fn, *args, **kwargs) -> str:
    """Run something that must be refused; return why."""
    try:
        fn(*args, **kwargs)
    except (ansible_library.LibraryError, ansible_keys.KeyError_) as exc:
        return str(exc)
    return ""


# ---------------------------------------------------------------------------
def templates() -> None:
    print("\n-- Templates --")

    saved = ansible_library.save_template({
        "name": "Shut an interface",
        "description": "Take a port down, deliberately.",
        "body": "- hosts: {{ target }}\n  tasks:\n    - name: shut {{ interface }}\n",
        "variables": [
            {"name": "target", "label": "Devices"},
            {"name": "interface", "label": "Interface", "help": "e.g. Gi1/0/4"},
        ],
    })
    check("a template saves", saved["name"] == "Shut an interface" and saved["id"],
          str(saved))
    check("its holes are described", len(saved["variables"]) == 2,
          str(saved["variables"]))
    check("a label is filled in from the name when absent",
          ansible_library.save_template({
              "name": "Bare", "body": "{{ some_var }}",
              "variables": [{"name": "some_var"}],
          })["variables"][0]["label"] == "some var",
          "an unlabelled variable should read as words, not as a key")

    why = refuses(ansible_library.save_template,
                  {"name": "Leaky", "body": "- hosts: {{ nobody_asked }}",
                   "variables": []})
    check("a hole nothing describes is refused", "nobody_asked" in why,
          f"it said: {why!r}")

    why = refuses(ansible_library.save_template,
                  {"name": "Bad var", "body": "x",
                   "variables": [{"name": "Not-A-Var"}]})
    check("an unusable variable name is refused", bool(why), "it was accepted")

    why = refuses(ansible_library.save_template,
                  {"name": "Twice", "body": "{{ a }}",
                   "variables": [{"name": "a"}, {"name": "a"}]})
    check("the same variable described twice is refused", "twice" in why.lower(),
          f"it said: {why!r}")

    why = refuses(ansible_library.save_template, {"name": "Empty", "body": "  \n"})
    check("a template with no body is refused", bool(why), "it was accepted")

    why = refuses(ansible_library.save_template,
                  {"name": "../../etc/passwd", "body": "x"})
    check("a name that is a path is refused", bool(why),
          "names become filenames; this one must not")

    print("\n-- Filling one in --")
    text = ansible_library.render_template(saved, {"target": "core", "interface": "Gi1/0/4"})
    check("both holes are filled",
          "hosts: core" in text and "shut Gi1/0/4" in text, text)

    why = refuses(ansible_library.render_template, saved, {"target": "core"})
    check("a missing value stops it, by label", "Interface" in why,
          f"it said: {why!r}")

    # Substitution is literal on purpose: a template is text somebody typed,
    # and handing that to a Jinja environment would be a code path from a
    # text box. Ansible still evaluates its own Jinja when the play runs.
    injected = ansible_library.render_template(
        saved, {"target": "{{ lookup('pipe', 'whoami') }}", "interface": "Gi1"})
    check("a value that looks like Jinja is inserted, not evaluated",
          "lookup('pipe', 'whoami')" in injected,
          "ShellMate must not evaluate a template it was handed")

    with_choices = ansible_library.save_template({
        "name": "Choice", "body": "state: {{ state }}",
        "variables": [{"name": "state", "choices": ["up", "down"]}],
    })
    why = refuses(ansible_library.render_template, with_choices, {"state": "sideways"})
    check("a value outside the choices is refused", "up, down" in why,
          f"it said: {why!r}")

    check("a template can be deleted",
          ansible_library.delete_template(saved["id"]) is True)
    check("deleting one that is gone says so",
          ansible_library.delete_template(saved["id"]) is False)


# ---------------------------------------------------------------------------
def environments() -> None:
    print("\n-- Environments --")

    prod = ansible_library.save_environment({
        "name": "Production",
        "variables": {"ansible_user": "netops"},
        "force_check": True, "verbosity": 9,
    })
    check("an environment saves", prod["name"] == "Production", str(prod))
    check("force_check survives", prod["force_check"] is True, str(prod))
    check("verbosity is clamped to what Ansible has", prod["verbosity"] == 4,
          f"verbosity was {prod['verbosity']}")

    why = refuses(ansible_library.save_environment,
                  {"name": "Bad", "variables": {"Not A Var": 1}})
    check("an unusable variable name is refused", bool(why), "it was accepted")

    why = refuses(ansible_library.save_environment,
                  {"name": "Nowhere", "inventory_source": "elsewhere"})
    check("an inventory from nowhere is refused", bool(why), "it was accepted")

    found = ansible_library.environment(prod["id"])
    check("an environment can be looked up by id",
          found and found["name"] == "Production", str(found))

    # Updating in place, rather than gaining a second Production.
    again = ansible_library.save_environment({
        "id": prod["id"], "name": "Production", "force_check": False})
    check("saving with the same id updates rather than duplicates",
          again["id"] == prod["id"]
          and len([e for e in ansible_library.environments()
                   if e["name"] == "Production"]) == 1,
          str(ansible_library.environments()))

    check("an environment can be deleted",
          ansible_library.delete_environment(prod["id"]) is True)


# ---------------------------------------------------------------------------
def repositories() -> None:
    print("\n-- Repositories --")

    repo = ansible_library.save_repository({
        "name": "Netops playbooks",
        "url": "https://github.com/example/netops.git",
    })
    check("a repository saves", repo["branch"] == "main", str(repo))

    for bad in ("file:///etc", "javascript:alert(1)", "nonsense"):
        check(f"{bad!r} is refused as a URL",
              bool(refuses(ansible_library.save_repository,
                           {"name": "Bad", "url": bad})),
              "it was accepted")

    for good in ("git@github.com:example/x.git", "ssh://git@host/x.git"):
        check(f"{good!r} is accepted",
              bool(ansible_library.save_repository({"name": "OK", "url": good})),
              "a legitimate remote was refused")

    noted = ansible_library.note_revision(repo["id"], "a1b2c3d")
    check("a revision can be noted", noted and noted["revision"] == "a1b2c3d",
          str(noted))
    check("and when it was noted is recorded", noted.get("checked"), str(noted))

    check("counts sees all three kinds",
          set(ansible_library.counts()) ==
          {"templates", "environments", "repositories"},
          str(ansible_library.counts()))


# ---------------------------------------------------------------------------
def keys() -> None:
    print("\n-- The key store --")

    made = ansible_keys.save_key({
        "name": "azure_secret", "kind": "cloud", "delivery": "env",
        "value": "s3cret-value",
    })
    check("a key saves", made["name"] == "azure_secret", str(made))
    check("an env key gets a shouting default target",
          made["target"] == "AZURE_SECRET", str(made))
    check("it reports itself readable", made["readable"] is True, str(made))

    listed = ansible_keys.keys()
    check("listing returns the key", len(listed) == 1, str(listed))
    check("listing carries no value at all",
          all("value" not in row for row in listed)
          and "s3cret-value" not in str(listed),
          "a value reached an API response — the store is not a store")

    env, extra, unreadable = ansible_keys.resolve(["azure_secret"])
    check("resolving produces the environment variable",
          env == {"AZURE_SECRET": "s3cret-value"}, str(env))
    check("and nothing as an extra var", extra == {}, str(extra))
    check("and nothing unreadable", unreadable == [], str(unreadable))

    _e, extra, _u = ansible_keys.resolve(["meraki_key"])
    check("a key that does not exist is reported, not silently skipped",
          ansible_keys.resolve(["meraki_key"])[2] == ["meraki_key"],
          "an unknown key must stop a run, not blank a variable")

    var = ansible_keys.save_key({
        "name": "vault_pass", "kind": "vault", "delivery": "extra_var",
        "value": "hunter2",
    })
    check("an extra-var key keeps its own case", var["target"] == "vault_pass",
          str(var))
    _env, extra, _u = ansible_keys.resolve(["vault_pass"])
    check("and resolves as an extra var", extra == {"vault_pass": "hunter2"},
          str(extra))

    print("\n-- What it refuses --")
    for bad in ("Azure Secret", "9lives", "has-dashes", ""):
        check(f"{bad!r} is refused as a key name",
              bool(refuses(ansible_keys.save_key, {"name": bad, "value": "x"})),
              "it was accepted; a key name becomes a variable")

    check("a duplicate name is refused",
          "already" in refuses(ansible_keys.save_key,
                               {"name": "azure_secret", "value": "x"}),
          "two keys with one name make the reference ambiguous")

    check("a new key with no value is refused",
          bool(refuses(ansible_keys.save_key, {"name": "empty_one"})),
          "a key with nothing in it resolves to a blank credential")

    check("an unknown delivery is refused",
          bool(refuses(ansible_keys.save_key,
                       {"name": "odd_one", "delivery": "carrier_pigeon",
                        "value": "x"})),
          "it was accepted")

    print("\n-- Editing without retyping the secret --")
    moved = ansible_keys.save_key({
        "id": made["id"], "name": "azure_secret", "kind": "cloud",
        "delivery": "env", "target": "AZURE_CLIENT_SECRET",
    })
    check("the target can change with no value given",
          moved["target"] == "AZURE_CLIENT_SECRET", str(moved))
    check("and the stored value survives it",
          ansible_keys.resolve(["azure_secret"])[0]
          == {"AZURE_CLIENT_SECRET": "s3cret-value"},
          "changing where a key is delivered lost the key")

    renamed = ansible_keys.save_key({
        "id": made["id"], "name": "azure_client_secret", "kind": "cloud",
        "delivery": "env", "target": "AZURE_CLIENT_SECRET",
    })
    check("a rename carries the value across",
          ansible_keys.resolve(["azure_client_secret"])[0]
          == {"AZURE_CLIENT_SECRET": "s3cret-value"},
          "renaming a key lost its value")
    check("and the old name resolves to nothing",
          ansible_keys.resolve(["azure_secret"])[2] == ["azure_secret"],
          "the old name still resolves — the value was left behind")

    print("\n-- Forgetting one --")
    check("a key can be deleted", ansible_keys.delete_key(renamed["id"]) is True)
    check("and its value goes with it",
          ansible_keys.resolve(["azure_client_secret"])[2]
          == ["azure_client_secret"],
          "the metadata went but the secret stayed in the vault")
    check("deleting one that is gone says so",
          ansible_keys.delete_key(renamed["id"]) is False)


def routes() -> None:
    """
    No two routes share a method and a path.

    Nothing enforces this. Declaring two routes on one method and path is
    not an error: FastAPI serves whichever was registered first and leaves
    the other unreachable, silently. With eight new Ansible routes going in
    beside a set that already used similar names, the odds of landing on
    one were good enough to spend a check on — and the failure it prevents
    is a caller quietly receiving the wrong shape and doubting its own
    parsing.
    """
    print("\n-- No route is shadowed by another --")
    from backend.app import app

    seen: dict[tuple, str] = {}
    clashes: list[str] = []
    for route in app.routes:
        for method in getattr(route, "methods", None) or []:
            if method in ("HEAD", "OPTIONS"):
                continue
            key = (method, getattr(route, "path", ""))
            name = getattr(route, "name", "?")
            if key in seen:
                clashes.append(f"{method} {key[1]}: {seen[key]} then {name}")
            else:
                seen[key] = name
    check("no two routes share a method and a path", not clashes,
          "; ".join(clashes))


if __name__ == "__main__":
    templates()
    environments()
    repositories()
    keys()
    routes()

    print("\n" + "=" * 52)
    print(f"  {passed} passed  |  {len(failed)} failed")
    print("=" * 52)
    for line in failed:
        print("  -", line)
    sys.exit(1 if failed else 0)
