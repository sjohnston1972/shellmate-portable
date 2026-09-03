"""
test_buffer.py — What zero means, on the way to a provider.

``ai.context_lines`` is documented as "zero sends none", and it is the setting
somebody chooses precisely to keep device output away from a cloud model. It
sent everything (#494): ``lines[-0:]`` is ``lines[0:]``, the whole buffer,
and the same slice shaped the command list. These check each place a limit
of zero is applied, and that a positive limit still works as it did.

    python test_buffer.py
"""

import shutil
import sys
import tempfile
from pathlib import Path

from backend import paths

_TEMP = Path(tempfile.mkdtemp(prefix="shellmate-buffer-"))
paths._data_dir_cache = _TEMP

from backend import advanced                                    # noqa: E402
from backend.ai.prompts import build_context_prompt             # noqa: E402
from backend.session import outbound                            # noqa: E402
from backend.session.buffer import SessionBuffer                # noqa: E402

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


def make_buffer() -> SessionBuffer:
    buffer = SessionBuffer("s1")
    buffer.write("line one\r\nline two\r\nline three\r\nsw1#")
    return buffer


def test_get_lines() -> None:
    print("\n-- The buffer --")
    buffer = make_buffer()
    check("zero lines is no lines", buffer.get_lines(0) == [], str(buffer.get_lines(0)))
    check("  and so is a negative count", buffer.get_lines(-5) == [])
    check("  and get_text follows", buffer.get_text(0) == "")
    check("two lines is the last two, the prompt included",
          buffer.get_lines(2) == ["line three", "sw1#"], str(buffer.get_lines(2)))
    check("more than there are is all of them",
          len(buffer.get_lines(200)) == 4 and buffer.line_count == 4)
    check("the empty buffer is still empty", SessionBuffer("s2").get_lines(0) == [])


def test_outbound_honours_zero() -> None:
    print("\n-- The one door out --")
    session = {"buffer": make_buffer()}
    check("session_text with zero lines sends nothing",
          outbound.session_text(session, 0) == "", repr(outbound.session_text(session, 0)))
    check("  and with a positive count sends that many",
          outbound.session_text(session, 1) == "sw1#")


def test_command_list_honours_zero() -> None:
    print("\n-- The command list --")
    history = ["show version", "show ip interface brief", "show run"]
    advanced.reset()
    try:
        advanced.update({"ai.context_commands": 0})
        prompt = build_context_prompt([], "out", "sw1", history)
        check("zero commands of history sends no command list",
              "Commands run this session" not in prompt and "show run" not in prompt, prompt)

        advanced.update({"ai.context_commands": 2})
        prompt = build_context_prompt([], "out", "sw1", history)
        check("two keeps the most recent two",
              "show run" in prompt and "show ip interface brief" in prompt
              and "show version" not in prompt, prompt)
    finally:
        advanced.reset()


def main() -> int:
    print("=" * 52)
    print("  Buffer limits")
    print("=" * 52)
    for test in (test_get_lines, test_outbound_honours_zero, test_command_list_honours_zero):
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
