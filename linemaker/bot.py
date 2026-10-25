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

from .envfile import load_all, load_env

load_all()  # /set overrides, settings.env from GitHub, then the project's .env
load_env()  # a .env in some other working directory last; real environment variables win

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


PICK_DETAILS = {}          # button key -> pick detail (saved to disk, so buttons survive restarts)
MAX_DETAILS = 2000
SAVED_DETAILS = 800        # how many of the newest are kept on disk


def _details_path() -> str:
    return os.path.join(os.environ.get("LINEMAKER_CACHE_DIR", "cache"), "pick_details.pkl")


def save_details():
    """Write the newest button details to disk (skips anything that can't be saved)."""
    import pickle
    keep = {}
    for k in list(PICK_DETAILS)[-SAVED_DETAILS:]:
        try:
            pickle.dumps(PICK_DETAILS[k])
            keep[k] = PICK_DETAILS[k]
        except Exception:
            continue
    path = _details_path()
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path + ".tmp", "wb") as f:
            pickle.dump(keep, f)
        os.replace(path + ".tmp", path)
    except OSError as e:
        log.warning("couldn't save button details: %s", e)


def load_details():
    import pickle
    try:
        with open(_details_path(), "rb") as f:
            PICK_DETAILS.update(pickle.load(f))
        log.info("buttons restored for %d saved picks", len(PICK_DETAILS))
    except (OSError, EOFError, pickle.UnpicklingError, AttributeError, ImportError) as e:
        if not isinstance(e, FileNotFoundError):
            log.warning("couldn't load button details: %s", e)


async def route_component(interaction: discord.Interaction) -> bool:
    """Answer every Linemaker button / menu press by its custom_id, so cards posted before
    a restart keep working. Returns False for components that aren't ours."""
    cid = (interaction.data or {}).get("custom_id", "")
    if not cid.startswith("lm:"):
        return False
    parts = cid.split(":")
    kind, key = parts[1], parts[2] if len(parts) > 2 else ""
    pages = {k: page for k, _label, _emoji, page in BUTTONS}
    if kind == "bet":
        handler = PickView._bet_single(key)
    elif kind == "betp":
        handler = PickView._bet_parlay(key, int(parts[3]))
    elif kind == "pick":
        values = (interaction.data or {}).get("values") or [key]
        handler = CompactView._show_pick(values[0])
    elif kind == "pplog":
        handler = _pp_log(key, int(parts[3]))
    elif kind == "mkparlay":
        handler = CompactView._parlay_picker(key)
    elif kind == "slip":   # lm:slip:key:mask:page[:boost]:action (older slips have no boost)
        handler = (_slip_tap(key, parts[3], parts[4], parts[6], parts[5]) if len(parts) >= 7
                   else _slip_tap(key, parts[3], parts[4], parts[5]))
    elif kind == "notes":
        handler = CompactView._show_notes(key)
    elif kind == "board":
        handler = _show_board(key, parts[3])
    elif kind == "targets":
        handler = _show_targets(key)
    elif kind == "mkt":
        handler = _market_rescan(key)
    elif kind == "pbet":   # lm:pbet:<key>:<i>; the dropdown sends the chosen parlay as key:i
        values = (interaction.data or {}).get("values") or [f"{key}:{parts[3] if len(parts) > 3 else 0}"]
        handler = _parlay_bet_card(values[0])
    elif kind == "sboard":
        handler = _show_board(key, parts[4], int(parts[3], 16))
    elif kind in pages:
        handler = PickView._handler(key, pages[kind])
    else:
        return False
    await handler(interaction)
    return True
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
            self.add_item(button)
        for i, p in enumerate((detail.get("parlays") or [])[:4]):
            button = discord.ui.Button(label=f"Log {len(p.legs)}-leg {p.book}", emoji="📝", row=2,
                                       style=discord.ButtonStyle.success, custom_id=f"lm:betp:{key}:{i}")
            self.add_item(button)
        if "ev" not in detail and detail:
            return   # a parlay card: bet buttons only
        for kind, label, emoji, page in BUTTONS:
            button = discord.ui.Button(label=label, emoji=emoji, style=discord.ButtonStyle.secondary,
                                       custom_id=f"lm:{kind}:{key}")
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
            leg = leg_from_eval(ev)
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
                f"{len(p.legs)}-leg {getattr(p, 'kind', '') or 'parlay'}"))
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


def _pp_log(key, i):
    async def callback(interaction: discord.Interaction):
        detail = PICK_DETAILS.get(key)
        if detail is None:
            await interaction.response.send_message(
                "This card is from before the bot restarted, so I can't log it.", ephemeral=True)
            return
        await interaction.response.send_modal(PPLogModal(detail["pp"][i]))
    return callback


class PPLogModal(discord.ui.Modal):
    """Log a PrizePicks entry: just the amount (payouts come from the entry's table)."""

    def __init__(self, entry):
        super().__init__(title=f"Log {entry.size}-pick {entry.kind}", timeout=600)
        self.entry = entry
        self.stake = discord.ui.TextInput(label="Entry amount ($)", placeholder="5", max_length=10)
        self.add_item(self.stake)

    async def on_submit(self, interaction: discord.Interaction):
        from .bets import add_bet, leg_from_eval
        from .odds import decimal_to_american
        try:
            stake = float(str(self.stake.value).replace("$", "").strip())
            if stake <= 0:
                raise ValueError
        except ValueError:
            await interaction.response.send_message("Enter an amount like 5.", ephemeral=True)
            return
        e = self.entry
        from .pp import leg_mult
        legs = [dict(leg_from_eval(ev, book="PrizePicks"), folder="pp", pp_mult=leg_mult(ev))
                for ev in e.legs]
        add_bet(legs, decimal_to_american(e.payouts[0]), stake, "PrizePicks", str(interaction.user),
                kind="pp", pp_kind=e.kind)
        await interaction.response.send_message(
            f"✅ Logged **${stake:,.2f}** {e.size}-pick {e.kind}. It settles after grading; "
            "`/record` shows your results.", ephemeral=True)


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


MAX_PARLAY_BOXES = 6   # parlays shown as boxes on a card; every one is in the Bet a parlay menu


def _clip_embed(e: discord.Embed, chars: int) -> discord.Embed:
    d = e.description or ""
    if len(d) > chars:
        e.description = d[:max(chars - 1, 0)].rsplit("\n", 1)[0] + "…"
    return e


def compact_card(messages: list) -> tuple:
    """(embeds, view) for one message: summary + board (+ parlays); every pick's full
    write-up behind a dropdown, the scoping notes behind a button."""
    header, boards, parlays, notes, picks, legs, mls = None, [], [], [], [], [], []
    books = None
    for embeds, detail in messages:
        books = (detail or {}).get("books") or books
        if detail and "moneylines" in detail:   # the header: every moneyline, for the ML parlay builder
            mls, detail = detail["moneylines"], None
        if detail and "legs" in detail:      # a volume board: its legs feed the parlay builder
            boards.extend(embeds)
            legs = detail["legs"]
            continue
        if detail and "ev" in detail:
            picks.append((embeds[0], detail))
            continue
        if detail and ("parlays" in detail or "pp" in detail):
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
    # Number every parlay in card order (#1, #2 ...): its box, the Bet a parlay menu and the
    # Bet buttons all use the same number.
    n = 0
    for e, d in parlays:
        if "parlays" in d:
            d["numbers"] = list(range(n + 1, n + 1 + len(d["parlays"])))
            n += len(d["parlays"])
            if len(d["parlays"]) == 1 and e.title and not e.title.startswith("#"):
                e.title = f"#{d['numbers'][0]} · {e.title}"[:256]
    parlay_boxes = [p[0] for p in parlays if "parlays" in p[1]]
    shown = parlay_boxes[:MAX_PARLAY_BOXES] + [p[0] for p in parlays if "parlays" not in p[1]]
    hidden = len(parlay_boxes) - min(len(parlay_boxes), MAX_PARLAY_BOXES)
    embeds = [e for e in [header] + boards + shown if e is not None][:10]
    if not picks and not parlays:        # nothing cleared: the notes are the answer
        embeds += notes[:10 - len(embeds)]
        notes = []
    if header is not None:
        over = sum(_embed_len(e) for e in embeds) - EMBED_BUDGET
        if over > 0:
            _clip_embed(header, max(len(header.description or "") - over, 300))
    boxes = {id(e) for e in parlay_boxes}
    while len(embeds) > 1 and sum(_embed_len(e) for e in embeds) > EMBED_BUDGET:
        last = embeds.pop()                  # still too long: parlays to the menu, the rest behind Notes
        if id(last) in boxes:
            hidden += 1
        else:
            notes.insert(0, last)
    if hidden and header is not None:
        header.description = (header.description or "") + (
            f"\n\n🧾 **{hidden} more parlay{'s' if hidden != 1 else ''}** in the **Bet a parlay** menu below.")
        over = sum(_embed_len(e) for e in embeds) - EMBED_BUDGET
        if over > 0:
            _clip_embed(header, max(len(header.description or "") - over, 300))
    view = CompactView(picks, parlays, notes, legs, mls, books)
    return embeds, view


def moneyline_button(moneylines: list, row: int = 4, books: Optional[tuple] = None) -> discord.ui.Button:
    key = _remember({"picks": moneylines, "books": books})
    return discord.ui.Button(label="Parlay moneylines", emoji="💰", row=row,
                             style=discord.ButtonStyle.primary, custom_id=f"lm:mkparlay:{key}")


def _leg_detail(d: dict, books) -> dict:
    """A volume leg as a pick detail (write-up embed, Bet links, I bet this)."""
    from .betlinks import pick_links
    from .parlay import leg_price
    from .odds import american_to_decimal
    ev = d["ev"]
    s = ev.best
    books = tuple(books or PARLAY_BOOKS)
    prices = [(b, leg_price(ev, b)) for b in books]
    lines = [" · ".join(f"{b} {fmt_odds(o)}" for b, o in prices if o) or f"{s.best_book} {fmt_odds(s.best_odds)}",
             f"Hit chance **{s.blended:.0%}** · EV {s.blended * american_to_decimal(s.best_odds) - 1:+.0%} at the best price"]
    prof = d.get("profile")
    if prof is not None and getattr(prof, "season", None):
        lines.append(f"Season hit rate at this line: {prof.season.rate:.0%}")
    if ev.market.notes:
        lines.append(ev.market.notes[:300])
    embed = discord.Embed(title=f"{d['rank']}. {s.label}"[:256], description="\n".join(lines), color=0xF1C40F)
    return dict(d, embed=embed.to_dict(), links=pick_links(ev, books), books=books)


class CompactView(discord.ui.View):
    """One scan, one message: a dropdown for each pick's write-up and buttons, parlay
    buttons, and a Notes button. Details answer privately (only you see them)."""

    def __init__(self, picks: list, parlays: list, notes: list, legs: Optional[list] = None,
                 moneylines: Optional[list] = None, books: Optional[tuple] = None):
        super().__init__(timeout=None)
        from .staking import _money, stake_tier
        if picks:
            options = []
            for embed, detail in picks[:25]:
                ev = detail["ev"]
                s = ev.best
                key = _remember(dict(detail, embed=embed.to_dict()))
                stake = stake_tier(s.blended, s.best_odds)
                desc = f"{fmt_odds(s.best_odds)} {s.best_book} · {s.blended:.0%} hit" + (
                    f" · {_money(stake)}" if stake else "")
                options.append(discord.SelectOption(label=f"{detail['rank']}. {s.label}"[:100],
                                                    description=desc[:100], value=key))
            pick = discord.ui.Select(placeholder="🔎 See a pick: write-up, Bet buttons, I bet this",
                                     options=options, row=0, custom_id=f"lm:pick:{options[0].value}")
            self.add_item(pick)
        elif legs:   # a volume board: every leg can be bet straight, parlay or not
            options = [discord.SelectOption(label=f"{d['rank']}. {d['ev'].best.label}"[:100],
                                            description=f"{fmt_odds(d['ev'].best.best_odds)} {d['ev'].best.best_book} · "
                                                        f"{d['ev'].best.blended:.0%} hit"[:100],
                                            value=_remember(_leg_detail(d, books)))
                       for d in list(legs)[:25]]
            self.add_item(discord.ui.Select(placeholder="🧾 Bet a leg: Bet buttons, I bet this",
                                            options=options, row=0, custom_id=f"lm:pick:{options[0].value}"))
        # Fixed rows, the same on every card:
        #   0  🔎 see a pick / 🧾 bet a leg          (dropdown)
        #   1  🧾 bet a parlay #1, #2 ...            (dropdown: Bet and Log for any parlay)
        #   2  Bet #1 · Bet #2 ...                   (one-tap slips; PrizePicks entries)
        #   3  Odds · Hit Rates · Line Moves · Key Stats   (PrizePicks: Log buttons)
        #   4  Safest by payout · Build a parlay · Parlay moneylines · Market edges · Notes
        auto = [(_remember(d), d) for _e, d in parlays if "parlays" in d]
        if auto:
            from .betlinks import parlay_link
            from .parlay_format import short_label
            options = []
            for key, detail in auto:
                nums = detail.get("numbers") or []
                for i, p in enumerate(detail.get("parlays") or []):
                    kind = getattr(p, "kind", "") or ("Value" if p.style == "value" else "Safest")
                    leg_text = ", ".join(short_label(ev.best.label) for ev, _o in p.legs)
                    num = f"#{nums[i]} · " if i < len(nums) else ""
                    options.append(discord.SelectOption(
                        label=f"{num}{p.book} · {len(p.legs)}-leg {kind} · {fmt_odds(p.american)}"[:100],
                        description=f"{p.prob:.0%} hit · {leg_text}"[:100], value=f"{key}:{i}",
                        emoji="🟦" if p.book == "FanDuel" else "🟩"))
            if options:
                self.add_item(discord.ui.Select(placeholder="🧾 Bet a parlay: pick #1, #2 ... for its Bet & Log buttons",
                                                options=options[:25], row=1,
                                                custom_id=f"lm:pbet:{options[0].value}"))
            bets = 0
            for key, detail in auto:
                nums = detail.get("numbers") or []
                for i, p in enumerate(detail.get("parlays") or []):
                    url = parlay_link(p)
                    if bets < 5 and url and len(url) <= 512:
                        tag = f"#{nums[i]} " if i < len(nums) else ""
                        self.add_item(discord.ui.Button(
                            label=f"Bet {tag}{'FD' if p.book == 'FanDuel' else 'DK'} {fmt_odds(p.american)}"[:80],
                            url=url, row=2, emoji="🟦" if p.book == "FanDuel" else "🟩"))
                        bets += 1
        pp_used = False
        for _embed, detail in [p for p in parlays if "pp" in p[1]]:
            from .pp import board_link, entry_link
            key = _remember(detail)
            links = [(e, entry_link(e)) for e in detail["pp"]]
            if not any(u for _e, u in links):
                self.add_item(discord.ui.Button(label="Open PrizePicks", url=board_link(), row=2, emoji="🟣"))
            on_row2 = sum(1 for c in self.children if c.row == 2)
            for i, (e, url) in enumerate(links[:5]):
                if url and len(url) <= 512 and on_row2 < 5:
                    self.add_item(discord.ui.Button(label=f"{e.size}-pick {e.kind}", url=url, row=2, emoji="🟣"))
                    on_row2 += 1
                if sum(1 for c in self.children if c.row == 3) < 5:
                    self.add_item(discord.ui.Button(label=f"Log {e.size}-pick {e.kind}", emoji="📝", row=3,
                                                    style=discord.ButtonStyle.success,
                                                    custom_id=f"lm:pplog:{key}:{i}"))
                    pp_used = True
        views = [d for _e, d in picks[:25]] or list(legs or [])[:25]
        if views and not pp_used:   # whole-card views of every pick / leg
            from .scan_format import BOARDS
            bkey = _remember({"picks": views})
            for kind, (label, emoji, _fn) in BOARDS.items():
                self.add_item(discord.ui.Button(label=label, emoji=emoji, row=3,
                                                style=discord.ButtonStyle.secondary,
                                                custom_id=f"lm:board:{bkey}:{kind}"))
        pool = [d for _e, d in picks[:25]] or list(legs or [])[:25]
        if len(pool) >= 2:
            pkey = _remember({"picks": pool, "books": books})
            self.add_item(discord.ui.Button(label="Safest by payout", emoji="🎯", row=4,
                                            style=discord.ButtonStyle.primary,
                                            custom_id=f"lm:targets:{pkey}"))
            b = discord.ui.Button(label="Turn into parlay" if picks else "Build my own parlay",
                                  emoji="🎰", row=4, style=discord.ButtonStyle.primary,
                                  custom_id=f"lm:mkparlay:{pkey}")
            self.add_item(b)
        if moneylines and len(moneylines) >= 2:
            self.add_item(moneyline_button(moneylines, books=books))
        if notes:
            nkey = _remember({"notes": [n.to_dict() for n in notes]})
            b = discord.ui.Button(label="Notes & passes", emoji="ℹ️", row=4,
                                  style=discord.ButtonStyle.secondary, custom_id=f"lm:notes:{nkey}")
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
            embed, view = slip_message(key)
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        return callback

    @staticmethod
    def _show_pick(key):
        async def callback(interaction: discord.Interaction):
            detail = PICK_DETAILS.get(key)
            if detail is None:
                await interaction.response.send_message(
                    "This card is from before the bot restarted. Ask again for fresh numbers.",
                    ephemeral=True)
                return
            embed = detail["embed"]
            embed = discord.Embed.from_dict(embed) if isinstance(embed, dict) else embed
            await interaction.response.send_message(embed=embed, view=PickView(key, detail),
                                                    ephemeral=True)
        return callback

    @staticmethod
    def _show_notes(key):
        async def callback(interaction: discord.Interaction):
            detail = PICK_DETAILS.get(key)
            if detail is None:
                await interaction.response.send_message("These notes expired with a restart.",
                                                        ephemeral=True)
                return
            notes = [discord.Embed.from_dict(n) if isinstance(n, dict) else n for n in detail["notes"]]
            await interaction.response.send_message(embeds=notes[:10], ephemeral=True)
        return callback


PARLAY_BOOKS = ("FanDuel", "DraftKings")


def parlay_card(legs: list, books: Optional[tuple] = None, boost: float = 0.0) -> tuple:
    """(embed, view) for a parlay you built: price, payout and hit chance at each book,
    with Bet buttons that load every leg and Log buttons. boost: a profit boost (0.25 = +25%)."""
    from .betlinks import parlay_link
    from .odds import fmt_odds as _fo
    from .parlay import custom_parlays, leg_price, sgp_check
    from .parlay_format import header, money_line, short_label
    from .parlay_math import hit_chance, leg_value
    books = tuple(books or PARLAY_BOOKS)
    short = {"FanDuel": "FD", "DraftKings": "DK"}
    lines = []
    for ev in legs:
        quotes = [(b, leg_price(ev, b)) for b in books]
        prices = " · ".join(f"{short.get(b, b)} {_fo(o) if o else '—'}" for b, o in quotes)
        best = max((leg_value(ev, o) for _b, o in quotes if o), default=None)
        val = "" if best is None else (f" · ✅ {best - 1:+.0%} EV" if best > 1 else f" · {best - 1:+.0%} EV")
        lines.append(f"• **{short_label(ev.best.label)}**\n  {prices} · {hit_chance(ev):.0%} hit{val}")
    embed = discord.Embed(title=f"🎰 Your {len(legs)}-Leg Parlay", color=0xF1C40F,
                          description="\n".join(lines)[:3500])
    built = custom_parlays(legs, books, boost)
    view = discord.ui.View(timeout=None)
    for p, missing in built:
        from .discord_format import _style
        name, _tag, emoji, _c = _style(p.book)
        value = money_line(p)
        if abs(p.prob - p.indep_prob) >= 0.01:
            value += f"\n↔️ {p.indep_prob:.0%} if the legs were independent"
        if missing:
            value += f"\n⚠️ Not offered here: {', '.join(missing)[:200]}"
        sgp = sgp_check([ev for ev, _o in p.legs], p.book)
        if sgp["blocked"]:
            value += (f"\n⛔ **{p.book} won't take this as one ticket:** "
                      + "; ".join(sgp["blocked"])[:300] + ". Drop one of those legs.")
        elif sgp["kind"]:
            games = ", ".join(f"{g} ({n} legs)" for g, n in sgp["groups"])
            value += (f"\n🔗 **Works as a {p.book} {sgp['kind']}**: {games}"[:400]
                      + ". The book prices same-game legs together, so expect a lower price than shown.")
        url = parlay_link(p)
        if url and len(url) <= 512:
            view.add_item(discord.ui.Button(label=f"Bet on {p.book}", url=url,
                                            emoji="🟦" if p.book == "FanDuel" else "🟩"))
        elif url:   # too long for a button (Discord caps those at 512): a link in the text
            value += f"\n🧾 [Open this {len(p.legs)}-leg slip on {p.book}]({url})"
        else:       # a leg came without the book's slip ids: open the book, add the legs by hand
            from .betlinks import book_home
            live = any(getattr(ev, "phase", "pre") == "live" for ev, _o in p.legs)
            view.add_item(discord.ui.Button(label=f"Open {p.book}" + (" live" if live else ""),
                                            url=book_home(p.book, live),
                                            emoji="🟦" if p.book == "FanDuel" else "🟩"))
            value += (f"\n🧾 {p.book} didn't send slip ids for every leg, so there's no one-tap slip: "
                      "open the book and add the legs above.")
        embed.add_field(name=header(p), value=value[:1024], inline=False)
        key = _remember({"parlays": [p]})
        b = discord.ui.Button(label=f"Log {p.book} parlay", emoji="📝", style=discord.ButtonStyle.success,
                              custom_id=f"lm:betp:{key}:0")
        view.add_item(b)
    if not built:
        embed.add_field(name="Can't price this one",
                        value=("Neither FanDuel nor DraftKings" if len(books) > 1 else books[0])
                              + " offers at least two of these legs.",
                        inline=False)
    notes = ["✅ = leg is +EV at its best price. Hit chance counts same-game legs moving "
             "together. Books' parlay prices can differ slightly from the multiplied odds."]
    if any(sgp_check(legs, b)["groups"] for b in books):
        notes.insert(0, "Same-game legs: the book prices them as an SGP, usually lower than the "
                        "multiplied odds shown; check its price on the slip before you bet.")
    embed.set_footer(text=" ".join(n.replace("**", "") for n in notes)[:2048])
    return embed, view


def target_parlays(picks: list, books) -> dict:
    """{(target, book): Parlay or None}: the search (slow for one game: run it in a thread).
    Per book, the payouts are built smallest first and each one steers clear of the legs the
    smaller ones already use (see parlay._search), so placing the whole ladder doesn't ride
    on one chalk leg."""
    from .parlay import TARGETS, best_for_target, exposure
    legs = [d["ev"] for d in picks]
    out = {}
    for b in tuple(books or PARLAY_BOOKS):
        used = {}
        for t in TARGETS:
            p = out[(t, b)] = best_for_target(legs, b, t, used=used)
            if p is None:   # not enough separate legs left: the best one anyway, flagged on the card
                p = out[(t, b)] = best_for_target(legs, b, t)
                if p is not None:
                    p.reuses = True
            for ev, _o in (p.legs if p else []):
                used[exposure(ev)] = used.get(exposure(ev), 0) + 1
    return out


def targets_card(picks: list, books, found: Optional[dict] = None) -> tuple:
    """(embeds, view): an intro, then one box per payout target with each book's safest parlay.
    A parlay that's already the answer for a smaller payout isn't repeated: that box says
    which payouts it covers."""
    from .betlinks import parlay_link
    from .parlay import TARGETS
    from .parlay_format import block
    books = tuple(books or PARLAY_BOOKS)
    found = found if found is not None else target_parlays(picks, books)
    from .parlay import _ladder_limits, exposure
    max_shared, max_uses, _floor = _ladder_limits()
    intro = discord.Embed(title="🎯 Safest Parlay for Each Payout", color=0x1ABC9C, description=(
        "Highest real hit chance for each payout · one leg per game (or per player for one game) · "
        "every leg priced fair or better. Fewer, better-priced legs beat stacking favorites.\n"
        f"🧩 **Spread out:** each payout uses different legs where it can (at most {max_shared} "
        f"shared, and no player or game in more than {max_uses} of them), so if you place the "
        "whole ladder, one bad leg can't sink every ticket."))
    first_seen = {}   # (book, exposure) -> the payout that used it first
    view = discord.ui.View(timeout=None)
    shown, boxes = {}, []    # signature -> box index; boxes: [target, [(book, parlay)], [covers]]
    for target in TARGETS:
        here = []
        for book in books:
            p = found.get((target, book))
            if p is None:
                continue
            sig = (book, tuple(sorted(f"{ev.market.key}:{ev.best.side}" for ev, _o in p.legs)))
            if sig in shown:
                boxes[shown[sig]][2].append(target)
                continue
            shown[sig] = len(boxes)
            here.append((book, p))
        if here:
            boxes.append([target, here, []])
        elif not any(target in b[2] for b in boxes):
            boxes.append([target, [], []])
    colors = (0x1ABC9C, 0x3498DB, 0x9B59B6, 0xE67E22)
    embeds = [intro]
    for n, (target, parlays, covers) in enumerate(boxes):
        label = " · ".join(f"+{(t - 1) * 100:.0f}" for t in [target] + covers)
        e = discord.Embed(title=f"Payout {label}", color=colors[n % len(colors)],
                          description=f"Break-even hit chance {1 / target:.0%}" + (
                              f" (this parlay pays enough for {label})" if covers else ""))
        if not parlays:
            e.description += "\nNo combination of fair-priced legs reaches this payout."
        for book, p in parlays:
            roll = f"🔁 Roll it over: 2x {p.prob ** 2:.0%} · 3x {p.prob ** 3:.0%}"
            shared = []
            for ev, _o in p.legs:
                was = first_seen.setdefault((book, exposure(ev)), target)
                if was != target:
                    shared.append(f"{ev.best.label} (also in +{(was - 1) * 100:.0f})")
            extra = [roll] + ([f"🔗 Shares: {', '.join(shared)}"] if shared else [])
            if getattr(p, "reuses", False):
                extra.append("⚠️ Not enough separate legs left for this payout, so it reuses legs "
                             "from the smaller ones.")
            name, value = block(p, extra)
            e.add_field(name=name, value=value, inline=False)
            k = _remember({"parlays": [p]})
            if len(view.children) < 24:
                url = parlay_link(p)
                tag = f"{p.american:+d} {book}"
                if url and len(url) <= 512:
                    view.add_item(discord.ui.Button(label=f"Bet {tag}", url=url,
                                                    emoji="🟦" if book == "FanDuel" else "🟩"))
                view.add_item(discord.ui.Button(label=f"Log {tag}", emoji="📝",
                                                style=discord.ButtonStyle.success, custom_id=f"lm:betp:{k}:0"))
        embeds.append(e)
    embeds[-1].set_footer(text="Hit chances are calibrated to past results and count legs from one "
                               "game moving together. Rolling over: every step has to hit.")
    return embeds[:10], view


def _parlay_bet_card(value: str):
    """One auto-parlay from a card, privately: its price at the book, a one-tap Bet button
    (the slip opens with every leg) and Log."""
    async def callback(interaction: discord.Interaction):
        key, _, i = value.rpartition(":")
        detail = PICK_DETAILS.get(key)
        if detail is None or not detail.get("parlays"):
            await interaction.response.send_message("This card is too old. Ask again for fresh parlays.",
                                                    ephemeral=True)
            return
        try:
            p = detail["parlays"][int(i)]
            embed, view = parlay_card([ev for ev, _o in p.legs], (p.book,))
            kind = getattr(p, "kind", "") or ("Value" if p.style == "value" else "Safest")
            embed.title = f"🎰 {len(p.legs)}-Leg {kind} Parlay · {p.book}"
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        except Exception as e:
            await _report_error(interaction, e)
    return callback


def _market_rescan(key: str):
    """⚡ Market edges: the same scan (league, team, book, live or not) in market-edge mode,
    on the odds already bought (no extra credits inside the reuse window)."""
    async def callback(interaction: discord.Interaction):
        detail = PICK_DETAILS.get(key)
        if detail is None or "market_scans" not in detail:
            await interaction.response.send_message("This card is too old. Ask again for fresh picks.",
                                                    ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        try:
            from .chat import LinemakerChat
            helper = bot.chat or _TOOL_HELPER.setdefault("chat", LinemakerChat.__new__(LinemakerChat))
            if not hasattr(helper, "_feed_cache"):
                helper._feed_cache = {}
            ctx = {"files": [], "attached_rows": []}
            for args in detail["market_scans"]:
                await asyncio.to_thread(LinemakerChat.run_tool, helper, "find_plays",
                                        dict(args, mode="market", teams=args.get("teams") or None), ctx)
            results = ctx.get("results") or []
            if not results:
                await interaction.followup.send("No market edges found on this slate right now.")
                return
            from .pro import market_config
            messages = scan_messages(results, today(), market_config())
            embeds, view = compact_card(messages)
            await interaction.followup.send(embeds=embeds, view=view)
            await asyncio.to_thread(save_details)
        except Exception as e:
            await _report_error(interaction, e)
    return callback


def _show_targets(key: str):
    async def callback(interaction: discord.Interaction):
        detail = PICK_DETAILS.get(key)
        if detail is None:
            await interaction.response.send_message("This card is too old. Ask again for fresh picks.",
                                                    ephemeral=True)
            return
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)   # the search can take a few seconds
            found = await asyncio.to_thread(target_parlays, detail["picks"], detail.get("books"))
            embeds, view = targets_card(detail["picks"], detail.get("books"), found)
            await interaction.followup.send(embeds=embeds, view=view, ephemeral=True)
            await asyncio.to_thread(save_details)
        except Exception as e:
            await _report_error(interaction, e)
    return callback


def _show_board(key: str, kind: str, mask: Optional[int] = None):
    """A whole-card view (every pick), or with mask, just the legs chosen on a live slip."""
    async def callback(interaction: discord.Interaction):
        from .scan_format import BOARDS
        detail = PICK_DETAILS.get(key)
        if detail is None or kind not in BOARDS:
            await interaction.response.send_message(
                "This card is too old for that view. Ask again for fresh picks.", ephemeral=True)
            return
        picks = detail["picks"]
        if mask is not None:
            picks = [d for i, d in enumerate(picks) if mask >> i & 1]
        try:
            embed = BOARDS[kind][2](picks)
            if mask is not None:
                embed.title = embed.title.replace("Every Pick", f"Your {len(picks)}-Leg Slip")
            await interaction.response.send_message(embed=embed, ephemeral=True)
        except Exception as e:
            await _report_error(interaction, e)
    return callback


SLIP_PAGE = 10   # leg buttons per page (2 rows); then the views, the controls, Bet / Log


BOOSTS = (0.0, 0.10, 0.20, 0.25, 0.30, 0.50, 1.00)   # profit boosts the 🚀 button cycles through


def best_next_leg(picks: list, chosen: list, books) -> Optional[int]:
    """The leg that makes the slip worth the most (correlated hit chance x payout) at the
    book that prices it best, among legs the book will take on one ticket."""
    from .parlay import leg_price, price, sgp_check
    from .parlay_math import leg_value
    best, best_val = None, None
    for i, d in enumerate(picks):
        if i in chosen:
            continue
        legs = [picks[j]["ev"] for j in chosen] + [d["ev"]]
        for book in (books or PARLAY_BOOKS):
            quotes = [(ev, leg_price(ev, book)) for ev in legs]
            if any(o is None for _e, o in quotes):
                continue
            if len(legs) == 1:
                val = leg_value(*quotes[0])
            else:
                if sgp_check(legs, book)["blocked"]:
                    continue
                p = price(book, quotes)
                val = p.prob * p.decimal
            if best_val is None or val > best_val:
                best, best_val = i, val
    return best


def slip_message(key: str, mask: int = 0, page: int = 0, boost_i: int = 0) -> tuple:
    """(embed, view) for the live slip: one button per leg, and every tap re-prices it
    (hit chance, EV, payout, odds at each book). State rides in the buttons' custom_ids
    (pool key, chosen legs as a bitmask, page), so the slip never expires and survives
    restarts."""
    from .parlay import leg_price
    from .parlay_math import hit_chance
    detail = PICK_DETAILS[key]
    picks, books = detail["picks"], detail.get("books")
    chosen = [i for i in range(len(picks)) if mask >> i & 1]
    boost_i = boost_i % len(BOOSTS)
    if len(chosen) >= 2:
        embed, slip = parlay_card([picks[i]["ev"] for i in chosen], books, BOOSTS[boost_i])
        embed.title += " · live slip"
    else:
        slip = None
        if chosen:
            ev = picks[chosen[0]]["ev"]
            prices = " · ".join(f"{b} {fmt_odds(o)}" for b in (books or PARLAY_BOOKS)
                                for o in [leg_price(ev, b)] if o)
            desc = (f"• **{ev.best.label}** · {prices} · {ev.best.blended:.0%} hit\n\n"
                    "Tap one more leg to see the parlay price, EV and payout.")
        else:
            desc = "Tap legs below to add them (tap again to remove). The slip re-prices on every tap."
        embed = discord.Embed(title="🎰 Your Parlay · live slip", color=0xF1C40F, description=desc)
    pages = max(1, -(-len(picks) // SLIP_PAGE))
    page = min(max(page, 0), pages - 1)
    view = discord.ui.View(timeout=None)
    base = f"lm:slip:{key}:{mask:x}:{page}:{boost_i}"
    short = {"FanDuel": "FD", "DraftKings": "DK"}
    for n, i in enumerate(range(page * SLIP_PAGE, min(len(picks), (page + 1) * SLIP_PAGE))):
        ev, on = picks[i]["ev"], mask >> i & 1
        price = next((f"{short.get(b, b)} {fmt_odds(o)}" for b in (books or PARLAY_BOOKS)
                      for o in [leg_price(ev, b)] if o), fmt_odds(ev.best.best_odds))
        view.add_item(discord.ui.Button(
            label=(f"{'✅ ' if on else ''}{picks[i]['rank']}. {ev.best.label}"[:62]
                   + f" · {price} · {hit_chance(ev):.0%}")[:80],
            style=discord.ButtonStyle.success if on else discord.ButtonStyle.secondary,
            row=n // 5, custom_id=f"{base}:t{i}"))
    if chosen:   # whole-slip views: odds, hit rates, line moves, key stats for the chosen legs
        from .scan_format import BOARDS
        for kind, (label, emoji, _fn) in BOARDS.items():
            view.add_item(discord.ui.Button(label=label, emoji=emoji, row=2,
                                            custom_id=f"lm:sboard:{key}:{mask:x}:{kind}"))
    if len(chosen) < len(picks):
        view.add_item(discord.ui.Button(label="Best next leg", emoji="➕", row=2,
                                        style=discord.ButtonStyle.primary, custom_id=f"{base}:s"))
    if pages > 1:
        view.add_item(discord.ui.Button(label="Prev", emoji="◀️", row=3, disabled=page == 0,
                                        custom_id=f"{base}:p{page - 1}"))
        view.add_item(discord.ui.Button(label=f"Next ({page + 1}/{pages})", emoji="▶️", row=3,
                                        disabled=page == pages - 1, custom_id=f"{base}:p{page + 1}"))
    view.add_item(discord.ui.Button(label=f"Use all {len(picks)}", emoji="🎯", row=3,
                                    style=discord.ButtonStyle.primary, custom_id=f"{base}:all"))
    if mask:
        view.add_item(discord.ui.Button(label="Clear", emoji="🧹", row=3, custom_id=f"{base}:clr"))
    if len(chosen) >= 2:
        nxt = BOOSTS[(boost_i + 1) % len(BOOSTS)]
        view.add_item(discord.ui.Button(
            label=(f"Boost +{BOOSTS[boost_i]:.0%} (tap: " + (f"+{nxt:.0%})" if nxt else "off)")) if boost_i
            else "Add a profit boost", emoji="🚀", row=3, custom_id=f"{base}:b"))
    for item in (slip.children if slip else []):     # Bet / Log for the current slip
        item.row = 4
        view.add_item(item)
    return embed, view


def _slip_tap(key: str, mask: str, page: str, action: str, boost: str = "0"):
    async def callback(interaction: discord.Interaction):
        if key not in PICK_DETAILS:
            await interaction.response.send_message(
                "This slip's picks are gone (the card is too old). Ask again for fresh picks.",
                ephemeral=True)
            return
        PICK_DETAILS[key] = PICK_DETAILS.pop(key)    # in use: keep it from being pruned
        m, pg, n, bi = int(mask, 16), int(page), len(PICK_DETAILS[key]["picks"]), int(boost or 0)
        if action == "b":
            bi = (bi + 1) % len(BOOSTS)
        elif action == "s":
            d = PICK_DETAILS[key]
            nxt = await asyncio.to_thread(best_next_leg, d["picks"], [i for i in range(n) if m >> i & 1],
                                          d.get("books"))
            if nxt is not None:
                m |= 1 << nxt
                pg = nxt // SLIP_PAGE
        elif action.startswith("t"):
            m ^= 1 << int(action[1:])
        elif action.startswith("p"):
            pg = int(action[1:])
        elif action == "all":
            m = (1 << n) - 1
        elif action == "clr":
            m = 0
        embed, view = slip_message(key, m, pg, bi)   # builds a View: needs the event loop
        await interaction.response.edit_message(content=None, embed=embed, view=view)
        await asyncio.to_thread(save_details)
    return callback


async def _send_card(send, messages: list, markdown=None, filename=None, text: str = "",
                     market_key: Optional[str] = None):
    """Post a value scan. Compact (default): one message with a pick dropdown. Full: header
    and board, then one message per pick with its buttons, then the notes. Plain embed
    lists are posted as-is."""
    if messages and not isinstance(messages[0], tuple):
        messages = [(messages, None)]
    if card_style() == "compact" and any(d is not None for _e, d in messages):
        embeds, view = compact_card(messages)
        if market_key and sum(1 for c in view.children if c.row == 4) < 5:
            view.add_item(discord.ui.Button(label="Market edges", emoji="⚡", row=4,
                                            style=discord.ButtonStyle.secondary,
                                            custom_id=f"lm:mkt:{market_key}"))
        await send(content=text[:2000] or None, embeds=embeds, view=view)
        await asyncio.to_thread(save_details)
        return
    if text:
        await send(content=text[:2000])
    for embeds, detail in messages:
        if detail and "moneylines" in detail:
            view = discord.ui.View(timeout=None)
            view.add_item(moneyline_button(detail["moneylines"], row=0, books=detail.get("books")))
            for i, group in enumerate(chunk_embeds(embeds)):
                await send(embeds=group, **({"view": view} if i == 0 else {}))
            continue
        if detail is not None:
            await send(embeds=embeds, view=PickView(_remember(detail), detail))
            continue
        for group in chunk_embeds(embeds):
            await send(embeds=group)
    await asyncio.to_thread(save_details)


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
                app_commands.Choice(name="Live in-game lines", value="live"),
                app_commands.Choice(name="PrizePicks", value="pp")]


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
        if kind == "pp":
            from .pp import pp_config
            from .tracking import Tracker
            cfg, freezer = pp_config(), None
            tracker = Tracker(os.path.join(TRACKING, "pp"), date)
        res = run_pro(rows_to_markets(rows), date, cfg, freezer, tracker)
        if kind == "pp":
            from .pp import select_legs
            res.picks = select_legs(res.picks, cfg.pp_floor)
        res.slate, res.source_warning, res.projection_source = slate, warning, source
        res.kind = kind
        from .chat import slate_age_minutes
        res.odds_age = slate_age_minutes(sport, date, kind)
        sections.append((sport, res, enrich(res, sport, date)))
    if kind == "pp":
        from .scan_format import pp_messages
        return pp_messages(sections, date), None
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
        await asyncio.to_thread(load_details)   # buttons on earlier cards keep working
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
        if self.alert_channel_id() and not health_loop.is_running():
            health_loop.start()
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

    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type == discord.InteractionType.component:
            try:
                await route_component(interaction)
            except Exception as e:
                log.exception("button failed")
                try:
                    await _report_error(interaction, e)
                except Exception:
                    pass

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
        _spend_as("chat asks")
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
            if all(getattr(r, "kind", "") == "pp" for _s, r, _ex in turn.results):
                from .scan_format import pp_messages
                cards = pp_messages(turn.results, today())
            elif all((ex or {}).get("mode") == "volume" for _s, _r, ex in turn.results):
                from .pro import volume_config
                from .scan_format import volume_messages
                vcfg = volume_config()
                if all(getattr(r, "ml_only", False) for _s, r, _e in turn.results):
                    from .pro import ml_parlay_config
                    vcfg = ml_parlay_config(turn.results[0][1].game_types)
                if all(getattr(r, "kind", "") == "live" for _s, r, _e in turn.results):
                    from .live import live_parlay_gates
                    live_parlay_gates(vcfg)
                cards = volume_messages(turn.results, today(), vcfg)
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
            scans = [r.scan_args for _s, r, _ex in turn.results
                     if getattr(r, "scan_args", None) and r.scan_args["mode"] != "market"
                     and r.scan_args["kind"] != "pp"]
            mkey = _remember({"market_scans": scans}) if scans else None
            await _send_card(send_cards, cards, text=chunks[0] if inline and chunks else "", market_key=mkey)


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
        from .grade_report import day_records, grade_report, graded_keys
        before = graded_keys(day_records(date))
        summary, remaining, tracked = await asyncio.to_thread(grade_day, date, rows, auto)
        embeds, table = await asyncio.to_thread(grade_report, date, summary, before, remaining)
        if not summary and not tracked and table is None:
            await interaction.followup.send(f"Nothing graded for {date} yet: games may not be "
                                            "final, or nothing was tracked that day.")
            return
        groups = chunk_embeds(embeds)
        for i, group in enumerate(groups):
            last = i == len(groups) - 1
            files = ([discord.File(io.BytesIO(table.encode()), filename=f"graded_{date}.csv")]
                     if last and table else [])
            await interaction.followup.send(embeds=group, files=files)
    except Exception as e:
        await _report_error(interaction, e)


@bot.tree.command(name="markets", description="Owner: which bet types FanDuel/DraftKings/PrizePicks have posted (about 1 credit per event)")
@app_commands.describe(sport="League (default UFC)", events="How many upcoming events to check (default 2)")
async def markets_cmd(interaction: discord.Interaction, sport: Optional[str] = "UFC", events: Optional[int] = 2):
    if not await _owner_only(interaction):
        return
    await interaction.response.defer(thinking=True, ephemeral=True)
    try:
        api = OddsAPI()
        books = "fanduel,draftkings,prizepicks,underdog"

        def run():
            now = dt.datetime.now(dt.timezone.utc)
            upcoming = [e for e in api.events(sport.upper())
                        if dt.datetime.fromisoformat(e["commence_time"].replace("Z", "+00:00")) > now]
            out = []
            for e in upcoming[:max(1, min(events or 2, 5))]:
                data = api.event_markets(sport.upper(), e["id"], books)
                per_book = {bk.get("title") or bk["key"]: sorted(m["key"] for m in bk.get("markets", []))
                            for bk in data.get("bookmakers", [])}
                out.append((f"{e['away_team']} vs {e['home_team']}", per_book))
            return out, api.quota.remaining
        found, left = await asyncio.to_thread(run)
        if not found:
            await interaction.followup.send(f"No upcoming {sport.upper()} events in the feed.", ephemeral=True)
            return
        lines = []
        for name, per_book in found:
            lines.append(f"**{name}**")
            lines += [f"• {b}: `{', '.join(keys) or 'nothing yet'}`" for b, keys in per_book.items()] or \
                     ["• no FanDuel / DraftKings / PrizePicks / Underdog markets posted yet"]
        lines.append(f"\nCredits left: {left}")
        await interaction.followup.send("\n".join(lines)[:1990], ephemeral=True)
    except Exception as e:
        await _report_error(interaction, e)


@bot.tree.command(name="weekly", description="The last 7 days: record, closing-line value, what's working, weight changes")
async def weekly_cmd(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    try:
        from .reports import weekly_lines
        lines = await asyncio.to_thread(weekly_lines, today())
        await interaction.followup.send(embed=discord.Embed(
            title="🗓️ Weekly Summary · last 7 days", description="\n\n".join(lines)[:4000], color=0x9B59B6))
    except Exception as e:
        await _report_error(interaction, e)


@bot.tree.command(name="ufc", description="UFC fighter projections: win chance, significant strikes, takedowns, fight time")
async def ufc_cmd(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    try:
        from .ufc_model import next_card_date, props_table
        date = await asyncio.to_thread(next_card_date, today())
        lines, rows = await asyncio.to_thread(props_table, date)
        if not rows:
            await interaction.followup.send("No upcoming UFC fights on ESPN's schedule right now.")
            return
        early = {}
        for f in rows:
            early.setdefault(tuple(sorted((f["fighter"], f["opponent"]))), (f["early"], f["rounds"]))
        embed = discord.Embed(title=f"🥊 UFC Fighter Projections · {date}", color=0xE74C3C,
                              description="```\n" + "\n".join(lines)[:3900] + "\n```")
        embed.add_field(name="Ends early", inline=False, value="\n".join(
            f"{a} vs {b}: {p:.0%} ({r} rds)" for (a, b), (p, r) in list(early.items())[:15])[:1024])
        embed.set_footer(text="SigStr = significant strikes landed · TD = takedowns · Mins = expected fight "
                              "minutes. From ESPN's UFC stats; compare with PrizePicks lines. The bot prices "
                              "them automatically once a fighter-props odds feed is connected.")
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
            if len(s.get("by_sport", {})) > 1:
                lines.append("**By league:**")
                for sport, k in sorted(s["by_sport"].items(), key=lambda kv: -kv[1]["profit"]):
                    kroi = k["profit"] / k["staked"] if k["staked"] else 0
                    lines.append(f"• {sport}: {k['won']}/{k['n']} won · {fmt_money(k['profit'])} ({kroi:+.1%})")
            lines.append("")
            lines.append("**Recent:**")
            for b in sorted(s["settled"], key=lambda b: b.get("settled", ""), reverse=True)[:8]:
                mark = {"won": "✅", "lost": "❌", "push": "➖"}[b["status"]]
                what = b["legs"][0]["label"] if b["kind"] == "single" else f"{len(b['legs'])}-leg parlay"
                lines.append(f"{mark} {what} {fmt_odds(b['odds'])} · ${b['stake']:,.0f} → {fmt_money(b.get('profit', 0))}")
        else:
            lines.append("No settled bets yet. Tap **📝 I bet this** on a pick (or **Log parlay**) "
                         "after you place it, and it settles automatically after grading.")
        from .bets import clv_summary
        c = await asyncio.to_thread(clv_summary, days or None, today())
        if c["n"]:
            lines += ["", f"📉 **Vs the closing line:** you beat it on **{c['beat']:.0%}** of {c['n']} legs, "
                          f"**{c['avg'] * 100:+.2f} pts** on average. Beating the close over time is the "
                          "best sign your bets have a real edge, even before results catch up."]
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


def _spend_as(source: str):
    """Credits spent from here on in this task (and its threads) count toward `source`."""
    from .oddsapi import SPEND_SOURCE
    SPEND_SOURCE.set(source)


async def _warm_up():
    """Rosters and defense-vs-position for today, in the background (first scan is faster)."""
    _spend_as("startup warm-up")
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
    _spend_as("pre-tip scans & closing reads")
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
            try:   # near-closing game lines too, logged (not posted) for grading and tuning
                from .chat import track_game_lines
                await asyncio.to_thread(track_game_lines, sport, date)
            except Exception as e:
                log.warning("game-line tracking %s: %s", sport, e)
        from .alerts import close_minutes
        from .oddsapi import credits_ok
        if not close_minutes() or not credits_ok():   # optional spend: skipped once today's budget is used
            continue
        closed = set(load_state(f"close_{date}.json", []))
        for group in pretip_due(events, now, close_minutes(), closed):   # closing read, not posted
            ids = {e["id"] for e in group}
            closed |= ids
            save_state(f"close_{date}.json", sorted(closed))
            try:
                await asyncio.to_thread(scan_events, sport, date, ids,
                                        f"{SPORT_NAMES.get(sport, sport)} closing read", True, True)
                from .chat import track_game_lines
                await asyncio.to_thread(track_game_lines, sport, date)
            except Exception:
                log.exception("closing read failed (%s)", sport)


@tasks.loop(minutes=12)
async def news_loop():
    """Re-price a game when a rotation player is newly ruled Out (basketball, NFL) or an NHL
    team's starting goalie changes."""
    _spend_as("injury/lineup news re-checks")
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
    if "NHL" in _alert_sports() and sent < cap:
        await _goalie_news(channel, date, now, sent, cap)
        sent = load_state(f"news_sent_{date}.json", sent)
    if "MLB" in _alert_sports() and sent < cap:
        await _pitcher_news(channel, date, now, sent, cap)


async def _pitcher_news(channel, date: str, now, sent: int, cap: int):
    """MLB: a team's probable starter changed (scratch): re-price that game."""
    from .alerts import event_for_team, goalie_changes, load_state, save_state
    from .chat import drop_slate, scan_events
    from .mlb_matchups import pitcher_starts, probables
    try:
        current = await asyncio.to_thread(pitcher_starts, date)
        previous = load_state(f"pitchers_{date}.json", None)
        save_state(f"pitchers_{date}.json", current)
        changed = goalie_changes(previous, current)
        if not changed:
            return
        info = await asyncio.to_thread(probables, date)
        events = await asyncio.to_thread(_todays_events, "MLB", date)
    except Exception:
        log.exception("pitcher news check failed")
        return
    for team, pitcher in changed:
        p = info.get(team)
        ev = event_for_team(events, p["team"]) if p else None
        if not ev or sent >= cap:
            continue
        if (dt.datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00")) - now).total_seconds() < 600:
            continue
        sent += 1
        save_state(f"news_sent_{date}.json", sent)
        game = f"{ev['away_team']} @ {ev['home_team']}"
        was = (previous or {}).get(team, ["?"])[0]
        drop_slate("MLB", date)
        drop_slate("MLB", date, "games")
        try:
            messages, res = await asyncio.to_thread(
                scan_events, "MLB", date, {ev["id"]}, f"⚾ {pitcher} starting · Re-Price", False)
        except Exception:
            log.exception("pitcher re-price failed: %s", game)
            continue
        if res.picks:
            await _send_card(channel.send, messages)
        else:
            await channel.send(embed=discord.Embed(
                title=f"⚾ {pitcher} now starting for the {p['team']} · {game}", color=0x2E86C1,
                description=(f"Was {was}. Re-priced {game} against the new starter: "
                             f"{len(res.evaluated)} props priced; nothing cleared the gates, so the "
                             "books have likely already adjusted.")))


async def _goalie_news(channel, date: str, now, sent: int, cap: int):
    """NHL: a team's starting goalie changed (or a backup was confirmed): re-price that game."""
    from .alerts import goalie_changes, load_state, save_state
    from .chat import drop_slate, scan_events
    from .nhl_goalies import goalie_effects, goalie_starts
    try:
        current = await asyncio.to_thread(goalie_starts, date)
        previous = load_state(f"goalies_{date}.json", None)
        save_state(f"goalies_{date}.json", current)
        changed = dict(goalie_changes(previous, current))
        alerted = set(load_state(f"goalies_alerted_{date}.json", []))
        if previous is not None:   # a backup newly confirmed also counts
            for team, (goalie, confirmed) in current.items():
                old = (previous or {}).get(team)
                if confirmed and old and not old[1]:
                    changed.setdefault(team, goalie)
        if not changed:
            return
        effects = await asyncio.to_thread(goalie_effects, date)
        events = await asyncio.to_thread(_todays_events, "NHL", date)
    except Exception:
        log.exception("goalie news check failed")
        return
    from .alerts import event_for_team
    for team, goalie in changed.items():
        key = f"{team}|{goalie}"
        eff = effects.get(team)
        ev = event_for_team(events, eff["team"] if eff else team)
        if key in alerted or not eff or not ev or sent >= cap:
            continue                      # usual starter in net, not on tonight's slate, or done
        if (dt.datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00")) - now).total_seconds() < 600:
            continue
        sent += 1
        alerted.add(key)
        save_state(f"news_sent_{date}.json", sent)
        save_state(f"goalies_alerted_{date}.json", sorted(alerted))
        game = f"{ev['away_team']} @ {ev['home_team']}"
        drop_slate("NHL", date)
        drop_slate("NHL", date, "games")
        try:
            messages, res = await asyncio.to_thread(
                scan_events, "NHL", date, {ev["id"]}, f"🥅 {goalie} in net · Re-Price", False)
        except Exception:
            log.exception("goalie re-price failed: %s", game)
            continue
        if res.picks:
            await _send_card(channel.send, messages)
        else:
            await channel.send(embed=discord.Embed(
                title=f"🥅 {goalie} starting · {game}", color=0x3498DB,
                description=(f"{eff['note']}.\nRe-priced {game}: {len(res.evaluated)} props priced; "
                             "nothing cleared the gates, so the books have likely already adjusted.")))


@tasks.loop(minutes=30)
async def health_loop():
    """Post a warning when credits run low, errors pile up, or an update was rolled back."""
    from .admin import due_alerts, health_problems
    from .alerts import load_state, save_state
    channel = bot.get_channel(bot.alert_channel_id() or 0)
    if channel is None:
        return
    try:
        problems = await asyncio.to_thread(health_problems)
    except Exception:
        log.exception("health check failed")
        return
    sent = load_state("health_sent.json", {})
    msgs = due_alerts(problems, sent)
    if msgs:
        save_state("health_sent.json", sent)
        await channel.send("\n\n".join(msgs)[:1990])


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
    _spend_as("nightly grading")
    day = (dt.datetime.now(TZ).date() - dt.timedelta(days=1)).isoformat()
    from .grade_report import day_records, grade_report, graded_keys
    try:
        before = graded_keys(day_records(day))
        summary, remaining, tracked = await asyncio.to_thread(grade_day, day, [], True)
        embeds, table = await asyncio.to_thread(grade_report, day, summary, before, remaining)
    except Exception:
        log.exception("daily grade failed")
        return
    log.info("daily grade %s: %d tracked markets graded", day, tracked)
    channel_id = os.environ.get("LINEMAKER_CHANNEL_ID") or bot.home
    channel = bot.get_channel(int(channel_id)) if channel_id else None
    from .backup import configured, run_backup
    if configured():   # everything learned overnight goes to the private backup repo
        try:
            log.info(await asyncio.to_thread(run_backup))
        except Exception as e:
            log.error("backup failed: %s", e)
            if channel is not None:
                await channel.send(f"💾⚠️ Nightly backup failed: {str(e)[:300]}")
    await _warm_up()
    if channel is None or not (summary or tracked):
        return
    embeds[0].title = f"📈 Morning Grade · {day}"
    groups = chunk_embeds(embeds)
    for i, group in enumerate(groups):
        files = ([discord.File(io.BytesIO(table.encode()), filename=f"graded_{day}.csv")]
                 if i == len(groups) - 1 and table else [])
        await channel.send(embeds=group, files=files)


@tasks.loop(time=dt.time(11, 0, tzinfo=TZ))
async def daily_post():
    _spend_as("daily post")
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


# ---- remote admin: settings, restart and logs from Discord (owner only) ----------------

async def _owner_only(interaction: discord.Interaction) -> bool:
    from .admin import is_owner
    if await is_owner(bot, interaction.user):
        return True
    await interaction.response.send_message("Only the bot's owner can use this.", ephemeral=True)
    return False


@bot.tree.command(name="settings", description="Owner: show the bot's settings and where each comes from")
@app_commands.describe(search="Only settings containing this, e.g. GAP or ODDS")
async def settings_cmd(interaction: discord.Interaction, search: Optional[str] = None):
    if not await _owner_only(interaction):
        return
    from .admin import settings_lines
    lines = settings_lines(search)
    text = "\n".join(lines)
    if len(text) <= 1900:
        await interaction.response.send_message(text or "No settings match.", ephemeral=True)
    else:
        await interaction.response.send_message(
            f"{len(lines) - 1} settings (attached). Use `search:` to narrow it down.", ephemeral=True,
            file=discord.File(io.BytesIO(text.encode()), filename="settings.txt"))


@bot.tree.command(name="set", description="Owner: change a setting on the server (like editing .env)")
@app_commands.describe(key="Setting name, e.g. LINEMAKER_OVER_MIN_GAP", value="New value (blank turns it off)",
                       restart="Restart now so every part of the bot picks it up (default: no)")
async def set_cmd(interaction: discord.Interaction, key: str, value: str = "", restart: bool = False):
    if not await _owner_only(interaction):
        return
    from .admin import apply_set
    msg = apply_set(key, value)
    await interaction.response.send_message(msg + ("\n🔄 Restarting in a few seconds…" if restart else ""),
                                            ephemeral=True)
    if restart:
        from .admin import restart_soon
        restart_soon()


@bot.tree.command(name="unset", description="Owner: undo a /set (back to settings.env / .env)")
@app_commands.describe(key="Setting name")
async def unset_cmd(interaction: discord.Interaction, key: str):
    if not await _owner_only(interaction):
        return
    from .admin import apply_unset
    await interaction.response.send_message(apply_unset(key), ephemeral=True)


@bot.tree.command(name="envchanges", description="Owner: settings changed remotely, and the lines to copy into .env")
async def envchanges_cmd(interaction: discord.Interaction):
    if not await _owner_only(interaction):
        return
    from .admin import changelog, env_changes
    lines = env_changes()
    text = ["📝 **Copy these into your server's .env** (then ask Claude to clear settings.env, "
            "or /unset the Discord ones):"] + (lines or ["Nothing: everything running matches your .env."])
    log_lines = changelog()
    if log_lines:
        text += ["", "**Change log** (newest first):"] + [f"• {l}" for l in log_lines[:15]]
    await interaction.response.send_message("\n".join(text)[:1990], ephemeral=True)


@bot.tree.command(name="backup", description="Owner: back up tracking, weights and bets to your private GitHub repo now")
async def backup_cmd(interaction: discord.Interaction):
    if not await _owner_only(interaction):
        return
    await interaction.response.defer(thinking=True, ephemeral=True)
    try:
        from .backup import run_backup
        await interaction.followup.send(await asyncio.to_thread(run_backup), ephemeral=True)
    except Exception as e:
        await _report_error(interaction, e)


@bot.tree.command(name="restart", description="Owner: restart the bot on the server")
async def restart_cmd(interaction: discord.Interaction):
    if not await _owner_only(interaction):
        return
    from .admin import restart_soon
    await interaction.response.send_message("🔄 Restarting. Back in about 15–30 seconds.", ephemeral=True)
    restart_soon()


@bot.tree.command(name="logs", description="Owner: the bot's recent console output")
@app_commands.describe(lines="How many lines (default 60)", search="Only lines containing this")
async def logs_cmd(interaction: discord.Interaction, lines: Optional[int] = 60, search: Optional[str] = None):
    if not await _owner_only(interaction):
        return
    from .admin import recent_logs
    text = recent_logs(lines or 60, search)
    if len(text) <= 1900:
        await interaction.response.send_message(f"```\n{text or '(nothing yet)'}\n```", ephemeral=True)
    else:
        await interaction.response.send_message(
            "Recent log lines attached.", ephemeral=True,
            file=discord.File(io.BytesIO(text.encode()), filename="linemaker.log"))


@bot.tree.command(name="status", description="Owner: version, uptime and health of the bot")
async def status_cmd(interaction: discord.Interaction):
    if not await _owner_only(interaction):
        return
    from .admin import status_lines
    await interaction.response.send_message("\n".join(await asyncio.to_thread(status_lines)),
                                            ephemeral=True)


@daily_post.before_loop
async def _wait_ready():
    await bot.wait_until_ready()


def main():
    logging.basicConfig(level=logging.INFO)
    from .admin import install_log_buffer
    install_log_buffer()
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise SystemExit("Set DISCORD_TOKEN to your bot token.")
    bot.run(token, log_handler=None)


if __name__ == "__main__":
    main()
