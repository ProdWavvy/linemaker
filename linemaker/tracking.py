"""Log every priced market, attach results, and calibrate the model/market blend weight.

tracking/<date>.json holds one record per market (every game line and prop the engine
priced, picks or not). A record keeps updating while its game is upcoming and stops
changing once the game has started, so it reflects the last pre-game read.

Calibration: for graded records, the blended probability for side A is
    w * model + (1 - w) * no-vig market
and each w from 0 to 1 is scored by log loss and Brier score against what happened.
The best w is what LINEMAKER_MODEL_WEIGHT should be.
"""
import datetime as dt
import glob
import json
import math
import os
import re
from dataclasses import asdict
from typing import Optional

from .odds import fmt_odds, net_profit
from .oddsapi import norm


MAX_HISTORY = 60


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _started(commence: str, now: dt.datetime) -> bool:
    if not commence:
        return False
    return dt.datetime.fromisoformat(commence.replace("Z", "+00:00")) <= now


class Tracker:
    def __init__(self, directory: str, date: str):
        self.path = os.path.join(directory, f"{date}.json")
        self.records = {}
        if os.path.exists(self.path):
            with open(self.path) as f:
                self.records = json.load(f)
        self._touched = set()

    def record(self, ev, now: Optional[dt.datetime] = None):
        m = ev.market
        now = now or _now()
        old = self.records.get(m.key)
        if old is not None and _started(old.get("commence", ""), now):
            return   # game under way: keep the last pre-game read
        from .pro import book_prices
        sides = {s.side: s for s in ev.sides}
        a = sides.get("A")
        books = {side: book_prices(m, side) for side in m.side_keys} if m.books else {}
        history = list(old.get("history", [])) if old else []
        if books and (not history or {k: v for k, v in history[-1].items() if k != "t"} != books):
            history.append({"t": now.isoformat(timespec="seconds"), **books})
            history = history[-MAX_HISTORY:]
        self.records[m.key] = {
            "sport": m.sport, "game_id": m.game_id, "game": m.game, "commence": m.commence,
            "subject": m.subject, "opponent": m.opponent, "market": m.market,
            "position": m.position, "threshold": m.threshold,
            "inputs": {"bet_type": m.bet_type, "line": m.line, "mean": m.mean,
                       "mean_b": m.mean_b, "stdev": m.stdev, "dist": ev.sim.dist},
            "sim": {"sd": ev.sim.sd, "median": ev.sim.median, "p_over": ev.sim.p_over,
                    "p_push": ev.sim.p_push, "p_under": ev.sim.p_under},
            # How the projection was built (Step / Value): kept here, not shown on the card.
            "projection_steps": [list(step) for step in m.projection.steps] if m.projection else [],
            "model_prob_a": a.model_prob if a else None,
            "market_prob_a": a.market_prob if a else None,
            "model_weight": ev.model_weight, "news_fresh": m.news_fresh,
            "injury_status": m.injury_status, "source": m.notes,
            "sides": [asdict(s) for s in ev.sides],
            "books": books,
            "history": history,          # price snapshots, appended when any price moves
            "best_side": ev.best.side, "picked": old.get("picked", False) if old else False,
            "first_seen": old["first_seen"] if old else now.isoformat(timespec="seconds"),
            "updated": now.isoformat(timespec="seconds"),
            "result": old.get("result") if old else None,
        }
        self._touched.add(m.key)

    def mark_picks(self, picks):
        for ev in picks:
            if ev.market.key in self._touched:
                rec = self.records[ev.market.key]
                rec["picked"] = True
                rec["pick_side"] = ev.best.side

    def save(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.records, f)
        os.replace(tmp, self.path)


def _winner(rec: dict, actual: float) -> Optional[str]:
    t = rec["threshold"]
    if actual > t:
        return "A"
    if actual < t:
        return "B"
    return "D" if rec["inputs"].get("bet_type") == "1x2" else None   # None = push


def apply_results(path: str, results: list) -> int:
    """Attach results rows (game_id, subject, market, line, actual) to tracked records."""
    with open(path) as f:
        records = json.load(f)
    index = {(r["game_id"], norm(r["subject"]), r["market"].strip().lower(), float(r["line"])): r
             for r in results if (r.get("actual") or "").strip()}
    n = 0
    for rec in records.values():
        r = index.get((rec["game_id"], norm(rec["subject"]), rec["market"].lower(),
                       float(rec["inputs"]["line"])))
        if r is None:
            continue
        actual = float(r["actual"])
        rec["result"] = {"actual": actual, "winner": _winner(rec, actual)}
        n += 1
    with open(path, "w") as f:
        json.dump(records, f)
    return n


def load_records(directory: str, days: Optional[int] = None, today: Optional[str] = None) -> list:
    paths = sorted(p for p in glob.glob(os.path.join(directory, "*.json"))
                   if re.fullmatch(r"\d{4}-\d{2}-\d{2}\.json", os.path.basename(p)))
    if days is not None:
        end = dt.date.fromisoformat(today) if today else dt.date.today()
        start = end - dt.timedelta(days=days)
        paths = [p for p in paths
                 if start <= dt.date.fromisoformat(os.path.basename(p)[:10]) <= end]
    out = []
    for p in paths:
        with open(p) as f:
            for key, rec in json.load(f).items():
                rec["key"] = key
                out.append(rec)
    return out


def _clip(p: float) -> float:
    return min(max(p, 1e-4), 1 - 1e-4)


def _score(rows: list, w: float) -> tuple:
    ll = br = 0.0
    for m, mk, y in rows:
        p = _clip(w * m + (1 - w) * mk)
        ll -= y * math.log(p) + (1 - y) * math.log(1 - p)
        br += (p - y) ** 2
    return ll / len(rows), br / len(rows)


def best_weight(rows: list) -> tuple:
    grid = [i / 20 for i in range(21)]
    scored = [(w, *_score(rows, w)) for w in grid]
    return min(scored, key=lambda x: x[1]), scored


def calibrate(records: list, min_group: int = 30) -> str:
    """Markdown report: best blend weight overall, by group, plus pick ROI and buckets."""
    graded = [r for r in records if r.get("result") and r["result"]["winner"] in ("A", "B")
              and r.get("model_prob_a") is not None and r.get("market_prob_a") is not None
              and r["inputs"].get("bet_type") != "1x2"]
    out = ["# 🧪 Linemaker Calibration", "",
           f"Tracked markets: **{len(records)}** · graded (two-way, no push): **{len(graded)}**", ""]
    if len(graded) < 10:
        out.append("Not enough graded results yet (need at least 10). Keep tracking and grading.")
        return "\n".join(out) + "\n"

    def rows_of(recs):
        return [(r["model_prob_a"], r["market_prob_a"], 1.0 if r["result"]["winner"] == "A" else 0.0)
                for r in recs]

    (w, ll, br), scored = best_weight(rows_of(graded))
    current = graded[-1].get("model_weight", 0.5)
    cur_ll, cur_br = _score(rows_of(graded), current)
    out += ["## Model weight", "",
            f"- **Best weight: {w:.2f}** (log loss {ll:.4f}, Brier {br:.4f})",
            f"- Current weight {current:.2f}: log loss {cur_ll:.4f}, Brier {cur_br:.4f}",
            f"- Market only (0.00): log loss {scored[0][1]:.4f} · model only (1.00): log loss "
            f"{scored[-1][1]:.4f}", "",
            f"Suggested `.env` line: `LINEMAKER_MODEL_WEIGHT={w:.2f}`", ""]

    fresh = [r for r in graded if r.get("news_fresh")]
    if len(fresh) >= min_group:
        (fw, fll, _), _ = best_weight(rows_of(fresh))
        out += [f"- Fresh-news rows ({len(fresh)}): best weight {fw:.2f} → "
                f"`LINEMAKER_FRESH_WEIGHT={fw:.2f}`", ""]

    groups = {}
    for r in graded:
        groups.setdefault(f"{r['sport']} {r['market']}", []).append(r)
    rows = [(name, len(rs), *best_weight(rows_of(rs))[0]) for name, rs in groups.items()
            if len(rs) >= min_group]
    if rows:
        out += ["## By market (groups with ≥%d results)" % min_group, "",
                "| Market | Graded | Best weight | Log loss |", "|---|---|---|---|"]
        out += [f"| {name} | {n} | {gw:.2f} | {gll:.4f} |"
                for name, n, gw, gll, _ in sorted(rows, key=lambda x: -x[1])]
        out.append("")

    buckets = {}
    for r in graded:
        p = _clip(w * r["model_prob_a"] + (1 - w) * r["market_prob_a"])
        side_p, won = (p, r["result"]["winner"] == "A") if p >= 0.5 else (1 - p, r["result"]["winner"] == "B")
        b = min(int(side_p * 20) * 5, 95)
        buckets.setdefault(b, []).append((side_p, won))
    out += ["## Calibration at the best weight", "",
            "| Predicted | Count | Avg predicted | Actual hit rate |", "|---|---|---|---|"]
    for b in sorted(buckets):
        items = buckets[b]
        out.append(f"| {b}–{b + 5}% | {len(items)} | {sum(p for p, _ in items) / len(items):.1%} | "
                   f"{sum(1 for _, won in items if won) / len(items):.1%} |")
    out.append("")

    out += _roi_tables(graded)

    picks = [r for r in graded if r.get("picked")]
    if picks:
        pl = 0.0
        for r in picks:
            side = r.get("pick_side", r["best_side"])
            odds = next(s["best_odds"] for s in r["sides"] if s["side"] == side)
            pl += net_profit(odds, 1.0) if r["result"]["winner"] == side else -1.0
        out += ["## Picks", "",
                f"- {len(picks)} graded picks · P/L **{pl:+.2f}u** · ROI **{pl / len(picks):+.1%}**", ""]
    return "\n".join(out) + "\n"


def price_history(directory: str, date: str, key: str) -> list:
    """[(iso time, {side: {book: odds}})] for one tracked market, oldest first."""
    path = os.path.join(directory, f"{date}.json")
    if not os.path.exists(path):
        return []
    with open(path) as f:
        rec = json.load(f).get(key)
    if not rec:
        return []
    return [(h["t"], {k: v for k, v in h.items() if k != "t"}) for h in rec.get("history", [])]


def movement(history: list, side: str, book: str) -> Optional[tuple]:
    """(opening odds, current odds) at one book for one side, if it was quoted twice."""
    quotes = [h[1].get(side, {}).get(book) for h in history]
    quotes = [q for q in quotes if q is not None]
    if len(quotes) < 2:
        return None
    return quotes[0], quotes[-1]


ODDS_BANDS = [(-110, -150), (-151, -175), (-176, -200), (-201, -250), (-251, -300), (-301, -10000)]
GAP_BANDS = [(0.0, 0.03), (0.03, 0.06), (0.06, 0.10), (0.10, 0.15), (0.15, 1.0)]


def _flat_bet(r: dict) -> Optional[tuple]:
    """(odds, gap, profit) for a 1-unit bet on the record's best side, if it had a gap."""
    side = r.get("pick_side") or r.get("best_side")
    s = next((x for x in r.get("sides", []) if x["side"] == side), None)
    if not s or s["gap"] <= 0:
        return None
    won = r["result"]["winner"] == side
    return s["best_odds"], s["gap"], net_profit(s["best_odds"], 1.0) if won else -1.0


def _roi_tables(graded: list) -> list:
    """ROI of flat bets on every positive-gap market, by price and by gap size: the evidence
    for LINEMAKER_ODDS_MIN/MAX, MIN_GAP and MAX_GAP."""
    bets = [b for b in (_flat_bet(r) for r in graded) if b]
    if not bets:
        return []

    def table(title, bands, pick, fmt):
        rows = [f"## {title}", "", "| Range | Bets | Won | ROI |", "|---|---|---|---|"]
        for lo, hi in bands:
            inb = [b for b in bets if pick(b, lo, hi)]
            if inb:
                won = sum(1 for b in inb if b[2] > 0)
                roi = sum(b[2] for b in inb) / len(inb)
                rows.append(f"| {fmt(lo, hi)} | {len(inb)} | {won / len(inb):.1%} | {roi:+.1%} |")
        return rows + [""]

    out = table("ROI by price (every positive-gap market, 1u flat)", ODDS_BANDS,
                lambda b, lo, hi: hi <= b[0] <= lo if b[0] < 0 else lo == -110,
                lambda lo, hi: f"{fmt_odds(lo)} to {fmt_odds(hi)}" if hi > -10000 else f"{fmt_odds(lo)} or longer")
    out += table("ROI by gap size", GAP_BANDS, lambda b, lo, hi: lo < b[1] <= hi,
                 lambda lo, hi: f"{lo * 100:.0f}–{hi * 100:.0f} pts" if hi < 1 else f"{lo * 100:.0f}+ pts")
    out += ["Use the bands with enough bets and a positive ROI to set LINEMAKER_ODDS_MIN/MAX "
            "and LINEMAKER_MIN_GAP/MAX_GAP.", ""]
    return out
