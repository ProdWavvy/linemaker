"""Remove the book's margin from a market before comparing against it.

proportional: each implied probability / total implied (the standard, transparent default).
shin:         Shin (1993) model, which assigns more of the margin to longshots and corrects
              for favorite-longshot bias; often more accurate on 3-way and lopsided markets.
"""
import math

from .odds import implied_probability


def overround(*odds: int) -> float:
    """Total implied probability minus 1 (e.g. -110/-110 -> 0.0476)."""
    return sum(implied_probability(o) for o in odds) - 1


def hold(*odds: int) -> float:
    """Margin as a share of the total handle (e.g. -115/-105 -> 0.045)."""
    return 1 - 1 / sum(implied_probability(o) for o in odds)


def _shin(implied: list) -> list:
    total = sum(implied)
    if total <= 1:
        return [p / total for p in implied]

    def probs(z):
        return [(math.sqrt(z * z + 4 * (1 - z) * q * q / total) - z) / (2 * (1 - z))
                for q in implied]

    lo, hi = 0.0, 0.999
    for _ in range(100):  # sum(probs) falls as z rises; bisect for sum == 1
        mid = (lo + hi) / 2
        if sum(probs(mid)) > 1:
            lo = mid
        else:
            hi = mid
    p = probs((lo + hi) / 2)
    s = sum(p)
    return [x / s for x in p]


def no_vig(*odds: int, method: str = "proportional") -> list:
    implied = [implied_probability(o) for o in odds]
    if method == "shin":
        return _shin(implied)
    if method != "proportional":
        raise ValueError(f"unknown de-vig method: {method}")
    total = sum(implied)
    return [p / total for p in implied]
