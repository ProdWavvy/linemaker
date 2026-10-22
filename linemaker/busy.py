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


def _path() -> str:
    return os.path.join(os.environ.get("LINEMAKER_CACHE_DIR", "cache"), "busy")


@contextlib.contextmanager
def busy():
    with _LOCK:
        _COUNT[0] += 1
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
            _COUNT[0] -= 1
            if _COUNT[0] == 0:
                try:
                    os.remove(_path())
                except OSError:
                    pass
