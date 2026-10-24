"""Game-situation adjustments for the ESPN projections (basketball; teammate-out for NFL too).

  Teammate out   When a rotation player (18+ minutes) is ruled Out, part of his production
                 (LINEMAKER_USAGE_SHARE, default 60%) is handed to the teammates being
                 projected, in proportion to their own share of that stat. Skipped when he
                 has already missed the team's last 3 games, because his teammates' recent
                 logs (which the projection weights heavily) already show the bump.
  Blowout risk   Starters (28+ minutes) lose 0.3 minutes per point of spread beyond 7 (up to
                 4); bench players (under 20) gain half that. Spread from ESPN's scoreboard.
  Back-to-back   Starters on a team that played yesterday lose a minute.

NFL teammate out: a receiver or back ruled Out (2.5+ catches or 6+ carries a game lately)
hands LINEMAKER_USAGE_SHARE of his receptions, receiving yards, carries and rushing yards to
the teammates being projected, in proportion to their own share (capped at +35% of a
player's own number; skipped when he's already missed the last 2 games). A starting QB
(20+ attempts a game) ruled Out cuts his receivers' yards 12% and catches 7% for the backup.

Every adjustment is written into the projection's notes so the card can say why.
"""
import logging
import os
import re
from typing import Optional

from .oddsapi import norm

log = logging.getLogger("linemaker.adjustments")
STATS = {"points": "points", "rebounds": "totalRebounds", "assists": "assists",
         "threes": "threePointFieldGoalsMade"}
ROLE_MINUTES = 18
STARTER_MINUTES = 28
BENCH_MINUTES = 20
BAKED_IN_GAMES = 3
MAX_SHARE_OF_OWN = 0.20
# Typical team totals per game, so each teammate's share is measured against the whole
# rotation (not just the players with a posted prop tonight).
TEAM_TOTALS = {"NBA": {"points": 114.0, "rebounds": 44.0, "assists": 27.0, "threes": 13.0},
               "WNBA": {"points": 82.0, "rebounds": 35.0, "assists": 20.0, "threes": 8.0},
               "NFL": {"receptions": 21.0, "receiving yards": 215.0, "rushing yards": 115.0,
                       "rush attempts": 26.0}}
NFL_STATS = {"receptions": "receptions", "receiving yards": "receivingYards",
             "rushing yards": "rushingYards", "rush attempts": "rushingAttempts"}
NFL_CAP = 0.35            # most a teammate's projection can grow (receivers soak up targets)
NFL_BAKED_IN = 2
BACKUP_QB = {"receiving yards": 0.88, "receptions": 0.93}


def usage_share() -> float:
    try:
        return float(os.environ.get("LINEMAKER_USAGE_SHARE", "0.6"))
    except ValueError:
        return 0.6


def spreads_today(sport: str, date: str) -> dict:
    """norm(team) -> point spread for that team tonight (favourite negative), from ESPN."""
    from .boxscores import _get
    from .gamelogs import PATHS
    try:
        board = _get(f"{PATHS[sport]}/scoreboard", dates=date.replace("-", ""))
    except Exception as e:
        log.warning("espn spreads %s: %s", sport, e)
        return {}
    out = {}
    for ev in board.get("events", []):
        comp = ev["competitions"][0]
        odds = (comp.get("odds") or [{}])[0]
        m = re.match(r"\s*([A-Z]{2,4})\s+(-?\d+(?:\.\d+)?)", odds.get("details") or "")
        if not m:
            continue
        fav, spread = m.group(1), -abs(float(m.group(2)))
        for c in comp["competitors"]:
            name = norm(c["team"]["displayName"])
            out[name] = spread if c["team"].get("abbreviation") == fav else -spread
    return out


def minutes_delta(avg_minutes: float, spread: Optional[float], tired: bool) -> tuple:
    """(minutes to add, [reasons]) for one player tonight."""
    delta, why = 0.0, []
    if spread is not None and abs(spread) > 7:
        cut = min(4.0, 0.3 * (abs(spread) - 7))
        if avg_minutes >= STARTER_MINUTES:
            delta -= cut
            why.append(f"blowout risk ({abs(spread):g}-point spread) −{cut:.1f} min")
        elif avg_minutes < BENCH_MINUTES:
            delta += cut / 2
            why.append(f"garbage time ({abs(spread):g}-point spread) +{cut / 2:.1f} min")
    if tired and avg_minutes >= STARTER_MINUTES:
        delta -= 1.0
        why.append("back-to-back −1.0 min")
    return delta, why


def _recent(games: list, n: int = 10, sport: str = "NBA") -> list:
    if sport == "NFL":   # football logs have no minutes: a game with any stats was played
        return [g for g in games if g["stats"]][:n]
    return [g for g in games if g["stats"].get("minutes")][:n]


def _avg(games: list, key: str) -> float:
    vals = [g["stats"].get(key) or 0.0 for g in games]
    return sum(vals) / len(vals) if vals else 0.0


def absence_baked_in(out_games: list, mate_games: list, sport: str = "NBA") -> bool:
    """True when the out player missed the team's last few games already."""
    played = _recent(out_games, 1, sport)
    if not played:
        return True
    last = played[0]["date"][:10]
    n = NFL_BAKED_IN if sport == "NFL" else BAKED_IN_GAMES
    team_dates = sorted({g["date"][:10] for games in mate_games for g in _recent(games, 20, sport)},
                        reverse=True)
    return len(team_dates) >= n and last < team_dates[n - 1]


def _nfl_role(recent: list) -> bool:
    return len(recent) >= 2 and (_avg(recent, "receptions") >= 2.5 or _avg(recent, "rushingAttempts") >= 6)


def backup_qb(outs: dict, mates: dict) -> Optional[str]:
    """Name of a ruled-out starting QB (20+ attempts a game lately, and not already out for
    the last 2 games, whose receivers' logs show it), if any."""
    mate_logs = list(mates.values())
    for name, games in outs.items():
        recent = _recent(games, 5, "NFL")
        if (len(recent) >= 2 and _avg(recent, "passingAttempts") >= 20
                and not absence_baked_in(games, mate_logs, "NFL")):
            return name
    return None


def usage_boosts(outs: dict, mates: dict, sport: str = "NBA") -> dict:
    """outs/mates: {player: game log}, one team. Returns {mate: {stat market: points added}}
    plus a "_why" list per mate."""
    share = usage_share()
    boosts = {m: {} for m in mates}
    mate_logs = list(mates.values())
    nfl = sport == "NFL"
    stats, cap = (NFL_STATS, NFL_CAP) if nfl else (STATS, MAX_SHARE_OF_OWN)
    for name, games in outs.items():
        recent = _recent(games, 5 if nfl else 10, sport)
        if nfl:
            if not _nfl_role(recent):
                continue
        elif len(recent) < 3 or _avg(recent, "minutes") < ROLE_MINUTES:
            continue
        if absence_baked_in(games, mate_logs, sport):
            continue
        for market, key in stats.items():
            freed = _avg(recent, key) * share
            if freed <= 0:
                continue
            own = {m: _avg(_recent(g, 5 if nfl else 10, sport), key) for m, g in mates.items()}
            team = TEAM_TOTALS.get(sport, TEAM_TOTALS["NBA"])[market]
            total = max(team - _avg(recent, key), sum(own.values()))
            if total <= 0:
                continue
            for m, v in own.items():
                add = min(freed * v / total, cap * v)
                if add > 0.05:
                    boosts[m][market] = boosts[m].get(market, 0.0) + add
        for m in mates:
            if boosts[m]:
                boosts[m].setdefault("_why", []).append(name)
    return boosts
