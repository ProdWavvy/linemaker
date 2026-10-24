"""Stake sizing: fractional Kelly.

  full Kelly  f = (b * p - (1 - p)) / b      b = decimal odds - 1, p = our hit chance
  suggested   LINEMAKER_KELLY (default 0.25, "quarter Kelly") x f, capped at
              LINEMAKER_MAX_STAKE_PCT (default 2%) of bankroll

Quarter Kelly gives up a little growth for much smaller swings, and it forgives an estimate
that's a few points too confident.

Cards show one of your usual amounts (LINEMAKER_STAKE_TIERS, default 2.5,5,10,15,20): the
Kelly suggestion as a share of the cap picks the tier, so the strongest edges get the top
amount. Parlays with an edge go up to LINEMAKER_PARLAY_MAX_STAKE ($10 by default) and
parlays without one get the smallest amount (they swing much harder). Set the tiers blank to
see Kelly percentages (and dollars, with LINEMAKER_BANKROLL) instead.
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


def tiers() -> list:
    raw = os.environ.get("LINEMAKER_STAKE_TIERS", "2.5,5,10,15,20")
    out = []
    for x in raw.split(","):
        try:
            out.append(float(x.strip().lstrip("$")))
        except ValueError:
            continue
    return sorted(v for v in out if v > 0)


def stake_tier(p: float, american: int, parlay: bool = False) -> Optional[float]:
    """One of your amounts for this bet, or None when there's no edge at this price."""
    levels = tiers()
    if not levels:
        return None
    share = suggested(p, american)
    if share <= 0:
        return levels[0] if parlay else None      # a fun-money parlay: smallest amount
    cap = _env("LINEMAKER_MAX_STAKE_PCT", 2.0) / 100
    # Parlays with an edge go up to LINEMAKER_PARLAY_MAX_STAKE (default $10): they swing harder
    # than singles, so the top amounts stay for straight bets.
    usable = [v for v in levels if v <= _env("LINEMAKER_PARLAY_MAX_STAKE", 10.0)] or levels[:1] if parlay else levels
    i = min(int(share / cap * len(usable) - 1e-9), len(usable) - 1)
    return usable[max(i, 0)]


def _money(v: float) -> str:
    return f"${v:,.2f}" if v != int(v) else f"${v:,.0f}"


def stake_line(p: float, american: int, parlay: bool = False) -> str:
    if tiers():
        amount = stake_tier(p, american, parlay)
        if amount is None:
            return "💵 **Stake:** skip — no edge left at this price"
        if parlay and suggested(p, american) <= 0:
            return f"💵 **Stake:** {_money(amount)} (fun money: no edge, so keep it small)"
        return f"💵 **Stake:** {_money(amount)}"
    share = suggested(p, american)
    if share <= 0:
        return ("💵 **Stake:** fun money only (no edge at this price, so Kelly says $0)" if parlay
                else "💵 **Stake:** none — no edge left at this price")
    roll = bankroll()
    money = f" (${share * roll:,.0f})" if roll else ""
    mult = _env("LINEMAKER_KELLY", 0.25)
    name = {0.25: "quarter-Kelly", 0.5: "half-Kelly", 1.0: "full Kelly"}.get(mult, f"{mult:g}× Kelly")
    return f"💵 **Stake:** {share:.1%} of bankroll{money} · {name}"


def suggested_dollars(p: float, american: int, parlay: bool = False) -> Optional[float]:
    if tiers():
        return stake_tier(p, american, parlay)
    roll = bankroll()
    share = suggested(p, american)
    return round(share * roll, 2) if roll and share > 0 else None


def parlay_pay_line(p: float, american: int, decimal: float) -> str:
    """'💵 Stake $5 → pays $27.80 ($22.80 profit)' at your suggested amount for a parlay."""
    amount = suggested_dollars(p, american, parlay=True)
    if not amount:
        return stake_line(p, american, parlay=True) + f" · $10 pays **${10 * decimal:,.2f}**"
    fun = " · fun money: no edge, keep it small" if suggested(p, american) <= 0 else ""
    return (f"💵 **Stake {_money(amount)}** → pays **{_money(round(amount * decimal, 2))}** "
            f"({_money(round(amount * (decimal - 1), 2))} profit){fun}")
