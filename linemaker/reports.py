"""Weekly summary: the model's record, closing-line value, your real bets, weight changes."""
import datetime as dt
import os
from typing import Optional

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


def _units(u: float) -> str:
    return f"{'🟢' if u > 0 else '🔴' if u < 0 else '⚪'} {u:+.1f}u"


def weekly_lines(today: str, details: bool = False) -> list:
    """The week in a few short lines: picks, segments, closing-line value, your bets, what the
    tuner paused or brought back, auto-parlays, what's working. details=True adds every tuned
    weight (/weekly details:true)."""
    tdir = os.environ.get("LINEMAKER_TRACKING_DIR", "tracking")
    recs = load_records(tdir, 7, today, lean=True)
    live = load_records(os.path.join(tdir, "live"), 7, today, lean=True)
    out = []
    pr, lr = pick_record(recs), pick_record(live)
    picks = []
    if pr["won"] + pr["lost"]:
        picks.append(f"Pre-game **{pr['won']}-{pr['lost']}" + (f"-{pr['push']}" if pr["push"] else "")
                     + f"** {_units(pr['units'])} ({pr['roi']:+.1%})")
    if lr["won"] + lr["lost"]:
        picks.append(f"Live **{lr['won']}-{lr['lost']}** {_units(lr['units'])}")
    out.append("🎯 **Picks:** " + (" · ".join(picks) if picks else "none graded this week")
               + "  (1 unit each)")
    per = []
    for phase, rs in (("", recs), ("Live ", live)):
        segs = {}
        for r in rs:
            segs.setdefault(r.get("segment", "?"), []).append(r)
        for seg, srs in segs.items():
            sr = pick_record(srs)
            if sr["won"] + sr["lost"]:
                sport, kind = (seg.rsplit("-", 1) + [""])[:2]
                per.append((sr["units"], f"{phase}{sport} {'lines' if kind == 'games' else 'props'} "
                                         f"{sr['won']}-{sr['lost']} {_units(sr['units'])}"))
    if len(per) > 1:
        out.append("📊 **By segment**\n" + "\n".join(t for _u, t in sorted(per, key=lambda x: -x[0])))
    c = clv_summary(recs)
    if c["n"]:
        verdict = (" · ✅ getting better prices than the close" if c["beat"] >= 0.55 else
                   " · ⚠️ the market moves against these picks after they're made" if c["beat"] < 0.45 else "")
        out.append(f"📉 **Closing line:** beat it {c['beat']:.0%} · {c['avg_prob'] * 100:+.2f} pts "
                   f"({c['n']} picks){verdict}")
    from .bets import per_user_on, summary, viewer
    who = viewer()   # /weekly: the caller's bets; the Monday post: everyone's
    b = summary(7, today, user=who)
    if b["settled"]:
        out.append(f"💵 **{'Your' if who or not per_user_on() else 'Everyone' + chr(39) + 's'} bets:** "
                   f"{b['won']}-{b['lost']}" + (f"-{b['push']}" if b["push"] else "")
                   + f" · ${b['staked']:,.2f} staked · {fmt_money(b['profit'])}"
                   + (f" ({b['roi']:+.1%})" if b["roi"] is not None else "")
                   + (f" · {len(b['open'])} open" if b["open"] else ""))
    from .autotune import current_summary, load_weights, manual_pauses, SEGMENTS, SEGMENT_LABELS
    w = load_weights()
    start = (dt.date.fromisoformat(today) - dt.timedelta(days=7)).isoformat()
    week = [h for h in w.get("history", []) if h.get("date", "") >= start]
    paused = sorted({k for n in SEGMENTS for k in (w.get("segments", {}).get(n, {}).get("pruned") or {})})
    lifted = w.get("manual_lifted") or {}
    paused += [f"{m} (by you)" for m in sorted(manual_pauses()) if m not in lifted]
    back = []
    for h in week:
        for ch in h.get("changes", []):
            if "▶️" in ch:
                back.append(ch.split("▶️", 1)[1].strip().split(" back on")[0].strip())
    tune = [f"{len(week)} re-fit{'s' if len(week) != 1 else ''} this week"]
    if paused:
        tune.append("⏸️ paused: " + ", ".join(paused))
    if back:
        tune.append("▶️ back: " + ", ".join(dict.fromkeys(back)))
    out.append("🔧 **Tuning:** " + " · ".join(tune) + ("" if details else "  (`/weekly details:true` for every weight)"))
    if details:
        latest = [c for c in (week[-1].get("changes", []) if week else []) if not c.startswith("Still collecting")]
        if latest:
            out.append("**Latest re-fit:** " + "; ".join(latest)[:1500])
        out.append(current_summary()[:1500])
    from .parlay_track import load_range, summary_lines
    parlay_lines = summary_lines(load_range(7, today))
    if parlay_lines:
        out.append("🎰 **Auto-parlays**\n" + "\n".join(parlay_lines[:6]))
    working = whats_working(recs)
    if working:
        out.append("🔍 **What's working**\n" + "\n".join(working[:6]))
    graded = sum(1 for r in recs if r.get("result"))
    out.append(f"_Tracked {len(recs):,} markets this week, {graded:,} graded._")
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


# ---- all-time record ------------------------------------------------------------------

_PER_PICK = {"$": None}   # dollars a pick for the all-time lines (alltime_sections sets it)


def _money_units(u: float) -> str:
    per = _PER_PICK["$"]
    if not per:
        return f"{u:+.1f}u"
    d = u * per
    return f"**{'+' if d >= 0 else '-'}${abs(d):,.0f}** ({u:+.1f}u)"


def _rec_line(name: str, pr: dict) -> str:
    n = pr["won"] + pr["lost"]
    return (f"{name}: **{pr['won']}-{pr['lost']}" + (f"-{pr['push']}" if pr["push"] else "") + "** · "
            f"{_money_units(pr['units'])}" + (f" · ROI {pr['roi']:+.1%}" if pr["roi"] is not None else "")
            + (f" · hit {pr['won'] / n:.0%}" if n else ""))


def alltime_sections(today: str, per_pick: Optional[float] = None) -> list:
    """[(title, [lines])]: every graded model pick the bot has tracked (1 unit each, at the
    price when picked): totals per phase, per sport, per market, overs vs unders, plus the
    market-edge, paused-market (shadow) and auto-parlay records and your own bets."""
    from .autotune import _as_pick
    _PER_PICK["$"] = per_pick
    tdir = os.environ.get("LINEMAKER_TRACKING_DIR", "tracking")
    pre = load_records(tdir, None, today, graded_only=True, lean=True)
    live = load_records(os.path.join(tdir, "live"), None, today, graded_only=True, lean=True)
    pp = load_records(os.path.join(tdir, "pp"), None, today, graded_only=True, lean=True)
    picks = [r for r in pre if r.get("picked")]
    days = sorted({r["key"][:10] for r in pre if r.get("key")})
    span = f"{days[0]} to {days[-1]} ({len(days)} days)" if days else "no graded days yet"
    one = {}
    for r in picks:   # one per player per game: the rule picks follow now
        k = (r.get("game_id"), r.get("subject"))
        if k not in one or (r.get("pick_time") or "") < (one[k].get("pick_time") or ""):
            one[k] = r
    unit = f"${per_pick:g} a pick (1 unit)" if per_pick else "1 unit a pick"
    out = [("📊 Totals", [f"Graded {span}. {unit}, at the price when picked.",
                         _rec_line("Pre-game picks", pick_record(picks)),
                         _rec_line("↳ one per player per game (today's rule)", pick_record(list(one.values()))),
                         _rec_line("Live picks", pick_record([r for r in live if r.get("picked")])),
                         _rec_line("PrizePicks picks", pick_record([r for r in pp if r.get("picked")]))])]
    by_sport = {}
    for r in picks:
        by_sport.setdefault(r.get("sport", "?"), []).append(r)
    out.append(("🏟️ By Sport (pre-game)", [_rec_line(s, pick_record(rs)) for s, rs in
                                          sorted(by_sport.items(), key=lambda kv: -pick_record(kv[1])["units"])]))
    by_market = {}
    for r in picks:
        by_market.setdefault(f"{r.get('sport')} {r.get('market')}", []).append(r)
    big = sorted(((k, pick_record(v)) for k, v in by_market.items() if len(v) >= 10), key=lambda kv: -kv[1]["units"])
    rest = [r for k, v in by_market.items() if len(v) < 10 for r in v]
    lines = [_rec_line(k, pr) for k, pr in big]
    if rest:
        lines.append(_rec_line(f"{len(by_market) - len(big)} smaller markets (under 10 picks)", pick_record(rest)))
    out.append(("📈 By Market (pre-game, best first)", lines))
    sides = {"Overs": [], "Unders": []}
    for r in picks:
        if (r.get("inputs") or {}).get("bet_type") == "prop":
            sides["Overs" if r.get("pick_side") == "A" else "Unders"].append(r)
    other = [_rec_line(k, pick_record(v)) for k, v in sides.items() if v]
    edges = [dict(r, picked=True, pick_side=r["market_pick"]["side"], pick_odds=r["market_pick"].get("odds"))
             for r in pre if r.get("market_pick") and r["market_pick"].get("odds") is not None]
    if edges:
        other.append(_rec_line("⚡ Market-edge picks", pick_record(edges)))
    shadows = [_as_pick(r) for r in pre if r.get("shadow_pick") and r["shadow_pick"].get("odds") is not None]
    if shadows:
        other.append(_rec_line("⏸️ Paused markets' shadow picks", pick_record(shadows)))
    try:
        from .parlay_track import load_range
        parlays = [p for p in load_range(len(days) + 1 if days else 0, today) if p.get("result") in ("won", "lost")]
        if parlays:
            won = sum(p["result"] == "won" for p in parlays)
            other.append(f"🎰 Auto-parlays: **{won}-{len(parlays) - won}** · "
                         f"{sum(p.get('profit', 0) for p in parlays):+.1f}u (1u each)")
    except Exception:
        pass
    try:
        from .bets import per_user_on, summary, viewer
        who = viewer()
        s = summary(None, today, user=who)
        if s["settled"]:
            other.append(f"🧾 {'Your' if who or not per_user_on() else 'Everyone' + chr(39) + 's'} bets: **{s['won']}-{s['lost']}** · ${s['staked']:,.2f} staked · "
                         f"{s['profit']:+,.2f}" + (f" · ROI {s['roi']:+.1%}" if s["roi"] is not None else "")
                         + " (`/record` for more)")
    except Exception:
        pass
    if other:
        out.append(("🔎 Everything Else", other))
    return out
