"""PrizePicks: the same projections, scored against PrizePicks' lines and payout tables.

PrizePicks has no odds: every pick in an entry pays the same multiplier, so a leg is worth
playing when our hit chance beats the entry's break-even rate (2-pick Power at 3x needs
57.7% per leg). The bot:

  1. pulls PrizePicks' standard lines from The Odds API (bookmaker "prizepicks", main
     markets only; goblins/demons have their own multipliers and aren't used yet),
  2. prices each one with the props model, blended with the sportsbooks' no-vig line at the
     same number when FanDuel/DraftKings/etc. post it (PrizePicks' own indicative price is
     not counted as a market),
  3. ranks legs by hit chance (one per player-stat) and flags line gaps (PrizePicks 24.5
     while the books hang 26.5),
  4. builds the best 2-6 pick Power and Flex entries with their hit chance and EV.

Payout tables are settings, because they differ by state and change over time:
  LINEMAKER_PP_POWER  "2:3,3:5,4:10,5:20,6:37.5"         (all correct)
  LINEMAKER_PP_FLEX   "3:2.25/1.25,4:5/1.5,5:10/2/0.4,6:25/2/0.4"  (all, all-1, all-2 correct)
Check them against your PrizePicks app.

Costs about 5 credits per game (one bookmaker, main markets), on demand only.
"""
import itertools
import math
import os
from dataclasses import dataclass, field
from typing import Optional

from .oddsapi import norm

BOOK = "PrizePicks"


def _env(name: str, default: str) -> str:
    return os.environ.get(name, "").strip() or default


def power_table() -> dict:
    """picks -> payout multiplier when every pick hits."""
    out = {}
    for part in _env("LINEMAKER_PP_POWER", "2:3,3:5,4:10,5:20,6:37.5").split(","):
        k, _, v = part.partition(":")
        try:
            out[int(k)] = float(v)
        except ValueError:
            continue
    return out


def flex_table() -> dict:
    """picks -> [payout with all hits, all-1, all-2, ...]."""
    out = {}
    for part in _env("LINEMAKER_PP_FLEX", "3:2.25/1.25,4:5/1.5,5:10/2/0.4,6:25/2/0.4").split(","):
        k, _, v = part.partition(":")
        try:
            out[int(k)] = [float(x) for x in v.split("/") if x]
        except ValueError:
            continue
    return out


def break_even() -> float:
    """Per-leg hit rate a leg needs for the smallest Power entry (2 picks at 3x: 57.7%).
    Bigger entries have lower per-leg break-evens on paper, but far more variance, so the
    leg floor is set by the entry most people play."""
    table = power_table() or {2: 3.0}
    n = min(table)
    return table[n] ** (-1 / n)


def hits_distribution(probs: list) -> list:
    """P(exactly k legs hit) for k = 0..n (independent legs, Poisson-binomial)."""
    dist = [1.0]
    for p in probs:
        nxt = [0.0] * (len(dist) + 1)
        for k, q in enumerate(dist):
            nxt[k] += q * (1 - p)
            nxt[k + 1] += q * p
        dist = nxt
    return dist


@dataclass
class Entry:
    kind: str                     # "Power" or "Flex"
    legs: list                    # ProEval legs
    payouts: list                 # [all hit, all-1, ...] multipliers
    p_all: float = 0.0
    expected: float = 0.0         # expected return per $1 staked (1.0 = break even)

    @property
    def ev(self) -> float:
        """Expected profit per $100."""
        return (self.expected - 1) * 100

    @property
    def size(self) -> int:
        return len(self.legs)


def price_entry(kind: str, legs: list) -> Optional[Entry]:
    n = len(legs)
    payouts = [power_table().get(n)] if kind == "Power" else flex_table().get(n)
    if not payouts or payouts[0] is None:
        return None
    dist = hits_distribution([ev.best.blended for ev in legs])
    expected = sum(payouts[i] * dist[n - i] for i in range(len(payouts)) if n - i >= 0)
    return Entry(kind=kind, legs=list(legs), payouts=payouts, p_all=dist[n], expected=expected)


def _games_ok(legs: list) -> bool:
    """PrizePicks needs players from at least two different teams: require two games."""
    return len({ev.market.game_id for ev in legs}) >= 2


def best_entries(legs: list, sizes=(2, 3, 4, 5, 6), pool: int = 10) -> list:
    """The best Power and Flex entry of each size from the likeliest legs."""
    legs = sorted(legs, key=lambda ev: ev.best.blended, reverse=True)[:pool]
    out = []
    for n in sizes:
        if len(legs) < n:
            continue
        for kind in ("Power", "Flex"):
            best = None
            for combo in itertools.islice(itertools.combinations(legs, n), 2000):
                if not _games_ok(combo):
                    continue
                e = price_entry(kind, combo)
                if e and (best is None or e.expected > best.expected):
                    best = e
            if best:
                out.append(best)
    return out


def pp_config():
    """Engine settings for PrizePicks legs: rank by hit chance, no odds window or price
    gap (there are no odds), a hit-chance floor at the break-even rate."""
    from .pro import ProConfig
    floor = os.environ.get("LINEMAKER_PP_MIN_PROB", "").strip()
    cfg = ProConfig(bet_books=(BOOK,), rank_by="prob", require_model_edge=False, min_gap=-1.0,
                    max_gap=None, min_ev=None, min_odds=None, max_odds=None,
                    min_prob=float(floor) if floor else round(break_even(), 3))
    cfg.track = False            # PrizePicks reads are logged to tracking/pp by the caller
    return cfg


def proj_rows(slate_rows: list) -> list:
    """Projection rows (one per player-stat) from a priced props slate."""
    keep = ("sport", "subject", "market", "mean", "stdev", "dist", "injury_status", "position",
            "notes", "lineup_confirmed")
    seen, out = set(), []
    for r in slate_rows:
        k = (r.get("sport"), norm(r.get("subject", "")), (r.get("market") or "").lower())
        if k in seen or not (r.get("mean") or "").strip():
            continue
        seen.add(k)
        out.append({c: r.get(c, "") for c in keep})
    return out


def book_lines(slate_rows: list) -> dict:
    """(player, market, line) -> sportsbook prices string, and (player, market) -> the
    sportsbooks' main line, for line-gap notes."""
    exact, main = {}, {}
    for r in slate_rows:
        key = (norm(r.get("subject", "")), (r.get("market") or "").lower())
        try:
            line = float(r.get("line") or 0)
        except ValueError:
            continue
        exact[key + (line,)] = r.get("books", "")
        books = r.get("books", "")
        if "FanDuel" in books or "DraftKings" in books:
            main.setdefault(key, []).append(line)
    return {"exact": exact, "main": {k: sorted(v)[len(v) // 2] for k, v in main.items()}}


def run_pp(odds, sport: str, date: str, tz, slate_rows: list):
    """Fetch PrizePicks lines and join them to the props projections and the sportsbook
    prices at the same number. Returns (rows, feed)."""
    from .oddsapi import PROP_MARKETS, books_str, fetch_slate, feed_row, merge, sport_key
    family = sport_key(sport).split("_")[0]
    feed = fetch_slate(odds, sport, date, tz, props=True, game_lines=False,
                       prop_markets=list(PROP_MARKETS.get(family, {})))
    feed = [fm for fm in feed if any(b == BOOK or norm(b) == "prizepicks" for b, _p in fm.books)]
    for fm in feed:
        fm.books = [(BOOK, p) for b, p in fm.books if norm(b) == "prizepicks"]
    index = book_lines(slate_rows)
    rows, _missing = merge(feed, proj_rows(slate_rows))
    for r in rows:
        key = (norm(r["subject"]), r["market"].lower())
        other = index["exact"].get(key + (float(r["line"]),), "")
        if other:   # the sportsbooks' prices at the same number anchor the estimate
            r["books"] = r["books"] + "|" + other
        main = index["main"].get(key)
        if main is not None and abs(main - float(r["line"])) >= 0.5:
            r["notes"] = (r.get("notes") or "") + f"; books' line {main:g}"
    return rows, feed


def line_gap(ev) -> Optional[float]:
    """Books' main line minus PrizePicks' line, from the row notes (positive = PP lower)."""
    note = ev.market.notes or ""
    if "books' line " not in note:
        return None
    try:
        return float(note.rsplit("books' line ", 1)[1].split(";")[0]) - ev.market.line
    except ValueError:
        return None


# ---- entry links ---------------------------------------------------------------------

def entry_link(entry: Entry) -> Optional[str]:
    """A PrizePicks link for the entry, built from the legs' links when the API supplies
    them (format unconfirmed: the first scan logs a sample so it can be checked)."""
    import urllib.parse
    from .betlinks import fill_state, leg
    ids, first = [], None
    for ev in entry.legs:
        e = leg(ev.market, ev.best.side, BOOK)
        if not e:
            return None
        url = fill_state(e.get("link") or "")
        first = first or url
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query) if url else {}
        pid = (q.get("projections") or q.get("projId") or q.get("projection_id") or [e.get("sid")])[0]
        if not pid:
            return None
        ids.append(str(pid))
    base = "https://app.prizepicks.com/"
    if first:
        parts = urllib.parse.urlsplit(first)
        base = f"{parts.scheme}://{parts.netloc}{parts.path}"
    return f"{base}?projections={','.join(ids)}"


def board_link() -> str:
    return "https://app.prizepicks.com/"


def settle_entry(bet: dict, outcomes: list) -> Optional[tuple]:
    """(status, profit) for a logged PrizePicks entry, or None while legs are pending.
    A pushed leg (tie / did not play) drops out and the entry pays at the smaller size."""
    if None in outcomes:
        return None
    live = [o for o in outcomes if o != "push"]
    n, hits = len(live), sum(o == "won" for o in live)
    stake = bet["stake"]
    if n < 2:
        return "push", 0.0
    if bet.get("pp_kind") == "Flex":
        table = flex_table().get(n) or []
        idx = n - hits
        mult = table[idx] if idx < len(table) else 0.0
    else:
        mult = (power_table().get(n) or 0.0) if hits == n else 0.0
    profit = round(stake * mult - stake, 2)
    return ("won" if profit > 0 else "push" if profit == 0 else "lost"), profit
