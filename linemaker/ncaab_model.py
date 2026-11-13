"""College basketball game ratings: every Division I result this season, adjusted for the
opponents each team played.

ESPN's college standings only count conference games (all zeros until January), and raw
points per game ignore schedule strength, which matters a lot across 365 teams. So:

  1. Every final score since the season opened (ESPN scoreboard, all D-I, one call per day;
     past days are saved to disk because they never change).
  2. Opponent-adjusted offense and defense, iterated: a team's offense is the points it
     scored minus how much easier or harder than average each opponent's defense was
     (and the same for defense), with home court taken out (HOME_EDGE) and two "average"
     games mixed in so a 2-game sample can't swing far.
  3. Early on, blended with last season (ESPN conference standings, pulled a third of the
     way back to average) by games played: w = gp / (gp + K).

Neutral-site games (early-season tournaments) get no home edge. Not modelled: injuries,
lineups, travel. The market usually knows more; this is a second opinion.
"""
import datetime as dt
import json
import logging
import os
import statistics
import urllib.request
from typing import Optional

from .oddsapi import norm

log = logging.getLogger("linemaker.ncaab")
PATH = "basketball/mens-college-basketball"
HOME_EDGE = 3.0          # points; college home court is worth about 3
PRIOR_GAMES = 2.0        # "average" games mixed into each team's adjusted numbers
SEASON_K = 6             # this season's games that count as much as all of last season
ITERATIONS = 25


def season_start(date: str) -> dt.date:
    d = dt.date.fromisoformat(date)
    return dt.date(d.year if d.month >= 8 else d.year - 1, 11, 1)


def _cache_path(day: str) -> str:
    return os.path.join(os.environ.get("LINEMAKER_CACHE_DIR", "cache"), "ncaab", f"scoreboard_{day}.json")


def scoreboard(day: str) -> dict:
    """ESPN's D-I scoreboard for one date (finished days come from the disk cache)."""
    path = _cache_path(day)
    past = dt.date.fromisoformat(day) < dt.date.today() - dt.timedelta(days=1)
    if past and os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except (OSError, ValueError):
            pass
    url = (f"https://site.api.espn.com/apis/site/v2/sports/{PATH}/scoreboard"
           f"?dates={day.replace('-', '')}&groups=50&limit=500")
    req = urllib.request.Request(url, headers={"User-Agent": "Linemaker/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        log.warning("espn ncaab scoreboard %s: %s", day, e)
        return {}
    if past:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                json.dump({"events": data.get("events", [])}, f)
        except OSError:
            pass
    return data


def _teams(comp: dict) -> dict:
    return {c.get("homeAway"): c for c in comp.get("competitors", [])}


def parse_results(data: dict) -> list:
    """[(home, away, home pts, away pts, neutral)] for the finished games on a scoreboard."""
    out = []
    for ev in data.get("events", []):
        comp = (ev.get("competitions") or [{}])[0]
        if not ((comp.get("status") or {}).get("type") or {}).get("completed"):
            continue
        t = _teams(comp)
        if "home" not in t or "away" not in t:
            continue
        try:
            hs, as_ = float(t["home"]["score"]), float(t["away"]["score"])
        except (KeyError, TypeError, ValueError):
            continue
        out.append((t["home"]["team"]["displayName"], t["away"]["team"]["displayName"], hs, as_,
                    bool(comp.get("neutralSite"))))
    return out


_RESULTS = {}   # date -> (time read, results): reused for an hour (every scan prices from them)


def season_results(date: str) -> list:
    """Every finished D-I game from opening night to the day before `date`."""
    import time
    hit = _RESULTS.get(date)
    if hit and time.time() - hit[0] < 3600:
        return hit[1]
    out = _season_results(date)
    _RESULTS[date] = (time.time(), out)
    return out


def _season_results(date: str) -> list:
    day, end, out = season_start(date), dt.date.fromisoformat(date), []
    while day < end:
        out += parse_results(scoreboard(day.isoformat()))
        day += dt.timedelta(days=1)
    return out


def neutral_games(date: str) -> set:
    """{(norm away, norm home)} for the date's neutral-site games."""
    out = set()
    for ev in scoreboard(date).get("events", []):
        comp = (ev.get("competitions") or [{}])[0]
        t = _teams(comp)
        if comp.get("neutralSite") and "home" in t and "away" in t:
            out.add((norm(t["away"]["team"]["displayName"]), norm(t["home"]["team"]["displayName"])))
    return out


def adjusted(games: list) -> dict:
    """norm(team) -> {"off", "def", "games"} opponent-adjusted points scored / allowed."""
    if not games:
        return {}
    avg = statistics.mean(x for g in games for x in (g[2], g[3]))
    rows = {}
    for home, away, hs, as_, neutral in games:
        edge = 0.0 if neutral else HOME_EDGE / 2
        h, a = norm(home), norm(away)
        rows.setdefault(h, []).append((a, hs - edge, as_ + edge))   # (opponent, scored, allowed), home court removed
        rows.setdefault(a, []).append((h, as_ + edge, hs - edge))
    off = {t: avg for t in rows}
    de = {t: avg for t in rows}
    for _ in range(ITERATIONS):
        new_off, new_de = {}, {}
        for t, gs in rows.items():
            s_off = sum(sc - (de[o] - avg) for o, sc, _al in gs)
            s_de = sum(al - (off[o] - avg) for o, _sc, al in gs)
            new_off[t] = (s_off + PRIOR_GAMES * avg) / (len(gs) + PRIOR_GAMES)
            new_de[t] = (s_de + PRIOR_GAMES * avg) / (len(gs) + PRIOR_GAMES)
        off, de = new_off, new_de
    return {t: {"off": off[t], "def": de[t], "games": len(rows[t]), "avg": avg} for t in rows}


def ratings(date: str, last_season: Optional[dict] = None) -> dict:
    """norm(team) -> {"scored", "allowed", "games", "avg", "basis"}: this season's adjusted
    numbers blended with last season's (game_model.standings, regressed) by games played."""
    now = adjusted(season_results(date))
    last = last_season or {}
    vals = [x for gp, pf, pa in last.values() if gp > 0 for x in (pf, pa)]
    avg_last = statistics.mean(vals) if vals else None
    avg_now = next(iter(now.values()))["avg"] if now else None
    avg = avg_now if avg_now is not None else avg_last
    if avg is None:
        return {}
    out = {}
    for team in set(now) | set(last):
        cur = now.get(team)
        gp, pf, pa = last.get(team, (0, avg, avg))
        lpf = pf + (avg - pf) / 3 if gp else avg
        lpa = pa + (avg - pa) / 3 if gp else avg
        if avg_last is not None and avg_now is not None:   # put last season on this season's scale
            lpf += avg_now - avg_last
            lpa += avg_now - avg_last
        n = cur["games"] if cur else 0
        w = n / (n + SEASON_K) if n else 0.0
        scored = w * cur["off"] + (1 - w) * lpf if cur else lpf
        allowed = w * cur["def"] + (1 - w) * lpa if cur else lpa
        out[team] = {"scored": scored, "allowed": allowed, "games": n, "avg": avg,
                     "basis": f"{n} games this season, opponent-adjusted"
                              + ("" if w > 0.9 else f", {1 - w:.0%} last season")}
    return out
