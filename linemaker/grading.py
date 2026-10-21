"""Grading: final scores (Odds API, game lines) and box scores (ESPN, props) -> results on
the frozen picks and every tracked market, then an automatic weight re-fit."""
import os

from .freeze import grade
from .live import auto_results
from .oddsapi import OddsAPI

SNAPSHOTS = os.environ.get("LINEMAKER_SNAPSHOTS", "snapshots")
TRACKING = os.environ.get("LINEMAKER_TRACKING_DIR", "tracking")


def grade_day(date: str, rows: list, auto: bool):
    """Score the day's picks and attach results to every tracked market.
    Returns (summary lines, remaining credits, number of tracked markets graded)."""
    from .tracking import apply_results
    snap = os.path.join(SNAPSHOTS, f"{date}.json")
    track = os.path.join(TRACKING, f"{date}.json")
    remaining = None
    summary_live = []
    if auto and os.environ.get("ODDS_API_KEY"):
        api = OddsAPI()
        have = {(r["game_id"], r["subject"], r["market"]) for r in rows}
        source = track if os.path.exists(track) else snap
        if os.path.exists(source):
            rows += [r for r in auto_results(api, source)
                     if (r["game_id"], r["subject"], r["market"]) not in have]
        live_track = os.path.join(TRACKING, "live", f"{date}.json")
        if os.path.exists(live_track):   # in-game reads: graded from the same final scores
            from .tracking import apply_results
            live_rows = auto_results(api, live_track)
            if live_rows:
                n_live = apply_results(live_track, live_rows)
                if n_live:
                    summary_live.append(f"- 🔴 Live reads graded: **{n_live}**")
        remaining = api.quota.remaining
    # Player props: final box scores from ESPN (free), for tracked and frozen props alike.
    if auto:
        import json as _json
        from .boxscores import prop_results
        have = {(r["game_id"], r["subject"], r["market"]) for r in rows}
        for path in (track, snap):
            if os.path.exists(path):
                with open(path) as f:
                    recs = _json.load(f)
                rows += [r for r in prop_results(recs, date)
                         if (r["game_id"], r["subject"], r["market"]) not in have]
                have = {(r["game_id"], r["subject"], r["market"]) for r in rows}
    summary = []
    if rows and os.path.exists(snap):
        md = grade(snap, rows)
        summary = [l for l in md.splitlines() if l.startswith("- ")]
    summary += summary_live
    tracked = apply_results(track, rows) if rows and os.path.exists(track) else 0
    from .bets import settle
    settled = settle()
    if settled:
        summary.append(f"- 🧾 Settled **{settled}** of your logged bets (`/record` for your P/L)")
    if tracked:
        from .autotune import run_autotune
        try:
            summary += ["- ⚖️ " + line for line in run_autotune(TRACKING, date)]
        except Exception as e:   # tuning must never block a grade
            summary.append(f"- ⚖️ Weight tuning skipped ({type(e).__name__}: {e})")
    return summary, remaining, tracked
