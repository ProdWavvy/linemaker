"""Linemaker CLI.

  python -m linemaker lite  data/sample_slate.csv --date 2026-10-21
  python -m linemaker pro   data/sample_pro_slate.csv --date 2026-10-21
  python -m linemaker grade snapshots/2026-10-21.json data/sample_results.csv
  python -m linemaker fetch --sport NBA --props -o slates/nba_odds.csv      (needs ODDS_API_KEY)
  python -m linemaker live  --sport NBA --projections projections.csv     (needs ODDS_API_KEY)
  python -m linemaker auto  --sport NBA          (FantasyPros projections; needs both keys)
"""
import argparse
import datetime as dt
import os
import sys
from zoneinfo import ZoneInfo

from .config import Config
from .freeze import Freezer, grade
from .market import load_markets
from .pipeline import run
from .pro import ProConfig, load_pro_markets, run_pro
from .pro_report import render_pro
from .report import render


def _emit(md: str, path: str | None):
    if path:
        with open(path, "w") as f:
            f.write(md)
    else:
        sys.stdout.write(md + "\n")


def main(argv=None):
    p = argparse.ArgumentParser(prog="linemaker", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    today = dt.date.today().isoformat()

    lite = sub.add_parser("lite", help="hit-rate probability-gap card")
    lite.add_argument("csv")
    lite.add_argument("--date", default=today)
    lite.add_argument("--odds-min", type=int, help="shortest odds allowed, e.g. -200")
    lite.add_argument("--odds-max", type=int, help="longest odds allowed, e.g. -1000")
    lite.add_argument("--min-prob", type=float)
    lite.add_argument("--max-picks", type=int)
    lite.add_argument("-o", "--out")

    pro = sub.add_parser("pro", help="simulation + de-vig + market-blend card")
    pro.add_argument("csv")
    pro.add_argument("--date", default=today)
    pro.add_argument("--sims", type=int)
    pro.add_argument("--model-weight", type=float, help="model share of the blend (default 0.5)")
    pro.add_argument("--fresh-weight", type=float, help="model share on fresh news (default 0.8)")
    pro.add_argument("--devig", choices=("proportional", "shin"), help="de-vig method")
    pro.add_argument("--min-gap", type=float)
    pro.add_argument("--min-prob", type=float, help="optional blended-probability floor")
    pro.add_argument("--odds-min", type=int, help="optional window, e.g. -200")
    pro.add_argument("--odds-max", type=int, help="optional window, e.g. -1000")
    pro.add_argument("--confirmed-only", action="store_true")
    pro.add_argument("--max-picks", type=int)
    pro.add_argument("--snapshots", default="snapshots", help="freeze directory ('' to disable)")
    pro.add_argument("-o", "--out")

    for name, help_ in (("fetch", "write live odds to a slate CSV with blank projection columns"),
                        ("live", "fetch live odds, join projections, print the pro card"),
                        ("auto", "FantasyPros projections + live odds -> pro card")):
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("--sport", action="append", required=True,
                        help="NBA, NFL, NHL, MLB, NCAAF, WNBA, UCL, LALIGA (repeatable)")
        sp.add_argument("--date", default=None, help="local slate date (default today)")
        sp.add_argument("--tz", default=os.environ.get("LINEMAKER_TZ", "America/New_York"))
        sp.add_argument("--props", action="store_true", default=None,
                        help="also fetch player props (costs credits per event)")
        sp.add_argument("--max-events", type=int, help="cap events fetched for props")
        sp.add_argument("--bookmakers", help="comma list, e.g. draftkings,fanduel (overrides region)")
        sp.add_argument("--regions", default="us")
        sp.add_argument("-o", "--out")
        if name in ("live", "auto"):
            if name == "live":
                sp.add_argument("--projections", required=True)
            sp.add_argument("--devig", choices=("proportional", "shin"))
            sp.add_argument("--snapshots", default="snapshots")
            sp.add_argument("--save-slate", help="also write the merged slate CSV here")

    cal = sub.add_parser("calibrate", help="test blend weights against graded tracked markets")
    cal.add_argument("--dir", default=os.environ.get("LINEMAKER_TRACKING_DIR", "tracking"))
    cal.add_argument("--days", type=int)
    cal.add_argument("-o", "--out")

    gr = sub.add_parser("grade", help="grade a frozen snapshot against results")
    gr.add_argument("snapshot")
    gr.add_argument("results", nargs="?", help="results CSV (props; optional with --auto)")
    gr.add_argument("--auto", action="store_true",
                    help="fetch final scores for game lines from The Odds API (2 credits/sport)")
    gr.add_argument("-o", "--out")

    a = p.parse_args(argv)

    if a.cmd == "lite":
        cfg = Config()
        for attr, val in (("min_odds", a.odds_min), ("max_odds", a.odds_max),
                          ("min_true_prob", a.min_prob), ("max_picks", a.max_picks)):
            if val is not None:
                setattr(cfg, attr, val)
        _emit(render(run(load_markets(a.csv), a.date, cfg), cfg), a.out)
    elif a.cmd == "pro":
        cfg = ProConfig(confirmed_only=a.confirmed_only)
        for attr, val in (("n_sims", a.sims), ("model_weight", a.model_weight),
                          ("model_weight_fresh", a.fresh_weight), ("min_gap", a.min_gap),
                          ("min_prob", a.min_prob), ("min_odds", a.odds_min),
                          ("max_odds", a.odds_max), ("max_picks", a.max_picks),
                          ("devig_method", a.devig)):
            if val is not None:
                setattr(cfg, attr, val)
        freezer = Freezer(a.snapshots, a.date) if a.snapshots else None
        _emit(render_pro(run_pro(load_pro_markets(a.csv), a.date, cfg, freezer), cfg), a.out)
    elif a.cmd in ("fetch", "live", "auto"):
        from .live import odds_template, read_projections, run_live, write_rows, rows_csv
        from .oddsapi import OddsAPI
        tz = ZoneInfo(a.tz)
        date = a.date or dt.datetime.now(tz).date().isoformat()
        api = OddsAPI(regions=a.regions, bookmakers=a.bookmakers)
        if a.cmd == "fetch":
            rows = odds_template(api, a.sport, date, tz, bool(a.props), a.max_events)
            if a.out:
                write_rows(rows, a.out)
            else:
                sys.stdout.write(rows_csv(rows))
        else:
            cfg = ProConfig(devig_method=a.devig or "proportional")
            freezer = Freezer(a.snapshots, date) if a.snapshots else None
            if a.cmd == "auto":
                from .fantasypros import FantasyPros
                from .live import run_auto
                lr = run_auto(api, FantasyPros(), a.sport, date, tz, cfg, freezer, a.max_events)
            else:
                lr = run_live(api, a.sport, read_projections(a.projections), date, tz, cfg,
                              freezer, a.props, a.max_events)
            if a.save_slate:
                write_rows(lr.rows, a.save_slate)
            _emit(render_pro(lr.result, cfg), a.out)
        sys.stderr.write(f"Odds API credits remaining: {api.quota.remaining} "
                         f"(used {api.quota.used})\n")
    elif a.cmd == "calibrate":
        from .tracking import calibrate, load_records
        _emit(calibrate(load_records(a.dir, a.days)), a.out)
    else:
        rows = []
        if a.results:
            import csv
            with open(a.results, newline="") as f:
                rows = list(csv.DictReader(f))
        if a.auto:
            from .live import auto_results
            from .oddsapi import OddsAPI
            api = OddsAPI()
            have = {(r["game_id"], r["subject"], r["market"]) for r in rows}
            rows += [r for r in auto_results(api, a.snapshot)
                     if (r["game_id"], r["subject"], r["market"]) not in have]
            import json as _json
            from .boxscores import prop_results
            with open(a.snapshot) as f:
                recs = _json.load(f)
            have = {(r["game_id"], r["subject"], r["market"]) for r in rows}
            rows += [r for r in prop_results(recs, os.path.basename(a.snapshot)[:10])
                     if (r["game_id"], r["subject"], r["market"]) not in have]
            sys.stderr.write(f"Odds API credits remaining: {api.quota.remaining}\n")
        if not rows:
            p.error("give a results CSV, --auto, or both")
        _emit(grade(a.snapshot, rows), a.out)


if __name__ == "__main__":
    main()
