"""NHL ice time from the league's own stats feed (api-web.nhle.com, free, no key).

A skater's per-game average lags when his role changes (moved up a line, onto the first power
play, or down to the fourth line). The NHL game log carries time on ice for every game, so the
projection blends the per-game average with his season rate per 60 minutes x his ice time over
the last five games:

    projection = (1 - TOI_WEIGHT) x per-game average + TOI_WEIGHT x rate per 60 x recent TOI / 60

Tested on this season's games (356 skaters, 16 teams, 3,597 player-games after each player's
first six), predicting each game from the games before it, Poisson log-likelihood per game:
    shots   -1.6065 -> -1.6049    points  -0.9690 -> -0.9668    goals  -0.5570 -> -0.5554
better on both halves of the players for all three, with TOI_WEIGHT 0.5. LINEMAKER_NHL_TOI=off
turns it off.
"""
import datetime as dt
import json
import logging
import os
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from .oddsapi import norm

log = logging.getLogger("linemaker.nhl_api")
BASE = "https://api-web.nhle.com/v1"
TOI_WEIGHT = 0.5
MIN_GAMES = 6
RECENT = 5
STAT_KEYS = {"shots on goal": "shots", "hockey points": "points", "goals": "goals",
             "anytime goals": "goals", "hockey assists": "assists"}


def on() -> bool:
    return (os.environ.get("LINEMAKER_NHL_TOI", "on").lower() not in ("off", "0", "no", "false")
            and not os.environ.get("LINEMAKER_OFFLINE"))


def _get(path: str, max_age: float) -> dict:
    """NHL JSON, cached on disk for max_age seconds."""
    cache = os.path.join(os.environ.get("LINEMAKER_CACHE_DIR", "cache"), "nhl_api")
    name = os.path.join(cache, path.strip("/").replace("/", "_") + ".json")
    try:
        if time.time() - os.path.getmtime(name) < max_age:
            with open(name) as f:
                return json.load(f)
    except (OSError, ValueError):
        pass
    req = urllib.request.Request(BASE + path, headers={"User-Agent": "Linemaker/1.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read())
    try:
        os.makedirs(cache, exist_ok=True)
        with open(name, "w") as f:
            json.dump(data, f)
    except OSError:
        pass
    return data


def season_id(date: str) -> str:
    d = dt.date.fromisoformat(date)
    start = d.year if d.month >= 8 else d.year - 1
    return f"{start}{start + 1}"


def team_abbrevs() -> dict:
    """norm(full team name) -> abbreviation ("edmonton oilers" -> "EDM")."""
    out = {}
    for t in _get("/standings/now", 12 * 3600).get("standings", []):
        name = (t.get("teamName") or {}).get("default") or ""
        abbrev = (t.get("teamAbbrev") or {}).get("default")
        if name and abbrev:
            out[norm(name)] = abbrev
    return out


def roster_ids(abbrev: str) -> dict:
    """norm(player name) -> NHL player id, skaters on a team's current roster."""
    r = _get(f"/roster/{abbrev}/current", 12 * 3600)
    out = {}
    for group in ("forwards", "defensemen"):
        for p in r.get(group, []):
            name = f"{(p.get('firstName') or {}).get('default', '')} {(p.get('lastName') or {}).get('default', '')}"
            out[norm(name)] = p["id"]
    return out


def _toi(text) -> float:
    try:
        m, s = str(text).split(":")
        return int(m) + int(s) / 60
    except (ValueError, AttributeError):
        return 0.0


def game_log(player_id: int, date: str) -> list:
    """This season's regular-season games, oldest first."""
    data = _get(f"/player/{player_id}/game-log/{season_id(date)}/2", 3 * 3600)
    games = [g for g in data.get("gameLog", []) if (g.get("gameDate") or "") < date]
    return list(reversed(games))


def toi_projection(games: list, stat: str, base: float) -> Optional[tuple]:
    """(blended projection, recent TOI, season TOI) from a game log, or None if too few games."""
    played = [g for g in games if _toi(g.get("toi")) > 0]
    if len(played) < MIN_GAMES:
        return None
    total_toi = sum(_toi(g["toi"]) for g in played)
    rate = sum(g.get(stat, 0) or 0 for g in played) / total_toi * 60 if total_toi else None
    recent = sum(_toi(g["toi"]) for g in played[-RECENT:]) / len(played[-RECENT:])
    if rate is None:
        return None
    return ((1 - TOI_WEIGHT) * base + TOI_WEIGHT * rate * recent / 60, recent, total_toi / len(played))


def _mmss(minutes: float) -> str:
    return f"{int(minutes)}:{int(round((minutes % 1) * 60)):02d}"


def apply_toi(date: str, rows: list) -> int:
    """Blend recent ice time into NHL skater projections in place. Returns rows changed."""
    if not on():
        return 0
    todo = [r for r in rows if r.get("market", "").lower() in STAT_KEYS and r.get("_team")]
    if not todo:
        return 0
    try:
        abbrevs = team_abbrevs()
    except Exception as e:
        log.warning("nhl standings: %s", e)
        return 0
    teams = {abbrevs.get(norm(r["_team"])) for r in todo} - {None}
    rosters = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        for abbrev, ids in zip(teams, pool.map(lambda a: _safe(roster_ids, a), teams)):
            rosters[abbrev] = ids or {}
    wanted = {}
    for r in todo:
        pid = rosters.get(abbrevs.get(norm(r["_team"])), {}).get(norm(r["subject"]))
        if pid:
            wanted.setdefault(pid, []).append(r)
    with ThreadPoolExecutor(max_workers=8) as pool:
        logs = dict(zip(wanted, pool.map(lambda pid: _safe(game_log, pid, date), wanted)))
    changed = 0
    for pid, rs in wanted.items():
        games = logs.get(pid) or []
        for r in rs:
            base = float(r["mean"])
            res = toi_projection(games, STAT_KEYS[r["market"].lower()], base)
            if not res:
                continue
            new, recent, season = res
            r["mean"] = f"{new:.3f}"
            r["notes"] += (f"; NHL ice time {_mmss(recent)} last {RECENT} vs {_mmss(season)} season "
                           f"(rate x TOI blended {TOI_WEIGHT:.0%}: {base:.2f} -> {new:.2f})")
            changed += 1
    log.info("nhl ice time: %d of %d skater projections adjusted", changed, len(todo))
    return changed


def _safe(fn, *args):
    try:
        return fn(*args)
    except Exception as e:
        log.warning("nhl api %s%s: %s", fn.__name__, args, e)
        return None
