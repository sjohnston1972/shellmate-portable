"""
test_ansible_github.py — A playbook's history, and the ways it must not cost you (#609).

Committing a playbook to GitHub is a small feature with three ways of
being quietly catastrophic, and they are what this covers:

- **The token in settings.json.** It is a credential, so it goes to the
  vault like every other one. `settings.json` is a plain file people are
  told to edit, get told to attach to a support request, and sync between
  machines.
- **A failed commit costing the save.** GitHub being unreachable must
  never lose somebody's work. That would be an appalling trade for a
  feature whose entire purpose is not losing work.
- **A public repository by accident.** A playbook carries hostnames,
  addresses and the shape of an estate, and a public repository cannot be
  un-published — the mirrors have it. So private is not merely the
  default in the UI; it is what the request carries unless something says
  otherwise.

GitHub itself is replaced by a small server that answers the same shapes.
Reaching the real one from a test would need somebody's credentials and
would create real repositories, and the thing being asserted is what
ShellMate sends and how it behaves when the answer is bad — both of which
a stand-in can say more precisely than the real thing.

Run: python test_ansible_github.py
"""

import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backend import paths  # noqa: E402

_TEMP = Path(tempfile.mkdtemp(prefix="shellmate-ansgit-"))
paths._data_dir_cache = _TEMP
assert paths.data_dir() == _TEMP, "test isolation failed — refusing to run"

from backend import ansible_git, settings_store  # noqa: E402
from backend.vault import vault  # noqa: E402

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
    try:
        fn(*args, **kwargs)
    except ansible_git.GitError as exc:
        return str(exc)
    return ""


# ---------------------------------------------------------------------------
# A stand-in for GitHub
# ---------------------------------------------------------------------------
#: Every request it received, so what ShellMate *sent* can be asserted —
#: which for the visibility question is the only thing that matters. A
#: response saying "private" proves nothing if the request asked for
#: public and the stand-in ignored it.
seen: list[dict] = []

#: What to answer next, keyed by "METHOD /path". A missing entry is a 404,
#: which is also how "the file is not there yet" is expressed.
answers: dict = {}


class Fake(BaseHTTPRequestHandler):
    def log_message(self, *args):                        # quiet
        pass

    def _handle(self, method: str):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw) if raw else None
        except ValueError:
            body = None
        seen.append({"method": method, "path": self.path, "body": body,
                     "auth": self.headers.get("Authorization", "")})

        status, payload = answers.get(f"{method} {self.path}",
                                      (404, {"message": "Not Found"}))
        text = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(text)))
        self.end_headers()
        self.wfile.write(text)

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PUT(self):
        self._handle("PUT")


def start_fake() -> str:
    server = HTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_port}"


PLAYBOOK = "---\n- hosts: all\n  tasks:\n    - name: nothing\n      ping:\n"


def the_token_never_lands_in_settings() -> None:
    print("\n-- The token goes to the vault, never to settings.json --")

    settings_store.update_settings({"ansible": {
        "github_token": "ghp_pretendthisisreal", "github_enabled": True,
        "github_owner": "netops", "github_repo": "playbooks"}})

    on_disk = paths.settings_file().read_text(encoding="utf-8")
    check("the token is not in the file",
          "ghp_pretendthisisreal" not in on_disk,
          "settings.json is a plain file people are told to edit and attach "
          "to support requests")
    check("and the field it would have been in is blank",
          json.loads(on_disk).get("ansible", {}).get("github_token", "") == "",
          json.loads(on_disk).get("ansible", {}).get("github_token", ""))
    check("it is readable from the vault",
          vault.get("ansible_github_token", "") == "ghp_pretendthisisreal")

    ui = settings_store.get_settings_for_ui()
    check("the settings screen is told one exists",
          ui["ansible"]["has_github_token"] is True, str(ui["ansible"]))
    check("and is given a mask rather than the token",
          set(ui["ansible"]["github_token"]) == {"•"},
          ui["ansible"]["github_token"])

    # The mask coming back is what a scripted caller would send after a
    # GET. The frontend filters it; the backend has to as well, because
    # the API is scriptable and settings.json is a file people edit.
    settings_store.update_settings({"ansible": {"github_token": "•" * 8}})
    check("saving the mask back does not overwrite the real token",
          vault.get("ansible_github_token", "") == "ghp_pretendthisisreal",
          "opening settings and pressing save would have replaced a working "
          "token with a row of dots")

    settings_store.update_settings({"ansible": {"github_token": ""}})
    check("and an empty value does clear it",
          not vault.get("ansible_github_token", ""),
          "clearing has to still work, or the token can never be removed")


def nothing_is_public_by_accident() -> None:
    print("\n-- Private unless somebody says otherwise --")

    seen.clear()
    answers.clear()
    answers["POST /user/repos"] = (201, {
        "name": "playbooks", "private": True, "html_url": "https://x/y",
        "owner": {"login": "netops"}})

    vault.set("ansible_github_token", "ghp_token")
    ansible_git.create_repository("playbooks")
    sent = seen[-1]["body"]
    check("a create with nothing said asks for a private repository",
          sent["private"] is True, str(sent))
    check("and it initialises, so there is a branch to commit onto",
          sent.get("auto_init") is True, str(sent))

    ansible_git.create_repository("playbooks", private=False)
    check("public is only ever what was explicitly asked for",
          seen[-1]["body"]["private"] is False, str(seen[-1]["body"]))

    # An organisation is a different endpoint, not a field.
    answers["POST /orgs/acme/repos"] = (201, {
        "name": "playbooks", "private": True, "html_url": "https://x/y",
        "owner": {"login": "acme"}})
    ansible_git.create_repository("playbooks", org="acme")
    check("an organisation repository goes to the organisation's endpoint",
          seen[-1]["path"] == "/orgs/acme/repos", seen[-1]["path"])


def only_the_playbook_travels() -> None:
    print("\n-- The playbook, and nothing else --")

    seen.clear()
    answers.clear()
    answers["PUT /repos/netops/playbooks/contents/playbooks/site.yml"] = (201, {
        "content": {"html_url": "https://x/y/site.yml"},
        "commit": {"sha": "abcdef1234567890"}})

    result = ansible_git.commit_playbook("site.yml", PLAYBOOK,
                                         owner="netops", repo="playbooks")
    check("it commits", result["committed"] is True, str(result))
    check("as a new file when none was there",
          result["created"] is True, str(result))

    puts = [r for r in seen if r["method"] == "PUT"]
    check("exactly one file was sent", len(puts) == 1, str(len(puts)))
    import base64
    body = base64.b64decode(puts[0]["body"]["content"]).decode("utf-8")
    check("and it is the playbook, byte for byte", body == PLAYBOOK, repr(body))
    check("nothing that looks like an inventory went with it",
          not any("inventor" in r["path"].lower() for r in seen),
          "the inventory is the whole device list, and a repository can be "
          "made public later")
    check("the token travels as a bearer header, not in the path",
          puts[0]["auth"].startswith("Bearer ")
          and "ghp_" not in puts[0]["path"], puts[0]["path"])

    # A second save updates rather than creating, which needs the sha of
    # what is there — GitHub refuses the update otherwise rather than
    # overwriting, so this has to be asked for.
    answers["GET /repos/netops/playbooks/contents/playbooks/site.yml"] = (
        200, {"sha": "deadbeef"})
    result = ansible_git.commit_playbook("site.yml", PLAYBOOK,
                                         owner="netops", repo="playbooks")
    check("a second save updates rather than creating",
          result["created"] is False, str(result))
    check("carrying the sha of what was already there",
          seen[-1]["body"].get("sha") == "deadbeef", str(seen[-1]["body"]))


def a_failure_never_costs_the_save() -> None:
    print("\n-- A failure to reach GitHub still leaves the playbook saved --")

    from backend import ansible as ansible_module

    seen.clear()
    answers.clear()   # every path 404s from here

    saved = ansible_module.save_playbook("orphan.yml", PLAYBOOK)
    outcome = ansible_git.publish("orphan.yml", PLAYBOOK)
    check("publish reports the failure rather than raising",
          outcome["ok"] is False and outcome["why"], str(outcome))
    check("the playbook is still in the library",
          "orphan.yml" in [p["name"] for p in ansible_module.library()],
          "losing work because GitHub was unreachable would be an appalling "
          "trade for a feature about not losing work")
    check("and its contents are intact",
          ansible_module.read_playbook("orphan.yml") == PLAYBOOK)
    check("the saved answer said what it wrote", saved["name"] == "orphan.yml")

    # An unreachable host, as opposed to a refusal, is a different thing
    # to go and fix — and saying the wrong one sends somebody to
    # regenerate a token that was fine.
    was = ansible_git.API
    try:
        ansible_git.API = "http://127.0.0.1:1"
        outcome = ansible_git.publish("orphan.yml", PLAYBOOK)
        check("an unreachable GitHub is reported as unreachable",
              outcome["code"] == "unreachable", str(outcome))
    finally:
        ansible_git.API = was

    vault.delete("ansible_github_token")
    outcome = ansible_git.publish("orphan.yml", PLAYBOOK)
    check("and no token at all says so plainly",
          outcome["code"] == "no-token", str(outcome))


def pointing_at_one_that_exists() -> None:
    print("\n-- Using a repository that already exists --")

    vault.set("ansible_github_token", "ghp_token")
    seen.clear()
    answers.clear()
    answers["GET /repos/netops/existing"] = (200, {
        "name": "existing", "private": True, "html_url": "https://x/y",
        "owner": {"login": "netops"}, "permissions": {"push": True}})

    found = ansible_git.repository("netops", "existing")
    check("it is read before it is trusted",
          found["repo"] == "existing" and found["private"] is True, str(found))
    check("and no repository was created to do it",
          not any(r["method"] == "POST" for r in seen),
          "using an existing one must work with a token that cannot create "
          "repositories at all — that is the whole point of offering both")

    answers["GET /repos/netops/readonly"] = (200, {
        "name": "readonly", "private": True, "owner": {"login": "netops"},
        "permissions": {"push": False}})
    why = refuses(ansible_git.repository, "netops", "readonly")
    check("a repository the token cannot write to is refused now",
          "not write" in why or "write" in why, why)

    why = refuses(ansible_git.repository, "netops", "missing")
    check("and one that cannot be read does not claim it does not exist",
          "may not exist" in why and "access" in why,
          "404 is also what GitHub answers for a private repository the "
          "token cannot see")


if __name__ == "__main__":
    ansible_git.API = start_fake()

    the_token_never_lands_in_settings()
    nothing_is_public_by_accident()
    only_the_playbook_travels()
    a_failure_never_costs_the_save()
    pointing_at_one_that_exists()

    print("\n" + "=" * 52)
    print(f"  {passed} passed  |  {len(failed)} failed")
    print("=" * 52)
    for line in failed:
        print("  -", line)
    sys.exit(1 if failed else 0)
