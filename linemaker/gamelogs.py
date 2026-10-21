"""Player game logs from ESPN's public API -> hit rates against a prop line.

For each player: season-long rate (this season plus last, preseason excluded, most recent
82 games), last 5/10/15/20 windows, home/away split and versus tonight's opponent, each with
the number of games and the average. Free and keyless, but unofficial (see boxscores.py).
Covers NBA, WNBA, NFL, college football, NHL and MLB.
"""
import datetime as dt
import functools
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Optional

from .boxscores import _get, _num
from .oddsapi import norm

log = logging.getLogger("linemaker.gamelogs")
WEB = "https://site.web.api.espn.com/apis/common/v3/sports"
PATHS = {"NBA": "basketball/nba", "WNBA": "basketball/wnba", "NFL": "football/nfl",
         "NCAAF": "football/college-football", "NHL": "hockey/nhl", "MLB": "baseball/mlb"}
BASKETBALL = {"points": ("points",), "rebounds": ("totalRebounds",), "assists": ("assists",),
              "threes": ("threePointFieldGoalsMade",),
              "pra": ("points", "totalRebounds", "assists"),
              "pts + reb": ("points", "totalRebounds"), "pts + ast": ("points", "assists"),
              "reb + ast": ("totalRebounds", "assists")}
FOOTBALL = {"passing yards": ("passingYards",), "passing tds": ("passingTouchdowns",),
            "pass completions": ("completions",), "pass attempts": ("passingAttempts",),
            "rushing yards": ("rushingYards",), "rush attempts": ("rushingAttempts",),
            "longest rush": ("longRushing",), "receiving yards": ("receivingYards",),
            "receptions": ("receptions",), "longest reception": ("longReception",),
            "anytime td": ("rushingTouchdowns", "receivingTouchdowns")}
HOCKEY = {"shots on goal": ("shotsTotal",), "hockey points": ("points",),
          "anytime goals": ("goals",)}
BASEBALL = {"hits": ("hits",), "home runs": ("homeRuns",), "strikeouts": ("strikeouts",),
            "total bases": ("hits", "doubles", "triples", "triples", "homeRuns", "homeRuns",
                            "homeRuns")}   # 1B + 2*2B + 3*3B + 4*HR = H + 2B + 2*3B + 3*HR
SPORT_MARKETS = {"NBA": BASKETBALL, "WNBA": BASKETBALL, "NFL": FOOTBALL, "NCAAF": FOOTBALL,
                 "NHL": HOCKEY, "MLB": BASEBALL}
# Stats where a missing table means "didn't happen" (a WR with no carries ran for 0 yards).
ZERO_IF_MISSING = {"NFL", "NCAAF"}
WINDOWS = (5, 10, 15, 20)
SEASON_GAMES = 82
MIN_SEASON_GAMES = 15   # below this the season rate is too thin to veto a play


@dataclass
class Window:
    hits: int
    games: int
    avg: float

    @property
    def rate(self) -> float:
        return self.hits / self.games if self.games else 0.0


@dataclass
class HitProfile:
    season: Optional[Window] = None
    recent: dict = field(default_factory=dict)       # 5/10/15/20 -> Window
    venue: Optional[Window] = None                   # tonight's venue (home or road)
    venue_label: str = ""
    versus: Optional[Window] = None
    opponent: str = ""
    games: list = field(default_factory=list)        # [(date, opponent, home, value)] newest first
    line: float = 0.0
    over: bool = True


def _season_year(sport: str, date: str) -> int:
    """ESPN's season label: NBA/NHL by the year they end (2026-27 -> 2027), NFL and college
    football by the year they start, WNBA and MLB by calendar year."""
    d = dt.date.fromisoformat(date)
    if sport in ("NBA", "NHL"):
        return d.year + 1 if d.month >= 8 else d.year
    if sport in ("NFL", "NCAAF"):
        return d.year if d.month >= 3 else d.year - 1
    return d.year


def _games(sport: str, athlete_id: str, season: int) -> list:
    data = _get_web(f"{PATHS[sport]}/athletes/{athlete_id}/gamelog", season=season)
    names = data.get("names") or []
    meta = data.get("events") or {}
    out = []
    for stype in data.get("seasonTypes", []):
        if "preseason" in (stype.get("displayName") or "").lower():
            continue
        for cat in stype.get("categories", []):
            for ev in cat.get("events", []):
                info = meta.get(ev.get("eventId"), {})
                stats = {}
                for key, cell in zip(names, ev.get("stats", [])):
                    if "-" in key and isinstance(cell, str) and "-" in cell[1:]:
                        left, right = key.split("-", 1)
                        a, b = cell.split("-", 1)
                        stats[left], stats[right] = _num(a), _num(b)
                    else:
                        stats[key] = _num(cell)
                opp = info.get("opponent") or {}
                out.append({"date": info.get("gameDate", ""), "home": info.get("atVs") == "vs",
                            "opponent": opp.get("displayName", ""),
                            "abbr": opp.get("abbreviation", ""), "stats": stats})
    return out


def _get_web(path: str, **params) -> dict:
    import json
    import time
    import urllib.request
    query = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"{WEB}/{path}" + (f"?{query}" if query else "")
    req = urllib.request.Request(url, headers={"User-Agent": "Linemaker/1.0"})
    started = time.time()
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read())
    log.info("espn %s: %.1fs", path.rsplit("/", 2)[-2:], time.time() - started)
    return data


def game_log(sport: str, athlete_id: str, date: str) -> list:
    """Most recent games first, this season and last, up to SEASON_GAMES. Cached for the day:
    projections, injury adjustments and hit rates all read the same logs."""
    return [dict(g) for g in _game_log(sport, athlete_id, date)]


@functools.lru_cache(maxsize=2048)
def _game_log_cached(sport: str, athlete_id: str, date: str) -> tuple:
    return tuple(_game_log_fetch(sport, athlete_id, date))


def _game_log(sport: str, athlete_id: str, date: str) -> tuple:
    return _game_log_cached(sport, athlete_id, date)


def _game_log_fetch(sport: str, athlete_id: str, date: str) -> list:
    year = _season_year(sport, date)
    games = []
    for season in (year, year - 1):
        try:
            games += _games(sport, athlete_id, season)
        except Exception as e:
            log.warning("espn game log %s %s: %s", athlete_id, season, e)
    cutoff = date
    games = [g for g in games if g["date"][:10] < cutoff]
    games.sort(key=lambda g: g["date"], reverse=True)
    return games[:SEASON_GAMES]


POSITIONS = {}   # (sport, norm(player)) -> "G" / "F" / "C" (basketball), filled by roster_index


def position_group(abbr: str) -> str:
    a = (abbr or "").upper()
    if a.startswith("C"):
        return "C"
    if a in ("PG", "SG", "G") or a.startswith("G"):
        return "G"
    return "F" if a else ""


@functools.lru_cache(maxsize=16)
def roster_index(sport: str, date: str) -> dict:
    """norm(player) -> (athlete id, team name, opponent name, home tonight) for the slate."""
    path = PATHS.get(sport)
    if not path:
        return {}
    board = _get(f"{path}/scoreboard", dates=date.replace("-", ""))
    teams = []
    for event in board.get("events", []):
        comps = event["competitions"][0]["competitors"]
        for c in comps:
            other = next(o for o in comps if o is not c)
            teams.append((c["team"]["id"], c["team"]["displayName"],
                          other["team"]["displayName"], c.get("homeAway") == "home"))
    index = {}

    def roster(team):
        tid, name, opp, home = team
        try:
            data = _get(f"{path}/teams/{tid}/roster")
        except Exception as e:
            log.warning("espn roster %s: %s", name, e)
            return []
        people = []
        for a in data.get("athletes", []):
            if isinstance(a, dict) and "items" in a:          # grouped by position (NFL, NHL)
                people += a["items"]
            elif isinstance(a, dict):
                people.append(a)
        for a in people:
            if "displayName" in a and isinstance(a.get("position"), dict):
                POSITIONS[(sport, norm(a["displayName"]))] = position_group(
                    a["position"].get("abbreviation", ""))
        return [(a["displayName"], a["id"], name, opp, home) for a in people
                if "displayName" in a and "id" in a]

    with ThreadPoolExecutor(max_workers=12) as pool:
        for players in pool.map(roster, teams):
            for display, aid, name, opp, home in players:
                index[norm(display)] = (aid, name, opp, home)
    return index


def _value(sport: str, market: str, stats: dict) -> Optional[float]:
    keys = SPORT_MARKETS.get(sport, {}).get(market.lower())
    if not keys:
        return None
    vals = [stats.get(k) for k in keys]
    if sport in ZERO_IF_MISSING and stats:
        vals = [0.0 if v is None else v for v in vals]
    return None if any(v is None for v in vals) else sum(vals)


def supported(sport: str, market: str) -> bool:
    return market.lower() in SPORT_MARKETS.get(sport, {})


def _window(games: list, market: str, line: float, over: bool, sport: str = "NBA") -> Optional[Window]:
    vals = [v for v in (_value(sport, market, g["stats"]) for g in games) if v is not None]
    if not vals:
        return None
    hits = sum(1 for v in vals if (v > line if over else v < line))
    return Window(hits, len(vals), sum(vals) / len(vals))


def hit_profile(games: list, market: str, line: float, over: bool, opponent: str = "",
                home: Optional[bool] = None, sport: str = "NBA") -> HitProfile:
    prof = HitProfile(opponent=opponent, line=line, over=over)
    played = [g for g in games if _value(sport, market, g["stats"]) is not None]
    prof.season = _window(played, market, line, over, sport)
    for n in WINDOWS:
        if len(played) >= n:
            w = _window(played[:n], market, line, over, sport)
            if w:
                prof.recent[n] = w
    if home is not None:
        prof.venue = _window([g for g in played if g["home"] == home], market, line, over, sport)
        prof.venue_label = "at home" if home else "on the road"
    if opponent:
        prof.versus = _window([g for g in played if norm(g["opponent"]) == norm(opponent)],
                              market, line, over, sport)
    prof.games = [(g["date"][:10], g.get("abbr") or g["opponent"], g["home"],
                   _value(sport, market, g["stats"])) for g in played[:20]]
    return prof


def profiles_for(picks: list, sport: str, date: str) -> dict:
    """ProEval key -> HitProfile for the picks of one sport (empty if unsupported)."""
    if sport not in PATHS:
        return {}
    try:
        index = roster_index(sport, date)
    except Exception as e:
        log.warning("espn rosters unavailable: %s", e)
        return {}
    wanted = {}
    for ev in picks:
        m = ev.market
        if m.bet_type != "prop" or not supported(sport, m.market):
            continue
        hit = index.get(norm(m.subject))
        if hit:
            wanted.setdefault(hit[0], []).append((ev, hit))
    out = {}

    def build(item):
        aid, evs = item
        games = game_log(sport, aid, date)
        res = {}
        for ev, (_, _team, opp, home) in evs:
            over = ev.best.side == "A"
            res[ev.market.key] = hit_profile(games, ev.market.market, ev.market.line, over,
                                             opp, home, sport)
        return res

    with ThreadPoolExecutor(max_workers=12) as pool:
        for res in pool.map(build, wanted.items()):
            out.update(res)
    return out


def apply_hit_gate(res, profiles: dict, floor: Optional[float] = None):
    """Move picks whose season hit rate sits below the price's implied probability into
    the passes (with the reason), as a guard against streak-driven numbers."""
    kept, vetoed = [], []
    for ev in res.picks:
        prof = profiles.get(ev.market.key)
        season = prof.season if prof else None
        need = ev.best.best_implied if floor is None else floor
        if season and season.games >= MIN_SEASON_GAMES and season.rate < need:
            vetoed.append((ev, season))
        else:
            kept.append(ev)
    res.picks = kept
    res.hit_vetoes = getattr(res, "hit_vetoes", []) + vetoed
    games = {}
    for ev in kept:
        games.setdefault(ev.market.game_id, []).append(ev.best.label)
    res.correlated = {g: v for g, v in games.items() if len(v) > 1}
    return res
