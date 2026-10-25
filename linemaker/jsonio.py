"""Safe JSON files: writes never leave a half-written file behind, reads ride out a race.

save_json writes to a temp file unique to this process and thread, then swaps it in with
os.replace (atomic), so a reader sees the old file or the new one, never a partial one.
load_json retries a few times on a decode error (a file written by an older, non-atomic
writer or another program), then raises.
path_lock gives one lock per file so read-modify-write updates inside the bot don't
overwrite each other (e.g. a scan saving tracking while live grading adds results).
"""
import json
import logging
import os
import threading
import time

_LOCKS = {}
_GUARD = threading.Lock()


def path_lock(path: str) -> threading.RLock:
    key = os.path.abspath(path)
    with _GUARD:
        return _LOCKS.setdefault(key, threading.RLock())


def save_json(path: str, data, **kw):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(data, f, **kw)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def load_json(path: str, tries: int = 4):
    for i in range(tries):
        try:
            with open(path) as f:
                return json.load(f)
        except json.JSONDecodeError:
            if i == tries - 1:
                raise
            time.sleep(0.5 * (i + 1))


def load_or_quarantine(path: str) -> dict:
    """A day's records, or {} after moving an unreadable file aside (kept as .corrupt-<time>
    for recovery) so one bad write can't break every scan for the rest of the day."""
    try:
        return load_json(path)
    except json.JSONDecodeError as e:
        aside = f"{path}.corrupt-{int(time.time())}"
        os.replace(path, aside)
        logging.getLogger("linemaker.jsonio").error("%s was unreadable (%s); moved to %s, starting fresh",
                                                    path, e, aside)
        return {}
