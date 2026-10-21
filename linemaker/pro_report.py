"""Markdown card for the Pro engine."""
from .odds import american_to_decimal, fmt_money, fmt_odds
from .pro import ProConfig, ProResult
from .report import SPORT_EMOJI


def render_pro(res: ProResult, cfg: ProConfig) -> str:
    out = [f"# 🎲 Linemaker Pro Card — {res.slate_date}", "",
           f"{cfg.n_sims:,} simulations per market · de-vigged market · blend "
           f"{cfg.model_weight:.0%}/{1 - cfg.model_weight:.0%} model/market "
           f"({cfg.model_weight_fresh:.0%}/{1 - cfg.model_weight_fresh:.0%} on fresh news) · "
           f"{cfg.devig_method} de-vig · "
           f"bet the gap at the best price", "", "---", ""]

    if not res.picks:
        out += ["## No qualifying plays", "", "No market cleared the gap at its best price.", ""]
    else:
        out += ["## 🏆 Ranked Plays", "",
                "| # | Sport | Play | Best Price | Fair | Blended | No-Vig Mkt | Gap | EV/$100 |",
                "|---|---|---|---|---|---|---|---|---|"]
        for i, ev in enumerate(res.picks, 1):
            s, m = ev.best, ev.market
            out.append(f"| {i} | {SPORT_EMOJI.get(m.sport.upper(), '🎯')} {m.sport} | {s.label} | "
                       f"{fmt_odds(s.best_odds)} {s.best_book} | {fmt_odds(s.fair)} | "
                       f"{s.blended:.1%} | {s.market_prob:.1%} | **{s.gap:+.1%}** | {fmt_money(s.ev)} |")
        out += ["", "---", ""]

        for i, ev in enumerate(res.picks, 1):
            s, m, sim = ev.best, ev.market, ev.sim
            status = (f"🧊 frozen {ev.frozen_at}" if ev.frozen_at else
                      "✅ lineups confirmed" if m.lineup_confirmed else "🔄 live — not final")
            out += [f"### {i}. {s.label} ({fmt_odds(s.best_odds)}, {s.best_book or 'book n/a'})",
                    f"*{m.game} · {m.sport} · {status}*", "",
                    "| Step | Value |", "|---|---|",
                    *[f"| ↳ {label} | {val} |" for label, val in
                      (m.projection.steps if m.projection and not ev.frozen_at else [])],
                    f"| Projection (mean) | {sim.mean:.2f} |",
                    f"| Distribution | {sim.dist}, sd {sim.sd:.2f} |",
                    (f"| Median goal difference | {sim.median:+.0f} |" if sim.mean_b is not None else
                     f"| Simulated median | {sim.median:.1f} "
                     f"({'mean above median: right skew' if sim.mean > sim.median else 'mean ≈ median'}) |"),
                    (f"| Sim P(win / draw / loss) | {sim.p_over:.1%} / {sim.p_push:.1%} / {sim.p_under:.1%}"
                     f" (goals {sim.mean:.2f} vs {sim.mean_b:.2f}) |" if sim.mean_b is not None else
                     f"| Sim P(over / push / under) | {sim.p_over:.1%} / {sim.p_push:.1%} / {sim.p_under:.1%} |"),
                    f"| Model probability (this side, ex-push) | {s.model_prob:.1%} |"]
            if ev.market_hold is not None:
                out.append(f"| Market overround / hold | {ev.market_overround:.2%} / {ev.market_hold:.2%} |")
            out += [f"| No-vig market probability | {s.market_prob:.1%} |",
                    f"| Model edge vs no-vig | {s.model_edge:+.1%} |",
                    f"| Blend weight (model) | {ev.model_weight:.0%}"
                    f"{' — fresh news' if m.news_fresh else ''} |",
                    f"| **Blended probability** | **{s.blended:.1%}** (fair {fmt_odds(s.fair)}, "
                    f"decimal {1 / s.blended:.2f}) |",
                    f"| Best price implied | {s.best_implied:.1%} (decimal "
                    f"{american_to_decimal(s.best_odds):.2f}) |",
                    f"| **Gap** | **{s.gap:+.1%}** |",
                    f"| EV per $100 | {fmt_money(s.ev)} |", ""]
            for note in ev.notes:
                out.append(f"⚠️ {note}")
            if m.injury_status not in ("", "active"):
                out.append(f"⚠️ Injury status: **{m.injury_status}** — re-check before start.")
            if m.notes:
                out.append(f"**Notes:** {m.notes}")
            out += ["", "---", ""]

    if res.correlated:
        out += ["## 🔗 Correlated Legs", "", "Same game — not independent; size together.", ""]
        out += ["- " + " + ".join(v) for v in res.correlated.values()]
        out += ["", "---", ""]
    if res.passes:
        out += ["## 🚫 Passes", "", "| Market | Reason |", "|---|---|"]
        out += [f"| {label} | {reason} |" for label, reason in res.passes]
        out.append("")
    return "\n".join(out)
