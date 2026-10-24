"""Markdown output in the Linemaker card format."""
from .config import Config
from .odds import fair_odds, fmt_odds
from .pipeline import Result, alt_lines

SPORT_EMOJI = {
    "NBA": "🏀", "WNBA": "🏀", "NCAAB": "🏀", "NHL": "🏒", "NFL": "🏈", "NCAAF": "🏈",
    "MLB": "⚾", "UCL": "⚽", "LALIGA": "⚽", "EPL": "⚽", "MLS": "⚽", "UFC": "🥊",
}


def _pct(v):
    return "data unavailable" if v is None else f"{v:.1%}"


def render(res: Result, cfg: Config) -> str:
    out = [f"# 📊 Linemaker Card — {res.slate_date}", ""]
    out.append(f"Filters: odds {fmt_odds(cfg.min_odds)} to {fmt_odds(cfg.max_odds)} · "
               f"true probability ≥ {cfg.min_true_prob:.0%} · positive gap only · "
               f"max {cfg.max_picks} plays")
    out += ["", "---", ""]

    if not res.picks:
        out += ["## No qualifying plays", "",
                "Nothing on this slate cleared every filter. No plays forced.", ""]
    else:
        out += ["## 🏆 Ranked Plays", "",
                "| # | Sport | Play | Book | Odds | Implied | True Prob | Gap | Season Edge |",
                "|---|---|---|---|---|---|---|---|---|"]
        for i, ev in enumerate(res.picks, 1):
            m = ev.market
            out.append(f"| {i} | {SPORT_EMOJI.get(m.sport.upper(), '🎯')} {m.sport} | "
                       f"{m.label} | {m.book or '—'} | {fmt_odds(m.odds)} | {ev.implied:.1%} | "
                       f"{ev.true_prob:.1%} | **{ev.gap:+.1%}** | {ev.season_edge:+.1%} |")
        out += ["", "---", ""]

        for i, ev in enumerate(res.picks, 1):
            m = ev.market
            out += [f"### {i}. {m.label} ({fmt_odds(m.odds)}, {m.book or 'book n/a'})",
                    f"*{m.game} · {m.sport}*", ""]
            out += ["| Input | Value |", "|---|---|",
                    f"| Season hit rate | {ev.season_rate:.1%} ({m.season_hits}/{m.season_games}) |",
                    f"| Home/away split | " + (f"{ev.split_rate:.1%} ({m.split_hits}/{m.split_games})"
                                               if ev.split_rate is not None else "data unavailable") + " |"]
            for name, val in m.recent_windows.items():
                out.append(f"| {name} | {val:.1%} |")
            if m.projection is not None:
                vs = f" vs {m.line:g}" if m.line is not None else " (projected margin)"
                out.append(f"| Projection vs line | {m.projection:g}{vs} |")
            out += [f"| Weighted blend | {ev.blend:.1%} |",
                    f"| **Estimated true probability** | **{ev.true_prob:.1%}** "
                    f"(fair {fmt_odds(fair_odds(ev.true_prob))}) |",
                    f"| Implied probability | {ev.implied:.1%} |",
                    f"| **Gap** | **{ev.gap:+.1%}** |",
                    f"| Season edge (season − implied) | {ev.season_edge:+.1%} |", ""]
            if ev.adjustments:
                out.append("**Adjustments:** " + "; ".join(
                    f"{r} ({d:+.1%})" for r, d in ev.adjustments))
                out.append("")
            alts = alt_lines(ev, cfg)
            if alts:
                out += ["**Alternate lines:**", "", "| Line | Odds | Implied | In window |",
                        "|---|---|---|---|"]
                for a in alts:
                    out.append(f"| {a.line:g} | {fmt_odds(a.odds)} | {a.implied:.1%} | "
                               f"{'✅' if a.in_window else '—'} |")
                out.append("")
            if m.injury_status not in ("", "active"):
                out += [f"⚠️ Injury status: **{m.injury_status}** — re-check before start.", ""]
            if m.notes:
                out += [f"**Notes:** {m.notes}", ""]
            out += ["---", ""]

    if res.correlated:
        out += ["## 🔗 Correlated Legs", "",
                "These share a game and are **not independent** — size them together, "
                "not as separate edges.", ""]
        for labels in res.correlated.values():
            out.append("- " + " + ".join(labels))
        out += ["", "---", ""]

    if res.excluded:
        out += ["## 🚫 Exclusions", "", "| Market | Reason |", "|---|---|"]
        for m, reason in res.excluded:
            out.append(f"| {m.label} ({fmt_odds(m.odds)}) | {reason} |")
        out.append("")
    return "\n".join(out)
