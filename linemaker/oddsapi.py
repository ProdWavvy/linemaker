"""The Odds API (v4) feed: live prices -> pro-engine markets.

The API supplies prices only. Projections come from a separate CSV (see README) and are
joined on sport + subject + market; the line comes from the books. Game lines use the home
team as subject (moneyline, spread, game total / total goals, match result). Every distinct line
becomes its own market, priced by the books that post it.

Credits (per the v4 guide): /odds and event-odds cost markets x regions, counting only
markets that come back with data (empty responses are free); up to 10 `bookmakers` count as
one region. /sports and /events are free. /scores costs 1, or 2 with daysFrom. Remaining
credits are read from the x-requests-remaining header after every call. 429 = rate limited:
retried after a few seconds.
"""
import datetime as dt
import json
import logging
import os
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Optional
from zoneinfo import ZoneInfo

BASE = "https://api.the-odds-api.com/v4"
log = logging.getLogger("linemaker.odds")

SPORT_KEYS = {
    "NBA": "basketball_nba", "WNBA": "basketball_wnba", "NCAAB": "basketball_ncaab",
    "NHL": "icehockey_nhl", "NFL": "americanfootball_nfl", "NCAAF": "americanfootball_ncaaf",
    "MLB": "baseball_mlb", "UCL": "soccer_uefa_champs_league", "LALIGA": "soccer_spain_la_liga",
    "EPL": "soccer_epl", "MLS": "soccer_usa_mls",
}
SOCCER = {"UCL", "LALIGA", "EPL", "MLS"}

# Odds API market key -> engine market name, per sport family.
PROP_MARKETS = {
    "basketball": {
        "player_points": "points", "player_rebounds": "rebounds", "player_assists": "assists",
        "player_threes": "threes", "player_points_rebounds_assists": "PRA",
    },
    "americanfootball": {
        "player_pass_yds": "passing yards", "player_rush_yds": "rushing yards",
        "player_reception_yds": "receiving yards", "player_receptions": "receptions",
        "player_reception_longest": "longest reception", "player_anytime_td": "anytime td",
        "player_pass_tds": "passing tds", "player_pass_completions": "pass completions",
        "player_pass_attempts": "pass attempts", "player_rush_attempts": "rush attempts",
        "player_rush_longest": "longest rush",
    },
    "icehockey": {
        "player_shots_on_goal": "shots on goal", "player_points": "hockey points",
        "player_goal_scorer_anytime": "anytime goals",
    },
    "baseball": {
        "pitcher_strikeouts": "strikeouts", "batter_hits": "hits",
        "batter_home_runs": "home runs", "batter_total_bases": "total bases",
    },
}
YES_NO_MARKETS = {"player_anytime_td", "player_goal_scorer_anytime"}
# Alternate-line ladders (same stat, many lines). Off by default: each is billed as its own
# market per game, so they roughly double prop credits. LINEMAKER_ALT_LINES=on to include.
ALT_MARKETS = {
    "basketball": {
        "player_points_alternate": "points", "player_rebounds_alternate": "rebounds",
        "player_assists_alternate": "assists", "player_threes_alternate": "threes",
        "player_points_rebounds_assists_alternate": "PRA",
    },
    "americanfootball": {
        "player_pass_yds_alternate": "passing yards", "player_rush_yds_alternate": "rushing yards",
        "player_reception_yds_alternate": "receiving yards",
        "player_receptions_alternate": "receptions",
    },
    "icehockey": {
        "player_shots_on_goal_alternate": "shots on goal", "player_points_alternate": "hockey points",
    },
    "baseball": {
        "pitcher_strikeouts_alternate": "strikeouts", "batter_hits_alternate": "hits",
        "batter_total_bases_alternate": "total bases",
    },
}


# Always plus-money: they can never clear a favorites-only odds window, so they aren't bought
# when LINEMAKER_ODDS_MIN is negative (LINEMAKER_SKIP_LONGSHOTS=off buys them anyway).
LONGSHOT_MARKETS = {"batter_home_runs", "player_goal_scorer_anytime"}


def favorites_only() -> bool:
    if os.environ.get("LINEMAKER_SKIP_LONGSHOTS", "auto").strip().lower() in ("off", "0", "no", "false"):
        return False
    try:
        return int(os.environ.get("LINEMAKER_ODDS_MIN", "").strip()) < 0
    except ValueError:
        return False


def skip_started() -> bool:
    return os.environ.get("LINEMAKER_SKIP_STARTED", "on").strip().lower() not in ("off", "0", "no", "false")


def alt_lines_on() -> bool:
    return os.environ.get("LINEMAKER_ALT_LINES", "off").lower() in ("on", "1", "true", "yes")


class OddsAPIError(RuntimeError):
    pass


@dataclass
class Quota:
    remaining: Optional[int] = None
    used: Optional[int] = None
    last_cost: Optional[int] = None


@dataclass
class FeedMarket:
    """One priced market from the feed, before a projection is attached."""
    date: str
    sport: str
    game_id: str
    game: str
    commence: str
    bet_type: str             # prop, total, spread, moneyline, 1x2
    subject: str
    opponent: str
    market: str
    line: float
    books: list = field(default_factory=list)   # [(book title, [odds per side])]
    links: dict = field(default_factory=dict)   # book -> [{"link", "msid", "sid"} | None per side]
    updated: dict = field(default_factory=dict)  # book -> ISO time the price last changed
    multipliers: dict = field(default_factory=dict)  # book -> [payout multiplier per side] (pick'em)


def configured_keys() -> list:
    """ODDS_API_KEY, then ODDS_API_KEY_2, ODDS_API_KEY_3, ... (or a comma list in ODDS_API_KEYS)."""
    keys = [k.strip() for k in os.environ.get("ODDS_API_KEYS", "").split(",") if k.strip()]
    if not keys:
        keys = [os.environ.get("ODDS_API_KEY", "").strip()]
        n = 2
        while os.environ.get(f"ODDS_API_KEY_{n}", "").strip():
            keys.append(os.environ[f"ODDS_API_KEY_{n}"].strip())
            n += 1
    return [k for k in keys if k]


# Which configured key is in use, shared by every client so an exhausted key stays skipped.
_ACTIVE_KEY = {"index": 0}


class OddsAPI:
    def __init__(self, api_key: Optional[str] = None, regions: str = "us",
                 bookmakers: Optional[str] = None, timeout: float = 20):
        self.keys = [api_key] if api_key else configured_keys()
        if not self.keys:
            raise OddsAPIError("Set ODDS_API_KEY to your The Odds API key.")
        self.regions = regions
        self.bookmakers = bookmakers or os.environ.get("ODDS_API_BOOKMAKERS") or None
        self.timeout = timeout
        self.retry_wait = 2.0
        self.quota = Quota()
        self.slate = {}   # sport -> [(game label, commence)] for every game on the date

    @property
    def api_key(self) -> str:
        return self.keys[min(_ACTIVE_KEY["index"], len(self.keys) - 1)]

    def _next_key(self, reason: str) -> bool:
        if _ACTIVE_KEY["index"] + 1 >= len(self.keys):
            return False
        _ACTIVE_KEY["index"] += 1
        log.warning("odds api key #%d %s: switching to key #%d", _ACTIVE_KEY["index"], reason,
                    _ACTIVE_KEY["index"] + 1)
        return True

    def _get(self, path: str, **params):
        params = {k: v for k, v in params.items() if v is not None}
        started = time.time()
        attempt = 0
        while True:
            url = f"{BASE}{path}?{urllib.parse.urlencode({**params, 'apiKey': self.api_key})}"
            try:
                with urllib.request.urlopen(url, timeout=self.timeout) as resp:
                    h = resp.headers
                    body = resp.read()
                break
            except urllib.error.HTTPError as e:
                detail = e.read().decode(errors="replace")[:300]
                if e.code == 429 and attempt < 3:
                    log.warning("odds api %s: rate limited, retrying", path)
                    time.sleep(self.retry_wait * 2 ** attempt)
                    attempt += 1
                    continue
                # 401 = key out of credits, invalid or disabled: try the next configured key.
                if e.code == 401 and self._next_key(f"refused ({detail[:80]})"):
                    continue
                log.warning("odds api %s: HTTP %s", path, e.code)
                raise OddsAPIError(f"HTTP {e.code} from The Odds API: {detail}") from None
        log.info("odds api %s: %.1fs", path, time.time() - started)
        for attr, header in (("remaining", "x-requests-remaining"), ("used", "x-requests-used"),
                             ("last_cost", "x-requests-last")):
            if h.get(header) is not None:
                setattr(self.quota, attr, int(float(h[header])))
        if self.quota.remaining is not None and self.quota.remaining <= 0:
            self._next_key("is out of credits")
        return json.loads(body)

    def sports(self) -> list:
        """Free: in-season sports."""
        return self._get("/sports")

    def events(self, sport: str, commence_from: Optional[str] = None,
               commence_to: Optional[str] = None) -> list:
        """Free: event ids, teams and start times, no odds."""
        return self._get(f"/sports/{sport_key(sport)}/events", dateFormat="iso",
                         commenceTimeFrom=commence_from, commenceTimeTo=commence_to)

    def scores(self, sport: str, days_from: Optional[int] = 3) -> list:
        """Live and recently completed games with scores (2 credits with daysFrom)."""
        return self._get(f"/sports/{sport_key(sport)}/scores", daysFrom=days_from,
                         dateFormat="iso")

    def _price_params(self, markets: str) -> dict:
        p = {"markets": markets, "oddsFormat": "american", "dateFormat": "iso"}
        from .betlinks import links_on
        if links_on():   # betslip deep links for the Bet buttons
            p["includeLinks"] = "true"
            p["includeSids"] = "true"
        if getattr(self, "include_multipliers", False):
            p["includeMultipliers"] = "true"   # pick'em payout multipliers (goblins / demons)
        if self.bookmakers:
            p["bookmakers"] = self.bookmakers   # overrides regions
        else:
            p["regions"] = self.regions
        return p

    def game_odds(self, sport: str, markets: str = "h2h,spreads,totals",
                  commence_from: Optional[str] = None, commence_to: Optional[str] = None) -> list:
        return self._get(f"/sports/{sport_key(sport)}/odds", **self._price_params(markets),
                         commenceTimeFrom=commence_from, commenceTimeTo=commence_to)

    def event_odds(self, sport: str, event_id: str, markets: str) -> dict:
        return self._get(f"/sports/{sport_key(sport)}/events/{event_id}/odds",
                         **self._price_params(markets))


def sport_key(sport: str) -> str:
    s = sport.upper()
    if s in SPORT_KEYS:
        return SPORT_KEYS[s]
    if "_" in sport:
        return sport
    raise OddsAPIError(f"unknown sport {sport!r}; use one of {', '.join(SPORT_KEYS)}")


def default_prop_markets(sport: str) -> list:
    family = sport_key(sport).split("_")[0]
    keys = list(PROP_MARKETS.get(family, {}))
    if alt_lines_on():
        keys += list(ALT_MARKETS.get(family, {}))
    if favorites_only():
        keys = [k for k in keys if k not in LONGSHOT_MARKETS]
    return keys


def slate_window(date: str, tz: ZoneInfo) -> tuple:
    """UTC commence window covering the local calendar day."""
    start = dt.datetime.combine(dt.date.fromisoformat(date), dt.time(0), tz)
    end = start + dt.timedelta(days=1)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return (start.astimezone(dt.timezone.utc).strftime(fmt),
            end.astimezone(dt.timezone.utc).strftime(fmt))


def _local_date(iso: str, tz: ZoneInfo) -> str:
    return dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(tz).date().isoformat()


def _game(event: dict) -> str:
    return f"{event['away_team']} @ {event['home_team']}"


class _Grouper:
    """Collects per-book prices into one FeedMarket per (event, subject, market, line)."""

    def __init__(self):
        self.markets = {}

    def add(self, base: dict, key: tuple, book: str, side: int, n_sides: int, price: int,
            market: Optional[dict] = None, outcome: Optional[dict] = None,
            updated: Optional[str] = None):
        fm = self.markets.get(key)
        if fm is None:
            fm = self.markets[key] = FeedMarket(**base)
        stamp = (market or {}).get("last_update") or updated
        if stamp:
            fm.updated[book] = stamp
        if outcome and outcome.get("multiplier") is not None:
            mults = fm.multipliers.setdefault(book, [None] * n_sides)
            try:
                mults[side] = float(outcome["multiplier"])
            except (TypeError, ValueError):
                pass
        if outcome and (outcome.get("link") or outcome.get("sid")):
            slots = fm.links.setdefault(book, [None] * n_sides)
            slots[side] = {"link": outcome.get("link") or "", "sid": outcome.get("sid") or "",
                           "msid": (market or {}).get("sid") or ""}
        for b, prices in fm.books:
            if b == book:
                prices[side] = price
                return
        prices = [None] * n_sides
        prices[side] = price
        fm.books.append((book, prices))


def parse_game_odds(events: list, sport: str, tz: ZoneInfo) -> list:
    sport_u = sport.upper()
    g = _Grouper()
    for ev in events:
        home, away = ev["home_team"], ev["away_team"]
        common = dict(date=_local_date(ev["commence_time"], tz), sport=sport_u, game_id=ev["id"],
                      game=_game(ev), commence=ev["commence_time"])
        for bk in ev.get("bookmakers", []):
            book = bk.get("title") or bk["key"]
            for mk in bk.get("markets", []):
                outs = mk.get("outcomes", [])
                if mk["key"] == "h2h":
                    three = any(o["name"] == "Draw" for o in outs) or sport_u in SOCCER
                    order = [home, "Draw", away] if three else [home, away]
                    base = dict(common, bet_type="1x2" if three else "moneyline", subject=home,
                                opponent=away, market="match result" if three else "moneyline",
                                line=0.0)
                    for o in outs:
                        if o["name"] in order:
                            g.add(base, (ev["id"], "h2h", 0.0), book, order.index(o["name"]),
                                  len(order), o["price"], mk, o, bk.get("last_update"))
                elif mk["key"] == "spreads":
                    home_pt = next((o["point"] for o in outs if o["name"] == home), None)
                    if home_pt is None:
                        continue
                    base = dict(common, bet_type="spread", subject=home, opponent=away,
                                market="spread", line=float(home_pt))
                    for o in outs:
                        side = 0 if o["name"] == home else 1
                        g.add(base, (ev["id"], "spread", float(home_pt)), book, side, 2, o["price"],
                              mk, o, bk.get("last_update"))
                elif mk["key"] == "totals":
                    for o in outs:
                        pt = float(o["point"])
                        base = dict(common, bet_type="total", subject=home, opponent=away,
                                    market="game total" if sport_u not in SOCCER else "total goals",
                                    line=pt)
                        side = 0 if o["name"] == "Over" else 1
                        g.add(base, (ev["id"], "total", pt), book, side, 2, o["price"], mk, o,
                              bk.get("last_update"))
    return list(g.markets.values())


def parse_event_props(event: dict, sport: str, tz: ZoneInfo) -> list:
    family = sport_key(sport).split("_")[0]
    names = {**PROP_MARKETS.get(family, {}), **ALT_MARKETS.get(family, {})}
    common = dict(date=_local_date(event["commence_time"], tz), sport=sport.upper(),
                  game_id=event["id"], game=_game(event), commence=event["commence_time"],
                  bet_type="prop", opponent="")
    g = _Grouper()
    for bk in event.get("bookmakers", []):
        book = bk.get("title") or bk["key"]
        for mk in bk.get("markets", []):
            if mk["key"] not in names:
                continue
            yes_no = mk["key"] in YES_NO_MARKETS
            for o in mk.get("outcomes", []):
                player = o.get("description") or o["name"]
                if yes_no:
                    line, side = 0.5, 0 if o["name"] in ("Yes", "Over") else 1
                else:
                    if o.get("point") is None:
                        continue
                    line, side = float(o["point"]), 0 if o["name"] == "Over" else 1
                base = dict(common, subject=player, market=names[mk["key"]], line=line)
                g.add(base, (event["id"], player, mk["key"], line), book, side, 2, o["price"],
                      mk, o, bk.get("last_update"))
    return list(g.markets.values())


def fetch_slate(api: OddsAPI, sport: str, date: str, tz: ZoneInfo, props: bool = False,
                prop_markets: Optional[list] = None, max_events: Optional[int] = None,
                game_markets: str = "h2h,spreads,totals", game_lines: bool = True,
                only: Optional[set] = None) -> list:
    """All priced markets for one sport on one local date. With game_lines=False the
    event list comes from the free /events endpoint instead of a paid /odds call."""
    start, end = slate_window(date, tz)
    source = (api.game_odds(sport, game_markets, start, end) if game_lines
              else api.events(sport, start, end))
    events = [e for e in source if _local_date(e["commence_time"], tz) == date]
    if only is not None:   # a single-game scan (pre-tip, injury news)
        events = [e for e in events if e["id"] in only]
    if hasattr(api, "slate"):
        api.slate[sport.upper()] = [(_game(e), e["commence_time"]) for e in events]
    out = parse_game_odds(events, sport, tz) if game_lines else []
    if props:
        keys = ",".join(prop_markets or default_prop_markets(sport))
        from concurrent.futures import ThreadPoolExecutor
        chosen = events[:max_events] if max_events else events
        if skip_started():   # live props can't be bet pre-game; don't pay for them
            now = dt.datetime.now(dt.timezone.utc)
            chosen = [e for e in chosen
                      if dt.datetime.fromisoformat(e["commence_time"].replace("Z", "+00:00")) > now]
        with ThreadPoolExecutor(max_workers=4) as pool:   # a few at a time; 429s are retried
            payloads = list(pool.map(lambda ev: api.event_odds(sport, ev["id"], keys), chosen))
        for payload in payloads:
            out += parse_event_props(payload, sport, tz)
    return out


# ---- joining projections to the feed -------------------------------------------------

def norm(name: str) -> str:
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z0-9 ]", "", s)
    s = re.sub(r"\b(jr|sr|ii|iii|iv)\b", "", s)
    return " ".join(s.split())


def proj_key(sport: str, subject: str, market: str) -> tuple:
    return (sport.upper(), norm(subject), market.strip().lower())


# Projection columns copied straight into the pro slate row.
PROJ_COLUMNS = (
    "lineup_confirmed", "position", "mean", "mean_b", "stdev", "dist", "season_games",
    "opp_season", "opp_recent", "opp_recent_games", "opp_override", "rate_season",
    "rate_recent", "rate_recent_games", "rate_expected", "matchup_factor", "pace_factor",
    "venue_factor", "rest_factor", "news_fresh", "injury_status", "notes",
)
SLATE_COLUMNS = ("date", "sport", "game_id", "game", "commence", "bet_type", "subject",
                 "opponent", "market", "line", "books") + PROJ_COLUMNS + ("links",)


def books_str(books: list) -> str:
    def f(o):
        return "" if o is None else (f"+{o}" if o > 0 else str(o))
    return "|".join(f"{b.replace(':', ' ').replace('|', ' ')}:" + "/".join(f(o) for o in p)
                    for b, p in books)


def feed_row(fm: FeedMarket) -> dict:
    row = {c: "" for c in SLATE_COLUMNS}
    row.update(date=fm.date, sport=fm.sport, game_id=fm.game_id, game=fm.game,
               commence=fm.commence,
               bet_type=fm.bet_type, subject=fm.subject, opponent=fm.opponent,
               market=fm.market, line=f"{fm.line:g}", books=books_str(fm.books),
               links=json.dumps(fm.links, separators=(",", ":")) if fm.links else "")
    return row


def _flip_to_home(fm: FeedMarket, p: dict) -> Optional[dict]:
    """A game-line projection written for the away team, restated for the home team
    (the feed's subject): margins are negated, 1x2 goal means are swapped."""
    q = {k: (v.strip() if isinstance(v, str) else v) for k, v in p.items()}
    line = (q.get("line") or "")
    if fm.bet_type in ("moneyline", "spread"):
        if q.get("mean"):
            q["mean"] = f"{-float(q['mean']):g}"
        if line:
            q["line"] = f"{-float(line):g}"
    elif fm.bet_type == "1x2":
        q["mean"], q["mean_b"] = q.get("mean_b", ""), q.get("mean", "")
    elif fm.bet_type != "total":
        return None
    return q


def merge(feed: list, projections: list) -> tuple:
    """(slate rows with a projection, projection rows that found no odds).
    Game-line projections may name either team; away-team ones are restated for home."""
    by_key = {}
    for p in projections:
        by_key.setdefault(proj_key(p["sport"], p["subject"], p["market"]), p)
    rows, used = [], set()
    for fm in feed:
        candidates = [(proj_key(fm.sport, fm.subject, fm.market), False)]
        if fm.bet_type != "prop" and fm.opponent:
            candidates.append((proj_key(fm.sport, fm.opponent, fm.market), True))
        for k, flipped in candidates:
            p = by_key.get(k)
            if p is None:
                continue
            if flipped:
                p = _flip_to_home(fm, p)
                if p is None:
                    continue
            line = (p.get("line") or "").strip()
            if line and float(line) != fm.line:
                continue   # projection was written for a specific line
            row = feed_row(fm)
            for c in PROJ_COLUMNS:
                if p.get(c):
                    row[c] = str(p[c]).strip()
            rows.append(row)
            used.add(k)
            break
    missing = [p for k, p in by_key.items() if k not in used]
    return rows, missing


# ---- results from /scores ------------------------------------------------------------

GAME_LINE_TYPES = ("moneyline", "spread", "total", "1x2")


def results_from_scores(scores: list, records: dict) -> list:
    """Results rows (grade() format) for frozen game-line records whose games are final.
    `actual` is the subject's margin (moneyline/spread/1x2) or the combined score (totals)."""
    final = {s["id"]: s for s in scores if s.get("completed") and s.get("scores")}
    rows = []
    for rec in records.values():
        bet_type = rec["inputs"].get("bet_type")
        game = final.get(rec["game_id"])
        if game is None or bet_type not in GAME_LINE_TYPES:
            continue
        pts = {norm(x["name"]): float(x["score"]) for x in game["scores"]}
        if len(pts) != 2:
            continue
        if bet_type == "total":
            actual = sum(pts.values())
        else:
            mine = pts.get(norm(rec["subject"]))
            if mine is None:
                continue
            actual = mine - next(v for k, v in pts.items() if k != norm(rec["subject"]))
        rows.append({"game_id": rec["game_id"], "subject": rec["subject"],
                     "market": rec["market"], "line": str(rec["inputs"]["line"]),
                     "actual": f"{actual:g}"})
    return rows


def key_quotas() -> list:
    """[(key number, masked key, remaining, used, error, active)] for every configured key.
    Uses the free /sports endpoint, so checking costs no credits."""
    out = []
    for i, key in enumerate(configured_keys()):
        masked = f"…{key[-4:]}" if len(key) >= 4 else "…"
        api = OddsAPI(api_key=key)
        try:
            api.sports()
            out.append((i + 1, masked, api.quota.remaining, api.quota.used, None,
                        i == _ACTIVE_KEY["index"]))
        except OddsAPIError as e:
            out.append((i + 1, masked, None, None, str(e)[:80], i == _ACTIVE_KEY["index"]))
    return out
