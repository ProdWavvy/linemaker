"""Discord bot front end.

  DISCORD_TOKEN=... python -m linemaker.bot

Environment:
  DISCORD_TOKEN          bot token (required)
  LINEMAKER_GUILD_ID     sync slash commands to one server instantly (optional; global sync
                         can take up to an hour to appear)
  LINEMAKER_TZ           timezone for "today" and the daily post (default America/New_York)
  LINEMAKER_SNAPSHOTS    freeze directory (default ./snapshots)
  LINEMAKER_CHANNEL_ID   channel for the automatic daily pro card (optional)
  LINEMAKER_POST_TIME    daily post time, HH:MM in LINEMAKER_TZ (default 11:00)
  Daily post source, first match wins:
  LINEMAKER_SPORTS + LINEMAKER_PROJECTIONS_DIR   live odds (needs ODDS_API_KEY) joined with
                         <PROJECTIONS_DIR>/<YYYY-MM-DD>.csv; SPORTS is e.g. "NBA,NHL,NFL"
  LINEMAKER_SLATE_DIR    folder holding complete <YYYY-MM-DD>.csv pro slates
  LINEMAKER_SPORTS alone (with FANTASYPROS_API_KEY)  FantasyPros projections, NBA/NFL/MLB
  ODDS_API_KEY           The Odds API key (for /live, /odds, /quota)
  ODDS_API_BOOKMAKERS    optional comma list, e.g. draftkings,fanduel,betrivers,williamhill_us
  ANTHROPIC_API_KEY      enables plain-English chat (mention the bot, DM it, or talk in a
                         chat channel); needs Message Content Intent on in the Developer Portal
  LINEMAKER_CHAT_CHANNELS  comma list of channel IDs where every message is answered
  LINEMAKER_HOME_CHANNEL   lock the bot to one channel (ID): every message there is answered,
                         slash commands elsewhere are refused, DMs and @mentions elsewhere
                         are ignored, and the daily post goes there
"""
import asyncio
import csv
import datetime as dt
import io
import logging
import os
import tempfile
from typing import Optional
from zoneinfo import ZoneInfo

from .envfile import load_env

load_env()  # .env in the working directory; real environment variables take priority
load_env(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

import discord
from discord import app_commands
from discord.ext import tasks

from .config import Config
from .devig import hold, no_vig, overround
from .discord_format import chunk_embeds, lite_embeds
from .scan_format import (chart_page, movement_page, odds_page, scan_messages,
                          stats_page, SPORT_NAMES)
from .freeze import Freezer, grade
from .market import load_markets
from .odds import american_to_decimal, expected_value, fair_odds, fmt_money, fmt_odds, implied_probability, parse_odds
from .pipeline import run
from .live import auto_results, board_lines, fetch_feed, odds_template, read_projections, rows_csv, run_live
from .oddsapi import SPORT_KEYS, OddsAPI, feed_row
from .pro import ProConfig, load_pro_markets, run_pro
from .pro_report import render_pro
from .report import render

log = logging.getLogger("linemaker")
TZ = ZoneInfo(os.environ.get("LINEMAKER_TZ", "America/New_York"))
SNAPSHOTS = os.environ.get("LINEMAKER_SNAPSHOTS", "snapshots")
MAX_UPLOAD = 2_000_000
CHAT_TIMEOUT = int(os.environ.get("LINEMAKER_CHAT_TIMEOUT", "240"))


def today() -> str:
    return dt.datetime.now(TZ).date().isoformat()


def _post_time() -> dt.time:
    h, m = os.environ.get("LINEMAKER_POST_TIME", "11:00").split(":")
    return dt.time(int(h), int(m), tzinfo=TZ)


async def _save_attachment(att: discord.Attachment, suffix: str) -> str:
    if att.size > MAX_UPLOAD:
        raise ValueError(f"{att.filename} is too large ({att.size:,} bytes)")
    fd, path = tempfile.mkstemp(suffix=suffix)
    with os.fdopen(fd, "wb") as f:
        f.write(await att.read())
    return path


PICK_DETAILS = {}          # button key -> pick detail (kept in memory until restart)
MAX_DETAILS = 2000
BUTTONS = (("odds", "Odds Comparison", "🏷️", odds_page), ("chart", "Hit Rate Chart", "📊", chart_page),
           ("move", "Line Movement", "📉", movement_page), ("stats", "Key Stats", "📈", stats_page))


class PickView(discord.ui.View):
    """Odds Comparison · Hit Rate Chart · Line Movement · Key Stats, answered privately."""

    def __init__(self, key: str, detail: Optional[dict] = None):
        super().__init__(timeout=None)
        detail = detail or {}
        # Bet buttons open the book with the slip loaded (Discord caps button URLs at 512).
        for label, url in (detail.get("links") or [])[:5]:
            if url and len(url) <= 512:
                book = next((b for b in ("FanDuel", "DraftKings") if b in label), label)
                text = f"Bet on {label}" if label in ("FanDuel", "DraftKings") else label
                self.add_item(discord.ui.Button(label=text[:80], url=url, row=1,
                                                emoji="🟦" if book == "FanDuel" else "🟩"))
        if "ev" in detail:
            button = discord.ui.Button(label="I bet this", emoji="📝", row=1,
                                       style=discord.ButtonStyle.success, custom_id=f"lm:bet:{key}")
            button.callback = self._bet_single(key)
            self.add_item(button)
        for i, p in enumerate((detail.get("parlays") or [])[:4]):
            button = discord.ui.Button(label=f"Log {len(p.legs)}-leg {p.book}", emoji="📝", row=2,
                                       style=discord.ButtonStyle.success, custom_id=f"lm:betp:{key}:{i}")
            button.callback = self._bet_parlay(key, i)
            self.add_item(button)
        if "ev" not in detail and detail:
            return   # a parlay card: bet buttons only
        for kind, label, emoji, page in BUTTONS:
            button = discord.ui.Button(label=label, emoji=emoji, style=discord.ButtonStyle.secondary,
                                       custom_id=f"lm:{kind}:{key}")
            button.callback = self._handler(key, page)
            self.add_item(button)

    @staticmethod
    def _bet_single(key):
        async def callback(interaction: discord.Interaction):
            detail = PICK_DETAILS.get(key)
            if detail is None:
                await interaction.response.send_message(
                    "This card is from before the bot restarted, so I can't log it. Ask again "
                    "for a fresh card.", ephemeral=True)
                return
            from .bets import leg_from_eval
            from .staking import suggested_dollars
            ev = detail["ev"]
            leg = leg_from_eval(ev, live=ev.market.notes.startswith("LIVE"))
            await interaction.response.send_modal(BetModal(
                [leg], ev.best.best_odds, ev.best.best_book,
                suggested_dollars(ev.best.blended, ev.best.best_odds), ev.best.label))
        return callback

    @staticmethod
    def _bet_parlay(key, i):
        async def callback(interaction: discord.Interaction):
            detail = PICK_DETAILS.get(key)
            if detail is None:
                await interaction.response.send_message(
                    "This card is from before the bot restarted, so I can't log it.", ephemeral=True)
                return
            from .bets import leg_from_eval
            from .staking import suggested_dollars
            p = detail["parlays"][i]
            legs = [leg_from_eval(ev, odds=o, book=p.book) for ev, o in p.legs]
            await interaction.response.send_modal(BetModal(
                legs, p.american, p.book, suggested_dollars(p.prob, p.american, parlay=True),
                f"{len(p.legs)}-leg parlay"))
        return callback

    @staticmethod
    def _handler(key, page):
        async def callback(interaction: discord.Interaction):
            detail = PICK_DETAILS.get(key)
            if detail is None:
                await interaction.response.send_message(
                    "This card is from before the bot restarted. Ask again for fresh numbers.",
                    ephemeral=True)
                return
            try:
                await interaction.response.send_message(embed=page(detail), ephemeral=True)
            except Exception as e:
                await _report_error(interaction, e)
        return callback


class BetModal(discord.ui.Modal):
    """'I bet this': stake, the odds you actually got, and the book."""

    def __init__(self, legs: list, odds: int, book: str, stake: Optional[float], what: str):
        super().__init__(title="Log this bet", timeout=600)
        self.legs, self.what = legs, what
        self.stake = discord.ui.TextInput(label="Stake ($)", placeholder="25",
                                          default=f"{stake:g}" if stake else None, max_length=12)
        self.odds = discord.ui.TextInput(label="Odds you got (American)", default=fmt_odds(odds),
                                         max_length=8)
        self.book = discord.ui.TextInput(label="Book", default=book or "", required=False,
                                         max_length=40)
        for item in (self.stake, self.odds, self.book):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        from .bets import add_bet
        try:
            stake = float(str(self.stake.value).replace("$", "").replace(",", "").strip())
            odds = int(str(self.odds.value).replace("+", "").strip())
            if stake <= 0 or abs(odds) < 100:
                raise ValueError
        except ValueError:
            await interaction.response.send_message(
                "Couldn't read that: stake should be a number like 25 and odds like -150 or +120.",
                ephemeral=True)
            return
        add_bet(self.legs, odds, stake, str(self.book.value).strip(), str(interaction.user))
        await interaction.response.send_message(
            f"✅ Logged **${stake:,.2f}** on {self.what} at **{fmt_odds(odds)}**"
            + (f" ({self.book.value})" if self.book.value else "")
            + ". It settles automatically after grading; `/record` shows your results.",
            ephemeral=True)


def _remember(detail: dict) -> str:
    import uuid
    key = uuid.uuid4().hex[:16]
    PICK_DETAILS[key] = detail
    while len(PICK_DETAILS) > MAX_DETAILS:
        PICK_DETAILS.pop(next(iter(PICK_DETAILS)))
    return key


EMBED_BUDGET = 5800   # Discord allows 6000 characters across one message's embeds


def card_style() -> str:
    """compact (default): one message per scan; full: one message per pick."""
    return "full" if os.environ.get("LINEMAKER_CARD_STYLE", "compact").strip().lower() == "full" else "compact"


def _embed_len(e: discord.Embed) -> int:
    n = len(e.title or "") + len(e.description or "") + len(e.footer.text or "" if e.footer else "")
    return n + sum(len(f.name) + len(f.value) for f in e.fields)


def _clip_embed(e: discord.Embed, chars: int) -> discord.Embed:
    d = e.description or ""
    if len(d) > chars:
        e.description = d[:max(chars - 1, 0)].rsplit("\n", 1)[0] + "…"
    return e


def compact_card(messages: list) -> tuple:
    """(embeds, view) for one message: summary + board (+ parlays); every pick's full
    write-up behind a dropdown, the scoping notes behind a button."""
    header, boards, parlays, notes, picks = None, [], [], [], []
    for embeds, detail in messages:
        if detail and "ev" in detail:
            picks.append((embeds[0], detail))
            continue
        if detail and "parlays" in detail:
            parlays.append((embeds[0], detail))
            continue
        for e in embeds:
            title, desc = e.title or "", e.description or ""
            if header is None:
                header = e
            elif title.startswith(("📊", "✅")):
                boards.append(e)
            elif " from " in desc and desc.rstrip(".").split()[-1] in ("game", "games"):
                continue          # "🏀 NBA · 10 plays from 6 games" section labels
            else:
                notes.append(e)
    embeds = [e for e in [header] + boards + [p[0] for p in parlays] if e is not None][:10]
    if not picks and not parlays:        # nothing cleared: the notes are the answer
        embeds += notes[:10 - len(embeds)]
        notes = []
    if header is not None:
        over = sum(_embed_len(e) for e in embeds) - EMBED_BUDGET
        if over > 0:
            _clip_embed(header, max(len(header.description or "") - over, 300))
    while len(embeds) > 1 and sum(_embed_len(e) for e in embeds) > EMBED_BUDGET:
        notes.insert(0, embeds.pop())        # still too long: move the last one behind Notes
    view = CompactView(picks, parlays, notes)
    return embeds, view


class CompactView(discord.ui.View):
    """One scan, one message: a dropdown for each pick's write-up and buttons, parlay
    buttons, and a Notes button. Details answer privately (only you see them)."""

    def __init__(self, picks: list, parlays: list, notes: list):
        super().__init__(timeout=None)
        from .staking import _money, stake_tier
        if picks:
            options = []
            for embed, detail in picks[:25]:
                ev = detail["ev"]
                s = ev.best
                key = _remember(dict(detail, embed=embed))
                stake = stake_tier(s.blended, s.best_odds)
                desc = f"{fmt_odds(s.best_odds)} {s.best_book} · {s.blended:.0%} hit" + (
                    f" · {_money(stake)}" if stake else "")
                options.append(discord.SelectOption(label=f"{detail['rank']}. {s.label}"[:100],
                                                    description=desc[:100], value=key))
            pick = discord.ui.Select(placeholder="🔎 See a pick: write-up, Bet buttons, I bet this",
                                     options=options, row=0, custom_id=f"lm:pick:{options[0].value}")
            pick.callback = self._show_pick(pick)
            self.add_item(pick)
        row, count = 1, 0
        for _embed, detail in parlays:
            key = _remember(detail)
            for label, url in detail.get("links") or []:
                if url and len(url) <= 512 and row <= 3:
                    self.add_item(discord.ui.Button(label=label[:80], url=url, row=row,
                                                    emoji="🟦" if "FanDuel" in label else "🟩"))
                    count += 1
                    row, count = (row + 1, 0) if count == 5 else (row, count)
            for i, p in enumerate((detail.get("parlays") or [])[:4]):
                if row > 3:
                    break
                b = discord.ui.Button(label=f"Log {len(p.legs)}-leg {p.book}", emoji="📝", row=row,
                                      style=discord.ButtonStyle.success,
                                      custom_id=f"lm:betp:{key}:{i}")
                b.callback = PickView._bet_parlay(key, i)
                self.add_item(b)
                count += 1
                row, count = (row + 1, 0) if count == 5 else (row, count)
        if len(picks) >= 2:
            pkey = _remember({"picks": [d for _e, d in picks[:25]]})
            b = discord.ui.Button(label="Turn into parlay", emoji="🎰", row=4,
                                  style=discord.ButtonStyle.primary, custom_id=f"lm:mkparlay:{pkey}")
            b.callback = self._parlay_picker(pkey)
            self.add_item(b)
        if notes:
            nkey = _remember({"notes": notes})
            b = discord.ui.Button(label="Notes & passes", emoji="ℹ️", row=4,
                                  style=discord.ButtonStyle.secondary, custom_id=f"lm:notes:{nkey}")
            b.callback = self._show_notes(nkey)
            self.add_item(b)

    @staticmethod
    def _parlay_picker(key):
        async def callback(interaction: discord.Interaction):
            detail = PICK_DETAILS.get(key)
            if detail is None:
                await interaction.response.send_message(
                    "This card is from before the bot restarted. Ask again for fresh picks.",
                    ephemeral=True)
                return
            await interaction.response.send_message(
                "🎰 **Build a parlay:** pick the legs below (or **Use all picks**). I'll price it "
                "at FanDuel and DraftKings with each book's own odds.",
                view=ParlayPickerView(detail["picks"]), ephemeral=True)
        return callback

    @staticmethod
    def _show_pick(select):
        async def callback(interaction: discord.Interaction):
            key = select.values[0]
            detail = PICK_DETAILS.get(key)
            if detail is None:
                await interaction.response.send_message(
                    "This card is from before the bot restarted. Ask again for fresh numbers.",
                    ephemeral=True)
                return
            await interaction.response.send_message(embed=detail["embed"],
                                                    view=PickView(key, detail), ephemeral=True)
        return callback

    @staticmethod
    def _show_notes(key):
        async def callback(interaction: discord.Interaction):
            detail = PICK_DETAILS.get(key)
            if detail is None:
                await interaction.response.send_message("These notes expired with a restart.",
                                                        ephemeral=True)
                return
            await interaction.response.send_message(embeds=detail["notes"][:10], ephemeral=True)
        return callback


def parlay_card(legs: list) -> tuple:
    """(embed, view) for a parlay you built: price, payout and hit chance at each book,
    with Bet buttons that load every leg and Log buttons."""
    from .betlinks import parlay_link
    from .odds import fmt_odds as _fo
    from .parlay import custom_parlays, leg_price, same_game
    from .staking import _money, stake_tier
    lines = []
    for ev in legs:
        fd, dk = leg_price(ev, "FanDuel"), leg_price(ev, "DraftKings")
        lines.append(f"• **{ev.best.label}** · FD {_fo(fd) if fd else '—'} · DK {_fo(dk) if dk else '—'}"
                     f" · {ev.best.blended:.0%}")
    embed = discord.Embed(title=f"🎰 Your {len(legs)}-Leg Parlay", color=0xF1C40F,
                          description="\n".join(lines)[:3500])
    built = custom_parlays(legs)
    view = discord.ui.View(timeout=None)
    for p, missing in built:
        stake = stake_tier(p.prob, p.american, parlay=True)
        from .discord_format import _style
        name, _tag, emoji, _c = _style(p.book)
        value = (f"**{_fo(p.american)}** · hit chance **{p.prob:.0%}** · EV {p.ev:+.1f}%\n"
                 + (f"{_money(stake)} pays **{_money(stake * p.decimal)}**" if stake else
                    f"$10 pays **${10 * p.decimal:,.2f}**"))
        if missing:
            value += f"\n⚠️ Not offered here: {', '.join(missing)[:200]}"
        embed.add_field(name=f"{emoji} {name}", value=value, inline=False)
        url = parlay_link(p)
        if url and len(url) <= 512:
            view.add_item(discord.ui.Button(label=f"Bet on {p.book}", url=url,
                                            emoji="🟦" if p.book == "FanDuel" else "🟩"))
        key = _remember({"parlays": [p]})
        b = discord.ui.Button(label=f"Log {p.book} parlay", emoji="📝", style=discord.ButtonStyle.success,
                              custom_id=f"lm:betp:{key}:0")
        b.callback = PickView._bet_parlay(key, 0)
        view.add_item(b)
    if not built:
        embed.add_field(name="Can't price this one",
                        value="Neither FanDuel nor DraftKings offers at least two of these legs.",
                        inline=False)
    notes = ["Hit chance multiplies each leg's estimate, so it assumes the legs are independent. "
             "Books' parlay prices can differ slightly from the multiplied odds."]
    sg = same_game(legs)
    if sg:
        notes.insert(0, f"⚠️ **Same-game legs** ({'; '.join(sg)[:300]}): these move together and "
                        "the book will treat them as a same-game parlay with its own (usually "
                        "worse) price. Check the slip before you bet.")
    embed.set_footer(text=" ".join(n.replace("**", "") for n in notes)[:2048])
    return embed, view


class ParlayPickerView(discord.ui.View):
    """Private menu: choose the legs from a scan's picks, or use them all."""

    def __init__(self, picks: list):
        super().__init__(timeout=900)
        self.picks = picks
        options = [discord.SelectOption(label=f"{d['rank']}. {d['ev'].best.label}"[:100],
                                        description=f"{fmt_odds(d['ev'].best.best_odds)} "
                                                    f"{d['ev'].best.best_book} · {d['ev'].best.blended:.0%} hit"[:100],
                                        value=str(i))
                   for i, d in enumerate(picks)]
        select = discord.ui.Select(placeholder="Choose 2 or more legs", options=options,
                                   min_values=2, max_values=len(options))
        select.callback = self._build(select)
        self.add_item(select)
        everything = discord.ui.Button(label=f"Use all {len(picks)} picks", emoji="🎯",
                                       style=discord.ButtonStyle.primary)
        everything.callback = self._build(None)
        self.add_item(everything)

    def _build(self, select):
        async def callback(interaction: discord.Interaction):
            chosen = ([self.picks[int(v)] for v in select.values] if select is not None
                      else self.picks)
            embed, view = parlay_card([d["ev"] for d in chosen])
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        return callback


async def _send_card(send, messages: list, markdown=None, filename=None, text: str = ""):
    """Post a value scan. Compact (default): one message with a pick dropdown. Full: header
    and board, then one message per pick with its buttons, then the notes. Plain embed
    lists are posted as-is."""
    if messages and not isinstance(messages[0], tuple):
        messages = [(messages, None)]
    if card_style() == "compact" and any(d is not None for _e, d in messages):
        embeds, view = compact_card(messages)
        await send(content=text[:2000] or None, embeds=embeds, view=view)
        return
    if text:
        await send(content=text[:2000])
    for embeds, detail in messages:
        if detail is not None:
            await send(embeds=embeds, view=PickView(_remember(detail), detail))
            continue
        for group in chunk_embeds(embeds):
            await send(embeds=group)


def build_pro(slate_path: str, date: str, cfg: ProConfig):
    from .chat import enrich
    markets = load_pro_markets(slate_path)
    sections = []
    for sport in sorted({m.sport for m in markets}):
        res = run_pro([m for m in markets if m.sport == sport], date, cfg, Freezer(SNAPSHOTS, date))
        sections.append((sport, res, enrich(res, sport, date)))
    return scan_messages(sections, date, cfg), None


def build_live(sports: list, projections: list, date: str, cfg: ProConfig,
               props: Optional[bool] = None):
    api = OddsAPI()
    lr = run_live(api, sports, projections, date, TZ, cfg, Freezer(SNAPSHOTS, date), props)
    from .chat import enrich
    sections = [(sport, lr.result, enrich(lr.result, sport, date)) for sport in sports[:1]]
    return scan_messages(sections, date, cfg, f"{'/'.join(sports)} Projection Check"), None, \
        rows_csv(lr.rows)


BLUE_BOARD = 0x3498DB
SPORT_CHOICES = [app_commands.Choice(name=k, value=k) for k in SPORT_KEYS]
FP_CHOICES = [app_commands.Choice(name=k, value=k)
              for k in ("NBA", "WNBA", "NFL", "NCAAF", "MLB", "NHL")]
KIND_CHOICES = [app_commands.Choice(name="Player props", value="props"),
                app_commands.Choice(name="Moneylines, spreads, totals", value="games"),
                app_commands.Choice(name="Live in-game lines", value="live")]


ARMED_NOW = []   # leagues whose pre-tip scans the last /picks switched on


def build_auto(sports: list, date: str, cfg: ProConfig, kind: str = "props",
               refresh: bool = False):
    """Uses the same cached odds as chat, so asking twice inside the window costs nothing."""
    from .chat import enrich, slate_for
    from .pro import rows_to_markets
    sections = []
    for sport in sports:
        rows, slate, _left, warning, source = slate_for(sport, date, kind, refresh)
        if kind == "props" and date == today():
            from .alerts import arm_pretip
            if arm_pretip(sport, date):
                ARMED_NOW.append(sport)
        freezer, tracker = Freezer(SNAPSHOTS, date), None
        if kind == "live":
            from .live import live_settings
            cfg, tracker = live_settings(ProConfig(), date)
            freezer = None
        res = run_pro(rows_to_markets(rows), date, cfg, freezer, tracker)
        res.slate, res.source_warning, res.projection_source = slate, warning, source
        res.kind = kind
        from .chat import slate_age_minutes
        res.odds_age = slate_age_minutes(sport, date, kind)
        sections.append((sport, res, enrich(res, sport, date)))
    return scan_messages(sections, date, cfg), None


def build_lite(slate_path: str, date: str, cfg: Config):
    res = run(load_markets(slate_path), date, cfg)
    return lite_embeds(res, cfg), render(res, cfg)


def home_channel() -> Optional[int]:
    raw = os.environ.get("LINEMAKER_HOME_CHANNEL", "").strip()
    return int(raw) if raw else None


def in_home(channel, home: Optional[int]) -> bool:
    """True when no home channel is set, or the channel (or a thread's parent) is it."""
    if home is None:
        return True
    return getattr(channel, "id", None) == home or getattr(channel, "parent_id", None) == home


class HomeChannelTree(app_commands.CommandTree):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        home = home_channel()
        if in_home(interaction.channel, home):
            return True
        await interaction.response.send_message(f"Linemaker only works in <#{home}>.",
                                                ephemeral=True)
        return False


class LinemakerBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        self.chat = None
        if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("GEMINI_API_KEY"):
            from .chat import LinemakerChat
            self.chat = LinemakerChat()
            intents.message_content = True   # privileged: enable it in the Developer Portal
        super().__init__(intents=intents)
        self.tree = HomeChannelTree(self)
        self.home = home_channel()
        self.chat_channels = {int(c) for c in
                              os.environ.get("LINEMAKER_CHAT_CHANNELS", "").split(",") if c.strip()}
        if self.home:
            self.chat_channels.add(self.home)

    async def setup_hook(self):
        # Linemaker owns this application: each sync replaces the whole command list, so
        # commands left over from whatever the bot ran before are removed.
        guild_id = os.environ.get("LINEMAKER_GUILD_ID")
        if guild_id:
            guild = discord.Object(id=int(guild_id))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            # Clear old global commands too, or they'd show up next to the server ones.
            self.tree.clear_commands(guild=None)
            await self.tree.sync()
        else:
            await self.tree.sync()
        log.info("slash commands synced: %s",
                 ", ".join(sorted(c.name for c in self.tree.get_commands(
                     guild=discord.Object(id=int(guild_id)) if guild_id else None))))
        # The daily post spends credits every day, so it only runs when a time is set.
        if os.environ.get("LINEMAKER_POST_TIME", "").strip() and (
                os.environ.get("LINEMAKER_CHANNEL_ID") or self.home) and (
                os.environ.get("LINEMAKER_SLATE_DIR") or os.environ.get("LINEMAKER_PROJECTIONS_DIR")
                or os.environ.get("LINEMAKER_SPORTS")):
            daily_post.change_interval(time=_post_time())
            daily_post.start()
        # Grade yesterday every morning (~2 credits per league with tracked game lines; props
        # are free), which also re-fits the weights. LINEMAKER_AUTOGRADE_TIME= (blank) turns it off.
        from .alerts import news_minutes, pretip_minutes
        if self.alert_channel_id() and pretip_minutes():
            pretip_loop.start()
        if self.alert_channel_id() and news_minutes():
            news_loop.change_interval(minutes=news_minutes())
            news_loop.start()
        weekly_at = os.environ.get("LINEMAKER_WEEKLY_TIME", "10:00").strip()
        if weekly_at and self.alert_channel_id():
            h, m = weekly_at.split(":")
            weekly_post.change_interval(time=dt.time(int(h), int(m), tzinfo=TZ))
            weekly_post.start()
        grade_at = os.environ.get("LINEMAKER_AUTOGRADE_TIME", "09:30").strip()
        if grade_at:
            h, m = grade_at.split(":")
            daily_grade.change_interval(time=dt.time(int(h), int(m), tzinfo=TZ))
            daily_grade.start()

    def alert_channel_id(self) -> Optional[int]:
        raw = os.environ.get("LINEMAKER_CHANNEL_ID") or self.home
        return int(raw) if raw else None

    async def on_ready(self):
        log.info("logged in as %s (chat %s)", self.user,
                 f"on: {self.chat.provider}" if self.chat else "off")
        asyncio.get_running_loop().create_task(_warm_up())
        if self.home is not None:
            ch = self.get_channel(self.home)
            if ch is None:
                log.warning("home channel %s not found: check LINEMAKER_HOME_CHANNEL, and that "
                            "the bot has been added to that channel", self.home)
            else:
                log.info("home channel: #%s (answers every message there)", ch.name)
        else:
            log.info("no home channel set: answers @mentions and DMs")

    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        from .busy import busy
        with busy():   # the auto-updater waits while a reply is in progress
            await self._on_message(message)

    async def _on_message(self, message: discord.Message):
        where = f"#{getattr(message.channel, 'name', 'DM')} ({message.channel.id})"
        if self.chat is None:
            log.info("message in %s skipped: chat is off (no GEMINI_API_KEY or "
                     "ANTHROPIC_API_KEY in .env)", where)
            return
        if self.home is not None:
            if not in_home(message.channel, self.home):
                log.info("message in %s skipped: not the home channel (%s)", where, self.home)
                return
        else:
            is_dm = message.guild is None
            mentioned = self.user in message.mentions
            if not (is_dm or mentioned or message.channel.id in self.chat_channels):
                log.info("message in %s skipped: bot not @mentioned (set "
                         "LINEMAKER_HOME_CHANNEL to answer every message there)", where)
                return
        if not message.content and not message.attachments:
            log.warning("message in %s arrived empty: turn on Message Content Intent in the "
                        "Developer Portal (Bot -> Privileged Gateway Intents)", where)
            return
        log.info("message in %s from %s: answering", where, message.author.display_name)
        text = message.content
        for token in (f"<@{self.user.id}>", f"<@!{self.user.id}>"):
            text = text.replace(token, "")
        text = text.strip()
        csv_text = None
        for att in message.attachments:
            if att.filename.lower().endswith(".csv") and att.size <= MAX_UPLOAD:
                csv_text = (await att.read()).decode("utf-8-sig")
                break
        if not text and not csv_text:
            return
        from .chat import split_for_discord
        try:
            async with message.channel.typing():
                turn = await asyncio.wait_for(
                    self.chat.respond(message.channel.id, message.author.display_name,
                                      text or "Price the attached projections.", csv_text),
                    timeout=CHAT_TIMEOUT)
        except asyncio.TimeoutError:
            log.warning("chat timed out after %ss", CHAT_TIMEOUT)
            await message.reply(f"⏱️ That took longer than {CHAT_TIMEOUT}s, so I stopped. The "
                                "console log shows which step was slow. Try again, or use "
                                "`/picks` directly.")
            return
        except Exception as e:
            log.exception("chat failed")
            await message.reply(f"⚠️ Something went wrong: {type(e).__name__}: {e}")
            return
        chunks = split_for_discord(turn.text)
        files = [discord.File(io.BytesIO(md.encode()), filename=name) for name, md in turn.files]
        cards = []
        if turn.results:
            if all((ex or {}).get("mode") == "volume" for _s, _r, ex in turn.results):
                from .pro import volume_config
                from .scan_format import volume_messages
                cards = volume_messages(turn.results, today(), volume_config())
            else:
                cards = scan_messages(turn.results, today(), ProConfig())
        none = discord.AllowedMentions.none()
        # A short reply (the "I'll re-check before tip" note) rides on the card itself.
        inline = cards and not files and card_style() == "compact" and len(chunks) <= 1
        if not inline:
            for i, chunk in enumerate(chunks):
                last = i == len(chunks) - 1
                send = message.reply if i == 0 else message.channel.send
                await send(chunk, files=files if last else [], allowed_mentions=none)
        if cards:
            first = inline or not chunks

            async def send_cards(**kw):
                nonlocal first
                fn = message.reply if first else message.channel.send
                first = False
                return await fn(allowed_mentions=none, **kw)
            await _send_card(send_cards, cards, text=chunks[0] if inline and chunks else "")


bot = LinemakerBot()


async def _report_error(interaction: discord.Interaction, err: Exception):
    log.exception("command failed")
    msg = f"⚠️ {type(err).__name__}: {err}"
    if "OUT_OF_USAGE_CREDITS" in str(err) or "401" in str(err):
        from .chat import friendly_error
        msg = friendly_error("Odds", err)
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)


@bot.tree.command(name="pro", description="Simulation + de-vig + market-blend card from a slate CSV")
@app_commands.describe(
    slate="Pro slate CSV", date="Slate date YYYY-MM-DD (default today)",
    odds_min="Shortest odds allowed, e.g. -200", odds_max="Longest odds allowed, e.g. -1000",
    min_prob="Blended probability floor, e.g. 0.70",
    confirmed_only="Only price games with confirmed lineups/inactives",
    devig="De-vig method (default proportional)")
@app_commands.choices(devig=[app_commands.Choice(name="proportional", value="proportional"),
                             app_commands.Choice(name="shin", value="shin")])
async def pro_cmd(interaction: discord.Interaction, slate: discord.Attachment,
                  date: Optional[str] = None, odds_min: Optional[int] = None,
                  odds_max: Optional[int] = None, min_prob: Optional[float] = None,
                  confirmed_only: bool = False,
                  devig: Optional[app_commands.Choice[str]] = None):
    await interaction.response.defer(thinking=True)
    try:
        date = date or today()
        cfg = ProConfig(min_odds=odds_min, max_odds=odds_max, min_prob=min_prob,
                        confirmed_only=confirmed_only,
                        devig_method=devig.value if devig else "proportional")
        path = await _save_attachment(slate, ".csv")
        try:
            embeds, md = await asyncio.to_thread(build_pro, path, date, cfg)
        finally:
            os.unlink(path)
        await _send_card(interaction.followup.send, embeds, md, f"pro_card_{date}.md")
    except Exception as e:
        await _report_error(interaction, e)


@bot.tree.command(name="lite", description="Hit-rate probability-gap card from a slate CSV")
@app_commands.describe(
    slate="Lite slate CSV", date="Slate date YYYY-MM-DD (default today)",
    odds_min="Shortest odds allowed (default -200)", odds_max="Longest odds allowed (default -1000)",
    min_prob="True-probability floor (default 0.70)")
async def lite_cmd(interaction: discord.Interaction, slate: discord.Attachment,
                   date: Optional[str] = None, odds_min: Optional[int] = None,
                   odds_max: Optional[int] = None, min_prob: Optional[float] = None):
    await interaction.response.defer(thinking=True)
    try:
        date = date or today()
        cfg = Config()
        for attr, val in (("min_odds", odds_min), ("max_odds", odds_max),
                          ("min_true_prob", min_prob)):
            if val is not None:
                setattr(cfg, attr, val)
        path = await _save_attachment(slate, ".csv")
        try:
            embeds, md = await asyncio.to_thread(build_lite, path, date, cfg)
        finally:
            os.unlink(path)
        await _send_card(interaction.followup.send, embeds, md, f"card_{date}.md")
    except Exception as e:
        await _report_error(interaction, e)


TRACKING = os.environ.get("LINEMAKER_TRACKING_DIR", "tracking")
from .grading import grade_day  # noqa: E402  (shared with chat's grade tool)


@bot.tree.command(name="grade", description="Score a day's picks and log results for calibration")
@app_commands.describe(results="Optional results CSV (props are scored from box scores automatically)",
                       date="Slate date YYYY-MM-DD (default today)",
                       auto="Fetch final scores for game lines from The Odds API (default on)")
async def grade_cmd(interaction: discord.Interaction, results: Optional[discord.Attachment] = None,
                    date: Optional[str] = None, auto: bool = True):
    await interaction.response.defer(thinking=True)
    try:
        date = date or today()
        rows = []
        if results is not None:
            if results.size > MAX_UPLOAD:
                raise ValueError("results file too large")
            rows = list(csv.DictReader(io.StringIO((await results.read()).decode("utf-8-sig"))))
        summary, remaining, tracked = await asyncio.to_thread(grade_day, date, rows, auto)
        if not summary and not tracked:
            await interaction.followup.send(f"Nothing graded for {date} yet: games may not be "
                                            "final, or nothing was tracked that day.")
            return
        lines = summary + [f"- 📊 Results logged for **{tracked}** tracked markets "
                           "(`/calibrate` shows the full report)"]
        embed = discord.Embed(title=f"📈 Grading · {date}", description="\n".join(lines),
                              color=BLUE_BOARD)
        if remaining is not None:
            embed.set_footer(text=f"Game lines scored from final scores · credits left: {remaining}")
        await interaction.followup.send(embed=embed)
    except Exception as e:
        await _report_error(interaction, e)


@bot.tree.command(name="record", description="Your logged bets: open bets, record, profit and ROI")
@app_commands.describe(days="How many days back (default 30; 0 = all time)")
async def record_cmd(interaction: discord.Interaction, days: Optional[int] = 30):
    await interaction.response.defer(thinking=True)
    try:
        from .bets import settle, summary
        await asyncio.to_thread(settle)
        s = await asyncio.to_thread(summary, days or None, today())
        span = f"last {days} days" if days else "all time"
        lines = []
        if s["settled"]:
            roi = f"{s['roi']:+.1%}" if s["roi"] is not None else "—"
            lines.append(f"**{s['won']}-{s['lost']}" + (f"-{s['push']}" if s["push"] else "")
                         + f"** · staked ${s['staked']:,.2f} · profit **{fmt_money(s['profit'])}** · ROI **{roi}**")
            for kind, k in sorted(s["by_kind"].items()):
                kroi = k["profit"] / k["staked"] if k["staked"] else 0
                lines.append(f"• {kind.title()}s: {k['won']}/{k['n']} won · {fmt_money(k['profit'])} ({kroi:+.1%})")
            lines.append("")
            lines.append("**Recent:**")
            for b in sorted(s["settled"], key=lambda b: b.get("settled", ""), reverse=True)[:8]:
                mark = {"won": "✅", "lost": "❌", "push": "➖"}[b["status"]]
                what = b["legs"][0]["label"] if b["kind"] == "single" else f"{len(b['legs'])}-leg parlay"
                lines.append(f"{mark} {what} {fmt_odds(b['odds'])} · ${b['stake']:,.0f} → {fmt_money(b.get('profit', 0))}")
        else:
            lines.append("No settled bets yet. Tap **📝 I bet this** on a pick (or **Log parlay**) "
                         "after you place it, and it settles automatically after grading.")
        if s["open"]:
            lines += ["", f"**Open ({len(s['open'])}):** " + "; ".join(
                (b["legs"][0]["label"] if b["kind"] == "single" else f"{len(b['legs'])}-leg parlay")
                + f" ${b['stake']:,.0f}" for b in s["open"][:10])]
        embed = discord.Embed(title=f"🧾 Your Bets · {span}", description="\n".join(lines)[:4000],
                              color=0x2ECC71 if s["profit"] >= 0 else 0xE74C3C)
        await interaction.followup.send(embed=embed)
    except Exception as e:
        await _report_error(interaction, e)


@bot.tree.command(name="tips", description="How to think about these bets long-term")
async def tips_cmd(interaction: discord.Interaction):
    from .scan_format import tips_embed
    await interaction.response.send_message(embed=tips_embed())


@bot.tree.command(name="clv", description="Closing-line value: did the market move toward our picks?")
@app_commands.describe(days="How many days back (default 14)")
async def clv_cmd(interaction: discord.Interaction, days: Optional[int] = 14):
    await interaction.response.defer(thinking=True)
    try:
        from .tracking import clv_lines, load_records
        recs = await asyncio.to_thread(load_records, TRACKING, days or None, today())
        embed = discord.Embed(title=f"📏 Closing-Line Value · last {days} days", color=0x3498DB,
                              description="\n\n".join(clv_lines(recs))[:4000])
        await interaction.followup.send(embed=embed)
    except Exception as e:
        await _report_error(interaction, e)


@bot.tree.command(name="calibrate", description="Test blend weights against graded results")
@app_commands.describe(days="How many days of tracked results to use (default all)")
async def calibrate_cmd(interaction: discord.Interaction, days: Optional[int] = None):
    await interaction.response.defer(thinking=True)
    try:
        from .tracking import calibrate, load_records
        report = await asyncio.to_thread(lambda: calibrate(load_records(TRACKING, days, today())))
        body = report.replace("# 🧪 Linemaker Calibration\n\n", "")
        embed = discord.Embed(title="🧪 Linemaker Calibration", color=0x9B59B6,
                              description=body[:4000])
        from .autotune import autotune_on, current_summary
        embed.set_footer(text=(current_summary() + " · re-fit automatically after every grade"
                               if autotune_on() else
                               "Auto-tuning is off: change LINEMAKER_MODEL_WEIGHT in .env and restart")[:2048])
        await interaction.followup.send(embed=embed)
    except Exception as e:
        await _report_error(interaction, e)


@bot.tree.command(name="devig", description="Fair probabilities, overround and hold for a 2- or 3-way market")
@app_commands.describe(side_a="Odds for one side: -115, +150 or decimal 2.30",
                       side_b="Odds for the other side",
                       side_c="Third outcome for 3-way markets (e.g. the draw)",
                       method="De-vig method (default proportional)")
@app_commands.choices(method=[app_commands.Choice(name="proportional", value="proportional"),
                              app_commands.Choice(name="shin", value="shin")])
async def devig_cmd(interaction: discord.Interaction, side_a: str, side_b: str,
                    side_c: Optional[str] = None,
                    method: Optional[app_commands.Choice[str]] = None):
    try:
        odds = [parse_odds(x) for x in (side_a, side_b, side_c) if x]
        how = method.value if method else "proportional"
        fair = no_vig(*odds, method=how)
        lines = [f"{fmt_odds(o):>6}  implied {implied_probability(o):6.2%}  fair {p:6.2%}  "
                 f"fair odds {fmt_odds(fair_odds(p)):>5} / {1 / p:.2f}"
                 for o, p in zip(odds, fair)]
        lines.append(f"total implied {1 + overround(*odds):.2%} · overround {overround(*odds):.2%}"
                     f" · hold {hold(*odds):.2%} · {how}")
        await interaction.response.send_message("```\n" + "\n".join(lines) + "\n```")
    except Exception as e:
        await _report_error(interaction, e)


@bot.tree.command(name="ev", description="Expected value of a bet at a price, per $100")
@app_commands.describe(probability="Your win probability, as 38 or 0.38",
                       odds="Price: +200, -110 or decimal 3.00",
                       stake="Stake in dollars (default 100)")
async def ev_cmd(interaction: discord.Interaction, probability: float, odds: str,
                 stake: float = 100.0):
    try:
        p = probability / 100 if probability > 1 else probability
        o = parse_odds(odds)
        ev = expected_value(p, o, stake)
        verdict = "✅ positive EV" if ev > 0 else "❌ negative EV"
        await interaction.response.send_message(
            f"{p:.1%} at {fmt_odds(o)} ({american_to_decimal(o):.2f}): EV **{fmt_money(ev)}** on "
            f"${stake:g} · break-even {implied_probability(o):.2%} · fair {fmt_odds(fair_odds(p))}"
            f" · {verdict}")
    except Exception as e:
        await _report_error(interaction, e)


@bot.tree.command(name="fair", description="Convert a win probability to fair American odds")
@app_commands.describe(probability="Win probability, as 58 or 0.58")
async def fair_cmd(interaction: discord.Interaction, probability: float):
    try:
        p = probability / 100 if probability > 1 else probability
        await interaction.response.send_message(f"{p:.1%} → fair **{fmt_odds(fair_odds(p))}**")
    except Exception as e:
        await _report_error(interaction, e)


@bot.tree.command(name="live", description="Live Odds API prices + your projections → pro card")
@app_commands.describe(sport="League", projections="Projections CSV (sport, subject, market, mean or inputs)",
                       date="Slate date YYYY-MM-DD (default today)",
                       props="Fetch player props (default: only if a projection needs them)",
                       devig="De-vig method (default proportional)")
@app_commands.choices(sport=SPORT_CHOICES,
                      devig=[app_commands.Choice(name="proportional", value="proportional"),
                             app_commands.Choice(name="shin", value="shin")])
async def live_cmd(interaction: discord.Interaction, sport: app_commands.Choice[str],
                   projections: discord.Attachment, date: Optional[str] = None,
                   props: Optional[bool] = None,
                   devig: Optional[app_commands.Choice[str]] = None):
    await interaction.response.defer(thinking=True)
    try:
        date = date or today()
        cfg = ProConfig(devig_method=devig.value if devig else "proportional")
        if projections.size > MAX_UPLOAD:
            raise ValueError("projections file too large")
        rows = read_projections(io.StringIO((await projections.read()).decode("utf-8-sig")))
        embeds, md, slate = await asyncio.to_thread(build_live, [sport.value], rows, date, cfg, props)
        await _send_card(interaction.followup.send, embeds, md, f"live_card_{sport.value}_{date}.md")
        await interaction.followup.send(file=discord.File(
            io.BytesIO(slate.encode()), filename=f"live_slate_{sport.value}_{date}.csv"))
    except Exception as e:
        await _report_error(interaction, e)


@bot.tree.command(name="picks", description="Today's best plays: projections vs live FD/DK odds")
@app_commands.describe(sport="League", bet="Player props (default) or game lines",
                       date="Slate date YYYY-MM-DD (default today)",
                       refresh="Buy fresh odds now instead of reusing the last 30 minutes")
@app_commands.choices(sport=FP_CHOICES, bet=KIND_CHOICES)
async def picks_cmd(interaction: discord.Interaction, sport: app_commands.Choice[str],
                    bet: Optional[app_commands.Choice[str]] = None, date: Optional[str] = None,
                    refresh: bool = False):
    await interaction.response.defer(thinking=True)
    try:
        date = date or today()
        kind = bet.value if bet else "props"
        ARMED_NOW.clear()
        embeds, md = await asyncio.to_thread(build_auto, [sport.value], date, ProConfig(), kind,
                                             refresh)
        await _send_card(interaction.followup.send, embeds, md, f"picks_{sport.value}_{date}.md")
        from .chat import pretip_note
        if pretip_note(ARMED_NOW):
            await interaction.followup.send(pretip_note(ARMED_NOW))
    except Exception as e:
        await _report_error(interaction, e)


@bot.tree.command(name="parlay", description="Volume mode: likeliest legs and ready-made parlays")
@app_commands.describe(sport="League", bet="Player props (default) or game lines",
                       date="Slate date YYYY-MM-DD (default today)",
                       refresh="Buy fresh odds now instead of reusing the last 30 minutes")
@app_commands.choices(sport=FP_CHOICES, bet=KIND_CHOICES)
async def parlay_cmd(interaction: discord.Interaction, sport: app_commands.Choice[str],
                     bet: Optional[app_commands.Choice[str]] = None, date: Optional[str] = None,
                     refresh: bool = False):
    await interaction.response.defer(thinking=True)
    try:
        date = date or today()

        def work():
            from .chat import LinemakerChat
            from .pro import volume_config
            from .scan_format import volume_messages
            helper = bot.chat or _TOOL_HELPER.setdefault("chat", LinemakerChat.__new__(LinemakerChat))
            if not hasattr(helper, "_feed_cache"):
                helper._feed_cache = {}
            ctx = {"files": [], "attached_rows": []}
            LinemakerChat.run_tool(helper, "find_plays", {"sport": sport.value, "mode": "volume",
                                                          "date": date,
                                                          "kind": bet.value if bet else "props",
                                                          "refresh": refresh},
                                                   ctx)
            return volume_messages(ctx["results"], date, volume_config())
        messages = await asyncio.to_thread(work)
        await _send_card(interaction.followup.send, messages)
    except Exception as e:
        await _report_error(interaction, e)


_TOOL_HELPER = {}


@bot.tree.command(name="odds", description="Today's market board (no-vig) + a CSV to fill with projections")
@app_commands.describe(sport="League", date="Slate date YYYY-MM-DD (default today)",
                       props="Include player props in the CSV (costs credits per game)")
@app_commands.choices(sport=SPORT_CHOICES)
async def odds_cmd(interaction: discord.Interaction, sport: app_commands.Choice[str],
                   date: Optional[str] = None, props: bool = False):
    await interaction.response.defer(thinking=True)
    try:
        date = date or today()

        def work():
            api = OddsAPI()
            feed = fetch_feed(api, [sport.value], date, TZ, props)
            return board_lines(feed), rows_csv([feed_row(f) for f in feed]), api.quota.remaining

        lines, template, remaining = await asyncio.to_thread(work)
        embed = discord.Embed(title=f"📋 {sport.value} board — {date}", color=BLUE_BOARD)
        by_game = {}
        for game, _kind, desc, n_books in lines:
            by_game.setdefault(game, []).append(f"{desc} ({n_books} books)")
        for game, descs in list(by_game.items())[:25]:
            embed.add_field(name=game[:256], value="\n".join(descs)[:1024], inline=False)
        if not by_game:
            embed.description = "No games with prices on this date."
        embed.set_footer(text=f"Consensus no-vig probabilities · credits left: {remaining} · "
                              "fill `mean` in the CSV and pass it to /live")
        await interaction.followup.send(embed=embed, file=discord.File(
            io.BytesIO(template.encode()), filename=f"odds_{sport.value}_{date}.csv"))
    except Exception as e:
        await _report_error(interaction, e)


@bot.tree.command(name="quota", description="The Odds API credits left on every key (free call)")
async def quota_cmd(interaction: discord.Interaction):
    try:
        from .oddsapi import key_quotas
        rows = await asyncio.to_thread(key_quotas)
        if not rows:
            await interaction.response.send_message("No ODDS_API_KEY set in .env.", ephemeral=True)
            return
        lines, total = [], 0
        for n, masked, remaining, used, error, active in rows:
            tag = " · **in use**" if active else ""
            if error:
                lines.append(f"🔴 Key #{n} ({masked}): {error}{tag}")
                continue
            total += remaining or 0
            dot = "🟢" if (remaining or 0) > 50 else ("🟡" if remaining else "🔴")
            lines.append(f"{dot} Key #{n} ({masked}): **{remaining}** left · {used} used{tag}")
        embed = discord.Embed(title="🔋 The Odds API credits", color=BLUE_BOARD,
                              description="\n".join(lines))
        embed.add_field(name="Total remaining", value=f"**{total}** across {len(rows)} "
                                                      f"key{'s' if len(rows) != 1 else ''}")
        await interaction.response.send_message(embed=embed, ephemeral=True)
    except Exception as e:
        await _report_error(interaction, e)


async def _warm_up():
    """Rosters and defense-vs-position for today, in the background (first scan is faster)."""
    from .espn_projections import warm_up
    for sport in _alert_sports():
        try:
            await asyncio.to_thread(warm_up, sport, today())
        except Exception as e:
            log.warning("warm-up %s: %s", sport, e)


def _alert_sports(override: str = "") -> list:
    from .gamelogs import PATHS
    raw = (os.environ.get(override, "").strip() if override else "") or os.environ.get("LINEMAKER_SPORTS", "NBA")
    return [s.strip().upper() for s in raw.split(",") if s.strip().upper() in PATHS]


def _todays_events(sport: str, date: str) -> list:
    """Today's games from The Odds API's free /events list."""
    from .oddsapi import slate_window
    start, end = slate_window(date, TZ)
    return OddsAPI().events(sport, start, end)


@tasks.loop(minutes=5)
async def pretip_loop():
    """Props for each game LINEMAKER_PRETIP_MINUTES before it tips; posts only real plays."""
    from .alerts import load_state, pretip_due, pretip_minutes, save_state
    from .chat import scan_events
    date = today()
    channel = bot.get_channel(bot.alert_channel_id() or 0)
    if channel is None:
        return
    done = set(load_state(f"pretip_{date}.json", []))
    now = dt.datetime.now(dt.timezone.utc)
    from .alerts import pretip_sports
    for sport in pretip_sports(date, _alert_sports("LINEMAKER_PRETIP_SPORTS")):
        try:
            events = await asyncio.to_thread(_todays_events, sport, date)
        except Exception as e:
            log.warning("pre-tip %s: %s", sport, e)
            continue
        for group in pretip_due(events, now, pretip_minutes(), done):
            ids = {e["id"] for e in group}
            done |= ids
            save_state(f"pretip_{date}.json", sorted(done))
            names = ", ".join(f"{e['away_team']} @ {e['home_team']}" for e in group)
            try:
                messages, _res = await asyncio.to_thread(
                    scan_events, sport, date, ids, f"{SPORT_NAMES.get(sport, sport)} Pre-Tip Scan")
            except Exception:
                log.exception("pre-tip scan failed: %s", names)
                continue
            if messages:
                await _send_card(channel.send, messages)
            else:
                log.info("pre-tip %s: nothing cleared the gates (%s)", sport, names)


@tasks.loop(minutes=12)
async def news_loop():
    """Re-price a game when a rotation player is newly ruled Out (basketball)."""
    from .alerts import NEWS_SPORTS, load_state, news_events, save_state
    from .chat import drop_slate, scan_events
    from .espn_projections import injuries
    date = today()
    channel = bot.get_channel(bot.alert_channel_id() or 0)
    if channel is None:
        return
    cap = int(os.environ.get("LINEMAKER_NEWS_MAX_ALERTS", "8") or 0)
    sent = load_state(f"news_sent_{date}.json", 0)
    now = dt.datetime.now(dt.timezone.utc)
    for sport in [s for s in _alert_sports() if s in NEWS_SPORTS]:
        try:
            current = await asyncio.to_thread(injuries, sport)
            if not current:
                continue
            previous = load_state(f"news_{sport}_{date}.json", None)
            events = await asyncio.to_thread(_todays_events, sport, date) if previous is not None else []
            hits = await asyncio.to_thread(news_events, sport, date, events, now, previous, current)
            save_state(f"news_{sport}_{date}.json", current)
        except Exception:
            log.exception("injury news check failed (%s)", sport)
            continue
        for ev, players in hits:
            if sent >= cap:
                log.info("news alerts: daily cap of %d reached", cap)
                break
            sent += 1
            save_state(f"news_sent_{date}.json", sent)
            who = " & ".join(players)
            game = f"{ev['away_team']} @ {ev['home_team']}"
            drop_slate(sport, date)          # the next full scan should see the news too
            try:
                messages, res = await asyncio.to_thread(
                    scan_events, sport, date, {ev["id"]}, f"🚨 {who} OUT · Re-Price", False)
            except Exception:
                log.exception("news re-price failed: %s", game)
                continue
            if res.picks:
                await _send_card(channel.send, messages)
            else:
                await channel.send(embed=discord.Embed(
                    title=f"🚨 {who} ruled OUT · {game}", color=0xE74C3C,
                    description=(f"Re-priced {game} with the usage bump for his teammates. "
                                 f"{len(res.evaluated)} props priced; nothing cleared the gates, "
                                 "so the books have likely already adjusted.")))


@tasks.loop(time=dt.time(10, 0, tzinfo=TZ))
async def weekly_post():
    """Monday (LINEMAKER_WEEKLY_DAY, 0 = Monday) summary of the last 7 days."""
    if dt.datetime.now(TZ).weekday() != int(os.environ.get("LINEMAKER_WEEKLY_DAY", "0") or 0):
        return
    channel = bot.get_channel(bot.alert_channel_id() or 0)
    if channel is None:
        return
    from .reports import weekly_lines
    try:
        lines = await asyncio.to_thread(weekly_lines, today())
    except Exception:
        log.exception("weekly summary failed")
        return
    await channel.send(embed=discord.Embed(title="🗓️ Weekly Summary · last 7 days",
                                           description="\n\n".join(lines)[:4000], color=0x9B59B6))


@tasks.loop(time=dt.time(9, 30, tzinfo=TZ))
async def daily_grade():
    day = (dt.datetime.now(TZ).date() - dt.timedelta(days=1)).isoformat()
    try:
        summary, remaining, tracked = await asyncio.to_thread(grade_day, day, [], True)
    except Exception:
        log.exception("daily grade failed")
        return
    log.info("daily grade %s: %d tracked markets graded", day, tracked)
    await _warm_up()
    channel_id = os.environ.get("LINEMAKER_CHANNEL_ID") or bot.home
    channel = bot.get_channel(int(channel_id)) if channel_id else None
    if channel is None or not (summary or tracked):
        return
    embed = discord.Embed(title=f"📈 Morning grade · {day}", color=BLUE_BOARD,
                          description="\n".join(summary + [f"- 📊 Results logged for **{tracked}** "
                                                           "tracked markets"])[:4000])
    if remaining is not None:
        embed.set_footer(text=f"Credits left: {remaining}")
    await channel.send(embed=embed)


@tasks.loop(time=dt.time(11, 0, tzinfo=TZ))
async def daily_post():
    date = today()
    channel = bot.get_channel(int(os.environ.get("LINEMAKER_CHANNEL_ID") or bot.home))
    if channel is None:
        log.warning("daily post skipped: channel not found")
        return
    sports = [s.strip() for s in os.environ.get("LINEMAKER_SPORTS", "").split(",") if s.strip()]
    proj_dir = os.environ.get("LINEMAKER_PROJECTIONS_DIR")
    try:
        if sports and not proj_dir:   # FantasyPros or the free ESPN model
            embeds, md = await asyncio.to_thread(build_auto, sports, date, ProConfig())
        elif sports and proj_dir:
            path = os.path.join(proj_dir, f"{date}.csv")
            if not os.path.exists(path):
                log.warning("daily post skipped: no projections at %s", path)
                return
            embeds, md, _ = await asyncio.to_thread(
                build_live, sports, read_projections(path), date, ProConfig())
        else:
            path = os.path.join(os.environ.get("LINEMAKER_SLATE_DIR", ""), f"{date}.csv")
            if not os.path.exists(path):
                log.warning("daily post skipped: no slate at %s", path)
                return
            embeds, md = await asyncio.to_thread(build_pro, path, date, ProConfig())
        await _send_card(channel.send, embeds, md, f"pro_card_{date}.md")
    except Exception:
        log.exception("daily post failed")


@daily_post.before_loop
async def _wait_ready():
    await bot.wait_until_ready()


def main():
    logging.basicConfig(level=logging.INFO)
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise SystemExit("Set DISCORD_TOKEN to your bot token.")
    bot.run(token, log_handler=None)


if __name__ == "__main__":
    main()
