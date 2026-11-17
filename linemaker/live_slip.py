"""Slips for games that already started.

A pre-game bet's slip ids die at kickoff: the books move it to a live market with new ids, so a
pre-game link opens to "Your pick couldn't be added". When you tap Bet on a parlay with a leg
whose game has started, the bot reads that game's live odds (one Odds API call per started game,
only the markets those legs need) and swaps in the live ids and price for the same bet: same
player or team, same market, same line, same side. A leg whose line isn't offered live keeps its
old link and is reported, with the nearest live line, so you can add it by hand.
"""
import copy
import datetime as dt
import logging
import os
from typing import Optional
from zoneinfo import ZoneInfo

log = logging.getLogger("linemaker.live_slip")
GAME_KEYS = {"moneyline": "h2h", "spread": "spreads", "total": "totals"}


def started(m, now: Optional[dt.datetime] = None) -> bool:
    try:
        t = dt.datetime.fromisoformat((getattr(m, "commence", "") or "").replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return False
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    return t <= (now or dt.datetime.now(dt.timezone.utc))


def _api_key(m) -> Optional[str]:
    from .oddsapi import ALT_MARKETS, EXTRA_MARKETS, PROP_MARKETS, sport_key
    if m.bet_type in GAME_KEYS:
        return GAME_KEYS[m.bet_type]
    family = sport_key(m.sport).split("_")[0]
    for table in (PROP_MARKETS, EXTRA_MARKETS, ALT_MARKETS):
        for key, name in (table.get(family) or {}).items():
            if name == m.market:
                return key
    return None


def _norm(s: str) -> str:
    from .oddsapi import norm
    return norm(s or "")


def _live_markets(sport: str, game_id: str, keys: list, api) -> list:
    from .oddsapi import fetch_event_props, parse_event_props, parse_game_odds
    tz = ZoneInfo(os.environ.get("LINEMAKER_TZ", "America/New_York"))
    event = fetch_event_props(api, sport, game_id, ",".join(sorted(set(keys))))
    if not event or not event.get("bookmakers"):
        return []
    out = parse_game_odds([event], sport, tz) if any(k in GAME_KEYS.values() for k in keys) else []
    if any(k not in GAME_KEYS.values() for k in keys):
        out += parse_event_props(event, sport, tz)
    return out


def refresh(p, api=None, now: Optional[dt.datetime] = None) -> tuple:
    """(parlay with live slip ids and prices for started legs, notes). Unchanged (p, []) when
    nothing has started or the book isn't FanDuel / DraftKings."""
    from .betlinks import _key, leg
    from .parlay import price
    book = p.book
    if _key(book) not in ("fanduel", "draftkings"):
        return p, []
    todo = [ev for ev, _o in p.legs if started(ev.market, now)]
    if not todo:
        return p, []
    if api is None:
        from .oddsapi import OddsAPI
        api = OddsAPI()
    by_game = {}
    for ev in todo:
        k = _api_key(ev.market)
        if k:
            by_game.setdefault((ev.market.sport, ev.market.game_id), set()).add(k)
    live = {}
    for (sport, gid), keys in by_game.items():
        try:
            live[gid] = _live_markets(sport, gid, sorted(keys), api)
        except Exception as e:
            log.info("live odds for %s: %s", gid, e)
            live[gid] = []
    notes, legs, changed = [], [], False
    for ev, odds in p.legs:
        m = ev.market
        if ev not in todo:
            legs.append((ev, odds))
            continue
        same = [f for f in live.get(m.game_id, []) if f.bet_type == m.bet_type and f.market == m.market
                and _norm(f.subject) == _norm(m.subject)]
        idx = m.side_keys.index(ev.best.side)
        hit = next((f for f in same if abs(float(f.line) - float(m.line)) < 1e-6), None)
        entry = None
        if hit is not None:
            entries = next((v for b, v in (hit.links or {}).items() if _key(b) == _key(book)), None) or []
            entry = entries[idx] if idx < len(entries) else None
            new_odds = next((o[idx] for b, o in hit.books if _key(b) == _key(book) and idx < len(o)), None)
        if hit is None or not entry or new_odds is None:
            near = sorted({float(f.line) for f in same if any(_key(b) == _key(book) for b, _o in f.books)},
                          key=lambda x: abs(x - float(m.line)))
            notes.append(f"{ev.best.label}: not offered live at {m.line:g} on {book}"
                         + (f" (live line {near[0]:g}); add it from the game's live page" if near else
                            "; add it from the game's live page"))
            legs.append((ev, odds))
            continue
        mk = copy.copy(m)
        mk.links = {**(m.links or {}), book: [entry if i == idx else None for i in range(len(m.side_keys))]}
        mk.books = [(b, o) for b, o in m.books if _key(b) != _key(book)] \
            + [(book, [new_odds if i == idx else None for i in range(len(m.side_keys))])]
        ev2 = copy.copy(ev)
        ev2.market = mk
        legs.append((ev2, int(new_odds)))
        changed = True
        if int(new_odds) != int(odds):
            from .odds import fmt_odds
            notes.append(f"{ev.best.label}: game started, live price {fmt_odds(int(new_odds))} (was {fmt_odds(int(odds))})")
        if leg(mk, ev.best.side, book) is None:
            notes.append(f"{ev.best.label}: no live slip id")
    if not changed:
        return p, notes
    return price(book, legs, kind=getattr(p, "kind", ""), style=getattr(p, "style", "safest")), notes
