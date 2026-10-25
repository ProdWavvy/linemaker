"""Grading: final scores (Odds API, game lines) and box scores (ESPN, props) -> results on
the frozen picks and every tracked market, then an automatic weight re-fit."""
import os

from .freeze import grade
from .live import auto_results
from .oddsapi import OddsAPI

SNAPSHOTS = os.environ.get("LINEMAKER_SNAPSHOTS", "snapshots")
TRACKING = os.environ.get("LINEMAKER_TRACKING_DIR", "tracking")


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
            live_rows = auto_results(api, live) + prop_results(live, date)
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
