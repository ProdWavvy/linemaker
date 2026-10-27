"""/help (what to ask, what the buttons do) and /today (bankroll, bets, credits, news) for
quick reading on a phone."""
import datetime as dt
import os

import discord

TEAL, GOLD = 0x1ABC9C, 0xF1C40F

ASKS = [
    ("🏀 Props", "`best nba props` · `best nfl props` · `mlb props` · `knicks props` · `celtics vs knicks props`"),
    ("🎰 Parlays", "`best nba props parlay` · `ml parlay nfl` · `ml and spread parlay nba` · "
                  "`spread parlay nfl` · `run line parlay mlb`"),
    ("🔴 Live", "`live props nba` · `live props parlay nfl` · `live ml parlay nba` · "
               "`live ml and spread parlay nfl`"),
    ("⚡ Edges & books", "`market edges nba` · `best fanduel props` · `dk parlay` · `prizepicks nba`"),
    ("🔄 Fresh odds", "add **fresh** to any ask (`fresh nba props parlay`) to buy new odds instead of "
                     "reusing ones from the last few minutes"),
]
ROWS = [
    "**Row 1** 🔎 See a pick / 🧾 Bet a leg: write-up, Bet, I bet this",
    "**Row 2** 🧾 Bet a parlay: every parlay on the card, #1, #2 …",
    "**Row 3** Bet #1 · Bet #2 …: logs it, then opens the slip (↩️ Undo if you don't place it)",
    "**Row 4** 🏷️ Odds · 📊 Hit Rates · 📉 Line Moves · 📈 Key Stats · 🔄 Fresh odds",
    "**Row 5** 🎯 Safest by payout · 🎰 Build my own parlay · 💰 Parlay moneylines · ⚡ Market edges · ℹ️ Notes",
]
COMMANDS = [
    ("💰 Money", "`/bankroll` stakes as a share of what you have · `/paper` practice bets · "
                "`/bets` open bets (change / undo) · `/record` your results · `/today` dashboard"),
    ("📊 Results", "`/grade` grade a day · `/weekly` the week · `/clv` beat the close? · `/calibrate`"),
    ("⚙️ Owner", "`/status` version, credits · `/settings` · `/set` · `/envchanges` · `/logs` · "
                "`/restart` · `/backup`"),
]
LEGEND = ("🎯 model pick / Edge parlay (priced above fair) · 🔒 safest legs · 💰 legs beating their "
          "price · 🔗 same-game (≈ the book's SGP price) · ✅ leg +EV at this price · 🩹 injury news "
          "already in the numbers · 🧯 already in your open bets")


def help_embed() -> discord.Embed:
    e = discord.Embed(title="🤖 Linemaker · How to Use It", color=TEAL,
                      description="Just type in this channel. A few examples:")
    for name, value in ASKS:
        e.add_field(name=name, value=value, inline=False)
    e.add_field(name="🔘 Buttons on every card (same rows every time)", value="\n".join(ROWS), inline=False)
    for name, value in COMMANDS:
        e.add_field(name=name, value=value, inline=False)
    e.set_footer(text=LEGEND)
    return e


def today_embed(date: str) -> discord.Embed:
    """Bankroll, open and settled bets, credits and injury news, on one screen."""
    from .bets import load_bets
    from .odds import fmt_money
    from .staking import _money, bankroll, current_bankroll, paper_on
    lines = []
    start = bankroll()
    if start is not None:
        now = current_bankroll() or 0.0
        pct = float(os.environ.get("LINEMAKER_MAX_STAKE_PCT", "") or 2) / 100
        lines.append(f"💰 **Bankroll {_money(round(now, 2))}** (started {_money(start)}) · top stake "
                     f"{_money(max(round(now * pct * 20) / 20, 0.1))}")
    else:
        lines.append("💰 No bankroll set (`/bankroll amount:30` sizes every stake to it)")
    lines.append("🧪 **Practice mode on**: bets you log are practice" if paper_on()
                 else "💵 Practice mode off: bets you log are real")
    bets = load_bets()
    for paper, label in ((False, "Real"), (True, "Practice")):
        mine = [b for b in bets if bool(b.get("paper")) == paper]
        open_ = [b for b in mine if b["status"] == "open"]
        today = [b for b in mine if b["status"] != "open" and (b.get("settled") or "")[:10] == date]
        if not open_ and not today:
            continue
        line = f"🧾 **{label} bets:** {len(open_)} open (${sum(b['stake'] for b in open_):,.2f} out)"
        if today:
            w = sum(b["status"] == "won" for b in today)
            l = sum(b["status"] == "lost" for b in today)
            line += f" · settled today {w}-{l} · {fmt_money(sum(b.get('profit', 0) for b in today))}"
        lines.append(line)
    try:
        from .oddsapi import _spend, credits_today, daily_budget, days_to_reset, local_today
        budget = daily_budget()
        left = _spend(local_today()).get("remaining")
        lines.append(f"💳 **Credits today:** {credits_today():,}" + (f" of {budget:,}" if budget else "")
                     + (f" · {left:,} left on the account, {days_to_reset()} day(s) to refill" if left is not None else ""))
    except Exception:
        pass
    from .alerts import load_state
    news = load_state(f"news_log_{date}.json", [])
    if news:
        lines.append(f"🩹 **News today ({len(news)}):** " + " · ".join(f"{n['what']} ({n['game']})" for n in news[-4:]))
    armed = load_state(f"pretip_armed_{date}.json", [])
    configured = [s.strip() for s in os.environ.get("LINEMAKER_PRETIP_SPORTS", "").split(",") if s.strip()]
    pre = sorted(set(configured) | set(armed))
    if pre:
        lines.append(f"⏰ **Pre-tip scans:** {', '.join(pre)} (about "
                     f"{os.environ.get('LINEMAKER_PRETIP_MINUTES', '45') or '45'} min before each game)")
    e = discord.Embed(title=f"📅 Today · {dt.date.fromisoformat(date):%a %b %d}", color=GOLD,
                      description="\n".join(lines))
    e.set_footer(text="/bets to change or undo an open bet · /record for your full results · /help for what to ask")
    return e


HELP_WORDS = ("help", "/help", "!help", "commands", "what can you do", "what can you do?", "how do i use this",
              "how do i use this?")
