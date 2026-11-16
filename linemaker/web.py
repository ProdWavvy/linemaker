"""Private web dashboard, served by the bot itself (aiohttp, already part of discord.py).

  /web in Discord  -> a personal login link (one use, 15 minutes) -> a 30-day cookie on that device
  Today       the latest cards' picks, legs and parlays, in the book colours, with reason tags
  Line shop   every pick's price at each book, the best one marked
  Record      the model's picks day by day, closing-line value, your bets' running profit
  My bets     your open and settled bets (Polymarket positions sync in on their own)
  Group       everyone's record side by side

It listens on 127.0.0.1 only (LINEMAKER_WEB_PORT, default 8765): nothing is opened on the
server. HTTPS comes from a Cloudflare quick tunnel the bot starts itself (cloudflared, the
official binary, downloaded once into cache/bin): it prints a *.trycloudflare.com address,
new after every restart, which /web always uses. LINEMAKER_WEB_URL=https://your.domain uses
your own tunnel or proxy instead; LINEMAKER_WEB=off turns all of it off.
"""
import asyncio
import collections
import datetime as dt
import json
import logging
import os
import platform
import re
import secrets
import stat
import time
import urllib.request
from typing import Optional

log = logging.getLogger("linemaker.web")
TOKEN_TTL = 15 * 60
SESSION_TTL = 30 * 24 * 3600
CARDS_KEPT = 8
_TOKENS = {}      # token -> (user id, name, expires)
_SESSIONS = {}    # session -> (user id, name, expires)
_CARDS = collections.deque(maxlen=CARDS_KEPT)
STATE = {"url": None, "proc": None}
_CACHE = {}


def on() -> bool:
    return os.environ.get("LINEMAKER_WEB", "on").lower() not in ("off", "0", "no", "false")


def port() -> int:
    try:
        return int(os.environ.get("LINEMAKER_WEB_PORT", "8765"))
    except ValueError:
        return 8765


def base_url() -> Optional[str]:
    return (os.environ.get("LINEMAKER_WEB_URL") or "").rstrip("/") or STATE["url"]


# ---- login -------------------------------------------------------------------------------------
def login_link(user_id: str, name: str) -> Optional[str]:
    url = base_url()
    if not url:
        return None
    now = time.time()
    for t in [t for t, v in _TOKENS.items() if v[2] < now]:
        _TOKENS.pop(t, None)
    tok = secrets.token_urlsafe(32)
    _TOKENS[tok] = (str(user_id), name, now + TOKEN_TTL)
    return f"{url}/login?t={tok}"


def redeem(token: str) -> Optional[str]:
    """A login token -> a new session id (the token is spent either way)."""
    got = _TOKENS.pop(token or "", None)
    if not got or got[2] < time.time():
        return None
    sid = secrets.token_urlsafe(32)
    _SESSIONS[sid] = (got[0], got[1], time.time() + SESSION_TTL)
    return sid


def session(sid: Optional[str]) -> Optional[tuple]:
    got = _SESSIONS.get(sid or "")
    if not got or got[2] < time.time():
        _SESSIONS.pop(sid or "", None)
        return None
    return got


# ---- what the cards showed -------------------------------------------------------------------
def _cards_path() -> str:
    return os.path.join(os.environ.get("LINEMAKER_CACHE_DIR", "cache"), "web_cards.json")


def _ev_json(ev, profile=None) -> dict:
    from .parlay_format import short_label
    from .pro import book_prices
    from .scan_format import reason_tags
    s, m = ev.best, ev.market
    try:
        prices = book_prices(m, s.side)
    except Exception:
        prices = {}
    try:
        from .betlinks import pick_links
        links = [{"book": b, "url": u} for b, u in pick_links(ev) if u and len(u) < 2000]
    except Exception:
        links = []
    return {"label": short_label(s.label), "sport": m.sport, "game": m.game, "commence": getattr(m, "commence", ""),
            "book": s.best_book, "odds": s.best_odds, "ours": round(s.blended, 4), "implied": round(s.best_implied, 4),
            "gap": round(s.gap, 4), "tags": reason_tags(ev, profile), "links": links,
            "books": [{"book": b, "odds": o} for b, o in sorted(prices.items(), key=lambda kv: kv[0]) if o is not None]}


def _parlay_json(p) -> dict:
    from .parlay_format import short_label
    kind = "Edge" if p.style == "edge" else getattr(p, "kind", "") or ("Value" if p.style == "value" else "Safest")
    try:
        from .betlinks import leg_links, parlay_link
        url = parlay_link(p)
        legs_urls = [u for _l, u in leg_links(p)] if not url else []
    except Exception:
        url, legs_urls = None, []
    return {"book": p.book, "kind": kind, "odds": p.american, "prob": round(p.prob, 4), "url": url,
            "legs": [{"label": short_label(ev.best.label), "odds": o,
                      "url": legs_urls[i] if i < len(legs_urls) else None} for i, (ev, o) in enumerate(p.legs)]}


def _entry_json(e) -> dict:
    """A PrizePicks / Underdog entry (its multiplier instead of odds)."""
    from .parlay_format import short_label
    try:
        from .pp import entry_link
        url = entry_link(e)
    except Exception:
        url = None
    from .pp import APPS
    return {"book": (APPS.get(e.app) or {}).get("book", e.app.title()),
            "kind": e.label, "odds": None, "pays": e.payouts[0] if e.payouts else None, "prob": round(e.p_all, 4),
            "url": url, "legs": [{"label": short_label(ev.best.label), "odds": None} for ev in e.legs]}


def record_card(title: str, picks: list, legs: list, parlays: list, entries: Optional[list] = None,
                ufc: Optional[dict] = None) -> None:
    """Keep what a posted card showed (newest first, last CARDS_KEPT), also on disk for restarts.
    picks / legs: [(ev, profile)]; parlays: [Parlay]; entries: pick'em entries; ufc: the UFC
    props card's rows (ufc_props.props_card)."""
    try:
        card = {"title": title, "at": dt.datetime.now().isoformat(timespec="seconds"),
                "picks": [_ev_json(ev, pr) for ev, pr in picks][:25],
                "legs": [_ev_json(ev, pr) for ev, pr in legs][:25],
                "parlays": ([_parlay_json(p) for p in parlays] + [_entry_json(e) for e in entries or []])[:25]}
        if ufc:
            card["ufc"] = ufc
        if not (card["picks"] or card["legs"] or card["parlays"] or ufc):
            return
        same = lambda c: (c["title"], [p["label"] for p in c["picks"] + c["legs"]])
        if _CARDS and same(_CARDS[0]) == same(card):   # the same card built again (a slimmer retry)
            _CARDS[0] = card
        else:
            _CARDS.appendleft(card)
        os.makedirs(os.path.dirname(_cards_path()) or ".", exist_ok=True)
        with open(_cards_path(), "w") as f:
            json.dump(list(_CARDS), f)
    except Exception as e:
        log.warning("web: couldn't keep the card: %s", e)


def cards() -> list:
    if not _CARDS:
        try:
            with open(_cards_path()) as f:
                _CARDS.extend(json.load(f)[:CARDS_KEPT])
        except (OSError, ValueError):
            pass
    return list(_CARDS)


# ---- numbers for the page ---------------------------------------------------------------------
def _cached(key: str, ttl: float, fn):
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    val = fn()
    _CACHE[key] = (time.time(), val)
    return val


def record_data(name: str, days: int = 14) -> dict:
    """Model picks day by day (units), closing-line value, and this member's bets' running profit."""
    from .bets import load_bets, mine, per_user_on
    from .reports import clv_summary, pick_record
    from .tracking import load_records
    tdir = os.environ.get("LINEMAKER_TRACKING_DIR", "tracking")

    def model():
        today = dt.date.today()
        series, allrecs = [], []
        for i in range(days, 0, -1):
            d = (today - dt.timedelta(days=i)).isoformat()
            recs = load_records(tdir, 0, d, graded_only=True, lean=True)
            pr = pick_record(recs)
            allrecs += [r for r in recs if r.get("picked")]
            series.append({"date": d, "units": round(pr["units"], 2), "won": pr["won"], "lost": pr["lost"]})
        c = clv_summary(allrecs)
        return {"series": series, "clv": {"n": c["n"], "beat": c.get("beat"), "pts": c.get("avg_prob")}}
    out = dict(_cached("model", 1800, model))
    who = name if per_user_on() else None
    settled = sorted((b for b in load_bets() if mine(b, who) and not b.get("paper") and b.get("status") != "open"
                      and b.get("settled")), key=lambda b: b["settled"])
    run, pts = 0.0, []
    for b in settled:
        run += b.get("profit", 0.0)
        pts.append({"date": b["settled"][:10], "profit": round(run, 2)})
    out["mine"] = pts
    return out


def weekly_data(name: str) -> list:
    """The weekly summary's lines (the Monday post's numbers, any day)."""
    from .bets import CURRENT_USER
    from .reports import weekly_lines
    tok = CURRENT_USER.set(name)   # "Your bets" = this member's
    try:
        return _cached(f"weekly|{name}", 900, lambda: weekly_lines(dt.date.today().isoformat()))
    finally:
        CURRENT_USER.reset(tok)


def bets_data(name: str) -> dict:
    from .bets import load_bets, mine, per_user_on
    who = name if per_user_on() else None
    rows = [b for b in load_bets() if mine(b, who) and not b.get("paper")]
    slim = lambda b: {"what": " + ".join(l.get("label", "") for l in b.get("legs", []))[:200], "book": b.get("book", ""),
                      "odds": b.get("odds"), "stake": b.get("stake"), "status": b.get("status"),
                      "profit": b.get("profit"), "placed": b.get("placed", "")[:16], "pm": bool(b.get("pm_slug"))}
    open_ = [slim(b) for b in rows if b.get("status") == "open"]
    done = [slim(b) for b in sorted((b for b in rows if b.get("status") != "open"),
                                    key=lambda b: b.get("settled", ""), reverse=True)[:40]]
    return {"open": open_, "settled": done}


def group_data(days: int = 30) -> list:
    from .bets import load_bets
    cut = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    by = {}
    for b in load_bets():
        if b.get("paper") or b.get("status") == "open" or b.get("placed", "") < cut:
            continue
        g = by.setdefault(b.get("user") or "everyone", {"name": b.get("user") or "everyone", "won": 0, "lost": 0,
                                                         "staked": 0.0, "profit": 0.0})
        g["won"] += b["status"] == "won"
        g["lost"] += b["status"] == "lost"
        g["staked"] += 0.0 if b.get("bonus") else b.get("stake", 0.0)
        g["profit"] += b.get("profit", 0.0)
    out = sorted(by.values(), key=lambda g: -g["profit"])
    for g in out:
        g["roi"] = round(g["profit"] / g["staked"], 4) if g["staked"] else None
        g["profit"], g["staked"] = round(g["profit"], 2), round(g["staked"], 2)
    return out


# ---- the server ----------------------------------------------------------------------------------
def make_app():
    from aiohttp import web
    from .web_page import PAGE, SIGNED_OUT

    def who(request) -> Optional[tuple]:
        return session(request.cookies.get("lm_session"))

    async def index(request):
        if not who(request):
            return web.Response(text=SIGNED_OUT, content_type="text/html")
        return web.Response(text=PAGE, content_type="text/html", headers={"Cache-Control": "no-store"})

    async def login(request):
        sid = redeem(request.query.get("t", ""))
        if not sid:
            return web.Response(text=SIGNED_OUT, content_type="text/html", status=403)
        resp = web.HTTPFound("/")
        resp.set_cookie("lm_session", sid, max_age=SESSION_TTL, httponly=True, secure=True, samesite="Lax")
        raise resp

    async def logout(request):
        _SESSIONS.pop(request.cookies.get("lm_session") or "", None)
        resp = web.HTTPFound("/")
        resp.del_cookie("lm_session")
        raise resp

    def api(fn):
        async def handler(request):
            s = who(request)
            if not s:
                return web.json_response({"error": "signed out"}, status=401)
            data = await asyncio.to_thread(fn, s)
            return web.json_response(data, headers={"Cache-Control": "no-store"})
        return handler

    app = web.Application()
    app.add_routes([web.get("/", index), web.get("/login", login), web.get("/logout", logout),
                    web.get("/api/me", api(lambda s: {"name": s[1]})),
                    web.get("/api/cards", api(lambda s: cards())),
                    web.get("/api/record", api(lambda s: record_data(s[1]))),
                    web.get("/api/weekly", api(lambda s: weekly_data(s[1]))),
                    web.get("/api/bets", api(lambda s: bets_data(s[1]))),
                    web.get("/api/group", api(lambda s: group_data()))])
    return app


async def start() -> None:
    """Serve on 127.0.0.1 and open the tunnel (best effort: the bot runs fine without it)."""
    if not on():
        return
    from aiohttp import web
    runner = web.AppRunner(make_app(), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port()).start()
    log.info("web dashboard on 127.0.0.1:%d", port())
    if os.environ.get("LINEMAKER_WEB_URL") or os.environ.get("LINEMAKER_WEB_TUNNEL", "quick").lower() == "off":
        return
    try:
        await _tunnel()
    except Exception as e:
        log.warning("web tunnel not started: %s", e)


def _cloudflared() -> str:
    """The official cloudflared binary, downloaded once into cache/bin."""
    path = os.path.join(os.environ.get("LINEMAKER_CACHE_DIR", "cache"), "bin", "cloudflared")
    if os.path.exists(path):
        return path
    arch = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(platform.machine().lower())
    if not arch:
        raise RuntimeError(f"no cloudflared build for {platform.machine()}")
    url = f"https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-{arch}"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".part"
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Linemaker/1.0"}), timeout=120) as r, \
            open(tmp, "wb") as f:
        while True:
            chunk = r.read(1 << 16)
            if not chunk:
                break
            f.write(chunk)
    os.chmod(tmp, stat.S_IRWXU)
    os.replace(tmp, path)
    return path


async def _tunnel() -> None:
    binary = await asyncio.to_thread(_cloudflared)
    proc = await asyncio.create_subprocess_exec(
        binary, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{port()}",
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    STATE["proc"] = proc

    async def read():
        pat = re.compile(rb"https://[a-z0-9-]+\.trycloudflare\.com")
        while True:
            line = await proc.stderr.readline()
            if not line:
                break
            m = pat.search(line)
            if m and not STATE["url"]:
                STATE["url"] = m.group(0).decode()
                log.info("web dashboard tunnel up")
        STATE["url"] = None
        log.warning("web tunnel stopped")
    asyncio.get_running_loop().create_task(read())
