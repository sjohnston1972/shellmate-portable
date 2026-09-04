"""
test_updater.py — The download is verified, bounded, licensed and honest.

Offline: a fake release served by a mocked transport. The checks are the
ones that matter for code that replaces the executable — a checksum
mismatch deletes the file and says so, a missing checksum refuses, the
download is refused without a licence, the helper script does the swap in
the right order with a rollback, and applying is refused while a device has
something pending.

    python test_updater.py
"""

import hashlib
import shutil
import sys
import tempfile
import time
from pathlib import Path

from backend import paths

_TEMP = Path(tempfile.mkdtemp(prefix="shellmate-updater-"))
paths._data_dir_cache = _TEMP

import httpx                                                             # noqa: E402

from backend import licence, updater                                     # noqa: E402

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


EXE_BYTES = b"MZ" + bytes(range(256)) * 64
GOOD_SHA = hashlib.sha256(EXE_BYTES).hexdigest()


def fake_transport(sha: str | None, size: int | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/releases?" in url:
            stable = {"tag_name": "v9.9.9", "html_url": "https://example.test/rel", "body": "notes",
                      "published_at": "2026-09-03T00:00:00Z", "prerelease": False, "draft": False,
                      "assets": [{"name": "ShellMate-Portable.exe", "size": len(EXE_BYTES),
                                  "browser_download_url": "https://example.test/dl/ShellMate-Portable.exe"},
                                 {"name": "ShellMate-Portable.exe.sha256", "size": 80,
                                  "browser_download_url": "https://example.test/dl/ShellMate-Portable.exe.sha256"}]}
            beta = dict(stable, tag_name="v10.0.0-beta.2", prerelease=True)
            older_beta = dict(stable, tag_name="v10.0.0-beta.1", prerelease=True)
            draft = dict(stable, tag_name="v11.0.0", draft=True)
            return httpx.Response(200, json=[older_beta, draft, stable, beta])
        if url.endswith("/releases/latest"):
            assets = [{"name": "ShellMate-Portable.exe", "size": size if size is not None else len(EXE_BYTES),
                       "browser_download_url": "https://example.test/dl/ShellMate-Portable.exe"}]
            if sha is not None:
                assets.append({"name": "ShellMate-Portable.exe.sha256", "size": 80,
                               "browser_download_url": "https://example.test/dl/ShellMate-Portable.exe.sha256"})
            return httpx.Response(200, json={"tag_name": "v9.9.9", "html_url": "https://example.test/rel",
                                             "body": "notes", "published_at": "2026-09-03T00:00:00Z", "assets": assets})
        if url.endswith(".sha256"):
            return httpx.Response(200, text=f"{sha}  ShellMate-Portable.exe\n")
        if url.endswith("ShellMate-Portable.exe"):
            transport.downloads += 1
            return httpx.Response(200, content=EXE_BYTES)
        return httpx.Response(404)
    transport = httpx.MockTransport(handler)
    transport.downloads = 0
    return transport


class PatchedClient(httpx.Client):
    transport = None

    def __init__(self, *a, **kw):
        kw["transport"] = PatchedClient.transport
        super().__init__(*a, **kw)


def wait_for_phase(*phases, timeout=10.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = updater.state()
        if s["phase"] in phases:
            return s
        time.sleep(0.05)
    return updater.state()


def with_licence(valid: bool):
    updater.licence.has_feature = lambda name="updates": valid


def test_download() -> None:
    print("\n-- Download and verify --")
    real_client = httpx.Client
    real_feature = licence.has_feature
    httpx.Client = PatchedClient
    try:
        with_licence(False)
        check("refused without a licence", _raises(lambda: updater.start_download("x/y"), PermissionError))

        with_licence(True)
        PatchedClient.transport = fake_transport(GOOD_SHA)
        updater.start_download("x/y")
        s = wait_for_phase("ready", "failed")
        check("a good release downloads and verifies", s["phase"] == "ready", str(s))
        check("  the file is where the state says", s["path"] and Path(s["path"]).read_bytes() == EXE_BYTES)
        check("  and the version came from the tag", s["version"] == "9.9.9", s["version"])

        updater._set(phase="idle")
        updater.start_download("x/y")
        s = wait_for_phase("ready", "failed")
        check("asking again does not download again — the verified file is reused (#450)",
              s["phase"] == "ready" and PatchedClient.transport.downloads == 1, f"{s['phase']} downloads={PatchedClient.transport.downloads}")

        PatchedClient.transport = fake_transport("0" * 64)
        updater._set(phase="idle")
        updater.start_download("x/y")
        s = wait_for_phase("ready", "failed")
        check("a checksum mismatch fails and says so", s["phase"] == "failed" and "not what the release says" in s["error"], str(s))
        check("  and leaves no partial file", not list(updater.updates_dir().glob("*.part")))

        PatchedClient.transport = fake_transport(None)
        updater._set(phase="idle")
        updater.start_download("x/y")
        s = wait_for_phase("ready", "failed")
        check("a release with no checksum is refused", s["phase"] == "failed" and "no checksum" in s["error"], str(s))

        PatchedClient.transport = fake_transport(GOOD_SHA, size=10)
        updater._set(phase="idle")
        updater.start_download("x/y")
        s = wait_for_phase("ready", "failed")
        check("a stream larger than the release says is abandoned",
              s["phase"] == "failed" and "larger" in s["error"], str(s))
    finally:
        httpx.Client = real_client
        updater.licence.has_feature = real_feature


def test_channels_and_versions() -> None:
    print("\n-- Channels and prerelease versions (#567) --")
    from backend import version as v
    check("a prerelease sorts before its release", v.is_newer("1.2.0", "1.2.0-beta.1") and not v.is_newer("1.2.0-beta.1", "1.2.0"))
    check("betas order numerically", v.is_newer("1.2.0-beta.2", "1.2.0-beta.1") and not v.is_newer("1.2.0-beta.10", "1.2.0-beta.11"))
    check("rc comes after beta", v.is_newer("1.2.0-rc.1", "1.2.0-beta.9"))
    check("a prerelease of the next version is newer than this release", v.is_newer("1.2.0-beta.1", "1.1.3"))
    check("plain numbers still compare as before", v.is_newer("v1.10.0", "1.9.9") and v.parse("v1.2.3") == (1, 2, 3))
    check("build metadata never orders", not v.is_newer("1.2.0+build.7", "1.2.0"))
    check("is_prerelease says which is which", v.is_prerelease("1.2.0-beta.1") and not v.is_prerelease("1.2.0"))

    real_client = httpx.Client
    httpx.Client = PatchedClient
    try:
        PatchedClient.transport = fake_transport(GOOD_SHA)
        stable = updater.latest_release("x/y", "stable")
        beta = updater.latest_release("x/y", "beta")
        check("stable answers GitHub's latest", stable["version"] == "9.9.9" and not stable["prerelease"], str(stable)[:120])
        check("beta answers the highest version, prereleases included, drafts excluded",
              beta["version"] == "10.0.0-beta.2" and beta["prerelease"] and beta["channel"] == "beta", str(beta)[:120])
        check("the default channel is stable", updater.channel() == "stable")
    finally:
        httpx.Client = real_client


def test_helper_and_apply() -> None:
    print("\n-- The swap --")
    script = updater.helper_script(Path(r"C:\x\ShellMate-Portable.exe"), Path(r"C:\x\new.exe"), 8765, 4242)
    check("the helper waits for the process to go", "set PID=4242" in script and "PID eq %PID%" in script and ":wait" in script)
    check("  moves the old aside before the new in",
          script.index("%CURRENT%\" \"%OLD%\"") < script.index("%FRESH%\" \"%CURRENT%\""))
    check("  starts the new copy and checks its port", "--updated" in script and "8765/api/health" in script)
    check("  and puts the old one back if it does not answer", "Putting the previous one back" in script
          and script.index("Putting the previous") > script.index("--updated"))
    check("  never uses timeout, which needs a console the helper has not got (#450)",
          "timeout /t" not in script and "ping -n" in script)
    check("  retries the move instead of trying once", ":aside" in script and "tries%% lss 30" in script.replace("%", "%%"))
    check("  and writes a verdict the next start can read", 'echo OK:' in script and 'echo FAILED:' in script)
    both = updater.helper_script(Path(r"C:\x\ShellMate-Portable.exe"), Path(r"C:\x\new.exe"), 8765, 4242, 4200)
    check("  waits for the bootloader parent too when given one", 'PID eq 4200' in both and 'set PID=4242' in both)

    class Manager:
        def __init__(self, pending): self.pending = pending
        def get_all_sessions(self): return [{"session_id": "s1", "display_label": "core-1"}]
        def get_session(self, sid):
            class T:
                def __init__(s, p): s.p = p
                def payload(s): return {"pending": s.p}
            return {"alerts": T(self.pending)}

    check("a pending reload blocks the apply",
          updater.blockers(Manager({"kind": "reload"})) == ["core-1"])
    check("nothing pending, nothing blocking", updater.blockers(Manager(None)) == [])

    real_feature = updater.licence.has_feature
    updater.licence.has_feature = lambda name="updates": False
    try:
        check("apply is refused without a licence", _raises(lambda: updater.apply(Manager(None), 8765), PermissionError))
    finally:
        updater.licence.has_feature = real_feature
    updater._set(phase="idle", path="")
    updater.licence.has_feature = lambda name="updates": True
    try:
        check("apply is refused with nothing downloaded", _raises(lambda: updater.apply(Manager(None), 8765), RuntimeError))
    finally:
        updater.licence.has_feature = real_feature
    check("a source checkout has nothing to tidy", updater.tidy_after_launch() == "")


def _raises(fn, kind) -> bool:
    try:
        fn()
    except kind:
        return True
    except Exception:
        return False
    return False


def main() -> int:
    print("=" * 52)
    print("  Updater")
    print("=" * 52)
    for test in (test_download, test_channels_and_versions, test_helper_and_apply):
        try:
            test()
        except Exception as exc:
            failed.append(f"{test.__name__}: raised {type(exc).__name__}: {exc}")
            print(f"  FAIL {test.__name__} raised {type(exc).__name__}: {exc}")
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
