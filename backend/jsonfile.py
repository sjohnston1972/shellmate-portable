"""
jsonfile.py — The one way a JSON data file is read and written (#457).

Seven files in the data folder — profiles, groups, credential sets, the
plaintext credentials, snippets, platforms, settings — were each written
with a truncating ``write_text`` and read back with ``except: return []``.
That pair loses everything in two ordinary ways:

- Two writers at once. "Connect all" on forty devices runs forty
  load → change → save cycles on profiles.json from worker threads. Two
  truncating writes interleave, the file is no longer JSON, the next
  reader gets ``[]``, and the next writer saves ``[]`` over five thousand
  connections. Even without interleaving, two writers each merge their
  own change over the same snapshot and one edit vanishes.
- A power cut mid-write, which leaves a truncated file with the same
  outcome.

Three rules, enforced here so no module has to remember them:

1. **Read-modify-write happens under a lock.** One re-entrant lock per
   path, taken by ``locked(path)`` around the whole cycle, not just the
   write. Re-entrant, because a save routinely calls a load.
2. **Writes are atomic.** The bytes go to a temp file beside the target
   and ``os.replace`` swaps it in, so a reader sees the old file or the
   new one and never a half-written one. The temp name is unique per
   writer, so two processes cannot truncate each other's temp file.
3. **A corrupt file is set aside, never silently replaced.** ``read``
   renames it to ``<name>.corrupt-<timestamp>`` and logs a warning; the
   caller gets the default and the evidence survives for support.
"""

import json
import logging
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()


def lock_for(path: Path) -> threading.RLock:
    """The lock for one file, created on first use."""
    key = str(path)
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = _locks[key] = threading.RLock()
        return lock


@contextmanager
def locked(path: Path):
    """Hold the file's lock around a load → change → save cycle."""
    with lock_for(path):
        yield


def read(path: Path, default: Any, expect: type | tuple[type, ...] | None = None) -> Any:
    """
    Parse the file, or return ``default`` when it is absent.

    A file that exists but does not parse — or parses to the wrong shape
    when ``expect`` is given — is renamed aside with a warning rather than
    treated as empty, so a later save cannot overwrite the only copy.
    """
    with lock_for(path):
        if not path.exists():
            return default
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("%s could not be read (%s); using defaults for now", path.name, exc)
            return default
        try:
            data = json.loads(text)
        except ValueError as exc:
            _set_aside(path, f"is not valid JSON ({exc})")
            return default
        if expect is not None and not isinstance(data, expect):
            _set_aside(path, f"holds a {type(data).__name__}, not the expected shape")
            return default
        return data


def write(path: Path, data: Any, *, indent: int | None = 2, mode: int | None = None) -> None:
    """
    Write atomically: temp file beside the target, then ``os.replace``.

    ``mode`` restricts the file's permissions (best effort; a no-op on
    Windows, where the folder's ACL is what protects it).
    """
    with lock_for(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        if indent is None:
            text = json.dumps(data, separators=(",", ":"))
        else:
            text = json.dumps(data, indent=indent)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            if mode is not None:
                try:
                    tmp.chmod(mode)
                except OSError:
                    pass
            os.replace(tmp, path)
        except BaseException:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise


def _set_aside(path: Path, why: str) -> None:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    aside = path.with_name(f"{path.name}.corrupt-{stamp}")
    try:
        os.replace(path, aside)
        logger.warning("%s %s; moved it to %s and starting from empty. "
                       "The original is there for recovery.", path.name, why, aside.name)
    except OSError as exc:
        logger.warning("%s %s and could not be moved aside (%s); using defaults for now",
                       path.name, why, exc)
