"""Defense vs position (basketball): what each defense allows to guards, forwards and centers.

For every team on the slate, its last LINEMAKER_DVP_GAMES (default 10) finished games this
season are read from ESPN box scores (cached on disk; finished games never change). Per game
we add up the points, rebounds, assists and threes its opponents' guards, forwards and
centers produced. The factor for a market is that team's allowance divided by the slate
average, pulled toward 1.0 by sample size (n / (n + 15)), so two games barely move it.
"""
import datetime as dt
import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from .gamelogs import PATHS, position_group
from .oddsapi import norm

log = logging.getLogger("linemaker.dvp")
SHRINK = 15
MARKETS = {"points": "points", "rebounds": "rebounds", "assists": "assists",
           "threes": "threePointFieldGoalsMade"}


def _cache_dir() -> str:
    return os.path.join(os.environ.get("LINEMAKER_CACHE_DIR", "cache"), "espn_summary")


def _summary(sport: str, event_id: str) -> Optional[dict]:
    from .boxscores import _get
    path = os.path.join(_cache_dir(), f"{sport}_{event_id}.json")
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        pass
    try:
        data = _get(f"{PATHS[sport]}/summary", event=event_id)
    except Exception as e:
        log.warning("espn summary %s: %s", event_id, e)
        return None
    slim = {"boxscore": data.get("boxscore", {})}
    try:
        os.makedirs(_cache_dir(), exist_ok=True)
        with open(path, "w") as f:
            json.dump(slim, f)
    except OSError:
        pass
    return slim


def allowed_by_position(summary: dict, defense: str) -> dict:
    """{"G": {"points": x, ...}, "F": ..., "C": ...} produced against `defense` in one game."""
    out = {}
    for team in summary.get("boxscore", {}).get("players", []):
        if norm(team.get("team", {}).get("displayName", "")) == norm(defense):
            continue          # that's the defense's own players
        for group in team.get("statistics", []):
            keys = group.get("keys") or group.get("names") or []
            for a in group.get("athletes", []):
                if a.get("didNotPlay") or not a.get("stats"):
                    continue
                pos = position_group((a.get("athlete", {}).get("position") or {}).get("abbreviation", ""))
                if not pos:
                    continue
                row = out.setdefault(pos, {m: 0.0 for m in MARKETS})
                for key, cell in zip(keys, a["stats"]):
                    if key == "points":
                        row["points"] += _num(cell)
                    elif key in ("rebounds", "totalRebounds"):
                        row["rebounds"] += _num(cell)
                    elif key == "assists":
                        row["assists"] += _num(cell)
                    elif key.startswith("threePointFieldGoalsMade"):
                        row["threes"] += _num(str(cell).split("-")[0])
    return out


def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _finished(sport: str, team_id: str, date: str, season: Optional[int] = None) -> list:
    from .boxscores import _get
    params = {"season": season} if season else {}
    try:
        data = _get(f"{PATHS[sport]}/teams/{team_id}/schedule", **params)
    except Exception as e:
        log.warning("espn schedule %s: %s", team_id, e)
        return []
    done = []
    for ev in data.get("events", []):
        comp = (ev.get("competitions") or [{}])[0]
        state = (comp.get("status") or {}).get("type", {}).get("state")
        played = comp.get("boxscoreAvailable") or state == "post"
        if played and (ev.get("date") or "")[:10] < date and state != "pre":
            done.append((ev["date"], ev["id"]))
    return sorted(done, reverse=True)


def _team_games(sport: str, team_id: str, date: str, n: int) -> list:
    """Last n finished games; early in a season, topped up with last season's final games."""
    games = _finished(sport, team_id, date)
    if len(games) < n:
        from .gamelogs import _season_year
        games += _finished(sport, team_id, date, _season_year(sport, date) - 1)
    return [eid for _d, eid in games[:n]]


def position_factors(sport: str, date: str, teams: set) -> dict:
    """norm(team) -> {pos: {market: factor}} for the given teams (basketball only)."""
    if sport not in ("NBA", "WNBA"):
        return {}
    path = os.path.join(os.environ.get("LINEMAKER_CACHE_DIR", "cache"), f"dvp_{sport}_{date}.json")
    try:
        with open(path) as f:
            cached = json.load(f)
        if set(map(norm, teams)) <= set(cached):
            return cached
    except (OSError, ValueError):
        pass
    from .context import _team_ids
    ids = _team_ids(sport, date)
    n = int(os.environ.get("LINEMAKER_DVP_GAMES", "10"))
    per_team = {}

    def one(team):
        hit = ids.get(norm(team))
        if not hit:
            return team, None
        games = _team_games(sport, hit[0], date, n)
        totals, count = {}, 0
        with ThreadPoolExecutor(max_workers=4) as inner:
            summaries = list(inner.map(lambda eid: _summary(sport, eid), games))
        for s in summaries:
            if not s:
                continue
            count += 1
            for pos, stats in allowed_by_position(s, hit[1]).items():
                t = totals.setdefault(pos, {m: 0.0 for m in MARKETS})
                for m, v in stats.items():
                    t[m] += v
        if not count:
            return team, None
        return team, (count, {p: {m: v / count for m, v in st.items()} for p, st in totals.items()})

    with ThreadPoolExecutor(max_workers=12) as pool:
        for team, res in pool.map(one, sorted(teams)):
            if res:
                per_team[norm(team)] = res
    if not per_team:
        return {}
    league = {}
    for _n, table in per_team.values():
        for pos, st in table.items():
            for m, v in st.items():
                league.setdefault(pos, {}).setdefault(m, []).append(v)
    base = {p: {m: sum(v) / len(v) for m, v in st.items()} for p, st in league.items()}
    out = {}
    for team, (count, table) in per_team.items():
        w = count / (count + SHRINK)
        out[team] = {p: {m: round(1 + w * (v / base[p][m] - 1), 4) if base[p][m] else 1.0
                         for m, v in st.items()} for p, st in table.items()}
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w") as f:
            json.dump(out, f)
    except OSError:
        pass
    log.info("dvp %s: %d teams", sport, len(out))
    return out
