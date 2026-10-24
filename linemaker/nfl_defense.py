"""NFL defense matchups: how much each defense gives up through the air and on the ground.

From each team's last 8 finished games (this season, topped up with last season's final
games early on; ESPN box scores, cached), the opponents' per-game totals: passing yards,
completions, receptions, receiving yards, rushing yards. Against the slate average:

  factor = 1 + n/(n + 6) x (defense's allowed / slate average - 1)      capped at ±20%

A defense allowing 260 passing yards against a 220 average lifts the opposing QB's passing
yards and the receivers' yards ~12% with 8 games behind it; a thin sample stays near 1.
"""
import logging
from concurrent.futures import ThreadPoolExecutor

from .oddsapi import norm

log = logging.getLogger("linemaker.nfl_defense")
GAMES, SHRINK, CAP = 8, 6, 0.20
# prop market -> (box score group, stat key) the defense is judged on
MARKETS = {"passing yards": ("passing", "passingYards"), "pass completions": ("passing", "completions"),
           "receiving yards": ("receiving", "receivingYards"), "receptions": ("receiving", "receptions"),
           "rushing yards": ("rushing", "rushingYards")}


def _cell(v) -> float:
    """'29/40' -> 29 (completions), '300' -> 300."""
    return float(str(v).split("/")[0])


def allowed(summary: dict, team: str) -> dict:
    """{stat key: total} the opponents of `team` produced in one box score."""
    out = {}
    for t in summary.get("boxscore", {}).get("players", []):
        if norm(t.get("team", {}).get("displayName", "")) == norm(team):
            continue
        for group in t.get("statistics", []):
            keys = group.get("keys") or []
            for market, (gname, key) in MARKETS.items():
                if group.get("name") != gname:
                    continue
                col = next((i for i, k in enumerate(keys) if k == key or k.startswith(key + "/")
                            or (key == "completions" and k.startswith("completions/"))), None)
                if col is None:
                    continue
                for a in group.get("athletes", []):
                    try:
                        out[key] = out.get(key, 0.0) + _cell(a["stats"][col])
                    except (KeyError, IndexError, ValueError, TypeError):
                        continue
    return out


def defense_factors(date: str, teams: set) -> dict:
    """norm(team) -> {prop market: multiplier for the OPPOSING players}."""
    from .context import _team_ids
    from .dvp import _summary, _team_games
    ids = _team_ids("NFL", date)

    def one(team):
        hit = ids.get(norm(team))
        if not hit:
            return team, None
        games = _team_games("NFL", hit[0], date, GAMES)
        per = [allowed(s, hit[1]) for s in (_summary("NFL", e) for e in games) if s]
        per = [p for p in per if p.get("passingYards") is not None]
        if not per:
            return team, None
        return team, ({k: sum(p.get(k, 0.0) for p in per) / len(per) for _g, k in MARKETS.values()}, len(per))
    with ThreadPoolExecutor(max_workers=8) as pool:
        got = {norm(t): r for t, r in pool.map(one, sorted(teams)) if r}
    if not got:
        return {}
    avg = {k: sum(v[k] for v, _n in got.values()) / len(got) for _g, k in MARKETS.values()}
    out = {}
    for team, (v, n) in got.items():
        out[team] = {}
        for market, (_g, k) in MARKETS.items():
            if avg[k] > 0:
                f = 1 + n / (n + SHRINK) * (v[k] / avg[k] - 1)
                out[team][market] = round(max(1 - CAP, min(1 + CAP, f)), 3)
    log.info("nfl defense factors: %d teams", len(out))
    return out
