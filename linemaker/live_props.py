"""Live (in-game) player props: what a player has so far, plus what's left of his game.

For every game in progress (ESPN scoreboard) with player props up at FanDuel/DraftKings:

  so far     ESPN's live box score (the same reader that grades props)
  rest       his pre-game projection (ESPN game-log model, with tonight's adjustments)
             x the share of the game left (MLB hitters: their team's innings left)
  blowout    basketball, final third, lead over 15: the rest is cut up to 50% (starters sit)
  final      so far + rest; spread = pre-game spread x sqrt(share left)

Skipped: games in their last few minutes, MLB pitcher props (he can be pulled any inning),
a player missing from the box score once a third of the game is gone (likely out), and lines
already decided (he's past the line: the over has cashed, the under has lost). Books leave
those prices up for a while, but they can't be bet; graded, they looked like sure wins.
Live books move within seconds, so prices older than LINEMAKER_LIVE_MAX_ODDS_AGE seconds are
dropped, the market gets most of the weight (LINEMAKER_LIVE_MODEL_WEIGHT), and every read is
logged to <tracking>/live, apart from the pre-game calibration.
"""
import datetime as dt
import logging
import math
import re
from typing import Optional
from zoneinfo import ZoneInfo

from .oddsapi import norm

log = logging.getLogger("linemaker.live_props")
SKIP_MARKETS = {"strikeouts", "hits allowed", "walks allowed", "earned runs", "pitcher outs"}
BASKETBALL = {"NBA", "WNBA"}


def espn_games(sport: str, date: str) -> list:
    """[{id, away, home, state, detail, left, home_left, away_left, lead}] from the scoreboard."""
    from .boxscores import _get
    from .gamelogs import PATHS
    from .live_model import parse_scoreboard
    data = _get(f"{PATHS[sport]}/scoreboard", dates=date.replace("-", ""))
    states = parse_scoreboard(sport, data)
    out = []
    for ev in data.get("events", []):
        teams = {c["homeAway"]: c["team"]["displayName"] for c in ev["competitions"][0]["competitors"]}
        g = states.get((norm(teams.get("away", "")), norm(teams.get("home", ""))))
        if g:
            out.append({"id": ev["id"], "away": g.away, "home": g.home, "state": g.state, "detail": g.detail,
                        "left": g.left, "home_left": g.home_left, "away_left": g.away_left, "lead": g.lead})
    return out


def box(sport: str, event_id: str) -> tuple:
    """(norm player -> stats, norm player -> team) from ESPN's live box score."""
    from .boxscores import _get, parse_box
    from .gamelogs import PATHS
    data = _get(f"{PATHS[sport]}/summary", event=event_id)
    teams = {}
    for t in data.get("boxscore", {}).get("players", []):
        name = t.get("team", {}).get("displayName", "")
        for group in t.get("statistics", []):
            for a in group.get("athletes", []):
                teams[norm(a["athlete"]["displayName"])] = name
    return parse_box(data), teams


def _match(game: dict, events: list) -> Optional[dict]:
    h, a = norm(game["home"]), norm(game["away"])
    for ev in events:
        if norm(ev["home_team"]) == h and norm(ev["away_team"]) == a:
            return ev
    last = lambda x: x.split()[-1] if x else ""
    return next((ev for ev in events if last(norm(ev["home_team"])) == last(h)
                 and last(norm(ev["away_team"])) == last(a)), None)


SO_FAR = re.compile(r"LIVE · (-?\d+(?:\.\d+)?) so far")


def decided(so_far: float, line: float) -> bool:
    return so_far > line


def decided_record(rec: dict) -> bool:
    """A tracked live prop read taken after the line was already settled."""
    m = SO_FAR.search(rec.get("source") or "")
    return bool(m) and rec.get("inputs", {}).get("bet_type") == "prop" and decided(
        float(m.group(1)), float(rec.get("threshold", rec["inputs"].get("line", 0))))


def live_prop_rows(sport: str, date: str, game: dict, feed: list) -> list:
    """Projection rows (final value) for the props in `feed` from one game in progress."""
    from .boxscores import market_value
    from .espn_projections import espn_projections
    from .live_model import MIN_LEFT
    if game["state"] != "in" or game["left"] <= MIN_LEFT:
        return []
    wanted = {}
    for fm in feed:
        if fm.bet_type == "prop" and fm.market.lower() not in SKIP_MARKETS:
            wanted.setdefault(fm.subject, set()).add(fm.market)
    if not wanted:
        return []
    try:
        stats, teams = box(sport, game["id"])
    except Exception as e:
        log.warning("live box %s %s: %s", sport, game["id"], e)
        return []
    pre = {(norm(r["subject"]), r["market"].lower()): r for r in espn_projections(sport, date, wanted)}
    rows = []
    for player, markets in wanted.items():
        mine = stats.get(norm(player))
        left = game["left"]
        if sport == "MLB" and teams.get(norm(player)):
            left = game["home_left"] if norm(teams[norm(player)]) == norm(game["home"]) else game["away_left"]
        if mine is None and left < 0.67:
            continue          # a third of the game gone and he hasn't shown up: likely out
        if sport in BASKETBALL and mine is not None and left < 0.5:
            mins = next((v for k, v in mine.items() if k.endswith(".minutes")), None)
            if not mins:
                continue      # half over with no minutes: not playing
        for market in markets:
            p = pre.get((norm(player), market.lower()))
            if not p:
                continue
            so_far = market_value(sport, market, mine) if mine else 0.0
            if so_far is None:
                so_far = 0.0 if left >= 0.67 else None
            if so_far is None:
                continue
            rest = float(p["mean"]) * left
            note = ""
            if sport in BASKETBALL and left < 0.35 and abs(game["lead"]) > 15:
                cut = min(0.5, (abs(game["lead"]) - 15) / 30)
                rest *= 1 - cut
                note = f", rest cut {cut:.0%} for a {abs(game['lead'])}-point game"
            sd = max(float(p.get("stdev") or 1.0) * math.sqrt(left), 0.35)
            rows.append({"sport": sport, "subject": player, "market": market, "mean": f"{so_far + rest:.3f}",
                         "_so_far": so_far,
                         "stdev": f"{sd:.3f}", "dist": "normal", "lineup_confirmed": "true",
                         "notes": f"LIVE · {so_far:g} so far, {rest:.1f} more expected "
                                  f"({game['detail']}){note}"})
    return rows


def run_live_props(api, sports: list, date: str, tz: ZoneInfo):
    """Fetch live player props for every game in progress and project them. Fetch-only (the
    caller prices the rows, as with the other slates). About one prop call per live game."""
    from .live import LiveRun, live_dates
    from .live_model import drop_stale_books
    from .oddsapi import (EXTRA_MARKETS, PROP_MARKETS, credits_ok, extra_markets_on, fetch_event_props,
                          parse_event_props, sport_key)
    from .pro import ProResult
    feed, rows, slate = [], [], []
    now = dt.datetime.now(dt.timezone.utc)
    for sport in sports:
        s = sport.upper()
        family = sport_key(s).split("_")[0]
        keys = list(PROP_MARKETS.get(family, {}))
        if extra_markets_on() and credits_ok():
            keys += [k for k in EXTRA_MARKETS.get(family, {}) if not k.endswith("_alternate")]
        for d in live_dates(date, tz):
            try:
                games = [g for g in espn_games(s, d) if g["state"] == "in"]
            except Exception as e:
                log.warning("live scoreboard %s %s: %s", s, d, e)
                continue
            if not games:
                continue
            start, end = (dt.datetime.fromisoformat(d) - dt.timedelta(days=1)).isoformat() + "Z", \
                (dt.datetime.fromisoformat(d) + dt.timedelta(days=2)).isoformat() + "Z"
            events = [e for e in api.events(s, start, end)
                      if dt.datetime.fromisoformat(e["commence_time"].replace("Z", "+00:00")) < now]
            for g in games:
                ev = _match(g, events)
                if not ev:
                    continue
                try:
                    payload = fetch_event_props(api, s, ev["id"], ",".join(keys))
                except Exception as e:
                    log.warning("live props %s %s: %s", s, ev["id"], e)
                    continue
                game_feed = drop_stale_books(parse_event_props(payload, s, tz))
                game_rows = live_prop_rows(s, d, g, game_feed)
                done = {(norm(r["subject"]), r["market"].lower()): r.pop("_so_far") for r in game_rows}
                game_feed = [fm for fm in game_feed if not decided(
                    done.get((norm(fm.subject), fm.market.lower()), -1.0), fm.line)]
                for fm in game_feed:
                    fm.date = date      # in progress now: this live scan's slate
                feed += game_feed
                rows += game_rows
                slate.append((f"{g['away']} @ {g['home']}", ev["commence_time"]))
    from .oddsapi import merge
    merged, missing = merge(feed, rows)
    result = ProResult(slate_date=date)
    result.slate = slate
    result.projection_source = "live props model (box score so far + pre-game projection for the rest)"
    result.kind = "live"
    lr = LiveRun(result=result, rows=merged, feed_count=len(feed), missing=missing,
                 remaining=api.quota.remaining)
    lr.feed = feed
    return lr
