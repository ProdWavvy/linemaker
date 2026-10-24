"""Automatic scans: one shortly before each game tips, and one when injury news breaks.

Pre-tip   LINEMAKER_PRETIP_MINUTES (default 45) before each game, props for just that game
          are priced (lineups are usually confirmed by then, so these are the sharpest
          reads of the day). Games tipping together are scanned together. Only posts when
          something clears the gates. ~5-10 credits per game.
News      Every LINEMAKER_NEWS_MINUTES (default 12) ESPN's injury report is checked (free).
          When a rotation player on a team that hasn't started yet changes to Out (basketball:
          18+ minutes; NFL: a QB, or 2.5+ catches / 6+ carries a game), his game is re-priced
          with the teammate bump and posted. NHL: when a team's starting goalie changes, the
          game is re-priced with the goalie adjustment. MLB: when a team's probable starter
          changes (a scratch), the game is re-priced against the new starter. Capped at LINEMAKER_NEWS_MAX_ALERTS
          per day (default 8).
"""
import datetime as dt
import json
import logging
import os
from typing import Optional

from .oddsapi import norm

log = logging.getLogger("linemaker.alerts")
NEWS_SPORTS = ("NBA", "WNBA", "NFL")
GOALIE_SPORTS = ("NHL",)


def _env_int(name: str, default: Optional[int]) -> Optional[int]:
    raw = os.environ.get(name)
    if raw is None:
        return default
    raw = raw.strip()
    return int(raw) if raw else None


def pretip_minutes() -> Optional[int]:
    return _env_int("LINEMAKER_PRETIP_MINUTES", 45)


def arm_pretip(sport: str, date: str) -> bool:
    """Asking for a league's props turns on its pre-tip scans for the rest of that day.
    Returns True when this call switched it on (False if already on, or pre-tip is off)."""
    if not pretip_minutes():
        return False
    armed = set(load_state(f"pretip_armed_{date}.json", []))
    if sport.upper() in armed:
        return False
    armed.add(sport.upper())
    save_state(f"pretip_armed_{date}.json", sorted(armed))
    return True


def pretip_sports(date: str, configured: list) -> list:
    """Leagues to pre-tip scan today: LINEMAKER_PRETIP_SPORTS plus any you asked about."""
    from .gamelogs import PATHS
    raw = os.environ.get("LINEMAKER_PRETIP_SPORTS", "").strip().lower()
    base = list(PATHS) if raw == "all" else list(configured)
    return sorted(set(base) | {s for s in load_state(f"pretip_armed_{date}.json", []) if s in PATHS})


def close_minutes() -> Optional[int]:
    """Closing read: every pre-tip game is re-read this close to the start (logged only), so
    tracking and closing-line value use near-final prices. Blank = off."""
    return _env_int("LINEMAKER_CLOSE_MINUTES", 12)


def news_minutes() -> Optional[int]:
    return _env_int("LINEMAKER_NEWS_MINUTES", 12)


def _state_path(name: str) -> str:
    return os.path.join(os.environ.get("LINEMAKER_CACHE_DIR", "cache"), name)


def load_state(name: str, default):
    try:
        with open(_state_path(name)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_state(name: str, data):
    path = _state_path(name)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path + ".tmp", "w") as f:
        json.dump(data, f)
    os.replace(path + ".tmp", path)


def _start(event: dict) -> dt.datetime:
    return dt.datetime.fromisoformat(event["commence_time"].replace("Z", "+00:00"))


def pretip_due(events: list, now: dt.datetime, minutes: int, done: set) -> list:
    """Groups of events (same tip time) entering the pre-tip window and not yet scanned."""
    due = {}
    for ev in events:
        left = (_start(ev) - now).total_seconds() / 60
        if 5 < left <= minutes and ev["id"] not in done:
            due.setdefault(ev["commence_time"], []).append(ev)
    return [due[k] for k in sorted(due)]


def newly_out(previous: dict, current: dict) -> list:
    """Players whose status is now Out and wasn't before (previous = last check's report)."""
    return sorted(p for p, s in current.items() if s == "out" and previous.get(p) != "out")


def event_for_team(events: list, team: str) -> Optional[dict]:
    t = norm(team)
    return next((e for e in events if t in (norm(e["home_team"]), norm(e["away_team"]))), None)


def rotation_player(sport: str, athlete_id: str, date: str) -> bool:
    from .adjustments import ROLE_MINUTES, _avg, _nfl_role, _recent
    from .gamelogs import game_log
    try:
        if sport == "NFL":   # a QB, or a receiver / back with real volume
            recent = _recent(game_log(sport, athlete_id, date), 5, "NFL")
            return _nfl_role(recent) or (len(recent) >= 2 and _avg(recent, "passingAttempts") >= 20)
        games = [g for g in game_log(sport, athlete_id, date) if g["stats"].get("minutes")][:10]
    except Exception:
        return False
    return len(games) >= 3 and sum(g["stats"]["minutes"] for g in games) / len(games) >= ROLE_MINUTES


def news_events(sport: str, date: str, events: list, now: dt.datetime, previous: Optional[dict],
                current: dict) -> list:
    """[(event, [player names])] to re-price because of new Outs."""
    if previous is None:
        return []                      # first look today: just remember the report
    from .gamelogs import roster_index
    index = roster_index(sport, date)
    hits = {}
    for player in newly_out(previous, current):
        info = index.get(player)
        if not info:
            continue
        aid, team = info[0], info[1]
        ev = event_for_team(events, team)
        if not ev or (_start(ev) - now).total_seconds() < 10 * 60:
            continue                   # not on tonight's slate, or too close to tip
        if not rotation_player(sport, aid, date):
            continue
        from .espn_projections import DISPLAY_NAMES
        hits.setdefault(ev["id"], (ev, []))[1].append(DISPLAY_NAMES.get(player, player.title()))
    return list(hits.values())


def goalie_changes(previous: Optional[dict], current: dict) -> list:
    """Teams whose starting goalie changed since the last check: [(team key, new goalie)].
    previous/current: norm(team) -> [goalie, confirmed]. The first look of the day only
    remembers (None -> [])."""
    if previous is None:
        return []
    out = []
    for team, (goalie, _confirmed) in current.items():
        old = previous.get(team)
        if old and old[0] != goalie:
            out.append((team, goalie))
    return out
