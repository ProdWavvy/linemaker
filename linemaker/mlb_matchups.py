"""MLB starting pitchers and lineups: who's on the mound, who's batting where.

Pitchers  ESPN's scoreboard lists each team's probable starter. His career rates per batter
          faced (hits, total bases, home runs, earned runs) are pulled toward the league
          average by 600 batters, then compared with the league:
            hitter factor = 1 + 0.6 x (pitcher's rate / league rate - 1)
          (0.6: the share of a hitter's plate appearances that come against the starter).
          Applied to the opposing hitters' hits, total bases and home runs props. The game
          model moves runs by (pitcher's ERA - league ERA) x 5.5 innings / 9, at 70%.
          A pitcher's own strikeouts prop is skipped when he isn't tonight's listed starter
          (scratched).
Lineups   ESPN posts the batting order about 3 hours before first pitch. A projected hitter
          who isn't in it is skipped (resting / scratched); one who is gets his at-bats scaled
          to his spot (leadoff ~4.6 plate appearances, ninth ~3.8) vs his recent average,
          capped at ±15%.
News      The news loop re-prices a game when a team's probable starter changes.
"""
import logging
from typing import Optional

from .oddsapi import norm

log = logging.getLogger("linemaker.mlb")
LEAGUE = {"hits": 0.225, "tb": 0.36, "hr": 0.030, "era": 4.20}
PRIOR_BF = 600
STARTER_SHARE = 0.6
STARTER_INNINGS = 5.5
GAME_SHRINK = 0.7
PA_BY_SPOT = (4.65, 4.55, 4.45, 4.35, 4.25, 4.15, 4.05, 3.95, 3.85)
AB_PER_PA = 0.88
HITTER_MARKETS = {"hits": "hits", "total bases": "tb", "home runs": "hr"}


def probables(date: str) -> dict:
    """norm(team) -> {"team", "name", "id", "opp", "event", "game"} for the date."""
    from .boxscores import _get
    try:
        data = _get("baseball/mlb/scoreboard", dates=date.replace("-", ""))
    except Exception as e:
        log.warning("mlb scoreboard %s: %s", date, e)
        return {}
    out = {}
    for ev in data.get("events", []):
        comp = (ev.get("competitions") or [{}])[0]
        teams = [c.get("team", {}).get("displayName", "") for c in comp.get("competitors", [])]
        for c in comp.get("competitors", []):
            team = c.get("team", {}).get("displayName", "")
            p = next((x for x in c.get("probables") or [] if x.get("name") == "probableStartingPitcher"), None)
            if not p:
                continue
            out[norm(team)] = {"team": team, "name": p["athlete"]["displayName"], "id": str(p["athlete"]["id"]),
                               "opp": next((t for t in teams if t != team), ""), "event": ev["id"],
                               "game": ev.get("name", "")}
    return out


def pitcher_rates(pitcher_id: str) -> Optional[dict]:
    """Career rates per batter faced, pulled toward the league by PRIOR_BF batters."""
    from .ufc_model import _get as get_json   # cached ESPN JSON reader
    try:
        data = get_json(f"https://sports.core.api.espn.com/v2/sports/baseball/leagues/mlb/athletes/{pitcher_id}/statistics")
    except Exception as e:
        log.warning("mlb pitcher %s: %s", pitcher_id, e)
        return None
    s = {x["name"]: x.get("value") for c in data.get("splits", {}).get("categories", [])
         if c.get("name") == "pitching" for x in c.get("stats", [])}
    bf = s.get("battersFaced") or 0
    if not s:
        return None

    def rate(count, league):
        return ((count or 0) + league * PRIOR_BF) / (bf + PRIOR_BF)
    inn = s.get("innings") or 0
    era = ((s.get("earnedRuns") or 0) * 9 + LEAGUE["era"] * 135) / (inn + 135)   # 135 innings of league ERA
    return {"hits": rate(s.get("hits"), LEAGUE["hits"]), "tb": rate(s.get("opponentTotalBases"), LEAGUE["tb"]),
            "hr": rate(s.get("homeRuns"), LEAGUE["hr"]), "era": era, "bf": bf}


def hitter_factor(rates: dict, key: str) -> float:
    return 1 + STARTER_SHARE * (rates[key] / LEAGUE[key] - 1)


def extra_runs(rates: dict) -> float:
    """Runs a starter gives up beyond (or below, negative) a league-average one."""
    return (rates["era"] - LEAGUE["era"]) * STARTER_INNINGS / 9 * GAME_SHRINK


def starters(date: str) -> dict:
    """norm(team) -> probable starter dict with his "rates" (None when ESPN has no stats)."""
    from concurrent.futures import ThreadPoolExecutor
    found = probables(date)
    with ThreadPoolExecutor(max_workers=6) as pool:
        rates = dict(zip(found, pool.map(lambda k: pitcher_rates(found[k]["id"]), found)))
    for k, v in found.items():
        v["rates"] = rates[k]
    return found


def lineups(event_id: str) -> dict:
    """norm(team) -> {norm(hitter): batting spot} once the batting order is posted ({} before)."""
    from .boxscores import _get
    try:
        data = _get("baseball/mlb/summary", event=event_id)
    except Exception as e:
        log.warning("mlb summary %s: %s", event_id, e)
        return {}
    out = {}
    for t in data.get("rosters", []):
        order = {norm(p["athlete"]["displayName"]): p.get("batOrder") for p in t.get("roster", [])
                 if p.get("starter") and p.get("batOrder")}
        if order:
            out[norm(t.get("team", {}).get("displayName", ""))] = order
    return out


def spot_factor(spot: int, games: list) -> Optional[float]:
    """At-bats at this lineup spot vs the hitter's recent average (capped at ±15%)."""
    recent = [g["stats"].get("atBats") for g in games[:15] if g["stats"].get("atBats")]
    if len(recent) < 5 or not 1 <= spot <= 9:
        return None
    avg = sum(recent) / len(recent)
    return max(0.85, min(1.15, PA_BY_SPOT[spot - 1] * AB_PER_PA / avg))


def pitcher_starts(date: str) -> dict:
    """norm(team) -> [starter, False]: what the news loop compares between checks."""
    return {k: [v["name"], False] for k, v in probables(date).items()}
