"""Conservative true-probability estimate and gap calculation."""
from dataclasses import dataclass, field

from .config import Config
from .market import INJURY_FLAG, Market
from .odds import implied_probability


@dataclass
class Evaluation:
    market: Market
    implied: float
    season_rate: float
    split_rate: float | None
    recent_rate: float | None
    blend: float
    true_prob: float
    gap: float
    season_edge: float          # season hit rate minus implied
    adjustments: list = field(default_factory=list)  # (reason, delta)


def _projection_agrees(m: Market) -> bool | None:
    if m.projection is None or m.line is None:
        return None
    if m.side.lower() == "under":
        return m.projection < m.line
    if m.side.lower() == "over":
        return m.projection > m.line
    # Spreads/ML: projection is the projected margin for `side`; line is the spread.
    if m.bet_type == "spread":
        return m.projection + m.line > 0
    if m.bet_type == "moneyline":
        return m.projection > 0
    return None


def estimate(m: Market, cfg: Config) -> Evaluation:
    implied = implied_probability(m.odds)
    season = m.season_hits / m.season_games if m.season_games else 0.0

    parts = [(season, cfg.w_season)]
    split = None
    if m.split_games:
        split = m.split_hits / m.split_games
        weight = cfg.w_split * min(1.0, m.split_games / cfg.split_full_weight_games)
        parts.append((split, weight))
    recent_vals = list(m.recent_windows.values())
    recent = sum(recent_vals) / len(recent_vals) if recent_vals else None
    if recent is not None:
        parts.append((recent, cfg.w_recent))
    total_w = sum(w for _, w in parts)
    blend = sum(p * w for p, w in parts) / total_w

    adjustments = []
    # Never raise the estimate above the season baseline on recent form or splits.
    est = min(blend, season)
    if blend > season:
        adjustments.append(("capped at season baseline", est - blend))
    if recent is not None and recent > season:
        est -= cfg.hot_streak_haircut
        adjustments.append(("recent form hotter than season (no streak chasing)",
                            -cfg.hot_streak_haircut))
    if m.is_combo:
        est -= cfg.combo_haircut
        adjustments.append(("multi-stat combo variance", -cfg.combo_haircut))
    if m.season_games < cfg.small_sample_games:
        est -= cfg.small_sample_haircut
        adjustments.append((f"small season sample ({m.season_games} games)",
                            -cfg.small_sample_haircut))
    agrees = _projection_agrees(m)
    if agrees is False:
        est -= cfg.projection_miss_haircut
        adjustments.append(("projection on wrong side of line", -cfg.projection_miss_haircut))
    if m.matchup_adj:
        adj = max(-cfg.max_matchup_adj, min(cfg.max_matchup_adj, m.matchup_adj))
        est += adj
        adjustments.append((m.matchup_note or "matchup adjustment", adj))
    if m.injury_status in INJURY_FLAG and m.injury_status != "probable":
        est -= cfg.questionable_haircut
        adjustments.append((f"injury status: {m.injury_status}", -cfg.questionable_haircut))

    est = max(0.01, min(0.99, est))
    return Evaluation(
        market=m, implied=implied, season_rate=season, split_rate=split,
        recent_rate=recent, blend=blend, true_prob=est, gap=est - implied,
        season_edge=season - implied, adjustments=adjustments,
    )
