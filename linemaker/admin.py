"""Remote admin from Discord (owner only): settings, restart, logs, status.

Who counts as the owner: LINEMAKER_OWNER_IDS (comma-separated Discord user IDs) if set,
otherwise the owner of the bot's application in the Discord Developer Portal (or its team).

/set writes cache/settings_override.env on the server (see envfile.py for the layers) and
updates the running bot at once; most settings are read fresh for every scan, so they apply
on the next ask. A few (tokens, channels, post times) are read only at startup: /set offers
a restart for those.
"""
import asyncio
import collections
import datetime as dt
import logging
import os
import re
import subprocess
import sys
from typing import Optional

from .envfile import SECRET, effective, layers, mask, remove_override, root_dir, set_override

STARTED = dt.datetime.now()
KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,80}$")
STARTUP_ONLY = re.compile(r"DISCORD_TOKEN|CHANNEL|POST_TIME|_TIME$|GRADE_TIME|CHAT_CHANNELS|PROVIDER|"
                          r"ANTHROPIC|GEMINI|MODEL$|OWNER_IDS")
_LOG = collections.deque(maxlen=2000)
_OWNERS = {}


async def is_owner(client, user) -> bool:
    raw = os.environ.get("LINEMAKER_OWNER_IDS", "").strip()
    if raw:
        return str(user.id) in {x.strip() for x in raw.split(",")}
    if "ids" not in _OWNERS:
        info = await client.application_info()
        ids = {info.owner.id} if info.owner else set()
        if info.team:
            ids |= {m.id for m in info.team.members}
        _OWNERS["ids"] = ids
    return user.id in _OWNERS["ids"]


def _scrub(text: str) -> str:
    """Never let a key or token reach Discord, even inside a log line."""
    for k, v in os.environ.items():
        if v and len(v) >= 8 and SECRET.search(k):
            text = text.replace(v, f"<{k}>")
    return text


class _Buffer(logging.Handler):
    def emit(self, record):
        try:
            _LOG.append(self.format(record))
        except Exception:
            pass


def install_log_buffer():
    h = _Buffer()
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%m-%d %H:%M:%S"))
    logging.getLogger().addHandler(h)


def recent_logs(n: int = 60, search: Optional[str] = None) -> str:
    lines = list(_LOG)
    if search:
        lines = [l for l in lines if search.lower() in l.lower()]
    return _scrub("\n".join(lines[-max(1, min(n, 2000)):]))


def settings_lines(search: Optional[str] = None) -> list:
    eff = effective()
    keys = sorted(k for k in eff if not search or search.upper() in k.upper())
    out = [f"⚙️ **Settings** ({len(keys)}): value · where it comes from"]
    for k in keys:
        v, src = eff[k]
        live = os.environ.get(k)
        note = "" if live is None or live == v or SECRET.search(k) else f" (running: {mask(k, live)})"
        out.append(f"`{k}` = `{mask(k, v)}` · {src}{note}")
    return out


def apply_set(key: str, value: str) -> str:
    key = key.strip().upper()
    value = value.strip()
    if not KEY_RE.match(key):
        return "That doesn't look like a setting name (letters, digits and _ only, e.g. LINEMAKER_MIN_GAP)."
    old = os.environ.get(key)
    set_override(key, value)
    os.environ[key] = value
    if SECRET.search(key):
        shown = "saved (hidden)"
        tip = ("\n🔐 Saved on the server only (cache/settings_override.env), never in GitHub. Discord "
               "keeps a copy of slash-command input, so rotate it later if that matters to you.")
    else:
        shown = f"`{old if old not in (None, '') else '(blank)'}` → `{value or '(blank)'}`"
        tip = ""
    known = any(key in vals for _s, vals in layers()) or key.startswith("LINEMAKER_")
    warn = "" if known else "\n⚠️ Not a setting I've seen before: check the spelling."
    when = ("\n🔄 This one is read at startup: run /restart (or /set again with restart:True)."
            if STARTUP_ONLY.search(key) else "\n✅ Applies to the next scan or question, no restart needed.")
    return f"**{key}** {shown}{when}{tip}{warn}"


def apply_unset(key: str) -> str:
    key = key.strip().upper()
    if not remove_override(key):
        return f"**{key}** wasn't set from Discord, so there's nothing to undo."
    fallback = None
    for src, vals in layers()[1:]:
        if key in vals:
            fallback = (vals[key], src)
            break
    if fallback:
        os.environ[key] = fallback[0]
        return f"**{key}** back to `{mask(key, fallback[0])}` from {fallback[1]}."
    os.environ.pop(key, None)
    return f"**{key}** removed (back to the built-in default)."


def restart_soon(delay: float = 2.0):
    """Exit; systemd (Restart=always) starts the bot again in about 10 seconds."""
    logging.getLogger("linemaker.admin").warning("restart requested from Discord")
    asyncio.get_running_loop().call_later(delay, os._exit, 0)


def status_lines() -> list:
    root = root_dir()
    try:
        commit = subprocess.run(["git", "-C", root, "log", "-1", "--format=%h · %s · %cr"],
                                capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        commit = ""
    up = dt.datetime.now() - STARTED
    h, rem = divmod(int(up.total_seconds()), 3600)
    out = ["🩺 **Linemaker status**",
           f"• Version: {commit or 'unknown (not a git clone)'}",
           f"• Up for {h}h {rem // 60}m (since {STARTED:%b %d %H:%M}) · Python {sys.version.split()[0]}"]
    over = layers()[0][1]
    out.append(f"• Settings changed from Discord: {', '.join(over) if over else 'none'}")
    busy = os.path.join(os.environ.get("LINEMAKER_CACHE_DIR", "cache"), "busy")
    import time
    fresh = os.path.exists(busy) and time.time() - os.path.getmtime(busy) < 600
    out.append("• Busy right now (scan or answer running)" if fresh else "• Idle")
    if os.path.exists(os.path.join(root, ".git", "linemaker-bad-commit")):
        out.append("• ⚠️ The last update failed to load and was rolled back")
    errors = [l for l in list(_LOG)[-500:] if " ERROR " in l]
    out.append(f"• Errors in recent logs: {len(errors)}" + (f" (latest: {_scrub(errors[-1])[:200]})" if errors else ""))
    return out


def env_changes() -> list:
    """Settings running from settings.env (GitHub) or /set that differ from the server's .env:
    the lines to copy into .env to make them permanent."""
    disc, repo, env = (vals for _s, vals in layers())
    out = []
    for source, vals in (("set from Discord", disc), ("settings.env (GitHub)", repo)):
        for k, v in vals.items():
            if env.get(k) == v:
                continue
            was = env.get(k)
            if SECRET.search(k):
                out.append(f"• `{k}` · {source} · new key/token: paste it into .env yourself (hidden here)")
            else:
                out.append(f"• `{k}={v}` · {source} · .env has "
                           + (f"`{was}`" if was not in (None, "") else "nothing"))
    return out


def changelog() -> list:
    """The change-log comment lines from settings.env."""
    path = os.path.join(root_dir(), "settings.env")
    try:
        with open(path) as f:
            lines = f.read().splitlines()
    except OSError:
        return []
    try:
        start = next(i for i, l in enumerate(lines) if "Change log" in l) + 1
    except StopIteration:
        return []
    out = []
    for l in lines[start:]:
        if l.startswith("# ----"):
            break
        l = l.lstrip("# ").strip()
        if l and l != "(no changes yet)":
            out.append(l)
    return out
