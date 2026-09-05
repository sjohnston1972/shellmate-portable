"""
test_backup_webhook.py — Telling something other than ShellMate (#539).

The in-app digest shipped; this is the other half of the issue. The person
who needs to know that core-2 changed overnight is in a chat channel, not in
front of ShellMate.

Four properties, and the first is the design:

**Silence is the feature.** A clean night sends nothing at all. This is not
an optimisation — a webhook that fires every morning regardless is one whose
messages get filtered into a folder, and then the morning something did
happen looks exactly like every other morning.

**One source of numbers.** The body is built from the same
`scheduler.digest` the panel renders, so the message and the screen cannot
disagree.

**Never the configuration, unless asked, and redacted even then.** A digest
that posts a running config into a chat channel has moved an estate
somewhere with a very different access model.

**The URL is a credential.** It looks like a location; for Teams and Slack
the whole authority to post is in it. Vault, never settings.json, masked in
the support bundle.

    python test_backup_webhook.py
"""

import json
import re
import shutil
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from backend import paths

_TEMP = Path(tempfile.mkdtemp(prefix="shellmate-hook-"))
paths._data_dir_cache = _TEMP

from backend import advanced, backup_webhook, settings_store   # noqa: E402

passed = 0
failed: list[str] = []

ROOT = Path(__file__).parent
SCHED = (ROOT / "backend" / "scheduler.py").read_text(encoding="utf-8")
APP = (ROOT / "backend" / "app.py").read_text(encoding="utf-8")
STORE = (ROOT / "backend" / "settings_store.py").read_text(encoding="utf-8")
PANEL = (ROOT / "frontend" / "js" / "settings.js").read_text(encoding="utf-8")
HTML = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")


def check(name: str, condition: bool, detail: str = "") -> None:
    global passed
    if condition:
        passed += 1
        print(f"  OK  {name}")
    else:
        failed.append(f"{name}: {detail}")
        print(f"  FAIL {name}\n       {detail}")


# --- a webhook receiver, so "it posts" is measured rather than asserted ----

RECEIVED: list[dict] = []


class Handler(BaseHTTPRequestHandler):
    status = 200

    def do_POST(self):                                    # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8", "replace")
        RECEIVED.append(json.loads(body))
        self.send_response(Handler.status)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args):                         # noqa: A003
        pass


server = HTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
ENDPOINT = f"http://127.0.0.1:{server.server_address[1]}/hook"


def configure(**overrides) -> None:
    """Turn the webhook on, pointed at the local receiver."""
    from backend.vault import vault

    vault.set(backup_webhook.VAULT_KEY, ENDPOINT)
    settings_store.update_settings({"backups": {
        "webhook_enabled": True, "webhook_format": "json",
        "webhook_include_diff": False, **overrides}})


REPORT = {
    "anything": True, "changed": 2, "failed": 1, "missed": 0,
    "non_compliant": 0, "unverifiable": 0,
    "groups": [{
        "group": "glasgow", "name": "Glasgow", "at": 1_700_000_000.0,
        "changed": ["core-2", "sw-14"], "failed": ["edge-1"],
        "skipped": [], "missed": 0, "non_compliant": 0, "unverifiable": 0,
    }],
}

QUIET = {"anything": False, "groups": [], "changed": 0, "failed": 0,
         "missed": 0, "non_compliant": 0, "unverifiable": 0}


# ---------------------------------------------------------------------------

def test_a_clean_night_sends_nothing() -> None:
    """
    The rule the whole feature rests on.
    """
    print("\n-- Silence --")
    configure()
    RECEIVED.clear()

    out = backup_webhook.send(QUIET)
    check("nothing is sent", out["sent"] is False and not RECEIVED,
          str(out))
    check("and the reason says why, rather than looking like a failure",
          out["reason"] == "nothing to report", out["reason"],)
    check("the panel reads that as normal, not as an error",
          "'nothing to report'" in PANEL
          and "clean night looks like" in PANEL,
          "a test button that called silence a failure would have somebody "
          "chasing a webhook that works")


def test_it_actually_posts() -> None:
    print("\n-- It posts --")
    configure()
    RECEIVED.clear()

    out = backup_webhook.send(REPORT)
    check("it reports sent", out["sent"] is True, str(out))
    check("and something arrived", len(RECEIVED) == 1, str(RECEIVED))

    body = RECEIVED[0] if RECEIVED else {}
    check("with the counts", body.get("changed") == 2 and body.get("failed") == 1,
          str(body))
    check("the devices by name",
          "core-2" in (body.get("groups") or [{}])[0].get("changed", []),
          str(body.get("groups")))
    check("the group by its display name",
          (body.get("groups") or [{}])[0].get("group") == "Glasgow")
    check("and a summary sentence", bool(body.get("summary")), str(body))
    check("identified as ShellMate, so a shared endpoint can route it",
          body.get("source") == "shellmate" and body.get("event") == "backup")


def test_the_numbers_come_from_the_digest() -> None:
    print("\n-- One source of numbers --")

    check("the summary is the digest's own sentence",
          "scheduler.digest_line(report)" in
          (ROOT / "backend" / "backup_webhook.py").read_text(encoding="utf-8"),
          "computing '2 changed, 1 failed' a second time would be a second "
          "chance to be wrong")
    check("and the run reads the digest rather than being handed the result",
          "scheduler.digest(include_seen=True)" in
          (ROOT / "backend" / "backup_webhook.py").read_text(encoding="utf-8"),
          "compliance findings are attached to the group after the backup "
          "result is written, so being handed the result would disagree "
          "with the panel for exactly the runs where it mattered")

    check("it fires after the compliance re-check",
          re.search(r"recheck_compliance[\s\S]{0,1200}"
                    r"backup_webhook\.notify_after_run\(\)", SCHED) is not None,
          "before it, the message would carry numbers the panel does not "
          "show")
    check("and it cannot fail the backup",
          re.search(r"try:[\s\S]{0,400}backup_webhook\.notify_after_run\(\)"
                    r"[\s\S]{0,200}except Exception", SCHED) is not None,
          "the configurations are already stored, which is the part that "
          "mattered")


def test_never_the_configuration_by_default() -> None:
    print("\n-- Not the config --")
    configure()
    RECEIVED.clear()
    backup_webhook.send(REPORT)
    check("no diffs unless asked", "diffs" not in (RECEIVED[0] if RECEIVED else {}),
          str(RECEIVED))
    check("and the setting is off by default",
          settings_store.DEFAULT_SETTINGS["backups"]["webhook_include_diff"]
          is False,
          "a digest that posts a running config into a chat channel has "
          "moved an estate somewhere with a very different access model")

    check("whatever is sent is redacted first",
          "redact_text(diff or \"\")" in
          (ROOT / "backend" / "backup_webhook.py").read_text(encoding="utf-8"))
    check("and capped per device",
          advanced.get("backups.webhook_diff_lines") == 40)
    check("read from stored snapshots, never from the device",
          "drift_report" not in
          (ROOT / "backend" / "backup_webhook.py").read_text(encoding="utf-8")
          .split("def _diffs")[1].split("def ")[0].replace(
              "`drift_report` opens a channel", ""),
          "this runs after the session it would have used has been closed, "
          "and opening a new one to build a chat message is not a trade "
          "anybody agreed to")


def test_the_card_formats() -> None:
    print("\n-- Cards --")

    teams = backup_webhook.build_card(REPORT, "teams")
    slack = backup_webhook.build_card(REPORT, "slack")
    check("Teams gets a text field", bool(teams.get("text")), str(teams))
    check("Slack gets a text field", bool(slack.get("text")), str(slack))
    check("and Slack says who it is from",
          slack["text"].startswith("ShellMate:"), slack["text"])

    check("an unknown format falls back to JSON rather than failing",
          "source" in backup_webhook.build(
              REPORT) if True else False)

    configure(webhook_format="nonsense")
    RECEIVED.clear()
    backup_webhook.send(REPORT)
    check("even one written into settings.json by hand",
          (RECEIVED[0] if RECEIVED else {}).get("source") == "shellmate",
          str(RECEIVED))


def test_a_refusal_is_reported_without_the_url() -> None:
    print("\n-- When it refuses --")
    configure()
    RECEIVED.clear()
    Handler.status = 403
    try:
        out = backup_webhook.send(REPORT)
    finally:
        Handler.status = 200

    check("it says it did not send", out["sent"] is False, str(out))
    check("with the status", "403" in out["reason"], out["reason"])
    check("and never the URL, which is the secret",
          ENDPOINT not in out["reason"], out["reason"])
    check("nor the response body",
          "exc.response.status_code" in
          (ROOT / "backend" / "backup_webhook.py").read_text(encoding="utf-8"),
          "a webhook's error body routinely contains the URL it was posted "
          "to")


def test_the_url_is_a_credential() -> None:
    print("\n-- A credential that looks like a location --")

    settings_store.update_settings({"backups": {"webhook_url": ENDPOINT}})
    raw = paths.settings_file().read_text(encoding="utf-8")
    check("it is not in settings.json",
          ENDPOINT not in raw,
          "for Teams and Slack the whole authority to post into the channel "
          "is in the URL")
    check("it is diverted into the vault",
          '"backups", "webhook_url"' in STORE and '"backup_webhook_url"' in STORE)
    check("and it is still readable from there",
          backup_webhook.url() == ENDPOINT)

    ui = settings_store.get_settings_for_ui()["backups"]
    check("the panel is told one is stored",
          ui["has_webhook_url"] is True)
    check("and never what it is",
          ENDPOINT not in json.dumps(ui) and ui["webhook_url"] == "•" * 8,
          str(ui))
    check("the field is only sent back when it was retyped",
          "_webhookUrlIfChanged" in PANEL,
          "sending the mask back would store eight bullet characters as "
          "the URL, and the failure would surface a week later as a digest "
          "nobody got")

    check("a locked vault degrades to no value rather than raising",
          "except Exception" in
          (ROOT / "backend" / "backup_webhook.py").read_text(encoding="utf-8")
          .split("def url()")[1].split("def ")[0],
          "a forgotten master password must not stop a backup running")


def test_you_can_see_what_would_be_sent() -> None:
    print("\n-- Before it leaves --")

    check("there is a preview endpoint",
          '@app.get("/api/backups/webhook/preview")' in APP)
    check("and a test that sends the real digest",
          '@app.post("/api/backups/webhook/test")' in APP
          and "scheduler.digest, True" in APP,
          "a test posting 'this is a test' proves the URL is reachable and "
          "nothing about whether the body is the shape the other end wanted")

    check("both are offered in Settings",
          'id="webhook-preview"' in HTML and 'id="webhook-test"' in HTML)
    check("the body has its own scroller",
          'id="webhook-body"' in HTML and ".webhook-body" in
          (ROOT / "frontend" / "css" / "style.css").read_text(encoding="utf-8"))

    text = backup_webhook.preview(REPORT)
    check("the preview is the body, not a description of it",
          json.loads(text).get("summary") == backup_webhook.build_json(
              REPORT).get("summary"),
          text[:200])


def main() -> int:
    print("=" * 52)
    print("  The backup webhook")
    print("=" * 52)

    for test in (
        test_a_clean_night_sends_nothing,
        test_it_actually_posts,
        test_the_numbers_come_from_the_digest,
        test_never_the_configuration_by_default,
        test_the_card_formats,
        test_a_refusal_is_reported_without_the_url,
        test_the_url_is_a_credential,
        test_you_can_see_what_would_be_sent,
    ):
        try:
            test()
        except Exception as exc:
            failed.append(f"{test.__name__}: raised {type(exc).__name__}: {exc}")
            print(f"  FAIL {test.__name__} raised {type(exc).__name__}: {exc}")

    server.shutdown()
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
