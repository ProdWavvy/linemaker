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
  - Three levels: overall -> sport ("NBA") -> market ("NBA points"). Each is its own best fit
    pulled toward the level above in proportion to its sample (credibility n / (n + 150)):
        sport weight  = overall + n/(n+150) x (sport's best fit - overall)
        market weight = sport   + n/(n+150) x (market's best fit - sport)
    so a thin sport or market can't swing far on a hot or cold week, and a deep one earns its
    own number. A sport and its markets need 75+ outcomes before they get their own weight.
    Sports with their own start (UFC 0.35, or LINEMAKER_<SPORT>_MODEL_WEIGHT) start there.
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
START_PRIORS = ("UFC",)   # sports whose start weight differs from overall: always kept


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
           "markets": dict(current.get("markets", {})), "sports": dict(current.get("sports", {})),
           "history": list(current.get("history", []))}
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
    # Three levels, each pulled toward the one above by its sample size (credibility
    # weighting): weight = parent + n / (n + 150) x (its own best fit - parent). A sport or
    # market with little data stays near its parent; one with lots of data earns its own.
    from .pro import _start_weight
    g = new["model_weight"]
    by_sport, by_market = {}, {}
    for r in graded:
        by_sport.setdefault(r["sport"], []).append(r)
        by_market.setdefault(f"{r['sport']} {r['market']}", []).append(r)
    for sport, recs in sorted(by_sport.items()):
        prior = _start_weight(sport, g)
        n = _outcomes(recs)
        old_s = new["sports"].get(sport, prior)
        if n < MIN_GROUP:
            continue
        (sb, _l, _b), _ = best_weight(_rows(recs))
        target = prior + n / (n + SHRINK) * (sb - prior)
        moved = _step(old_s, target, recs, max_step)
        if abs(moved - g) < 0.025 and sport not in START_PRIORS:
            new["sports"].pop(sport, None)       # same as overall: no special case needed
        else:
            new["sports"][sport] = moved
        if abs(moved - old_s) >= 0.001:
            changes.append(f"{sport}: {old_s:.2f} → {moved:.2f} (best fit {sb:.2f}, {n} outcomes)")
    for name, recs in sorted(by_market.items()):
        n = _outcomes(recs)
        if n < MIN_GROUP:
            continue
        sport = name.split(" ", 1)[0]
        parent = new["sports"].get(sport, _start_weight(sport, g))
        (gb, _l, _b), _ = best_weight(_rows(recs))
        target = parent + n / (n + SHRINK) * (gb - parent)
        old_m = new["markets"].get(name, parent)
        moved = _step(old_m, target, recs, max_step)
        if abs(moved - parent) < 0.025:
            new["markets"].pop(name, None)       # same as its sport: no special case needed
        else:
            new["markets"][name] = moved
        if abs(moved - old_m) >= 0.001:
            changes.append(f"{name}: {old_m:.2f} → {moved:.2f} ({n} outcomes)")
    if changes:
        new["history"] = (new["history"] + [{"date": dt.date.today().isoformat(),
                                              "changes": changes}])[-30:]
    new["graded"] = n_total
    return new, changes


# ---- projection bias ------------------------------------------------------------------
# If a market's projections keep landing high (or low), every price built on them is off in
# the same direction, which no blend weight fixes. Per sport + market ("NBA points"), with one
# outcome per player-stat (alt lines share an actual), the raw projections' bias is
#   ratio = sum(actual) / sum(raw projection)          raw = tracked mean / correction applied
# and the correction becomes 1 + n/(n+150) x (ratio - 1), but only when the ratio is off by
# more than twice its noise (measured game by game) and 1%; it moves at most 0.04 per run and
# stays within 0.80-1.20. New projections are multiplied by it.
BIAS_MIN, BIAS_STEP, BIAS_LOW, BIAS_HIGH = 75, 0.04, 0.80, 1.20


def tune_bias(records: list, current: dict) -> tuple:
    """({"NBA points": 0.97, ...}, [changes]) from graded prop records."""
    one = {}
    for r in records:
        inp = r.get("inputs") or {}
        res = r.get("result") or {}
        if inp.get("bet_type") != "prop" or res.get("actual") is None or not inp.get("mean"):
            continue
        one.setdefault((r.get("game_id"), r.get("subject"), r.get("sport"), r.get("market")), r)
    groups = {}
    for (gid, _s, sport, market), r in one.items():
        raw = float(r["inputs"]["mean"]) / float(r["inputs"].get("mean_bias") or 1.0)
        groups.setdefault(f"{sport} {market}", []).append((gid, raw, float(r["result"]["actual"])))
    bias, changes = dict(current.get("bias", {})), []
    for name, rows in sorted(groups.items()):
        n = len(rows)
        if n < BIAS_MIN:
            continue
        m_sum, a_sum = sum(x[1] for x in rows), sum(x[2] for x in rows)
        if m_sum <= 0:
            continue
        ratio = a_sum / m_sum
        by_game = {}
        for gid, m, a in rows:
            g = by_game.setdefault(gid, [0.0, 0.0])
            g[0] += m
            g[1] += a
        se = math.sqrt(sum((a - ratio * m) ** 2 for m, a in by_game.values())) / m_sum
        old = bias.get(name, 1.0)
        if abs(ratio - 1) < max(0.01, 2 * se):
            target = old if abs(old - 1) > 1e-9 else 1.0   # noise: hold where it is
        else:
            target = 1 + n / (n + SHRINK) * (ratio - 1)
        new = round(min(BIAS_HIGH, max(BIAS_LOW, old + max(-BIAS_STEP, min(BIAS_STEP, target - old)))), 3)
        if abs(new - 1) < 0.005:
            bias.pop(name, None)
        else:
            bias[name] = new
        if abs(new - old) >= 0.001:
            changes.append(f"{name} projections ×{old:.2f} → ×{new:.2f} (actual/projected {ratio:.3f}, "
                           f"{n} outcomes)")
    return bias, changes


# ---- pausing markets that keep losing ---------------------------------------------------
# A market (e.g. "NBA threes") stops producing picks when, over its last PRUNE_MIN+ graded
# model picks, it has lost money (ROI below -3%) AND the picks have closed worse than they
# were taken (average closing-line value below 0). Both together are the signature of a
# market the model reads badly, not of bad luck. It's checked again every grading run and
# comes back once either turns. LINEMAKER_PRUNE=off disables it.
PRUNE_MIN, PRUNE_ROI = 100, -0.03


def tune_prune(records: list) -> tuple:
    """({"NBA threes": reason}, [changes vs before])."""
    if os.environ.get("LINEMAKER_PRUNE", "on").strip().lower() in ("off", "0", "no", "false"):
        return {}, []
    from .reports import pick_record
    from .tracking import pick_clv
    groups = {}
    for r in records:
        if r.get("picked") and r.get("result") and r.get("pick_odds") is not None:
            groups.setdefault(f"{r.get('sport')} {r.get('market')}", []).append(r)
    out = {}
    for name, recs in groups.items():
        pr = pick_record(recs)
        n = pr["won"] + pr["lost"]
        clvs = [c["prob"] for c in (pick_clv(r) for r in recs) if c]
        if n < PRUNE_MIN or len(clvs) < PRUNE_MIN // 2:
            continue
        clv = sum(clvs) / len(clvs)
        if pr["roi"] is not None and pr["roi"] < PRUNE_ROI and clv < 0:
            out[name] = f"{n} picks, ROI {pr['roi']:+.1%}, closing-line value {clv * 100:+.2f} pts"
    return out, []


def pruned(sport: str, market: str) -> Optional[str]:
    return load_weights().get("pruned", {}).get(f"{sport} {market}")


# ---- probability calibration ------------------------------------------------------------
# Are the hit chances honest? Per sport, every graded two-way market's final hit chance for
# side A (after the model/market blend) is fit against what happened:
#   calibrated = 1 / (1 + e^-(a + b x logit(p)))          (Platt scaling)
# b < 1 means the extremes are too confident (a "90%" that hits 86%). It's used where the hit
# chance itself is the product: volume legs, parlays and PrizePicks. Kept only when it beats
# the raw chances on log loss by more than the game-to-game noise, and pulled toward "no
# change" by n / (n + 500) unique outcomes so a thin sample can't bend it far.
CAL_MIN, CAL_SHRINK = 300, 500


def _logit(p: float) -> float:
    p = min(max(p, 1e-4), 1 - 1e-4)
    return math.log(p / (1 - p))


def _platt(xs: list, ys: list) -> tuple:
    """Fit a, b by Newton's method on log loss."""
    a, b = 0.0, 1.0
    for _ in range(25):
        ga = gb = haa = hab = hbb = 0.0
        for x, y in zip(xs, ys):
            p = 1 / (1 + math.exp(-(a + b * x)))
            d, w = p - y, p * (1 - p)
            ga += d
            gb += d * x
            haa += w
            hab += w * x
            hbb += w * x * x
        det = haa * hbb - hab * hab
        if det <= 1e-12:
            break
        da, db = (hbb * ga - hab * gb) / det, (haa * gb - hab * ga) / det
        a, b = a - da, b - db
        if abs(da) + abs(db) < 1e-7:
            break
    return a, b


def tune_calibration(records: list) -> tuple:
    """({"NBA": [a, b, n], ...}, [notes])."""
    by_sport = {}
    for r in records:
        res = r.get("result") or {}
        a = next((s for s in r.get("sides", []) if s.get("side") == "A"), None)
        if not a or res.get("winner") not in ("A", "B") or len(r.get("sides", [])) != 2:
            continue
        by_sport.setdefault(r.get("sport"), []).append(
            (r.get("game_id"), (r.get("game_id"), r.get("subject"), r.get("market")),
             _logit(a["blended"]), 1.0 if res["winner"] == "A" else 0.0))
    out, notes = {}, []
    for sport, rows in sorted(by_sport.items()):
        n = len({u for _g, u, _x, _y in rows})
        if n < CAL_MIN:
            continue
        xs, ys = [x for _g, _u, x, _y in rows], [y for _g, _u, _x, y in rows]
        a, b = _platt(xs, ys)
        w = n / (n + CAL_SHRINK)
        a, b = w * a, 1 + w * (b - 1)

        def loss(x, y, aa, bb):
            p = min(max(1 / (1 + math.exp(-(aa + bb * x))), 1e-4), 1 - 1e-4)
            return -(y * math.log(p) + (1 - y) * math.log(1 - p))
        gains, counts = {}, {}
        for g, _u, x, y in rows:
            gains[g] = gains.get(g, 0.0) + loss(x, y, 0, 1) - loss(x, y, a, b)
            counts[g] = counts.get(g, 0) + 1
        total = len(rows)
        mean = sum(gains.values()) / total
        se = math.sqrt(sum((v - mean * counts[g]) ** 2 for g, v in gains.items())) / total   # by game
        if mean > max(0.0005, 2 * se):
            out[sport] = [round(a, 4), round(b, 4), n]
            notes.append(f"{sport} hit chances recalibrated (a {a:+.2f}, b {b:.2f}; {n} outcomes): "
                         f"a raw 90% now reads {calibrate_p(0.9, a, b):.0%}")
    return out, notes


def calibrate_p(p: float, a: float, b: float) -> float:
    return 1 / (1 + math.exp(-(a + b * _logit(p))))


def calibrated(p: float, sport: str) -> float:
    c = load_weights().get("calibration", {}).get(sport)
    return calibrate_p(p, c[0], c[1]) if c else p


def bias_for(sport: str, market: str) -> float:
    return float(load_weights().get("bias", {}).get(f"{sport} {market}", 1.0))


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
    new["bias"], bias_changes = tune_bias(records, current)
    new["pruned"], _ = tune_prune(records)
    new["calibration"], cal_notes = tune_calibration(records)
    if new["calibration"] != current.get("calibration", {}):
        bias_changes += cal_notes
    before = current.get("pruned", {})
    bias_changes += [f"⏸️ Paused {k}: {v}" for k, v in new["pruned"].items() if k not in before]
    bias_changes += [f"▶️ {k} back on (no longer losing on both counts)" for k in before if k not in new["pruned"]]
    if bias_changes:
        changes = changes + bias_changes
        new["history"] = (new.get("history", []) + [{"date": dt.date.today().isoformat(),
                                                    "changes": bias_changes}])[-30:]
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
    from .jsonio import save_json
    save_json(path, new, indent=2)
    return changes or [f"Weights checked against {new.get('graded', 0)} graded outcomes: no change "
                       "needed (current weights already fit best, or the difference is noise)."]


def current_summary() -> str:
    w = load_weights()
    if not w:
        return "Using .env weights (nothing auto-tuned yet)."
    parts = [f"overall {w.get('model_weight', 0.5):.2f}", f"fresh news {w.get('fresh_weight', 0.8):.2f}"]
    parts += [f"{k} {v:.2f}" for k, v in sorted(w.get("sports", {}).items())]
    parts += [f"{k} {v:.2f}" for k, v in sorted(w.get("markets", {}).items())]
    if w.get("live_weight") is not None:
        parts.append(f"live {w['live_weight']:.2f}")
    out = "Auto-tuned weights: " + ", ".join(parts)
    if w.get("calibration"):
        out += "\nHit-chance calibration (volume, parlays, PrizePicks): " + ", ".join(
            f"{k} 90%→{calibrate_p(0.9, v[0], v[1]):.0%}" for k, v in sorted(w["calibration"].items()))
    if w.get("pruned"):
        out += "\nPaused markets: " + ", ".join(f"{k} ({v})" for k, v in sorted(w["pruned"].items()))
    if w.get("bias"):
        out += "\nProjection corrections: " + ", ".join(f"{k} ×{v:.2f}" for k, v in sorted(w["bias"].items()))
    return out


def tuned(name: str) -> Optional[float]:
    v = load_weights().get(name)
    return float(v) if v is not None else None
