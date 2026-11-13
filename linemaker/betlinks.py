"""Betslip deep links: tap a button and FanDuel or DraftKings opens with the bet loaded.

The Odds API returns them when asked (includeLinks / includeSids): each outcome can carry a
`link` (the book's add-to-betslip URL) and a `sid` (the book's own outcome id; the market
carries its own `sid` too). Per leg we keep both, so a parlay link can be built even when
the API's link is for a single selection:

  FanDuel     .../addToBetslip?marketId[0]=M1&selectionId[0]=S1&marketId[1]=M2&selectionId[1]=S2
  DraftKings  .../event/<event>?outcomes=O1+O2

Links that mention {state} get LINEMAKER_STATE (e.g. nj, pa; blank drops the state part).
Books change their link formats now and then; when a leg can't be read the parlay simply
gets no link, never a wrong one.
"""
import os
import re
import urllib.parse
from typing import Optional

FD_BASE = "https://sportsbook.fanduel.com/addToBetslip"
DK_BASE = "https://sportsbook.draftkings.com/event/{event}"


def _key(name: str) -> str:
    return "".join(ch for ch in name.lower() if ch.isalnum())


def links_on() -> bool:
    return os.environ.get("LINEMAKER_BET_LINKS", "on").strip().lower() not in ("off", "0", "false", "no")


def fill_state(url: str) -> str:
    state = os.environ.get("LINEMAKER_STATE", "").strip().lower()
    if state:
        return url.replace("{state}", state)
    return url.replace("{state}.", "").replace(".{state}", "").replace("{state}", "")


def leg(m, side: str, book: str) -> Optional[dict]:
    """{"link", "msid", "sid"} for one side of a market at one book, if the feed had one."""
    entries = next((v for b, v in (m.links or {}).items() if _key(b) == _key(book)), None)
    if not entries:
        return None
    idx = m.side_keys.index(side)
    e = entries[idx] if idx < len(entries) else None
    return e if e and (e.get("link") or e.get("sid") or e.get("mlink") or e.get("elink")) else None


def leg_link(m, side: str, book: str) -> Optional[str]:
    """The best link for one bet: the add-to-betslip link, else one built from the ids, else
    the market's or the event's page at the book (Polymarket sends pages, not slips)."""
    e = leg(m, side, book)
    if not e:
        return None
    if e.get("link"):
        return fill_state(e["link"])
    built = combine(book, [e], m.game_id) if e.get("sid") else None
    return built or fill_state(e.get("mlink") or e.get("elink") or "") or None


def leg_links(parlay) -> list:
    """[(leg label, url)] for each leg of a parlay at its book (where a combined slip can't be
    built, e.g. Polymarket, whose parlays are put together in the app one leg at a time)."""
    out = []
    for ev, _odds in parlay.legs:
        url = leg_link(ev.market, ev.best.side, parlay.book)
        if not url:
            return []
        out.append((getattr(ev.best, "label", "leg"), url))
    return out


def slip_text(parlay, limit: int = 600) -> str:
    """'🧾 Add each leg on Polymarket: 1 · 2 · 3' (markdown links) when the parlay has no
    one-tap slip but every leg has its own page; '' otherwise."""
    if parlay_link(parlay):
        return ""
    legs = leg_links(parlay)
    if not legs:
        return ""
    text = f"🧾 Add each leg on {parlay.book}: " + " · ".join(f"[{i}]({u})" for i, (_l, u) in enumerate(legs, 1))
    return text if len(text) <= limit else ""


def _query(url: str) -> dict:
    return urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)


def _fanduel_ids(e: dict) -> list:
    """[(marketId, selectionId)] carried by one leg."""
    if e.get("link"):
        q = _query(fill_state(e["link"]))
        markets = [v for k, vs in sorted(q.items()) if k.startswith("marketId") for v in vs]
        sels = [v for k, vs in sorted(q.items()) if k.startswith("selectionId") for v in vs]
        if markets and len(markets) == len(sels):
            return list(zip(markets, sels))
    if e.get("msid") and e.get("sid"):
        return [(str(e["msid"]), str(e["sid"]))]
    return []


def _draftkings_ids(e: dict) -> list:
    if e.get("link"):
        q = _query(fill_state(e["link"]))
        ids = [x for v in q.get("outcomes", []) for x in re.split(r"[+ ,]", v) if x]
        if ids:
            return ids
    return [str(e["sid"])] if e.get("sid") else []


def combine(book: str, legs: list, event: str = "") -> Optional[str]:
    """One betslip URL holding every leg, or None if any leg can't be read."""
    if not legs:
        return None
    if _key(book) == "fanduel":
        pairs = []
        for e in legs:
            ids = _fanduel_ids(e)
            if not ids:
                return None
            pairs += ids
        base = FD_BASE
        first = next((e["link"] for e in legs if e.get("link")), "")
        if "addtobetslip" in first.lower():
            parts = urllib.parse.urlsplit(fill_state(first))
            base = f"{parts.scheme}://{parts.netloc}{parts.path}"
        query = "&".join(f"marketId[{i}]={urllib.parse.quote(m)}&selectionId[{i}]={urllib.parse.quote(s)}"
                         for i, (m, s) in enumerate(pairs))
        return f"{base}?{query}"
    if _key(book) == "draftkings":
        ids = []
        for e in legs:
            got = _draftkings_ids(e)
            if not got:
                return None
            ids += got
        first = next((e["link"] for e in legs if e.get("link")), "")
        if first:
            parts = urllib.parse.urlsplit(fill_state(first))
            base = f"{parts.scheme}://{parts.netloc}{parts.path}"
        else:
            base = DK_BASE.format(event=urllib.parse.quote(event or ""))
        return f"{base}?outcomes={'+'.join(urllib.parse.quote(i) for i in ids)}"
    return None if len(legs) > 1 else (fill_state(legs[0]["link"]) if legs[0].get("link") else None)


def book_home(book: str, live: bool = False) -> str:
    """The book's sportsbook page (live section for in-game bets), for when no slip can be built."""
    if _key(book) == "polymarket":
        return "https://polymarket.com/sports/live" if live else "https://polymarket.com/sports"
    if _key(book) == "draftkings":
        return "https://sportsbook.draftkings.com/live" if live else "https://sportsbook.draftkings.com/"
    return "https://sportsbook.fanduel.com/live" if live else "https://sportsbook.fanduel.com/"


def parlay_link(parlay) -> Optional[str]:
    legs = [leg(ev.market, ev.best.side, parlay.book) for ev, _odds in parlay.legs]
    if any(e is None for e in legs):
        import logging
        missing = [getattr(ev.best, "label", "?") for (ev, _o), e in zip(parlay.legs, legs) if e is None]
        logging.getLogger("linemaker.betlinks").info("no %s slip ids for: %s", parlay.book, "; ".join(missing))
        return None
    return combine(parlay.book, legs, parlay.legs[0][0].market.game_id)


def pick_links(ev, books=None) -> list:
    """[(book, url)] for a single pick's chosen side."""
    if books is None:
        from .pro import parlay_books
        books = parlay_books()
    out = []
    for book in books:
        url = leg_link(ev.market, ev.best.side, book)
        if url:
            out.append((book, url))
    return out
