"""PropLine (prop-line.com): a second odds feed, used where The Odds API has nothing.

Set the key with /set PROPLINE_API_KEY (kept on the server, never in GitHub). Without a key this
module does nothing. Hobby plan ($9/mo): 5,000 requests a day, reset at 00:00 UTC; every
response carries the quota (X-Daily-Used / X-Daily-Limit), shown in /status.

What the bot uses it for:
  - UFC fighter props (significant strikes, takedowns, fight time), which The Odds API doesn't
    carry for MMA: ufc_props.propline_lines.
  - Same-game parlay prices at FanDuel's and DraftKings' own correlated odds (POST .../sgp),
    instead of the bot's estimate: parlay.refine_sgp_prices.
  - /propline: how its lines compare with The Odds API's on the same games.

API (v1, The Odds API's response shape): base https://api.prop-line.com/v1, key as the apiKey
query parameter; /sports/{key}/events, /sports/{key}/events/{id}/odds?markets=&bookmakers=
(American prices; props as Over / Under outcomes with the player in `description`).
"""
import datetime as dt
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

log = logging.getLogger("linemaker.propline")
BASE = "https://api.prop-line.com/v1"
SPORT_KEYS = {"NBA": "basketball_nba", "WNBA": "basketball_wnba", "NCAAB": "basketball_ncaab",
              "NFL": "football_nfl", "NCAAF": "football_ncaaf", "NHL": "hockey_nhl",
              "MLB": "baseball_mlb", "UFC": "mma_ufc"}
EVENTS_TTL = 600        # seconds an events list is reused
ODDS_TTL = 300          # seconds one event's odds are reused
_CACHE = {}             # key -> (time, data)
_LOCK = threading.Lock()
_QUOTA = {}             # the latest X-Daily-* headers


class PropLineError(RuntimeError):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(f"PropLine {status} {code}: {message}"[:300])
        self.status, self.code = status, code


def api_key() -> str:
    return os.environ.get("PROPLINE_API_KEY", "").strip()


def on() -> bool:
    return bool(api_key())


def _note_quota(headers) -> None:
    for name in ("X-Daily-Limit", "X-Daily-Used", "X-Daily-Remaining"):
        v = headers.get(name)
        if v is not None:
            try:
                _QUOTA[name] = int(v)
            except ValueError:
                pass


def usage_line() -> str:
    if not _QUOTA:
        return "PropLine: key set, no requests yet since the last restart"
    return (f"PropLine: {_QUOTA.get('X-Daily-Used', '?')} of {_QUOTA.get('X-Daily-Limit', '?')} requests "
            "used today (resets 8 PM ET / midnight UTC)")


def _call(path: str, params: Optional[dict] = None, body: Optional[dict] = None,
          opener=None, timeout: float = 20):
    opener = opener or urllib.request.urlopen
    key = api_key()
    if not key:
        raise PropLineError(0, "missing_api_key", "set PROPLINE_API_KEY with /set")
    query = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v not in (None, "")})
    url = f"{BASE}{path}" + (f"?{query}" if query else "")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if body is not None else "GET",
                                 headers={"X-API-Key": key, "Accept": "application/json",
                                          "Content-Type": "application/json", "User-Agent": "Linemaker/1.0"})
    for attempt in range(3):
        try:
            with opener(req, timeout=timeout) as resp:
                _note_quota(resp.headers)
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            _note_quota(e.headers or {})
            try:
                detail = json.loads(e.read() or b"{}")
            except ValueError:
                detail = {}
            info = detail.get("detail", detail) if isinstance(detail, dict) else {}
            info = info if isinstance(info, dict) else {"message": str(info)}
            code = info.get("error") or str(e.code)
            if e.code == 429 and code != "daily_limit_exceeded" and attempt < 2:
                wait = float((e.headers or {}).get("Retry-After") or info.get("retry_after_seconds") or 2)
                time.sleep(min(wait, 10))
                continue
            raise PropLineError(e.code, code, info.get("message") or e.reason) from None
    raise PropLineError(429, "burst_limit_exceeded", "rate limited")


def _cached(key: str, ttl: float, fetch):
    with _LOCK:
        hit = _CACHE.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
    data = fetch()
    with _LOCK:
        _CACHE[key] = (time.time(), data)
    return data


def sport_key(sport: str) -> Optional[str]:
    return SPORT_KEYS.get(sport.upper())


def events(sport: str) -> list:
    """Upcoming events of a league (cached 10 minutes)."""
    key = sport_key(sport)
    if not key:
        return []
    return _cached(f"events|{key}", EVENTS_TTL, lambda: _call(f"/sports/{key}/events")) or []


def event_odds(sport: str, event_id, markets: str, bookmakers: str = "", ids: bool = False) -> dict:
    """One event's odds for the given markets (cached 5 minutes). ids: the books' own ids and
    event links too (for betslip links)."""
    key = sport_key(sport)
    extra = {"includeBookIds": "true", "includeLinks": "true"} if ids else {}
    return _cached(f"odds|{key}|{event_id}|{markets}|{bookmakers}|{ids}", ODDS_TTL,
                   lambda: _call(f"/sports/{key}/events/{event_id}/odds",
                                 dict({"markets": markets, "bookmakers": bookmakers}, **extra))) or {}


def freshness() -> dict:
    return _call("/freshness")


def _norm(name: str) -> str:
    from .oddsapi import norm
    return norm(name or "")


def find_event(sport: str, home: str, away: str, commence: str = "") -> Optional[dict]:
    """PropLine's event for a game we know by team names (either way round) and start time."""
    want = {_norm(home), _norm(away)}
    try:
        when = dt.datetime.fromisoformat(commence.replace("Z", "+00:00")) if commence else None
    except ValueError:
        when = None
    best = None
    for ev in events(sport):
        if {_norm(ev.get("home_team")), _norm(ev.get("away_team"))} != want:
            continue
        if when and ev.get("commence_time"):
            try:
                gap = abs((dt.datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00")) - when).total_seconds())
            except ValueError:
                gap = 0
            if gap > 8 * 3600:
                continue
            if best is None or gap < best[0]:
                best = (gap, ev)
        else:
            return ev
    return best[1] if best else None


def sgp_quote(sport: str, event_id, legs: list, bookmaker: str) -> dict:
    """The book's own price for a same-game slip: {"quoted", "sgp_price", "sgp_price_decimal",
    "independent_price", "correlation_factor", "legs": [...]} (Hobby plan)."""
    key = sport_key(sport)
    return _call(f"/sports/{key}/events/{event_id}/sgp", body={"bookmaker": bookmaker, "legs": legs},
                 timeout=12)


# ---- side by side with The Odds API (/propline) ---------------------------------------------
def _prices(event: dict, books: set) -> dict:
    """{(book, player, side, point): American price} from one event's odds (either feed)."""
    out = {}
    for bk in (event or {}).get("bookmakers", []):
        key = (bk.get("key") or "").lower()
        if key not in books:
            continue
        for mk in bk.get("markets", []):
            for o in mk.get("outcomes", []):
                if o.get("price") is None:
                    continue
                out[(key, _norm(o.get("description") or ""), (o.get("name") or "").lower(),
                     o.get("point"))] = int(o["price"])
    return out


def _imp(odds: int) -> float:
    return 100 / (odds + 100) if odds > 0 else -odds / (-odds + 100)


def compare(odds_api_event: dict, propline_event: dict, books=("fanduel", "draftkings")) -> dict:
    """How the two feeds' lines agree on one event: {"matched", "same", "avg_gap", "only_oa",
    "only_pl", "worst": [(key, oa price, pl price)], "by_book": {book: (matched, same)}}."""
    a, b = _prices(odds_api_event, set(books)), _prices(propline_event, set(books))
    both = sorted(set(a) & set(b))
    gaps = [(abs(_imp(a[k]) - _imp(b[k])), k) for k in both]
    by_book = {}
    for k in both:
        m, s = by_book.get(k[0], (0, 0))
        by_book[k[0]] = (m + 1, s + (a[k] == b[k]))
    worst = [(k, a[k], b[k]) for _g, k in sorted(gaps, reverse=True)[:5] if a[k] != b[k]]
    return {"matched": len(both), "same": sum(a[k] == b[k] for k in both),
            "avg_gap": sum(g for g, _k in gaps) / len(gaps) if gaps else 0.0,
            "only_oa": len(set(a) - set(b)), "only_pl": len(set(b) - set(a)), "worst": worst,
            "by_book": by_book}


def freshness_lines(data, books=("fanduel", "draftkings", "kalshi", "prizepicks")) -> list:
    """Staleness per book from /v1/freshness, read defensively (props class when present)."""
    rows = data.get("bookmakers") if isinstance(data, dict) else data
    if isinstance(rows, dict):
        rows = [dict(v, key=k) for k, v in rows.items() if isinstance(v, dict)]
    out = []
    for r in rows or []:
        key = (r.get("key") or r.get("bookmaker") or "").lower()
        if key not in books:
            continue
        classes = r.get("market_classes") or {}
        props = classes.get("props") if isinstance(classes, dict) else None
        src = props if isinstance(props, dict) else r
        age = src.get("staleness_seconds")
        if age is not None:
            out.append(f"{key}: props {int(age) // 60} min old" + (" ⚠️ stale" if src.get("is_stale") else ""))
    return out
