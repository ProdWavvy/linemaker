"""Every auto-parlay the bot posts, logged and graded, so its hit chances can be checked.

Kept apart by phase, league and kind, like the market tracking:
    tracking/parlays/<pre|live>-<SPORT or MULTI>-<props|games|mixed>/<date>.json
(e.g. pre-NBA-props, live-NFL-games). Older days in tracking/parlays/<date>.json are still read.
One entry per distinct parlay (book + legs): style (safest
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


def _root() -> str:
    return os.path.join(os.environ.get("LINEMAKER_TRACKING_DIR", "tracking"), "parlays")


def _path(date: str, segment: Optional[str] = None) -> str:
    return os.path.join(_root(), segment, f"{date}.json") if segment else os.path.join(_root(), f"{date}.json")


def _files(date: str) -> list:
    import glob
    out = glob.glob(os.path.join(_root(), "*", f"{date}.json"))
    legacy = _path(date)
    return sorted(out) + ([legacy] if os.path.exists(legacy) else [])


def _read(path: str) -> dict:
    try:
        from .jsonio import load_json
        return load_json(path)
    except (OSError, ValueError):
        return {}


def _load(date: str) -> dict:
    """Every logged parlay of the day, all segments."""
    out = {}
    for path in _files(date):
        out.update(_read(path))
    return out


def segment_of(legs: list) -> str:
    """pre-NBA-props, live-NFL-games, pre-MULTI-mixed ..."""
    phase = "live" if any(l.get("live") for l in legs) else "pre"
    sports = {(l.get("sport") or "OTHER").upper() for l in legs}
    kinds = {l.get("kind") or "props" for l in legs}
    return f"{phase}-{sports.pop() if len(sports) == 1 else 'MULTI'}-{kinds.pop() if len(kinds) == 1 else 'mixed'}"


def log_parlays(parlays: list, date: str):
    """Remember posted parlays (first sighting wins; re-posts of the same parlay are ignored)."""
    if os.environ.get("LINEMAKER_TRACKING", "on").lower() in ("off", "0", "false", "no"):
        return
    from .jsonio import path_lock, save_json
    from .tracking import kind_of
    seen = set(_load(date))
    new = {}
    for p in parlays:
        legs = [{"key": ev.market.key, "side": ev.best.side, "label": ev.best.label, "odds": o,
                 "date": ev.market.date, "sport": getattr(ev.market, "sport", None),
                 "kind": kind_of(getattr(ev.market, "bet_type", "prop")),
                 "live": getattr(ev, "phase", "pre") == "live"} for ev, o in p.legs]
        pid = hashlib.sha1(json.dumps([p.book] + sorted(f"{l['key']}|{l['side']}" for l in legs)).encode()).hexdigest()[:12]
        if pid in seen:
            continue
        seen.add(pid)
        seg = segment_of(legs)
        new.setdefault(seg, {})[pid] = {
            "book": p.book, "style": p.style, "kind": p.kind, "legs": legs, "segment": seg,
            "prob": round(p.prob, 4), "raw_prob": round(getattr(p, "raw_prob", p.prob), 4),
            "indep_prob": round(p.indep_prob, 4),
            "decimal": round(p.decimal, 4), "listed_decimal": round(getattr(p, "listed_decimal", p.decimal), 4),
            "logged": dt.datetime.now().isoformat(timespec="seconds"),
            "result": None}
    for seg, entries in new.items():
        path = _path(date, seg)
        with path_lock(path):
            data = _read(path)
            data.update({k: v for k, v in entries.items() if k not in data})
            save_json(path, data)


def grade_parlays(date: str) -> int:
    """Settle the day's logged parlays from the tracked leg results. Returns how many settled."""
    from .bets import leg_outcome
    from .jsonio import path_lock, save_json
    cache, total = {}, 0
    for path in _files(date):
        with path_lock(path):
            data = _read(path)
            n = _grade(data, cache, leg_outcome)
            if n:
                save_json(path, data)
        total += n
    return total


def _grade(data: dict, cache: dict, leg_outcome) -> int:
    n = 0
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
            # same-game tickets pay the book's SGP price, below the multiplied odds
            dec *= p.get("decimal", 1.0) / p.get("listed_decimal", p.get("decimal", 1.0) or 1.0)
            p["result"], p["profit"] = "won", round(max(dec, 1.0) - 1, 4)
        p["outcomes"] = outs
        n += 1
    return n


def load_range(days: int, today: Optional[str] = None) -> list:
    end = dt.date.fromisoformat(today) if today else dt.date.today()
    out = []
    for i in range(days + 1):
        out += list(_load((end - dt.timedelta(days=i)).isoformat()).values())
    return out


def segment_label(seg: str) -> str:
    """pre-NBA-props -> 'Pre-game NBA props'."""
    phase, sport, kind = (seg.split("-") + ["", "", ""])[:3]
    kind = {"games": "ML/spreads/totals", "mixed": "props + lines"}.get(kind, kind)
    return f"{'Live' if phase == 'live' else 'Pre-game'} {'multi-league' if sport == 'MULTI' else sport} {kind}"


def summary_lines(parlays: list) -> list:
    """Record, units and predicted vs actual hit rate, by style and by size."""
    done = [p for p in parlays if p.get("result") in ("won", "lost")]
    if not done:
        return []
    groups = {}
    segs = {p.get("segment") or segment_of(p["legs"]) for p in done}
    for p in done:
        style = ("🎯 edge" if p.get("style") == "edge" else "SGP" if p.get("kind") else
                 "💰 value" if p.get("style") == "value" else "🔒 safest")
        seg = segment_label(p.get("segment") or segment_of(p["legs"])) + " · " if len(segs) > 1 else ""
        groups.setdefault(f"{seg}{style} {len(p['legs'])}-leg", []).append(p)
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
