"""
test_env_example.py — Documenting a variable is a claim that setting it works.

`.env.example` is the only place most people will learn that a configuration
variable exists. That makes it two promises, and both can rot silently:

- **Everything the code reads is listed.** A variable honoured by the code
  and mentioned nowhere is a variable nobody knows exists. This drift is
  invisible — nothing fails, the feature simply appears not to have the
  option. Eight had accumulated that way: the Ansible runner token, three
  provider keys and all four Jira settings.
- **Everything listed actually works.** A documented no-op is worse than an
  undocumented variable, because the undocumented one at least does not lie
  about itself. Somebody sets it, nothing happens, and nothing says so.

The second promise has a specific way of failing here that no amount of
reading the source would catch. Under `--onefile` the process unpacks into
a temporary directory, so a `.env` located relative to `__file__` is looked
for in a folder that is deleted on exit — found empty, silently, every
time. `paths.env_file()` exists to prevent that, and this asserts it by
loading a real file from a real app directory rather than by trusting that
it still does.

Run: python test_env_example.py
"""

import os
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

ROOT = Path(__file__).parent

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                        # pragma: no cover
    pass

passed = 0
failed: list[str] = []

#: Variables the operating system provides. Reading these is not a
#: ShellMate setting and listing them would be noise.
_OS_PROVIDED = {
    "APPDATA", "LOCALAPPDATA", "USERNAME", "XDG_DATA_HOME", "HOME", "PATH",
    "TEMP", "TMP", "USERPROFILE", "COMSPEC", "PATHEXT", "SYSTEMROOT",
    "PYTHONIOENCODING", "PYTHONUTF8", "PYTHONPATH",
}

#: Set by ShellMate for its own child processes rather than read from a
#: user's .env, so documenting them would invite somebody to set them.
_INTERNAL = {"SHELLMATE_SKIP_TESTS", "SHELLMATE_TEST_TIMEOUT"}


def check(name: str, condition: bool, detail: str = "") -> None:
    global passed
    if condition:
        passed += 1
        print(f"  OK  {name}")
    else:
        failed.append(f"{name}: {detail}")
        print(f"  FAIL {name}\n       {detail}")


def documented() -> set[str]:
    """Every variable named in .env.example."""
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    found = set()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            found.add(line.split("=", 1)[0].strip())
    return found


def read_by_code() -> dict[str, str]:
    """Every variable the backend actually reads, and where."""
    # Every shape a variable is read in, including the wrappers. Missing one
    # produces a false "documented but never read", and a test that cries
    # wolf gets muted — `_env_int` alone accused three working settings of
    # being no-ops on the first run.
    patterns = [
        # os.getenv("X"), os.environ.get("X"), and the aliased import form
        re.compile(r"""(?:os\.environ\.get|os\.getenv|environ\.get"""
                   r"""|_os\.environ\.get|_os\.getenv)"""
                   r"""\(\s*["']([A-Z][A-Z0-9_]{2,})["']"""),
        # config.py's own helpers: _env("X", …), _env_int("X", …)
        re.compile(r"""_env(?:_int|_bool|_float)?\("""
                   r"""\s*["']([A-Z][A-Z0-9_]{2,})["']"""),
        # os.environ["X"]
        re.compile(r"""os\.environ\[\s*["']([A-Z][A-Z0-9_]{2,})["']"""),
        # A module-level constant naming the variable it reads, which is how
        # auth.py holds SHELLMATE_AUTH_TOKEN.
        re.compile(r"""^ENV_VAR\s*=\s*["']([A-Z][A-Z0-9_]{2,})["']""",
                   re.MULTILINE),
    ]

    found: dict[str, str] = {}
    for source in list((ROOT / "backend").rglob("*.py")) + [ROOT / "run.py"]:
        text = source.read_text(encoding="utf-8")
        names: set[str] = set()
        for pattern in patterns:
            names.update(pattern.findall(text))
        for name in names:
            found.setdefault(name, str(source.relative_to(ROOT)))
    return found


def every_variable_is_documented() -> None:
    print("\n-- Nothing the code reads is a secret from the reader --")
    listed = documented()
    used = read_by_code()

    check("the example file lists something at all", len(listed) > 5,
          f"only {len(listed)}: {sorted(listed)}")

    missing = {n: where for n, where in used.items()
               if n not in listed and n not in _OS_PROVIDED and n not in _INTERNAL}
    check("every variable the code reads is in .env.example", not missing,
          "read but undocumented: "
          + ", ".join(f"{n} ({w})" for n, w in sorted(missing.items()))
          + " — a variable nobody knows exists")


def every_variable_does_something() -> None:
    print("\n-- And nothing listed is a no-op --")
    listed = documented()
    used = set(read_by_code())

    # A name in the example that nothing reads is a promise the code does
    # not keep. Someone sets it, nothing happens, nothing says so.
    dead = sorted(n for n in listed if n not in used)
    check("every variable in .env.example is read somewhere", not dead,
          "documented but never read: " + ", ".join(dead)
          + " — a documented no-op lies about itself, which an undocumented "
            "variable at least does not")


def a_real_env_file_is_actually_loaded() -> None:
    """
    The portable trap, asserted rather than reasoned about.

    Under --onefile the process unpacks into a temporary directory, so a
    .env resolved relative to __file__ is read from a folder that does not
    survive the run — found empty, with no error, every time. This loads a
    real file from a real app directory and checks the values arrive.
    """
    print("\n-- A .env beside the executable actually reaches the code --")

    from dotenv import load_dotenv

    from backend import paths

    home = Path(tempfile.mkdtemp(prefix="shellmate-envtest-"))
    (home / ".env").write_text(
        "ANSIBLE_RUNNER_TOKEN=from-the-env-file\n"
        "OPENAI_API_KEY=openai-from-env\n"
        "JIRA_PROJECT_KEY=NET\n",
        encoding="utf-8")

    real_app_dir = paths.app_dir
    saved = {k: os.environ.get(k) for k in
             ("ANSIBLE_RUNNER_TOKEN", "OPENAI_API_KEY", "JIRA_PROJECT_KEY")}
    for key in saved:
        os.environ.pop(key, None)

    try:
        paths.app_dir = lambda: home
        target = paths.env_file()
        check("env_file() points beside the executable, not at the source tree",
              target == home / ".env",
              f"it pointed at {target} — under --onefile a path derived from "
              f"__file__ is inside the unpack directory and is deleted on exit")

        load_dotenv(dotenv_path=target)
        check("a value in that file reaches the environment",
              os.environ.get("ANSIBLE_RUNNER_TOKEN") == "from-the-env-file",
              f"it read {os.environ.get('ANSIBLE_RUNNER_TOKEN')!r}")

        # And that the code which reads it agrees, rather than the variable
        # merely existing in os.environ with nothing looking at it.
        from backend import ansible

        cfg_token = ""
        try:
            cfg_token = ansible.config().token
        except Exception as exc:                          # pragma: no cover
            cfg_token = f"raised {exc}"
        check("and the Ansible client picks the token up from there",
              cfg_token == "from-the-env-file",
              f"the client saw {cfg_token!r} — documenting the variable "
              f"claims setting it works")

        check("a provider key arrives the same way",
              os.environ.get("OPENAI_API_KEY") == "openai-from-env",
              str(os.environ.get("OPENAI_API_KEY")))
        check("and so does a Jira setting",
              os.environ.get("JIRA_PROJECT_KEY") == "NET",
              str(os.environ.get("JIRA_PROJECT_KEY")))
    finally:
        paths.app_dir = real_app_dir
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


if __name__ == "__main__":
    every_variable_is_documented()
    every_variable_does_something()
    a_real_env_file_is_actually_loaded()

    print("\n" + "=" * 52)
    print(f"  {passed} passed  |  {len(failed)} failed")
    print("=" * 52)
    for line in failed:
        print("  -", line)
    sys.exit(1 if failed else 0)
