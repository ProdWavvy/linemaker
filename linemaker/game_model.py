"""Free game-line model: moneyline, spread and total projections from ESPN standings + Kalshi.

Per team (ESPN standings, unofficial): points (goals, runs) scored and allowed per game. Early
in a season the current numbers are blended with last season's, which are themselves pulled
a third of the way back to league average (rosters change over a summer):

  w        = gp / (gp + K)                     K per sport (NBA 15 games, NFL 4, MLB 40, ...)
  scored   = w * this season + (1 - w) * last season (regressed)
Per game, each side's expected score is its offense plus the other side's defense, relative
to league average (home team's view):

  home pts = home scored + away allowed - league avg     (away pts likewise)
  margin   = home pts - away pts + home edge - rest penalties
  total    = home pts + away pts

Rest: a team that also played yesterday (back-to-back) loses REST_PENALTY (NBA, WNBA, NHL).
Kalshi: when the game has a Kalshi market, its win probability is turned into a margin on the
same normal curve (margin = sd * z(p)) and blended in at LINEMAKER_KALSHI_WEIGHT (default
0.4). The engine then blends this with the sportsbook no-vig price as usual.

Not modelled: injuries and lineups, starting pitchers and goalies, travel, motivation. Those
are why the market usually knows more; the model is a second opinion, not an oracle.
"""
import datetime as dt
import functools
import json
import logging
import os
import statistics
import urllib.request
from typing import Optional

from .gamelogs import PATHS, _season_year
from .oddsapi import norm
from .sim import DEFAULT_NORMAL_SD

log = logging.getLogger("linemaker.game_model")
SUPPORTED = ("NBA", "WNBA", "NFL", "NCAAF", "NHL", "MLB", "UFC")
# Games of this season that count as much as all of last season.
SEASON_K = {"NBA": 15, "WNBA": 10, "NFL": 4, "NCAAF": 4, "NHL": 20, "MLB": 40}
HOME_EDGE = {"NBA": 2.3, "WNBA": 2.0, "NFL": 1.5, "NCAAF": 2.8, "NHL": 0.15, "MLB": 0.15}
REST_PENALTY = {"NBA": 1.5, "WNBA": 1.0, "NHL": 0.12}
SD = {**{k[0]: v for k, v in DEFAULT_NORMAL_SD.items() if k[1] == "margin"}, "MLB": 4.4}
TOTAL_SD = {**{k[0]: v for k, v in DEFAULT_NORMAL_SD.items() if k[1] == "total"}, "MLB": 4.5}
LAST_SEASON_REGRESS = 1 / 3


def kalshi_weight() -> float:
    try:
        return min(max(float(os.environ.get("LINEMAKER_KALSHI_WEIGHT", "0.4")), 0.0), 1.0)
    except ValueError:
        return 0.4


def _f(v) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


@functools.lru_cache(maxsize=16)
def standings(sport: str, season: int) -> dict:
    """norm(team) -> (games, scored per game, allowed per game)."""
    req = urllib.request.Request(
        f"https://site.api.espn.com/apis/v2/sports/{PATHS[sport]}/standings?season={season}",
        headers={"User-Agent": "Linemaker/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        log.warning("espn standings %s %s: %s", sport, season, e)
        return {}
    return parse_standings(data)


def parse_standings(data: dict) -> dict:
    out = {}

    def walk(node):
        for child in node.get("children", []):
            walk(child)
        for entry in node.get("standings", {}).get("entries", []):
            st = {s.get("name"): _f(s.get("value")) for s in entry.get("stats", [])}
            gp = st.get("gamesPlayed") or sum(st.get(k) or 0 for k in
                                              ("wins", "losses", "ties", "otLosses"))
            pf = st.get("avgPointsFor")
            pa = st.get("avgPointsAgainst")
            if pf is None and gp and st.get("pointsFor") is not None:
                pf = st["pointsFor"] / gp
            if pa is None and gp and st.get("pointsAgainst") is not None:
                pa = st["pointsAgainst"] / gp
            if pf is not None and pa is not None:
                out[norm(entry["team"]["displayName"])] = (int(gp or 0), pf, pa)
    walk(data)
    return out


def team_ratings(sport: str, date: str) -> dict:
    """norm(team) -> {"scored", "allowed", "games", "basis"} blended across two seasons."""
    year = _season_year(sport, date)
    now, last = standings(sport, year), standings(sport, year - 1)

    def league_avg(table):
        vals = [x for gp, pf, pa in table.values() if gp > 0 for x in (pf, pa)]
        return statistics.mean(vals) if vals else None

    avg_now, avg_last = league_avg(now), league_avg(last)
    if avg_now is None and avg_last is None:
        return {}
    avg_last = avg_last if avg_last is not None else avg_now
    k = SEASON_K.get(sport, 10)
    out = {}
    for team in set(now) | set(last):
        gp, pf, pa = now.get(team, (0, 0.0, 0.0))
        _, lpf, lpa = last.get(team, (0, avg_last, avg_last))
        lpf += (avg_last - lpf) * LAST_SEASON_REGRESS
        lpa += (avg_last - lpa) * LAST_SEASON_REGRESS
        w = gp / (gp + k) if gp > 0 and avg_now is not None else 0.0
        avg = w * avg_now + (1 - w) * avg_last if w else avg_last
        out[team] = {"scored": w * pf + (1 - w) * lpf, "allowed": w * pa + (1 - w) * lpa,
                     "games": gp, "avg": avg,
                     "basis": f"{gp} games this season" + ("" if w > 0.9 else
                                                          f", {1 - w:.0%} last season")}
    return out


def _find(ratings: dict, team: str) -> Optional[dict]:
    key = norm(team)
    if key in ratings:
        return ratings[key]
    nick = key.split()[-1:]
    hits = [v for k, v in ratings.items() if k.split()[-1:] == nick]
    return hits[0] if len(hits) == 1 else None


def played_yesterday(sport: str, date: str) -> set:
    """norm(team) for teams that played the day before (back-to-backs)."""
    if sport not in REST_PENALTY:
        return set()
    from .context import _team_ids
    prev = (dt.date.fromisoformat(date) - dt.timedelta(days=1)).isoformat()
    try:
        return set(_team_ids(sport, prev))
    except Exception as e:
        log.warning("espn scoreboard %s %s: %s", sport, prev, e)
        return set()


def _z(p: float) -> float:
    return statistics.NormalDist().inv_cdf(min(max(p, 0.01), 0.99))


def project_game(sport: str, home: str, away: str, ratings: dict, tired: set,
                 kalshi_home: Optional[float]) -> Optional[dict]:
    """{"margin", "total", "steps"} from the home team's view, or None without ratings."""
    h, a = _find(ratings, home), _find(ratings, away)
    if not h or not a:
        return None
    pts_h = h["scored"] + a["allowed"] - h["avg"]
    pts_a = a["scored"] + h["allowed"] - h["avg"]
    margin = pts_h - pts_a + HOME_EDGE.get(sport, 0.0)
    steps = [(f"{home} expected score ({h['basis']})", f"{pts_h:.2f}"),
             (f"{away} expected score ({a['basis']})", f"{pts_a:.2f}"),
             ("home edge", f"{HOME_EDGE.get(sport, 0.0):+.2f}")]
    pen = REST_PENALTY.get(sport, 0.0)
    for team, sign in ((home, -1), (away, 1)):
        if norm(team) in tired:
            margin += sign * pen
            steps.append((f"{team} on a back-to-back", f"{sign * pen:+.2f}"))
    sd = SD.get(sport, 12.0)
    if kalshi_home is not None and kalshi_weight() > 0:
        k_margin = sd * _z(kalshi_home)
        steps.append((f"ratings margin {margin:+.2f}; Kalshi {kalshi_home:.1%} home = "
                      f"{k_margin:+.2f}, weighted {kalshi_weight():.0%}", ""))
        margin += kalshi_weight() * (k_margin - margin)
    steps.append(("projected home margin", f"{margin:+.2f}"))
    total = pts_h + pts_a
    steps.append(("projected total", f"{total:.2f}"))
    return {"margin": margin, "total": total, "steps": steps}


def game_projections(sport: str, date: str, games: list, use_kalshi: bool = True) -> list:
    """games: [(away, home)] -> projection rows (moneyline, spread, game total) for the engine."""
    if sport not in SUPPORTED or not games:
        return []
    if sport == "UFC":
        from .ufc_model import ufc_projections
        return ufc_projections(date, games)
    ratings = team_ratings(sport, date)
    tired = played_yesterday(sport, date)
    kalshi = []
    if use_kalshi and kalshi_weight() > 0:
        from .context import kalshi_win_probs
        try:
            kalshi = kalshi_win_probs(sport, date)
        except Exception as e:
            log.warning("kalshi unavailable: %s", e)
    from .context import game_kalshi
    goalies = {}
    if sport == "NHL":
        from .nhl_goalies import goalie_effects
        try:
            goalies = goalie_effects(date)
        except Exception as e:
            log.warning("nhl goalies unavailable: %s", e)
    rows = []
    for away, home in games:
        k = game_kalshi(kalshi, away, home) if kalshi else None
        g = project_game(sport, home, away, ratings, tired, k.get(home) if k else None)
        if not g:
            log.info("game model: no ratings for %s @ %s", away, home)
            continue
        note = "game model: ESPN ratings" + (" + Kalshi" if k else "")
        gh, ga = goalies.get(norm(home)), goalies.get(norm(away))
        if gh or ga:   # a backup in net: extra goals for the other side
            eh, ea = (gh or {}).get("extra", 0.0), (ga or {}).get("extra", 0.0)
            g["margin"] += ea - eh
            g["total"] += ea + eh
            note += "; " + "; ".join(x["note"] for x in (gh, ga) if x)
        sd = SD.get(sport, 12.0)
        for market in ("moneyline", "spread"):
            rows.append({"sport": sport, "subject": home, "market": market,
                         "mean": f"{g['margin']:.3f}", "stdev": f"{sd:g}", "notes": note})
        rows.append({"sport": sport, "subject": home, "market": "game total",
                     "mean": f"{g['total']:.3f}", "stdev": f"{TOTAL_SD.get(sport, 15.0):g}",
                     "notes": note})
    log.info("game model %s: %d games asked, %d projected", sport, len(games), len(rows) // 3)
    return rows
