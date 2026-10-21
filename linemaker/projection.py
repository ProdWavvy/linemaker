"""Step 1: build a projected mean from input categories.

    mean = opportunity x efficiency x matchup x pace x venue x rest

Opportunity (minutes, carries, targets, batters faced, TOI, shots) leans on recent games,
because role changes show up there first and volume is the most predictable input.
Efficiency (stat per opportunity) leans on the season, and on an expected rate when one
is given (xG/shot, expected yards/target, K% from whiff rate), because hot shooting and
long-TD luck regress. Each environment factor is a multiplier vs. league average (1.00),
shrunk toward 1 and clamped so no single input can swing the number wildly.

Units are up to you as long as they're consistent: e.g. NBA minutes x points/minute,
NFL carries x yards/carry, MLB batters faced x K%, NHL TOI x shots/minute.
"""
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ProjectionConfig:
    opp_recent_k: float = 5.0        # recent games at which recent role gets 50% weight
    rate_recent_k: float = 25.0      # recent games at which recent efficiency gets 50% weight
    quality_k: float = 10.0          # season games at which observed rate gets 50% vs expected
    factor_strength: float = 0.6     # share of each environment factor's deviation applied
    factor_clamp: float = 0.20       # max +/- swing per factor after shrinking


@dataclass
class ProjectionInputs:
    season_games: int = 0
    opp_season: Optional[float] = None
    opp_recent: Optional[float] = None
    opp_recent_games: int = 0
    opp_override: Optional[float] = None   # known role change (teammate out, new starter)
    rate_season: Optional[float] = None
    rate_recent: Optional[float] = None
    rate_recent_games: int = 0
    rate_expected: Optional[float] = None  # quality signal: what the volume "should" produce
    matchup_factor: Optional[float] = None
    pace_factor: Optional[float] = None
    venue_factor: Optional[float] = None   # park, weather, altitude, dome/wind
    rest_factor: Optional[float] = None    # back-to-back, travel, short week

    @property
    def usable(self) -> bool:
        has_opp = any(v is not None for v in (self.opp_override, self.opp_season, self.opp_recent))
        has_rate = any(v is not None for v in (self.rate_season, self.rate_recent, self.rate_expected))
        return has_opp and has_rate


@dataclass
class Projection:
    mean: float
    opportunity: float
    rate: float
    steps: list = field(default_factory=list)   # (label, value) for the card


def _blend(base: Optional[float], recent: Optional[float], n: int, k: float):
    if recent is None or n <= 0:
        return base, 0.0
    if base is None:
        return recent, 1.0
    w = n / (n + k)
    return base + w * (recent - base), w


def _factor(raw: Optional[float], cfg: ProjectionConfig) -> Optional[float]:
    if raw is None:
        return None
    dev = (raw - 1) * cfg.factor_strength
    return 1 + max(-cfg.factor_clamp, min(cfg.factor_clamp, dev))


def build_projection(inp: ProjectionInputs, cfg: ProjectionConfig = ProjectionConfig()) -> Projection:
    if not inp.usable:
        raise ValueError("projection needs an opportunity input and an efficiency input")
    steps = []

    if inp.opp_override is not None:
        opp = inp.opp_override
        steps.append(("opportunity (role override)", f"{opp:g}"))
    else:
        opp, w = _blend(inp.opp_season, inp.opp_recent, inp.opp_recent_games, cfg.opp_recent_k)
        steps.append(("opportunity", f"{opp:.2f} (recent weight {w:.0%})"))

    base = inp.rate_season
    if inp.rate_expected is not None:
        if base is None:
            base = inp.rate_expected
        else:
            n = inp.season_games
            w_obs = n / (n + cfg.quality_k) if n > 0 else 0.0
            base = inp.rate_expected + w_obs * (base - inp.rate_expected)
            steps.append(("efficiency regressed to expected",
                          f"{inp.rate_season:.4g} → {base:.4g} (expected {inp.rate_expected:.4g})"))
    rate, w = _blend(base, inp.rate_recent, inp.rate_recent_games, cfg.rate_recent_k)
    steps.append(("efficiency", f"{rate:.4g} (recent weight {w:.0%})"))

    mean = opp * rate
    for name, raw in (("matchup", inp.matchup_factor), ("pace", inp.pace_factor),
                      ("venue/conditions", inp.venue_factor), ("rest/travel", inp.rest_factor)):
        f = _factor(raw, cfg)
        if f is not None:
            mean *= f
            steps.append((f"{name} ×{f:.3f}", f"input {raw:.2f}"))
    steps.append(("projected mean", f"{mean:.2f}"))
    return Projection(mean=mean, opportunity=opp, rate=rate, steps=steps)


# Markets the write-up calls soft: predictable volume the posted number is slow to price.
def soft_market_note(market: str, position: str) -> Optional[str]:
    mk, pos = market.lower(), position.upper()
    if pos == "QB" and "rushing" in mk:
        return "soft market: dual-threat QB rushing — books anchor to 'QBs throw'"
    if "longest" in mk:
        return "soft market: longest reception — volatile, priced wide"
    if "anytime" in mk or "touchdown" in mk:
        return "soft market: anytime TD — priced off season totals, not inside-10 role"
    if "receiving" in mk and pos in ("WR", "TE"):
        return "soft market: receiving yards — line leans on reputation over target share"
    return None
