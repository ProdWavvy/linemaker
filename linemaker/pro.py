"""Pro engine: simulate -> de-vig -> blend with market -> bet the gap at the best price.

Each CSV row is one market. Two-way: side A is the Over (props/totals) or the subject team
(spread/moneyline), side B the Under or the opponent. Three-way (bet_type 1x2, soccer):
A = subject win, D = draw, B = opponent win.
"""
import csv
import json
import os
from dataclasses import dataclass, field
from typing import Optional

from .devig import hold, no_vig, overround
from .market import INJURY_EXCLUDE
from .odds import expected_value, fair_odds, fmt_odds, implied_probability, parse_odds
from .projection import Projection, ProjectionInputs, build_projection, soft_market_note
from .sim import SimResult, default_sd, pick_dist, simulate, simulate_1x2, simulate_ladder

def _env_num(name: str, cast):
    raw = os.environ.get(name, "").strip()
    return cast(raw) if raw else None


def _weight(name: str, env: str, default: str) -> float:
    from .autotune import tuned
    v = tuned(name)
    return v if v is not None else float(os.environ.get(env, default))


def _market_weights() -> dict:
    from .autotune import load_weights
    return dict(load_weights().get("markets", {}))


def _default_bet_books() -> Optional[tuple]:
    """Books you can actually bet at. LINEMAKER_BET_BOOKS="all" allows every book."""
    raw = os.environ.get("LINEMAKER_BET_BOOKS", "FanDuel,DraftKings")
    if raw.strip().lower() in ("", "all", "*"):
        return None
    return tuple(b.strip() for b in raw.split(",") if b.strip())


def book_key(name: str) -> str:
    return "".join(ch for ch in name.lower() if ch.isalnum())


THREE_WAY = ("A", "D", "B")
TWO_WAY = ("A", "B")


@dataclass
class ProConfig:
    n_sims: int = 20000
    # Blend weights; tune them from `python -m linemaker calibrate` / `/calibrate`.
    # Auto-tuned values (<tracking>/weights.json, written after each grading) win over .env.
    model_weight: float = field(default_factory=lambda: _weight("model_weight",
                                                                "LINEMAKER_MODEL_WEIGHT", "0.5"))
    model_weight_fresh: float = field(default_factory=lambda: _weight("fresh_weight",
                                                                      "LINEMAKER_FRESH_WEIGHT", "0.8"))
    # Per-market overrides ("NBA points": 0.62), only for markets with a big enough sample.
    market_weights: dict = field(default_factory=lambda: _market_weights())
    # Log every priced market (not just picks) to tracking/<date>.json for calibration.
    track: bool = field(default_factory=lambda: os.environ.get(
        "LINEMAKER_TRACKING", "on").lower() not in ("off", "0", "false", "no"))
    devig_method: str = "proportional" # or "shin" (corrects favorite-longshot bias)
    assumed_hold: float = 0.045        # used only when one side of a 2-way market is missing
    # blended prob minus best-price implied must exceed min_gap; a gap above max_gap against
    # a sharp market is more often stale data (missed injury, moved line) than a real edge.
    min_gap: float = field(default_factory=lambda: _env_num("LINEMAKER_MIN_GAP", float) or 0.0)
    max_gap: Optional[float] = field(default_factory=lambda: _env_num("LINEMAKER_MAX_GAP", float))
    min_ev: Optional[float] = field(default_factory=lambda: _env_num("LINEMAKER_MIN_EV", float))
    # Optional gates, also settable in .env (e.g. -175 / -300 / 0.70).
    min_prob: Optional[float] = field(default_factory=lambda: _env_num("LINEMAKER_MIN_PROB", float))
    min_odds: Optional[int] = field(default_factory=lambda: _env_num("LINEMAKER_ODDS_MIN", int))
    max_odds: Optional[int] = field(default_factory=lambda: _env_num("LINEMAKER_ODDS_MAX", int))
    confirmed_only: bool = False       # only price games with confirmed lineups/inactives
    # Up to 25 picks (the card's dropdown holds 25). LINEMAKER_MAX_PICKS to change.
    max_picks: int = field(default_factory=lambda: _env_num("LINEMAKER_MAX_PICKS", int) or 25)
    # One line per player-stat (or per game market): an alt ladder's best line only, not
    # 25.5 and 26.5 PRA for the same player. LINEMAKER_ONE_PER_STAT=off allows them all.
    one_per_stat: bool = field(default_factory=lambda: os.environ.get(
        "LINEMAKER_ONE_PER_STAT", "on").lower() not in ("off", "0", "false", "no"))
    # Best price is shopped only at these books; consensus still uses every book.
    bet_books: Optional[tuple] = field(default_factory=_default_bet_books)
    # Volume mode: rank by hit chance instead of gap and allow a slightly negative edge.
    require_model_edge: bool = True
    rank_by: str = "gap"          # "gap" (smart) or "prob" (volume)


def volume_config(**overrides) -> "ProConfig":
    """Parlay/volume settings: high hit chance over edge. All tunable in .env."""
    cfg = ProConfig(
        min_prob=_env_num("LINEMAKER_VOLUME_MIN_PROB", float) or 0.75,
        min_odds=_env_num("LINEMAKER_VOLUME_ODDS_MIN", int) or -200,
        max_odds=_env_num("LINEMAKER_VOLUME_ODDS_MAX", int) or -1000,
        min_gap=_env_num("LINEMAKER_VOLUME_MIN_GAP", float) if os.environ.get(
            "LINEMAKER_VOLUME_MIN_GAP", "").strip() else -0.02,
        max_gap=_env_num("LINEMAKER_MAX_GAP", float), min_ev=None,
        max_picks=_env_num("LINEMAKER_VOLUME_LEGS", int) or 25,
        require_model_edge=False, rank_by="prob")
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


@dataclass
class ProMarket:
    date: str
    sport: str
    game_id: str
    game: str
    lineup_confirmed: bool
    bet_type: str                  # prop, total, spread, moneyline, 1x2
    subject: str                   # player, or team for spread/moneyline/1x2
    opponent: str
    market: str
    line: float
    mean: float                    # projected stat / margin / total / subject goals (1x2)
    stdev: Optional[float]
    dist: str                      # blank -> picked from market
    mkt_a_odds: Optional[int]      # consensus prices, used for de-vig (2-way fallback)
    mkt_b_odds: Optional[int]
    best_a_odds: Optional[int]     # best available prices, used for the bet (2-way fallback)
    best_a_book: str
    best_b_odds: Optional[int]
    best_b_book: str
    news_fresh: bool
    injury_status: str
    notes: str = ""
    position: str = ""
    projection: Optional[Projection] = None   # set when the mean was built from inputs
    books: list = field(default_factory=list)  # [(book, [odds or None per side])]
    mean_b: Optional[float] = None             # 1x2: opponent's projected goals
    commence: str = ""                         # ISO start time, when known
    links: dict = field(default_factory=dict)  # book -> [betslip link info per side]

    @property
    def key(self) -> str:
        return f"{self.date}|{self.game_id}|{self.subject}|{self.market}|{self.line:g}"

    @property
    def side_keys(self) -> tuple:
        return THREE_WAY if self.bet_type == "1x2" else TWO_WAY

    def side_label(self, side: str) -> str:
        if self.bet_type == "1x2":
            return {"A": f"{self.subject} win", "D": f"Draw ({self.game})",
                    "B": f"{self.opponent} win"}[side]
        if self.bet_type == "spread":
            team, line = (self.subject, self.line) if side == "A" else (self.opponent, -self.line)
            return f"{team} {line:+g}"
        if self.bet_type == "moneyline":
            return f"{self.subject if side == 'A' else self.opponent} ML"
        word = "Over" if side == "A" else "Under"
        who = self.game if self.bet_type == "total" else self.subject
        return f"{who} {word} {self.line:g} {self.market}"

    @property
    def threshold(self) -> float:
        """Side A wins when the simulated outcome is above this."""
        if self.bet_type == "spread":
            return -self.line      # margin + line > 0
        if self.bet_type in ("moneyline", "1x2"):
            return 0.0
        return self.line


@dataclass
class SideEval:
    side: str
    label: str
    model_prob: float      # model, pushes removed
    market_prob: float     # no-vig market
    blended: float
    best_odds: int
    best_book: str
    best_implied: float
    gap: float             # blended - best_implied
    model_edge: float      # model - no-vig market
    ev: float              # expected profit per $100 at best price, pushes included
    fair: int


@dataclass
class ProEval:
    market: ProMarket
    sim: SimResult
    model_weight: float
    market_hold: Optional[float]
    market_overround: Optional[float]
    vig_assumed: bool
    sides: list
    frozen_at: Optional[str] = None
    notes: list = field(default_factory=list)

    @property
    def best(self) -> SideEval:
        chosen = getattr(self, "chosen", None)   # volume mode picks the likelier side
        if chosen:
            return next(s for s in self.sides if s.side == chosen)
        return max(self.sides, key=lambda s: s.gap)


def _opt_int(v):
    v = (v or "").strip()
    return parse_odds(v) if v else None


def _opt_float(v):
    v = (v or "").strip()
    return float(v) if v else None


def parse_books(v: str) -> list:
    """'DraftKings:-115/-105|Caesars:+130/' -> [('DraftKings', [-115, -105]), ('Caesars', [130, None])].
    Three-way: 'Bet365:2.10/3.40/3.60' (subject / draw / opponent; decimal or American)."""
    out = []
    for part in (v or "").split("|"):
        part = part.strip()
        if not part:
            continue
        book, prices = part.rsplit(":", 1)
        out.append((book.strip(), [_opt_int(x) for x in prices.split("/")]))
    return out


def best_price(books: list, idx: int, allowed: Optional[tuple] = None):
    """(odds, book) paying the most on one side; American odds rank the same as payout.
    `allowed` limits the shop to those book names (case/spacing ignored)."""
    ok = None if allowed is None else {book_key(b) for b in allowed}
    priced = [(b[1][idx], b[0]) for b in books if idx < len(b[1]) and b[1][idx] is not None
              and (ok is None or book_key(b[0]) in ok)]
    return max(priced) if priced else (None, "")


def projection_inputs(r: dict) -> ProjectionInputs:
    return ProjectionInputs(
        season_games=int(r.get("season_games") or 0),
        opp_season=_opt_float(r.get("opp_season")), opp_recent=_opt_float(r.get("opp_recent")),
        opp_recent_games=int(r.get("opp_recent_games") or 0),
        opp_override=_opt_float(r.get("opp_override")),
        rate_season=_opt_float(r.get("rate_season")), rate_recent=_opt_float(r.get("rate_recent")),
        rate_recent_games=int(r.get("rate_recent_games") or 0),
        rate_expected=_opt_float(r.get("rate_expected")),
        matchup_factor=_opt_float(r.get("matchup_factor")),
        pace_factor=_opt_float(r.get("pace_factor")),
        venue_factor=_opt_float(r.get("venue_factor")),
        rest_factor=_opt_float(r.get("rest_factor")),
    )


def _bool(v):
    return (v or "").strip().lower() in {"1", "true", "yes", "y"}


def load_pro_markets(path: str) -> list:
    with open(path, newline="") as f:
        return rows_to_markets(csv.DictReader(f))


def _links(v) -> dict:
    if isinstance(v, dict):
        return v
    try:
        return json.loads(v) if v else {}
    except ValueError:
        return {}


def rows_to_markets(rows) -> list:
    out = []
    for r in rows:
        sd = (r.get("stdev") or "").strip()
        mean, proj = _opt_float(r.get("mean")), None
        if mean is None:
            proj = build_projection(projection_inputs(r))
            mean = proj.mean
        books = parse_books(r.get("books", ""))
        best_a, best_a_book = best_price(books, 0)
        best_b, best_b_book = best_price(books, 1)
        out.append(ProMarket(
            date=r["date"].strip(), sport=r["sport"].strip(), game_id=r["game_id"].strip(),
            game=r["game"].strip(), lineup_confirmed=_bool(r.get("lineup_confirmed")),
            bet_type=r["bet_type"].strip().lower(), subject=r["subject"].strip(),
            opponent=r.get("opponent", "").strip(), market=r["market"].strip(),
            line=float(r.get("line") or 0), mean=mean,
            stdev=float(sd) if sd else None, dist=(r.get("dist") or "").strip().lower(),
            mkt_a_odds=_opt_int(r.get("mkt_a_odds")), mkt_b_odds=_opt_int(r.get("mkt_b_odds")),
            best_a_odds=best_a or _opt_int(r.get("best_a_odds")),
            best_a_book=best_a_book if best_a else r.get("best_a_book", "").strip(),
            best_b_odds=best_b or _opt_int(r.get("best_b_odds")),
            best_b_book=best_b_book if best_b else r.get("best_b_book", "").strip(),
            news_fresh=_bool(r.get("news_fresh")),
            injury_status=(r.get("injury_status") or "active").strip().lower(),
            notes=r.get("notes", "").strip(),
            position=r.get("position", "").strip(), projection=proj, books=books,
            mean_b=_opt_float(r.get("mean_b")),
            commence=(r.get("commence") or "").strip(),
            links=_links(r.get("links")),
        ))
    return out


def run_sim(m: ProMarket, cfg: ProConfig) -> SimResult:
    if m.bet_type == "1x2":
        if m.mean_b is None:
            raise ValueError(f"{m.game}: 1x2 needs mean (subject goals) and mean_b (opponent goals)")
        return simulate_1x2(m.mean, m.mean_b, cfg.n_sims, seed_key=m.key)
    dist = m.dist or pick_dist(m.market, m.bet_type)
    sd = m.stdev or default_sd(dist, m.mean, m.market, m.sport, m.bet_type)
    # One simulation per player-stat (or game line); each alt line reads the same draws.
    ladder = f"{m.date}|{m.game_id}|{m.subject}|{m.market}"
    return simulate_ladder(m.mean, m.threshold, dist, sd, cfg.n_sims, seed_key=ladder)


def side_prices(m: ProMarket, allowed: Optional[tuple] = None) -> dict:
    """side -> (best odds, book) among bettable books. Books feed first, then the 2-way
    fallback columns (a blank book name there is assumed bettable)."""
    out = {}
    ok = None if allowed is None else {book_key(b) for b in allowed}
    for i, side in enumerate(m.side_keys):
        odds, book = best_price(m.books, i, allowed)
        if odds is None and not m.books and m.bet_type != "1x2":
            odds, book = ((m.best_a_odds or m.mkt_a_odds, m.best_a_book) if side == "A"
                          else (m.best_b_odds or m.mkt_b_odds, m.best_b_book))
            if ok is not None and book and book_key(book) not in ok:
                odds = None
        if odds is not None:
            out[side] = (odds, book)
    return out


def sharp_weights() -> dict:
    """book_key -> weight in the consensus. LINEMAKER_SHARP_BOOKS, e.g. "Pinnacle:3,LowVig:2":
    sharp books (high limits, low margins) move first and are right more often, so their
    no-vig line counts more. Unlisted books weigh 1."""
    out = {}
    for part in os.environ.get("LINEMAKER_SHARP_BOOKS", "Pinnacle:3").split(","):
        name, _, w = part.partition(":")
        if name.strip():
            try:
                out[book_key(name)] = float(w) if w.strip() else 3.0
            except ValueError:
                continue
    return out


def market_probs(m: ProMarket, cfg: ProConfig):
    """(no-vig probability per side, avg hold, avg overround, vig assumed) or None.
    With a multi-book feed, consensus is the weighted average of each complete book's no-vig
    line (sharp books such as Pinnacle count more; see sharp_weights)."""
    n = len(m.side_keys)
    weights = sharp_weights()
    named = [(b, p[:n]) for b, p in m.books if len(p) >= n and all(x is not None for x in p[:n])]
    complete = [p for _b, p in named]
    book_w = [weights.get(book_key(b), 1.0) for b, _p in named]
    if not complete and m.books and m.bet_type != "1x2":
        # One-sided feed (typical for alternate lines): average the posted side's implied
        # probability and remove an assumed hold.
        for i, side in enumerate(m.side_keys):
            posted = [p[i] for _, p in m.books if i < len(p) and p[i] is not None]
            if posted:
                imp = sum(implied_probability(o) for o in posted) / len(posted)
                p = imp * (1 - cfg.assumed_hold)
                probs = {"A": p, "B": 1 - p} if side == "A" else {"A": 1 - p, "B": p}
                return probs, None, None, True
    if not complete and m.bet_type != "1x2":
        a, b = m.mkt_a_odds or m.best_a_odds, m.mkt_b_odds or m.best_b_odds
        if a is not None and b is not None:
            complete, book_w = [[a, b]], [1.0]
        elif a is not None or b is not None:
            p = implied_probability(a if a is not None else b) * (1 - cfg.assumed_hold)
            pa = p if a is not None else 1 - p
            return {"A": pa, "B": 1 - pa}, None, None, True
    if not complete:
        return None
    fair = [no_vig(*prices, method=cfg.devig_method) for prices in complete]
    total_w = sum(book_w)
    probs = {side: sum(w * f[i] for w, f in zip(book_w, fair)) / total_w
             for i, side in enumerate(m.side_keys)}
    avg_hold = sum(hold(*p) for p in complete) / len(complete)
    avg_over = sum(overround(*p) for p in complete) / len(complete)
    return probs, avg_hold, avg_over, False


def model_probs(m: ProMarket, sim: SimResult) -> tuple:
    """(model probability per side with pushes removed, push probability)."""
    if m.bet_type == "1x2":
        return {"A": sim.p_over, "D": sim.p_push, "B": sim.p_under}, 0.0
    decided = sim.p_over + sim.p_under
    a = sim.p_over / decided if decided else 0.5
    return {"A": a, "B": 1 - a}, sim.p_push


def evaluate(m: ProMarket, cfg: ProConfig, sim: Optional[SimResult] = None) -> Optional[ProEval]:
    sim = sim or run_sim(m, cfg)
    mp = market_probs(m, cfg)
    if mp is None:
        return None
    market, mkt_hold, mkt_over, assumed = mp
    model, push = model_probs(m, sim)
    w = (cfg.model_weight_fresh if m.news_fresh
         else cfg.market_weights.get(f"{m.sport} {m.market}", cfg.model_weight))

    sides = []
    for side, (odds, book) in side_prices(m, cfg.bet_books).items():
        blended = w * model[side] + (1 - w) * market[side]
        imp = implied_probability(odds)
        sides.append(SideEval(
            side=side, label=m.side_label(side), model_prob=model[side],
            market_prob=market[side], blended=blended, best_odds=odds, best_book=book,
            best_implied=imp, gap=blended - imp, model_edge=model[side] - market[side],
            ev=expected_value(blended, odds, 100, push),
            fair=fair_odds(min(max(blended, 0.001), 0.999))))
    if not sides:
        return None
    ev = ProEval(market=m, sim=sim, model_weight=w, market_hold=mkt_hold,
                 market_overround=mkt_over, vig_assumed=assumed, sides=sides)
    if assumed:
        ev.notes.append(f"one side unpriced: assumed {cfg.assumed_hold:.1%} hold to de-vig")
    soft = soft_market_note(m.market, m.position)
    if soft:
        ev.notes.append(soft)
    if len(m.books) > 1:
        shop = "/".join(cfg.bet_books) if cfg.bet_books else "all of them"
        ev.notes.append(f"consensus of {len(m.books)} books; best price shopped at {shop}")
    if not m.lineup_confirmed:
        ev.notes.append("lineups/inactives not confirmed: projection is live, not final")
    return ev


def _window_ok(odds: int, cfg: ProConfig) -> bool:
    if cfg.min_odds is not None and odds > cfg.min_odds:
        return False
    if cfg.max_odds is not None and odds < cfg.max_odds:
        return False
    return True


@dataclass
class ProResult:
    slate_date: str
    picks: list = field(default_factory=list)      # ProEvals, ranked by best side's gap
    passes: list = field(default_factory=list)     # (label, reason)
    correlated: dict = field(default_factory=dict)
    evaluated: list = field(default_factory=list)  # every ProEval priced, picks or not
    slate: list = field(default_factory=list)      # [(game label, commence)] on the date
    source_warning: Optional[str] = None           # e.g. projections look like sample data
    projection_source: str = ""                    # where the projections came from

    @property
    def games(self) -> int:
        return len({ev.market.game_id for ev in self.evaluated})


def book_prices(m: ProMarket, side: str) -> dict:
    """book name -> odds for one side of a market, from the multi-book feed."""
    idx = m.side_keys.index(side)
    return {book: prices[idx] for book, prices in m.books
            if idx < len(prices) and prices[idx] is not None}


def run_pro(markets: list, slate_date: str, cfg: ProConfig, freezer=None,
            tracker=None) -> ProResult:
    res = ProResult(slate_date=slate_date)
    if tracker is None and cfg.track:
        from .tracking import Tracker
        tracker = Tracker(os.environ.get("LINEMAKER_TRACKING_DIR", "tracking"), slate_date)
    quals = []
    # Ladder lines next to each other, so they reuse one simulation (see simulate_ladder).
    for m in sorted(markets, key=lambda m: (m.game_id, m.subject, m.market)):
        label = m.side_label("A")
        if m.date != slate_date:
            res.passes.append((label, f"not on today's slate ({m.date})"))
            continue
        if m.injury_status in INJURY_EXCLUDE:
            res.passes.append((label, f"injury status: {m.injury_status}"))
            continue
        if cfg.confirmed_only and not m.lineup_confirmed:
            res.passes.append((label, "lineups/inactives not yet confirmed"))
            continue
        sim, frozen_at = None, None
        if freezer is not None:
            sim, frozen_at, changed = freezer.projection(m, cfg)
            if changed:
                res.passes.append((label, "inputs changed after freeze — frozen projection kept"))
        ev = evaluate(m, cfg, sim)
        if ev is None:
            where = f" at {'/'.join(cfg.bet_books)}" if cfg.bet_books else ""
            res.passes.append((label, f"no bettable price{where}"))
            continue
        ev.frozen_at = frozen_at
        res.evaluated.append(ev)
        if freezer is not None:
            freezer.record(ev)
        if tracker is not None:
            tracker.record(ev)
        if cfg.rank_by == "prob":
            in_window = [s for s in ev.sides if _window_ok(s.best_odds, cfg)]
            if in_window:
                ev.chosen = max(in_window, key=lambda s: s.blended).side
        best = ev.best
        if cfg.require_model_edge and best.model_edge <= 0:
            res.passes.append((best.label, f"market caught up: model {best.model_prob:.1%} vs "
                                           f"no-vig {best.market_prob:.1%}"))
            continue
        if best.gap <= cfg.min_gap:
            res.passes.append((best.label, f"no gap: blended {best.blended:.1%} vs best price "
                                           f"{fmt_odds(best.best_odds)} ({best.best_implied:.1%})"))
            continue
        if cfg.max_gap is not None and best.gap > cfg.max_gap:
            res.passes.append((best.label, f"gap {best.gap:+.1%} too large to trust: check injuries "
                                           f"and the line before betting"))
            continue
        if cfg.min_ev is not None and best.ev / 100 < cfg.min_ev:
            res.passes.append((best.label, f"EV {best.ev / 100:+.1%} per dollar below "
                                           f"{cfg.min_ev:.0%} floor"))
            continue
        if not _window_ok(best.best_odds, cfg):
            res.passes.append((best.label, f"odds {fmt_odds(best.best_odds)} outside window"))
            continue
        if cfg.min_prob is not None and best.blended < cfg.min_prob:
            res.passes.append((best.label, f"blended {best.blended:.1%} below {cfg.min_prob:.0%} floor"))
            continue
        quals.append(ev)

    quals.sort(key=(lambda e: e.best.blended) if cfg.rank_by == "prob" else (lambda e: e.best.gap),
               reverse=True)
    if cfg.one_per_stat:
        seen, kept = {}, []
        for ev in quals:   # best-ranked line of each ladder first; the rest are near-duplicates
            k = (ev.market.game_id, ev.market.subject, ev.market.market)
            if k in seen:
                res.passes.append((ev.best.label, f"similar line already on the board ({seen[k]})"))
                continue
            seen[k] = ev.best.label
            kept.append(ev)
        quals = kept
    for ev in quals[cfg.max_picks:]:
        res.passes.append((ev.best.label, f"qualified (gap {ev.best.gap:+.1%}) but below cutoff"))
    res.picks = quals[:cfg.max_picks]
    if freezer is not None:
        freezer.mark_picks(res.picks)
        freezer.save()
    if tracker is not None:
        tracker.mark_picks(res.picks)
        tracker.save()
    games = {}
    for ev in res.picks:
        games.setdefault(ev.market.game_id, []).append(ev.best.label)
    res.correlated = {g: v for g, v in games.items() if len(v) > 1}
    return res
