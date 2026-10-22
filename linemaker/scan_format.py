"""Long-form "value scan" replies: ranked board, a write-up per play, long-term notes and
scoping/exclusion notes, plus the pages behind each pick's buttons (odds comparison, hit
rate chart, line movement, key stats). Every sentence is built from numbers the engine or
the data sources produced; nothing is invented (no sharp-money or position-defense claims)."""
import datetime as dt
import os
from typing import Optional
from zoneinfo import ZoneInfo

import discord

from .discord_format import BOOK_STYLE, GOLD, GREY, _book_key, _clip, _emoji, _kickoff, _style
from .odds import fmt_money, fmt_odds
from .gamelogs import MIN_SEASON_GAMES, apply_hit_gate
from .pro import book_prices

PURPLE, RED = 0x9B59B6, 0xE67E22
SPORT_NAMES = {"NBA": "NBA", "WNBA": "WNBA", "NFL": "NFL", "NCAAF": "College Football",
               "MLB": "MLB", "NHL": "NHL", "UCL": "Champions League", "LALIGA": "La Liga"}


def _pct(p: float) -> str:
    return f"{p * 100:.1f}%".replace(".0%", "%")


def _pts(gap: float) -> str:
    return f"{gap * 100:+.1f}"


def _last(name: str) -> str:
    parts = name.replace(" Jr.", "").replace(" Sr.", "").replace(" III", "").split()
    return parts[-1] if parts else name


def _day(date: str) -> str:
    d = dt.date.fromisoformat(date)
    return d.strftime("%b %d, %Y").replace(" 0", " ")


def _price_sentence(ev) -> str:
    prices = book_prices(ev.market, ev.best.side)
    shown = []
    for key in ("fanduel", "draftkings"):
        o = next((v for b, v in prices.items() if _book_key(b) == key), None)
        name = BOOK_STYLE[key][0]
        if o is not None:
            shown.append(f"**{name} {fmt_odds(o)}**" if _book_key(ev.best.best_book) == key
                         else f"{name} {fmt_odds(o)}")
    n = len(ev.market.books)
    line = " · ".join(shown) if shown else f"{ev.best.best_book} {fmt_odds(ev.best.best_odds)}"
    return (f"{line}. Stripping the vig across {n} book{'s' if n != 1 else ''}, the market's "
            f"fair read is **{_pct(ev.best.market_prob)}**, so the model sits "
            f"{_pts(ev.best.model_edge)} points above consensus; blended at "
            f"{ev.model_weight:.0%} model it lands at **{_pct(ev.best.blended)}**.")


def _projection_sentence(ev) -> str:
    m, s, sim = ev.market, ev.best, ev.sim
    over = s.side == "A"
    diff = m.mean - m.line
    lean = "above" if diff > 0 else "below"
    word = "Over" if over else "Under"
    return (f"The projection sits at **{m.mean:.2f}** against a line of {m.line:g}, "
            f"{abs(diff):.2f} {lean} it. Run {sim.n:,} times, the {word} lands "
            f"**{_pct(s.model_prob)}** of the time, with a median outcome of {sim.median:g}.")


def _hit_sentences(ev, prof) -> list:
    if not prof or not prof.season:
        return []
    who, line = _last(ev.market.subject), f"{ev.market.line:g}"
    word = "cleared" if ev.best.side == "A" else "stayed under"
    s = prof.season
    out = [f"Over the last {s.games} games, {who} has {word} {line} **{_pct(s.rate)}** of the "
           f"time ({s.avg:.1f} average)."]
    if prof.recent:
        parts = [f"{_pct(w.rate)} over the last {n} ({w.avg:.1f})" for n, w in sorted(prof.recent.items())]
        out.append("Recent windows: " + ", ".join(parts) + ".")
        hot = max(w.rate for w in prof.recent.values())
        if hot > s.rate + 0.15:
            out.append("The recent run is hotter than the season baseline, so the estimate "
                       "leans on the larger sample rather than the streak.")
    if prof.venue and prof.venue.games:
        v = prof.venue
        out.append(f"{prof.venue_label.capitalize()}, where tonight's game is, it's "
                   f"{_pct(v.rate)} across {v.games} games ({v.avg:.1f} average).")
    if prof.versus and prof.versus.games:
        v = prof.versus
        small = " Small sample, so it's weighted lightly." if v.games <= 5 else ""
        out.append(f"Against the {prof.opponent}, {v.hits} of {v.games} ({v.avg:.1f} average)."
                   + small)
    return out


def _risk_sentences(ev, partners: list) -> list:
    m = ev.market
    out = []
    if m.injury_status in ("questionable", "doubtful"):
        out.append(f"⚠️ {m.subject} is listed **{m.injury_status}**. Re-check the status before "
                   "the game; a scratch voids the prop at most books, but a limited role doesn't.")
    if m.market.lower() == "pra" or "+" in m.market:
        out.append("Multi-stat combos carry wider variance than a single-stat market, so expect "
                   "more nights that miss by a little.")
    if any(n.startswith("soft market") for n in ev.notes):
        out.append("🎯 " + next(n for n in ev.notes if n.startswith("soft market")).capitalize() + ".")
    if partners:
        out.append("🔗 Same game as " + ", ".join(f"#{p}" for p in partners)
                   + ", so these are not independent bets.")
    return out


def _closer(ev, prof) -> str:
    s = ev.best
    base = (f"**Why it's a good long-term bet at this price:** {fmt_odds(s.best_odds)} implies "
            f"{_pct(s.best_implied)}.")
    if prof and prof.season and prof.season.games >= MIN_SEASON_GAMES:
        rate = prof.season.rate
        base += (f" The outcome has landed {_pct(rate)} of the time across "
                 f"{prof.season.games} games, ")
        if s.blended <= rate:
            base += f"and the blended estimate of {_pct(s.blended)} is conservative next to that."
        else:
            base += (f"a little below the blended estimate of {_pct(s.blended)}, so the season "
                     "rate is the more cautious read and the edge is thinner than the gap says.")
    else:
        base += f" The blended estimate is {_pct(s.blended)}."
    return (base + f" That's roughly **{_pts(s.gap)} points** of probability in your favor, worth "
            f"**{fmt_money(s.ev)}** per $100 over the long run. It can lose tonight and still have "
            "been the right price.")


def _context_sentences(ev, ctx, prof) -> list:
    if not ctx:
        return []
    out = []
    k = ctx.get("kalshi")
    if k:
        (fav, pf), (dog, pd) = sorted(k.items(), key=lambda kv: -kv[1])
        out.append(f"Kalshi's prediction market has the {fav} at **{_pct(pf)}** to win "
                   f"({dog} {_pct(pd)}).")
    teams = ctx.get("teams") or {}
    opp = prof.opponent if prof and prof.opponent else None
    focus = [teams[opp]] if opp and opp in teams else list(teams.values())
    for tc in focus:
        if tc.def_rating is not None and tc.pace is not None:
            out.append(f"The {tc.team} ({tc.season_label}, {tc.games} games) played at an "
                       f"estimated {tc.pace:.1f} possessions per game and allowed "
                       f"{tc.allowed:.1f} points a night, an estimated defensive rating of "
                       f"{tc.def_rating:.1f}.")
        elif tc.allowed is not None:
            unit = "goals" if ev.market.sport == "NHL" else ("runs" if ev.market.sport == "MLB" else "points")
            out.append(f"The {tc.team} allowed {tc.allowed:.2f} {unit} per game "
                       f"({tc.season_label}, {tc.games} games).")
    return out


def _movement_sentence(ev, history) -> str:
    from .tracking import movement
    if not history or len(history) < 2:
        return ""
    s = ev.best
    mv = movement(history, s.side, s.best_book)
    if not mv:
        return ""
    first, now = mv
    if first == now:
        return f"Line movement: {s.best_book} has held at {fmt_odds(now)} since the first scan."
    return (f"Line movement: {s.best_book} opened at {fmt_odds(first)} on the first scan and is "
            f"{fmt_odds(now)} now.")


def _pick_embed(rank: int, ev, prof, partners: list, biggest: bool, ctx=None,
                history=None) -> discord.Embed:
    s, m = ev.best, ev.market
    name, _tag, emoji, colour = _style(s.best_book)
    when = _kickoff(m.commence)
    head = (f"**Implied {_pct(s.best_implied)} → Estimated {_pct(s.blended)} → Gap {_pts(s.gap)}.**"
            + (" The biggest disconnect on the board." if biggest else "")
            + f"\n{_emoji(m.sport)} {m.game}" + (f" · {when}" if when else ""))
    paras = [head, _projection_sentence(ev), _price_sentence(ev)]
    tweaks = [t for t in m.notes.split("; ")[2:] if t] if m.notes.startswith("ESPN game-log") else []
    if tweaks:
        paras.insert(2, "🔧 **Adjusted for tonight:** " + "; ".join(tweaks) + ".")
    if m.notes.startswith("LIVE · "):
        paras.insert(1, "🔴 **Live:** " + m.notes[len("LIVE · "):] + ". Live prices move fast; "
                     "check the line in the app before you bet.")
    mv = _movement_sentence(ev, history)
    if mv:
        paras[-1] += " " + mv
    hits = _hit_sentences(ev, prof)
    if hits:
        paras.append(" ".join(hits))
    context = _context_sentences(ev, ctx, prof)
    if context:
        paras.append(" ".join(context))
    risks = _risk_sentences(ev, partners)
    if risks:
        paras.append(" ".join(risks))
    paras.append(_closer(ev, prof))
    paras.append(f"{emoji} **Bet this at {name}**")
    from .staking import stake_line
    paras.append(stake_line(s.blended, s.best_odds))
    return discord.Embed(title=_clip(f"{rank}. {s.label} ({fmt_odds(s.best_odds)}, {name})", 256),
                         description=_clip("\n\n".join(paras), 4096), color=colour)


def _board(picks: list, profiles: dict) -> str:
    width = max([len(f"{ev.best.label} (XX)") for ev in picks] + [3])
    width = min(width, 44)
    rows = [f"{'#':>2} {'Bet':<{width}} {'Odds':>5} {'Impl':>6} {'Ours':>6} {'Gap':>6}"]
    for i, ev in enumerate(picks, 1):
        s = ev.best
        tag = _style(s.best_book)[1]
        bet = f"{s.label} ({tag})"
        rows.append(f"{i:>2} {bet[:width]:<{width}} {fmt_odds(s.best_odds):>5} {_pct(s.best_implied):>6} "
                    f"{_pct(s.blended):>6} {_pts(s.gap):>6}")
    return "```\n" + "\n".join(rows) + "\n```"


def _short_reason(ev, prof) -> str:
    if prof and prof.season and prof.season.games >= MIN_SEASON_GAMES:
        return (f"{_pct(prof.season.rate)} season hit rate over {prof.season.games} games, "
                f"proj {ev.market.mean:.2f}")
    return f"proj {ev.market.mean:.2f} vs line {ev.market.line:g}"


def _norm_sections(sections: list) -> list:
    """Accept (sport, result, extras) or (sport, result, profiles) and return the first form."""
    out = []
    for sport, res, extra in sections:
        if not (isinstance(extra, dict) and "profiles" in extra):
            extra = {"profiles": extra or {}, "context": {}, "history": {}}
        out.append((sport, res, extra))
    return out


def scan_embeds(sections: list, date: str, cfg, title: str = "") -> list:
    """All embeds in reading order (no buttons)."""
    return [e for group, _detail in scan_messages(sections, date, cfg, title) for e in group]


def scan_messages(sections: list, date: str, cfg, title: str = "") -> list:
    """[(embeds, detail)] in reading order. Each pick is its own message, and its detail
    (pick, hit profile, game context, price history) feeds that message's buttons."""
    sections = _norm_sections(sections)
    extras_by_sport = {sport: ex for sport, _r, ex in sections}
    sections = [(sport, res, ex["profiles"]) for sport, res, ex in sections]
    for _sport, res, prof in sections:
        apply_hit_gate(res, prof)
    ranked = sorted(((ev, sport, prof) for sport, res, prof in sections for ev in res.picks),
                    key=lambda x: x[0].best.gap, reverse=True)[:cfg.max_picks]
    ranks = {id(ev): i for i, (ev, _s, _p) in enumerate(ranked, 1)}
    by_game = {}
    for ev, _s, _p in ranked:
        by_game.setdefault(ev.market.game_id, []).append(ranks[id(ev)])

    def _scope(sp, res):
        n = len(res.slate) or res.games
        if res.slate and res.games < n:
            return (f"{SPORT_NAMES.get(sp, sp)} ({n} game{'s' if n != 1 else ''} on the slate, "
                    f"{res.games} with {_what(res)} I could price)")
        return f"{SPORT_NAMES.get(sp, sp)} ({n} game{'s' if n != 1 else ''})"
    scanned = ", ".join(_scope(sp, res) for sp, res, _p in sections)
    live = all(getattr(res, "kind", "") == "live" for _s, res, _p in sections)
    title = title or ("Cross-Sport " if len(sections) > 1 else
                      f"{SPORT_NAMES.get(sections[0][0], sections[0][0])} ") + (
        "Live Edge Scan" if live else "Value Scan")
    intro = [f"I swept every market with a projection and a live FanDuel/DraftKings price on "
             f"today's date: {scanned}. Only games starting today were priced; anything "
             "tomorrow was cut before pricing."]
    sources = sorted({getattr(res, "projection_source", "") for _s, res, _p in sections} - {""})
    if sources:
        intro[0] += f" Projections: {', '.join(sources)}."
    if live:
        intro.append("🔴 **Live mode:** only games in progress, priced from the score, the clock "
                     "and the pre-game line. The market gets most of the weight because live books "
                     "see possession, fouls, injuries and lineups within seconds and this model "
                     "doesn't. Odds older than a minute or two are thrown out, and the last few "
                     "minutes of each game are skipped.")
    gates = ["the model beats the no-vig market", "the best price still pays better than fair",
             "a positive gap after blending with the market",
             f"a season hit rate that doesn't contradict the price ({MIN_SEASON_GAMES}+ games)"]
    if cfg.min_odds is not None or cfg.max_odds is not None:
        gates.insert(0, f"odds inside {fmt_odds(cfg.min_odds or -100)} to {fmt_odds(cfg.max_odds or -10000)}")
    if cfg.min_prob is not None:
        gates.insert(1, f"estimated probability of {cfg.min_prob:.0%} or better")
    if cfg.min_gap:
        gates.append(f"a gap of at least {cfg.min_gap * 100:.0f} points")
    if cfg.max_gap is not None:
        gates.append(f"a gap no bigger than {cfg.max_gap * 100:.0f} points (bigger usually means stale data)")
    if cfg.min_ev is not None:
        gates.append(f"at least {cfg.min_ev:.0%} expected return per dollar")
    if ranked:
        clusters = sorted(by_game.items(), key=lambda kv: -len(kv[1]))
        top = [next(ev.market.game for ev, _s, _p in ranked if ev.market.game_id == g)
               for g, rs in clusters if len(rs) > 1][:3]
        where = f" The softest prices cluster in {', '.join(top)}." if top else ""
        intro.append(f"Below are the **{len(ranked)} plays** that clear every gate: "
                     + "; ".join(gates) + "." + where)
    else:
        intro.append("**Nothing cleared every gate today.** No plays are forced.")
    header = discord.Embed(title=f"🎯 {title} · Today's Board ({_day(date)})",
                           description=_clip("\n\n".join(intro), 4096), color=GOLD if ranked else GREY)
    header.set_footer(text=odds_age_note(sections))
    messages = []
    embeds = [header]
    if ranked:
        board = discord.Embed(title="📊 The Board, Ranked by Probability Gap", color=GOLD,
                              description=_board([e for e, _s, _p in ranked],
                                                 {k: v for _s, _r, p in sections for k, v in p.items()}))
        for ev, _s, prof in ranked[:10]:
            board.add_field(name=f"{ranks[id(ev)]}. {_emoji(ev.market.sport)} {ev.best.label[:60]}",
                            value=_short_reason(ev, prof.get(ev.market.key)), inline=False)
        embeds.append(board)
    messages.append((embeds, None))

    for sport, res, prof in sections:
        mine = [(ev, p) for ev, s, p in ranked if s == sport]
        if not mine:
            continue
        ex = extras_by_sport.get(sport, {})
        games = {ev.market.game for ev, _p in mine}
        messages.append(([discord.Embed(title=f"{_emoji(sport)} {SPORT_NAMES.get(sport, sport)}",
                                        description=f"{len(mine)} play{'s' if len(mine) != 1 else ''} "
                                                    f"from {len(games)} game{'s' if len(games) != 1 else ''}.",
                                        color=GREY)], None))
        for ev, p in mine:
            r = ranks[id(ev)]
            pr = p.get(ev.market.key)
            ctx = (ex.get("context") or {}).get(ev.market.game)
            hist = (ex.get("history") or {}).get(ev.market.key)
            from .betlinks import pick_links
            detail = {"rank": r, "ev": ev, "profile": pr, "context": ctx, "history": hist,
                      "links": pick_links(ev)}
            messages.append(([_pick_embed(r, ev, pr, [x for x in by_game[ev.market.game_id] if x != r],
                                          r == 1, ctx, hist)], detail))

    notes = _staking_note(ranked, by_game)
    messages.append((([notes] if notes else []) + [_scoping(sections)], None))
    return messages


TIPS = ["**A bet can lose and still have been a great decision.** If a play priced with a "
        "real edge misses tonight, that's one result, not a verdict. You want to make the "
        "bet a thousand times, not win it once.",
        "**A bet can win and still have been a bad decision.** A hot five-game streak on a "
        "player whose season rate sits below the price is a losing proposition even when "
        "it cashes. That's why plays like that are moved to the notes at the end of each scan.",
        "The goal is repetition: find places where the true probability beats the implied "
        "probability, size so a losing streak doesn't break you, and let the math work "
        "over hundreds of wagers."]


def tips_embed() -> discord.Embed:
    """/tips: how to think about these bets long-term."""
    return discord.Embed(title="🧠 How to Think About These Long-Term",
                         description="\n\n".join(TIPS), color=PURPLE)


def _staking_note(ranked: list, by_game: dict) -> Optional[discord.Embed]:
    """Tonight's price range and correlated groups (the general advice lives in /tips)."""
    paras = []
    if ranked:
        odds = [ev.best.best_odds for ev, _s, _p in ranked]
        avg_gap = sum(ev.best.gap for ev, _s, _p in ranked) / len(ranked)
        staking = (f"**Staking note:** these prices run from {fmt_odds(max(odds))} to "
                   f"{fmt_odds(min(odds))}, with an average gap of {_pts(avg_gap)} points. Size "
                   "by edge, not by price; an expensive favorite is not a safe leg that deserves "
                   "a bigger unit.")
        groups = [g for g in by_game.values() if len(g) > 1]
        if groups:
            staking += " Correlated groups: " + "; ".join(
                " + ".join(f"#{r}" for r in g) for g in groups) + ". They aren't independent bets."
        paras.append(staking + " (`/tips` for how to think about these long-term.)")
    if not paras:
        return None
    return discord.Embed(title="📌 Staking Note", description=_clip("\n\n".join(paras), 4096),
                         color=PURPLE)


PASS_GROUPS = (   # pass reason prefix -> plain-English label for the summary
    ("market caught up", "the market already agreed with the projection"),
    ("no gap", "the best price didn't beat our estimate"),
    ("odds ", "price outside your odds window"),
    ("blended", "estimate below your probability floor"),
    ("EV ", "expected return below your EV floor"),
    ("gap ", "gap too large to trust"),
    ("no bettable price", "no FanDuel/DraftKings price"),
    ("injury status", "player ruled out"),
    ("qualified", "qualified but outside the top plays"),
    ("similar line", "a better line on the same player-stat is already on the board"),
)


def _pass_summary(res) -> str:
    counts = {}
    for _lbl, reason in res.passes:
        label = next((lbl for prefix, lbl in PASS_GROUPS if reason.startswith(prefix)), None)
        if label:
            counts[label] = counts.get(label, 0) + 1
    vetoes = len(getattr(res, "hit_vetoes", []))
    if vetoes:
        counts["season hit rate contradicted the price"] = vetoes
    if not counts:
        return ""
    return ", ".join(f"{n} {lbl}" for lbl, n in sorted(counts.items(), key=lambda kv: -kv[1]))


def _closest_misses(res, n: int = 5) -> list:
    """The best-looking markets that didn't make the board, with the gate each one failed."""
    reasons = {}
    for lbl, reason in res.passes:
        reasons.setdefault(lbl, reason)
    vetoed = {id(ev): w for ev, w in getattr(res, "hit_vetoes", [])}
    picked = {id(ev) for ev in res.picks}
    rows = []
    for ev in sorted(res.evaluated, key=lambda e: e.best.gap, reverse=True):
        if id(ev) in picked:
            continue
        s = ev.best
        if id(ev) in vetoed:
            w = vetoed[id(ev)]
            why = f"season hit rate {_pct(w.rate)} over {w.games} games"
        else:
            why = reasons.get(s.label) or reasons.get(ev.market.side_label("A"), "didn't clear a gate")
        rows.append(f"• {s.label} {fmt_odds(s.best_odds)} ({s.best_book}): ours {_pct(s.blended)} vs "
                    f"{_pct(s.best_implied)} implied, gap {_pts(s.gap)} → {why}")
        if len(rows) == n:
            break
    return rows


def _missing_games(res) -> list:
    import datetime as _dt
    priced = {ev.market.game for ev in res.evaluated}
    now = _dt.datetime.now(_dt.timezone.utc)
    started, waiting = [], []
    for game, commence in res.slate:
        if game in priced:
            continue
        t = _dt.datetime.fromisoformat(commence.replace("Z", "+00:00")) if commence else None
        (started if t and t <= now else waiting).append(game)
    return started, waiting


def pass_counts(res) -> dict:
    counts = {}
    for _lbl, reason in res.passes:
        label = next((lbl for prefix, lbl in PASS_GROUPS if reason.startswith(prefix)), reason[:40])
        counts[label] = counts.get(label, 0) + 1
    return counts


def _what(res) -> str:
    kind = getattr(res, "kind", "props")
    return {"games": "lines", "live": "live lines"}.get(kind, "props")


def odds_age_note(sections: list) -> str:
    """'Odds pulled 12 min ago' footer text, or '' when they were just bought."""
    ages = [getattr(res, "odds_age", None) for _s, res, _p in sections]
    ages = [a for a in ages if a is not None]
    oldest = max(ages) if ages else None
    if oldest is None or oldest < 1:
        return "Odds pulled just now"
    return (f"Odds pulled {oldest:.0f} min ago (saved to spare credits) · ask for \"fresh odds\" "
            "to update · the Bet buttons always open the live price")


def _scoping(sections: list) -> discord.Embed:
    lines = []
    for sport, res, _p in sections:
        name = SPORT_NAMES.get(sport, sport)
        if getattr(res, "source_warning", None):
            lines.append(f"🚨 **{name}: {res.source_warning}**")
        if res.slate:
            started, waiting = _missing_games(res)
            if waiting:
                why = ("these games aren't in progress yet, or are in their last few minutes "
                       "(the live model sits those out)." if _what(res) == "live lines" else
                       "the game model had no ESPN ratings for a team, or FanDuel/DraftKings "
                       "haven't posted the line yet." if _what(res) == "lines" else
                       "FanDuel/DraftKings often post player props a few hours before tip-off, "
                       "or no projection matched a posted line. Ask again closer to game time.")
                lines.append(f"**{name}, no priceable {_what(res)} yet ({len(waiting)}):** "
                             + "; ".join(waiting[:8]) + ". " + why)
            if started:
                lines.append(f"**{name}, already started ({len(started)}):** " + "; ".join(started[:8])
                             + f". Pre-game {_what(res)} are off the board once a game tips.")
        summary = _pass_summary(res)
        if summary:
            lines.append(f"**{name}, {len(res.evaluated)} markets priced. Why they didn't make "
                         f"the board:** {summary}.")
        misses = _closest_misses(res)
        if misses:
            lines.append(f"**{name}, closest misses:**\n" + "\n".join(misses))
        vetoes = getattr(res, "hit_vetoes", [])
        if vetoes:
            items = [f"{ev.best.label} (season rate {_pct(w.rate)} over {w.games} games vs "
                     f"{_pct(ev.best.best_implied)} implied)" for ev, w in vetoes[:5]]
            lines.append(f"**{name}, excluded on data grounds:** " + "; ".join(items) + ".")
        outs = sorted({lbl.split(" Over ")[0].split(" Under ")[0] for lbl, r in res.passes
                       if r.startswith("injury status")})
        if outs:
            lines.append(f"**{name}, ruled out:** " + ", ".join(outs[:8])
                         + (" and others." if len(outs) > 8 else "."))
        wide = [lbl for lbl, r in res.passes if "too large to trust" in r]
        if wide:
            lines.append(f"**{name}, gap too large to trust:** " + "; ".join(wide[:5])
                         + ". Edges this big against a sharp market usually mean stale data; "
                         "check injuries and the line before betting them.")
        caught = [(lbl, r) for lbl, r in res.passes if r.startswith("market caught up")]
        if caught:
            lines.append(f"**{name}:** {len(caught)} markets where the projection and the market "
                         "already agree were passed.")
        if not res.evaluated:
            lines.append(f"**{name}:** no priced markets with a projection today.")
    lines.append("**Sources:** FantasyPros consensus projections, The Odds API prices, ESPN game "
                 "logs and team averages (unofficial feed), Kalshi market prices, and the bot's own "
                 "price history for line movement. Pace and defensive ratings are estimates from "
                 "ESPN team averages. Sharp-money splits and defense-versus-position aren't in these "
                 "sources, so they aren't claimed.")
    return discord.Embed(title="⚠️ Scoping & Data Notes", description=_clip("\n\n".join(lines), 4096),
                         color=RED)


# ---- button pages ----------------------------------------------------------------------

def odds_page(detail: dict) -> discord.Embed:
    from .devig import no_vig
    ev = detail["ev"]
    m, s = ev.market, ev.best
    names = [ev.market.side_label(x).replace(m.subject, "").strip() or x for x in m.side_keys]
    rows = [f"{'Book':<12} " + " ".join(f"{n[:9]:>9}" for n in names) + f" {'Fair':>7}"]
    for book, prices in sorted(m.books, key=lambda b: (_book_key(b[0]) not in BOOK_STYLE, b[0])):
        cells = [fmt_odds(p) if p is not None else "—" for p in prices[:len(m.side_keys)]]
        fair = ""
        if all(p is not None for p in prices[:len(m.side_keys)]):
            fair = _pct(no_vig(*prices[:len(m.side_keys)])[m.side_keys.index(s.side)])
        star = "★" if book == s.best_book else " "
        rows.append(f"{star}{book[:11]:<11} " + " ".join(f"{c:>9}" for c in cells) + f" {fair:>7}")
    e = discord.Embed(title=f"🏷️ Odds Comparison · {s.label}", color=GOLD,
                      description="```\n" + "\n".join(rows) + "\n```")
    e.add_field(name="Consensus (no-vig)", value=_pct(s.market_prob))
    e.add_field(name="Our estimate", value=_pct(s.blended))
    e.add_field(name="Fair odds", value=fmt_odds(s.fair))
    e.set_footer(text="★ best bettable price · Fair = this side's no-vig probability at that book")
    return e


def chart_page(detail: dict) -> discord.Embed:
    ev, prof = detail["ev"], detail["profile"]
    s, m = ev.best, ev.market
    if not prof or not prof.games:
        return discord.Embed(title=f"📊 Hit Rate Chart · {s.label}", color=GREY,
                             description="No game log is available for this player and market.")
    top = max([v for *_x, v in prof.games] + [m.line]) or 1
    rows = []
    for date, opp, home, v in prof.games:
        hit = v > m.line if prof.over else v < m.line
        bar_len = int(round(14 * v / top))
        mark = int(round(14 * m.line / top))
        bar = "".join("█" if i < bar_len else ("│" if i == mark else "·") for i in range(15))
        rows.append(f"{date[5:]} {'vs' if home else '@ '} {_short_team(opp):<4} {bar} "
                    f"{v:>5g} {'✅' if hit else '❌'}")
    w = prof.recent.get(20) or prof.season
    desc = (f"Last {len(prof.games)} games, newest first · line **{m.line:g}** (│)\n"
            "```\n" + "\n".join(rows) + "\n```")
    e = discord.Embed(title=f"📊 Hit Rate Chart · {s.label}", color=GOLD, description=_clip(desc, 4096))
    if prof.season:
        e.add_field(name="Season", value=f"{_pct(prof.season.rate)} ({prof.season.hits}/{prof.season.games})")
    for n in (5, 10, 20):
        if n in prof.recent:
            e.add_field(name=f"Last {n}", value=f"{_pct(prof.recent[n].rate)} · avg {prof.recent[n].avg:.1f}")
    return e


def _short_team(name: str) -> str:
    if len(name) <= 4:          # already an abbreviation (ESPN game logs)
        return name.upper()
    words = name.split()
    return (words[-1][:3] if words else name[:3]).upper()


def movement_page(detail: dict, tz_name: str = None) -> discord.Embed:
    ev, history = detail["ev"], detail.get("history") or []
    s = ev.best
    tz = ZoneInfo(tz_name or os.environ.get("LINEMAKER_TZ", "America/New_York"))
    if len(history) < 2:
        return discord.Embed(title=f"📉 Line Movement · {s.label}", color=GREY,
                             description="Only one price snapshot so far. Movement is recorded "
                                         "each time the board is scanned and a price changes.")
    rows = [f"{'Time':<9} {'FanDuel':>8} {'DraftKings':>11}"]
    for t, books in history[-15:]:
        side = books.get(s.side, {})
        fd = next((v for b, v in side.items() if _book_key(b) == "fanduel"), None)
        dk = next((v for b, v in side.items() if _book_key(b) == "draftkings"), None)
        when = dt.datetime.fromisoformat(t).astimezone(tz).strftime("%I:%M %p").lstrip("0")
        rows.append(f"{when:<9} {fmt_odds(fd) if fd is not None else '—':>8} "
                    f"{fmt_odds(dk) if dk is not None else '—':>11}")
    return discord.Embed(title=f"📉 Line Movement · {s.label}", color=GOLD,
                         description="Prices for this side at each scan where something moved:\n"
                                     "```\n" + "\n".join(rows) + "\n```")


def stats_page(detail: dict) -> discord.Embed:
    ev, prof, ctx = detail["ev"], detail["profile"], detail.get("context")
    s, m, sim = ev.best, ev.market, ev.sim
    e = discord.Embed(title=f"📈 Key Stats · {s.label}", color=GOLD)
    e.add_field(name="Projection", value=f"{m.mean:.2f} (median {sim.median:g})")
    e.add_field(name="Line", value=f"{m.line:g}")
    e.add_field(name="Model / Market / Blend",
                value=f"{_pct(s.model_prob)} / {_pct(s.market_prob)} / {_pct(s.blended)}")
    if prof and prof.season:
        e.add_field(name="Season avg", value=f"{prof.season.avg:.1f} over {prof.season.games} games")
        if 5 in prof.recent:
            e.add_field(name="Last 5 avg", value=f"{prof.recent[5].avg:.1f}")
        if 10 in prof.recent:
            e.add_field(name="Last 10 avg", value=f"{prof.recent[10].avg:.1f}")
        if prof.venue:
            e.add_field(name=prof.venue_label.capitalize(),
                        value=f"{_pct(prof.venue.rate)} · avg {prof.venue.avg:.1f} ({prof.venue.games} g)")
        if prof.versus and prof.versus.games:
            e.add_field(name=f"vs {prof.opponent}",
                        value=f"{prof.versus.hits}/{prof.versus.games} · avg {prof.versus.avg:.1f}")
    if m.injury_status not in ("", "active"):
        e.add_field(name="Injury status", value=m.injury_status.title())
    if ctx and ctx.get("kalshi"):
        e.add_field(name="Kalshi win prob", inline=False,
                    value=" · ".join(f"{t} {_pct(p)}" for t, p in ctx["kalshi"].items()))
    for tc in (ctx or {}).get("teams", {}).values():
        if tc.def_rating is not None:
            val = f"pace ~{tc.pace:.1f} · def rtg ~{tc.def_rating:.1f} · allows {tc.allowed:.1f}"
        elif tc.allowed is not None:
            val = f"allows {tc.allowed:.2f} per game"
        else:
            continue
        e.add_field(name=f"{tc.team} ({tc.season_label}, est.)", value=val, inline=False)
    return e


# ---- volume / parlay mode -------------------------------------------------------------

def volume_messages(sections: list, date: str, cfg, title: str = "") -> list:
    """[(embeds, None)]: header, high-hit-rate legs, parlays per book, notes."""
    from .parlay import build_parlays
    sections = _norm_sections(sections)
    for _sport, res, ex in sections:
        apply_hit_gate(res, ex["profiles"], floor=cfg.min_prob)
    legs = sorted((ev for _s, res, _e in sections for ev in res.picks),
                  key=lambda ev: ev.best.blended, reverse=True)[:cfg.max_picks]
    profiles = {k: v for _s, _r, ex in sections for k, v in ex["profiles"].items()}
    name = "Cross-Sport" if len(sections) > 1 else SPORT_NAMES.get(sections[0][0], sections[0][0])
    title = title or f"{name} Volume Mode"

    def _scope(sp, res):
        n = len(res.slate) or res.games
        return f"{SPORT_NAMES.get(sp, sp)} ({n} game{'s' if n != 1 else ''})"
    sources = sorted({getattr(res, "projection_source", "") for _s, res, _e in sections} - {""})
    intro = [f"Volume mode: legs ranked by **how often they should hit**, not by edge. Scanned "
             + ", ".join(_scope(sp, res) for sp, res, _e in sections) + "."
             + (f" Projections: {', '.join(sources)}." if sources else ""),
             f"Each leg is priced {fmt_odds(cfg.min_odds)} to {fmt_odds(cfg.max_odds)}, has an "
             f"estimated hit chance of {cfg.min_prob:.0%}+, a season hit rate of "
             f"{cfg.min_prob:.0%}+ where there's a sample, and isn't priced more than "
             f"{abs(cfg.min_gap) * 100:.0f} points above our estimate."]
    if not legs:
        intro.append("**No legs cleared those bars today.** Try again closer to game time.")
    header = discord.Embed(title=f"🎰 {title} · Today's Board ({_day(date)})", color=GOLD if legs else GREY,
                           description="\n\n".join(intro))
    header.set_footer(text=odds_age_note(sections))
    out = [([header], None)]
    if legs:
        rows = [f"{'#':>2} {'Leg':<36} {'FD':>6} {'DK':>6} {'Hit':>6} {'Szn':>5}"]
        from .parlay import leg_price
        for i, ev in enumerate(legs, 1):
            fd, dk = leg_price(ev, "FanDuel"), leg_price(ev, "DraftKings")
            prof = profiles.get(ev.market.key)
            szn = _pct(prof.season.rate) if prof and prof.season else "—"
            rows.append(f"{i:>2} {ev.best.label[:36]:<36} {fmt_odds(fd) if fd else '—':>6} "
                        f"{fmt_odds(dk) if dk else '—':>6} {_pct(ev.best.blended):>6} {szn:>5}")
        board = discord.Embed(title="✅ High-Hit-Rate Legs", color=GOLD,
                              description="```\n" + "\n".join(rows) + "\n```")
        board.set_footer(text="Hit = our estimated chance · Szn = season hit rate at this line")
        out.append(([board], None))
        from .betlinks import parlay_link
        parlays = build_parlays(legs)
        for book in ("FanDuel", "DraftKings"):
            mine = [p for p in parlays if p.book == book]
            if not mine:
                continue
            bname, _tag, emoji, colour = _style(book)
            e = discord.Embed(title=f"{emoji} {bname} Parlays", color=colour)
            buttons = []
            for p in mine:
                legs_txt = "\n".join(f"• {ev.best.label} ({fmt_odds(o)})" for ev, o in p.legs)
                url = parlay_link(p)
                slip = f"\n[🧾 Open this {len(p.legs)}-leg slip on {bname}]({url})" if url else ""
                if url:
                    buttons.append((f"{len(p.legs)}-leg on {bname}", url))
                from .staking import stake_line
                body = (f"{legs_txt}\nHit chance **{_pct(p.prob)}** · $100 pays "
                        f"**${p.payout:,.0f}** · EV {fmt_money(p.ev)}\n"
                        + stake_line(p.prob, p.american, parlay=True))
                e.add_field(name=f"🔒 {len(p.legs)}-Leg · {fmt_odds(p.american)}",
                            value=body + slip if len(body + slip) <= 1024 else _clip(body, 1024),
                            inline=False)
            e.set_footer(text="One leg per game · payout multiplies the legs' prices; the book's "
                              "parlay price may differ slightly · check every leg before you bet")
            out.append(([e], {"links": buttons, "parlays": mine}))
    notes = discord.Embed(title="🧠 Volume Mode, Honestly", color=PURPLE, description=(
        "These legs are picked to **hit often**, not because the price is wrong. Parlaying "
        "heavy favorites multiplies the book's margin on every leg, so the expected value of a "
        "parlay is usually slightly negative even when each leg is likely. Treat it as "
        "entertainment money: small stakes, and fewer legs is safer than more.\n\n"
        "Legs from the same game are never combined, but legs from different games can still "
        "move together (late scratches, blowouts)."))
    out.append(([notes, _scoping([(s, r, ex["profiles"]) for s, r, ex in sections])], None))
    return out


def pp_messages(sections: list, date: str, title: str = "") -> list:
    """[(embeds, detail)] for a PrizePicks scan: header, legs board, best entries."""
    from .pp import best_entries, break_even, line_gap, power_table
    sections = _norm_sections(sections)
    legs = sorted((ev for _s, res, _e in sections for ev in res.picks),
                  key=lambda ev: ev.best.blended, reverse=True)
    name = "Cross-Sport" if len(sections) > 1 else SPORT_NAMES.get(sections[0][0], sections[0][0])
    be = break_even()
    priced = sum(len(res.evaluated) for _s, res, _e in sections)
    intro = [f"PrizePicks lines scored with the props model ({priced} lines priced). Every leg "
             f"below hits **{be:.1%}+** by our estimate, the break-even for "
             f"{min(power_table() or {2: 3})}-pick Power. Where FanDuel/DraftKings post the same "
             "number, their no-vig price is blended in; PrizePicks' own price isn't counted."]
    if not legs:
        intro.append("**No PrizePicks legs cleared the break-even rate.** Lines may not be up yet, "
                     "or they match the books closely tonight.")
    header = discord.Embed(title=f"🟣 {title or name + ' PrizePicks'} · {_day(date)}",
                           description="\n\n".join(intro), color=0x8E44AD if legs else GREY)
    header.set_footer(text=odds_age_note(sections))
    out = [([header], None)]
    if legs:
        rows = [f"{'#':>2} {'Pick':<34} {'Hit':>5} {'Gap':>6}"]
        for i, ev in enumerate(legs[:25], 1):
            side = "More" if ev.best.side == "A" else "Less"
            pick = f"{ev.market.subject} {side} {ev.market.line:g} {ev.market.market}"
            gap = line_gap(ev)
            gtxt = f"{gap:+.1f}" if gap is not None else "—"
            rows.append(f"{i:>2} {pick[:34]:<34} {_pct(ev.best.blended):>5} {gtxt:>6}")
        board = discord.Embed(title="✅ PrizePicks Legs", color=0x8E44AD,
                              description="```\n" + "\n".join(rows) + "\n```")
        board.set_footer(text="Hit = our chance · Gap = books' main line minus PrizePicks' line "
                              "(+ means PrizePicks is lower: easier More)")
        out.append(([board], None))
        entries = best_entries(legs)
        entries.sort(key=lambda e: (e.size, e.kind))
        if entries:
            e_embed = discord.Embed(title="🎟️ Best Entries", color=0x8E44AD)
            for en in entries[:10]:
                legs_txt = "\n".join(f"• {ev.market.subject} {'More' if ev.best.side == 'A' else 'Less'} "
                                     f"{ev.market.line:g} {ev.market.market} ({_pct(ev.best.blended)})"
                                     for ev in en.legs)
                pays = " / ".join(f"{m:g}x" for m in en.payouts)
                e_embed.add_field(
                    name=f"{en.size}-Pick {en.kind} · {pays}",
                    value=_clip(f"{legs_txt}\nHit all **{_pct(en.p_all)}** · EV **{en.ev:+.0f}%** per $100",
                                1024), inline=False)
            e_embed.set_footer(text="Payouts from LINEMAKER_PP_POWER / LINEMAKER_PP_FLEX: check them "
                                    "against your app. Legs are treated as independent.")
            out.append(([e_embed], {"pp": entries[:10]}))
    out.append(([_scoping([(s, r, ex["profiles"]) for s, r, ex in sections])], None))
    return out
