"""Free projections built from ESPN game logs (used when FantasyPros has no real data).

Only players with a posted prop get a projection. Per player and market:

  Basketball  minutes x per-minute rate (projection.build_projection: recent minutes weigh in
              fast, efficiency slowly), adjusted by tonight's estimated pace and the
              opponent's estimated defensive rating vs the slate average (points, threes, PRA;
              pace only for rebounds and assists). PRA is the sum of its parts.
  Football    opportunity x efficiency: targets x yards/target, carries x yards/carry,
              attempts x yards/attempt, targets x catch rate; TDs and longest plays per game.
  Hockey,     per-game averages, recent games blended in (10 games = half weight).
  baseball

The spread comes from the player's own game-to-game variation. Players ESPN lists as Out
are skipped; Questionable and Doubtful are passed through as injury flags.
"""
import json
import logging
import statistics
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from .gamelogs import PATHS, _value, game_log, roster_index
from .oddsapi import norm
from .projection import ProjectionInputs, build_projection

log = logging.getLogger("linemaker.espn_projections")
BASKETBALL = {"NBA", "WNBA"}
FOOTBALL = {"NFL", "NCAAF"}
INJURY_MAP = {"out": "out", "doubtful": "doubtful", "questionable": "questionable",
              "day-to-day": "questionable", "game time decision": "questionable",
              "suspension": "out", "injured reserve": "out"}
# Football: market -> (opportunity stat, production stat). Mean = opp/game x production/opp.
FOOTBALL_RATES = {"receiving yards": ("receivingTargets", "receivingYards"),
                  "receptions": ("receivingTargets", "receptions"),
                  "rushing yards": ("rushingAttempts", "rushingYards"),
                  "passing yards": ("passingAttempts", "passingYards"),
                  "pass completions": ("passingAttempts", "completions"),
                  "passing tds": ("passingAttempts", "passingTouchdowns")}
RECENT_K = 10.0


DISPLAY_NAMES = {}   # norm(player) -> name as ESPN writes it


def injuries(sport: str) -> dict:
    """norm(player) -> engine injury status, from ESPN's league injury report."""
    path = PATHS.get(sport)
    if not path:
        return {}
    req = urllib.request.Request(f"https://site.api.espn.com/apis/site/v2/sports/{path}/injuries",
                                 headers={"User-Agent": "Linemaker/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        log.warning("espn injuries %s: %s", sport, e)
        return {}
    out = {}
    for team in data.get("injuries", []):
        for inj in team.get("injuries", []):
            status = INJURY_MAP.get((inj.get("status") or "").strip().lower())
            if status:
                out[norm(inj["athlete"]["displayName"])] = status
                DISPLAY_NAMES[norm(inj["athlete"]["displayName"])] = inj["athlete"]["displayName"]
    return out


def _avg(vals: list) -> Optional[float]:
    vals = [v for v in vals if v is not None]
    return sum(vals) / len(vals) if vals else None


def _sd(vals: list, mean: float) -> float:
    vals = [v for v in vals if v is not None]
    sd = statistics.pstdev(vals) if len(vals) >= 5 else 0.0
    return max(sd, (max(mean, 0.25)) ** 0.5 * 1.05)   # at least a little overdispersed


def _blend(season: Optional[float], recent: Optional[float], n: int) -> Optional[float]:
    if season is None:
        return recent
    if recent is None or n <= 0:
        return season
    w = n / (n + RECENT_K)
    return season + w * (recent - season)


def _minutes(g: dict) -> Optional[float]:
    m = g["stats"].get("minutes")
    return m if m else None


def basketball_mean(games: list, market: str, factors: dict, minutes_delta: float = 0.0) -> Optional[tuple]:
    """(mean, sd, steps) for one basketball market. factors: pace, defense (team rating vs
    average) and dvp ({market: factor} for the player's position); minutes_delta adds or
    removes projected minutes (blowout risk, back-to-back)."""
    if market.lower() == "pra":
        parts = [basketball_mean(games, m, factors, minutes_delta) for m in ("points", "rebounds", "assists")]
        if any(p is None for p in parts):
            return None
        vals = [_value("NBA", "pra", g["stats"]) for g in games]
        mean = sum(p[0] for p in parts)
        return mean, _sd(vals, mean), [("PRA = points + rebounds + assists", f"{mean:.2f}")]
    played = [g for g in games if _minutes(g)]
    if len(played) < 5:
        return None
    vals = [_value("NBA", market, g["stats"]) for g in played]
    rates = [v / _minutes(g) for v, g in zip(vals, played) if v is not None]
    if not rates:
        return None
    recent = played[:5]
    inp = ProjectionInputs(
        season_games=len(played),
        opp_season=_avg([_minutes(g) for g in played]), opp_recent=_avg([_minutes(g) for g in recent]),
        opp_recent_games=len(recent),
        rate_season=sum(v for v in vals if v is not None) / sum(_minutes(g) for g in played),
        rate_recent=_avg(rates[:10]), rate_recent_games=min(10, len(rates)),
        pace_factor=factors.get("pace"),
        matchup_factor=(factors.get("dvp") or {}).get(market.lower())
        or (factors.get("defense") if market.lower() in ("points", "threes") else None))
    proj = build_projection(inp)
    mean, steps = proj.mean, list(proj.steps)
    if minutes_delta and proj.opportunity > 0:
        scale = max(proj.opportunity + minutes_delta, 0.0) / proj.opportunity
        mean *= scale
        steps.append((f"minutes adjusted {minutes_delta:+.1f}", f"{mean:.2f}"))
    return mean, _sd(vals, mean), steps


def football_mean(games: list, market: str) -> Optional[tuple]:
    played = [g for g in games if g["stats"]]
    if len(played) < 3:
        return None
    m = market.lower()
    vals = [_value("NFL", m, g["stats"]) for g in played]
    if m in FOOTBALL_RATES:
        opp_key, prod_key = FOOTBALL_RATES[m]
        opps = [g["stats"].get(opp_key) or 0.0 for g in played]
        prods = [g["stats"].get(prod_key) or 0.0 for g in played]
        if sum(opps) <= 0:
            return None
        recent = played[:4]
        inp = ProjectionInputs(
            season_games=len(played), opp_season=_avg(opps),
            opp_recent=_avg([g["stats"].get(opp_key) or 0.0 for g in recent]),
            opp_recent_games=len(recent),
            rate_season=sum(prods) / sum(opps),
            rate_recent=(sum(g["stats"].get(prod_key) or 0.0 for g in recent)
                         / max(sum(g["stats"].get(opp_key) or 0.0 for g in recent), 1e-9)),
            rate_recent_games=len(recent))
        proj = build_projection(inp)
        return proj.mean, _sd(vals, proj.mean), proj.steps
    mean = _blend(_avg(vals), _avg(vals[:4]), min(4, len(vals)))
    if mean is None or mean <= 0:
        return None
    return mean, _sd(vals, mean), [("per-game average, recent games blended in", f"{mean:.2f}")]


def per_game_mean(sport: str, games: list, market: str) -> Optional[tuple]:
    vals = [v for v in (_value(sport, market, g["stats"]) for g in games) if v is not None]
    if len(vals) < 5:
        return None
    mean = _blend(_avg(vals), _avg(vals[:10]), min(10, len(vals)))
    if mean is None or mean <= 0:
        return None
    return mean, _sd(vals, mean), [("per-game average, recent games blended in", f"{mean:.2f}")]


def _team_factors(sport: str, date: str, teams: set) -> dict:
    """norm(team) -> {"pace": x, "defense": y} vs the slate average (basketball only)."""
    if sport not in BASKETBALL:
        return {}
    from .context import team_contexts
    try:
        ctx = team_contexts(sport, date, teams)
    except Exception as e:
        log.warning("team context unavailable: %s", e)
        return {}
    paces = [c.pace for c in ctx.values() if c.pace]
    drtgs = [c.def_rating for c in ctx.values() if c.def_rating]
    if not paces or not drtgs:
        return {}
    avg_pace, avg_drtg = sum(paces) / len(paces), sum(drtgs) / len(drtgs)
    return {k: {"pace": c.pace / avg_pace if c.pace else None,
                "drtg": c.def_rating / avg_drtg if c.def_rating else None} for k, c in ctx.items()}


def _apply_usage(sport: str, date: str, rows: list, index: dict, hurt: dict):
    """Hand part of each ruled-out rotation player's production to his projected teammates."""
    from .adjustments import usage_boosts
    by_team = {}
    for r in rows:
        by_team.setdefault(norm(r["_team"]), {})[r["subject"]] = r["_games"]
    outs_by_team = {}
    for player, status in hurt.items():
        hit = index.get(player)
        if status == "out" and hit and norm(hit[1]) in by_team:
            outs_by_team.setdefault(norm(hit[1]), []).append((player, hit[0]))
    for team, outs in outs_by_team.items():
        logs = {}
        for player, aid in outs:
            try:
                logs[DISPLAY_NAMES.get(player, player.title())] = game_log(sport, aid, date)
            except Exception as e:
                log.warning("espn game log %s: %s", player, e)
        boosts = usage_boosts(logs, by_team[team], sport)
        for r in rows:
            if norm(r["_team"]) != team:
                continue
            b = boosts.get(r["subject"]) or {}
            m = r["market"].lower()
            add = (sum(b.get(k, 0.0) for k in ("points", "rebounds", "assists")) if m == "pra"
                   else b.get(m, 0.0))
            if add > 0.05:
                r["mean"] = f"{float(r['mean']) + add:.3f}"
                r["notes"] += f"; +{add:.1f} with {', '.join(b['_why'])} out"


def espn_projections(sport: str, date: str, wanted: dict) -> list:
    """wanted: {player name: {markets}} -> projection rows for the pro engine."""
    if sport not in PATHS:
        return []
    index = roster_index(sport, date)
    hurt = injuries(sport)
    teams = {v[1] for v in index.values()} | {v[2] for v in index.values()}
    factors = _team_factors(sport, date, teams)
    spreads, tired, dvp = {}, set(), {}
    if sport in BASKETBALL:
        from .adjustments import spreads_today
        from .game_model import played_yesterday
        spreads, tired = spreads_today(sport, date), played_yesterday(sport, date)
        try:
            from .dvp import position_factors
            dvp = position_factors(sport, date, teams)
        except Exception as e:
            log.warning("defense vs position unavailable: %s", e)
    from .gamelogs import POSITIONS

    def one(item):
        name, markets = item
        hit = index.get(norm(name))
        if not hit or hurt.get(norm(name)) == "out":
            return []
        aid, team, opp, home = hit
        try:
            games = game_log(sport, aid, date)
        except Exception as e:
            log.warning("espn game log %s: %s", name, e)
            return []
        rows = []
        extra = ""
        delta = 0.0
        if sport in BASKETBALL:
            from .adjustments import minutes_delta
            played = [g for g in games if _minutes(g)][:10]
            avg_min = sum(_minutes(g) for g in played) / len(played) if played else 0.0
            delta, why = minutes_delta(avg_min, spreads.get(norm(team)), norm(team) in tired)
            if why:
                extra = "; " + ", ".join(why)
        for market in markets:
            if sport in BASKETBALL:
                f = {}
                mine, theirs = factors.get(norm(team), {}), factors.get(norm(opp), {})
                if mine.get("pace") and theirs.get("pace"):
                    f["pace"] = (mine["pace"] + theirs["pace"]) / 2
                if theirs.get("drtg"):
                    f["defense"] = theirs["drtg"]
                pos = POSITIONS.get((sport, norm(name)))
                if pos and (dvp.get(norm(opp)) or {}).get(pos):
                    table = dvp[norm(opp)][pos]
                    f["dvp"] = dict(table, pra=None)
                res = basketball_mean(games, market, f, delta)
            elif sport in FOOTBALL:
                res = football_mean(games, market)
            else:
                res = per_game_mean(sport, games, market)
            if not res:
                continue
            mean, sd, steps = res
            row = {"sport": sport, "subject": name, "market": market, "mean": f"{mean:.3f}",
                   "stdev": f"{sd:.3f}",
                   "notes": f"ESPN game-log model ({len(games)} games); {team}{extra}",
                   "_team": team, "_games": games}
            if hurt.get(norm(name)):
                row["injury_status"] = hurt[norm(name)]
            rows.append(row)
        return rows

    out = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        for rows in pool.map(one, wanted.items()):
            out += rows
    if sport in BASKETBALL:
        _apply_usage(sport, date, out, index, hurt)
    for r in out:
        r.pop("_team", None)
        r.pop("_games", None)
    log.info("espn projections %s: %d players asked, %d projections built", sport, len(wanted), len(out))
    return out
