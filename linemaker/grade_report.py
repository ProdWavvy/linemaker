"""The /grade report: what was graded, how the model's picks and every other market did,
your bets, weight changes and anything worth knowing. Plus a CSV of every graded market.

Reads the tracking files after grading (tracking/<date>.json, live/, pp/), so it reports
on everything the bot priced that day, not only the picks.
"""
import csv
import io
import os
from typing import Optional

import discord

from .odds import american_to_decimal, fmt_money, fmt_odds

GOLD, GREEN, RED, BLUE, PURPLE, GREY = 0xF1C40F, 0x2ECC71, 0xE74C3C, 0x3498DB, 0x9B59B6, 0x95A5A6
BUCKETS = ((0.5, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 1.01))


def _tdir() -> str:
    return os.environ.get("LINEMAKER_TRACKING_DIR", "tracking")


def _load(path: str) -> dict:
    import json
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def day_records(date: str) -> dict:
    """{"main": {...}, "live": {...}, "pp": {...}} tracking records for the date."""
    t = _tdir()
    return {"main": _load(os.path.join(t, f"{date}.json")),
            "live": _load(os.path.join(t, "live", f"{date}.json")),
            "pp": _load(os.path.join(t, "pp", f"{date}.json"))}


def graded_keys(records: dict) -> set:
    return {(kind, k) for kind, recs in records.items() for k, r in recs.items() if r.get("result")}


def _side(rec: dict, side: Optional[str]) -> Optional[dict]:
    return next((s for s in rec.get("sides", []) if s.get("side") == side), None)


def _favored(rec: dict) -> Optional[dict]:
    sides = rec.get("sides") or []
    return max(sides, key=lambda s: s.get("blended", 0)) if sides else None


def _outcome(rec: dict, side: str) -> str:
    w = rec["result"].get("winner")
    return "push" if w is None else ("hit" if w == side else "miss")


def _is_prop(rec: dict) -> bool:
    return (rec.get("inputs") or {}).get("bet_type") == "prop"


def _line(rec: dict) -> float:
    return float((rec.get("inputs") or {}).get("line") or 0)


def _miss_by(rec: dict, side: str) -> str:
    """'scored 31 (line 24.5)' style result text."""
    actual = rec["result"].get("actual")
    if actual is None:
        return ""
    bt = (rec.get("inputs") or {}).get("bet_type")
    if bt in ("moneyline", "spread"):
        return f"final margin {actual:+g}"
    return f"{actual:g} vs line {_line(rec):g}"


def _record_line(rows: list) -> tuple:
    """rows: [(outcome, odds)] -> ('W-L-P', units, roi)."""
    w = sum(o == "hit" for o, _ in rows)
    l = sum(o == "miss" for o, _ in rows)
    p = sum(o == "push" for o, _ in rows)
    units = sum((american_to_decimal(odds) - 1) if o == "hit" else (-1 if o == "miss" else 0)
                for o, odds in rows if odds is not None)
    n = w + l
    return f"{w}-{l}" + (f"-{p}" if p else ""), units, (units / n if n else None)


def _pct(x: Optional[float]) -> str:
    return "—" if x is None else f"{x:.0%}"


def picks_embed(main: dict) -> Optional[discord.Embed]:
    picks = [r for r in main.values() if r.get("picked") and r.get("result")]
    if not picks:
        return None
    rows, lines = [], []
    picks.sort(key=lambda r: (r.get("sport", ""), r.get("game", "")))
    for r in picks:
        side = r.get("pick_side") or r.get("best_side")
        s = _side(r, side) or {}
        odds = r.get("pick_odds", s.get("best_odds"))
        o = _outcome(r, side)
        rows.append((o, odds))
        mark = {"hit": "✅", "miss": "❌", "push": "➖"}[o]
        lines.append(f"{mark} {s.get('label', r.get('subject'))} {fmt_odds(odds) if odds else ''}"
                     f" · {_pct(s.get('blended'))} · {_miss_by(r, side)}")
    rec, units, roi = _record_line(rows)
    head = (f"**{rec}** · {units:+.2f} units" + (f" · ROI **{roi:+.1%}**" if roi is not None else "")
            + " (1 unit each, at the price when picked)")
    body = "\n".join(lines)
    if len(body) > 3600:
        body = body[:3600].rsplit("\n", 1)[0] + f"\n…and more in the attached CSV"
    return discord.Embed(title="🎯 The Model's Picks", description=head + "\n\n" + body,
                         color=GREEN if units >= 0 else RED)


def model_embed(main: dict) -> Optional[discord.Embed]:
    """How the model read every two-way market, picks or not."""
    graded = [r for r in main.values() if r.get("result") and r["result"].get("winner") in ("A", "B")
              and r.get("model_prob_a") is not None and r.get("market_prob_a") is not None]
    if not graded:
        return None
    lines = []
    fav = [(_favored(r), r) for r in graded]
    fav = [(s, r) for s, r in fav if s]
    hits = sum(r["result"]["winner"] == s["side"] for s, r in fav)
    exp = sum(s["blended"] for s, _r in fav)
    lines.append(f"**Likelier side hit {hits}/{len(fav)} ({hits / len(fav):.0%})** · the model "
                 f"expected {exp / len(fav):.0%}")

    def brier(get):
        return sum((get(r) - (r["result"]["winner"] == "A")) ** 2 for r in graded) / len(graded)
    b_model = brier(lambda r: r["model_prob_a"])
    b_mkt = brier(lambda r: r["market_prob_a"])
    b_blend = brier(lambda r: (_side(r, "A") or {}).get("blended", r["market_prob_a"]))
    better = b_model < b_mkt
    lines.append(f"**Accuracy (Brier, lower is better):** model {b_model:.4f} · market "
                 f"{b_mkt:.4f} · blend {b_blend:.4f} → the model "
                 + ("**beat** the market today" if better else "**trailed** the market today"))
    lines.append("")
    lines.append("**Calibration** (when it said X%, how often it hit):")
    for lo, hi in BUCKETS:
        inb = [(s, r) for s, r in fav if lo <= s["blended"] < hi]
        if inb:
            h = sum(r["result"]["winner"] == s["side"] for s, r in inb)
            avg = sum(s["blended"] for s, _r in inb) / len(inb)
            label = f"{lo:.0%}–{min(hi, 1):.0%}" if hi <= 1 else f"{lo:.0%}+"
            lines.append(f"• {label}: said {avg:.0%}, hit **{h / len(inb):.0%}** ({h}/{len(inb)})")
    by = {}
    for s, r in fav:
        key = f"{r.get('sport', '')} {r.get('market', '')}".strip()
        by.setdefault(key, []).append(r["result"]["winner"] == s["side"])
    top = sorted(by.items(), key=lambda kv: -len(kv[1]))[:8]
    if top:
        lines.append("")
        lines.append("**By market** (likelier side):")
        lines += [f"• {k}: {sum(v)}/{len(v)} ({sum(v) / len(v):.0%})" for k, v in top]
    value = []
    for r in graded:
        s = _side(r, r.get("best_side"))
        if s and s.get("gap", 0) > 0:
            value.append((_outcome(r, s["side"]), s.get("best_odds")))
    if value:
        rec, units, roi = _record_line(value)
        lines.append("")
        lines.append(f"**Every positive-gap side** (picks or not, 1u flat): {rec} · {units:+.1f}u"
                     + (f" · ROI {roi:+.1%}" if roi is not None else ""))
    return discord.Embed(title=f"🧠 How the Model Read Everything ({len(graded)} markets)",
                         description="\n".join(lines)[:4000], color=BLUE)


def side_book_embed(records: dict) -> Optional[discord.Embed]:
    """Live reads and PrizePicks lines, briefly."""
    lines = []
    for kind, title in (("live", "🔴 Live reads"), ("pp", "🟣 PrizePicks lines")):
        recs = [r for r in records[kind].values() if r.get("result")]
        if not recs:
            continue
        picks = [r for r in recs if r.get("picked")]
        fav = [(_favored(r), r) for r in recs]
        fav = [(s, r) for s, r in fav if s and r["result"].get("winner") in ("A", "B")]
        hit = sum(r["result"]["winner"] == s["side"] for s, r in fav)
        line = f"**{title}:** {len(recs)} graded · likelier side hit {_pct(hit / len(fav) if fav else None)}"
        if picks:
            rows = [(_outcome(r, r.get("pick_side") or r.get("best_side")),
                     r.get("pick_odds") if kind == "live" else None) for r in picks]
            rec, units, _roi = _record_line(rows)
            line += f" · picks {rec}" + (f" ({units:+.2f}u)" if kind == "live" else "")
        lines.append(line)
    if not lines:
        return None
    return discord.Embed(title="📡 Live & PrizePicks", description="\n".join(lines), color=PURPLE)


def bets_embed(date: str, settled_now: int) -> Optional[discord.Embed]:
    from .bets import load_bets
    mine = [b for b in load_bets() if any(l.get("date") == date for l in b.get("legs", []))]
    if not mine:
        return None
    lines = []
    done = [b for b in mine if b["status"] != "open"]
    profit = sum(b.get("profit", 0) for b in done)
    staked = sum(b["stake"] for b in done)
    if done:
        w = sum(b["status"] == "won" for b in done)
        l = sum(b["status"] == "lost" for b in done)
        lines.append(f"**{w}-{l}** · staked ${staked:,.2f} · **{fmt_money(profit)}**"
                     + (f" · settled {settled_now} just now" if settled_now else ""))
    for b in done[:12]:
        mark = {"won": "✅", "lost": "❌", "push": "➖"}[b["status"]]
        what = b["legs"][0]["label"] if b["kind"] == "single" else f"{len(b['legs'])}-leg {b['kind']}"
        legs = ""
        if b["kind"] != "single":
            legs = " (" + " ".join({"won": "✅", "lost": "❌", "push": "➖"}.get(l.get("outcome"), "⏳")
                                   for l in b["legs"]) + ")"
        lines.append(f"{mark} {what} {fmt_odds(b['odds'])} · ${b['stake']:,.2f} → "
                     f"{fmt_money(b.get('profit', 0))}{legs}")
    still = [b for b in mine if b["status"] == "open"]
    if still:
        lines.append("")
        lines.append(f"⏳ **Still open ({len(still)}):** " + "; ".join(
            (b["legs"][0]["label"] if b["kind"] == "single" else f"{len(b['legs'])}-leg {b['kind']}")
            for b in still[:6]) + " (a game isn't final, or a leg wasn't tracked)")
    return discord.Embed(title="🧾 Your Bets", description="\n".join(lines)[:4000],
                         color=GREEN if profit >= 0 else RED)


def notes_embed(date: str, records: dict, summary: list, newly: int, remaining) -> discord.Embed:
    main = records["main"]
    lines = []
    tuning = [l.split("⚖️", 1)[1].strip() for l in summary if "⚖️" in l]
    lines.append("**⚖️ Weights:** " + ("; ".join(tuning) if tuning
                                      else "not re-checked (nothing new graded)"))
    from .autotune import current_summary
    lines.append(current_summary())
    from .tracking import clv_summary
    c = clv_summary(list(main.values()))
    if c["n"]:
        lines.append(f"**📉 Closing-line value:** beat the close {c['beat']:.0%} of the time, "
                     f"{c['avg_prob'] * 100:+.2f} pts on average ({c['n']} picks). Beating the "
                     "close is the best early sign the edges are real.")
    lines.append("")
    pending = {}
    for r in main.values():
        if not r.get("result"):
            pending.setdefault(r.get("game", "?"), 0)
            pending[r.get("game", "?")] += 1
    if pending:
        games = sorted(pending, key=lambda g: -pending[g])
        n, g = sum(pending.values()), len(pending)
        lines.append(f"⏳ **Not graded yet ({n} market{'s' if n != 1 else ''}, {g} game{'s' if g != 1 else ''}):** "
                     + ", ".join(games[:6]) + ("…" if len(games) > 6 else "")
                     + ". Usually a game that isn't final, or a player who didn't play. "
                     "Run /grade again later and they'll be picked up.")
    pushes = [r for r in main.values() if r.get("result") and r["result"].get("winner") is None]
    if pushes:
        lines.append(f"➖ **Pushes:** {len(pushes)} (landed exactly on the line).")
    misses = []
    for r in main.values():
        if r.get("picked") and r.get("result") and r["result"].get("winner") in ("A", "B"):
            side = r.get("pick_side") or r.get("best_side")
            s = _side(r, side)
            if s and r["result"]["winner"] != side and s.get("blended", 0) >= 0.7:
                misses.append(f"{s['label']} ({s['blended']:.0%}, {_miss_by(r, side)})")
    if misses:
        lines.append("💥 **Surprise misses** (70%+ picks that lost): " + "; ".join(misses[:5])
                     + ". One night proves nothing, but check for late scratches or minutes "
                     "limits if the same player keeps showing up.")
    graded = sum(1 for r in main.values() if r.get("result"))
    if graded < 200:
        lines.append(f"📏 Small sample: {graded} graded today. The weights only move once the "
                     "evidence is clearly better than what's set now, so expect few changes early.")
    if newly == 0:
        lines.append("ℹ️ Nothing new was graded on this run; everything below was graded earlier.")
    if remaining is not None:
        lines.append(f"💳 Odds API credits left: **{remaining:,}**" if isinstance(remaining, int)
                     else f"💳 Odds API credits left: **{remaining}**")
    return discord.Embed(title="📌 Changes & Things to Know", description="\n".join(lines)[:4000],
                         color=GOLD)


def overview_embed(date: str, records: dict, newly: int) -> discord.Embed:
    main = records["main"]
    total = len(main)
    graded = [r for r in main.values() if r.get("result")]
    props = sum(_is_prop(r) for r in graded)
    lines = [f"**{len(graded)} of {total}** tracked markets graded"
             + (f" (**{newly}** new this run)" if newly else ""),
             f"• Player props: **{props}** (from ESPN box scores)",
             f"• Game lines: **{len(graded) - props}** (moneylines, spreads, totals from final scores)"]
    for kind, label in (("live", "Live reads"), ("pp", "PrizePicks lines")):
        n = sum(1 for r in records[kind].values() if r.get("result"))
        if n:
            lines.append(f"• {label}: **{n}**")
    picks = sum(1 for r in graded if r.get("picked"))
    lines.append(f"• The model's picks among them: **{picks}**")
    by_sport = {}
    for r in graded:
        by_sport[r.get("sport", "?")] = by_sport.get(r.get("sport", "?"), 0) + 1
    if by_sport:
        lines.append("• By league: " + ", ".join(f"{k} {v}" for k, v in sorted(by_sport.items())))
    lines.append("")
    lines.append("Every market graded is in the attached CSV: line, the model's numbers, the "
                 "result, and whether it was a pick.")
    return discord.Embed(title=f"📈 Grading Report · {date}", description="\n".join(lines),
                         color=BLUE)


def graded_csv(records: dict) -> str:
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["source", "sport", "game", "subject", "market", "line", "likelier_side",
                "likelier_prob", "model_prob", "market_prob", "value_side", "value_gap",
                "best_odds", "best_book", "actual", "result", "picked", "pick_odds"])
    for kind, recs in records.items():
        for r in recs.values():
            if not r.get("result"):
                continue
            fav = _favored(r) or {}
            val = _side(r, r.get("pick_side") or r.get("best_side")) or {}
            pick_side = r.get("pick_side") or r.get("best_side")
            w.writerow([kind, r.get("sport"), r.get("game"), r.get("subject"), r.get("market"),
                        _line(r), fav.get("label"), round(fav.get("blended", 0), 4),
                        round(fav.get("model_prob", 0), 4), round(fav.get("market_prob", 0), 4),
                        val.get("label"), round(val.get("gap", 0), 4), val.get("best_odds"),
                        val.get("best_book"), r["result"].get("actual"),
                        _outcome(r, pick_side) if r.get("picked") else _outcome(r, fav.get("side")),
                        "yes" if r.get("picked") else "", r.get("pick_odds", "")])
    return out.getvalue()


def grade_report(date: str, summary: list, before: set, remaining) -> tuple:
    """(embeds, csv text or None) for /grade."""
    import re
    m = next((re.search(r"Settled \*\*(\d+)\*\*", l) for l in summary if "Settled" in l), None)
    settled_now = int(m.group(1)) if m else 0
    records = day_records(date)
    newly = len(graded_keys(records) - before)
    embeds = [overview_embed(date, records, newly)]
    for e in (picks_embed(records["main"]), model_embed(records["main"]), side_book_embed(records),
              bets_embed(date, settled_now)):
        if e is not None:
            embeds.append(e)
    embeds.append(notes_embed(date, records, summary, newly, remaining))
    text = graded_csv(records)
    return embeds, (text if text.count("\n") > 1 else None)
