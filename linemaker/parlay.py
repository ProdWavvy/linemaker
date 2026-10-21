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
