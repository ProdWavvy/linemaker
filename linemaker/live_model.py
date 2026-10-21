"""Live (in-game) model for moneylines, spreads and totals.

Start from the pre-game view of the game, then let the clock and the score do the work:

  prior     The pre-game market line when the bot saved one before tip-off (a game-lines
            scan earlier that day); otherwise the ESPN-ratings game model. Home margin M0 and
            total T0 for a full game.
  f         Share of regulation still to play, from ESPN's period and clock (MLB: half
            innings and outs, per team).

  Basketball, football (a random walk: what's left is the prior, scaled to the time left)
            final margin ~ Normal(lead + M0 * f, sd * sqrt(f))
            final total  = points so far + rate * f, where the rate blends the pre-game total
                           with tonight's observed pace (more weight as the game goes on)
  Hockey, baseball (goals and runs are rare, so they're counted, not smoothed)
            each side's remaining scoring ~ Poisson((T0 +/- M0) / 2 * its share left);
            a tie after regulation is a coin flip (overtime / extras)

Not modelled: possession, field position, down and distance, fouls and timeouts, power
plays, pitcher changes, injuries during the game. Live books see all of that within
seconds, so the model leans on the market (LINEMAKER_LIVE_MODEL_WEIGHT, default 0.35), skips
odds older than LINEMAKER_LIVE_MAX_ODDS_AGE seconds (default 90) and skips the last few
minutes, when fouling, timeouts and empty nets break the model.
"""
import datetime as dt
import logging
import math
import os
import statistics
from dataclasses import dataclass
from typing import Optional

from .devig import no_vig
from .game_model import SD, TOTAL_SD
from .gamelogs import PATHS
from .oddsapi import norm

log = logging.getLogger("linemaker.live_model")
SUPPORTED = ("NBA", "WNBA", "NFL", "NCAAF", "NHL", "MLB")
# (regulation periods, minutes per period, overtime minutes)
CLOCK = {"NBA": (4, 12, 5), "WNBA": (4, 10, 5), "NFL": (4, 15, 10), "NCAAF": (4, 15, 0),
         "NHL": (3, 20, 5)}
COUNTED = {"NHL", "MLB"}
MLB_INNINGS = 9
MIN_LEFT = 0.04          # skip the last ~2 minutes of a basketball game (and the like)
PACE_PRIOR = 1.0         # observed pace counts as much as the pre-game total once a full game in


def live_weight() -> float:
    return float(os.environ.get("LINEMAKER_LIVE_MODEL_WEIGHT", "0.35"))


def max_odds_age() -> float:
    return float(os.environ.get("LINEMAKER_LIVE_MAX_ODDS_AGE", "90"))


@dataclass
class GameState:
    sport: str
    away: str
    home: str
    away_score: int
    home_score: int
    state: str                 # pre, in, post
    detail: str                # "Q3 7:12", "Top 5th"
    left: float                # share of regulation left (both teams)
    home_left: float           # MLB: home team's share of its innings left; else = left
    away_left: float

    @property
    def lead(self) -> int:
        return self.home_score - self.away_score

    @property
    def label(self) -> str:
        return f"{self.away} {self.away_score}, {self.home} {self.home_score} · {self.detail}"


def _clock_seconds(text: str) -> float:
    text = (text or "0").strip()
    if ":" in text:
        m, s = text.split(":", 1)
        return int(m or 0) * 60 + float(s or 0)
    try:
        return float(text)
    except ValueError:
        return 0.0


def time_left(sport: str, period: int, clock: str) -> float:
    n, minutes, ot = CLOCK[sport]
    total = n * minutes * 60
    secs = _clock_seconds(clock)
    if period <= n:
        return ((n - period) * minutes * 60 + secs) / total
    if ot:
        return min(secs, ot * 60) / total
    return 0.05          # untimed overtime (college football): roughly one more series each


def mlb_left(detail: str, inning: int, outs: int) -> tuple:
    """(away share left, home share left) of nine innings, from 'Top 5th', 'Mid 5th', ..."""
    d = (detail or "").lower()
    part = (3 - min(max(outs, 0), 3)) / 3
    inning = max(inning, 1)
    if d.startswith("top"):
        away, home = MLB_INNINGS - inning + part, MLB_INNINGS - inning + 1
    elif d.startswith("bot"):
        away, home = MLB_INNINGS - inning, MLB_INNINGS - inning + part
    elif d.startswith("mid"):
        away, home = MLB_INNINGS - inning, MLB_INNINGS - inning + 1
    else:                 # "End 5th"
        away, home = MLB_INNINGS - inning, MLB_INNINGS - inning
    return max(away, 0.0) / MLB_INNINGS, max(home, 0.0) / MLB_INNINGS


def parse_scoreboard(sport: str, data: dict) -> dict:
    """(norm away, norm home) -> GameState for every game on an ESPN scoreboard."""
    out = {}
    for ev in data.get("events", []):
        comp = ev["competitions"][0]
        st = comp.get("status") or ev.get("status") or {}
        typ = st.get("type", {})
        teams = {c["homeAway"]: c for c in comp["competitors"]}
        if "home" not in teams or "away" not in teams:
            continue
        home, away = teams["home"]["team"]["displayName"], teams["away"]["team"]["displayName"]
        period = int(st.get("period") or 0)
        detail = typ.get("shortDetail") or typ.get("detail") or ""
        state = typ.get("state", "pre")
        if sport == "MLB":
            outs = int((comp.get("situation") or {}).get("outs") or 0)
            a_left, h_left = mlb_left(detail, period, outs)
            left = (a_left + h_left) / 2
        elif sport in CLOCK:
            left = time_left(sport, period, st.get("displayClock", "0:00")) if state == "in" else (
                1.0 if state == "pre" else 0.0)
            a_left = h_left = left
        else:
            continue
        out[(norm(away), norm(home))] = GameState(
            sport=sport, away=away, home=home,
            away_score=int(float(teams["away"].get("score") or 0)),
            home_score=int(float(teams["home"].get("score") or 0)),
            state=state, detail=detail, left=left, home_left=h_left, away_left=a_left)
    return out


def live_states(sport: str, date: str) -> dict:
    from .boxscores import _get
    return parse_scoreboard(sport, _get(f"{PATHS[sport]}/scoreboard", dates=date.replace("-", "")))


def _z(p: float) -> float:
    return statistics.NormalDist().inv_cdf(min(max(p, 0.005), 0.995))


def _consensus(books: list, n: int = 2) -> Optional[float]:
    fair = [no_vig(*p[:n])[0] for _b, p in books if len(p) >= n and None not in p[:n]]
    return sum(fair) / len(fair) if fair else None


def market_prior(rows: list, home: str, sport: str) -> Optional[tuple]:
    """(home margin, total) implied by the pre-game spread and total in saved slate rows."""
    from .pro import parse_books
    margin = total = None
    best_m = best_t = 1.0
    for r in rows:
        if norm(r.get("subject", "")) != norm(home):
            continue
        p = _consensus(parse_books(r.get("books", "")))
        if p is None:
            continue
        line = float(r.get("line") or 0)
        if r.get("bet_type") == "spread" and abs(p - 0.5) < best_m:
            best_m, margin = abs(p - 0.5), -line + SD.get(sport, 12.0) * _z(p)
        elif r.get("bet_type") == "total" and abs(p - 0.5) < best_t:
            best_t, total = abs(p - 0.5), line + TOTAL_SD.get(sport, 15.0) * _z(p)
    return (margin, total) if margin is not None and total is not None else None


def _poisson(k: int, lam: float) -> float:
    return math.exp(-lam) * lam ** k / math.factorial(k) if lam > 0 else float(k == 0)


def counted_win_prob(lead: int, lam_home: float, lam_away: float) -> float:
    """P(home wins) when each side's remaining goals/runs are Poisson; ties are 50/50."""
    top = 30
    ph = [_poisson(k, lam_home) for k in range(top)]
    pa = [_poisson(k, lam_away) for k in range(top)]
    win = tie = 0.0
    for i, x in enumerate(ph):
        for j, y in enumerate(pa):
            d = lead + i - j
            if d > 0:
                win += x * y
            elif d == 0:
                tie += x * y
    return win + 0.5 * tie


def project_live(g: GameState, prior_margin: float, prior_total: float) -> dict:
    """{"ml_mean", "spread_mean", "margin_sd", "total_mean", "total_sd", "win_prob"}."""
    sport, f = g.sport, g.left
    played = g.home_score + g.away_score
    if sport in COUNTED:
        lam_h = max((prior_total + prior_margin) / 2, 0.05) * g.home_left
        lam_a = max((prior_total - prior_margin) / 2, 0.05) * g.away_left
        sd = max(math.sqrt(lam_h + lam_a), 0.35)
        p = counted_win_prob(g.lead, lam_h, lam_a)
        return {"ml_mean": sd * _z(p), "spread_mean": g.lead + lam_h - lam_a, "margin_sd": sd,
                "total_mean": played + lam_h + lam_a, "total_sd": sd, "win_prob": p}
    sd = max(SD.get(sport, 12.0) * math.sqrt(f), 0.5)
    mean = g.lead + prior_margin * f
    elapsed = 1 - f
    observed = played / elapsed if elapsed > 0.05 else prior_total
    w = elapsed / (elapsed + PACE_PRIOR)
    rate = w * observed + (1 - w) * prior_total
    p = statistics.NormalDist(mean, sd).cdf(0) if sd else float(mean > 0)
    return {"ml_mean": mean, "spread_mean": mean, "margin_sd": sd,
            "total_mean": played + rate * f, "total_sd": max(TOTAL_SD.get(sport, 15.0) * math.sqrt(f), 0.5),
            "win_prob": 1 - p}


def live_projections(sport: str, date: str, states: dict, games: list, prior_rows: list,
                     prior_saved_at: Optional[float] = None) -> list:
    """games: [(away, home, commence)] in progress -> projection rows for the engine."""
    from .game_model import game_projections
    rows = []
    for away, home, commence in games:
        g = states.get((norm(away), norm(home)))
        if not g or g.state != "in" or not (MIN_LEFT <= g.left < 0.999):
            continue
        prior, source = None, ""
        start = dt.datetime.fromisoformat(commence.replace("Z", "+00:00")).timestamp() if commence else 0
        if prior_rows and prior_saved_at and prior_saved_at < start:
            prior = market_prior([r for r in prior_rows if r.get("game", "").endswith(home)], home, sport)
            source = "pre-game market line"
        if prior is None:
            # Kalshi trades live during the game, so it can't stand in for the pre-game view.
            model = game_projections(sport, date, [(away, home)], use_kalshi=False)
            m = next((float(r["mean"]) for r in model if r["market"] == "moneyline"), None)
            t = next((float(r["mean"]) for r in model if r["market"] == "game total"), None)
            if m is None or t is None:
                continue
            prior, source = (m, t), "ESPN team ratings"
        x = project_live(g, *prior)
        note = (f"LIVE · {g.label} · {x['win_prob']:.0%} {home} to win · prior: {source} "
                f"(home {prior[0]:+.1f}, total {prior[1]:.1f})")
        base = {"sport": sport, "subject": home, "notes": note}
        rows += [dict(base, market="moneyline", mean=f"{x['ml_mean']:.3f}", stdev=f"{x['margin_sd']:.3f}"),
                 dict(base, market="spread", mean=f"{x['spread_mean']:.3f}", stdev=f"{x['margin_sd']:.3f}"),
                 dict(base, market="game total", mean=f"{x['total_mean']:.3f}",
                      stdev=f"{x['total_sd']:.3f}")]
    return rows


def drop_stale_books(feed: list, now: Optional[dt.datetime] = None) -> list:
    """Remove book prices older than the max age (stale live odds make fake edges)."""
    now = now or dt.datetime.now(dt.timezone.utc)
    limit = max_odds_age()
    out = []
    for fm in feed:
        fresh = []
        for book, prices in fm.books:
            ts = (fm.updated or {}).get(book)
            if ts:
                age = (now - dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))).total_seconds()
                if age > limit:
                    continue
            fresh.append((book, prices))
        if fresh:
            fm.books = fresh
            fm.links = {b: v for b, v in (fm.links or {}).items() if b in {x for x, _ in fresh}}
            out.append(fm)
    return out
