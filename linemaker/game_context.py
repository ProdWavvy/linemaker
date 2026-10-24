"""What the betting market and the forecast say about tonight's game, for player props.

Team totals  ESPN's scoreboard carries the pre-game spread and total. Each team's expected
             points = (total - its spread) / 2 (a 6.5-point favourite in a 44.5 total ~25.5).
             Against the team's usual scoring (season blend, game_model.team_ratings):
               factor = (expected / usual) ^ k        capped at ±15%
             k is how much of a player's output moves with his team's: NBA/WNBA 0.4 (pace and
             defense are already in the projection, so only part of this is new), NFL/college
             0.5 (opponent defense is already applied separately). Spread-based sports only:
             NHL and MLB lines are a fixed 1.5-goal / run line, which says nothing about the
             size of the edge, so they're skipped.
Weather      NFL / college outdoor games: rain, snow or wind in ESPN's forecast trims passing
             and receiving 7% (receptions 4%) and nudges rushing up 3%; domes and clear skies
             change nothing. ESPN's feed has no wind speed, only conditions such as "Windy".
"""
import logging
import re
from typing import Optional

from .oddsapi import norm

log = logging.getLogger("linemaker.game_context")
EXPONENT = {"NBA": 0.4, "WNBA": 0.4, "NFL": 0.5, "NCAAF": 0.5}
CAP = 0.15
# which prop markets move with the team's scoring
TEAM_MARKETS = {
    "NBA": {"points", "assists", "threes", "pra", "pts + reb", "pts + ast", "reb + ast",
            "field goals", "free throws made"},
    "NFL": {"passing yards", "pass completions", "passing tds", "receiving yards", "receptions",
            "rushing yards", "anytime td", "pass + rush yards", "rush + rec yards", "receiving tds",
            "rushing tds", "kicking points", "field goals made", "extra points"},
}
TEAM_MARKETS["WNBA"], TEAM_MARKETS["NCAAF"] = TEAM_MARKETS["NBA"], TEAM_MARKETS["NFL"]
BAD_WEATHER = re.compile(r"rain|shower|storm|snow|sleet|flurr|wind|drizzle|ice", re.I)
WEATHER = {"passing yards": 0.93, "pass completions": 0.95, "receiving yards": 0.93,
           "receptions": 0.96, "passing tds": 0.93, "rushing yards": 1.03, "pass + rush yards": 0.95,
           "kicking points": 0.92, "field goals made": 0.92, "receiving tds": 0.93}


def _board(sport: str, date: str) -> dict:
    from .boxscores import _get
    from .gamelogs import PATHS
    try:
        return _get(f"{PATHS[sport]}/scoreboard", dates=date.replace("-", ""))
    except Exception as e:
        log.warning("espn scoreboard %s: %s", sport, e)
        return {}


def implied_totals(sport: str, date: str) -> dict:
    """norm(team) -> points the market expects that team to score tonight."""
    if sport not in EXPONENT:
        return {}
    out = {}
    for ev in _board(sport, date).get("events", []):
        comp = ev["competitions"][0]
        odds = (comp.get("odds") or [{}])[0]
        total = odds.get("overUnder")
        m = re.match(r"\s*([A-Z]{2,4})\s+(-?\d+(?:\.\d+)?)", odds.get("details") or "")
        if not total or not m:
            continue
        fav, spread = m.group(1), abs(float(m.group(2)))
        for c in comp["competitors"]:
            mine = -spread if c["team"].get("abbreviation") == fav else spread
            out[norm(c["team"]["displayName"])] = (float(total) - mine) / 2
    return out


def team_factors(sport: str, date: str) -> dict:
    """norm(team) -> (multiplier, expected points, usual points) for that team's players."""
    if sport not in EXPONENT:
        return {}
    implied = implied_totals(sport, date)
    if not implied:
        return {}
    from .game_model import team_ratings
    try:
        ratings = team_ratings(sport, date)
    except Exception as e:
        log.warning("team ratings %s: %s", sport, e)
        return {}
    out = {}
    for team, exp in implied.items():
        r = ratings.get(team)
        if not r or not r.get("scored"):
            continue
        f = (exp / r["scored"]) ** EXPONENT[sport]
        out[team] = (max(1 - CAP, min(1 + CAP, f)), exp, r["scored"])
    return out


def weather(sport: str, date: str) -> dict:
    """norm(team) -> forecast text for teams playing outdoors in bad weather (football)."""
    if sport not in ("NFL", "NCAAF"):
        return {}
    out = {}
    for ev in _board(sport, date).get("events", []):
        comp = ev["competitions"][0]
        w = ev.get("weather") or comp.get("weather") or {}
        text = w.get("displayValue") or ""
        if comp.get("venue", {}).get("indoor") or not BAD_WEATHER.search(text):
            continue
        for c in comp["competitors"]:
            out[norm(c["team"]["displayName"])] = f"{text}, {w.get('temperature', '?')}°"
    return out


def apply(sport: str, date: str, rows: list):
    """Team-total and weather factors on projection rows (rows carry _team)."""
    markets = TEAM_MARKETS.get(sport)
    if not markets:
        return
    try:
        factors = team_factors(sport, date)
        sky = weather(sport, date)
    except Exception as e:
        log.warning("game context %s: %s", sport, e)
        return
    for r in rows:
        m, team = r["market"].lower(), norm(r.get("_team", ""))
        hit = factors.get(team)
        if hit and m in markets and abs(hit[0] - 1) >= 0.01:
            r["mean"] = f"{float(r['mean']) * hit[0]:.3f}"
            r["notes"] += f"; ×{hit[0]:.2f} team expected {hit[1]:.1f} vs usual {hit[2]:.1f}"
        if team in sky and m in WEATHER:
            r["mean"] = f"{float(r['mean']) * WEATHER[m]:.3f}"
            r["notes"] += f"; ×{WEATHER[m]:.2f} weather ({sky[team]})"


def game_weather_total(sport: str, date: str) -> dict:
    """norm(team) -> points to take off the game total for bad weather (football)."""
    return {t: 2.5 for t in weather(sport, date)}
