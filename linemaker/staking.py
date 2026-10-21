"""Stake sizing: fractional Kelly.

  full Kelly  f = (b * p - (1 - p)) / b      b = decimal odds - 1, p = our hit chance
  suggested   LINEMAKER_KELLY (default 0.25, "quarter Kelly") x f, capped at
              LINEMAKER_MAX_STAKE_PCT (default 2%) of bankroll

Quarter Kelly gives up a little growth for much smaller swings, and it forgives an estimate
that's a few points too confident. With LINEMAKER_BANKROLL set, cards show dollars too.
"""
import os
from typing import Optional

from .odds import american_to_decimal


def _env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def bankroll() -> Optional[float]:
    v = _env("LINEMAKER_BANKROLL", 0.0)
    return v if v > 0 else None


def kelly_fraction(p: float, american: int) -> float:
    b = american_to_decimal(american) - 1
    return (b * p - (1 - p)) / b if b > 0 else 0.0


def suggested(p: float, american: int) -> float:
    """Share of bankroll to stake (0 when there's no edge at this price)."""
    f = kelly_fraction(p, american) * _env("LINEMAKER_KELLY", 0.25)
    return max(0.0, min(f, _env("LINEMAKER_MAX_STAKE_PCT", 2.0) / 100))


def stake_line(p: float, american: int, parlay: bool = False) -> str:
    share = suggested(p, american)
    if share <= 0:
        return ("💵 **Stake:** fun money only (no edge at this price, so Kelly says $0)" if parlay
                else "💵 **Stake:** none — no edge left at this price")
    roll = bankroll()
    money = f" (${share * roll:,.0f})" if roll else ""
    mult = _env("LINEMAKER_KELLY", 0.25)
    name = {0.25: "quarter-Kelly", 0.5: "half-Kelly", 1.0: "full Kelly"}.get(mult, f"{mult:g}× Kelly")
    return f"💵 **Stake:** {share:.1%} of bankroll{money} · {name}"


def suggested_dollars(p: float, american: int) -> Optional[float]:
    roll = bankroll()
    share = suggested(p, american)
    return round(share * roll, 2) if roll and share > 0 else None
