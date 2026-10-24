"""NHL shot suppression: how many shots each team allows, for shots-on-goal props.

From each team's last 10 finished games (this season, topped up with last season's final
games early on; ESPN box scores, cached) the opponents' total shots on goal per game:

  factor = 1 + n/(n + 10) x (team's shots allowed / slate average - 1)

so a team allowing 33 a game against a 29 average lifts the opposing skaters' shots-on-goal
projections about 10% once it has 10 games; a thin sample stays near 1. Capped at ±20%.
"""
import logging
from concurrent.futures import ThreadPoolExecutor

from .oddsapi import norm

log = logging.getLogger("linemaker.nhl_shots")
GAMES, SHRINK, CAP = 10, 10, 0.20


def shots_allowed(summary: dict, team: str):
    """Shots on goal by `team`'s opponents in one box score (None if it can't be read)."""
    total, seen = 0.0, False
    for t in summary.get("boxscore", {}).get("players", []):
        if norm(t.get("team", {}).get("displayName", "")) == norm(team):
            continue
        for group in t.get("statistics", []):
            keys = group.get("keys") or []
            if "shotsTotal" not in keys or "saves" in keys:
                continue
            i = keys.index("shotsTotal")
            for a in group.get("athletes", []):
                try:
                    total += float(a["stats"][i])
                    seen = True
                except (KeyError, IndexError, ValueError, TypeError):
                    continue
    return total if seen else None


def shot_factors(date: str, teams: set) -> dict:
    """norm(team) -> multiplier for the OPPOSING skaters' shots on goal."""
    from .context import _team_ids
    from .dvp import _summary, _team_games
    ids = _team_ids("NHL", date)

    def one(team):
        hit = ids.get(norm(team))
        if not hit:
            return team, None
        games = _team_games("NHL", hit[0], date, GAMES)
        vals = [v for v in (shots_allowed(s, hit[1]) for s in (_summary("NHL", e) for e in games) if s)
                if v is not None and v > 5]   # under 5 = a box score with missing rows
        return team, (sum(vals) / len(vals), len(vals)) if vals else None
    with ThreadPoolExecutor(max_workers=8) as pool:
        per = {norm(t): r for t, r in pool.map(one, sorted(teams)) if r}
    if not per:
        return {}
    avg = sum(v for v, _n in per.values()) / len(per)
    out = {}
    for team, (v, n) in per.items():
        f = 1 + n / (n + SHRINK) * (v / avg - 1)
        out[team] = round(max(1 - CAP, min(1 + CAP, f)), 3)
    log.info("nhl shot suppression: %d teams", len(out))
    return out
