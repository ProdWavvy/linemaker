"""NHL starting goalies: who's in net tonight, and what a backup does to the numbers.

Tonight's starter comes from ESPN's scoreboard ("probable" until the team confirms it). The
usual starter is whoever started most of the team's last 10 games (ESPN box scores, cached).
When they differ, the gap in goaltending is turned into goals:

  save %      career saves and shots against, pulled toward the league's .900 by 1000 shots
              (a backup with 300 career shots stays close to .900)
  extra goals (usual starter's save % - tonight's save %) x 29 shots against per game

Those extra goals feed:
  - the opposing skaters' points and goals props: x (1 + extra goals / 3.0 team goals a game)
  - the game model: total up by the extra goals, margin moved toward the other team
A team with its usual starter in net gets no adjustment (his work is already in the team's
numbers). Shots on goal props aren't touched: a weaker goalie doesn't change how much a
skater shoots.
"""
import collections
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from .oddsapi import norm

log = logging.getLogger("linemaker.nhl_goalies")
LEAGUE_SV = 0.900
PRIOR_SHOTS = 1000
SHOTS_AGAINST = 29.0
TEAM_GOALS = 3.0
LAST_GAMES = 10
MIN_EXTRA = 0.05   # smaller than this isn't worth a note
MIN_GAMES = 4      # this season's games needed before anyone counts as the usual starter
SHARE = 0.6        # ...and he must have started at least 60% of them (else it's a tandem)


def tonight(date: str) -> dict:
    """norm(team) -> {"team", "name", "id", "confirmed", "team_id", "game"} for the date."""
    from .boxscores import _get
    try:
        data = _get("hockey/nhl/scoreboard", dates=date.replace("-", ""))
    except Exception as e:
        log.warning("nhl scoreboard %s: %s", date, e)
        return {}
    out = {}
    for ev in data.get("events", []):
        comp = (ev.get("competitions") or [{}])[0]
        for c in comp.get("competitors", []):
            team = c.get("team", {}).get("displayName", "")
            g = next((p for p in c.get("probables") or [] if p.get("name") == "probableStartingGoalie"), None)
            if not g:
                continue
            out[norm(team)] = {"team": team, "name": g["athlete"]["displayName"], "id": str(g["athlete"]["id"]),
                               "confirmed": (g.get("status") or {}).get("type") == "confirmed",
                               "team_id": c.get("team", {}).get("id"), "game": ev.get("name", "")}
    return out


def _starter(summary: dict, team: str) -> Optional[tuple]:
    """(name, id) of the goalie who played the most minutes for `team` in one box score."""
    for t in summary.get("boxscore", {}).get("players", []):
        if norm(t.get("team", {}).get("displayName", "")) != norm(team):
            continue
        for group in t.get("statistics", []):
            keys = group.get("keys") or []
            if "saves" not in keys or "timeOnIce" not in keys:
                continue
            best = None
            for a in group.get("athletes", []):
                toi = str(a["stats"][keys.index("timeOnIce")]) if a.get("stats") else "0:00"
                try:
                    mins = int(toi.split(":")[0]) + int(toi.split(":")[1]) / 60
                except (ValueError, IndexError):
                    mins = 0
                if best is None or mins > best[0]:
                    best = (mins, a["athlete"]["displayName"], str(a["athlete"]["id"]))
            if best:
                return best[1], best[2]
    return None


def usual_starter(team: str, team_id: str, date: str) -> Optional[tuple]:
    """(name, id, starts, games) of the clear #1: started most of this season's last 10 games
    (at least MIN_GAMES of them, and SHARE of the starts). None for a tandem or a thin sample,
    where the team's numbers already mix both goalies."""
    from .dvp import _finished, _summary
    games = [eid for _d, eid in _finished("NHL", team_id, date)[:LAST_GAMES]]   # this season only
    if len(games) < MIN_GAMES:
        return None
    with ThreadPoolExecutor(max_workers=4) as pool:
        summaries = list(pool.map(lambda eid: _summary("NHL", eid), games))
    starts = collections.Counter(s for s in (_starter(x, team) for x in summaries if x) if s)
    if not starts:
        return None
    (name, gid), n = starts.most_common(1)[0]
    total = sum(starts.values())
    return (name, gid, n, total) if n >= SHARE * total else None


def save_pct(goalie_id: str) -> Optional[float]:
    """Career save %, pulled toward .900 by PRIOR_SHOTS shots."""
    from .ufc_model import _get as get_json   # same cached ESPN JSON reader
    try:
        data = get_json(f"https://sports.core.api.espn.com/v2/sports/hockey/leagues/nhl/athletes/{goalie_id}/statistics")
    except Exception as e:
        log.warning("nhl goalie %s: %s", goalie_id, e)
        return None
    s = {x["name"]: x.get("value") for c in data.get("splits", {}).get("categories", []) for x in c.get("stats", [])}
    saves, shots = s.get("saves") or 0, s.get("shotsAgainst") or 0
    return (saves + LEAGUE_SV * PRIOR_SHOTS) / (shots + PRIOR_SHOTS)


def goalie_effects(date: str) -> dict:
    """norm(team) -> {"extra": goals allowed above the usual starter (negative = better),
    "tonight", "usual", "confirmed", "note"} for teams NOT starting their usual goalie."""
    starters = tonight(date)

    def one(item):
        key, g = item
        try:
            usual = usual_starter(g["team"], g["team_id"], date)
        except Exception as e:
            log.warning("nhl usual starter %s: %s", g["team"], e)
            return key, None
        if not usual or usual[1] == g["id"]:
            return key, None
        sv_usual, sv_now = save_pct(usual[1]), save_pct(g["id"])
        if sv_usual is None or sv_now is None:
            return key, None
        extra = (sv_usual - sv_now) * SHOTS_AGAINST
        word = "confirmed" if g["confirmed"] else "probable"
        note = (f"{g['name']} in net ({word}) instead of {usual[0]} (started {usual[2]} of last "
                f"{usual[3]}): save % {sv_now:.3f} vs {sv_usual:.3f}, {extra:+.2f} goals allowed")
        return key, {"extra": extra, "tonight": g["name"], "usual": usual[0],
                     "confirmed": g["confirmed"], "note": note, "team": g["team"]}
    with ThreadPoolExecutor(max_workers=6) as pool:
        found = dict(pool.map(one, starters.items()))
    return {k: v for k, v in found.items() if v and abs(v["extra"]) >= MIN_EXTRA}


def skater_factor(extra: float) -> float:
    """Multiplier for the opposing skaters' points / goals."""
    return max(0.7, min(1.4, 1 + extra / TEAM_GOALS))


def goalie_starts(date: str) -> dict:
    """norm(team) -> (goalie name, confirmed) for tonight: what the news loop compares."""
    return {k: (v["name"], v["confirmed"]) for k, v in tonight(date).items()}
