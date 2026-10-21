"""Turn card results into Discord embeds.

Discord doesn't render markdown tables, so tables become monospace code blocks.
Limits respected here: 25 fields and 1024 chars per field, 4096 per description,
6000 chars across all embeds in one message (see `chunk_embeds`).
"""
import os

import discord

from .config import Config
from .odds import fmt_money, fmt_odds
from .pipeline import Result, alt_lines
from .pro import ProConfig, ProResult
from .report import SPORT_EMOJI

GREEN, GREY, BLUE = 0x2ECC71, 0x95A5A6, 0x3498DB


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 2] + "…"


def _emoji(sport: str) -> str:
    return SPORT_EMOJI.get(sport.upper(), "🎯")


def _passes_field(embed: discord.Embed, passes: list, title: str):
    if not passes:
        return
    lines = [f"• {label} — {reason}" for label, reason in passes]
    text = ""
    for i, line in enumerate(lines):
        if len(text) + len(line) + 40 > 1024:
            text += f"…and {len(lines) - i} more (see attached card)"
            break
        text += line + "\n"
    embed.add_field(name=title, value=text, inline=False)


def _correlated_field(embed: discord.Embed, correlated: dict):
    if correlated:
        embed.add_field(name="🔗 Correlated legs (not independent)",
                        value=_clip("\n".join("• " + " + ".join(v) for v in correlated.values()),
                                    1024), inline=False)


def pro_embeds(res: ProResult, cfg: ProConfig) -> list:
    summary = discord.Embed(
        title=f"🎲 Linemaker Pro Card — {res.slate_date}",
        color=GREEN if res.picks else GREY,
        description=(f"{cfg.n_sims:,} sims/market · {cfg.devig_method} de-vig · blend "
                     f"{cfg.model_weight:.0%} model ({cfg.model_weight_fresh:.0%} on fresh news)"))
    if res.picks:
        rows = [f"{i:>2}. {ev.best.label[:34]:<34} {fmt_odds(ev.best.best_odds):>5}  "
                f"gap {ev.best.gap:+.1%}" for i, ev in enumerate(res.picks, 1)]
        summary.description += "\n```\n" + "\n".join(rows) + "\n```"
    else:
        summary.description += "\n\n**No qualifying plays.** No market cleared the gap at its best price."
    _correlated_field(summary, res.correlated)
    _passes_field(summary, res.passes, "🚫 Passes")

    embeds = [summary]
    for i, ev in enumerate(res.picks, 1):
        s, m, sim = ev.best, ev.market, ev.sim
        status = (f"🧊 frozen {ev.frozen_at}" if ev.frozen_at else
                  "✅ lineups confirmed" if m.lineup_confirmed else "🔄 live — not final")
        e = discord.Embed(title=_clip(f"{i}. {_emoji(m.sport)} {s.label}", 256), color=BLUE,
                          description=f"{m.game} · {m.sport} · {status}")
        e.add_field(name="Best price", value=f"{fmt_odds(s.best_odds)} {s.best_book}".strip())
        e.add_field(name="Fair odds", value=fmt_odds(s.fair))
        e.add_field(name="Gap", value=f"**{s.gap:+.1%}** (EV {fmt_money(s.ev)}/$100)")
        if m.projection and not ev.frozen_at:
            e.add_field(name="Projection build", inline=False, value=_clip(
                "\n".join(f"{label}: {val}" for label, val in m.projection.steps), 1024))
        e.add_field(name="Simulation", inline=False, value=(
            f"mean {sim.mean:.2f} · median {sim.median:.1f} · {sim.dist} sd {sim.sd:.2f}\n"
            f"P({'win/draw/loss' if sim.mean_b is not None else 'over/push/under'}) "
            f"{sim.p_over:.1%} / {sim.p_push:.1%} / {sim.p_under:.1%}"))
        hold = (f" · overround {ev.market_overround:.1%} / hold {ev.market_hold:.1%}"
                if ev.market_hold is not None else "")
        e.add_field(name="Pricing", inline=False, value=(
            f"model {s.model_prob:.1%} · no-vig market {s.market_prob:.1%}{hold}\n"
            f"blend {ev.model_weight:.0%} model → **{s.blended:.1%}** vs best-price "
            f"implied {s.best_implied:.1%}"))
        warnings = list(ev.notes)
        if m.injury_status not in ("", "active"):
            warnings.append(f"injury status: {m.injury_status} — re-check before start")
        if warnings:
            e.add_field(name="⚠️ Flags", value=_clip("\n".join(warnings), 1024), inline=False)
        if m.notes:
            e.add_field(name="Notes", value=_clip(m.notes, 1024), inline=False)
        embeds.append(e)
    return embeds


def lite_embeds(res: Result, cfg: Config) -> list:
    summary = discord.Embed(
        title=f"📊 Linemaker Card — {res.slate_date}", color=GREEN if res.picks else GREY,
        description=(f"odds {fmt_odds(cfg.min_odds)} to {fmt_odds(cfg.max_odds)} · true prob ≥ "
                     f"{cfg.min_true_prob:.0%} · positive gap only"))
    if res.picks:
        rows = [f"{i:>2}. {ev.market.label[:34]:<34} {fmt_odds(ev.market.odds):>5}  "
                f"gap {ev.gap:+.1%}" for i, ev in enumerate(res.picks, 1)]
        summary.description += "\n```\n" + "\n".join(rows) + "\n```"
    else:
        summary.description += "\n\n**No qualifying plays.** None forced."
    _correlated_field(summary, res.correlated)
    _passes_field(summary, [(m.label, r) for m, r in res.excluded], "🚫 Exclusions")

    embeds = [summary]
    for i, ev in enumerate(res.picks, 1):
        m = ev.market
        e = discord.Embed(title=_clip(f"{i}. {_emoji(m.sport)} {m.label}", 256), color=BLUE,
                          description=m.game)
        e.add_field(name="Odds", value=f"{fmt_odds(m.odds)} {m.book}".strip())
        e.add_field(name="True / implied", value=f"{ev.true_prob:.1%} / {ev.implied:.1%}")
        e.add_field(name="Gap", value=f"**{ev.gap:+.1%}** (season edge {ev.season_edge:+.1%})")
        inputs = [f"season {ev.season_rate:.1%} ({m.season_hits}/{m.season_games})"]
        if ev.split_rate is not None:
            inputs.append(f"split {ev.split_rate:.1%} ({m.split_hits}/{m.split_games})")
        inputs += [f"{k} {v:.0%}" for k, v in m.recent_windows.items()]
        e.add_field(name="Inputs", value=" · ".join(inputs), inline=False)
        if ev.adjustments:
            e.add_field(name="Adjustments", inline=False, value=_clip(
                "\n".join(f"{r} ({d:+.1%})" for r, d in ev.adjustments), 1024))
        alts = alt_lines(ev, cfg)
        if alts:
            e.add_field(name="Alternate lines", inline=False, value="```\n" + "\n".join(
                f"{a.line:>6g} {fmt_odds(a.odds):>6} {a.implied:6.1%} {'✓' if a.in_window else ''}"
                for a in alts) + "\n```")
        if m.injury_status not in ("", "active"):
            e.add_field(name="⚠️ Injury", value=f"{m.injury_status} — re-check before start",
                        inline=False)
        embeds.append(e)
    return embeds


def chunk_embeds(embeds: list, max_chars: int = 5800, max_count: int = 10) -> list:
    """Split embeds into per-message groups under Discord's combined-size limit."""
    groups, cur, size = [], [], 0
    for e in embeds:
        n = len(e)
        if cur and (size + n > max_chars or len(cur) == max_count):
            groups.append(cur)
            cur, size = [], 0
        cur.append(e)
        size += n
    if cur:
        groups.append(cur)
    return groups


# ---- picks display -------------------------------------------------------------------

BOOK_STYLE = {   # book key -> (display name, short tag, emoji, embed colour)
    "fanduel": ("FanDuel", "FD", "🟦", 0x1493FF),
    "draftkings": ("DraftKings", "DK", "🟩", 0x53D337),
}
GOLD = 0xF1C40F


def _book_key(name: str) -> str:
    return "".join(ch for ch in name.lower() if ch.isalnum())


def _style(book: str):
    return BOOK_STYLE.get(_book_key(book), (book or "Other books", (book or "?")[:3].upper(),
                                            "⬜", GREY))


def _kickoff(commence: str) -> str:
    import datetime as dt
    import os
    from zoneinfo import ZoneInfo
    if not commence:
        return ""
    t = dt.datetime.fromisoformat(commence.replace("Z", "+00:00"))
    t = t.astimezone(ZoneInfo(os.environ.get("LINEMAKER_TZ", "America/New_York")))
    return t.strftime("%-I:%M %p") if os.name != "nt" else t.strftime("%#I:%M %p")


def _price_line(ev) -> str:
    """'**FD -110** │ DK -115' with the best (bettable) price bold."""
    from .pro import book_prices
    s = ev.best
    prices = book_prices(ev.market, s.side)
    parts = []
    for key in ("fanduel", "draftkings"):
        match = next((o for b, o in prices.items() if _book_key(b) == key), None)
        tag = BOOK_STYLE[key][1]
        if match is None:
            parts.append(f"{tag} —")
        elif _book_key(s.best_book) == key:
            parts.append(f"**{tag} {fmt_odds(match)}**")
        else:
            parts.append(f"{tag} {fmt_odds(match)}")
    if not any(_book_key(s.best_book) == k for k in BOOK_STYLE):   # best book is another site
        parts.append(f"**{s.best_book} {fmt_odds(s.best_odds)}**")
    return " │ ".join(parts)


def _pick_field(rank: int, ev, partners: list) -> tuple:
    s, m = ev.best, ev.market
    when = _kickoff(m.commence)
    lines = [f"{_emoji(m.sport)} {m.game}" + (f" · {when}" if when else ""),
             _price_line(ev),
             f"📈 Ours **{s.blended:.1%}** vs market {s.market_prob:.1%} → edge **{s.gap:+.1%}**"
             f" · EV **{fmt_money(s.ev)}**/100"]
    flags = []
    if m.injury_status not in ("", "active", "probable"):
        flags.append(f"⚠️ {m.injury_status.title()}")
    if partners:
        flags.append("🔗 Same game as " + ", ".join(f"#{p}" for p in partners))
    if any(n.startswith("soft market") for n in ev.notes):
        flags.append("🎯 Soft market")
    if flags:
        lines.append(" · ".join(flags))
    return _clip(f"{rank}. {s.label}", 256), _clip("\n".join(lines), 1024)


def picks_embeds(res, title: str, note: str = "") -> list:
    """Header card plus one card per sportsbook, picks grouped by where to bet them."""
    import datetime as dt
    day = dt.date.fromisoformat(res.slate_date).strftime("%a %b %-d") if os.name != "nt" \
        else dt.date.fromisoformat(res.slate_date).strftime("%a %b %#d")
    n = len(res.picks)
    desc = [f"**{n} play{'s' if n != 1 else ''}** with an edge" if n else
            "**No plays** cleared the edge at FanDuel / DraftKings prices today.",
            f"📊 Tracked **{len(res.evaluated)}** markets across **{res.games}** games"]
    if note:
        desc.append(note)
    header = discord.Embed(title=f"🎯 {title} · {day}", description="\n".join(desc),
                           color=GOLD if n else GREY)
    embeds = [header]

    ranks = {id(ev): i for i, ev in enumerate(res.picks, 1)}
    by_game = {}
    for ev in res.picks:
        by_game.setdefault(ev.market.game_id, []).append(ranks[id(ev)])
    groups = {}
    for ev in res.picks:
        groups.setdefault(_style(ev.best.best_book), []).append(ev)
    for (name, _tag, emoji, colour), evs in groups.items():
        e = discord.Embed(title=f"{emoji} Best at {name}", color=colour)
        for ev in evs:
            r = ranks[id(ev)]
            fname, fval = _pick_field(r, ev, [p for p in by_game[ev.market.game_id] if p != r])
            e.add_field(name=fname, value=fval, inline=False)
        embeds.append(e)
    embeds[-1].set_footer(text="Linemaker · 20,000 sims per market · de-vigged consensus · "
                               "positive edge only · bet responsibly")
    return embeds
