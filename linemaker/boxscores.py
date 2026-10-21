"""Final player box scores from ESPN's public site API -> results rows for player props.

Free and keyless, but unofficial: ESPN doesn't document it and may change it. Only games
marked final are used. A player who didn't play gets no result (books void those props).
Players are matched by name across that day's games; a name that appears twice is skipped.
"""
import json
import logging
import time
import urllib.error
import urllib.request
from typing import Optional

from .oddsapi import norm

log = logging.getLogger("linemaker.boxscores")
BASE = "https://site.api.espn.com/apis/site/v2/sports"
SPORT_PATH = {
    "NBA": "basketball/nba", "WNBA": "basketball/wnba", "NFL": "football/nfl",
    "NCAAF": "football/college-football", "MLB": "baseball/mlb", "NHL": "hockey/nhl",
}


def _get(path: str, **params) -> dict:
    query = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"{BASE}/{path}" + (f"?{query}" if query else "")
    req = urllib.request.Request(url, headers={"User-Agent": "Linemaker/1.0"})
    started = time.time()
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        log.warning("espn %s: HTTP %s", path, e.code)
        raise
    log.info("espn %s: %.1fs", path, time.time() - started)
    return data


def _num(v) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_box(summary: dict) -> dict:
    """norm(player name) -> {"group.key": value}. Compound cells ('12-21', '24/34') split."""
    players = {}
    for team in summary.get("boxscore", {}).get("players", []):
        for group in team.get("statistics", []):
            gname = group.get("name") or group.get("type") or ""
            keys = group.get("keys") or []
            for a in group.get("athletes", []):
                if a.get("didNotPlay") or not a.get("stats"):
                    continue
                name = a["athlete"]["displayName"]
                row = players.setdefault(norm(name), {"_name": name})
                for key, cell in zip(keys, a["stats"]):
                    for sep in ("-", "/"):
                        if sep in key and isinstance(cell, str) and sep in cell[1:]:
                            left, right = key.split(sep, 1)
                            a_val, b_val = cell.split(sep, 1)
                            row[f"{gname}.{left}"] = _num(a_val)
                            row[f"{gname}.{right}"] = _num(b_val)
                            break
                    else:
                        row[f"{gname}.{key}"] = _num(cell)
    return players


def _first(stats: dict, *keys) -> Optional[float]:
    """First key present; a player missing from a table (no carries) counts as 0."""
    for k in keys:
        if stats.get(k) is not None:
            return stats[k]
    return None


def _sum(stats: dict, *keys) -> Optional[float]:
    vals = [stats.get(k) for k in keys]
    if all(v is None for v in vals):
        return None
    return sum(v or 0 for v in vals)


def market_value(sport: str, market: str, s: dict) -> Optional[float]:
    m, sp = market.lower(), sport.upper()
    if sp in ("NBA", "WNBA"):
        table = {"points": ("points",), "rebounds": ("rebounds",), "assists": ("assists",),
                 "threes": ("threePointFieldGoalsMade",), "pra": ("points", "rebounds", "assists")}
        keys = table.get(m)
        if not keys:
            return None
        group = next((k.split(".")[0] for k in s if k.endswith(".points")), "")
        vals = [s.get(f"{group}.{k}") for k in keys]
        return None if any(v is None for v in vals) else sum(vals)
    if sp in ("NFL", "NCAAF"):
        if m == "anytime td":
            return _sum(s, "rushing.rushingTouchdowns", "receiving.receivingTouchdowns",
                        "kickReturns.kickReturnTouchdowns", "puntReturns.puntReturnTouchdowns")
        table = {"passing yards": "passing.passingYards", "passing tds": "passing.passingTouchdowns",
                 "pass completions": "passing.completions", "pass attempts": "passing.passingAttempts",
                 "rushing yards": "rushing.rushingYards", "rush attempts": "rushing.rushingAttempts",
                 "longest rush": "rushing.longRushing", "receiving yards": "receiving.receivingYards",
                 "receptions": "receiving.receptions", "longest reception": "receiving.longReception"}
        key = table.get(m)
        if key is None:
            return None
        if s.get(key) is not None:
            return s[key]
        # Played, but not in that table (e.g. a WR with no carries): that stat was 0.
        return 0.0 if any(k.split(".")[0] in ("passing", "rushing", "receiving") for k in s) else None
    if sp == "MLB":
        if m == "strikeouts":
            return s.get("pitching.strikeouts")
        return {"hits": s.get("batting.hits"), "home runs": s.get("batting.homeRuns")}.get(m)
    if sp == "NHL":
        group = next((k.split(".")[0] for k in s if k.endswith(".goals")), None)
        if group is None:
            return None
        g, a, sog = (s.get(f"{group}.{k}") for k in ("goals", "assists", "shotsTotal"))
        return {"shots on goal": sog, "hockey points": None if g is None or a is None else g + a,
                "anytime goals": g}.get(m)
    return None


def final_stats(sport: str, date: str) -> dict:
    """norm(name) -> stats for every player in that day's final games (duplicates dropped)."""
    path = SPORT_PATH.get(sport.upper())
    if path is None:
        return {}
    board = _get(f"{path}/scoreboard", dates=date.replace("-", ""))
    merged, dupes = {}, set()
    for event in board.get("events", []):
        if not event.get("status", {}).get("type", {}).get("completed"):
            continue
        for key, row in parse_box(_get(f"{path}/summary", event=event["id"])).items():
            if key in merged:
                dupes.add(key)
            merged[key] = row
    for key in dupes:
        merged.pop(key, None)
    return merged


def prop_results(records: dict, date: str, fetch=final_stats) -> list:
    """Results rows (grade/apply_results format) for tracked or frozen prop records."""
    sports = sorted({r.get("sport", "").upper() for r in records.values()
                     if r.get("inputs", {}).get("bet_type") == "prop" and r.get("sport")})
    rows = []
    for sport in sports:
        try:
            stats = fetch(sport, date)
        except Exception as e:   # unofficial source: a failure here shouldn't stop grading
            log.warning("espn %s box scores unavailable: %s", sport, e)
            continue
        for rec in records.values():
            if rec.get("sport", "").upper() != sport or rec["inputs"].get("bet_type") != "prop":
                continue
            player = stats.get(norm(rec["subject"]))
            value = market_value(sport, rec["market"], player) if player else None
            if value is None:
                continue
            rows.append({"game_id": rec["game_id"], "subject": rec["subject"],
                         "market": rec["market"], "line": str(rec["inputs"]["line"]),
                         "actual": f"{value:g}"})
    return rows
