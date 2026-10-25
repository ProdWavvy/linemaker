"""Every auto-parlay the bot posts, logged and graded, so its hit chances can be checked.

tracking/parlays/<date>.json holds one entry per distinct parlay (book + legs): style (safest
/ value), SGP kind, the legs (tracking key + side + price), the hit chance shown (correlated)
and the independent product, and the payout. Grading reads each leg's result from the day's
tracking file: any lost leg loses it, pushed legs drop out (paid on the rest), and it stays
open until every leg is graded. Reports compare the predicted hit chance with what happened,
which is how the correlation numbers in parlay_math get checked.
"""
import datetime as dt
import hashlib
import json
import os
from typing import Optional

from .odds import american_to_decimal


def _path(date: str) -> str:
    return os.path.join(os.environ.get("LINEMAKER_TRACKING_DIR", "tracking"), "parlays", f"{date}.json")


def _load(date: str) -> dict:
    try:
        with open(_path(date)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save(date: str, data: dict):
    path = _path(date)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    from .jsonio import save_json
    save_json(path, data)


def log_parlays(parlays: list, date: str):
    """Remember posted parlays (first sighting wins; re-posts of the same parlay are ignored)."""
    if os.environ.get("LINEMAKER_TRACKING", "on").lower() in ("off", "0", "false", "no"):
        return
    data = _load(date)
    for p in parlays:
        legs = [{"key": ev.market.key, "side": ev.best.side, "label": ev.best.label, "odds": o,
                 "date": ev.market.date} for ev, o in p.legs]
        pid = hashlib.sha1(json.dumps([p.book] + sorted(f"{l['key']}|{l['side']}" for l in legs)).encode()).hexdigest()[:12]
        if pid in data:
            continue
        data[pid] = {"book": p.book, "style": p.style, "kind": p.kind, "legs": legs,
                     "prob": round(p.prob, 4), "indep_prob": round(p.indep_prob, 4),
                     "decimal": round(p.decimal, 4), "logged": dt.datetime.now().isoformat(timespec="seconds"),
                     "result": None}
    _save(date, data)


def grade_parlays(date: str) -> int:
    """Settle the day's logged parlays from the tracked leg results. Returns how many settled."""
    data = _load(date)
    if not data:
        return 0
    from .bets import leg_outcome
    cache, n = {}, 0
    for p in data.values():
        if p.get("result"):
            continue
        outs = [leg_outcome(l, cache) for l in p["legs"]]
        if "lost" in outs:
            p["result"], p["profit"] = "lost", -1.0
        elif None in outs:
            continue
        elif all(o == "push" for o in outs):
            p["result"], p["profit"] = "push", 0.0
        else:
            dec = 1.0
            for l, o in zip(p["legs"], outs):
                if o == "won":
                    dec *= american_to_decimal(l["odds"])
            p["result"], p["profit"] = "won", round(dec - 1, 4)
        p["outcomes"] = outs
        n += 1
    if n:
        _save(date, data)
    return n


def load_range(days: int, today: Optional[str] = None) -> list:
    end = dt.date.fromisoformat(today) if today else dt.date.today()
    out = []
    for i in range(days + 1):
        out += list(_load((end - dt.timedelta(days=i)).isoformat()).values())
    return out


def summary_lines(parlays: list) -> list:
    """Record, units and predicted vs actual hit rate, by style and by size."""
    done = [p for p in parlays if p.get("result") in ("won", "lost")]
    if not done:
        return []
    groups = {}
    for p in done:
        style = ("SGP" if p.get("kind") else "💰 value" if p.get("style") == "value" else "🔒 safest")
        groups.setdefault(f"{style} {len(p['legs'])}-leg", []).append(p)
    out = []
    for name, ps in sorted(groups.items()):
        won = sum(p["result"] == "won" for p in ps)
        units = sum(p.get("profit", 0) for p in ps)
        pred = sum(p["prob"] for p in ps) / len(ps)
        out.append(f"{name}: {won}-{len(ps) - won} · {units:+.1f}u · hit {won / len(ps):.0%} vs predicted {pred:.0%}")
    won = sum(p["result"] == "won" for p in done)
    pred = sum(p["prob"] for p in done) / len(done)
    ind = sum(p.get("indep_prob", p["prob"]) for p in done) / len(done)
    out.append(f"**All:** {won}/{len(done)} hit ({won / len(done):.0%}) · predicted {pred:.0%} "
               f"(independent-legs math would say {ind:.0%}) · {sum(p.get('profit', 0) for p in done):+.1f}u at 1u each")
    return out
