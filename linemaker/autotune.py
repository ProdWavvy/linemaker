"""Automatic weight tuning from graded results, with guardrails.

After every grading run the bot re-fits the model-vs-market blend weight on the last
LINEMAKER_AUTOTUNE_DAYS (default 60) days of graded markets and saves it to
<tracking>/weights.json, which every new card reads (no restart). Guardrails:

  - Nothing moves until there are LINEMAKER_AUTOTUNE_MIN (default 200) graded outcomes (a player's
    alt lines on one stat count once), and a market (e.g. "NBA points") gets its own weight
    only at 75+.
  - A weight only moves when the better weight beats the current one on log loss by more
    than twice the noise (standard error measured game by game, since one blowout moves many
    props together) and by at least 0.002, and by at most LINEMAKER_AUTOTUNE_STEP (default
    0.10) per run.
  - Per-market weights are pulled toward the overall weight in proportion to their sample
    (n / (n + 150)), so a small market can't swing far on a hot or cold week.
  - Weights stay between 0.05 and 0.95.

LINEMAKER_AUTOTUNE=off freezes everything at the .env values. In-game (live) pricing keeps
its own LINEMAKER_LIVE_MODEL_WEIGHT.
"""
import datetime as dt
import json
import math
import os
from typing import Optional

from .tracking import best_weight, load_records

MIN_GROUP = 75
SHRINK = 150
MIN_GAIN = 0.002
LOW, HIGH = 0.05, 0.95


def autotune_on() -> bool:
    return os.environ.get("LINEMAKER_AUTOTUNE", "on").strip().lower() not in ("off", "0", "no", "false")


def weights_path() -> str:
    return os.environ.get("LINEMAKER_WEIGHTS_FILE") or os.path.join(
        os.environ.get("LINEMAKER_TRACKING_DIR", "tracking"), "weights.json")


_CACHE = {"mtime": None, "data": {}}


def load_weights() -> dict:
    """The saved weights ({} when auto-tuning is off or nothing has been tuned yet)."""
    if not autotune_on():
        return {}
    path = weights_path()
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {}
    if _CACHE["mtime"] != mtime:
        try:
            with open(path) as f:
                _CACHE["data"] = json.load(f)
        except (OSError, ValueError):
            _CACHE["data"] = {}
        _CACHE["mtime"] = mtime
    return _CACHE["data"]


def _env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def _graded(records: list) -> list:
    return [r for r in records if r.get("result") and r["result"].get("winner") in ("A", "B")
            and r.get("model_prob_a") is not None and r.get("market_prob_a") is not None
            and r.get("inputs", {}).get("bet_type") != "1x2"]


def _rows(recs: list) -> list:
    return [(r["model_prob_a"], r["market_prob_a"], 1.0 if r["result"]["winner"] == "A" else 0.0)
            for r in recs]


def _outcomes(recs: list) -> int:
    """Distinct outcomes: a player's ten alt lines on one stat are one result, not ten."""
    return len({(r.get("game_id"), r.get("subject"), r.get("market")) for r in recs}) or len(recs)


def _loss(m: float, mk: float, y: float, w: float) -> float:
    p = min(max(w * m + (1 - w) * mk, 1e-4), 1 - 1e-4)
    return -(y * math.log(p) + (1 - y) * math.log(1 - p))


def clearly_better(recs: list, current: float, target: float) -> bool:
    """Does `target` beat `current` on log loss by more than the noise? The noise is measured
    game by game (one blowout moves many props together), and the gain must exceed both
    MIN_GAIN and two standard errors."""
    by_game, n = {}, 0
    for r, (m, mk, y) in zip(recs, _rows(recs)):
        d = _loss(m, mk, y, current) - _loss(m, mk, y, target)
        g = by_game.setdefault(r.get("game_id") or id(r), [0.0, 0])
        g[0] += d
        g[1] += 1
        n += 1
    if n == 0 or len(by_game) < 2:
        return False
    mean = sum(s for s, _k in by_game.values()) / n
    se = math.sqrt(sum((s - k * mean) ** 2 for s, k in by_game.values())) / n
    return mean > max(MIN_GAIN, 2 * se)


def _step(current: float, target: float, recs: list, max_step: float) -> float:
    """Move toward target only if it scores clearly better than noise, and only so far."""
    if abs(target - current) < 1e-9 or not clearly_better(recs, current, target):
        return current
    move = max(-max_step, min(max_step, target - current))
    return round(min(HIGH, max(LOW, current + move)), 3)


def tune(records: list, current: dict) -> tuple:
    """(new weights dict, [change descriptions]) from graded records."""
    graded = _graded(records)
    min_total = int(_env("LINEMAKER_AUTOTUNE_MIN", 200))
    max_step = _env("LINEMAKER_AUTOTUNE_STEP", 0.10)
    new = {"model_weight": current.get("model_weight", _env("LINEMAKER_MODEL_WEIGHT", 0.5)),
           "fresh_weight": current.get("fresh_weight", _env("LINEMAKER_FRESH_WEIGHT", 0.8)),
           "markets": dict(current.get("markets", {})), "history": list(current.get("history", []))}
    n_total = _outcomes(graded)
    if n_total < min_total:
        return new, [f"Waiting for {min_total} graded outcomes before tuning (have {n_total}; "
                     "alt lines on the same stat count once)."]
    changes = []
    rows = _rows(graded)
    (best, _ll, _br), _ = best_weight(rows)
    old = new["model_weight"]
    new["model_weight"] = _step(old, best, graded, max_step)
    if new["model_weight"] != old:
        changes.append(f"Overall weight {old:.2f} → {new['model_weight']:.2f} (best fit {best:.2f}, "
                       f"{n_total} outcomes)")
    fresh = [r for r in graded if r.get("news_fresh")]
    if _outcomes(fresh) >= MIN_GROUP:
        fr = _rows(fresh)
        (fb, _l, _b), _ = best_weight(fr)
        old_f = new["fresh_weight"]
        new["fresh_weight"] = _step(old_f, fb, fresh, max_step)
        if new["fresh_weight"] != old_f:
            changes.append(f"Fresh-news weight {old_f:.2f} → {new['fresh_weight']:.2f}")
    groups = {}
    for r in graded:
        groups.setdefault(f"{r['sport']} {r['market']}", []).append(r)
    g = new["model_weight"]
    for name, recs in sorted(groups.items()):
        n = _outcomes(recs)
        if n < MIN_GROUP:
            continue
        gr = _rows(recs)
        (gb, _l, _b), _ = best_weight(gr)
        target = g + n / (n + SHRINK) * (gb - g)
        old_m = new["markets"].get(name, g)
        moved = _step(old_m, target, recs, max_step)
        if abs(moved - g) < 0.025:
            new["markets"].pop(name, None)       # same as overall: no special case needed
        else:
            new["markets"][name] = moved
        if abs(moved - old_m) >= 0.001:
            changes.append(f"{name}: {old_m:.2f} → {moved:.2f} ({n} outcomes)")
    if changes:
        new["history"] = (new["history"] + [{"date": dt.date.today().isoformat(),
                                              "changes": changes}])[-30:]
    new["graded"] = n_total
    return new, changes


def tune_live(records: list, current: dict) -> tuple:
    """(live weight, [changes]): the in-game blend, fit on graded live reads only."""
    graded = _graded(records)
    old = current.get("live_weight", _env("LINEMAKER_LIVE_MODEL_WEIGHT", 0.35))
    min_total = int(_env("LINEMAKER_AUTOTUNE_MIN", 200))
    n = _outcomes(graded)
    if n < min_total:
        return old, []
    (best, _ll, _br), _ = best_weight(_rows(graded))
    new = _step(old, best, graded, _env("LINEMAKER_AUTOTUNE_STEP", 0.10))
    return new, ([f"Live weight {old:.2f} → {new:.2f} ({n} live outcomes)"] if new != old else [])


def run_autotune(tracking_dir: str, today: str) -> list:
    """Re-fit and save. Returns lines describing what changed (or why nothing did)."""
    if not autotune_on():
        return ["Auto-tuning is off (LINEMAKER_AUTOTUNE=off); weights stay as set in .env."]
    days = int(_env("LINEMAKER_AUTOTUNE_DAYS", 60))
    records = load_records(tracking_dir, days, today)
    current = load_weights()
    new, changes = tune(records, current)
    live_w, live_changes = tune_live(load_records(os.path.join(tracking_dir, "live"), days, today),
                                     current)
    new["live_weight"] = live_w
    if live_changes:
        changes = changes + live_changes
        new["history"] = (new.get("history", []) + [{"date": dt.date.today().isoformat(),
                                                    "changes": live_changes}])[-30:]
    new["updated"] = dt.datetime.now().isoformat(timespec="seconds")
    path = weights_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(new, f, indent=2)
    os.replace(tmp, path)
    return changes or [f"Weights checked against {new.get('graded', 0)} graded outcomes: no change "
                       "needed (current weights already fit best, or the difference is noise)."]


def current_summary() -> str:
    w = load_weights()
    if not w:
        return "Using .env weights (nothing auto-tuned yet)."
    parts = [f"overall {w.get('model_weight', 0.5):.2f}", f"fresh news {w.get('fresh_weight', 0.8):.2f}"]
    parts += [f"{k} {v:.2f}" for k, v in sorted(w.get("markets", {}).items())]
    if w.get("live_weight") is not None:
        parts.append(f"live {w['live_weight']:.2f}")
    return "Auto-tuned weights: " + ", ".join(parts)


def tuned(name: str) -> Optional[float]:
    v = load_weights().get(name)
    return float(v) if v is not None else None
