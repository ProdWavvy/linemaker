"""Freeze projections once lineups are confirmed, then grade them honestly.

Snapshots live in <dir>/<date>.json. A record is written the first time a market is
priced with lineup_confirmed=true and is never overwritten afterwards.
"""
import csv
import datetime as dt
import json
import os
from dataclasses import asdict

from .jsonio import load_or_quarantine, path_lock, save_json
from .devig import no_vig
from .odds import fmt_odds, implied_probability, net_profit, parse_odds
from .sim import SimResult

INPUT_FIELDS = ("mean", "mean_b", "stdev", "dist", "line", "bet_type")


class Freezer:
    def __init__(self, directory: str, date: str):
        self.path = os.path.join(directory, f"{date}.json")
        os.makedirs(directory, exist_ok=True)
        self.records = {}
        if os.path.exists(self.path):
            self.records = load_or_quarantine(self.path)
        self._new = set()

    def projection(self, m, cfg):
        """(SimResult, frozen_at, inputs_changed) for a frozen market, else (None, None, False)."""
        rec = self.records.get(m.key)
        if rec is None:
            return None, None, False
        changed = any(rec["inputs"].get(k) != getattr(m, k) for k in INPUT_FIELDS)
        return SimResult(**rec["sim"]), rec["frozen_at"], changed

    def record(self, ev):
        m = ev.market
        if not m.lineup_confirmed or m.key in self.records:
            return
        best = ev.best
        self.records[m.key] = {
            "frozen_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "sport": m.sport, "game_id": m.game_id, "subject": m.subject, "market": m.market,
            "threshold": m.threshold,
            "inputs": {k: getattr(m, k) for k in INPUT_FIELDS},
            "sim": asdict(ev.sim),
            "model_weight": ev.model_weight,
            "sides": [asdict(s) for s in ev.sides],
            "best_side": best.side, "best_label": best.label,
            "best_odds": best.best_odds, "best_book": best.best_book,
            "blended": best.blended, "gap": best.gap,
            "picked": False,
        }
        self._new.add(m.key)

    def mark_picks(self, picks):
        for ev in picks:
            if ev.market.key in self._new:
                self.records[ev.market.key]["picked"] = True

    def save(self):
        with path_lock(self.path):
            disk = load_or_quarantine(self.path) if os.path.exists(self.path) else {}
            for k in self._new:          # first write wins: never overwrite a frozen record
                disk.setdefault(k, self.records[k])
                if self.records[k].get("picked"):
                    disk[k]["picked"] = True
            self.records = disk
            save_json(self.path, disk)


def grade(snapshot_path: str, results) -> str:
    """Score frozen projections against results (a CSV path or a list of row dicts).
    Results rows: game_id, subject, market,
    line, actual, and optionally close_a_odds / close_b_odds (/ close_d_odds for 1x2) for
    closing-line value. For 1x2, `actual` is subject goals minus opponent goals."""
    with open(snapshot_path) as f:
        records = json.load(f)
    if isinstance(results, str):
        with open(results, newline="") as f:
            results = list(csv.DictReader(f))
    by_key = {}
    for r in results:
        by_key[(r["game_id"], r["subject"], r["market"], float(r["line"]))] = r

    rows, briers, staked, profit, clvs = [], [], 0.0, 0.0, []
    for rec in records.values():
        r = by_key.get((rec["game_id"], rec["subject"], rec["market"], rec["inputs"]["line"]))
        if r is None or not r.get("actual", "").strip():
            continue
        actual = float(r["actual"])
        three_way = rec["inputs"].get("bet_type") == "1x2"
        if actual > rec["threshold"]:
            winner = "A"
        elif actual < rec["threshold"]:
            winner = "B"
        else:
            winner = "D" if three_way else None   # None = push
        side_a = next((s for s in rec["sides"] if s["side"] == "A"), None)
        if side_a and winner is not None:
            briers.append((side_a["model_prob"] - (winner == "A")) ** 2)
        if not rec["picked"]:
            continue
        res = "P" if winner is None else ("W" if winner == rec["best_side"] else "L")
        odds = rec["best_odds"]
        pl = net_profit(odds, 1.0) if res == "W" else (-1.0 if res == "L" else 0.0)
        staked += 1
        profit += pl
        clv = None
        closes = [r.get(f"close_{k}_odds", "").strip() for k in
                  (("a", "d", "b") if three_way else ("a", "b"))]
        if all(closes):
            order = ("A", "D", "B") if three_way else ("A", "B")
            close = no_vig(*[parse_odds(c) for c in closes])[order.index(rec["best_side"])]
            clv = close - implied_probability(odds)
            clvs.append(clv)
        rows.append((rec["best_label"], fmt_odds(odds), rec["blended"], res, pl, clv))

    out = ["# 📈 Grading Report", "", f"Snapshot: `{snapshot_path}`", ""]
    if rows:
        out += ["| Play | Odds | Blended | Result | P/L (units) | CLV |", "|---|---|---|---|---|---|"]
        for label, odds, p, res, pl, clv in rows:
            out.append(f"| {label} | {odds} | {p:.1%} | {res} | {pl:+.2f} | "
                       f"{'—' if clv is None else f'{clv:+.1%}'} |")
        out.append("")
    out.append(f"- Bets graded: {int(staked)} · P/L: {profit:+.2f}u · "
               f"ROI: {(profit / staked if staked else 0):+.1%}")
    if clvs:
        out.append(f"- Avg CLV: {sum(clvs) / len(clvs):+.2%} "
                   f"({sum(c > 0 for c in clvs)}/{len(clvs)} beat the close)")
    if briers:
        out.append(f"- Model Brier score (all frozen markets, n={len(briers)}): "
                   f"{sum(briers) / len(briers):.4f} (0.25 = coin flip; lower is better)")
    return "\n".join(out) + "\n"
