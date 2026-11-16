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
_TOKENS = {}      # sha256(token) -> (user id, name, expires, owner, avatar, guest)
_SESSIONS = {}    # sha256(session) -> (user id, name, expires, owner, avatar, guest)
LAST_SEEN = {}    # member name -> ISO time they last opened the dashboard
_CARDS = collections.deque(maxlen=CARDS_KEPT)
STATE = {"url": None, "proc": None}
ASK = {"handler": None}   # set by the bot: async (user id, name, text) -> answer dict
_JOBS = {}                # job id -> {"user", "done", "answer", "at"}
_ASKS = {}                # (name, date) -> asks today
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
# Tokens and sessions are kept by their SHA-256 (the link / cookie holds the secret itself) in
# ~/.linemaker-secrets/web_auth.json (0600), so a restart doesn't sign anyone out. Each entry:
# [user id, name, expires, owner, avatar, guest].
INVITE_TTL = 7 * 24 * 3600


def _auth_path() -> str:
    return os.path.join(os.path.expanduser(os.environ.get("LINEMAKER_SECRETS_DIR", "~/.linemaker-secrets")), "web_auth.json")


def _h(secret: str) -> str:
    import hashlib
    return hashlib.sha256((secret or "").encode()).hexdigest()


def _load_auth() -> None:
    if _TOKENS or _SESSIONS:
        return
    try:
        with open(_auth_path()) as f:
            d = json.load(f)
        _TOKENS.update({k: tuple(v) for k, v in d.get("tokens", {}).items()})
        _SESSIONS.update({k: tuple(v) for k, v in d.get("sessions", {}).items()})
    except (OSError, ValueError):
        pass


def _save_auth() -> None:
    now = time.time()
    for store in (_TOKENS, _SESSIONS):
        for k in [k for k, v in store.items() if v[2] < now]:
            store.pop(k, None)
    path = _auth_path()
    try:
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"tokens": _TOKENS, "sessions": _SESSIONS}, f)
        os.replace(path + ".tmp", path)
    except OSError as e:
        log.warning("web sign-ins not saved: %s", e)


def _new_token(entry: tuple, ttl: float) -> Optional[str]:
    url = base_url()
    if not url:
        return None
    _load_auth()
    tok = secrets.token_urlsafe(32)
    _TOKENS[_h(tok)] = (entry[0], entry[1], time.time() + ttl) + tuple(entry[2:])
    _save_auth()
    return f"{url}/login?t={tok}"


def login_link(user_id: str, name: str, owner: bool = False, avatar: str = "") -> Optional[str]:
    """A one-use sign-in link for a Discord member (15 minutes). owner: Members tab."""
    return _new_token((str(user_id), name, bool(owner), avatar or "", False), TOKEN_TTL)


def invite_link(name: str) -> Optional[str]:
    """A one-use invite for someone who isn't in the Discord (good for 7 days until it's
    opened, then 30 days signed in on that device): a guest named `name`."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:30] or "guest"
    return _new_token((f"guest:{slug}", name.strip()[:40] or "Guest", False, "", True), INVITE_TTL)


def redeem(token: str) -> Optional[str]:
    """A login token -> a new session id (the token is spent either way)."""
    _load_auth()
    got = _TOKENS.pop(_h(token), None)
    if not got or got[2] < time.time():
        _save_auth()
        return None
    sid = secrets.token_urlsafe(32)
    _SESSIONS[_h(sid)] = (got[0], got[1], time.time() + SESSION_TTL) + tuple(got[3:])
    _save_auth()
    return sid


def session(sid: Optional[str]) -> Optional[tuple]:
    _load_auth()
    got = _SESSIONS.get(_h(sid or ""))
    if not got or got[2] < time.time():
        return None
    return got


def is_owner(s: tuple) -> bool:
    return len(s) > 3 and bool(s[3])


def is_guest(s: tuple) -> bool:
    return len(s) > 5 and bool(s[5])


# ---- what the cards showed -------------------------------------------------------------------
def _cards_path() -> str:
    return os.path.join(os.environ.get("LINEMAKER_CACHE_DIR", "cache"), "web_cards.json")


def _ev_json(ev, profile=None) -> dict:
    from .parlay_format import short_label
    from .pro import book_prices
    from .scan_format import pick_stars, reason_tags
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
    try:
        from .bets import leg_from_eval
        from .polymarket_us import side_for
        leg = leg_from_eval(ev)
        pm = side_for(ev)
        pm = {k: pm[k] for k in ("pm_slug", "pm_long", "pm_tick", "pm_min")} if pm else None
    except Exception:
        leg, pm = None, None
    return {"label": short_label(s.label), "sport": m.sport, "game": m.game, "commence": getattr(m, "commence", ""),
            "book": s.best_book, "odds": s.best_odds, "ours": round(s.blended, 4), "implied": round(s.best_implied, 4),
            "gap": round(s.gap, 4), "tags": reason_tags(ev, profile), "links": links, "stars": pick_stars(ev, profile),
            "leg": leg, "pm": pm,
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
    try:
        from .bets import leg_from_eval
        raw = [leg_from_eval(ev, odds=o, book=p.book) for ev, o in p.legs]
    except Exception:
        raw = []
    return {"book": p.book, "kind": kind, "odds": p.american, "prob": round(p.prob, 4), "url": url, "legs_raw": raw,
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
    out = dict(_cached("model", 2400, model))
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


def members_data() -> list:
    """Owner view: every member the bot knows (bets, linked Polymarket, dashboard visits)."""
    from .bets import load_bets
    from .pm_accounts import linked
    cut = (dt.date.today() - dt.timedelta(days=30)).isoformat()
    pm = set(linked().values())
    by = {}
    for b in load_bets():
        if b.get("paper"):
            continue
        name = b.get("user") or "everyone"
        g = by.setdefault(name, {"name": name, "won": 0, "lost": 0, "profit": 0.0, "staked": 0.0, "open": 0,
                                 "last_bet": ""})
        g["last_bet"] = max(g["last_bet"], b.get("placed", ""))
        if b.get("status") == "open":
            g["open"] += 1
        elif b.get("placed", "") >= cut:
            g["won"] += b["status"] == "won"
            g["lost"] += b["status"] == "lost"
            g["profit"] += b.get("profit", 0.0)
            g["staked"] += 0.0 if b.get("bonus") else b.get("stake", 0.0)
    for name in pm | set(LAST_SEEN):
        by.setdefault(name, {"name": name, "won": 0, "lost": 0, "profit": 0.0, "staked": 0.0, "open": 0, "last_bet": ""})
    out = []
    for g in by.values():
        g.update(pm=g["name"] in pm, seen=LAST_SEEN.get(g["name"], ""), profit=round(g["profit"], 2),
                 roi=round(g["profit"] / g["staked"], 4) if g["staked"] else None)
        out.append(g)
    return sorted(out, key=lambda g: (g["last_bet"] or g["seen"]), reverse=True)


def activity_data(limit: int = 40) -> list:
    """Owner view: the newest bets from everyone."""
    from .bets import load_bets
    rows = sorted((b for b in load_bets() if not b.get("paper")), key=lambda b: b.get("placed", ""), reverse=True)[:limit]
    return [{"user": b.get("user") or "everyone", "what": " + ".join(l.get("label", "") for l in b.get("legs", []))[:160],
             "book": b.get("book", ""), "odds": b.get("odds"), "stake": b.get("stake"), "status": b.get("status"),
             "profit": b.get("profit"), "placed": b.get("placed", "")[:16], "pm": bool(b.get("pm_slug"))} for b in rows]


LEG_KEYS = {"key", "side", "label", "sport", "date", "bet_type", "game", "odds", "book", "live", "model_prob"}


def log_bet(name: str, body: dict) -> dict:
    """'I bet this' from the web: one leg or a parlay's legs, the odds you got, your stake."""
    from .bets import add_bet
    from .staking import paper_on
    legs = body.get("legs") or []
    if not isinstance(legs, list) or not 1 <= len(legs) <= 12:
        raise ValueError("pick a bet first")
    clean = []
    for l in legs:
        if not isinstance(l, dict) or not l.get("key") or l.get("side") not in ("A", "B", "C"):
            raise ValueError("that bet can't be logged from here")
        clean.append({k: l[k] for k in LEG_KEYS if k in l})
    odds, stake = int(float(body.get("odds"))), float(body.get("stake"))
    if abs(odds) < 100 or not 0 < stake <= 100000:
        raise ValueError("stake should be a dollar amount and odds like -150 or +120")
    book = str(body.get("book") or "")[:40] or "Other"
    bet = add_bet(clean, odds, stake, book, name, source="web", paper=paper_on())
    return {"ok": True, "id": bet["id"]}


def pm_status(uid: str) -> dict:
    from . import pm_accounts
    return {"linked": bool(pm_accounts.creds(uid)), "max": pm_accounts.max_stake(uid)}


def pm_link(uid: str, name: str, body: dict) -> dict:
    from . import pm_accounts
    pm_accounts.link(uid, name, str(body.get("key_id") or ""), str(body.get("secret") or ""))
    return pm_status(uid)


def pm_quote(uid: str, body: dict) -> dict:
    from . import pm_accounts
    from .polymarket_us import american, bbo, order_plan
    if not pm_accounts.creds(uid):
        raise ValueError("link your Polymarket account first (My bets tab)")
    q = bbo(str(body["slug"]))
    ask = q["long"] if body.get("long") else q["short"]
    stake = min(float(body.get("stake") or 5), pm_accounts.max_stake(uid))
    plan = order_plan(stake, ask, float(body.get("tick") or 0.01), float(body.get("min") or 1))
    if not plan:
        raise ValueError("nobody is offering this side, or that's less than one contract")
    return dict(plan, ask=ask, max=pm_accounts.max_stake(uid))


def pm_buy(uid: str, name: str, body: dict) -> dict:
    from . import pm_accounts
    from .bets import add_bet
    from .polymarket_us import buy
    cr = pm_accounts.creds(uid)
    if not cr:
        raise ValueError("link your Polymarket account first")
    qty, ask, cost = float(body["qty"]), float(body["ask"]), float(body.get("cost") or 0)
    if cost > pm_accounts.max_stake(uid) + 0.01:
        raise ValueError("over your max per order")
    got = buy(cr, str(body["slug"]), bool(body.get("long")), ask, qty, float(body.get("tick") or 0.01))
    if got.get("ok"):
        leg = body.get("leg") if isinstance(body.get("leg"), dict) else None
        legs = [dict({k: leg[k] for k in LEG_KEYS if k in leg}, odds=got["odds"], book="Polymarket")] if leg else \
            [{"label": str(body.get("label") or "Polymarket"), "sport": "Other", "bet_type": "external",
              "date": dt.date.today().isoformat(), "odds": got["odds"], "book": "Polymarket"}]
        add_bet(legs, got["odds"], got["cost"], "Polymarket", name, source="polymarket-bot",
                pm_slug=body["slug"], pm_long=bool(body.get("long")), pm_qty=got["qty"], pm_external=not leg)
    return got


def ask_limit(s: tuple) -> Optional[int]:
    """Asks a day (they spend odds credits): the owner unlimited, members 40, guests 15."""
    if is_owner(s):
        return None
    var, default = ("LINEMAKER_WEB_GUEST_ASKS", "15") if is_guest(s) else ("LINEMAKER_WEB_ASKS", "40")
    try:
        return int(os.environ.get(var, default))
    except ValueError:
        return int(default)


async def start_ask(s: tuple, text: str) -> dict:
    text = (text or "").strip()[:500]
    if not text:
        raise ValueError("type a question")
    if ASK["handler"] is None:
        raise ValueError("asking isn't available right now")
    day = dt.date.today().isoformat()
    cap, used = ask_limit(s), _ASKS.get((s[1], day), 0)
    if cap is not None and used >= cap:
        raise ValueError(f"that's your {cap} asks for today (they use the bot's odds credits); more tomorrow")
    _ASKS[(s[1], day)] = used + 1
    for k in [k for k, j in _JOBS.items() if time.time() - j["at"] > 3600]:
        _JOBS.pop(k, None)
    jid = secrets.token_urlsafe(12)
    job = _JOBS[jid] = {"user": s[0], "done": False, "answer": None, "at": time.time()}

    async def run():
        try:
            job["answer"] = await ASK["handler"](s[0], s[1], text)
        except asyncio.TimeoutError:
            job["answer"] = {"text": "⏱️ That took too long, so I stopped. Try again, or ask something narrower."}
        except Exception as e:
            log.warning("web ask failed: %s", type(e).__name__)
            job["answer"] = {"text": f"⚠️ Something went wrong ({type(e).__name__}). Try again in a minute."}
        job["done"] = True
    asyncio.get_running_loop().create_task(run())
    return {"job": jid, "left": None if cap is None else cap - used - 1}


def ask_result(s: tuple, jid: str) -> dict:
    job = _JOBS.get(jid)
    if not job or job["user"] != s[0]:
        raise ValueError("that ask expired")
    return {"done": job["done"], "answer": job["answer"]}


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
        _SESSIONS.pop(_h(request.cookies.get("lm_session") or ""), None)
        _save_auth()
        resp = web.HTTPFound("/")
        resp.del_cookie("lm_session")
        raise resp

    def api(fn, owner_only: bool = False):
        async def handler(request):
            s = who(request)
            if not s:
                return web.json_response({"error": "signed out"}, status=401)
            owner = is_owner(s)
            if owner_only and not owner:
                return web.json_response({"error": "owner only"}, status=403)
            LAST_SEEN[s[1]] = dt.datetime.now().isoformat(timespec="minutes")
            name = request.query.get("user") if owner and request.query.get("user") else s[1]   # owner: anyone's
            data = await asyncio.to_thread(fn, s, name)
            return web.json_response(data, headers={"Cache-Control": "no-store"})
        return handler

    def action(fn):
        """POST: signed in, JSON, and our own header (a page on another site can't send it)."""
        async def handler(request):
            s = who(request)
            if not s:
                return web.json_response({"error": "signed out"}, status=401)
            if request.headers.get("X-Linemaker") != "1":
                return web.json_response({"error": "bad request"}, status=400)
            try:
                body = await request.json()
            except Exception:
                body = {}
            try:
                return web.json_response(await asyncio.to_thread(fn, s, body if isinstance(body, dict) else {}))
            except (ValueError, KeyError, TypeError) as e:
                return web.json_response({"error": str(e)[:200]}, status=400)
            except Exception as e:
                log.warning("web action failed: %s", type(e).__name__)
                return web.json_response({"error": str(e)[:200]}, status=502)
        return handler

    async def ask(request):
        s = who(request)
        if not s:
            return web.json_response({"error": "signed out"}, status=401)
        if request.headers.get("X-Linemaker") != "1":
            return web.json_response({"error": "bad request"}, status=400)
        try:
            body = await request.json()
            return web.json_response(await start_ask(s, str((body or {}).get("text", ""))))
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)

    async def ask_poll(request):
        s = who(request)
        if not s:
            return web.json_response({"error": "signed out"}, status=401)
        try:
            return web.json_response(ask_result(s, request.match_info["jid"]), headers={"Cache-Control": "no-store"})
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=404)

    @web.middleware
    async def gzip(request, handler):   # smaller pages and data over the tunnel
        resp = await handler(request)
        if isinstance(resp, web.Response) and resp.body is not None and len(resp.body) > 1024 \
                and "gzip" in request.headers.get("Accept-Encoding", ""):
            resp.enable_compression()
        return resp

    app = web.Application(middlewares=[gzip])
    app.add_routes([web.get("/", index), web.get("/login", login), web.get("/logout", logout),
                    web.get("/api/me", api(lambda s, n: {"name": s[1], "owner": is_owner(s), "guest": is_guest(s),
                                                         "avatar": s[4] if len(s) > 4 else ""})),
                    web.get("/api/cards", api(lambda s, n: cards())),
                    web.get("/api/record", api(lambda s, n: record_data(n))),
                    web.get("/api/weekly", api(lambda s, n: weekly_data(n))),
                    web.get("/api/bets", api(lambda s, n: bets_data(n))),
                    web.get("/api/group", api(lambda s, n: group_data())),
                    web.get("/api/members", api(lambda s, n: members_data(), owner_only=True)),
                    web.get("/api/activity", api(lambda s, n: activity_data(), owner_only=True)),
                    web.get("/api/pm", api(lambda s, n: pm_status(s[0]))),
                    web.post("/api/bet", action(lambda s, b: log_bet(s[1], b))),
                    web.post("/api/pm/link", action(lambda s, b: pm_link(s[0], s[1], b))),
                    web.post("/api/pm/unlink", action(lambda s, b: {"ok": __import__("linemaker.pm_accounts", fromlist=["x"]).unlink(s[0])})),
                    web.post("/api/pm/quote", action(lambda s, b: pm_quote(s[0], b))),
                    web.post("/api/pm/buy", action(lambda s, b: pm_buy(s[0], s[1], b))),
                    web.post("/api/ask", ask), web.get("/api/ask/{jid}", ask_poll)])
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
    asyncio.get_running_loop().create_task(_warm())
    if os.environ.get("LINEMAKER_WEB_URL") or os.environ.get("LINEMAKER_WEB_TUNNEL", "quick").lower() == "off":
        return
    try:
        if os.environ.get("LINEMAKER_WEB_NGROK_DOMAIN") and os.environ.get("NGROK_AUTHTOKEN"):
            await _ngrok()
            return
        await _tunnel()
    except Exception as e:
        log.warning("web tunnel not started: %s", e)


async def _warm() -> None:
    """Rebuild the record numbers in the background (at start, then every 20 minutes while
    someone used the site in the last 2 hours), so the page never waits on them."""
    await asyncio.sleep(90)
    while True:
        recent = any(v >= (dt.datetime.now() - dt.timedelta(hours=2)).isoformat(timespec="minutes")
                     for v in LAST_SEEN.values())
        if recent or "model" not in _CACHE:
            try:
                _CACHE.pop("model", None)
                await asyncio.to_thread(record_data, "")
            except Exception as e:
                log.warning("web warm-up: %s", e)
        await asyncio.sleep(20 * 60)


def _ngrok_bin() -> str:
    """The official ngrok agent, downloaded once into cache/bin (for a fixed address)."""
    import tarfile
    path = os.path.join(os.environ.get("LINEMAKER_CACHE_DIR", "cache"), "bin", "ngrok")
    if os.path.exists(path):
        return path
    arch = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(platform.machine().lower())
    if not arch:
        raise RuntimeError(f"no ngrok build for {platform.machine()}")
    url = f"https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-linux-{arch}.tgz"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tgz = path + ".tgz"
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Linemaker/1.0"}), timeout=120) as r, \
            open(tgz, "wb") as f:
        f.write(r.read())
    with tarfile.open(tgz) as t:
        member = t.getmember("ngrok")
        with t.extractfile(member) as src, open(path + ".part", "wb") as out:
            out.write(src.read())
    os.remove(tgz)
    os.chmod(path + ".part", stat.S_IRWXU)
    os.replace(path + ".part", path)
    return path


async def _ngrok() -> None:
    """A fixed https address (ngrok's free static domain): invite links survive restarts."""
    domain = os.environ["LINEMAKER_WEB_NGROK_DOMAIN"].replace("https://", "").strip("/")
    binary = await asyncio.to_thread(_ngrok_bin)
    proc = await asyncio.create_subprocess_exec(
        binary, "http", f"127.0.0.1:{port()}", f"--url=https://{domain}", "--log=stdout", "--log-format=logfmt",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)   # NGROK_AUTHTOKEN comes from the env
    STATE["proc"] = proc

    async def read():
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            if b"started tunnel" in line and not STATE["url"]:
                STATE["url"] = f"https://{domain}"
                log.info("web dashboard on its fixed address")
            elif b"lvl=eror" in line or b"ERR_NGROK" in line:
                log.warning("ngrok: %s", line.decode(errors="ignore")[:200])
        STATE["url"] = None
        log.warning("ngrok stopped")
    asyncio.get_running_loop().create_task(read())


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
