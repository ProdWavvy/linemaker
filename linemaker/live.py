"""Fetch live odds, join projections, run the pro engine."""
import csv
import datetime as dt
import io
import os
from dataclasses import dataclass, field
from typing import Optional
from zoneinfo import ZoneInfo

from .devig import no_vig
from .oddsapi import SLATE_COLUMNS, OddsAPI, feed_row, fetch_slate, merge, results_from_scores
from .pro import ProConfig, ProResult, rows_to_markets, run_pro


@dataclass
class LiveRun:
    result: ProResult
    rows: list                      # merged slate rows (what the engine priced)
    feed_count: int                 # markets the feed returned
    missing: list = field(default_factory=list)   # projections with no odds
    remaining: Optional[int] = None


def has_projection(row: dict) -> bool:
    if (row.get("mean") or "").strip():
        return True
    has = lambda *cols: any((row.get(c) or "").strip() for c in cols)
    return (has("opp_season", "opp_recent", "opp_override")
            and has("rate_season", "rate_recent", "rate_expected"))


def read_projections(source) -> list:
    """Path or file-like object -> rows that carry a projection (blank template rows dropped)."""
    if isinstance(source, str):
        with open(source, newline="", encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
    else:
        rows = list(csv.DictReader(source))
    return [r for r in rows if has_projection(r)]


def write_rows(rows: list, path: str):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=SLATE_COLUMNS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def rows_csv(rows: list) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=SLATE_COLUMNS, extrasaction="ignore")
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue()


def lock_near_start(rows: list, now: Optional[dt.datetime] = None):
    """Rows with no lineup_confirmed value are treated as final (and so frozen for grading)
    once their game is within LINEMAKER_FREEZE_MINUTES (default 30) of starting."""
    minutes = float(os.environ.get("LINEMAKER_FREEZE_MINUTES", "30"))
    now = now or dt.datetime.now(dt.timezone.utc)
    for r in rows:
        if (r.get("lineup_confirmed") or "").strip() or not r.get("commence"):
            continue
        start = dt.datetime.fromisoformat(r["commence"].replace("Z", "+00:00"))
        if start - now <= dt.timedelta(minutes=minutes):
            r["lineup_confirmed"] = "true"


def run_auto(odds: OddsAPI, fp, sports: list, date: str, tz: ZoneInfo, cfg: ProConfig,
             freezer=None, max_events: Optional[int] = None, only: Optional[set] = None) -> LiveRun:
    """FantasyPros consensus projections -> live FanDuel/DraftKings odds -> pro card."""
    from .fantasypros import fetch_projections
    projections = []
    for sport in sports:
        projections += fetch_projections(fp, sport, date)
    lr = run_live(odds, sports, projections, date, tz, cfg, freezer, props=True,
                  max_events=max_events, only=only)
    # Thousands of projected players have no posted line; only report misses with a line.
    lr.result.passes = [(l, r) for l, r in lr.result.passes if r != "no odds found in the feed"]
    lr.result.source_warning = getattr(fp, "warning", None)
    return lr


def run_auto_espn(odds: OddsAPI, sports: list, date: str, tz: ZoneInfo, cfg: ProConfig,
                  freezer=None, max_events: Optional[int] = None,
                  only: Optional[set] = None) -> LiveRun:
    """Free projections from ESPN game logs, built only for players with a posted prop."""
    from .espn_projections import espn_projections
    feed = fetch_feed(odds, sports, date, tz, True, max_events, game_lines=False, only=only)
    projections = []
    for sport in sports:
        wanted = {}
        for fm in feed:
            if fm.sport.upper() == sport.upper() and fm.bet_type == "prop":
                wanted.setdefault(fm.subject, set()).add(fm.market)
        projections += espn_projections(sport.upper(), date, wanted)
    lr = price_feed(odds, feed, sports, projections, date, cfg, freezer)
    lr.result.passes = [(l, r) for l, r in lr.result.passes if r != "no odds found in the feed"]
    lr.result.projection_source = "ESPN game-log model"
    return lr


def projection_source() -> str:
    """LINEMAKER_PROJECTIONS: auto (FantasyPros, falling back to ESPN when it looks like
    sample data or the key is missing), fantasypros, or espn."""
    choice = os.environ.get("LINEMAKER_PROJECTIONS", "auto").strip().lower()
    if choice in ("espn", "fantasypros"):
        return choice
    return "auto" if os.environ.get("FANTASYPROS_API_KEY") else "espn"


def run_best(odds: OddsAPI, sports: list, date: str, tz: ZoneInfo, cfg: ProConfig,
             freezer=None, max_events: Optional[int] = None, only: Optional[set] = None) -> LiveRun:
    """Projections from the configured source; in auto mode a FantasyPros sample-data warning
    switches to the ESPN model (the odds feed is reused, so no credits are spent twice)."""
    source = projection_source()
    from .fantasypros import SUPPORTED
    if source == "espn" or any(s.upper() not in SUPPORTED for s in sports):
        # FantasyPros has no WNBA, NHL or college projections.
        return run_auto_espn(odds, sports, date, tz, cfg, freezer, max_events, only)
    from .fantasypros import FantasyPros
    lr = run_auto(odds, FantasyPros(), sports, date, tz, cfg, freezer, max_events, only)
    lr.result.projection_source = "FantasyPros consensus"
    if source == "auto" and lr.result.source_warning:
        import logging
        logging.getLogger("linemaker.live").warning("FantasyPros looks like sample data; "
                                                    "using the ESPN game-log model")
        from .espn_projections import espn_projections
        feed = getattr(lr, "feed", None) or fetch_feed(odds, sports, date, tz, True, max_events,
                                                       game_lines=False, only=only)
        projections = []
        for sport in sports:
            wanted = {}
            for fm in feed:
                if fm.sport.upper() == sport.upper() and fm.bet_type == "prop":
                    wanted.setdefault(fm.subject, set()).add(fm.market)
            projections += espn_projections(sport.upper(), date, wanted)
        lr = price_feed(odds, feed, sports, projections, date, cfg, freezer)
        lr.result.passes = [(l, r) for l, r in lr.result.passes if r != "no odds found in the feed"]
        lr.result.projection_source = "ESPN game-log model (FantasyPros returned sample data)"
    return lr


def run_games(odds: OddsAPI, sports: list, date: str, tz: ZoneInfo, cfg: ProConfig,
              freezer=None) -> LiveRun:
    """Moneylines, spreads and totals priced off the free game model (ESPN ratings + Kalshi).
    One /odds call per sport: about 3 credits."""
    from .game_model import game_projections
    from .oddsapi import skip_started
    feed = fetch_feed(odds, sports, date, tz, props=False, game_lines=True)
    if skip_started():   # in-progress games carry live odds; the pre-game model can't price them
        now = dt.datetime.now(dt.timezone.utc)
        feed = [fm for fm in feed if not fm.commence or
                dt.datetime.fromisoformat(fm.commence.replace("Z", "+00:00")) > now]
    projections = []
    for sport in sports:
        games = sorted({(fm.opponent, fm.subject) for fm in feed
                        if fm.sport.upper() == sport.upper() and fm.bet_type != "prop"})
        projections += game_projections(sport.upper(), date, games)
    lr = price_feed(odds, feed, sports, projections, date, cfg, freezer)
    lr.result.passes = [(l, r) for l, r in lr.result.passes if r != "no odds found in the feed"]
    lr.result.projection_source = "game model (ESPN team ratings + Kalshi)"
    lr.result.kind = "games"
    return lr


def live_settings(cfg: ProConfig, date: str) -> tuple:
    """(cfg, tracker) for in-game pricing: the market counts for more, and every live read is
    logged to <tracking>/live, apart from the pre-game calibration."""
    from .live_model import live_weight
    from .tracking import Tracker
    cfg.model_weight = cfg.model_weight_fresh = live_weight()
    cfg.market_weights = {}   # pre-game tuning doesn't carry over to in-game prices
    tracker = None
    if cfg.track:
        tracker = Tracker(os.path.join(os.environ.get("LINEMAKER_TRACKING_DIR", "tracking"), "live"), date)
    cfg.track = False
    return cfg, tracker


def run_live_games(odds: OddsAPI, sports: list, date: str, tz: ZoneInfo, cfg: ProConfig,
                   prior_rows: Optional[list] = None, prior_saved_at: Optional[float] = None) -> LiveRun:
    """In-game moneylines, spreads and totals: live FanDuel/DraftKings odds vs the live model
    (score + clock + the pre-game line). One /odds call per league, ~3 credits. Tracked in
    <tracking>/live so in-game bets never mix with the pre-game calibration."""
    from .live_model import drop_stale_books, live_projections, live_states
    feed = drop_stale_books(fetch_feed(odds, sports, date, tz, props=False, game_lines=True))
    projections = []
    for sport in sports:
        s = sport.upper()
        try:
            states = live_states(s, date)
        except Exception as e:
            import logging
            logging.getLogger("linemaker.live").warning("espn live scores %s: %s", s, e)
            states = {}
        games = sorted({(fm.opponent, fm.subject, fm.commence) for fm in feed
                        if fm.sport.upper() == s and fm.bet_type != "prop"})
        projections += live_projections(s, date, states, games, prior_rows or [], prior_saved_at)
    live_now = {(p["sport"], p["subject"]) for p in projections}
    feed = [fm for fm in feed if (fm.sport.upper(), fm.subject) in live_now]
    cfg, tracker = live_settings(cfg, date)
    lr = price_feed(odds, feed, sports, projections, date, cfg, None, tracker)
    lr.result.passes = [(l, r) for l, r in lr.result.passes if r != "no odds found in the feed"]
    lr.result.projection_source = "live model (score + clock + pre-game line)"
    lr.result.kind = "live"
    return lr


def fetch_feed(api: OddsAPI, sports: list, date: str, tz: ZoneInfo, props: bool = False,
               max_events: Optional[int] = None, game_lines: bool = True,
               only: Optional[set] = None) -> list:
    feed = []
    for sport in sports:
        feed += fetch_slate(api, sport, date, tz, props=props, max_events=max_events,
                            game_lines=game_lines, only=only)
    return feed


def odds_template(api: OddsAPI, sports: list, date: str, tz: ZoneInfo, props: bool = False,
                  max_events: Optional[int] = None) -> list:
    """Odds-only slate rows with blank projection columns, ready to fill in."""
    return [feed_row(fm) for fm in fetch_feed(api, sports, date, tz, props, max_events)]


def run_live(api: OddsAPI, sports: list, projections: list, date: str, tz: ZoneInfo,
             cfg: ProConfig, freezer=None, props: Optional[bool] = None,
             max_events: Optional[int] = None, only: Optional[set] = None) -> LiveRun:
    # Only spend credits on what the projections need.
    game_types = {"moneyline", "spread", "game total", "total goals", "match result"}
    markets = {p.get("market", "").strip().lower() for p in projections}
    if props is None:
        props = bool(markets - game_types)
    game_lines = bool(markets & game_types)
    feed = fetch_feed(api, sports, date, tz, props, max_events, game_lines, only)
    return price_feed(api, feed, sports, projections, date, cfg, freezer)


def price_feed(api: OddsAPI, feed: list, sports: list, projections: list, date: str,
               cfg: ProConfig, freezer=None, tracker=None) -> LiveRun:
    """Join projections to an already-fetched feed and run the engine."""
    rows, missing = merge(feed, [p for p in projections
                                 if p.get("sport", "").upper() in {s.upper() for s in sports}])
    lock_near_start(rows)
    if cfg is None:   # fetch-only (the caller prices the saved rows itself): skip the engine
        result = ProResult(slate_date=date)
    else:
        result = run_pro(rows_to_markets(rows), date, cfg, freezer, tracker)
    result.slate = [g for s in sports for g in getattr(api, "slate", {}).get(s.upper(), [])]
    for p in missing:
        result.passes.append((f"{p['subject']} {p['market']}", "no odds found in the feed"))
    _log_link_sample(feed)
    lr = LiveRun(result=result, rows=rows, feed_count=len(feed), missing=missing,
                 remaining=api.quota.remaining)
    lr.feed = feed
    return lr


def _log_link_sample(feed: list):
    """One raw betslip link per book in the console, to check the books' link formats."""
    import logging
    log = logging.getLogger("linemaker.live")
    seen = {}
    for fm in feed:
        for book, sides in (fm.links or {}).items():
            if book not in seen and any(sides):
                seen[book] = next(x for x in sides if x)
    with_links = sum(1 for fm in feed if fm.links)
    log.info("bet links: %d of %d markets have them; samples: %s", with_links, len(feed),
             "; ".join(f"{b} {e.get('link') or 'sid=' + str(e.get('sid'))}"
                       for b, e in seen.items() if b in ("FanDuel", "DraftKings")) or "none")


def board_lines(feed: list) -> list:
    """Quick market read with no projections: consensus no-vig per game line."""
    out = []
    for fm in feed:
        if fm.bet_type == "prop":
            continue
        n = 3 if fm.bet_type == "1x2" else 2
        complete = [p[:n] for _, p in fm.books if len(p) >= n and None not in p[:n]]
        if not complete:
            continue
        fair = [no_vig(*p) for p in complete]
        avg = [sum(f[i] for f in fair) / len(fair) for i in range(n)]
        if fm.bet_type == "1x2":
            desc = f"{fm.subject} {avg[0]:.1%} · draw {avg[1]:.1%} · {fm.opponent} {avg[2]:.1%}"
        elif fm.bet_type == "moneyline":
            desc = f"{fm.subject} {avg[0]:.1%} · {fm.opponent} {avg[1]:.1%}"
        elif fm.bet_type == "spread":
            desc = f"{fm.subject} {fm.line:+g} {avg[0]:.1%} · {fm.opponent} {-fm.line:+g} {avg[1]:.1%}"
        else:
            desc = f"o{fm.line:g} {avg[0]:.1%} · u{fm.line:g} {avg[1]:.1%}"
        out.append((fm.game, fm.bet_type, desc, len(complete)))
    return out


def auto_results(api: OddsAPI, snapshot_path: str) -> list:
    """Final scores from /scores (2 credits per sport) -> results rows for frozen game lines.
    Scores cover games from the last 3 days only; props still need a results CSV."""
    import json
    with open(snapshot_path) as f:
        records = json.load(f)
    sports = sorted({r["sport"] for r in records.values()
                     if r.get("sport") and r["inputs"].get("bet_type") != "prop"})
    rows = []
    cache = getattr(api, "_scores_cache", None)
    if cache is None:
        cache = api._scores_cache = {}   # one /scores call per league per grading run
    for sport in sports:
        if sport not in cache:
            cache[sport] = api.scores(sport, 3)
        rows += results_from_scores(cache[sport], records)
    return rows
