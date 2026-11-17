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
    Replaces any second-hand Polymarket price. A second-hand price on a market Polymarket US
    doesn't list is dropped (LINEMAKER_PM_US_ONLY, on): you can't bet it in the States, and a
    parlay with it couldn't be placed. Returns how many markets got a Polymarket US price."""
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
    by_game, n, dropped = {}, 0, 0
    us_only = bool(evs) and os.environ.get("LINEMAKER_PM_US_ONLY", "on").lower() not in ("off", "0", "no", "false")

    def drop(fm):   # a second-hand Polymarket price on a market Polymarket US doesn't list
        nonlocal dropped
        if us_only and any("polymarket" in b.lower() for b, _o in fm.books):
            fm.books = [(b, o) for b, o in fm.books if "polymarket" not in b.lower()]
            fm.links.pop("Polymarket", None)
            dropped += 1
    for fm in feed:
        by_game.setdefault(fm.game, []).append(fm)
    for game, fms in by_game.items():
        away, _, home = game.partition(" @ ")
        ev = find_event(evs, away, home)
        if not ev:
            for fm in fms:
                drop(fm)
            continue
        try:
            markets = event_markets(ev["slug"])
        except Exception as e:
            log.warning("polymarket us %s: %s", ev.get("slug"), e)
            continue
        for fm in fms:
            got = match(fm, markets)
            if not got:
                drop(fm)
                continue
            quotes = [side_quote(m, long) for m, long in got]
            odds = [american(q) if q else None for q in quotes]
            if all(o is None for o in odds):
                continue
            fm.books = [(b, o) for b, o in fm.books if "polymarket" not in b.lower()] + [("Polymarket", odds)]
            fm.links["Polymarket"] = [{"link": WEB + ev["slug"], "pm_slug": m["slug"], "pm_long": long,
                                       "pm_tick": m["tick"], "pm_min": m["min_qty"]} for m, long in got]
            n += 1
    if n or dropped:
        log.info("polymarket us: %s prices on %d markets (%d second-hand prices dropped)", sport, n, dropped)
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


# ---- parlays ("combos") from a linked account ------------------------------------------------
# docs.polymarket.us: Combos API + RFQ API. A combo is its own instrument made of 2-10 legs
# (market slug + SIDE_BUY for the long side / SIDE_SELL for the short side); POST /v1/combos makes
# it (or returns the existing one). Its price comes from market makers: POST /v1/rfqs asks for
# quotes on a cash amount (the combo taker fee comes out of that amount), GET /v1/rfqs/quotes reads
# them (for a buy, the lowest buyPrice wins), PUT .../accept takes one, the maker gets a last
# look, then the exchange sends both orders (ours immediate-or-cancel). GET /v1/order/{id} says
# what filled. Combo taker fee: C x p x [theta x (1 - p) + 0.06 x (1 - p)^4].
COMBO_FEE_EXTRA = 0.06


def combo_fee(p: float, qty: float) -> float:
    return qty * p * (taker_fee() * (1 - p) + COMBO_FEE_EXTRA * (1 - p) ** 4)


def combo_american(price: float) -> Optional[int]:
    """A combo's price (0-1) as American odds, its taker fee included."""
    if not price or not 0 < price < 1:
        return None
    dec = 1 / (price + combo_fee(price, 1.0))
    return int(round((dec - 1) * 100)) if dec >= 2 else int(round(-100 / (dec - 1)))


def combo_legs(sides: list) -> list:
    return [{"symbol": s["pm_slug"], "side": "SIDE_BUY" if s["pm_long"] else "SIDE_SELL"} for s in sides]


def create_combo(creds: tuple, sides: list, opener=None) -> dict:
    return _call("POST", "/v1/combos", body={"legs": combo_legs(sides)}, creds=creds, opener=opener).get("combo") or {}


def request_quotes(creds: tuple, symbol: str, cash: float, opener=None) -> str:
    body = {"symbol": symbol, "cashOrderQty": f"{cash:.2f}", "restRemainder": False}
    return _call("POST", "/v1/rfqs", body=body, creds=creds, opener=opener).get("rfqId") or ""


def quotes_for(creds: tuple, rfq_id: str = "", quote_id: str = "", opener=None) -> list:
    query = {"rfqId": rfq_id, "quoteId": quote_id or None}   # the API wants the rfqId either way
    return _call("GET", "/v1/rfqs/quotes", query, creds=creds, opener=opener).get("quotes") or []


def best_buy(quotes: list) -> Optional[dict]:
    """The quote that wins a requester buy: the lowest positive buyPrice (earliest on a tie)."""
    live = []
    for q in quotes:
        try:
            px = float(q.get("buyPrice") or 0)
        except (TypeError, ValueError):
            continue
        if q.get("status", "QUOTE_STATUS_ACTIVE") == "QUOTE_STATUS_ACTIVE" and 0 < px < 1 \
                and float(q.get("buyQtyDecimal") or 0) > 0:
            live.append((px, q.get("createdTime") or "", q.get("id") or "", q))
    return min(live, key=lambda t: t[:3])[3] if live else None


def close_rfq(creds: tuple, rfq_id: str, opener=None) -> None:
    try:
        _call("DELETE", f"/v1/rfqs/{rfq_id}", creds=creds, opener=opener)
    except Exception:
        pass


def _offer_from(q: dict, symbol: str, rfq_id: str, tick: float) -> dict:
    price, qty = float(q["buyPrice"]), float(q["buyQtyDecimal"])
    cost = round(qty * price + round(combo_fee(price, qty), 2), 2)
    return {"ok": True, "symbol": symbol, "rfq": rfq_id, "quote": q.get("id"), "price": price, "qty": qty,
            "cost": cost, "payout": round(qty, 2), "odds": combo_american(price), "tick": tick}


def combo_offer(creds: tuple, sides: list, cash: float, wait: float = 6.0, poll: float = 0.6,
                sleep=time.sleep, opener=None) -> dict:
    """Make the combo, ask market makers for a price on `cash` dollars (fee included) and wait
    up to `wait` seconds for quotes. {"ok", "symbol", "rfq", "quote", "price", "qty", "cost",
    "payout", "odds", "tick"} or {"ok": False, "msg"}."""
    try:
        combo = create_combo(creds, sides, opener)
    except PolymarketError as e:
        return {"ok": False, "msg": f"Polymarket won't make this parlay: {e.message}"}
    symbol = combo.get("id")
    if not symbol:
        return {"ok": False, "msg": "Polymarket didn't return the parlay."}
    if combo.get("state") not in (None, "", "INSTRUMENT_STATE_OPEN"):
        return {"ok": False, "msg": "Polymarket isn't taking this parlay right now (a leg is closed or suspended)."}
    tick = float(combo.get("tickSize") or 0.001)
    rfq = request_quotes(creds, symbol, cash, opener)
    if not rfq:
        return {"ok": False, "msg": "Polymarket didn't open a quote request."}
    best, waited = None, 0.0
    while waited < wait:
        sleep(poll)
        waited += poll
        best = best_buy(quotes_for(creds, rfq, opener=opener))
        if best:   # one more look: other makers answer within a moment
            sleep(poll)
            best = best_buy(quotes_for(creds, rfq, opener=opener)) or best
            break
    if not best:
        close_rfq(creds, rfq, opener)
        return {"ok": False, "msg": "No market maker priced this parlay just now. Try again in a minute, "
                                    "or build it in the Polymarket app."}
    return dict(_offer_from(best, symbol, rfq, tick), cash=round(cash, 2))


def order(creds: tuple, order_id: str, opener=None) -> dict:
    return _call("GET", f"/v1/order/{order_id}", creds=creds, opener=opener).get("order") or {}


DONE_STATES = ("ORDER_STATE_FILLED", "ORDER_STATE_CANCELED", "ORDER_STATE_REJECTED", "ORDER_STATE_EXPIRED")


def _amount(x) -> float:
    if isinstance(x, dict):
        x = x.get("value")
    try:
        return float(x or 0)
    except (TypeError, ValueError):
        return 0.0


def combo_buy(creds: tuple, offer: dict, sides: list, wait: float = 10.0, poll: float = 0.5,
              sleep=time.sleep, opener=None) -> dict:
    """Take the confirmed parlay price: the best live quote on the request, if it's no worse
    than what was confirmed (half a tick), else stop and report the new price. Then accept,
    wait for the maker's last look and our order's fill. {"ok", "qty", "price", "cost",
    "odds", "symbol"} / {"ok": False, "msg", "moved": new offer}."""
    best = best_buy(quotes_for(creds, offer["rfq"], opener=opener))
    if best is None:   # quotes pulled or the request closed: ask again for the same amount
        fresh = combo_offer(creds, sides, offer.get("cash") or offer["cost"], sleep=sleep, opener=opener)
        if not fresh.get("ok"):
            return fresh
        offer_now = fresh
    else:
        offer_now = _offer_from(best, offer["symbol"], offer["rfq"], offer["tick"])
    if offer_now["price"] > offer["price"] + offer["tick"] / 2:
        return {"ok": False, "moved": offer_now,
                "msg": f"The price moved to {offer_now['price'] * 100:.1f}¢ ({offer_now['odds']:+d}); "
                       f"you confirmed {offer['price'] * 100:.1f}¢."}
    rfq, qid = offer_now["rfq"], offer_now["quote"]
    try:
        _call("PUT", f"/v1/rfqs/{rfq}/quotes/{qid}/accept", body={"acceptedSide": "SIDE_BUY"},
              creds=creds, opener=opener)
    except PolymarketError as e:
        return {"ok": False, "msg": f"Polymarket refused it: {e.message}"}
    q, waited = {}, 0.0
    while waited < wait:
        sleep(poll)
        waited += poll
        q = next((x for x in quotes_for(creds, rfq, quote_id=qid, opener=opener) if x.get("id") == qid), None) or q
        if q.get("status") in ("QUOTE_STATUS_EXECUTED", "QUOTE_STATUS_DELETED"):
            break
    pending = {"ok": False, "pending": True, "symbol": offer_now["symbol"],
               "msg": "Sent, but Polymarket hasn't reported the fill yet. If it filled, it shows up in your "
                      "bets within 10 minutes (synced from your account)."}
    if q.get("status") == "QUOTE_STATUS_DELETED":
        return {"ok": False, "msg": "The market maker pulled the price at the last second (it happens). "
                                    "Nothing was bought; tap Buy again for a new price."}
    oid = q.get("rfqCreatorOrderId")
    if q.get("status") != "QUOTE_STATUS_EXECUTED" or not oid:
        return pending
    o, waited = {}, 0.0
    while waited < wait:
        try:
            o = order(creds, oid, opener)
        except PolymarketError:
            o = {}
        if o.get("state") in DONE_STATES or (_amount(o.get("cumQuantity")) and not _amount(o.get("leavesQuantity"))):
            break
        sleep(poll)
        waited += poll
    qty = _amount(o.get("cumQuantity"))
    if not o or (not qty and o.get("state") not in DONE_STATES):
        return pending
    if not qty:
        return {"ok": False, "msg": "No fill: the order came back unfilled. Nothing was bought."}
    price = _amount(o.get("avgPx")) or offer_now["price"]
    fee = _amount(o.get("commissionNotionalTotalCollected")) or round(combo_fee(price, qty), 2)
    cost = round(qty * price + fee, 2)
    dec = max(qty / cost, 1.0001)
    odds = int(round((dec - 1) * 100)) if dec >= 2 else int(round(-100 / (dec - 1)))
    return {"ok": True, "qty": qty, "price": price, "cost": cost, "odds": odds, "symbol": offer_now["symbol"]}


# ---- reading a combo you hold (labels and settlement for synced positions) ---------------------
def is_combo_symbol(symbol: str) -> bool:
    return (symbol or "").startswith(("crossd-", "caoc-"))


def combo_legs_of(creds: tuple, symbol: str, opener=None) -> list:
    """[{"symbol", "side"}] for a combo you hold (GET /v1/combos?symbol=, exact symbol)."""
    d = _call("GET", "/v1/combos", {"symbol": symbol}, creds=creds, opener=opener)
    combos = d.get("combos") or []
    return [{"symbol": l.get("symbol"), "side": l.get("side")} for l in (combos[0].get("legs") if combos else []) or []
            if l.get("symbol")]


def market_by_slug(slug: str) -> dict:
    return _cached(f"market|{slug}", 600, lambda: _call("GET", f"/v1/market/slug/{slug}").get("market") or {})


def _teams_from_slug(slug: str) -> str:
    parts = (slug or "").lower().split("-")
    tokens = {pre.strip("-") for pres in LEAGUES.values() for pre in pres}
    i = next((k for k, p in enumerate(parts) if p in tokens), None)
    return "/".join(p.upper() for p in parts[i + 1:i + 3]) if i is not None else ""


def leg_text(m: dict, long: bool, slug: str = "") -> str:
    """A combo leg in words: 'Cleveland Cavaliers ML', 'Chicago Bulls +8.50', 'Under 226.5 (CHI/CLE)',
    'Donovan Mitchell 3+ three-pointers made' (or 'No: ...')."""
    side = next((s for s in m.get("marketSides") or [] if bool(s.get("long")) == long), {}) or {}
    desc = (side.get("description") or "").strip()
    team = ((side.get("team") or {}).get("name") or "").strip()
    title = (m.get("title") or m.get("question") or slug or m.get("slug") or "").strip()
    typ = (m.get("sportsMarketType") or "").lower()
    if desc.lower() in ("yes", "no") or not desc:
        return title if (desc.lower() != "no" and long) or desc.lower() == "yes" else f"No: {title}"
    if typ.endswith("_winner"):
        return f"{team or desc} ML"
    if typ.endswith("_spread"):
        return f"{team} {desc}" if team else f"{title} {desc}"
    if typ.endswith("_total"):
        line = m.get("line")
        teams = _teams_from_slug(slug or m.get("slug") or "")
        return f"{desc} {line:g}".strip() + (f" ({teams})" if teams else "") if isinstance(line, (int, float)) \
            else f"{title} · {desc}"
    return f"{title} · {desc}" if desc.lower() not in title.lower() else title
