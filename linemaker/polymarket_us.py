"""Polymarket US (polymarket.us): live prices from its own book, and trading for linked accounts.

Public data (no key): https://gateway.polymarket.us/v1 - events per league (series), every market
of a game (moneyline, spreads, totals, player-prop ladders "Norman Powell 10+ points") with each
side's ask ("quote"), best bid / offer per market.
Account data and orders (a linked key): https://api.polymarket.us/v1, every request signed with
the key's Ed25519 secret: X-PM-Access-Key, X-PM-Timestamp (ms), X-PM-Signature = base64 of the
signature over timestamp + METHOD + path (no query string).

Markets are binary: the first side is "long" (YES), the other "short" (NO). An order's price is
always the LONG side's price: buying the short side at 0.76 is sent as 0.24
(docs.polymarket.us, "Understanding Price with Order Intent").

Taker fee per contract: theta x p x (1 - p) (theta 0.095 below $10K monthly volume,
LINEMAKER_PM_TAKER_FEE), so a 64c ask costs about 66.2c: the prices shown and priced here include it.

How the bot's markets map (FeedMarket conventions: moneyline side 0 = home; spread line = home's
handicap, side 0 = home; total side 0 = over; props side 0 = over):
  moneyline  *_full_game_winner market, the side whose team is ours
  spread     *_full_game_spread, the side with our team at our handicap ("+9.50")
  total      *_full_game_total at the line, long = over
  props      "<player> N+ <stat>" ladder: over X.5 = (X+0.5)+ YES, under X.5 = its NO
"""
import base64
import datetime as dt
import json
import logging
import math
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Optional

from .oddsapi import norm

log = logging.getLogger("linemaker.polymarket_us")
GATEWAY = "https://gateway.polymarket.us"
API = "https://api.polymarket.us"
WEB = "https://polymarket.us/event/"
LEAGUES = {"NBA": ("nba-",), "NHL": ("nhl-",), "NFL": ("nfl",), "NCAAB": ("cbb-",), "NCAAF": ("cfb-",),
           "MLB": ("mlb-",), "WNBA": ("wnba-",), "UFC": ("ufc-",)}
PROP_TYPES = {"points": "player_points", "rebounds": "player_rebounds", "assists": "player_assists",
              "threes": "player_threes", "pra": "player_points_rebounds_assists", "steals": "player_steals",
              "blocks": "player_blocks", "pts + reb": "player_points_rebounds", "pts + ast": "player_points_assists",
              "reb + ast": "player_rebounds_assists"}
_CACHE, _LOCK = {}, threading.Lock()


class PolymarketError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"Polymarket US {status}: {message}")
        self.status, self.message = status, message


def on() -> bool:
    return os.environ.get("LINEMAKER_POLYMARKET_US", "on").lower() not in ("off", "0", "no", "false") \
        and not os.environ.get("LINEMAKER_OFFLINE")


def taker_fee() -> float:
    try:
        return float(os.environ.get("LINEMAKER_PM_TAKER_FEE", "0.095"))
    except ValueError:
        return 0.095


def cost_per_contract(p: float) -> float:
    """What one contract bought at p really costs (price + taker fee)."""
    return p + taker_fee() * p * (1 - p)


def american(p: float) -> Optional[int]:
    """A side's ask (0-1) as American odds, fee included."""
    if not p or p <= 0 or p >= 1:
        return None
    dec = 1 / cost_per_contract(p)
    return int(round((dec - 1) * 100)) if dec >= 2 else int(round(-100 / (dec - 1)))


# ---- HTTP ------------------------------------------------------------------------------------
def _sign(key_id: str, secret: str, method: str, path: str) -> dict:
    from nacl.signing import SigningKey
    ts = str(int(time.time() * 1000))
    raw = base64.b64decode(secret)
    key = SigningKey(raw[:32] if len(raw) == 64 else raw)
    sig = key.sign(f"{ts}{method}{path}".encode()).signature
    return {"X-PM-Access-Key": key_id, "X-PM-Timestamp": ts, "X-PM-Signature": base64.b64encode(sig).decode()}


def _call(method: str, path: str, query: Optional[dict] = None, body: Optional[dict] = None,
          creds: Optional[tuple] = None, timeout: float = 20, opener=None, idempotency: Optional[str] = None):
    """JSON from the gateway (public) or the API (signed with creds = (key_id, secret))."""
    opener = opener or urllib.request.urlopen
    base = API if creds else GATEWAY
    qs = ""
    if query:
        pairs = []
        for k, v in query.items():
            for x in (v if isinstance(v, list) else [v]):
                if x is not None:
                    pairs.append((k, str(x).lower() if isinstance(x, bool) else str(x)))
        qs = "?" + urllib.parse.urlencode(pairs)
    headers = {"Content-Type": "application/json", "User-Agent": "Linemaker/1.0"}
    if creds:
        headers.update(_sign(creds[0], creds[1], method, path))
    if idempotency:
        headers["Idempotency-Key"] = idempotency
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path + qs, data=data, headers=headers, method=method)
    try:
        with opener(req, timeout=timeout) as resp:
            text = resp.read().decode() or "{}"
            return json.loads(text)
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read().decode() or "{}")
            msg = msg.get("message") or msg.get("error") or str(msg)
        except Exception:
            msg = e.reason
        raise PolymarketError(e.code, str(msg)[:300])


def _cached(key: str, ttl: float, fetch):
    now = time.time()
    with _LOCK:
        hit = _CACHE.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    data = fetch()
    with _LOCK:
        _CACHE[key] = (now, data)
        if len(_CACHE) > 300:   # small server: keep only the newest
            for k, _v in sorted(_CACHE.items(), key=lambda kv: kv[1][0])[:100]:
                _CACHE.pop(k, None)
    return data


# ---- public data -----------------------------------------------------------------------------
def series_ids(sport: str) -> list:
    prefixes = LEAGUES.get(sport.upper())
    if not prefixes:
        return []
    data = _cached("series", 24 * 3600, lambda: _call("GET", "/v1/series", {"limit": 500}))
    rows = data.get("series", data) if isinstance(data, dict) else data
    return [int(s["id"]) for s in rows or []
            if any((s.get("slug") or "").startswith(p) for p in prefixes) and "sl-" not in (s.get("slug") or "")]


def events(sport: str, start: str, end: str) -> list:
    """Open games of a league starting between start and end (ISO, UTC)."""
    ids = series_ids(sport)
    if not ids:
        return []
    return _cached(f"events|{sport}|{start}|{end}", 120, lambda: _call("GET", "/v1/events", {
        "seriesId": ids, "startTimeMin": start, "startTimeMax": end, "active": True, "closed": False,
        "limit": 100}).get("events", []))


def event_markets(slug: str) -> list:
    """Every market of one game, each side with its current ask (one request, cached 2 minutes)."""
    def fetch():
        d = _call("GET", f"/v1/events/slug/{slug}")
        return slim_markets((d.get("event") or d).get("markets") or [])
    return _cached(f"event|{slug}", 120, fetch)


def slim_markets(markets: list) -> list:
    """Only what pricing and orders need (a game's raw markets run to megabytes)."""
    out = []
    for m in markets:
        if not m.get("active", True) or m.get("closed"):
            continue
        sides = []
        for s in m.get("marketSides") or []:
            q = (s.get("quote") or {}).get("value")
            sides.append({"long": bool(s.get("long")), "desc": s.get("description") or "",
                          "team": (s.get("team") or {}).get("name") or "", "quote": float(q) if q else None})
        out.append({"slug": m.get("slug"), "type": m.get("sportsMarketType") or "", "title": m.get("title") or "",
                    "line": m.get("line"), "sides": sides, "tick": float(m.get("orderPriceMinTickSize") or 0.01),
                    "min_qty": float(m.get("minimumTradeQty") or 1)})
    return out


def bbo(slug: str) -> dict:
    """{"long": ask to buy the long side, "short": ask to buy the short side} right now."""
    d = _call("GET", f"/v1/markets/{slug}/bbo").get("marketData") or {}
    get = lambda k: float((d.get(k) or {}).get("value")) if (d.get(k) or {}).get("value") else None
    return {"long": get("longQuote") or get("bestAsk"), "short": get("shortQuote")}


# ---- matching the bot's markets ---------------------------------------------------------------
def _same_team(a: str, b: str) -> bool:
    a, b = norm(a), norm(b)
    return bool(a and b) and (a == b or a.split()[-1] == b.split()[-1] and (a in b or b in a or len(a.split()) > 1))


def find_event(evs: list, away: str, home: str) -> Optional[dict]:
    for e in evs:
        teams = {s.get("team", {}).get("name") or "" for m in e.get("markets") or [] for s in m.get("marketSides") or []}
        teams |= {e.get("title") or ""}
        if any(_same_team(away, t) for t in teams) and any(_same_team(home, t) for t in teams):
            return e
    for e in evs:   # title only ("CHI Bulls vs. CLE Cavaliers"): nicknames
        t = norm(e.get("title") or "")
        if norm(away).split()[-1:] and norm(away).split()[-1] in t and norm(home).split()[-1] in t:
            return e
    return None


def _player_market(markets: list, kind: str, player: str, n: float) -> Optional[dict]:
    want = norm(player)
    for m in markets:
        if not m["type"].endswith(kind) or m.get("line") is None or abs(float(m["line"]) - n) > 1e-6:
            continue
        title = norm(m["title"].split(f" {int(n)}+")[0]) if f" {int(n)}+" in m["title"] else ""
        if title and (title == want or title.split()[-1] == want.split()[-1] and title[0] == want[0]):
            return m
    return None


def match(fm, markets: list) -> Optional[list]:
    """[(market, long?) for side 0, (market, long?) for side 1] for a FeedMarket, or None."""
    bt = getattr(fm, "bet_type", "")
    away = fm.opponent if bt != "prop" else ""
    home = fm.subject if bt != "prop" else ""
    if bt == "moneyline":
        m = next((x for x in markets if x["type"].endswith("_full_game_winner")), None)
        if not m or len(m["sides"]) != 2:
            return None
        h = next((s for s in m["sides"] if _same_team(home, s["team"] or s["desc"])), None)
        return [(m, h["long"]), (m, not h["long"])] if h else None
    if bt == "spread":
        line = float(fm.line)
        for m in markets:
            if not m["type"].endswith("_full_game_spread") or m.get("line") is None or abs(abs(float(m["line"])) - abs(line)) > 1e-6:
                continue
            for s in m["sides"]:
                try:
                    pt = float(s["desc"].replace("+", ""))
                except ValueError:
                    continue
                if _same_team(home, s["team"]) and abs(pt - line) < 1e-6:
                    return [(m, s["long"]), (m, not s["long"])]
                if _same_team(away, s["team"]) and abs(pt + line) < 1e-6:
                    return [(m, not s["long"]), (m, s["long"])]
        return None
    if bt == "total":
        m = next((x for x in markets if x["type"].endswith("_full_game_total") and x.get("line") is not None
                  and abs(float(x["line"]) - float(fm.line)) < 1e-6), None)
        if not m:
            return None
        over = next((s for s in m["sides"] if s["desc"].lower().startswith("over")), None)
        return [(m, over["long"]), (m, not over["long"])] if over else [(m, True), (m, False)]
    if bt == "prop":
        kind = PROP_TYPES.get((fm.market or "").lower())
        line = float(fm.line)
        if not kind or abs(line - math.floor(line) - 0.5) > 1e-6:
            return None   # ladders are whole numbers: X.5 lines only
        m = _player_market(markets, kind, fm.subject, line + 0.5)
        if not m:
            return None
        yes = next((s for s in m["sides"] if s["desc"].lower() == "yes"), None)
        return [(m, yes["long"] if yes else True), (m, not (yes["long"] if yes else True))]
    return None


def side_quote(m: dict, long: bool) -> Optional[float]:
    s = next((x for x in m["sides"] if x["long"] == long), None)
    return s["quote"] if s and s["quote"] and 0 < s["quote"] < 1 else None


def inject(feed: list, sport: str) -> int:
    """Put Polymarket US's own prices (fee included) and market pages on the feed's markets of
    the games it lists; side links carry the market slug and side, for one-tap orders.
    Replaces any second-hand Polymarket price. Returns how many markets got one."""
    if not on() or sport.upper() not in LEAGUES or not feed:
        return 0
    commences = sorted({fm.commence for fm in feed if getattr(fm, "commence", "")})
    if not commences:
        return 0
    t0 = dt.datetime.fromisoformat(commences[0].replace("Z", "+00:00")) - dt.timedelta(hours=2)
    t1 = dt.datetime.fromisoformat(commences[-1].replace("Z", "+00:00")) + dt.timedelta(hours=2)
    fmt = lambda t: t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        evs = events(sport, fmt(t0), fmt(t1))
    except Exception as e:
        log.warning("polymarket us events %s: %s", sport, e)
        return 0
    by_game, n = {}, 0
    for fm in feed:
        by_game.setdefault(fm.game, []).append(fm)
    for game, fms in by_game.items():
        away, _, home = game.partition(" @ ")
        ev = find_event(evs, away, home)
        if not ev:
            continue
        try:
            markets = event_markets(ev["slug"])
        except Exception as e:
            log.warning("polymarket us %s: %s", ev.get("slug"), e)
            continue
        for fm in fms:
            got = match(fm, markets)
            if not got:
                continue
            quotes = [side_quote(m, long) for m, long in got]
            odds = [american(q) if q else None for q in quotes]
            if all(o is None for o in odds):
                continue
            fm.books = [(b, o) for b, o in fm.books if "polymarket" not in b.lower()] + [("Polymarket", odds)]
            fm.links["Polymarket"] = [{"link": WEB + ev["slug"], "pm_slug": m["slug"], "pm_long": long,
                                       "pm_tick": m["tick"], "pm_min": m["min_qty"]} for m, long in got]
            n += 1
    if n:
        log.info("polymarket us: %s prices on %d markets", sport, n)
    return n


def side_for(ev) -> Optional[dict]:
    """The Polymarket market + side (slug, long, tick, min) for a pick's chosen side, if any."""
    try:
        entries = (ev.market.links or {}).get("Polymarket") or []
        idx = ev.market.side_keys.index(ev.best.side) if hasattr(ev.market, "side_keys") else (0 if ev.best.side == "A" else 1)
        e = entries[idx] if idx < len(entries) else None
        return e if e and e.get("pm_slug") else None
    except (ValueError, AttributeError):
        return None


def buy(cr: tuple, slug: str, long: bool, ask: float, qty: float, tick: float = 0.01) -> dict:
    """Confirmed buy: re-check the offer (no worse than half a tick), then an immediate-or-cancel
    order. {"ok", "qty", "avg", "cost", "odds", "msg"}."""
    q = bbo(slug)
    now = q["long"] if long else q["short"]
    if now is None:
        return {"ok": False, "msg": "Nobody is offering this side right now."}
    if now > ask + tick / 2:
        return {"ok": False, "moved": now, "msg": f"The price moved to {now * 100:.1f}c (you confirmed {ask * 100:.1f}c)."}
    resp = place(cr, slug, long, ask, qty, tick)
    got, avg = fills(resp, long)
    if not got:
        return {"ok": False, "msg": "No fill: the offer was gone by the time the order arrived. Nothing was bought."}
    return {"ok": True, "qty": got, "avg": avg, "cost": round(got * cost_per_contract(avg), 2), "odds": american(avg)}


# ---- account & orders (a linked key) ----------------------------------------------------------
def balances(creds: tuple) -> dict:
    return _call("GET", "/v1/account/balances", creds=creds)


def positions(creds: tuple) -> list:
    out, cursor = [], None
    for _ in range(10):
        d = _call("GET", "/v1/portfolio/positions", {"limit": 100, "cursor": cursor}, creds=creds)
        out += [dict(p, slug=s) for s, p in (d.get("positions") or {}).items()]
        cursor = d.get("nextCursor")
        if d.get("eof") or not cursor:
            break
    return out


def activities(creds: tuple, limit: int = 100) -> list:
    d = _call("GET", "/v1/portfolio/activities", {"limit": limit, "sortOrder": "SORT_ORDER_DESCENDING"}, creds=creds)
    return d.get("activities") or []


def order_plan(stake: float, ask: float, tick: float = 0.01, min_qty: float = 1.0) -> Optional[dict]:
    """Contracts to buy for about `stake` dollars at `ask` (fee included), or None if too small."""
    if not ask or not 0 < ask < 1:
        return None
    each = cost_per_contract(ask)
    step = min_qty if min_qty > 0 else 1.0
    qty = math.floor(stake / each / step) * step
    if qty < step:
        return None
    qty = round(qty, 2)
    return {"qty": qty, "price": ask, "cost": round(qty * each, 2), "payout": round(qty, 2),
            "odds": american(ask)}


def place(creds: tuple, slug: str, long: bool, ask: float, qty: float, tick: float = 0.01,
          opener=None) -> dict:
    """Buy `qty` contracts of one side at `ask` or better, now or never (immediate-or-cancel
    limit: no order is left on the book, never more than the price confirmed). The order's
    price is the long side's: 1 - ask for the short side. Returns the API's response."""
    px = ask if long else 1 - ask
    px = round(round(px / tick) * tick, 4)
    px = min(max(px, 0.01), 0.99)
    body = {"marketSlug": slug, "intent": "ORDER_INTENT_BUY_LONG" if long else "ORDER_INTENT_BUY_SHORT",
            "type": "ORDER_TYPE_LIMIT", "price": {"value": f"{px:.4f}".rstrip("0").rstrip("."), "currency": "USD"},
            "quantity": qty, "tif": "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL",
            "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_MANUAL", "synchronousExecution": True}
    return _call("POST", "/v1/orders", body=body, creds=creds, opener=opener, idempotency=uuid.uuid4().hex)


def fills(resp: dict, long: bool) -> tuple:
    """(contracts filled, average price of OUR side) from an order response's executions."""
    qty = cost = 0.0
    for ex in resp.get("executions") or []:
        if ex.get("type") not in ("EXECUTION_TYPE_FILL", "EXECUTION_TYPE_PARTIAL_FILL"):
            continue
        q = float(ex.get("lastShares") or 0)
        px = float((ex.get("lastPx") or {}).get("value") or 0)
        side_px = px if long else 1 - px
        qty += q
        cost += q * side_px
    return qty, (cost / qty if qty else None)
