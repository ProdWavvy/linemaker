"""'Busy' marker for the auto-updater: while the bot is answering or scanning, cache/busy
exists, and deploy/autoupdate.sh defers its restart to the next check (so an update never
cuts off a reply). A crash can't leave it stuck: the updater ignores a marker older than
10 minutes."""
import contextlib
import os
import threading
import time

_LOCK = threading.Lock()
_COUNT = [0]
_ACTIVE = {}   # id -> start time of each scan / answer in progress (see oldest_seconds)


def oldest_seconds(now: float = None) -> float:
    """How long the longest-running scan or answer has been going (0 if none). A worker stuck
    for good (11/3-11/4: no scan finished for two days while the bot stayed online) shows up
    here even though the main loop still answers."""
    now = now or time.time()
    with _LOCK:
        return max((now - t for t in _ACTIVE.values()), default=0.0)


def _path() -> str:
    return os.path.join(os.environ.get("LINEMAKER_CACHE_DIR", "cache"), "busy")


@contextlib.contextmanager
def busy():
    token = object()
    with _LOCK:
        _COUNT[0] += 1
        _ACTIVE[id(token)] = time.time()
        try:
            os.makedirs(os.path.dirname(_path()) or ".", exist_ok=True)
            with open(_path(), "w") as f:
                f.write(str(time.time()))
        except OSError:
            pass
    try:
        yield
    finally:
        with _LOCK:
            _ACTIVE.pop(id(token), None)
            _COUNT[0] -= 1
            if _COUNT[0] == 0:
                try:
                    os.remove(_path())
                except OSError:
                    pass
