"""Grading: final scores (Odds API, game lines) and box scores (ESPN, props) -> results on
the frozen picks and every tracked market, then an automatic weight re-fit."""
import os

from .freeze import grade
from .live import auto_results
from .oddsapi import OddsAPI

SNAPSHOTS = os.environ.get("LINEMAKER_SNAPSHOTS", "snapshots")
TRACKING = os.environ.get("LINEMAKER_TRACKING_DIR", "tracking")


def _by_game_date(records: dict, date: str) -> dict:
    """{local game date: records}. A live read taken after midnight sits in the next day's
    file, but its box score is under the day the game started."""
    from zoneinfo import ZoneInfo
    from .oddsapi import _local_date
    tz = ZoneInfo(os.environ.get("LINEMAKER_TZ", "America/New_York"))
    out = {}
    for k, r in records.items():
        try:
            d = _local_date(r["commence"], tz) if r.get("commence") else date
        except (TypeError, ValueError):
            d = date
        out.setdefault(d, {})[k] = r
    return out


def live_prop_results(records: dict, date: str) -> list:
    from .boxscores import prop_results
    return [row for d, recs in _by_game_date(records, date).items() for row in prop_results(recs, d)]


def espn_game_results(records: dict, date: str, states=None) -> list:
    """Final scores from ESPN's scoreboard (free, any date) -> results rows for game-line
    records, for games older than The Odds API's 3-day /scores window."""
    from .live_model import live_states
    from .oddsapi import GAME_LINE_TYPES, norm, results_from_scores
    states = states or live_states
    rows = []
    for d, recs in _by_game_date(records, date).items():
        lines = {k: r for k, r in recs.items() if r.get("inputs", {}).get("bet_type") in GAME_LINE_TYPES}
        finals = []
        for sport in sorted({r.get("sport", "").upper() for r in lines.values()}):
            try:
                board = states(sport, d)
            except Exception:
                continue   # no ESPN scoreboard for this league
            for r in lines.values():
                if r.get("sport", "").upper() != sport or " @ " not in r.get("game", ""):
                    continue
                away, home = r["game"].split(" @ ", 1)
                st = board.get((norm(away), norm(home)))
                if st is not None and st.state == "post":
                    finals.append({"id": r["game_id"], "completed": True,
                                   "scores": [{"name": away, "score": st.away_score},
                                              {"name": home, "score": st.home_score}]})
        rows += results_from_scores(finals, lines)
    return rows


def regrade_live(date: str, api=None) -> int:
    """Grade one day's live reads again (catching up on bets that are still open)."""
    from .tracking import apply_results_day, load_day
    live_base = os.path.join(TRACKING, "live")
    live = load_day(live_base, date)
    if not live:
        return 0
    rows = live_prop_results(live, date) + espn_game_results(live, date)
    if api is not None:
        try:
            rows = auto_results(api, live) + rows
        except Exception:
            pass   # final scores older than 3 days aren't available; props still grade
    return apply_results_day(live_base, date, rows)


def grade_day(date: str, rows: list, auto: bool):
    """Score the day's picks and attach results to every tracked market, phase by phase
    (pre-game, live, PrizePicks), each league and kind in its own file.
    Returns (summary lines, remaining credits, number of tracked markets graded)."""
    from .boxscores import prop_results
    from .jsonio import load_json
    from .tracking import apply_results_day, load_day
    snap = os.path.join(SNAPSHOTS, f"{date}.json")
    pre = load_day(TRACKING, date)
    live_base, pp_base = os.path.join(TRACKING, "live"), os.path.join(TRACKING, "pp")
    remaining = None
    summary_live = []

    def keys(rs):
        return {(r["game_id"], r["subject"], r["market"]) for r in rs}
    if auto and os.environ.get("ODDS_API_KEY"):
        api = OddsAPI()
        have = keys(rows)
        source = pre or (load_json(snap) if os.path.exists(snap) else {})
        if source:
            rows += [r for r in auto_results(api, source) if (r["game_id"], r["subject"], r["market"]) not in have]
        live = load_day(live_base, date)
        if live:   # in-game reads: graded from the same final scores and box scores
            live_rows = auto_results(api, live) + live_prop_results(live, date)
            n_live = apply_results_day(live_base, date, live_rows)
            if n_live:
                summary_live.append(f"- 🔴 Live reads graded: **{n_live}**")
        remaining = api.quota.remaining
    # Player props: final box scores from ESPN (free), for tracked and frozen props alike.
    if auto:
        have = keys(rows)
        for recs in (pre, load_json(snap) if os.path.exists(snap) else {}):
            if recs:
                rows += [r for r in prop_results(recs, date)
                         if (r["game_id"], r["subject"], r["market"]) not in have]
                have = keys(rows)
    pp = load_day(pp_base, date) if auto else {}
    if pp:   # PrizePicks legs: same ESPN box scores (free)
        n_pp = apply_results_day(pp_base, date, prop_results(pp, date))
        if n_pp:
            summary_live.append(f"- 🟣 PrizePicks lines graded: **{n_pp}**")
    if auto and pre:   # UFC fights: winners from ESPN (free)
        from .ufc_model import ufc_results
        have = keys(rows)
        rows += [r for r in ufc_results(pre, date) if (r["game_id"], r["subject"], r["market"]) not in have]
    summary = []
    if rows and os.path.exists(snap):
        md = grade(snap, rows)
        summary = [l for l in md.splitlines() if l.startswith("- ")]
    summary += summary_live
    tracked = apply_results_day(TRACKING, date, rows) if rows and pre else 0
    from .bets import settle
    settled = settle()
    from .parlay_track import grade_parlays
    n_parlays = grade_parlays(date)
    if n_parlays:
        summary.append(f"- 🎰 Auto-parlays graded: **{n_parlays}**")
    if settled:
        summary.append(f"- 🧾 Settled **{settled}** of your logged bets (`/record` for your P/L)")
    if tracked:
        from .autotune import run_autotune
        try:
            summary += ["- ⚖️ " + line for line in run_autotune(TRACKING, date)]
        except Exception as e:   # tuning must never block a grade
            summary.append(f"- ⚖️ Weight tuning skipped ({type(e).__name__}: {e})")
    return summary, remaining, tracked
