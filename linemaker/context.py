"""Game context for pick write-ups: Kalshi win probabilities and team pace/defense.

Kalshi: public market data (no key). Win probability is the midpoint of the best yes bid
and ask for each team's "wins" contract.
Team context (ESPN, unofficial): basketball pace is estimated as possessions per game
(FGA - OREB + TOV + 0.44 * FTA) and defensive rating as points allowed per 100 of those
possessions; other sports report points (or goals) allowed per game. Both are labelled as
estimates. A season with fewer than 10 games falls back to last season's numbers.
"""
import datetime as dt
import functools
import json
import logging
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Optional

from .boxscores import _get
from .gamelogs import PATHS, _season_year
from .oddsapi import norm

log = logging.getLogger("linemaker.context")
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
KALSHI_SERIES = {"NBA": "KXNBAGAME", "WNBA": "KXWNBAGAME", "NFL": "KXNFLGAME",
                 "NCAAF": "KXNCAAFGAME", "NHL": "KXNHLGAME", "MLB": "KXMLBGAME"}
BASKETBALL = {"NBA", "WNBA"}


@dataclass
class TeamContext:
    team: str
    games: int
    season_label: str
    pace: Optional[float] = None         # basketball: est. possessions per game
    def_rating: Optional[float] = None   # basketball: est. points allowed per 100 possessions
    allowed: Optional[float] = None      # points (or goals) allowed per game


def _kalshi(path: str, **params) -> dict:
    query = "&".join(f"{k}={v}" for k, v in params.items())
    req = urllib.request.Request(f"{KALSHI}/{path}?{query}",
                                 headers={"User-Agent": "Linemaker/1.0", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read())


def _f(v) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _team_match(full_name: str, sub_title: str) -> bool:
    a, b = norm(full_name), norm(sub_title)
    return bool(b) and (a.startswith(b) or b in a)


def kalshi_win_probs(sport: str, date: str) -> list:
    """[(team label, probability)] per event on that date: [(\"Atlanta\", 0.425), ...]."""
    series = KALSHI_SERIES.get(sport)
    if not series:
        return []
    d = dt.date.fromisoformat(date)
    tag = f"{d:%y}{d.strftime('%b').upper()}{d:%d}"
    events = _kalshi("events", series_ticker=series, status="open", limit=200).get("events", [])
    out = []
    for ev in events:
        if tag not in ev.get("event_ticker", ""):
            continue
        teams = []
        for m in _kalshi("markets", event_ticker=ev["event_ticker"]).get("markets", []):
            bid, ask = _f(m.get("yes_bid_dollars")), _f(m.get("yes_ask_dollars"))
            if bid is None or ask is None or ask <= 0:
                continue
            teams.append((m.get("yes_sub_title", ""), (bid + ask) / 2))
        if teams:
            out.append(teams)
    return out


def game_kalshi(events: list, away: str, home: str) -> Optional[dict]:
    """{full team name: probability} for one game, matched by team names."""
    for teams in events:
        matched = {}
        for label, p in teams:
            if _team_match(home, label):
                matched[home] = p
            elif _team_match(away, label):
                matched[away] = p
        if len(matched) == 2:
            total = sum(matched.values())
            return {k: v / total for k, v in matched.items()}
    return None


def _team_ids(sport: str, date: str) -> dict:
    board = _get(f"{PATHS[sport]}/scoreboard", dates=date.replace("-", ""))
    out = {}
    for event in board.get("events", []):
        for c in event["competitions"][0]["competitors"]:
            out[norm(c["team"]["displayName"])] = (c["team"]["id"], c["team"]["displayName"])
    return out


def _stat_map(data: dict) -> dict:
    out = {}
    cats = (data.get("results", {}).get("stats", {}).get("categories")
            or data.get("statistics", {}).get("splits", {}).get("categories") or [])
    for cat in cats:
        for s in cat.get("stats", []):
            out[s.get("name")] = _f(s.get("value"))
    return out


@functools.lru_cache(maxsize=16)
def _standings_allowed(sport: str, season: int) -> dict:
    try:
        req = urllib.request.Request(
            f"https://site.api.espn.com/apis/v2/sports/{PATHS[sport]}/standings?season={season}",
            headers={"User-Agent": "Linemaker/1.0"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        log.warning("espn standings %s: %s", sport, e)
        return {}
    out = {}

    def walk(node):
        for child in node.get("children", []):
            walk(child)
        for entry in node.get("standings", {}).get("entries", []):
            stats = {s.get("name"): _f(s.get("value")) for s in entry.get("stats", [])}
            gp = stats.get("gamesPlayed") or ((stats.get("wins") or 0) + (stats.get("losses") or 0)
                                              + (stats.get("ties") or 0) + (stats.get("otLosses") or 0))
            pa = stats.get("avgPointsAgainst")
            if pa is None and stats.get("pointsAgainst") is not None and gp:
                pa = stats["pointsAgainst"] / gp
            out[norm(entry["team"]["displayName"])] = (gp or 0, pa)
    walk(data)
    return out


def team_contexts(sport: str, date: str, teams: set) -> dict:
    """norm(team) -> TeamContext for the given full team names."""
    if sport not in PATHS:
        return {}
    ids = _team_ids(sport, date)
    year = _season_year(sport, date)
    current = _standings_allowed(sport, year)
    previous = None
    out = {}

    def stats_for(team_id, season):
        try:
            return _stat_map(_get(f"{PATHS[sport]}/teams/{team_id}/statistics", season=season))
        except Exception:
            return {}

    for full in teams:
        key = norm(full)
        hit = ids.get(key) or next((v for k, v in ids.items() if k.split()[-1:] == key.split()[-1:]), None)
        if not hit:
            continue
        team_id, display = hit
        gp, pa = current.get(norm(display), (0, None))
        season, label = year, "this season"
        if gp < 10:
            if previous is None:
                previous = _standings_allowed(sport, year - 1)
            gp, pa = previous.get(norm(display), (0, None))
            season, label = year - 1, "last season"
        ctx = TeamContext(team=display, games=int(gp), season_label=label, allowed=pa)
        if sport in BASKETBALL:
            st = stats_for(team_id, season)
            fga, oreb, tov, fta = (st.get(k) for k in ("avgFieldGoalsAttempted", "avgOffensiveRebounds",
                                                         "avgTurnovers", "avgFreeThrowsAttempted"))
            if None not in (fga, oreb, tov, fta):
                ctx.pace = fga - oreb + tov + 0.44 * fta
                if pa:
                    ctx.def_rating = 100 * pa / ctx.pace
        out[key] = ctx
    return out


def game_context(sport: str, date: str, games: list) -> dict:
    """game label 'Away @ Home' -> {"kalshi": {...} | None, "teams": {name: TeamContext}}."""
    out = {g: {"kalshi": None, "teams": {}} for g in games}
    pairs = {g: tuple(x.strip() for x in g.split(" @ ", 1)) for g in games if " @ " in g}
    try:
        events = kalshi_win_probs(sport, date)
        for g, (away, home) in pairs.items():
            out[g]["kalshi"] = game_kalshi(events, away, home)
    except Exception as e:
        log.warning("kalshi unavailable: %s", e)
    try:
        teams = {t for pair in pairs.values() for t in pair}
        ctxs = team_contexts(sport, date, teams)
        for g, (away, home) in pairs.items():
            out[g]["teams"] = {t: ctxs[norm(t)] for t in (away, home) if norm(t) in ctxs}
    except Exception as e:
        log.warning("team context unavailable: %s", e)
    return out
