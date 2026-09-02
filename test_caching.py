"""
test_caching.py — An upgrade must not leave the old interface behind.

ShellMate is upgraded by replacing one executable. The window then talks to a
new backend with whatever frontend it had cached, and the symptoms are
arbitrary: a panel that is empty, a button that does nothing, a feature that
appears to have been deleted. It happened twice before anyone worked out what
it was, and the second time I was confidently telling the user a feature was
present while their screen showed it was not.

Nothing sent a `Cache-Control` header, so browsers fell back to heuristic
caching — reusing a response without asking. The mitigation that existed was
three hand-written `?v=2` markers on thirty-three script tags, which records
which files somebody remembered rather than which files change.

**Why no earlier test caught it.** Every browser check in this suite opens a
fresh context, so it always gets fresh assets. Catching a stale-cache bug
needs two loads in the *same* context with caching enabled, and a file changed
in between. That is what the Playwright half of this does.

The rest runs without a browser, so the important half still runs anywhere.

    python test_caching.py
"""

import re
import sys
import time
from pathlib import Path

passed = 0
failed: list[str] = []

ROOT = Path(__file__).parent


def check(name: str, condition: bool, detail: str = "") -> None:
    global passed
    if condition:
        passed += 1
        print(f"  OK  {name}")
    else:
        failed.append(f"{name}: {detail}")
        print(f"  FAIL {name}\n       {detail}")


def test_every_asset_carries_a_token() -> None:
    """
    Written into the response, not into the markup.

    The mechanism this replaces required remembering to bump a number when a
    file changed. It is the forgetting that has to be designed out, not the
    forgetfulness.
    """
    print("\n-- What the page asks for --")
    from fastapi.testclient import TestClient

    from backend.app import app

    client = TestClient(app, base_url="http://127.0.0.1")
    page = client.get("/")
    check("the page is served", page.status_code == 200)

    referenced = re.findall(r'(?:src|href)="(/static/[^"]+)"', page.text)
    check("it references a substantial number of assets", len(referenced) > 30,
          f"only {len(referenced)}")

    untokened = [r for r in referenced if "?b=" not in r]
    check("every one carries a build token", not untokened,
          f"{len(untokened)} without: {', '.join(untokened[:4])}")

    tokens = {r.split("?b=")[1].split("&")[0] for r in referenced}
    check("and they all carry the same one", len(tokens) == 1, str(tokens))

    # The markup itself must be clean, or there are two mechanisms and only
    # one of them works.
    source = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    leftovers = re.findall(r'/static/[^"]*\?v=\d+', source)
    check("no hand-written version markers remain", not leftovers,
          f"still there: {', '.join(leftovers)}")


def test_the_token_follows_the_files() -> None:
    """A token that does not change when a file does is decoration."""
    print("\n-- When the token changes --")
    from backend.app import _asset_token

    first = _asset_token()
    check("a token is produced", bool(first), "empty")

    scratch = ROOT / "frontend" / "_caching_test_marker.txt"
    try:
        scratch.write_text("touched by test_caching.py", encoding="utf-8")
        # Filesystem timestamps are coarse; make sure it lands in a later second.
        import os
        os.utime(scratch, (time.time() + 5, time.time() + 5))

        second = _asset_token()
        check("changing a frontend file changes it", second != first,
              f"{first} == {second} — an upgrade would not invalidate anything")
    finally:
        scratch.unlink(missing_ok=True)

    check("and it is stable when nothing changes",
          _asset_token() == _asset_token())


def test_nothing_is_served_without_cache_control() -> None:
    """
    The header whose absence caused this.

    With none at all a browser applies heuristic caching and may reuse a
    response without revalidating. `no-cache` does not mean "do not store" —
    it means "ask first", which over loopback is a stat and a 304.
    """
    print("\n-- What the server says about caching --")
    from fastapi.testclient import TestClient

    from backend.app import app

    client = TestClient(app, base_url="http://127.0.0.1")

    for path in ("/", "/static/js/stockton.js", "/static/css/style.css",
                 "/static/vendor/xterm.js", "/static/docs/legal.md"):
        response = client.get(path)
        header = response.headers.get("cache-control", "")
        check(f"{path} says how it may be cached", "no-cache" in header,
              f"got {header!r} — with no header the browser decides, and it "
              f"decides to keep it")

    # An API response is not a static asset and should not have been swept up.
    api = client.get("/api/system/info")
    check("API responses are left alone",
          "no-cache" not in api.headers.get("cache-control", ""),
          "the middleware is matching more than it should")


def test_a_second_load_picks_up_a_changed_file() -> None:
    """
    The one that needed a browser, and the one no earlier test could do.

    Two loads in the *same* context with caching enabled, with a file changed
    in between — which is exactly an upgrade, and exactly what every
    fresh-context check in this suite cannot see.
    """
    print("\n-- The same window, after an upgrade --")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("       playwright not installed — skipping the browser half")
        return

    import threading

    import uvicorn

    from backend.app import app

    port = 8821
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                           log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    time.sleep(3)

    probe = ROOT / "frontend" / "js" / "_caching_probe.js"
    marker = ROOT / "frontend" / "index.html"
    original = marker.read_text(encoding="utf-8")

    try:
        probe.write_text("window.__cachingProbe = 'first';\n", encoding="utf-8")
        marker.write_text(
            original.replace("</body>",
                             '  <script src="/static/js/_caching_probe.js"></script>\n</body>')
            if "</body>" in original else original, encoding="utf-8")

        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            # One context, one page, kept open across both loads — a window
            # that stays open across an upgrade, which is the real case.
            page = browser.new_page()
            page.goto(f"http://127.0.0.1:{port}/", wait_until="networkidle")
            time.sleep(1)
            first = page.evaluate("() => window.__cachingProbe")
            check("the probe script loaded", first == "first", repr(first))

            # The upgrade.
            probe.write_text("window.__cachingProbe = 'second';\n", encoding="utf-8")
            import os
            future = time.time() + 5
            os.utime(probe, (future, future))
            time.sleep(0.5)

            page.reload(wait_until="networkidle")
            time.sleep(1)
            second = page.evaluate("() => window.__cachingProbe")
            check("a reload in the same window picks up the new file",
                  second == "second",
                  f"got {second!r} — the window is running the old frontend "
                  f"against the new backend, which is the whole bug")
            browser.close()
    finally:
        marker.write_text(original, encoding="utf-8")
        probe.unlink(missing_ok=True)
        server.should_exit = True
        time.sleep(0.5)


def test_the_prompt_editor_is_visible_where_it_is_advertised() -> None:
    """
    Not "in the DOM" — visible.

    The system-prompt editor spent a while present, correctly built and
    invisible: parked inside the Settings panel, settings_nav.js indexed it as
    one of its sections and stamped class="hidden" on it whenever another
    category was selected, while Stockton cleared only the hidden *attribute*.
    Every check that asked "is it there" said yes throughout.

    This lives here rather than in a panel test because the mechanism is the
    same one this file is about: something that looks present and is not.
    """
    print(chr(10) + "-- The prompt editor, actually on screen --")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("       playwright not installed — skipping")
        return

    import threading

    import uvicorn

    from backend.app import app

    port = 8823
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                           log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    time.sleep(3)

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page(viewport={"width": 1500, "height": 1000})
            page.goto(f"http://127.0.0.1:{port}/", wait_until="networkidle")
            time.sleep(1.5)

            # The route a user is now told to take.
            page.eval_on_selector("#sidebar-link-settings", "e => e.click()")
            time.sleep(1.2)
            signpost = page.query_selector("#prompt-editor-signpost")
            check("Settings says where the editor is", signpost is not None,
                  "the section people open while looking for it says nothing")

            page.eval_on_selector("#open-prompt-editor", "e => e.click()")
            time.sleep(1.8)

            # It lives inside the AI section, which shows one at a time.
            # Found by key rather than by title: the heading was renamed to
            # disambiguate it from the hand-written AI Providers section
            # (#151), and a test that hardcodes a title breaks on a rename
            # while telling you the editor is missing.
            page.evaluate("""() => {
              const el = document.getElementById('prompt-editor-block');
              const section = el && el.closest('.settings-section:not(#prompt-editor-block)');
              const title = section && section.querySelector('.settings-section-title');
              if (title) window.openSettingsSection(title.textContent.trim());
            }""")
            time.sleep(1.2)

            # Stockton is gone (#135): the advanced sections live in Settings,
            # so the signpost scrolls rather than opening a second panel. The
            # guard below is the one that matters and is unchanged.
            active = page.query_selector(".settings-nav-item.active")
            check("Settings is open on a section",
                  active is not None,
                  "nothing active")

            block = page.query_selector("#prompt-editor-block")
            check("the editor is on screen", bool(block) and block.is_visible(),
                  "present in the DOM and not visible is the exact failure "
                  "this test exists for")

            area = page.query_selector("#prompt-editor-block textarea")
            check("with a prompt loaded in it",
                  bool(area) and len(area.input_value()) > 500,
                  f"{len(area.input_value()) if area else 0} characters")

            modes = page.eval_on_selector(
                "#prompt-mode-select", "e => [...e.options].map(o => o.value)")
            check("and both personas offered", set(modes) == {"tshoot", "learn"},
                  str(modes))

            browser.close()
    finally:
        server.should_exit = True
        time.sleep(0.5)


def main() -> int:
    print("\n" + "=" * 52)
    print("  Caching")
    print("=" * 52)

    for test in (test_every_asset_carries_a_token,
                 test_the_token_follows_the_files,
                 test_nothing_is_served_without_cache_control,
                 test_a_second_load_picks_up_a_changed_file,
                 test_the_prompt_editor_is_visible_where_it_is_advertised):
        try:
            test()
        except Exception as exc:
            failed.append(f"{test.__name__}: raised {type(exc).__name__}: {exc}")
            print(f"  FAIL {test.__name__} raised {type(exc).__name__}: {exc}")

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
