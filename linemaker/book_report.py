"""Which books' prices were right: each book's no-vig line in its last pre-game read, scored
against how graded markets actually landed (Brier score and log loss, lower is better).
This is the evidence for the devig weights (LINEMAKER_SHARP_BOOKS / _PROPS): a sharp book
is one whose prices predict results best, measured on your own slates."""
import math
from typing import Optional

from .devig import no_vig

MIN_MARKETS = 150   # a book needs this many graded two-sided markets to be ranked


def _last_snapshot(rec: dict) -> Optional[dict]:
    hist = rec.get("history") or []
    return hist[-1] if hist else None


def book_scores(records: list) -> dict:
    """{"props"|"games": {book: {"n", "brier", "logloss", "vs_avg"}}} over graded two-way
    markets. vs_avg: the book's Brier minus the average of every book that priced the same
    markets (negative = better than the field on those markets)."""
    acc = {"props": {}, "games": {}}
    for rec in records:
        res = rec.get("result") or {}
        if res.get("winner") not in ("A", "B"):
            continue
        snap = _last_snapshot(rec)
        if not snap:
            continue
        a, b = snap.get("A") or {}, snap.get("B") or {}
        y = 1.0 if res["winner"] == "A" else 0.0
        kind = "props" if (rec.get("inputs") or {}).get("bet_type") == "prop" else "games"
        rows = []
        for book, oa in a.items():
            ob = b.get(book)
            if oa is None or ob is None:
                continue
            p = min(max(no_vig(oa, ob)[0], 0.001), 0.999)
            rows.append((book, p))
        if len(rows) < 2:   # nothing to compare against
            continue
        field = sum((p - y) ** 2 for _b, p in rows) / len(rows)
        for book, p in rows:
            s = acc[kind].setdefault(book, {"n": 0, "brier": 0.0, "logloss": 0.0, "vs": 0.0})
            s["n"] += 1
            s["brier"] += (p - y) ** 2
            s["logloss"] += -(y * math.log(p) + (1 - y) * math.log(1 - p))
            s["vs"] += (p - y) ** 2 - field
    out = {}
    for kind, books in acc.items():
        out[kind] = {b: {"n": s["n"], "brier": s["brier"] / s["n"], "logloss": s["logloss"] / s["n"],
                         "vs_avg": s["vs"] / s["n"]} for b, s in books.items()}
    return out


def suggested_weights(scores: dict, min_n: int = MIN_MARKETS) -> dict:
    """Book -> weight 20-100 from how much better than the field it was (best = 100)."""
    ranked = {b: s for b, s in scores.items() if s["n"] >= min_n}
    if not ranked:
        return {}
    best, worst = min(s["vs_avg"] for s in ranked.values()), max(s["vs_avg"] for s in ranked.values())
    span = (worst - best) or 1.0
    return {b: round(100 - 80 * (s["vs_avg"] - best) / span) for b, s in ranked.items()}


def book_lines(records: list) -> list:
    scores = book_scores(records)
    lines = []
    for kind, title in (("props", "Player props"), ("games", "Game lines")):
        books = scores.get(kind) or {}
        if not books:
            continue
        weights = suggested_weights(books)
        lines.append(f"**{title}** (lower = more accurate; vs field = Brier against the other books "
                     "on the same markets)")
        for b, s in sorted(books.items(), key=lambda kv: kv[1]["vs_avg"]):
            w = weights.get(b)
            lines.append(f"• **{b}** · {s['n']:,} markets · Brier {s['brier']:.4f} · log loss "
                         f"{s['logloss']:.4f} · vs field {s['vs_avg'] * 1000:+.2f}‰"
                         + (f" · weight **{w}**" if w is not None else " · too few to rank"))
        if weights:
            lines.append("Suggested: `" + ",".join(f"{b}:{w}" for b, w in
                                                    sorted(weights.items(), key=lambda kv: -kv[1])) + "`")
        lines.append("")
    if not lines:
        return ["No graded markets with prices from two or more books yet."]
    lines.append("Scored on each market's last pre-game read across every book the bot pulls. "
                 f"Books need {MIN_MARKETS}+ markets to get a weight. A week or more is far more "
                 "reliable than a day.")
    return lines
