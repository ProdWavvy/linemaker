"""NFL rushing workload from nflverse (free weekly player stats, github.com/nflverse).

A running back's carries move with his role (starter hurt, committee shift, goal-line work) faster
than his per-game averages do. Rushing yards and rush attempts projections for running backs,
receivers and tight ends blend the per-game average with his season yards (or carries) per carry
x his carries over his last two games:

    projection = (1 - WEIGHT) x per-game average + WEIGHT x per-carry rate x recent carries

Tested on the 2026 season through week 10 (each game predicted from the games before it, players
with 6+ carries): mean squared error, rushing yards 952 -> 913, rush attempts 29.4 -> 27.4,
better on both halves of the players with WEIGHT 0.5 (and the move capped at 30%: 913 -> 908). Not applied to quarterbacks (it made their
carries worse) or to receiving props (recent targets and snap share both made those worse).
LINEMAKER_NFL_USAGE=off turns it off.
"""
import csv
import datetime as dt
import io
import logging
import os
import time
import urllib.request
from typing import Optional

from .oddsapi import norm

log = logging.getLogger("linemaker.nfl_usage")
URL = "https://github.com/nflverse/nflverse-data/releases/download/stats_player/stats_player_week_{season}.csv"
WEIGHT = 0.5
RECENT = 2
CAP = 0.30          # the blend moves a projection at most 30% (tested: 0.3 beat no cap on both halves)
MIN_CARRIES = 6
MARKETS = {"rushing yards": "rushing_yards", "rush attempts": "carries"}
POSITIONS = {"RB", "WR", "TE", "FB"}
TEAMS = {"arizona cardinals": "ARI", "atlanta falcons": "ATL", "baltimore ravens": "BAL",
         "buffalo bills": "BUF", "carolina panthers": "CAR", "chicago bears": "CHI",
         "cincinnati bengals": "CIN", "cleveland browns": "CLE", "dallas cowboys": "DAL",
         "denver broncos": "DEN", "detroit lions": "DET", "green bay packers": "GB",
         "houston texans": "HOU", "indianapolis colts": "IND", "jacksonville jaguars": "JAX",
         "kansas city chiefs": "KC", "las vegas raiders": "LV", "los angeles chargers": "LAC",
         "los angeles rams": "LA", "miami dolphins": "MIA", "minnesota vikings": "MIN",
         "new england patriots": "NE", "new orleans saints": "NO", "new york giants": "NYG",
         "new york jets": "NYJ", "philadelphia eagles": "PHI", "pittsburgh steelers": "PIT",
         "san francisco 49ers": "SF", "seattle seahawks": "SEA", "tampa bay buccaneers": "TB",
         "tennessee titans": "TEN", "washington commanders": "WAS"}
_MEM = {}   # season -> (time, {(norm name, team): [week rows]})


def on() -> bool:
    return (os.environ.get("LINEMAKER_NFL_USAGE", "on").lower() not in ("off", "0", "no", "false")
            and not os.environ.get("LINEMAKER_OFFLINE"))


def season_of(date: str) -> int:
    d = dt.date.fromisoformat(date)
    return d.year if d.month >= 8 else d.year - 1


def _download(season: int) -> str:
    cache = os.path.join(os.environ.get("LINEMAKER_CACHE_DIR", "cache"), "nflverse")
    path = os.path.join(cache, f"stats_player_week_{season}.csv")
    try:
        if time.time() - os.path.getmtime(path) < 12 * 3600:
            with open(path) as f:
                return f.read()
    except OSError:
        pass
    req = urllib.request.Request(URL.format(season=season), headers={"User-Agent": "Linemaker/1.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        text = resp.read().decode("utf-8")
    try:
        os.makedirs(cache, exist_ok=True)
        with open(path, "w") as f:
            f.write(text)
    except OSError:
        pass
    return text


def _f(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def weekly(season: int) -> dict:
    """{(norm player name, team abbrev): [{"week", "position", "carries", "rushing_yards"}]} (REG)."""
    hit = _MEM.get(season)
    if hit and time.time() - hit[0] < 3600:
        return hit[1]
    out = {}
    for r in csv.DictReader(io.StringIO(_download(season))):
        if r.get("season_type") != "REG":
            continue
        out.setdefault((norm(r.get("player_display_name", "")), r.get("team", "")), []).append(
            {"week": int(_f(r.get("week"))), "position": r.get("position", ""),
             "carries": _f(r.get("carries")), "rushing_yards": _f(r.get("rushing_yards"))})
    for v in out.values():
        v.sort(key=lambda x: x["week"])
    _MEM[season] = (time.time(), out)
    return out


def rush_projection(games: list, stat: str, base: float) -> Optional[tuple]:
    """(blended projection, recent carries per game, per-carry rate) or None (thin role, QB)."""
    if not games or games[-1]["position"] not in POSITIONS:
        return None
    carries = sum(g["carries"] for g in games)
    if carries < MIN_CARRIES:
        return None
    per = sum(g[stat] for g in games) / carries
    recent = sum(g["carries"] for g in games[-RECENT:]) / len(games[-RECENT:])
    new = (1 - WEIGHT) * base + WEIGHT * per * recent
    if base > 0:
        new = max(base * (1 - CAP), min(base * (1 + CAP), new))
    return new, recent, per


def apply_usage(date: str, rows: list) -> int:
    """Blend recent carries into running-game projections in place. Returns rows changed."""
    if not on():
        return 0
    todo = [r for r in rows if r.get("market", "").lower() in MARKETS]
    if not todo:
        return 0
    try:
        data = weekly(season_of(date))
    except Exception as e:
        log.warning("nflverse: %s", e)
        return 0
    by_name = {}
    for (name, team), games in data.items():
        by_name.setdefault(name, []).append((team, games))
    changed = 0
    for r in todo:
        team = TEAMS.get(norm(r.get("_team", "")))
        cands = by_name.get(norm(r["subject"]), [])
        games = next((g for t, g in cands if t == team), None) or (cands[0][1] if len(cands) == 1 else None)
        if not games:
            continue
        stat = MARKETS[r["market"].lower()]
        base = float(r["mean"])
        res = rush_projection(games, stat, base)
        if not res:
            continue
        new, recent, per = res
        r["mean"] = f"{new:.3f}"
        r["notes"] += (f"; nflverse workload: {recent:.1f} carries a game over his last {RECENT} "
                       f"({base:.1f} -> {new:.1f}, blended {WEIGHT:.0%})")
        changed += 1
    log.info("nfl workload: %d of %d rushing projections adjusted", changed, len(todo))
    return changed
