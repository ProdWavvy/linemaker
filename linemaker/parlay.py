"""Parlays from high-hit-rate legs (volume mode).

A parlay is placed at one book, so each is built from that book's own leg prices. Legs come
from different games so a single game can't sink several of them together. The combined hit
chance multiplies the legs' blended probabilities (independence assumed across games), and
the payout multiplies the legs' decimal odds; the book's actual parlay price can differ a
little from that product.
"""
from dataclasses import dataclass, field

from .odds import american_to_decimal, decimal_to_american
from .pro import book_prices

PARLAY_BOOKS = ("FanDuel", "DraftKings")


def _key(name: str) -> str:
    return "".join(ch for ch in name.lower() if ch.isalnum())


@dataclass
class Parlay:
    book: str
    legs: list = field(default_factory=list)   # [(ProEval, odds at this book)]
    prob: float = 1.0
    decimal: float = 1.0

    @property
    def american(self) -> int:
        return decimal_to_american(self.decimal)

    @property
    def payout(self) -> float:
        """Total return on a $100 stake."""
        return 100 * self.decimal

    @property
    def ev(self) -> float:
        """Expected profit per $100."""
        return (self.prob * self.decimal - 1) * 100


def leg_price(ev, book: str):
    prices = book_prices(ev.market, ev.best.side)
    return next((o for b, o in prices.items() if _key(b) == _key(book)), None)


def build_parlays(legs: list, sizes=(2, 3, 4), books=PARLAY_BOOKS) -> list:
    """Safest parlay of each size at each book: the likeliest legs, one per game."""
    out = []
    for book in books:
        priced = [(ev, leg_price(ev, book)) for ev in legs]
        priced = sorted([(ev, o) for ev, o in priced if o is not None],
                        key=lambda x: x[0].best.blended, reverse=True)
        for size in sizes:
            chosen, games = [], set()
            for ev, odds in priced:
                if ev.market.game_id in games:
                    continue
                chosen.append((ev, odds))
                games.add(ev.market.game_id)
                if len(chosen) == size:
                    break
            if len(chosen) < size:
                continue
            p = Parlay(book=book, legs=chosen)
            for ev, odds in chosen:
                p.prob *= ev.best.blended
                p.decimal *= american_to_decimal(odds)
            out.append(p)
    return out


def custom_parlays(legs: list, books=PARLAY_BOOKS) -> list:
    """Your own parlay from chosen picks, priced at each book with that book's leg prices.
    Returns [(Parlay, [labels of legs that book doesn't offer])]. Same-game legs are kept
    (it's your slip) but flagged by same_game()."""
    out = []
    for book in books:
        priced, missing = [], []
        for ev in legs:
            odds = leg_price(ev, book)
            if odds is None:
                missing.append(ev.best.label)
            else:
                priced.append((ev, odds))
        if len(priced) < 2:
            continue
        p = Parlay(book=book, legs=priced)
        for ev, odds in priced:
            p.prob *= ev.best.blended
            p.decimal *= american_to_decimal(odds)
        out.append((p, missing))
    return out


def same_game(legs: list) -> list:
    """Game labels with more than one chosen leg (results move together; books may price
    them as a same-game parlay instead)."""
    seen = {}
    for ev in legs:
        seen.setdefault(ev.market.game_id, []).append(ev)
    return [v[0].market.game for v in seen.values() if len(v) > 1]


SGP_NAMES = {"DraftKings": ("SGP", "SGPx"), "FanDuel": ("SGP", "SGP+")}


def sgp_check(legs: list, book: str) -> dict:
    """Can these legs go on one ticket at this book as a same-game parlay?

    Both books take most same-game combos (player props, moneyline, spread, totals) as an
    SGP, and several SGPs plus other games as SGPx (DraftKings) / SGP+ (FanDuel). What they
    refuse: both sides of one market, two lines of the same player stat (or game total, or
    spread), and usually a moneyline with a spread on the same game. Rule-based: the books
    don't publish their SGP menus, so it can't know a market that's simply not SGP-eligible.

    Returns {"groups": [(game, n legs)], "blocked": [reason], "kind": "", "SGP", "SGPx"...}.
    """
    games = {}
    for ev in legs:
        games.setdefault(ev.market.game_id, []).append(ev)
    groups, blocked = [], []
    for evs in games.values():
        if len(evs) < 2:
            continue
        groups.append((evs[0].market.game, len(evs)))
        seen = {}
        for ev in evs:
            m = ev.market
            stat = (m.bet_type, m.subject if m.bet_type == "prop" else "", m.market)
            if m.bet_type in ("moneyline", "spread", "1x2"):
                stat = ("side", "", m.bet_type)
            if stat in seen:
                other = seen[stat]
                why = ("both sides of one market" if other.market.key == m.key
                       else "two lines of the same market")
                blocked.append(f"{other.best.label} + {ev.best.label} ({why})")
            else:
                seen[stat] = ev
        if ("side", "", "moneyline") in seen and ("side", "", "spread") in seen:
            blocked.append(f"{seen[('side', '', 'moneyline')].best.label} + "
                           f"{seen[('side', '', 'spread')].best.label} (moneyline with spread on one "
                           "game: usually not allowed)")
    one, multi = SGP_NAMES.get(book, ("SGP", "SGP+"))
    kind = ""
    if groups:
        kind = one if len(groups) == 1 and len(games) == 1 else multi
    return {"groups": groups, "blocked": blocked, "kind": kind}
