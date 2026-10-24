"""Monte Carlo outcome distributions: turn a projected mean into P(over line)."""
import math
import random
import statistics
import zlib
from dataclasses import dataclass

# Default distribution family per market (matched by substring, first hit wins).
#   poisson  - low-count, roughly equidispersed: goals, SOG, TDs, HRs
#   negbin   - overdispersed counts: points, rebounds, assists, receptions, Ks
#   gamma    - continuous, non-negative, right-skewed: yardage
#   normal   - margins and game totals
MARKET_DIST = [
    ("spread", "normal"), ("moneyline", "normal"), ("game total", "normal"),
    ("anytime td", "poisson"), ("touchdown", "poisson"), ("goals", "poisson"),
    ("shots on goal", "poisson"), ("home run", "poisson"), ("hits", "poisson"),
    ("yards", "gamma"), ("longest", "gamma"),
    ("strikeouts", "negbin"), ("points", "negbin"), ("rebounds", "negbin"),
    ("assists", "negbin"), ("pra", "negbin"), ("receptions", "negbin"),
    ("threes", "negbin"), ("saves", "negbin"), ("total bases", "negbin"),
    ("tds", "poisson"), ("completions", "negbin"), ("attempts", "negbin"),
]

# Default spread of outcomes when the input gives no stdev.
DEFAULT_VAR_RATIO = 1.35          # negbin variance / mean
DEFAULT_GAMMA_CV = {"passing": 0.28, "rushing": 0.50, "receiving": 0.60, "longest": 0.55}
DEFAULT_NORMAL_SD = {             # (sport, kind) -> sd
    ("NFL", "margin"): 13.5, ("NCAAF", "margin"): 16.0, ("NBA", "margin"): 12.0,
    ("WNBA", "margin"): 11.0, ("NHL", "margin"): 2.3, ("NFL", "total"): 13.0,
    ("NCAAF", "total"): 16.0, ("NBA", "total"): 18.0, ("WNBA", "total"): 15.0,
    ("NHL", "total"): 2.2,
}


@dataclass
class SimResult:
    dist: str
    mean: float
    sd: float
    median: float
    p_over: float      # P(outcome > line)
    p_push: float      # P(outcome == line); 0 on half-point lines
    p_under: float
    n: int
    mean_b: float | None = None   # 3-way: opponent's projected goals (p_push = draw)


def pick_dist(market: str, bet_type: str) -> str:
    key = f"{bet_type} {market}".lower()
    for needle, dist in MARKET_DIST:
        if needle in key:
            return dist
    return "normal"


def default_sd(dist: str, mean: float, market: str, sport: str, bet_type: str) -> float:
    if dist == "poisson":
        return math.sqrt(mean)
    if dist == "negbin":
        return math.sqrt(mean * DEFAULT_VAR_RATIO)
    if dist == "gamma":
        cv = next((v for k, v in DEFAULT_GAMMA_CV.items() if k in market.lower()), 0.5)
        return mean * cv
    kind = "margin" if bet_type in ("spread", "moneyline") else "total"
    return DEFAULT_NORMAL_SD.get((sport.upper(), kind), 12.0)


def _poisson(rng: random.Random, lam: float) -> int:
    if lam <= 0:
        return 0
    if lam > 40:  # normal approximation; Knuth gets slow and the shapes converge
        return max(0, round(rng.gauss(lam, math.sqrt(lam))))
    limit, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1


# UFC total rounds ("ufcrounds"): mean = chance the fight ends early, sd = scheduled rounds.
# An early finish lands at a time drawn from a front-loaded curve (hazard FINISH_HAZARD per
# round: about 45% of finishes in round 1, 32% in round 2, 23% in round 3 of a 3-rounder);
# otherwise the fight goes the distance and the outcome is the full scheduled rounds.
FINISH_HAZARD = 0.35


def _finish_time(u: float, rounds: float) -> float:
    """Inverse of the exponential curve truncated to the scheduled rounds."""
    return -math.log(1 - u * (1 - math.exp(-FINISH_HAZARD * rounds))) / FINISH_HAZARD


def _draw(rng: random.Random, dist: str, mean: float, sd: float) -> float:
    if dist == "ufcrounds":
        return _finish_time(rng.random(), sd) if rng.random() < mean else sd
    if dist == "poisson":
        return _poisson(rng, mean)
    if dist == "negbin":
        var = sd * sd
        if var <= mean:  # not overdispersed: fall back to Poisson
            return _poisson(rng, mean)
        shape = mean * mean / (var - mean)
        return _poisson(rng, rng.gammavariate(shape, mean / shape))
    if dist == "gamma":
        shape = (mean / sd) ** 2
        return rng.gammavariate(shape, mean / shape)
    return rng.gauss(mean, sd)


try:
    import numpy as np
except ImportError:   # pure-Python fallback below (about 100x slower)
    np = None


def _np_draws(rng, dist: str, mean: float, sd: float, n: int):
    if dist == "ufcrounds":
        early = rng.random(n) < mean
        t = -np.log(1 - rng.random(n) * (1 - math.exp(-FINISH_HAZARD * sd))) / FINISH_HAZARD
        return np.where(early, t, sd)
    if dist == "poisson":
        return rng.poisson(max(mean, 0.0), n)
    if dist == "negbin":
        var = sd * sd
        if var <= mean:
            return rng.poisson(max(mean, 0.0), n)
        shape = mean * mean / (var - mean)
        return rng.poisson(rng.gamma(shape, mean / shape, n))
    if dist == "gamma":
        shape = (mean / sd) ** 2
        return rng.gamma(shape, mean / shape, n)
    return rng.normal(mean, sd, n)


def simulate(mean: float, line: float, dist: str, sd: float, n: int = 20000,
             seed_key: str = "") -> SimResult:
    """Simulate n outcomes. Seeded from seed_key so reruns give identical numbers."""
    if np is not None:
        draws = _np_draws(np.random.default_rng(zlib.crc32(seed_key.encode())), dist, mean, sd, n)
        over, push = int((draws > line).sum()), int((draws == line).sum())
        return SimResult(dist=dist, mean=mean, sd=sd, median=float(np.median(draws)),
                         p_over=over / n, p_push=push / n, p_under=(n - over - push) / n, n=n)
    rng = random.Random(zlib.crc32(seed_key.encode()))
    draws = [_draw(rng, dist, mean, sd) for _ in range(n)]
    over = sum(1 for x in draws if x > line)
    push = sum(1 for x in draws if x == line)
    return SimResult(dist=dist, mean=mean, sd=sd, median=statistics.median(draws),
                     p_over=over / n, p_push=push / n, p_under=(n - over - push) / n, n=n)


class DrawCache:
    """Sorted draws per projection, so every line of an alt ladder (same player, stat and
    projection) is read off one simulation instead of re-simulating each line."""

    def __init__(self, size: int = 64):
        self.size, self.store = size, {}

    def get(self, key, make):
        hit = self.store.get(key)
        if hit is None:
            hit = make()
            if len(self.store) >= self.size:
                self.store.pop(next(iter(self.store)))
            self.store[key] = hit
        return hit


_DRAWS = DrawCache()


def simulate_ladder(mean: float, line: float, dist: str, sd: float, n: int = 20000,
                    seed_key: str = "") -> SimResult:
    """Same as simulate(), but the draws are shared by every line with the same seed_key and
    projection (one simulation per player-stat, then a binary search per line)."""
    if np is None:
        return simulate(mean, line, dist, sd, n, seed_key)
    key = (seed_key, dist, round(mean, 6), round(sd, 6), n)

    def make():
        draws = np.sort(_np_draws(np.random.default_rng(zlib.crc32(seed_key.encode())), dist, mean, sd, n))
        return draws, float(np.median(draws))
    draws, median = _DRAWS.get(key, make)
    below_or_at = int(np.searchsorted(draws, line, side="right"))
    below = int(np.searchsorted(draws, line, side="left"))
    over, push = n - below_or_at, below_or_at - below
    return SimResult(dist=dist, mean=mean, sd=sd, median=median, p_over=over / n,
                     p_push=push / n, p_under=(n - over - push) / n, n=n)


def simulate_1x2(mean_a: float, mean_b: float, n: int = 20000, seed_key: str = "") -> SimResult:
    """Independent Poisson goals per side -> P(A win), P(draw), P(B win)."""
    if np is not None:
        g = np.random.default_rng(zlib.crc32(seed_key.encode()))
        d = g.poisson(mean_a, n) - g.poisson(mean_b, n)
        a, draw = int((d > 0).sum()), int((d == 0).sum())
        return SimResult(dist="poisson 1x2", mean=mean_a, sd=math.sqrt(mean_a + mean_b),
                         median=float(np.median(d)), p_over=a / n, p_push=draw / n,
                         p_under=(n - a - draw) / n, n=n, mean_b=mean_b)
    rng = random.Random(zlib.crc32(seed_key.encode()))
    diffs = [_poisson(rng, mean_a) - _poisson(rng, mean_b) for _ in range(n)]
    a = sum(1 for d in diffs if d > 0)
    draw = sum(1 for d in diffs if d == 0)
    return SimResult(dist="poisson 1x2", mean=mean_a, sd=math.sqrt(mean_a + mean_b),
                     median=statistics.median(diffs), p_over=a / n, p_push=draw / n,
                     p_under=(n - a - draw) / n, n=n, mean_b=mean_b)
