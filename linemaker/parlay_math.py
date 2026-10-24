"""Parlay math: how legs move together, the real hit chance, and what a parlay is worth.

Correlation  Legs from different games are independent. Legs from the same game share the
             game (pace, script, a blowout, a shootout), so their hit chances are tied. Each
             pair gets a correlation from what the two bets are (see pair_rho):
               same player, stats that overlap (points & PRA, catches & receiving yards)  ~0.7
               same player, different stats (points & rebounds)                           ~0.3
               QB passing with a teammate's receiving                                       0.4
               a game-total over with a player's over                                       0.25
               a team's moneyline / spread with its own players' overs                      0.12
               teammates otherwise ~0.05, opponents 0.08 (pace / shootout)
             signed by direction (an over with an under flips it). The values are typical
             same-game correlations, not fitted to this data yet; graded parlays will show
             whether they need moving.
Hit chance   A Gaussian copula: each leg hits when its own normal draw falls under its
             probability's quantile, the draws correlated as above; 20,000 draws, seeded. With
             no same-game pairs it's just the product.
Value        A leg's value = hit chance x decimal odds (above 1 = +EV at that price). A
             parlay's = joint hit chance x the product of decimal odds (times a profit boost).
"""
import math
import zlib
from statistics import NormalDist
from typing import Optional

from .oddsapi import norm

N_DRAWS = 20000
_TEAMS = {}

# Stats of one player that largely contain each other.
OVERLAP = [{"points", "pra", "pts + reb", "pts + ast"}, {"rebounds", "pra", "pts + reb", "reb + ast"},
           {"assists", "pra", "pts + ast", "reb + ast"}, {"receptions", "receiving yards"},
           {"rushing yards", "rush attempts"}, {"passing yards", "pass completions", "pass attempts"},
           {"hits", "total bases"}, {"shots on goal", "hockey points"},
           {"blocks", "steals", "blks + stls"}, {"pass + rush yards", "passing yards", "rushing yards"},
           {"rush + rec yards", "rushing yards", "receiving yards"},
           {"hits", "singles", "h+r+rbi", "total bases"}, {"runs", "rbis", "h+r+rbi"},
           {"goals", "anytime goals", "hockey points"}, {"hockey assists", "hockey points"},
           {"kicking points", "field goals made", "extra points"}, {"receiving tds", "anytime td"},
           {"rushing tds", "anytime td"}]
PASSING = {"passing yards", "pass completions", "passing tds", "pass attempts"}
RECEIVING = {"receiving yards", "receptions"}


def team_of(sport: str, date: str, player: str) -> Optional[str]:
    """The player's team tonight (ESPN rosters, cached for the day), or None."""
    import os
    if os.environ.get("LINEMAKER_OFFLINE"):
        return None
    key = (sport, date)
    if key not in _TEAMS:
        try:
            from .gamelogs import roster_index
            _TEAMS[key] = {k: v[1] for k, v in roster_index(sport, date).items()}
        except Exception:
            _TEAMS[key] = {}
    return _TEAMS[key].get(norm(player))


def direction(ev) -> int:
    """+1 for an over / a team to win or cover, -1 for an under (props and totals)."""
    m = ev.market
    if m.bet_type in ("prop", "total", "team_total"):
        return 1 if ev.best.side == "A" else -1
    return 1


def _backed_team(ev) -> Optional[str]:
    m = ev.market
    if m.bet_type in ("moneyline", "spread"):
        return m.subject if ev.best.side == "A" else m.opponent
    return None


def pair_rho(a, b) -> float:
    """Correlation between two legs' outcomes (0 for different games)."""
    ma, mb = a.market, b.market
    if ma.game_id != mb.game_id:
        return 0.0
    if not hasattr(ma, "sport") or not hasattr(mb, "sport"):
        return 0.0
    sign = direction(a) * direction(b)
    ka, kb = ma.market.lower(), mb.market.lower()
    props = ma.bet_type == "prop" and mb.bet_type == "prop"
    if props and norm(ma.subject) == norm(mb.subject):
        overlap = any(ka in s and kb in s for s in OVERLAP)
        return sign * (0.7 if overlap else 0.3)
    if props:
        ta, tb = team_of(ma.sport, ma.date, ma.subject), team_of(mb.sport, mb.date, mb.subject)
        same = ta is not None and tb is not None and norm(ta) == norm(tb)
        if ta is None or tb is None:
            return sign * 0.06
        if same and ((ka in PASSING and kb in RECEIVING) or (kb in PASSING and ka in RECEIVING)):
            return sign * 0.4
        return sign * (0.05 if same else 0.08)
    if "team_total" in (ma.bet_type, mb.bet_type):
        tt, other = (a, b) if ma.bet_type == "team_total" else (b, a)
        om = other.market
        if om.bet_type == "total":
            return sign * 0.6
        if om.bet_type == "team_total":
            return sign * 0.3
        if om.bet_type == "prop":
            t = team_of(om.sport, om.date, om.subject)
            return sign * (0.3 if t and norm(t) == norm(tt.market.subject) else 0.05)
        backed = _backed_team(other)
        if backed:
            return direction(tt) * (0.35 if norm(backed) == norm(tt.market.subject) else -0.35)
        return 0.0
    if "total" in (ma.bet_type, mb.bet_type):
        other = b if ma.bet_type == "total" else a
        if other.market.bet_type == "prop":
            return sign * 0.25
        return 0.0
    team = _backed_team(a) or _backed_team(b)
    prop = b if _backed_team(a) else a
    if team and prop.market.bet_type == "prop":
        t = team_of(prop.market.sport, prop.market.date, prop.market.subject)
        if t is None:
            return 0.0
        return direction(prop) * (0.12 if norm(t) == norm(team) else -0.05)
    return 0.0


def _cholesky(m: list) -> Optional[list]:
    n = len(m)
    L = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1):
            s = m[i][j] - sum(L[i][k] * L[j][k] for k in range(j))
            if i == j:
                if s <= 1e-9:
                    return None
                L[i][j] = math.sqrt(s)
            else:
                L[i][j] = s / L[j][j]
    return L


def hit_chance(ev) -> float:
    """A leg's hit chance for parlay math: calibrated to how past legs landed (autotune),
    unless the scan already calibrated it (volume mode)."""
    p = ev.best.blended
    if getattr(ev, "calibrated", False) or not hasattr(ev.market, "sport") or not hasattr(ev.market, "side_keys"):
        return p
    if len(ev.market.side_keys) != 2:
        return p
    from .autotune import calibrated
    p_a = calibrated(p if ev.best.side == "A" else 1 - p, ev.market.sport)
    return p_a if ev.best.side == "A" else 1 - p_a


def joint_prob(legs: list, probs: Optional[list] = None) -> float:
    """P(every leg hits), same-game legs correlated (see the module notes)."""
    probs = probs if probs is not None else [hit_chance(ev) for ev in legs]
    n = len(legs)
    if n == 0:
        return 1.0
    rho = [[1.0 if i == j else pair_rho(legs[i], legs[j]) for j in range(n)] for i in range(n)]
    if all(rho[i][j] == 0 for i in range(n) for j in range(n) if i != j):
        return math.prod(probs)
    L, shrink = None, 1.0
    while L is None and shrink > 0.05:   # keep the matrix valid: scale correlations down if needed
        L = _cholesky([[rho[i][j] * (shrink if i != j else 1) for j in range(n)] for i in range(n)])
        shrink *= 0.8
    if L is None:
        return math.prod(probs)
    cut = [NormalDist().inv_cdf(min(max(p, 1e-6), 1 - 1e-6)) for p in probs]
    seed = zlib.crc32("|".join(sorted(f"{ev.market.key}:{ev.best.side}" for ev in legs)).encode())
    try:
        import numpy as np
        rng = np.random.default_rng(seed)
        z = rng.standard_normal((N_DRAWS, n)) @ np.array(L).T
        return float((z < np.array(cut)).all(axis=1).mean())
    except ImportError:
        import random
        rng = random.Random(seed)
        hits = 0
        for _ in range(N_DRAWS // 4):
            e = [rng.gauss(0, 1) for _ in range(n)]
            z = [sum(L[i][k] * e[k] for k in range(i + 1)) for i in range(n)]
            hits += all(z[i] < cut[i] for i in range(n))
        return hits / (N_DRAWS // 4)


def leg_value(ev, odds: int) -> float:
    """Hit chance x decimal odds: above 1.0 the leg is +EV at that price."""
    from .odds import american_to_decimal
    return hit_chance(ev) * american_to_decimal(odds)


def boosted(decimal: float, boost: float) -> float:
    """Decimal odds after a profit boost (0.25 = +25% on the winnings)."""
    return 1 + (decimal - 1) * (1 + boost)
