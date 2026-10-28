"""Parlays from high-hit-rate legs (volume mode).

A parlay is placed at one book, so each is built from that book's own leg prices. Legs come
from different games so a single game can't sink several of them together. The combined hit
chance multiplies the legs' blended probabilities (independence assumed across games), and
the payout multiplies the legs' decimal odds; the book's actual parlay price can differ a
little from that product.
"""
import os
from dataclasses import dataclass, field
from typing import Optional

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
    listed_decimal: float = 1.0   # the legs' odds multiplied (what a regular parlay would pay)
    sgp_estimated: bool = False   # same-game legs: `decimal` is an estimate of the book's SGP price

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


def sgp_margin() -> float:
    """Extra margin a book takes on same-game parlays (LINEMAKER_SGP_MARGIN, default 5%)."""
    import os
    try:
        return float(os.environ.get("LINEMAKER_SGP_MARGIN", "") or 0.05)
    except ValueError:
        return 0.05


def sgp_payout(listed: float, indep: float, joint: float, groups: int) -> float:
    """What a book pays for same-game legs: it prices their correlation, so the multiplied
    odds shrink by independent / joint chance, then by its SGP margin per game group. Without
    this, legs that move together would look like free value (joint chance x full payout)."""
    if not groups or joint <= 0:
        return listed
    return max(1.01, listed * (indep / joint) * (1 - sgp_margin()) ** groups)


def price(book: str, chosen: list, kind: str = "", style: str = "safest", boost: float = 0.0) -> Parlay:
    """A Parlay for [(ev, odds at this book)]: payout multiplies the prices (plus any boost),
    hit chance is the correlated joint chance (parlay_math). Same-game legs are priced the way
    the book prices an SGP (sgp_payout), marked as an estimate."""
    from .parlay_math import boosted, hit_chance, joint_prob
    p = Parlay(book=book, legs=list(chosen), kind=kind, style=style, boost=boost)
    for ev, odds in p.legs:
        p.indep_prob *= hit_chance(ev)
        p.decimal *= american_to_decimal(odds)
    p.listed_decimal = p.decimal
    p.raw_prob = joint_prob([ev for ev, _o in p.legs])
    check = sgp_check([ev for ev, _o in p.legs], book)
    if check["groups"]:
        p.decimal = sgp_payout(p.decimal, p.indep_prob, p.raw_prob, len(check["groups"]))
        p.sgp_estimated = True
        p.kind = p.kind or check["kind"]
    p.decimal = boosted(p.decimal, boost) if boost else p.decimal
    from .autotune import parlay_calibrated
    from .tracking import kind_of
    phase = "live" if any(getattr(ev, "phase", "pre") == "live" for ev, _o in p.legs) else "pre"
    kinds = {kind_of(getattr(ev.market, "bet_type", "prop")) for ev, _o in p.legs}
    p.prob = parlay_calibrated(p.raw_prob, phase, kinds)   # checked against graded parlays (autotune)
    return p


def leg_price(ev, book: str):
    prices = book_prices(ev.market, ev.best.side)
    return next((o for b, o in prices.items() if _key(b) == _key(book)), None)


# ---- markets a book won't put in a parlay ---------------------------------------------
# The books mark some markets "can't be parlayed" (DraftKings does it per market and it
# changes); the odds feed doesn't say which. A ⛔ report from a Bet reply is only recorded
# (cache/parlay_refusals.json: book, market, side, line, odds, the other legs) so the
# pattern can be worked out later; nothing is blocked by it. LINEMAKER_NO_PARLAY blocks
# markets by hand if that's ever wanted: "DraftKings:NBA|player_steals,FanDuel:NFL|anytime td".

def _refusals_path() -> str:
    return os.path.join(os.environ.get("LINEMAKER_CACHE_DIR", "cache"), "parlay_refusals.json")


def no_parlay_markets() -> dict:
    """{book key: {"SPORT|market", ...}} from LINEMAKER_NO_PARLAY (empty unless set)."""
    out = {}
    for item in os.environ.get("LINEMAKER_NO_PARLAY", "").split(","):
        book, _, market = item.strip().partition(":")
        if book and market:
            out.setdefault(_key(book), set()).add(market.strip())
    return out


def leg_source(ev, book: str) -> str:
    """"SPORT|book market key" for this leg at this book (player_steals, player_points_alternate),
    or "SPORT|market name" when the feed didn't say."""
    from .betlinks import leg
    e = leg(ev.market, ev.best.side, book) or {}
    return f"{ev.market.sport}|{e.get('mk') or ev.market.market}"


def load_refusals() -> list:
    from .jsonio import load_json
    path = _refusals_path()
    try:
        data = load_json(path) if os.path.exists(path) else []
    except (OSError, ValueError):
        data = []
    return data if isinstance(data, list) else []


def record_refusal(ev, book: str, legs: list, odds=None) -> dict:
    """Log that `book` wouldn't parlay this leg (with the rest of the slip), nothing more."""
    import datetime as _dt
    from .jsonio import save_json
    m = ev.market
    entry = {"time": _dt.datetime.now().isoformat(timespec="seconds"), "book": book,
             "market": leg_source(ev, book), "label": ev.best.label, "side": ev.best.side,
             "line": getattr(m, "line", None), "odds": odds, "game": m.game, "commence": getattr(m, "commence", ""),
             "with": [e.best.label for e in legs if e is not ev]}
    data = load_refusals() + [entry]
    os.makedirs(os.path.dirname(_refusals_path()) or ".", exist_ok=True)
    save_json(_refusals_path(), data[-500:])
    return entry


def refusal_counts(book: str = "") -> dict:
    """{"SPORT|market": times refused} (for one book, or all)."""
    out = {}
    for r in load_refusals():
        if not book or _key(r.get("book", "")) == _key(book):
            out[r["market"]] = out.get(r["market"], 0) + 1
    return out


def is_prop_under(ev) -> bool:
    return getattr(ev.market, "bet_type", "") == "prop" and ev.best.side == "B" and " Under " in (ev.best.label or "")


def no_under_parlay_books() -> set:
    """LINEMAKER_NO_UNDER_PARLAYS: books that won't put any player-prop under in a parlay.
    Off by default: DraftKings took points and assists unders in parlays on 10/27; what it
    refused were single markets (steals, walks), which LINEMAKER_NO_PARLAY covers."""
    raw = os.environ.get("LINEMAKER_NO_UNDER_PARLAYS", "")
    return {_key(b) for b in raw.split(",") if b.strip()}


def no_parlay(ev, book: str, blocked: Optional[dict] = None) -> bool:
    if is_prop_under(ev) and _key(book) in no_under_parlay_books():
        return True
    blocked = no_parlay_markets() if blocked is None else blocked
    mine = blocked.get(_key(book)) or set()
    return bool(mine) and (leg_source(ev, book) in mine or f"{ev.market.sport}|{ev.market.market}" in mine)


def parlay_leg_price(ev, book: str, blocked: Optional[dict] = None):
    """leg_price, or None when the book won't take this market in a parlay."""
    return None if no_parlay(ev, book, blocked) else leg_price(ev, book)


FAIR_VALUE = 0.98   # legs worth less than this (2%+ under fair) are left out when possible


def _held_players(used: Optional[dict], book: str):
    """The players already in a parlay on the card, at either book (None: no spreading):
    place a FanDuel and a DraftKings parlay and they still don't share a player."""
    return None if used is None else used.setdefault("card", set())


def _fresh(priced: list, taken) -> list:
    """Legs whose player isn't in another parlay on the card (game lines always allowed)."""
    if taken is None:
        return priced
    return [x for x in priced if x[0].market.bet_type != "prop" or exposure(x[0]) not in taken]


def _take(taken, chosen) -> None:
    if taken is not None:
        taken.update(exposure(ev) for ev, _o in chosen if ev.market.bet_type == "prop")


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


def build_parlays(legs: list, sizes=(2, 3, 4), books=PARLAY_BOOKS, value: bool = True,
                  used: Optional[dict] = None) -> list:
    """Per book and size, one leg per game:
      safest  the likeliest legs, skipping legs priced 2%+ worse than fair while enough remain
      value   the legs that beat their price by the most (hit chance x odds above 1), only
              when the whole parlay is +EV; usually a bigger payout for the same number of legs
    used: {book: players already in a parlay on the card}, shared across builders, so a
    player shows up in one parlay per book (place several and one player can't sink them all).
    """
    from .parlay_math import leg_value
    out = []
    for book in books:
        priced = [(ev, parlay_leg_price(ev, book)) for ev in legs]
        priced = [(ev, o) for ev, o in priced if o is not None]
        by_prob = sorted(priced, key=lambda x: x[0].best.blended, reverse=True)
        fair = [x for x in by_prob if leg_value(*x) >= FAIR_VALUE]
        by_value = sorted([x for x in priced if leg_value(*x) > 1.0], key=lambda x: leg_value(*x), reverse=True)
        taken = _held_players(used, book)
        for size in sizes:
            chosen = _one_per_game(_fresh(fair, taken), size) or _one_per_game(_fresh(by_prob, taken), size)
            if chosen:
                out.append(price(book, chosen))
                _take(taken, chosen)
            if value:
                best = _one_per_game(_fresh(by_value, taken), size)
                if best and {id(e) for e, _o in best} != {id(e) for e, _o in chosen}:
                    p = price(book, best, style="value")
                    if p.prob * p.decimal > 1:
                        out.append(p)
                        _take(taken, best)
    return out


def build_edge_parlays(legs: list, sizes=(2, 3), books=PARLAY_BOOKS, used: Optional[dict] = None) -> list:
    """🎯 Edge parlays: the model's own picks (legs priced above fair by a real edge), one per
    game, biggest edge first, only legs that are +EV at that book and only when the whole
    ticket is +EV. They hit less often than the safest ones and pay more."""
    from .parlay_math import leg_value
    out = []
    for book in books:
        priced = [(ev, parlay_leg_price(ev, book)) for ev in legs]
        priced = sorted([(ev, o) for ev, o in priced if o is not None and leg_value(ev, o) > 1.0],
                        key=lambda x: x[0].best.gap, reverse=True)
        taken = _held_players(used, book)
        for size in sizes:
            chosen = _one_per_game(_fresh(priced, taken), size)
            if chosen:
                p = price(book, chosen, style="edge")
                if p.prob * p.decimal > 1:
                    out.append(p)
                    _take(taken, chosen)
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
    if len(legs) > 1:   # markets you've told the bot this book won't parlay
        blocked += [f"{ev.best.label} ({book} doesn't allow "
                    + ("player-prop unders" if is_prop_under(ev) and _key(book) in no_under_parlay_books()
                       else "this market") + " in parlays)"
                    for ev in legs if no_parlay(ev, book)]
        # NBA integrity rule (2025-26): no parlay with two unders on the same player, any book.
        unders = {}
        for ev in legs:
            if is_prop_under(ev) and ev.market.sport in ("NBA", "WNBA"):
                unders.setdefault(ev.market.subject, []).append(ev)
        blocked += [f"{' + '.join(e.best.label for e in evs)} (the NBA bars two unders on one player "
                    "in a parlay)" for evs in unders.values() if len(evs) > 1]
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


def build_sgps(legs: list, books=PARLAY_BOOKS, sgp_size: int = 3, sgpx_size: int = 4,
               used: Optional[dict] = None) -> list:
    """Auto same-game parlays at each book: the likeliest 3-leg SGP from one game, and an
    SGPx / SGP+ of the two likeliest 2-leg game groups (or one group plus the best legs from
    other games). Legs that clash (two lines of one stat, both sides, ML + spread) are
    skipped. The hit chance accounts for the legs moving together (parlay_math); the book's
    SGP price is lower than the multiplied odds."""
    out = []
    for book in books:
        taken = _held_players(used, book)

        def games_now():
            by_game = {}
            for ev in sorted(legs, key=lambda e: e.best.blended, reverse=True):
                odds = parlay_leg_price(ev, book)
                if odds is not None and _fresh([(ev, odds)], taken):
                    by_game.setdefault(ev.market.game_id, []).append(ev)
            return by_game
        by_game = games_now()
        odds_of = {id(ev): parlay_leg_price(ev, book) for evs in by_game.values() for ev in evs}

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
            best = max(sgps, key=score)
            out.append(make(best, one))
            _take(taken, [(ev, None) for ev in best])
            by_game = games_now()
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
                _take(taken, [(ev, None) for ev in chosen])
    return out


TARGETS = (2.0, 3.0, 4.0, 6.0)   # decimal payouts: +100, +200, +300, +500


def exposure(ev) -> tuple:
    """What a leg puts at risk: a player (all his props rise and fall together) or a game's
    result (a team's moneyline, spread and the total lose together)."""
    m = ev.market
    return (m.game_id, m.subject) if m.bet_type == "prop" else (m.game_id, "game line")


def swap_held(p, pool: list, held: Optional[dict] = None, cap: Optional[int] = None):
    """(parlay, notes) for a card's parlay opened after you've placed others: each player
    already in `cap`+ open bets is swapped for the best leg of the pool (same style: biggest
    edge / best value / likeliest) that isn't held or already on the ticket, from another
    game (from the same game for a same-game group). A leg with no replacement is dropped
    while 2+ legs remain. (None, notes) when the ticket can't be rebuilt; (p, []) unchanged."""
    from .bets import max_exposure, open_exposure
    from .parlay_math import leg_value
    held = open_exposure() if held is None else held
    cap = max_exposure() if cap is None else cap
    if not cap:
        return p, []

    def is_held(ev):
        return ev.market.bet_type == "prop" and held.get(exposure(ev), {}).get("n", 0) >= cap
    if not any(is_held(ev) for ev, _o in p.legs):
        return p, []
    score = {"edge": lambda ev, o: ev.best.gap, "value": lambda ev, o: leg_value(ev, o)}.get(
        p.style, lambda ev, o: ev.best.blended)
    keep = [(ev, o) for ev, o in p.legs if not is_held(ev)]
    notes = []
    for ev, _o in p.legs:
        if not is_held(ev):
            continue
        same_game = sum(e.market.game_id == ev.market.game_id for e, _x in p.legs) > 1
        taken = {exposure(e) for e, _x in keep} | {e.market.key for e, _x in keep}
        games = {e.market.game_id for e, _x in keep}
        best = None
        for cand in pool:
            o = parlay_leg_price(cand, p.book)
            if (o is None or is_held(cand) or exposure(cand) in taken or cand.market.key in taken
                    or (p.style in ("edge", "value") and leg_value(cand, o) <= 1.0)):
                continue
            if same_game:
                if cand.market.game_id != ev.market.game_id or sgp_check([e for e, _x in keep] + [cand], p.book)["blocked"]:
                    continue
            elif cand.market.game_id in games:
                continue
            if best is None or score(cand, o) > score(*best):
                best = (cand, o)
        if best:
            keep.append(best)
            notes.append(f"{ev.best.label} → {best[0].best.label}")
        else:
            notes.append(f"{ev.best.label} dropped (no fresh leg to swap in)")
    if len(keep) < 2:
        return None, notes
    same_game_left = sgp_check([e for e, _x in keep], p.book)["groups"]
    return price(p.book, keep, kind=p.kind if same_game_left else "", style=p.style), notes


def _ladder_limits() -> tuple:
    """(legs a parlay may share with the other payouts, payouts one leg may appear in,
    how close a fully fresh parlay must come to the best one to be chosen instead)."""
    import os

    def num(name, default, cast):
        try:
            return cast(os.environ.get(name, "") or default)
        except ValueError:
            return default
    return (num("LINEMAKER_TARGET_MAX_SHARED", 1, int), num("LINEMAKER_TARGET_MAX_USES", 2, int),
            num("LINEMAKER_TARGET_FRESH_FLOOR", 0.8, float))


def _search(cands: list, target: float, max_legs: int, evaluate, allowed, used: dict):
    """The combo of cands ([(ev, odds)]) paying at least target with the best hit chance,
    keeping the payout ladder spread out (used: exposure -> parlays it's already in):
    no leg past its use limit, at most MAX_SHARED reused legs, and a parlay with no reused
    leg wins unless it hits less than FRESH_FLOOR as often as the best one that shares.
    evaluate(combo) -> (hit chance, payout as decimal odds)."""
    import itertools
    import math
    max_shared, max_uses, floor = _ladder_limits()
    best = {}   # shares a leg? -> ((p, p x payout), combo)
    for k in range(1, min(max_legs, len(cands)) + 1):
        for combo in itertools.combinations(cands, k):
            listed = math.prod(american_to_decimal(o) for _e, o in combo)
            if listed < target:
                continue      # an SGP pays less than the multiplied odds, never more
            exps = [exposure(e) for e, _o in combo]
            if any(used.get(x, 0) >= max_uses for x in exps):
                continue
            shared = sum(1 for x in exps if used.get(x))
            if shared > max_shared or not allowed(combo):
                continue
            p, d = evaluate(combo)
            if d < target:
                continue
            key = (p, p * d)
            bucket = shared > 0
            if bucket not in best or key > best[bucket][0]:
                best[bucket] = (key, combo)
    fresh, mixed = best.get(False), best.get(True)
    if fresh and (mixed is None or fresh[0][0] >= floor * mixed[0][0]):
        return fresh[1]
    return (mixed or fresh or (None, None))[1]


def best_for_target(legs: list, book: str, target: float, max_legs: int = 6, pool: int = 12,
                    used: Optional[dict] = None) -> "Parlay | None":
    """The parlay with the highest hit chance that pays at least `target` (decimal) at this
    book: one leg per game (so legs don't move together), each at least fair-ish (hit chance
    x odds >= 0.97), searched exhaustively over the best-value legs. At a fixed payout the hit
    chance is (1 / payout) x the product of the legs' values, so this mostly picks the legs
    priced furthest above fair, and as few of them as the payout allows. used: the legs
    already in the other payouts' parlays (see _search), so one chalk leg can't sink them all."""
    import math
    from .parlay_math import hit_chance, leg_value
    if len({ev.market.game_id for ev in legs}) == 1:
        return _best_same_game(legs, book, target, max_legs, pool, used or {})
    best_per_game = {}
    for ev in legs:
        o = parlay_leg_price(ev, book)
        if o is None or leg_value(ev, o) < 0.97:
            continue
        g = ev.market.game_id
        if g not in best_per_game or leg_value(ev, o) > leg_value(*best_per_game[g]):
            best_per_game[g] = (ev, o)
    cands = sorted(best_per_game.values(), key=lambda x: leg_value(*x), reverse=True)[:pool]
    best = _search(cands, target, max_legs,
                   lambda combo: (math.prod(hit_chance(e) for e, _o in combo),
                                  math.prod(american_to_decimal(o) for _e, o in combo)),
                   lambda combo: True, used or {})
    if best is None:
        return None
    return price(book, list(best), style="target")


def _best_same_game(legs: list, book: str, target: float, max_legs: int, pool: int, used: dict):
    """One game only (you asked about one team or matchup): the same search, but legs share
    the game, so one leg per player, combos the book refuses are skipped, and the hit chance
    is the correlated joint chance."""
    from .parlay_math import joint_prob, leg_value
    best_per_player = {}
    for ev in legs:
        o = parlay_leg_price(ev, book)
        if o is None or leg_value(ev, o) < 0.97:
            continue
        k = ev.market.subject if ev.market.bet_type == "prop" else ev.market.key
        if k not in best_per_player or leg_value(ev, o) > leg_value(*best_per_player[k]):
            best_per_player[k] = (ev, o)
    cands = sorted(best_per_player.values(), key=lambda x: leg_value(*x), reverse=True)[:min(pool, 10)]
    def evaluate(combo):
        import math
        from .parlay_math import hit_chance
        listed = math.prod(american_to_decimal(o) for _e, o in combo)
        if len(combo) == 1:
            return hit_chance(combo[0][0]), listed
        joint = joint_prob([e for e, _o in combo], draws=3000)
        indep = math.prod(hit_chance(e) for e, _o in combo)
        return joint, sgp_payout(listed, indep, joint, 1)
    best = _search(cands, target, max_legs, evaluate,
                   lambda combo: len(combo) == 1 or not sgp_check([e for e, _o in combo], book)["blocked"],
                   used)
    if best is None:
        return None
    one, _multi = SGP_NAMES.get(book, ("SGP", "SGP+"))
    return price(book, list(best), kind=one if len(best) > 1 else "", style="target")
