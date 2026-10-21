"""Tunable thresholds and weights. Every number the model uses lives here."""
from dataclasses import dataclass


@dataclass
class Config:
    # Odds window (American). A market qualifies when MAX_ODDS <= odds <= MIN_ODDS,
    # e.g. -1000 <= odds <= -200. The handoff doc's framework used -175 to -300;
    # the stated preference is -200 to -1000. Override with --odds-min/--odds-max.
    min_odds: int = -200
    max_odds: int = -1000

    min_true_prob: float = 0.70
    max_picks: int = 10

    # Blend weights (renormalised over whichever inputs are present).
    w_season: float = 0.60
    w_split: float = 0.25
    w_recent: float = 0.15
    # A split only gets full weight once it has this many games behind it.
    split_full_weight_games: int = 15

    # Haircuts (subtracted from the probability estimate).
    hot_streak_haircut: float = 0.03    # recent windows hotter than the season rate
    combo_haircut: float = 0.02         # PRA and other multi-stat markets
    projection_miss_haircut: float = 0.03  # projection on the wrong side of the line
    small_sample_haircut: float = 0.03  # season sample below small_sample_games
    small_sample_games: int = 10
    questionable_haircut: float = 0.03  # player listed questionable / GTD

    # Matchup adjustment from the input is clamped to +/- this.
    max_matchup_adj: float = 0.03
