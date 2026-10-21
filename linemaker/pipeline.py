"""Slate check -> estimate -> filter -> rank."""
from collections import defaultdict
from dataclasses import dataclass, field

from .config import Config
from .market import INJURY_EXCLUDE, Market
from .model import Evaluation, estimate
from .odds import fmt_odds, implied_probability


@dataclass
class AltEval:
    line: float
    odds: int
    implied: float
    in_window: bool


@dataclass
class Result:
    slate_date: str
    picks: list = field(default_factory=list)        # ranked Evaluations
    excluded: list = field(default_factory=list)     # (Market, reason)
    correlated: dict = field(default_factory=dict)   # game_id -> [labels]


def in_odds_window(odds: int, cfg: Config) -> bool:
    return cfg.max_odds <= odds <= cfg.min_odds


def slate_reason(m: Market, slate_date: str) -> str | None:
    if m.date != slate_date:
        return f"not on today's slate (game date {m.date})"
    if not m.start_confirmed:
        return "start time unconfirmed"
    return None


def alt_lines(ev: Evaluation, cfg: Config) -> list:
    return [AltEval(a.line, a.odds, implied_probability(a.odds), in_odds_window(a.odds, cfg))
            for a in ev.market.alt_lines]


def run(markets: list, slate_date: str, cfg: Config) -> Result:
    res = Result(slate_date=slate_date)
    qualifiers = []
    for m in markets:
        reason = slate_reason(m, slate_date)
        if reason:
            res.excluded.append((m, reason))
            continue
        if m.injury_status in INJURY_EXCLUDE:
            res.excluded.append((m, f"injury status: {m.injury_status}"))
            continue
        if m.season_games == 0:
            res.excluded.append((m, "no season data available"))
            continue
        if not in_odds_window(m.odds, cfg):
            res.excluded.append((m, f"odds {fmt_odds(m.odds)} outside "
                                    f"{fmt_odds(cfg.min_odds)} to {fmt_odds(cfg.max_odds)}"))
            continue
        ev = estimate(m, cfg)
        if ev.true_prob < cfg.min_true_prob:
            res.excluded.append((m, f"true probability {ev.true_prob:.1%} below "
                                    f"{cfg.min_true_prob:.0%} floor"))
            continue
        if ev.gap <= 0:
            res.excluded.append((m, f"no edge: true {ev.true_prob:.1%} vs implied "
                                    f"{ev.implied:.1%}"))
            continue
        qualifiers.append(ev)

    qualifiers.sort(key=lambda e: e.gap, reverse=True)
    for ev in qualifiers[cfg.max_picks:]:
        res.excluded.append((ev.market, f"qualified (gap {ev.gap:+.1%}) but below "
                                        f"top-{cfg.max_picks} cutoff"))
    res.picks = qualifiers[:cfg.max_picks]

    by_game = defaultdict(list)
    for ev in res.picks:
        by_game[ev.market.game_id].append(ev.market.label)
    res.correlated = {g: labels for g, labels in by_game.items() if len(labels) > 1}
    return res
