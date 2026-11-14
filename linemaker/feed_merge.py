"""The Odds API and PropLine, hand in hand.

The Odds API stays the main feed (its event ids, its add-to-betslip links, its prices). When a
PropLine key is set, every slate it pulls is also read from PropLine (one request per game for
props, one per league for game lines; cached 5 minutes) and merged in:

  1. Books The Odds API didn't return are added to each market (LINEMAKER_PROPLINE_BOOKS, a
     short list so tracking files don't swell). They only count toward the fair price if
     LINEMAKER_SHARP_BOOKS(_PROPS) gives them a weight; /books measures how accurate each is.
  2. Fresher prices: PropLine streams DraftKings, FanDuel, Fanatics, Kalshi and Polymarket within
     about a second; The Odds API polls. Where both carry the same book on the same line, the
     PropLine price is used, but only for a book whose prices this scan shows both feeds agree
     on (20+ shared prices, half or more identical, median gap 2 points or less); any other
     book keeps The Odds API's prices and the reason is logged (and shown in /status).
  3. A game whose props The Odds API refused (out of credits, an error) is read from PropLine
     alone.

LINEMAKER_PROPLINE_MERGE: "fresh" (default: all three), "add" (1 and 3 only), "off".
"""
import datetime as dt
import logging
import os
import statistics
from typing import Optional

log = logging.getLogger("linemaker.feed_merge")
PUSH_BOOKS = {"draftkings", "fanduel", "fanatics", "kalshi", "polymarket"}
DEFAULT_BOOKS = ("draftkings,fanduel,fanatics,betmgm,betrivers,williamhill_us,hardrockbet,pinnacle,"
                 "betonlineag,lowvig,bovada,kalshi,polymarket,novig,prophetx")
MIN_OVERLAP, MIN_SAME, MAX_GAP = 20, 0.5, 0.02
LAST = {}   # sport -> stats of the latest merge (for /propline and /status)


def mode() -> str:
    from . import propline
    if not propline.on():
        return "off"
    m = os.environ.get("LINEMAKER_PROPLINE_MERGE", "fresh").strip().lower()
    return m if m in ("fresh", "add", "off") else "fresh"


def books_param() -> str:
    return os.environ.get("LINEMAKER_PROPLINE_BOOKS", "").strip() or DEFAULT_BOOKS


def _bk(name: str) -> str:
    return "".join(ch for ch in (name or "").lower() if ch.isalnum())


ALIASES = {"williamhillus": "caesars", "caesarssportsbook": "caesars", "hardrockbet": "hardrock",
           "betonlineag": "betonline", "underdogfantasy": "underdog"}


def same_book(name: str) -> str:
    k = _bk(name)
    return ALIASES.get(k, k)


def _key(fm) -> tuple:
    from .oddsapi import norm
    return (fm.game_id, norm(fm.subject), fm.market, fm.bet_type, round(float(fm.line), 2))


def _imp(odds: int) -> float:
    return 100 / (odds + 100) if odds > 0 else -odds / (-odds + 100)


def merge(primary: list, extra: list, how: str = "fresh", sport: str = "") -> dict:
    """Merge PropLine FeedMarkets into The Odds API's (in place). Returns the merge stats."""
    idx = {_key(fm): fm for fm in primary}
    stats = {"overlap": 0, "same": 0, "books_added": 0, "refreshed": 0, "applied_fresh": False,
             "markets_matched": 0, "why": ""}
    gaps, swaps, per_book = [], [], {}   # per_book: book -> [shared, identical, [gaps]]
    for pm in extra:
        fm = idx.get(_key(pm))
        if fm is None:
            continue   # new lines aren't added: alt ladders would flood the board
        stats["markets_matched"] += 1
        have = {same_book(b): i for i, (b, _p) in enumerate(fm.books)}
        for b, prices in pm.books:
            kb = same_book(b)
            if kb not in have:
                fm.books.append((b, list(prices)))
                if b in pm.updated:
                    fm.updated[b] = pm.updated[b]
                stats["books_added"] += 1
                continue
            old = fm.books[have[kb]][1]
            pb = per_book.setdefault(kb, [0, 0, []])
            for a, c in zip(old, prices):
                if a is not None and c is not None:
                    g = abs(_imp(a) - _imp(c))
                    stats["overlap"] += 1
                    stats["same"] += a == c
                    gaps.append(g)
                    pb[0] += 1
                    pb[1] += a == c
                    pb[2].append(g)
            if _bk(b) in PUSH_BOOKS and list(prices) != list(old) and all(p is not None for p in prices):
                swaps.append((fm, have[kb], prices))
    if how == "fresh" and swaps:
        # each book earns it on its own: enough shared prices, half identical, small median gap
        trusted, refused = set(), []
        for kb, (n, same, gs) in per_book.items():
            med = statistics.median(gs) if gs else 1.0
            if n >= MIN_OVERLAP and same / n >= MIN_SAME and med <= MAX_GAP:
                trusted.add(kb)
            elif kb in {same_book(fm.books[i][0]) for fm, i, _p in swaps}:
                refused.append(f"{kb} ({n} shared, {same / n if n else 0:.0%} identical, median gap {med * 100:.1f} pts)")
        now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        for fm, i, prices in swaps:
            name = fm.books[i][0]
            if same_book(name) in trusted:
                fm.books[i] = (name, list(prices))
                fm.updated[name] = now
                stats["refreshed"] += 1
        stats["applied_fresh"] = stats["refreshed"] > 0
        if refused:
            stats["why"] = "kept The Odds API prices for " + "; ".join(refused)
    LAST[sport.upper() or "?"] = dict(stats, at=dt.datetime.now().strftime("%H:%M"))
    log.info("propline merge %s: %s", sport, stats)
    return stats


def _as_odds_api(pl_event: dict, oa_event: dict) -> dict:
    """A PropLine event's odds relabelled with The Odds API's event id, teams and start time."""
    return dict(pl_event, id=oa_event["id"], commence_time=oa_event["commence_time"],
                home_team=oa_event["home_team"], away_team=oa_event["away_team"])


def propline_props(sport: str, events: list, keys: str, tz) -> list:
    """FeedMarkets from PropLine for these Odds API events (props), keyed to their ids."""
    from concurrent.futures import ThreadPoolExecutor
    from . import propline
    from .oddsapi import parse_event_props

    # PropLine keeps alt rungs under the main key: ask for main keys only (an unknown key is a 4xx)
    main = ",".join(sorted({k.replace("_alternate", "") for k in keys.split(",") if k}))

    def one(e):
        try:
            ev = propline.find_event(sport, e["home_team"], e["away_team"], e["commence_time"])
            if not ev:
                return []
            data = propline.event_odds(sport, ev["id"], main, books_param())
            return parse_event_props(_as_odds_api(data, e), sport, tz)
        except Exception as ex:
            log.warning("propline props %s %s: %s", sport, e.get("id"), ex)
            return []
    with ThreadPoolExecutor(max_workers=4) as pool:
        return [fm for rows in pool.map(one, events) for fm in rows]


def propline_games(sport: str, events: list, markets: str, tz) -> list:
    """FeedMarkets from PropLine's bulk game lines (one request), keyed to Odds API ids."""
    from . import propline
    from .oddsapi import norm, parse_game_odds
    key = propline.sport_key(sport)
    if not key or not events:
        return []
    try:
        bulk = propline._cached(f"games|{key}|{markets}", propline.ODDS_TTL,
                                lambda: propline._call(f"/sports/{key}/odds",
                                                       {"markets": markets, "bookmakers": books_param()}))
    except Exception as ex:
        log.warning("propline games %s: %s", sport, ex)
        return []
    by_teams = {}
    for ev in bulk or []:
        by_teams.setdefault(frozenset((norm(ev.get("home_team", "")), norm(ev.get("away_team", "")))), []).append(ev)
    matched = []
    for e in events:
        cands = by_teams.get(frozenset((norm(e["home_team"]), norm(e["away_team"]))), [])
        if not cands:
            continue
        when = dt.datetime.fromisoformat(e["commence_time"].replace("Z", "+00:00"))

        def gap(ev):
            try:
                return abs((dt.datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00")) - when).total_seconds())
            except (KeyError, ValueError):
                return 0
        best = min(cands, key=gap)
        if gap(best) <= 8 * 3600:
            matched.append(_as_odds_api(best, e))
    return parse_game_odds(matched, sport, tz) if matched else []


def status_line() -> Optional[str]:
    if not LAST:
        return None
    parts = []
    for sport, s in sorted(LAST.items()):
        share = s["same"] / s["overlap"] if s["overlap"] else 0
        parts.append(f"{sport} {s['at']}: +{s['books_added']} books, {s['refreshed']} fresher prices, "
                     f"{share:.0%} of {s['overlap']} shared identical")
    return "PropLine merge: " + " · ".join(parts)
