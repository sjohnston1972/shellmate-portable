"""
test_ticketing.py — Jira, configured from Settings rather than .env (#540).

The old arrangement bound four module constants when `app.py` imported, so
configuring Jira meant editing a file beside the executable and restarting —
which for a portable build closes every live session. The issue names that
as the single biggest reason the feature went unused.

Three properties, and the second is the one worth the file:

**The token never reaches settings.json.** It is a credential like any
other, and settings.json is a plain file that a support bundle can pick up.

**The mask is never sent as a token.** The panel shows dots when a token is
stored, so somebody changing a project key posts those dots back. Storing
eight bullet characters as the token is a failure that arrives much later,
as "Jira rejected ShellMate", with nothing on screen connecting the two —
the same trap the Ansible runner token already has a guard for.

**.env still works.** Anybody already running with JIRA_* variables must not
have the feature go dark the moment this ships.

    python test_ticketing.py
"""

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from backend import paths

_TEMP = Path(tempfile.mkdtemp(prefix="shellmate-ticketing-"))
paths._data_dir_cache = _TEMP

from backend import jira_client, settings_store             # noqa: E402
from backend.vault import vault                             # noqa: E402

passed = 0
failed: list[str] = []

MASK = "•" * 8
TOKEN = "atlassian-api-token-abc123"


def check(name: str, condition: bool, detail: str = "") -> None:
    global passed
    if condition:
        passed += 1
        print(f"  OK  {name}")
    else:
        failed.append(f"{name}: {detail}")
        print(f"  FAIL {name}\n       {detail}")


def settings_json() -> dict:
    """What is actually on disk, not what the API hands back."""
    path = paths.settings_file()
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def clear_env() -> None:
    for name in ("JIRA_URL", "JIRA_USER_EMAIL", "JIRA_API_TOKEN",
                 "JIRA_PROJECT_KEY"):
        os.environ.pop(name, None)


# ---------------------------------------------------------------------------

def test_the_block_exists_with_no_secret_in_it() -> None:
    print("\n-- The default --")
    block = settings_store.get_settings().get("ticketing")
    check("there is a ticketing section", isinstance(block, dict), str(block))
    check("it has the four fields",
          set(block or {}) >= {"jira_url", "jira_email", "jira_project_key",
                               "jira_api_token"},
          str(sorted(block or {})))
    check("and every one of them starts empty",
          not any((block or {}).values()), str(block))


def test_the_token_goes_to_the_vault_and_not_to_disk() -> None:
    print("\n-- Where the token lives --")
    clear_env()
    settings_store.update_settings({"ticketing": {
        "jira_url": "https://acme.atlassian.net",
        "jira_email": "you@example.com",
        "jira_project_key": "NET",
        "jira_api_token": TOKEN,
    }})

    on_disk = settings_json().get("ticketing", {})
    check("the URL is written in the clear",
          on_disk.get("jira_url") == "https://acme.atlassian.net",
          str(on_disk))
    check("the token is not in settings.json",
          not on_disk.get("jira_api_token"),
          f"settings.json carries {on_disk.get('jira_api_token')!r}")
    check("the whole file contains the token nowhere",
          TOKEN not in json.dumps(settings_json()),
          "the token reached disk through some other key")
    check("the vault has it", vault.get("jira_api_token", "") == TOKEN)


def test_the_panel_is_told_it_exists_but_not_what_it_is() -> None:
    print("\n-- What the panel is handed --")
    clear_env()
    ui = settings_store.get_settings_for_ui().get("ticketing", {})
    check("the panel is told a token is stored",
          ui.get("has_jira_token") is True, str(ui))
    check("but is handed the mask, not the token",
          ui.get("jira_api_token") == MASK,
          f"the panel received {ui.get('jira_api_token')!r}")
    check("the non-secret fields come back as themselves",
          ui.get("jira_project_key") == "NET", str(ui))


def test_resolution_order() -> None:
    print("\n-- Where the values come from --")
    clear_env()
    jira = jira_client.settings()
    check("settings win when they are set",
          jira.url == "https://acme.atlassian.net" and jira.project == "NET",
          f"{jira.url!r} / {jira.project!r}")
    check("the token comes out of the vault", jira.token == TOKEN)
    check("and it reports itself ready", jira.ready is True)

    # The environment is the fallback, not the winner: somebody who has set
    # a project key in the panel has said which project they mean.
    os.environ["JIRA_PROJECT_KEY"] = "OTHER"
    check("the environment does not override a stored value",
          jira_client.settings().project == "NET",
          jira_client.settings().project)
    clear_env()


def test_dot_env_still_works_on_its_own() -> None:
    """The people already using JIRA_* must not have this go dark."""
    print("\n-- The .env fallback --")
    settings_store.update_settings({"ticketing": {
        "jira_url": "", "jira_email": "", "jira_project_key": "",
    }})
    vault.delete("jira_api_token")
    clear_env()

    check("with nothing anywhere it is not ready",
          jira_client.settings().ready is False)

    os.environ["JIRA_URL"] = "https://legacy.atlassian.net/"
    os.environ["JIRA_USER_EMAIL"] = "old@example.com"
    os.environ["JIRA_API_TOKEN"] = "env-token"
    os.environ["JIRA_PROJECT_KEY"] = "OPS"

    jira = jira_client.settings()
    check("the environment alone configures it", jira.ready is True)
    check("the token comes from the environment", jira.token == "env-token")
    check("a trailing slash is trimmed once, here",
          jira.url == "https://legacy.atlassian.net", jira.url)
    check("and browse() does not double it",
          jira.browse("OPS-1") == "https://legacy.atlassian.net/browse/OPS-1",
          jira.browse("OPS-1"))
    clear_env()


def test_the_mask_is_never_stored_as_a_token() -> None:
    """
    The trap this shares with the Ansible runner token.

    The panel shows dots when a token is stored. Somebody changing a project
    key posts those dots back, and without a guard they become the token.
    The failure surfaces much later as "Jira rejected ShellMate", with
    nothing on screen connecting it to the edit that caused it.
    """
    print("\n-- The mask --")
    clear_env()
    settings_store.update_settings({"ticketing": {
        "jira_url": "https://acme.atlassian.net",
        "jira_email": "you@example.com",
        "jira_project_key": "NET",
        "jira_api_token": TOKEN,
    }})
    # What the browser would post if the guard in settings.js were removed.
    settings_store.update_settings({"ticketing": {
        "jira_project_key": "NEW", "jira_api_token": MASK,
    }})

    resolved = jira_client.settings()
    check("the project key changed", resolved.project == "NEW", resolved.project)
    check("the resolver refuses a value that is only bullets",
          resolved.token != MASK,
          "eight bullet characters would be sent to Jira as a token")

    # Belt and braces on the same trap from the other side: a mask that did
    # reach the vault must still not be handed out as a credential.
    vault.set("jira_api_token", MASK)
    check("a mask already in the vault is not returned either",
          jira_client.settings().token != MASK,
          "a stored mask was resolved as the token")

    vault.set("jira_api_token", TOKEN)
    check("a real token still resolves",
          jira_client.settings().token == TOKEN)


def test_partial_configuration_is_not_ready() -> None:
    print("\n-- Half-configured --")
    clear_env()
    settings_store.update_settings({"ticketing": {
        "jira_url": "https://acme.atlassian.net",
        "jira_email": "", "jira_project_key": "NET",
    }})
    check("a missing e-mail means not ready",
          jira_client.settings().ready is False,
          "Jira Cloud authenticates with the e-mail and token together")


def main() -> int:
    print("=" * 52)
    print("  Ticketing — Jira out of .env")
    print("=" * 52)

    for test in (
        test_the_block_exists_with_no_secret_in_it,
        test_the_token_goes_to_the_vault_and_not_to_disk,
        test_the_panel_is_told_it_exists_but_not_what_it_is,
        test_resolution_order,
        test_dot_env_still_works_on_its_own,
        test_the_mask_is_never_stored_as_a_token,
        test_partial_configuration_is_not_ready,
    ):
        try:
            test()
        except Exception as exc:
            failed.append(f"{test.__name__}: raised {type(exc).__name__}: {exc}")
            print(f"  FAIL {test.__name__} raised {type(exc).__name__}: {exc}")

    clear_env()
    shutil.rmtree(_TEMP, ignore_errors=True)

    print("\n" + "=" * 52)
    print(f"  {passed} passed  |  {len(failed)} failed")
    print("=" * 52)
    if failed:
        print("\nFAILURES:")
        for item in failed:
            print(f"  {item}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
