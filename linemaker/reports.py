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
    out.append(f"Tracked {len(recs)} markets this week, {graded} graded.")
    return out
