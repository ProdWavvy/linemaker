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

Bankroll mode (/bankroll, or LINEMAKER_BANKROLL): every stake is a share of what you have
now, the starting amount plus what your real (not practice) bets have won or lost since
LINEMAKER_BANKROLL_SINCE, so stakes shrink after losses and grow with wins:
  straight bets  quarter Kelly, at most LINEMAKER_MAX_STAKE_PCT (2%)
  parlays        quarter Kelly, at most LINEMAKER_PARLAY_STAKE_PCT (1%)
  no-edge parlay LINEMAKER_FUN_STAKE_PCT (0.5%)
rounded to 5 cents, never under the books' $0.10 minimum. It replaces the dollar tiers.
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


def bankroll_pl() -> float:
    """Profit / loss of your real (not practice) settled bets since the bankroll was set."""
    since = os.environ.get("LINEMAKER_BANKROLL_SINCE", "").strip()
    from .bets import load_bets
    return sum(b.get("profit", 0.0) for b in load_bets()
               if b.get("status") in ("won", "lost", "push") and not b.get("paper")
               and (not since or (b.get("settled") or b.get("placed", "")) >= since))


def current_bankroll() -> Optional[float]:
    start = bankroll()
    return None if start is None else max(start + bankroll_pl(), 0.0)


def bankroll_stake(p: float, american: int, parlay: bool = False) -> Optional[float]:
    """Dollars for this bet in bankroll mode (None: no edge on a straight bet, or no bankroll)."""
    roll = current_bankroll()
    if not roll:
        return None
    share = suggested(p, american)
    if parlay:
        share = (min(share, _env("LINEMAKER_PARLAY_STAKE_PCT", 1.0) / 100) if share > 0
                 else _env("LINEMAKER_FUN_STAKE_PCT", 0.5) / 100)
    if share <= 0:
        return None
    return max(round(roll * share * 20) / 20, 0.10)   # to the nickel


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
    if bankroll() is not None:
        return bankroll_stake(p, american, parlay)
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


def paper_on() -> bool:
    """Practice mode: Bet / I bet this log practice bets (no real money)."""
    return os.environ.get("LINEMAKER_PAPER", "off").strip().lower() in ("on", "1", "yes", "true")


def stake_line(p: float, american: int, parlay: bool = False) -> str:
    if bankroll() is not None:
        amount, roll = bankroll_stake(p, american, parlay), current_bankroll() or 0
        if amount is None:
            return "💵 **Stake:** skip — no edge left at this price"
        note = " (fun money: no edge, so keep it small)" if parlay and suggested(p, american) <= 0 else ""
        return f"💵 **Stake:** {_money(amount)} · {amount / roll:.1%} of your {_money(round(roll, 2))} bankroll{note}"
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
    if bankroll() is not None:
        return bankroll_stake(p, american, parlay)
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
