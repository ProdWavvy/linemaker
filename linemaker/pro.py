"""Pro engine: simulate -> de-vig -> blend with market -> bet the gap at the best price.

Each CSV row is one market. Two-way: side A is the Over (props/totals) or the subject team
(spread/moneyline), side B the Under or the opponent. Three-way (bet_type 1x2, soccer):
A = subject win, D = draw, B = opponent win.
"""
import csv
import functools
import json
import os
from dataclasses import dataclass, field, replace
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


def _segment_weights() -> dict:
    from .autotune import segment_weights
    return {k: dict(v) for k, v in segment_weights().items()}


# Per-sport versions of the gates: LINEMAKER_<SPORT>_<NAME> (e.g. LINEMAKER_NBA_MIN_GAP=0.04,
# LINEMAKER_UFC_ODDS_MIN=+200) win over the plain LINEMAKER_<NAME> for that sport only.
SPORT_SETTINGS = {"MIN_GAP": ("min_gap", float), "MAX_GAP": ("max_gap", float),
                  "MIN_EV": ("min_ev", float), "ODDS_MIN": ("min_odds", int),
                  "ODDS_MAX": ("max_odds", int), "MIN_PROB": ("min_prob", float),
                  "OVER_MIN_GAP": ("over_min_gap", float), "MAX_PICKS": ("max_picks", int)}


def sport_overrides(sport: str) -> dict:
    """{field: value} from LINEMAKER_<SPORT>_* settings ("off" = no limit)."""
    out = {}
    for suffix, (field_name, kind) in SPORT_SETTINGS.items():
        raw = os.environ.get(f"LINEMAKER_{sport.upper()}_{suffix}", "").strip()
        if not raw:
            continue
        if raw.lower() in ("off", "none", "no"):
            out[field_name] = None if field_name != "min_gap" else 0.0
            continue
        try:
            out[field_name] = kind(float(raw.replace("+", ""))) if kind is int else kind(raw)
        except ValueError:
            continue
    return out


def sport_cfg(cfg, sport: str, cache: dict):
    """cfg with this sport's own settings applied (volume / PrizePicks keep theirs)."""
    if not cfg.sport_settings:
        return cfg
    if sport not in cache:
        o = sport_overrides(sport)
        cache[sport] = replace(cfg, **o) if o else cfg
    return cache[sport]


START_WEIGHTS = {"UFC": 0.35}   # rough first-version models start leaning on the market


def _move_limit() -> Optional[float]:
    raw = os.environ.get("LINEMAKER_MAX_MOVE_AGAINST", "0.04").strip().lower()
    if raw in ("", "off", "no", "false"):
        return None
    try:
        return float(raw)
    except ValueError:
        return 0.04


def _start_weight(sport: str, default: float) -> float:
    """A sport's weight before the auto-tuner has enough of it: LINEMAKER_<SPORT>_MODEL_WEIGHT
    if set, else the built-in start (UFC 0.35), else the overall weight."""
    raw = os.environ.get(f"LINEMAKER_{sport.upper()}_MODEL_WEIGHT", "").strip()
    try:
        return float(raw) if raw else START_WEIGHTS.get(sport.upper(), default)
    except ValueError:
        return START_WEIGHTS.get(sport.upper(), default)


def model_weight_for(sport: str, market: str, cfg, bet_type: str = "prop") -> float:
    """The scan's own segment only (pre-game or live x props or game lines; see autotune):
    its market weight ("NBA points"), else its league weight ("NBA"), else the segment's base
    weight (pre-game: the league's start weight first, e.g. UFC 0.35), else cfg.model_weight.
    Each tuned level was already blended with the level above it by sample size, so a thin
    one never strays far."""
    from .tracking import kind_of
    seg = cfg.segment_weights.get(f"{cfg.phase} {kind_of(bet_type)}") or {}
    w = seg.get("markets", {}).get(f"{sport} {market}")
    if w is None:
        w = seg.get("sports", {}).get(sport)
    if w is not None:
        return w
    base = seg.get("weight", cfg.model_weight)
    return _start_weight(sport, base) if cfg.phase == "pre" else base


def _over_min_gap() -> Optional[float]:
    raw = os.environ.get("LINEMAKER_OVER_MIN_GAP", "0.04").strip().lower()
    if raw in ("", "off", "no", "false"):
        return None
    try:
        return float(raw)
    except ValueError:
        return 0.04


def _default_bet_books() -> Optional[tuple]:
    """Books you can actually bet at. LINEMAKER_BET_BOOKS="all" allows every book."""
    raw = os.environ.get("LINEMAKER_BET_BOOKS", "FanDuel,DraftKings")
    if raw.strip().lower() in ("", "all", "*"):
        return None
    return tuple(b.strip() for b in raw.split(",") if b.strip())


@functools.lru_cache(maxsize=512)
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
    # Tuned weights per segment (pre/live x props/games), league and market (see autotune).
    segment_weights: dict = field(default_factory=lambda: _segment_weights())
    sport_settings: bool = True        # apply LINEMAKER_<SPORT>_* gates (off for volume / PrizePicks)
    force_model_weight: Optional[float] = None   # market-edge mode: 0 = the sharp consensus alone
    min_books: int = 0                 # complete two-sided books needed in the consensus
    pick_tag: str = "model"            # what a pick is logged as in tracking ("model" / "market")
    calibrate: bool = False            # use calibrated hit chances (volume, PrizePicks: see autotune)
    phase: str = "pre"                 # pre-game or live: separate weights, calibration, tracking
    max_parlay_legs: Optional[int] = None   # cap on auto-parlay size (live parlays: 6)
    parlay_min_ev: Optional[float] = None   # parlays past 2 legs shown only at this EV or better
    game_parlay_types: tuple = ()      # game-line parlay legs (ml_parlay_config): bet types used
    one_per_player: bool = False       # at most one leg per player (volume: his legs move together)
    # Log every priced market (not just picks) to tracking/<date>.json for calibration.
    track: bool = field(default_factory=lambda: os.environ.get(
        "LINEMAKER_TRACKING", "on").lower() not in ("off", "0", "false", "no"))
    devig_method: str = "proportional" # or "shin" (corrects favorite-longshot bias)
    assumed_hold: float = 0.045        # used only when one side of a 2-way market is missing
    # blended prob minus best-price implied must exceed min_gap; a gap above max_gap against
    # a sharp market is more often stale data (missed injury, moved line) than a real edge.
    min_gap: float = field(default_factory=lambda: _env_num("LINEMAKER_MIN_GAP", float) or 0.0)
    max_gap: Optional[float] = field(default_factory=lambda: _env_num("LINEMAKER_MAX_GAP", float))
    # A model pick is dropped when the sharp consensus has moved this far against its side since
    # the first read of the day (someone knows something: injury, lineup, weather). Blank = off.
    max_move_against: Optional[float] = field(default_factory=lambda: _move_limit())
    min_ev: Optional[float] = field(default_factory=lambda: _env_num("LINEMAKER_MIN_EV", float))
    # Prop overs need a bigger gap: graded results showed the model's over leans run hot
    # (overs it liked by a little hit at the market's rate, not the model's). 0 / off = same
    # bar as unders.
    over_min_gap: Optional[float] = field(default_factory=lambda: _over_min_gap())
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


def market_config(**overrides) -> "ProConfig":
    """Market-edge mode: no model at all. The fair price is the 10-book no-vig consensus
    (Pinnacle and other sharp books weighted up); a pick is a FanDuel/DraftKings price that
    still beats it by LINEMAKER_MARKET_MIN_GAP (default 4 points: in the 10/22-10/26 grades,
    2-4 point gaps lost 9% while 4+ point gaps won) with at least
    LINEMAKER_MARKET_MIN_BOOKS (default 4) complete books behind the consensus. Gaps over 10
    points are left out (usually a stale line about to move)."""
    cfg = ProConfig(
        force_model_weight=0.0, require_model_edge=False, over_min_gap=None, sport_settings=False,
        min_gap=_env_num("LINEMAKER_MARKET_MIN_GAP", float) or 0.04, max_gap=0.10, min_ev=None,
        min_odds=_env_num("LINEMAKER_MARKET_ODDS_MIN", int) or 300,
        max_odds=_env_num("LINEMAKER_MARKET_ODDS_MAX", int) or -400, min_prob=None,
        min_books=_env_num("LINEMAKER_MARKET_MIN_BOOKS", int) or 4, pick_tag="market")
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def apply_calibration(ev):
    """Replace each side's hit chance with the calibrated one (autotune.tune_calibration) and
    re-derive gap, EV and fair odds. Two-way markets only; tracking keeps the raw numbers."""
    from .autotune import calibrated
    if len(ev.market.side_keys) != 2:
        return
    push = getattr(ev.sim, "p_push", 0.0) or 0.0
    ev.calibrated = True
    for s in ev.sides:
        p_a = s.blended if s.side == "A" else 1 - s.blended
        p_a = calibrated(p_a, ev.market.sport, ev.market.bet_type, getattr(ev, "phase", "pre"))
        s.blended = p_a if s.side == "A" else 1 - p_a
        s.gap = s.blended - s.best_implied
        s.ev = expected_value(s.blended, s.best_odds, 100, push)
        s.fair = fair_odds(min(max(s.blended, 0.001), 0.999))


def volume_score(ev) -> float:
    """Volume ranking: hit chance, nudged by price. A leg priced 5% better than fair moves up
    as if it were 2.5 points likelier; 5% worse, down 2.5. Between two lines of the same
    ladder this prefers the one that pays for its risk."""
    from .odds import american_to_decimal
    s = ev.best
    return s.blended + 0.5 * (s.blended * american_to_decimal(s.best_odds) - 1)


def consensus_books(m: "ProMarket") -> int:
    """Books with both sides priced (what the no-vig consensus is built from)."""
    n = len(m.side_keys)
    return sum(1 for _b, p in m.books if len(p) >= n and all(x is not None for x in p[:n]))


def ml_parlay_config(types=("moneyline",), **overrides) -> "ProConfig":
    """Game-line parlays ("nfl ml parlay", "ml and spread parlay"): per game, the likelier
    moneyline winner and/or the best-value spread side. A favourite's price carries the
    book's margin (3-4 points at -300), so a leg may sit up to LINEMAKER_ML_PARLAY_MAX_VIG
    (5 points) under fair; the volume gap gate (2 points) would turn away nearly every one.
    Legs are logged as parlay legs (volume_pick), never as model picks."""
    cfg = volume_config(
        min_prob=_env_num("LINEMAKER_ML_PARLAY_MIN_PROB", float) or 0.60,
        min_odds=_env_num("LINEMAKER_ML_PARLAY_ODDS_MIN", int) or -130,
        max_odds=_env_num("LINEMAKER_ML_PARLAY_ODDS_MAX", int) or -1000,
        min_gap=-(_env_num("LINEMAKER_ML_PARLAY_MAX_VIG", float) or 0.05))
    cfg.game_parlay_types = tuple(types)
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def _leg_ok(s, cfg) -> bool:
    from .odds import implied_probability
    return (s.best_odds is not None and s.blended >= cfg.min_prob
            and (effective_max_odds(cfg) or cfg.max_odds) <= s.best_odds <= cfg.min_odds
            and s.blended - implied_probability(s.best_odds) >= cfg.min_gap)


def ml_parlay_legs(res: "ProResult", cfg: "ProConfig") -> list:
    """Per game: the likelier moneyline side, and (with spreads asked for) the spread side,
    main or alternate, that pays best for its hit chance, each only if it clears the gates.
    Likeliest first."""
    import copy
    from .odds import american_to_decimal
    types = getattr(cfg, "game_parlay_types", None) or ("moneyline",)
    best = {}
    for ev in res.evaluated:
        m = ev.market
        if m.bet_type not in types or len(ev.sides) != 2:
            continue
        sides = [s for s in ev.sides if _leg_ok(s, cfg)]
        if m.bet_type == "moneyline":
            s = max(ev.sides, key=lambda x: x.blended)   # the likelier winner, or nothing
            sides = [s] if s in sides else []
        for s in sides:
            value = s.blended * american_to_decimal(s.best_odds)
            score = s.blended if m.bet_type == "moneyline" else value
            k = (m.game_id, m.bet_type)
            if k not in best or score > best[k][0]:
                best[k] = (score, ev, s.side)
    out = []
    for _score, ev, side in best.values():
        leg = copy.copy(ev)
        leg.chosen = side
        out.append(leg)
    return sorted(out, key=lambda e: -e.best.blended)


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
        require_model_edge=False, rank_by="prob", over_min_gap=None, sport_settings=False,
        pick_tag="volume",   # parlay legs: logged apart from the model's own picks
        calibrate=True, one_per_player=True)
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
    multiplier: Optional[float] = None         # pick'em payout multiplier (goblin < 1 < demon)
    mean_bias: float = 1.0                     # projection-bias correction already in `mean`

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
        if self.bet_type == "team_total":
            return f"{who} team total {word} {self.line:g}"
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
            multiplier=_opt_float(r.get("pp_mult")),
            mean_bias=_opt_float(r.get("mean_bias")) or 1.0,
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
    # Pick'em sites post flat indicative prices, not a market: they never move the consensus.
    for dfs in ("prizepicks", "underdog", "pick6", "dabble"):
        out.setdefault(dfs, 0.0)
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
    if total_w <= 0:   # only pick'em prices: fall back to them, evenly
        book_w, total_w = [1.0] * len(fair), float(len(fair))
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
    w = (cfg.force_model_weight if cfg.force_model_weight is not None
         else cfg.model_weight_fresh if m.news_fresh and cfg.phase == "pre"
         else model_weight_for(m.sport, m.market, cfg, m.bet_type))

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


def heaviest_allowed() -> Optional[int]:
    """LINEMAKER_MAX_FAVORITE (e.g. -250): no leg or pick priced heavier, in any mode."""
    return _env_num("LINEMAKER_MAX_FAVORITE", int)


def effective_max_odds(cfg) -> Optional[int]:
    cap = heaviest_allowed()
    if cap is None:
        return cfg.max_odds
    return cap if cfg.max_odds is None else max(cfg.max_odds, cap)


def _window_ok(odds: int, cfg: ProConfig) -> bool:
    if cfg.min_odds is not None and odds > cfg.min_odds:
        return False
    top = effective_max_odds(cfg)
    if top is not None and odds < top:
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
            tracker=None, sims: Optional[dict] = None) -> ProResult:
    """sims: {market key: SimResult} already run on these same projections (a second pass
    over one slate reuses them instead of simulating every line again)."""
    res = ProResult(slate_date=slate_date)
    if tracker is None and cfg.track:
        from .tracking import Tracker
        tracker = Tracker(os.environ.get("LINEMAKER_TRACKING_DIR", "tracking"), slate_date)
    quals, per_sport = [], {}
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
        if sim is None and sims:
            sim = sims.get(m.key)
        ev = evaluate(m, cfg, sim)
        if ev is not None:
            ev.phase = cfg.phase       # pre / live: picks where it's tracked, weighed, calibrated
        if ev is None:
            where = f" at {'/'.join(cfg.bet_books)}" if cfg.bet_books else ""
            res.passes.append((label, f"no bettable price{where}"))
            if tracker is not None and cfg.bet_books:
                # Not bettable at your books, but still a model-vs-market data point for
                # calibration: price it against every book and log it, marked unbettable.
                other = evaluate(m, replace(cfg, bet_books=None), sim)
                if other is not None:
                    tracker.record(other, keep_model=cfg.force_model_weight == 0)
                    tracker.records[m.key]["bettable"] = False
            continue
        if cfg.min_books and (ev.vig_assumed or consensus_books(m) < cfg.min_books):
            res.passes.append((ev.best.label, f"only {consensus_books(m)} books with both sides "
                                              f"(market edges need {cfg.min_books})"))
            if tracker is not None:
                tracker.record(ev, keep_model=cfg.force_model_weight == 0)
            continue
        ev.frozen_at = frozen_at
        res.evaluated.append(ev)
        if freezer is not None:
            freezer.record(ev)
        if tracker is not None:
            tracker.record(ev, keep_model=cfg.force_model_weight == 0)
        if cfg.calibrate:   # after tracking: the record keeps the raw hit chances for the next fit
            apply_calibration(ev)
        c = sport_cfg(cfg, m.sport, per_sport)   # this sport's own gates, if any
        if cfg.pick_tag == "model" and cfg.sport_settings:
            from .autotune import pruned
            why = pruned(m.sport, m.market, cfg.phase, m.bet_type)
            if why:
                res.passes.append((ev.best.label, f"market paused, it keeps losing ({why})"))
                continue
        if c.rank_by == "prob":
            in_window = [s for s in ev.sides if _window_ok(s.best_odds, c)]
            if in_window:
                ev.chosen = max(in_window, key=lambda s: s.blended).side
        best = ev.best
        if c.require_model_edge and best.model_edge <= 0:
            res.passes.append((best.label, f"market caught up: model {best.model_prob:.1%} vs "
                                           f"no-vig {best.market_prob:.1%}"))
            continue
        if best.gap <= c.min_gap:
            res.passes.append((best.label, f"no gap: blended {best.blended:.1%} vs best price "
                                           f"{fmt_odds(best.best_odds)} ({best.best_implied:.1%})"))
            continue
        if (c.over_min_gap and m.bet_type == "prop" and best.side == "A"
                and best.gap <= c.over_min_gap):
            res.passes.append((best.label, f"over with a {best.gap:+.1%} gap: overs need more than "
                                           f"{c.over_min_gap:.0%}"))
            continue
        if c.max_gap is not None and best.gap > c.max_gap:
            res.passes.append((best.label, f"gap {best.gap:+.1%} too large to trust: check injuries "
                                           f"and the line before betting"))
            continue
        if (c.max_move_against and cfg.pick_tag in ("model", "volume") and tracker is not None
                and m.key in tracker.records and len(m.side_keys) == 2):
            from .tracking import market_move
            move = market_move(tracker.records[m.key], best.side)
            if move is not None and move < -c.max_move_against:
                res.passes.append((best.label, f"sharp market moved {move * 100:+.1f} pts against this "
                                               "side today (likely news the model hasn't seen)"))
                continue
        if c.min_ev is not None and best.ev / 100 < c.min_ev:
            res.passes.append((best.label, f"EV {best.ev / 100:+.1%} per dollar below "
                                           f"{c.min_ev:.0%} floor"))
            continue
        if not _window_ok(best.best_odds, c):
            res.passes.append((best.label, f"odds {fmt_odds(best.best_odds)} outside window"))
            continue
        if c.min_prob is not None and best.blended < c.min_prob:
            res.passes.append((best.label, f"blended {best.blended:.1%} below {c.min_prob:.0%} floor"))
            continue
        quals.append(ev)

    quals.sort(key=(volume_score if cfg.rank_by == "prob" else (lambda e: e.best.gap)), reverse=True)
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
    # A player's props rise and fall together (more minutes than projected sinks every under),
    # so a few per player: 10/27 had 70 of 102 picks on 16 players, one of them 0-9.
    per_player = 1 if cfg.one_per_player else (_env_num("LINEMAKER_MAX_PICKS_PER_PLAYER", int) or 2)
    seen, kept = {}, []
    for ev in quals:   # best-ranked first
        k = (ev.market.game_id, ev.market.subject) if ev.market.bet_type == "prop" else id(ev)
        if len(seen.setdefault(k, [])) >= per_player:
            res.passes.append((ev.best.label, f"same player already on the board ({seen[k][0]})"))
            continue
        seen[k].append(ev.best.label)
        kept.append(ev)
    quals = kept
    sports = {ev.market.sport for ev in quals}
    limit = sport_cfg(cfg, sports.pop(), per_sport).max_picks if len(sports) == 1 else cfg.max_picks
    for ev in quals[limit:]:
        res.passes.append((ev.best.label, f"qualified (gap {ev.best.gap:+.1%}) but below cutoff"))
    res.picks = quals[:limit]
    if cfg.game_parlay_types:   # moneyline / spread parlays: their own leg picker (see ml_parlay_legs)
        res.picks = ml_parlay_legs(res, cfg)[:cfg.max_picks]
        res.ml_only, res.game_types = True, cfg.game_parlay_types
    if freezer is not None:
        freezer.mark_picks(res.picks)
        freezer.save()
    if tracker is not None:
        tracker.mark_picks(res.picks, cfg.pick_tag, background=getattr(cfg, "background", False))
        tracker.save()
    games = {}
    for ev in res.picks:
        games.setdefault(ev.market.game_id, []).append(ev.best.label)
    res.correlated = {g: v for g, v in games.items() if len(v) > 1}
    return res
