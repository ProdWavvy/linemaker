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
    kind: str = ""          # "", or SGP / SGPx / SGP+ (same-game legs on one ticket)
    style: str = "safest"   # safest (likeliest legs) or value (legs that beat their price)
    indep_prob: float = 1.0  # hit chance if the legs were independent (shown next to prob)
    boost: float = 0.0      # profit boost applied to `decimal` (0.25 = +25%)

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


def price(book: str, chosen: list, kind: str = "", style: str = "safest", boost: float = 0.0) -> Parlay:
    """A Parlay for [(ev, odds at this book)]: payout multiplies the prices (plus any boost),
    hit chance is the correlated joint chance (parlay_math)."""
    from .parlay_math import boosted, hit_chance, joint_prob
    p = Parlay(book=book, legs=list(chosen), kind=kind, style=style, boost=boost)
    for ev, odds in p.legs:
        p.indep_prob *= hit_chance(ev)
        p.decimal *= american_to_decimal(odds)
    p.decimal = boosted(p.decimal, boost) if boost else p.decimal
    p.prob = joint_prob([ev for ev, _o in p.legs])
    return p


def leg_price(ev, book: str):
    prices = book_prices(ev.market, ev.best.side)
    return next((o for b, o in prices.items() if _key(b) == _key(book)), None)


FAIR_VALUE = 0.98   # legs worth less than this (2%+ under fair) are left out when possible


def _one_per_game(priced: list, size: int) -> list:
    chosen, games = [], set()
    for ev, odds in priced:
        if ev.market.game_id in games:
            continue
        chosen.append((ev, odds))
        games.add(ev.market.game_id)
        if len(chosen) == size:
            return chosen
    return []


def build_parlays(legs: list, sizes=(2, 3, 4), books=PARLAY_BOOKS, value: bool = True) -> list:
    """Per book and size, one leg per game:
      safest  the likeliest legs, skipping legs priced 2%+ worse than fair while enough remain
      value   the legs that beat their price by the most (hit chance x odds above 1), only
              when the whole parlay is +EV; usually a bigger payout for the same number of legs
    """
    from .parlay_math import leg_value
    out = []
    for book in books:
        priced = [(ev, leg_price(ev, book)) for ev in legs]
        priced = [(ev, o) for ev, o in priced if o is not None]
        by_prob = sorted(priced, key=lambda x: x[0].best.blended, reverse=True)
        fair = [x for x in by_prob if leg_value(*x) >= FAIR_VALUE]
        by_value = sorted([x for x in priced if leg_value(*x) > 1.0], key=lambda x: leg_value(*x), reverse=True)
        for size in sizes:
            chosen = _one_per_game(fair, size) or _one_per_game(by_prob, size)
            if chosen:
                out.append(price(book, chosen))
            if value:
                best = _one_per_game(by_value, size)
                if best and {id(e) for e, _o in best} != {id(e) for e, _o in chosen}:
                    p = price(book, best, style="value")
                    if p.prob * p.decimal > 1:
                        out.append(p)
    return out


def custom_parlays(legs: list, books=PARLAY_BOOKS, boost: float = 0.0) -> list:
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
        out.append((price(book, priced, boost=boost), missing))
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
            stat = (m.bet_type, m.subject if m.bet_type in ("prop", "team_total") else "", m.market)
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


def _game_group(evs: list, book: str, size: int) -> list:
    """The likeliest legs from one game that the book will take together."""
    group = []
    for ev in evs:
        if not sgp_check(group + [ev], book)["blocked"]:
            group.append(ev)
        if len(group) == size:
            break
    return group if len(group) == size else []


def build_sgps(legs: list, books=PARLAY_BOOKS, sgp_size: int = 3, sgpx_size: int = 4) -> list:
    """Auto same-game parlays at each book: the likeliest 3-leg SGP from one game, and an
    SGPx / SGP+ of the two likeliest 2-leg game groups (or one group plus the best legs from
    other games). Legs that clash (two lines of one stat, both sides, ML + spread) are
    skipped. The hit chance accounts for the legs moving together (parlay_math); the book's
    SGP price is lower than the multiplied odds."""
    out = []
    for book in books:
        by_game = {}
        for ev in sorted(legs, key=lambda e: e.best.blended, reverse=True):
            odds = leg_price(ev, book)
            if odds is not None:
                by_game.setdefault(ev.market.game_id, []).append(ev)
        odds_of = {id(ev): leg_price(ev, book) for evs in by_game.values() for ev in evs}

        def make(chosen, kind):
            return price(book, [(ev, odds_of[id(ev)]) for ev in chosen], kind=kind)

        def score(group):
            prob = 1.0
            for ev in group:
                prob *= ev.best.blended
            return prob

        one, multi = SGP_NAMES.get(book, ("SGP", "SGP+"))
        sgps = [g for g in (_game_group(evs, book, sgp_size) for evs in by_game.values()) if g]
        if sgps:
            out.append(make(max(sgps, key=score), one))
        pairs = sorted((g for g in (_game_group(evs, book, 2) for evs in by_game.values()) if g),
                       key=score, reverse=True)
        if pairs:
            chosen = list(pairs[0])
            games = {chosen[0].market.game_id}
            for g in pairs[1:]:
                if len(chosen) + 2 <= sgpx_size:
                    chosen += g
                    games.add(g[0].market.game_id)
            singles = sorted((ev for gid, evs in by_game.items() if gid not in games for ev in evs[:1]),
                             key=lambda e: e.best.blended, reverse=True)
            chosen += singles[:sgpx_size - len(chosen)]
            if len(chosen) == sgpx_size and len({ev.market.game_id for ev in chosen}) > 1:
                out.append(make(chosen, multi))
    return out
