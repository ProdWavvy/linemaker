"""Weekly summary: the model's record, closing-line value, your real bets, weight changes."""
import datetime as dt
import os

from .odds import american_to_decimal, fmt_money
from .tracking import clv_summary, load_records


def pick_record(records: list) -> dict:
    """Flat 1-unit results for the model's picks (at the price when picked)."""
    w = l = p = 0
    units = 0.0
    for r in records:
        if not r.get("picked") or not r.get("result") or r.get("pick_odds") is None:
            continue
        winner = r["result"].get("winner")
        if winner is None:
            p += 1
        elif winner == r.get("pick_side", r.get("best_side")):
            w += 1
            units += american_to_decimal(r["pick_odds"]) - 1
        else:
            l += 1
            units -= 1
    n = w + l
    return {"won": w, "lost": l, "push": p, "units": units, "roi": units / n if n else None}


def weekly_lines(today: str) -> list:
    tdir = os.environ.get("LINEMAKER_TRACKING_DIR", "tracking")
    recs = load_records(tdir, 7, today)
    live = load_records(os.path.join(tdir, "live"), 7, today)
    out = []
    pr = pick_record(recs)
    if pr["won"] + pr["lost"]:
        out.append(f"**Model picks:** {pr['won']}-{pr['lost']}" + (f"-{pr['push']}" if pr["push"] else "")
                   + f" · {pr['units']:+.2f} units · ROI {pr['roi']:+.1%} (1 unit each, at the price picked)")
    else:
        out.append("**Model picks:** none graded this week.")
    lr = pick_record(live)
    if lr["won"] + lr["lost"]:
        out.append(f"**Live picks:** {lr['won']}-{lr['lost']} · {lr['units']:+.2f} units")
    c = clv_summary(recs)
    if c["n"]:
        out.append(f"**Closing-line value:** beat the close {c['beat']:.0%} of the time, "
                   f"{c['avg_prob'] * 100:+.2f} pts on average ({c['n']} picks)")
    from .bets import summary
    b = summary(7, today)
    if b["settled"]:
        out.append(f"**Your bets:** {b['won']}-{b['lost']}" + (f"-{b['push']}" if b["push"] else "")
                   + f" · staked ${b['staked']:,.2f} · {fmt_money(b['profit'])}"
                   + (f" · ROI {b['roi']:+.1%}" if b["roi"] is not None else ""))
    if b["open"]:
        out.append(f"**Open bets:** {len(b['open'])}")
    from .autotune import current_summary, load_weights
    start = (dt.date.fromisoformat(today) - dt.timedelta(days=7)).isoformat()
    changes = [c for h in load_weights().get("history", []) if h.get("date", "") >= start
               for c in h.get("changes", [])]
    out.append("**Weights:** " + ("; ".join(changes) if changes else "no changes this week")
               + f"\n{current_summary()}")
    graded = sum(1 for r in recs if r.get("result"))
    working = whats_working(recs)
    if working:
        out.append("🔍 **What's working**\n" + "\n".join(working))
    out.append(f"Tracked {len(recs)} markets this week, {graded} graded.")
    return out


def _flat(r: dict, side: str, odds) -> float:
    w = r["result"].get("winner")
    if w is None or odds is None:
        return 0.0
    return american_to_decimal(odds) - 1 if w == side else -1.0


def whats_working(recs: list, min_n: int = 15) -> list:
    """Which markets and rules are earning, from a window of tracked records."""
    out = []
    picks = [r for r in recs if r.get("picked") and r.get("result") and r.get("pick_odds") is not None]
    by = {}
    for r in picks:
        by.setdefault(f"{r.get('sport')} {r.get('market')}", []).append(
            _flat(r, r.get("pick_side") or r.get("best_side"), r["pick_odds"]))
    ranked = sorted(((k, len(v), sum(v)) for k, v in by.items() if len(v) >= min_n), key=lambda x: -x[2])
    if ranked:
        out.append("**Best markets:** " + " · ".join(f"{k} {u:+.1f}u ({n})" for k, n, u in ranked[:3]))
        worst = [x for x in ranked[::-1][:3] if x[2] < 0]
        if worst:
            out.append("**Worst markets:** " + " · ".join(f"{k} {u:+.1f}u ({n})" for k, n, u in worst))
    kinds = {"Overs": [], "Unders": [], "Game lines": []}
    for r in picks:
        bt = (r.get("inputs") or {}).get("bet_type")
        side = r.get("pick_side") or r.get("best_side")
        kind = "Game lines" if bt != "prop" else ("Overs" if side == "A" else "Unders")
        kinds[kind].append(_flat(r, side, r["pick_odds"]))
    parts = [f"{k} {sum(v):+.1f}u ({len(v)})" for k, v in kinds.items() if v]
    if parts:
        out.append("**By type:** " + " · ".join(parts))
    # The over rule: graded prop overs with a small edge (0 to the over bar) that it kept off the
    # board. If these would have made money, the rule is costing us.
    bar = float(os.environ.get("LINEMAKER_OVER_MIN_GAP", "0.04") or 0) if os.environ.get(
        "LINEMAKER_OVER_MIN_GAP", "0.04").strip().lower() not in ("off", "no", "false") else 0
    if bar:
        skipped, seen = [], set()
        for r in recs:
            if not r.get("result") or (r.get("inputs") or {}).get("bet_type") != "prop" or r.get("picked"):
                continue
            a = next((s for s in r.get("sides", []) if s.get("side") == "A"), None)
            k = (r.get("game_id"), r.get("subject"), r.get("market"))
            if a and 0 < a.get("gap", 0) <= bar and r.get("best_side") == "A" and k not in seen:
                seen.add(k)
                skipped.append(_flat(r, "A", a.get("best_odds")))
        if len(skipped) >= min_n:
            verdict = "the rule is saving money ✅" if sum(skipped) < 0 else "the rule is costing money ⚠️"
            out.append(f"**Over rule:** the small-edge overs it skipped went {sum(skipped):+.1f}u "
                       f"over {len(skipped)}: {verdict}")
    graded = [r for r in recs if r.get("result") and r["result"].get("winner") in ("A", "B")
              and r.get("model_prob_a") is not None and r.get("market_prob_a") is not None]
    sports = sorted({r.get("sport") for r in graded})
    acc = []
    for sp in sports:
        rs = [r for r in graded if r.get("sport") == sp]
        if len(rs) < 50:
            continue
        bm = sum((r["model_prob_a"] - (r["result"]["winner"] == "A")) ** 2 for r in rs) / len(rs)
        bk = sum((r["market_prob_a"] - (r["result"]["winner"] == "A")) ** 2 for r in rs) / len(rs)
        acc.append(f"{sp} {'model ✅' if bm < bk else 'market'}")
    if acc:
        out.append("**More accurate, model or market:** " + " · ".join(acc))
    mk = [_flat(r, r["market_pick"]["side"], r["market_pick"].get("odds"))
          for r in recs if r.get("market_pick") and r.get("result")]
    if mk:
        out.append(f"**⚡ Market-edge picks:** {sum(mk):+.1f}u over {len(mk)}")
    return out
