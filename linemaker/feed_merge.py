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
# The push books (fresher prices) plus the sharp ones The Odds API list lacks. Kept short: a full
# NBA slate with every book runs ~4 MB per game (18.5 MB for 5 games in one league call), so
# props are read one game at a time.
DEFAULT_BOOKS = "draftkings,fanduel,fanatics,kalshi,polymarket,pinnacle,betonlineag"
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


# ---- credit saver: props from PropLine, The Odds API for game lines --------------------------
# Player props are most of The Odds API bill (one credit per market per game). With
# LINEMAKER_PROPS_SOURCE=propline (the default once a PropLine key is set) a slate's props come
# from PropLine instead: one request per game (or one per league when the server has the memory
# for it), with FanDuel's and DraftKings' own ids so the Bet buttons still load the slip. Game
# lines, event ids and pick'em (PrizePicks / Underdog multipliers) stay on The Odds API. A game
# PropLine can't price falls back to The Odds API, and a once-a-day check (about 1 credit)
# compares the two feeds on one game: if they disagree, props go back to The Odds API for the day.
SAVER_BOOKS = "draftkings,fanduel,polymarket,kalshi,novig,prophetx,pinnacle,betmgm,hardrockbet,fanatics"
CHECK_MIN, CHECK_SAME, CHECK_GAP = 10, 0.5, 0.03


def _cache(name: str) -> str:
    return os.path.join(os.environ.get("LINEMAKER_CACHE_DIR", "cache"), name)


def _load(name: str) -> dict:
    import json
    try:
        with open(_cache(name)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save(name: str, data: dict) -> None:
    import json
    try:
        os.makedirs(os.path.dirname(_cache(name)) or ".", exist_ok=True)
        with open(_cache(name), "w") as f:
            json.dump(data, f)
    except OSError:
        pass


def _today() -> str:
    from .oddsapi import local_today
    return local_today()


def check_today() -> Optional[dict]:
    c = _load("propline_check.json")
    return c if c.get("date") == _today() else None


def props_source() -> str:
    """"propline" or "oddsapi" for today's player props."""
    from . import propline
    if not propline.on():
        return "oddsapi"
    if (os.environ.get("LINEMAKER_PROPS_SOURCE", "").strip().lower() or "propline") != "propline":
        return "oddsapi"
    c = check_today()
    return "oddsapi" if c and not c.get("ok", True) else "propline"


def saver_books() -> str:
    return os.environ.get("LINEMAKER_PROPLINE_PROPS_BOOKS", "").strip() or SAVER_BOOKS


def mem_available_mb() -> Optional[int]:
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def bulk_ok(n_games: int) -> bool:
    """One league call instead of one per game, when there's room: a 5-game NBA board with
    ten books was 18.5 MB of JSON and ~70 MB in memory while parsed."""
    try:
        need = int(os.environ.get("LINEMAKER_PROPLINE_BULK_MIN_MB", "") or 2000)
    except ValueError:
        need = 2000
    free = mem_available_mb()
    return n_games >= 3 and free is not None and free >= need


def as_odds_api_props(pl: dict, oa_event: dict, alt: bool = False) -> dict:
    """A PropLine event in The Odds API's shape: its ids, main lines (alternates when asked),
    FanDuel / DraftKings add-to-betslip links built from the books' own ids."""
    import urllib.parse
    out = _as_odds_api(pl, oa_event)
    books = []
    for bk in pl.get("bookmakers", []):
        key = (bk.get("key") or "").lower()
        markets = []
        for m in bk.get("markets", []):
            kind = m.get("line_type") or "main"
            if kind == "milestone" or (kind != "main" and not alt):
                continue
            mk = {"key": m["key"] if kind == "main" else m["key"] + "_alternate",
                  "last_update": m.get("last_update") or bk.get("last_update"), "outcomes": []}
            for o in m.get("outcomes", []):
                oc = {k: o[k] for k in ("name", "description", "price", "point") if k in o}
                bid = str(o.get("book_outcome_id") or "")
                if key == "fanduel" and ":" in bid:
                    mid, sel = bid.split(":", 1)
                    oc["sid"], mk["sid"] = sel, mid
                    oc["link"] = (f"https://sportsbook.fanduel.com/addToBetslip?marketId[0]={urllib.parse.quote(mid)}"
                                  f"&selectionId[0]={urllib.parse.quote(sel)}")
                elif key == "draftkings" and bid and bk.get("link"):
                    oc["sid"] = bid
                    oc["link"] = f"{bk['link']}?outcomes={urllib.parse.quote(bid)}"
                mk["outcomes"].append(oc)
            markets.append(mk)
        books.append({"key": key, "title": bk.get("title") or key, "link": bk.get("link") or "",
                      "last_update": bk.get("last_update"), "markets": markets})
    out["bookmakers"] = books
    return out


def _match(bulk: list, events: list) -> dict:
    """{Odds API event id: PropLine event} by teams (either way round) and start time."""
    from .oddsapi import norm
    by_teams = {}
    for ev in bulk or []:
        by_teams.setdefault(frozenset((norm(ev.get("home_team", "")), norm(ev.get("away_team", "")))), []).append(ev)
    out = {}
    for e in events:
        cands = by_teams.get(frozenset((norm(e["home_team"]), norm(e["away_team"]))), [])
        when = dt.datetime.fromisoformat(e["commence_time"].replace("Z", "+00:00"))

        def gap(ev):
            try:
                return abs((dt.datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00")) - when).total_seconds())
            except (KeyError, ValueError):
                return 10 ** 9
        best = min(cands, key=gap) if cands else None
        if best is not None and gap(best) <= 8 * 3600:
            out[e["id"]] = best
    return out


def saver_props(sport: str, events: list, keys: str, tz) -> dict:
    """{Odds API event id: [FeedMarket]} of props from PropLine (games it couldn't price left out)."""
    from concurrent.futures import ThreadPoolExecutor
    from . import propline
    from .oddsapi import parse_event_props
    alt = "_alternate" in keys
    main = ",".join(sorted({k.replace("_alternate", "") for k in keys.split(",") if k}))
    params = {"markets": main, "bookmakers": saver_books(), "includeBookIds": "true", "includeLinks": "true"}
    sk = propline.sport_key(sport)
    if not sk or not events:
        return {}
    payloads = {}
    if bulk_ok(len(events)):
        try:
            first = min(e["commence_time"] for e in events)
            last = max(e["commence_time"] for e in events)
            bulk = propline._call(f"/sports/{sk}/odds", dict(params, commenceTimeFrom=first, commenceTimeTo=last),
                                  timeout=60)
            payloads = _match(bulk, events)
            log.info("propline props %s: one league call, %d of %d games", sport, len(payloads), len(events))
        except Exception as ex:
            log.warning("propline league call %s: %s (one game at a time instead)", sport, ex)

    def one(e):
        try:
            ev = propline.find_event(sport, e["home_team"], e["away_team"], e["commence_time"])
            return propline._call(f"/sports/{sk}/events/{ev['id']}/odds", params) if ev else None
        except Exception as ex:
            log.warning("propline props %s %s: %s", sport, e.get("id"), ex)
            return None
    rest = [e for e in events if e["id"] not in payloads]
    if rest:
        with ThreadPoolExecutor(max_workers=4) as pool:
            payloads.update({e["id"]: p for e, p in zip(rest, pool.map(one, rest)) if p})
    out = {}
    for e in events:
        pl = payloads.get(e["id"])
        if pl:
            rows = parse_event_props(as_odds_api_props(pl, e, alt), sport, tz)
            if rows:
                out[e["id"]] = rows
    return out


def record_saved(credits: int) -> None:
    if credits <= 0:
        return
    d = _load("propline_saved.json")
    day = _today()
    if d.get("date") != day:
        d = {"date": day, "credits": 0, "month": d.get("month") if d.get("month_key") == day[:7] else 0,
             "month_key": day[:7]}
    d["credits"] = d.get("credits", 0) + credits
    d["month"] = d.get("month", 0) + credits
    _save("propline_saved.json", d)


def saved_line() -> Optional[str]:
    d = _load("propline_saved.json")
    if not d:
        return None
    today = d.get("credits", 0) if d.get("date") == _today() else 0
    return f"Credit saver: ~{today:,} Odds API credits saved today, ~{d.get('month', 0):,} this month (props from PropLine)"


def run_daily_check(api, sport: str, events: list, keys: str) -> dict:
    """Once a day: one game's FanDuel / DraftKings props from both feeds (~1 Odds API credit).
    ok=False sends today's props back to The Odds API."""
    from . import propline
    market = next((k for k in keys.split(",") if k and not k.endswith("_alternate")), "player_points")
    now = dt.datetime.now(dt.timezone.utc)
    upcoming = [e for e in events if dt.datetime.fromisoformat(e["commence_time"].replace("Z", "+00:00")) > now]
    result = {"date": _today(), "ok": True, "sport": sport, "market": market, "at": dt.datetime.now().strftime("%H:%M")}
    for e in upcoming[:3]:
        try:
            pl_ev = propline.find_event(sport, e["home_team"], e["away_team"], e["commence_time"])
            if not pl_ev:
                continue
            oa = api.event_odds(sport, e["id"], market)
            pl = propline.event_odds(sport, pl_ev["id"], market, "fanduel,draftkings")
        except Exception as ex:
            log.warning("propline daily check %s: %s", sport, ex)
            continue
        cmp_ = propline.compare(oa, pl)
        share = cmp_["same"] / cmp_["matched"] if cmp_["matched"] else 0.0
        result.update(game=f"{e['away_team']} @ {e['home_team']}", matched=cmp_["matched"], same=cmp_["same"],
                      avg_gap=round(cmp_["avg_gap"], 4), only_oa=cmp_["only_oa"], only_pl=cmp_["only_pl"])
        if cmp_["matched"] >= CHECK_MIN:
            result["ok"] = share >= CHECK_SAME and cmp_["avg_gap"] <= CHECK_GAP
            break
    else:
        result["inconclusive"] = True   # no game to compare: props stay on PropLine
    _save("propline_check.json", result)
    log.warning("propline daily check: %s", result)
    return result


def check_line() -> Optional[str]:
    c = check_today()
    if not c:
        return None
    if c.get("inconclusive"):
        return f"PropLine check {c['at']}: no game to compare yet"
    share = c["same"] / c["matched"] if c.get("matched") else 0
    verdict = "props from PropLine ✅" if c["ok"] else "⚠️ feeds disagree, props back on The Odds API today"
    return (f"PropLine check {c['at']} ({c.get('game', '?')}): {c.get('matched', 0)} lines in both, {share:.0%} "
            f"identical, avg gap {c.get('avg_gap', 0) * 100:.1f} pts · {verdict}")
