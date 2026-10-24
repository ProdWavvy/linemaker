"""Plain-English front end: an AI model reads the message, calls Linemaker's tools, and
writes the reply. Every number in a reply comes from a tool result, never from the model.

Provider: LINEMAKER_AI=gemini|claude. Unset, Gemini is used when GEMINI_API_KEY is set,
otherwise Claude.

  GEMINI_API_KEY      Google AI Studio key (free tier available)
  GEMINI_MODEL        model id (default gemini-3.8-flash)
  GEMINI_FALLBACK_MODELS  tried when it's overloaded (default gemini-flash-latest,gemini-2.5-flash)

  ANTHROPIC_API_KEY   Claude API key (required for chat)
  LINEMAKER_MODEL     model id (default claude-opus-5-5; claude-haiku-5-5 is ~40x cheaper)
  LINEMAKER_EFFORT    low | medium | high (default medium)
"""
import asyncio
import csv
import datetime as dt
import io
import logging
import os
import re
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Optional
from zoneinfo import ZoneInfo

from .devig import hold, no_vig, overround
from .freeze import Freezer, grade
from .live import auto_results, board_lines, fetch_feed, run_auto, run_live
from .odds import (american_to_decimal, expected_value, fair_odds, fmt_money, fmt_odds,
                   implied_probability, parse_odds)
from .oddsapi import SPORT_KEYS, OddsAPI
from .pro import ProConfig
from .pro_report import render_pro

log = logging.getLogger("linemaker.chat")

MODEL = os.environ.get("LINEMAKER_MODEL", "claude-opus-5-5")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")
GEMINI_TIMEOUT = int(os.environ.get("GEMINI_TIMEOUT", "45"))
# Tried in order when the main model is overloaded (503) or rate limited (429).
GEMINI_FALLBACKS = [m.strip() for m in os.environ.get(
    "GEMINI_FALLBACK_MODELS", "gemini-flash-latest,gemini-2.5-flash").split(",") if m.strip()]
EFFORT = os.environ.get("LINEMAKER_EFFORT", "medium")
MAX_TOOL_ROUNDS = 8
# Models that accept server-side refusal fallbacks (fallbacks="default"); others, such as
# claude-haiku-5-5, would reject the parameter.
FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5"}
HISTORY_TURNS = 6
# A full-slate prop search costs ~3-5 Odds API credits per game; reuse it for a while.
PLAYS_CACHE_SECONDS = int(os.environ.get("LINEMAKER_PLAYS_CACHE_MINUTES", "30")) * 60
FEED_CACHE_SECONDS = PLAYS_CACHE_SECONDS
# Live odds move by the minute: reuse a live pull only briefly (stops accidental double-asks).
LIVE_CACHE_SECONDS = int(os.environ.get("LINEMAKER_LIVE_CACHE_SECONDS", "60"))
# Bought slates, shared by chat, /picks, /parlay and the daily post (and kept on disk).
_SLATES = {}

def provider() -> str:
    choice = os.environ.get("LINEMAKER_AI", "").strip().lower()
    if choice in ("gemini", "claude"):
        return choice
    return "gemini" if os.environ.get("GEMINI_API_KEY") else "claude"


def chat_configured() -> bool:
    return bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("ANTHROPIC_API_KEY"))


SYSTEM = """You are Linemaker, a sports betting analysis assistant in a Discord server. You \
answer in plain English and use your tools for every number.

Rules:
- Never invent odds, probabilities, stats, injuries, projections or results. Every number you \
state must come from a tool result in this conversation or from the user's own message. If \
something isn't available, say "data unavailable".
- Bets are only placed at FanDuel or DraftKings; the market consensus uses every US book.
- A play qualifies only when the model beats the no-vig market AND the best FanDuel/DraftKings \
price still pays better than fair. Show positive-edge plays only; never force plays.
- For parlay, "safe legs" or volume requests use find_plays with mode "volume"; parlays are
built and posted automatically.
- For "any plays tonight?"-style questions in the NBA, NFL or MLB, use find_plays: it prices \
FantasyPros expert-consensus projections against live odds. When the user gives their own \
number ("I have Brunson at 29 points"), price it with price_projections instead. For other \
leagues there are no automatic projections: ask for one, or use price_saved_projections.
- When a player is listed questionable or doubtful, say so next to the play.
- Flag plays from the same game as correlated, not independent.
- Use Discord formatting: short headers, bullet points, bold for the key numbers. No tables.
- Pick cards with every play's FanDuel and DraftKings odds are posted under your reply \
automatically, so don't repeat each play's numbers: give a short take (best spots, risks, \
correlated legs) in a few lines.
- Answer what was asked without padding, and end with one short follow-up question.

Market names: points, rebounds, assists, threes, PRA, passing yards, rushing yards, receiving \
yards, receptions, longest reception, anytime td, passing tds, shots on goal, hockey points, \
strikeouts, hits, home runs, total bases; game lines: moneyline, spread, game total, \
total goals (soccer), match result (soccer 1X2). For spreads and moneylines, mean is the named \
team's projected margin. For match result, mean is the named team's goals and mean_b the \
opponent's. Player names must match the sportsbook's spelling."""

SPORTS = list(SPORT_KEYS)


def _tz() -> ZoneInfo:
    return ZoneInfo(os.environ.get("LINEMAKER_TZ", "America/New_York"))


def _today() -> str:
    return dt.datetime.now(_tz()).date().isoformat()


TOOLS = [
    {
        "name": "get_odds_board",
        "description": "Live game lines for one league on one date from The Odds API: every "
                       "game (away @ home), consensus no-vig probabilities for moneyline, spread "
                       "and total. Costs 3 API credits (cached for 2 minutes). Use it to see "
                       "tonight's games, who is home, and what the market thinks.",
        "input_schema": {
            "type": "object",
            "properties": {
                "sport": {"type": "string", "enum": SPORTS},
                "date": {"type": "string", "description": "YYYY-MM-DD; omit for today"},
            },
            "required": ["sport"],
        },
    },
    {
        "name": "price_projections",
        "description": "Price the user's projections against live FanDuel/DraftKings odds with "
                       "the full pro engine (simulation, de-vig, 50/50 model-market blend, gap, "
                       "EV). Returns qualifying plays and passes with reasons. Player props cost "
                       "credits per game; game lines cost 3 per league.",
        "input_schema": {
            "type": "object",
            "properties": {
                "sport": {"type": "string", "enum": SPORTS},
                "date": {"type": "string", "description": "YYYY-MM-DD; omit for today"},
                "projections": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "subject": {"type": "string", "description": "player or team"},
                            "market": {"type": "string"},
                            "mean": {"type": "number", "description": "projected stat, margin, total or goals"},
                            "mean_b": {"type": "number", "description": "opponent goals (match result only)"},
                            "line": {"type": "number", "description": "only to pin one specific line"},
                            "stdev": {"type": "number"},
                            "lineup_confirmed": {"type": "boolean"},
                            "news_fresh": {"type": "boolean", "description": "projection just moved on injury news"},
                            "injury_status": {"type": "string"},
                        },
                        "required": ["subject", "market", "mean"],
                    },
                },
            },
            "required": ["sport", "projections"],
        },
    },
    {
        "name": "find_plays",
        "description": "Find today's best plays: player props (kind=props: projections from "
                       "FantasyPros or ESPN game logs, ~3-5 Odds API credits per game) or "
                       "moneylines/spreads/totals (kind=games: free game model from ESPN team "
                       "ratings + Kalshi, ~3 credits per league), priced against live "
                       "FanDuel/DraftKings odds by the pro engine. Cached for 30 minutes.",
        "input_schema": {
            "type": "object",
            "properties": {
                "sport": {"type": "string", "enum": ["NBA", "WNBA", "NFL", "NCAAF", "MLB",
                                                      "NHL"]},
                "date": {"type": "string", "description": "YYYY-MM-DD; omit for today"},
                "refresh": {"type": "boolean",
                            "description": "true only when the user asks for fresh/updated "
                                           "odds; otherwise odds under 30 minutes old are reused"},
                "book": {"type": "string", "enum": ["FanDuel", "DraftKings"],
                         "description": "only when the user asks for one sportsbook's picks "
                                        "(e.g. best FanDuel picks): price and parlay at that "
                                        "book only"},
                "kind": {"type": "string", "enum": ["props", "games", "live", "pp"],
                         "description": "props (default); games = pre-game moneyline, spread, "
                                        "total; live = in-game moneyline/spread/total for games "
                                        "in progress (score + clock model); pp = PrizePicks "
                                        "lines with Power/Flex entries"},
                "mode": {"type": "string", "enum": ["smart", "volume"],
                         "description": "smart = best edges (default); volume = likeliest legs "
                                        "for parlays, ranked by hit chance, with parlays built"},
            },
            "required": ["sport"],
        },
    },
    {
        "name": "price_saved_projections",
        "description": "Run the pro card on the saved projections file for a date (and any CSV "
                       "the user attached to this message) against live odds.",
        "input_schema": {
            "type": "object",
            "properties": {
                "sport": {"type": "string", "enum": SPORTS},
                "date": {"type": "string", "description": "YYYY-MM-DD; omit for today"},
            },
            "required": ["sport"],
        },
    },
    {
        "name": "devig",
        "description": "Implied and no-vig probabilities, overround, hold and fair odds for a "
                       "2- or 3-way market. Odds as American (-110, +150) or decimal (2.10).",
        "input_schema": {
            "type": "object",
            "properties": {
                "odds": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 3},
                "method": {"type": "string", "enum": ["proportional", "shin"]},
            },
            "required": ["odds"],
        },
    },
    {
        "name": "expected_value",
        "description": "EV of a bet: P(win) x net profit - P(loss) x stake, plus break-even "
                       "probability and fair odds.",
        "input_schema": {
            "type": "object",
            "properties": {
                "probability": {"type": "number", "description": "0-1 or 0-100"},
                "odds": {"type": "string"},
                "stake": {"type": "number"},
            },
            "required": ["probability", "odds"],
        },
    },
    {
        "name": "grade_day",
        "description": "Grade a day's picks and tracked markets: game lines from final scores "
                       "(2 credits per league), props from ESPN box scores (free), then re-fit "
                       "the blend weights automatically.",
        "input_schema": {
            "type": "object",
            "properties": {"date": {"type": "string", "description": "YYYY-MM-DD; omit for today"}},
            "required": [],
        },
    },
    {
        "name": "api_credits",
        "description": "The Odds API credits remaining (free).",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
]


@dataclass
class Turn:
    """What one message produced: reply text and any full cards to attach."""
    text: str
    files: list = field(default_factory=list)   # (filename, markdown), e.g. grading reports
    results: list = field(default_factory=list) # (title, ProResult) shown as pick cards


class LinemakerChat:
    def __init__(self):
        self.provider = provider()
        if self.provider == "gemini":
            from google import genai
            self.client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
        else:
            import anthropic
            self.client = anthropic.AsyncAnthropic()
        self.history = defaultdict(lambda: deque(maxlen=HISTORY_TURNS * 2))
        self._feed_cache = {}

    # ---- tools ---------------------------------------------------------------------

    def _feed(self, sport: str, date: str):
        key = (sport, date)
        hit = self._feed_cache.get(key)
        if hit and time.time() - hit[0] < FEED_CACHE_SECONDS:
            return hit[1], hit[2]
        api = OddsAPI()
        feed = fetch_feed(api, [sport], date, _tz())
        self._feed_cache[key] = (time.time(), feed, api.quota.remaining)
        return feed, api.quota.remaining

    def _card(self, sport: str, date: str, projections: list, ctx: dict) -> str:
        cfg = ProConfig()
        snaps = os.environ.get("LINEMAKER_SNAPSHOTS", "snapshots")
        api = OddsAPI()
        lr = run_live(api, [sport], projections, date, _tz(), cfg, Freezer(snaps, date))
        profiles = enrich(lr.result, sport, date)
        ctx.setdefault("results", []).append((sport, lr.result, profiles))
        out = summarize(lr, f"{sport} {date}: {len(lr.rows)} of your projections matched a "
                            f"posted line; Odds API credits left {lr.remaining}.")
        ctx.setdefault("summaries", []).append(out)
        return out

    def run_tool(self, name: str, args: dict, ctx: dict) -> str:
        started = time.time()
        log.info("tool %s %s: started", name, args)
        try:
            out = self._run_tool(name, args, ctx)
        except Exception as e:
            log.warning("tool %s failed after %.1fs: %s: %s", name, time.time() - started,
                        type(e).__name__, e)
            raise
        log.info("tool %s: done in %.1fs", name, time.time() - started)
        return out

    def _run_tool(self, name: str, args: dict, ctx: dict) -> str:
        date = args.get("date") or _today()
        if name == "get_odds_board":
            feed, remaining = self._feed(args["sport"], date)
            lines = board_lines(feed)
            if not lines:
                return f"No {args['sport']} games with prices on {date}."
            out = [f"{args['sport']} board {date} (consensus no-vig; credits left {remaining}):"]
            out += [f"- {game} | {kind}: {desc} ({n} books)" for game, kind, desc, n in lines]
            return "\n".join(out)
        if name == "price_projections":
            rows = []
            for p in args["projections"]:
                row = {"sport": args["sport"]}
                for k, v in p.items():
                    row[k] = str(v).lower() if isinstance(v, bool) else str(v)
                rows.append(row)
            return self._card(args["sport"], date, rows, ctx)
        if name == "find_plays":
            mode = args.get("mode") or "smart"
            kind = args.get("kind") or "props"
            sport = args["sport"]
            from .pro import rows_to_markets, run_pro, volume_config
            snaps = os.environ.get("LINEMAKER_SNAPSHOTS", "snapshots")
            rows, slate, remaining, warning, source = slate_for(sport, date, kind,
                                                                bool(args.get("refresh")))
            if kind == "props" and date == _today():
                from .alerts import arm_pretip
                if arm_pretip(sport, date):
                    ctx.setdefault("armed", []).append(sport)
            cfg = volume_config() if mode == "volume" else ProConfig()
            freezer, tracker = Freezer(snaps, date), None
            if kind == "pp":
                from .pp import pp_config
                from .tracking import Tracker
                cfg, freezer = pp_config(), None
                if os.environ.get("LINEMAKER_TRACKING", "on").lower() not in ("off", "0", "false", "no"):
                    tracker = Tracker(os.path.join(os.environ.get("LINEMAKER_TRACKING_DIR", "tracking"), "pp"), date)
            if kind == "live":
                from .live import live_settings
                cfg, tracker = live_settings(cfg, date)
                freezer = None
            book = args.get("book") if kind != "pp" else None
            if book in ("FanDuel", "DraftKings"):
                # Same odds pull (no extra credits), only that book's prices are bettable.
                # Still tracked (one record per market, so no double counting): your bets
                # from this card settle from those records, and tuning reads the model and
                # no-vig market numbers, which don't depend on the book filter.
                from dataclasses import replace as _replace
                cfg = _replace(cfg, bet_books=(book,))
            else:
                book = None
            res = run_pro(rows_to_markets(rows), date, cfg, freezer, tracker)
            res.books = (book,) if book else ("FanDuel", "DraftKings")
            if kind == "pp":
                from .pp import select_legs
                res.picks = select_legs(res.picks, cfg.pp_floor)
            res.slate = slate
            res.source_warning = warning
            res.projection_source = source
            res.kind = kind
            res.odds_age = slate_age_minutes(sport, date, kind)
            extras = enrich(res, sport, date, cfg.min_prob if mode == "volume" else None)
            extras["mode"] = mode
            from .scan_format import pass_counts
            log.info("%s %s mode: %d matched lines, %d priced at FanDuel/DraftKings, %d picks; "
                     "passes: %s", sport, mode, len(rows), len(res.evaluated), len(res.picks),
                     pass_counts(res))

            class _LR:   # summarize() reads .result
                result = res
            what = ("high-hit-rate legs" if mode == "volume" else "plays") + (
                " (moneylines, spreads, totals)" if kind == "games" else
                " (live in-game lines)" if kind == "live" else "")
            out = summarize(_LR, f"{sport} {date} ({mode} mode, {what}): {source} projections "
                                 f"matched {len(rows)} posted {'/'.join(res.books)} lines; Odds API "
                                 f"credits left {remaining}.")
            ctx.setdefault("results", []).append((sport, res, extras))
            ctx.setdefault("summaries", []).append(out)
            return out
        if name == "price_saved_projections":
            rows = list(ctx.get("attached_rows", []))
            proj_dir = os.environ.get("LINEMAKER_PROJECTIONS_DIR")
            path = os.path.join(proj_dir, f"{date}.csv") if proj_dir else None
            if path and os.path.exists(path):
                from .live import read_projections
                rows += read_projections(path)
            rows = [r for r in rows if r.get("sport", "").upper() == args["sport"].upper()]
            if not rows:
                return (f"No saved {args['sport']} projections for {date}"
                        f"{'' if proj_dir else ' (LINEMAKER_PROJECTIONS_DIR is not set)'} and no "
                        "CSV attached. Ask the user for projections.")
            return self._card(args["sport"], date, rows, ctx)
        if name == "devig":
            odds = [parse_odds(str(o)) for o in args["odds"]]
            how = args.get("method", "proportional")
            fair = no_vig(*odds, method=how)
            lines = [f"{fmt_odds(o)}: implied {implied_probability(o):.2%}, no-vig {p:.2%}, "
                     f"fair {fmt_odds(fair_odds(p))} / {1 / p:.2f}" for o, p in zip(odds, fair)]
            lines.append(f"overround {overround(*odds):.2%}, hold {hold(*odds):.2%}, method {how}")
            return "\n".join(lines)
        if name == "expected_value":
            p = float(args["probability"])
            p = p / 100 if p > 1 else p
            o = parse_odds(str(args["odds"]))
            stake = float(args.get("stake") or 100)
            ev = expected_value(p, o, stake)
            return (f"{p:.1%} at {fmt_odds(o)} (decimal {american_to_decimal(o):.2f}): EV "
                    f"{fmt_money(ev)} on ${stake:g}; break-even {implied_probability(o):.2%}; "
                    f"fair odds {fmt_odds(fair_odds(p))}")
        if name == "grade_day":
            from .grading import grade_day
            summary, remaining, tracked = grade_day(date, [], True)
            if not summary and not tracked:
                return f"Nothing graded for {date} yet: games may not be final, or nothing was tracked."
            return "\n".join(summary + [f"- Results logged for {tracked} tracked markets; "
                                         f"credits left {remaining}."])
        if name == "api_credits":
            api = OddsAPI()
            api.sports()
            return f"{api.quota.remaining} credits remaining, {api.quota.used} used"
        raise ValueError(f"unknown tool {name}")

    # ---- conversation ----------------------------------------------------------------

    async def respond(self, channel_id: int, author: str, text: str,
                      attached_csv: Optional[str] = None) -> Turn:
        ctx = {"files": [], "attached_rows": []}
        if attached_csv:
            ctx["attached_rows"] = list(csv.DictReader(io.StringIO(attached_csv)))
            text += f"\n\n[attached projections CSV with {len(ctx['attached_rows'])} rows]"
        history = self.history[channel_id]
        system = f"{SYSTEM}\n\nToday is {_today()} ({_tz().key})."
        quick = None if attached_csv else quick_plays_request(text)
        if not attached_csv and TIPS_WORDS.fullmatch(text.strip()):
            from .scan_format import TIPS
            reply = "**🧠 How to Think About These Long-Term**\n\n" + "\n\n".join(TIPS)
            history.append({"role": "user", "content": f"{author}: {text}"})
            history.append({"role": "assistant", "content": reply})
            return Turn(reply, ctx["files"], [])
        other = None if attached_csv else unsupported_league(text)
        if quick and other:
            reply = (f"**{other} isn't covered yet**, so I didn't run anything (no credits used). "
                     "Covered: NBA, WNBA, NFL, college football, NHL and MLB props and game lines, "
                     "plus soccer game lines.")
            history.append({"role": "user", "content": f"{author}: {text}"})
            history.append({"role": "assistant", "content": reply})
            return Turn(reply, ctx["files"], [])
        if quick:
            mode, kind = quick_mode(text), quick_kind(text)
            book = None if kind == "pp" else quick_book(text)
            refresh = bool(REFRESH_WORDS.search(text))
            log.info("quick path: %s %s %s%s, no AI model", ",".join(quick), mode, kind,
                     f" ({book} only)" if book else "")
            errors = []
            for sport in quick:
                try:
                    await asyncio.to_thread(self.run_tool, "find_plays",
                                            {"sport": sport, "mode": mode, "kind": kind,
                                             "refresh": refresh, "book": book}, ctx)
                except Exception as e:
                    errors.append(friendly_error(sport, e))
            reply = "\n".join(errors)   # the pick cards carry the answer

        elif self.provider == "gemini":
            reply = await self._gemini_reply(list(history), f"{author}: {text}", system, ctx)
        else:
            reply = await self._claude_reply(list(history), f"{author}: {text}", system, ctx)
        if not reply and not ctx.get("results"):
            reply = "I ran out of steps before finishing. Try asking a narrower question."
        note = pretip_note(ctx.get("armed") or [])
        if note:
            reply = (reply + "\n" if reply else "") + note
        # Keep plain text only, so the next turn starts from a clean, append-only history.
        history.append({"role": "user", "content": f"{author}: {text}"})
        history.append({"role": "assistant", "content": reply or "\n\n".join(
            ctx.get("summaries", []))})
        return Turn(reply, ctx["files"], ctx.get("results", []))

    async def _claude_reply(self, history: list, user_text: str, system: str, ctx: dict) -> str:
        messages = history + [{"role": "user", "content": user_text}]
        extra = ({"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
                 if MODEL in FALLBACK_MODELS else {})
        response = None
        for _ in range(MAX_TOOL_ROUNDS):
            log.info("claude %s: thinking", MODEL)
            response = await self.client.beta.messages.create(
                model=MODEL,
                max_tokens=16000,
                system=system,
                tools=TOOLS,
                messages=messages,
                output_config={"effort": EFFORT},
                **extra,
            )
            if response.stop_reason == "refusal":
                return "I can't help with that one."
            messages.append({"role": "assistant", "content": response.content})
            calls = [b for b in response.content if b.type == "tool_use"]
            if response.stop_reason != "tool_use" or not calls:
                break
            results = await asyncio.gather(*(self._call(b, ctx) for b in calls))
            messages.append({"role": "user", "content": list(results)})

        return "\n".join(b.text for b in response.content if b.type == "text").strip()

    async def _gemini_reply(self, history: list, user_text: str, system: str, ctx: dict) -> str:
        from google.genai import types
        contents = [types.Content(role="user" if m["role"] == "user" else "model",
                                  parts=[types.Part.from_text(text=m["content"])])
                    for m in history]
        contents.append(types.Content(role="user", parts=[types.Part.from_text(text=user_text)]))
        level = os.environ.get("GEMINI_THINKING", "low").upper()
        config = types.GenerateContentConfig(
            system_instruction=system,
            thinking_config=types.ThinkingConfig(thinking_level=getattr(
                types.ThinkingLevel, level, types.ThinkingLevel.LOW)),
            tools=[types.Tool(function_declarations=[
                types.FunctionDeclaration(name=t["name"], description=t["description"],
                                          parameters_json_schema=t["input_schema"])
                for t in TOOLS])],
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        response = None
        for _ in range(MAX_TOOL_ROUNDS):
            started = time.time()
            try:
                response = await self._gemini_generate(contents, config)
            except GeminiBusy:
                if ctx.get("results"):
                    return "Gemini is overloaded right now, so here are the results without a write-up."
                return ("🧠 Gemini is overloaded right now (Google's side, usually clears in a few "
                        "minutes). Questions like \"best NBA props today\" skip Gemini entirely, "
                        "and slash commands like `/picks`, `/devig` and `/quota` always work.")
            except asyncio.TimeoutError:
                log.warning("gemini: no reply after %ss", GEMINI_TIMEOUT)
                if ctx.get("results"):   # the numbers are ready; post the cards without a write-up
                    return "Gemini was slow, so here are the results without a write-up."
                return ("Gemini's free tier is slow right now, so I stopped waiting. Try again in "
                        "a minute, or use a slash command like `/picks` or `/devig`.")
            log.info("gemini: replied in %.1fs (%d tool calls)", time.time() - started,
                     len(response.function_calls or []))
            calls = response.function_calls or []
            if not calls:
                break
            # Append the model turn unchanged (it carries Gemini's thought signatures).
            contents.append(response.candidates[0].content)
            results = await asyncio.gather(*(self._gemini_call(c, ctx) for c in calls))
            contents.append(types.Content(role="user", parts=list(results)))
        try:
            return (response.text or "").strip() if response else ""
        except ValueError:   # blocked by Gemini's safety filters
            return "I can't help with that one."

    async def _gemini_generate(self, contents, config):
        """Main model with retries, then the fallback models, on 503/429 overload errors."""
        from google.genai import errors
        models = [GEMINI_MODEL] + [m for m in GEMINI_FALLBACKS if m != GEMINI_MODEL]
        last = None
        for i, model in enumerate(models):
            for attempt in range(2 if i == 0 else 1):
                log.info("gemini %s: thinking", model)
                try:
                    return await asyncio.wait_for(self.client.aio.models.generate_content(
                        model=model, contents=contents, config=config), timeout=GEMINI_TIMEOUT)
                except errors.APIError as e:
                    if getattr(e, "code", None) not in (429, 500, 503, 404):
                        raise
                    last = e
                    log.warning("gemini %s: %s %s", model, getattr(e, "code", "?"),
                                getattr(e, "status", ""))
                    if attempt == 0 and i == 0:
                        await asyncio.sleep(3)
        raise GeminiBusy(str(last))

    async def _gemini_call(self, call, ctx: dict):
        from google.genai import types
        try:
            out = await asyncio.to_thread(self.run_tool, call.name, dict(call.args or {}), ctx)
            payload = {"result": out}
        except Exception as e:
            payload = {"error": f"{type(e).__name__}: {e}"}
        return types.Part.from_function_response(name=call.name, response=payload)

    async def _call(self, block, ctx: dict) -> dict:
        try:
            out = await asyncio.to_thread(self.run_tool, block.name, dict(block.input), ctx)
            return {"type": "tool_result", "tool_use_id": block.id, "content": out}
        except Exception as e:  # report to the model; it tells the user what failed
            return {"type": "tool_result", "tool_use_id": block.id, "is_error": True,
                    "content": f"{type(e).__name__}: {e}"}


class GeminiBusy(RuntimeError):
    pass


def slate_for(sport: str, date: str, kind: str = "props", refresh: bool = False) -> tuple:
    """(rows, slate, credits left, warning, source) for one league and date. Odds are bought
    at most once per LINEMAKER_PLAYS_CACHE_MINUTES; smart and volume mode share the pull."""
    from .busy import busy
    with busy():
        return _slate_for(sport, date, kind, refresh)


def _slate_for(sport: str, date: str, kind: str, refresh: bool) -> tuple:
    key = (sport, date, kind)
    hit = None if refresh else (_SLATES.get(key) or load_slate_cache(sport, date, kind))
    window = LIVE_CACHE_SECONDS if kind == "live" else PLAYS_CACHE_SECONDS
    if hit and time.time() - hit[0] < window:
        log.info("%s %s %s: reusing odds from %.0f min ago (0 credits)", sport, date, kind,
                 (time.time() - hit[0]) / 60)
        _SLATES[key] = hit
        return tuple(hit[1:])
    from .live import run_best, run_games, run_live_games
    snaps = os.environ.get("LINEMAKER_SNAPSHOTS", "snapshots")
    if kind == "pp":
        # PrizePicks lines, joined to the props projections (bought or reused first).
        from .pp import run_pp
        props_rows = slate_for(sport, date, "props")[0]
        rows, feed = run_pp(OddsAPI(bookmakers="prizepicks"), sport, date, _tz(), props_rows)
        log.info("prizepicks %s: %d lines, %d matched to projections", sport, len(feed), len(rows))
        from .live import _log_link_sample
        _log_link_sample(feed)
        props = _SLATES.get((sport, date, "props"))
        _SLATES[key] = (time.time(), rows, props[2] if props else [], None, None,
                        "PrizePicks lines vs the props model")
        save_slate_cache(sport, date, _SLATES[key], kind)
        return tuple(_SLATES[key][1:])
    if kind == "live":
        # The pre-game line saved earlier (if any) anchors the live model.
        from .live import live_dates
        rows = []
        for d in live_dates(date, _tz()):
            pre = _SLATES.get((sport, d, "games")) or load_slate_cache(sport, d, "games")
            if pre:   # keep only lines saved before their game started (true pre-game prices)
                rows += [r for r in pre[1] if r.get("commence") and
                         dt.datetime.fromisoformat(r["commence"].replace("Z", "+00:00")).timestamp() > pre[0]]
        lr = run_live_games(OddsAPI(), [sport], date, _tz(), ProConfig(), rows, 1.0 if rows else None)
    else:
        runner = run_games if kind == "games" else run_best
        # Fetch and project only; every caller prices the saved rows itself, so pricing here
        # too would run the whole slate through the engine twice.
        lr = runner(OddsAPI(), [sport], date, _tz(), None, None)
    source = lr.result.projection_source
    # Once the ESPN model has taken over, the sample-data warning is resolved.
    warning = None if "ESPN" in source else lr.result.source_warning
    _SLATES[key] = (time.time(), lr.rows, lr.result.slate, lr.remaining, warning, source)
    save_slate_cache(sport, date, _SLATES[key], kind)
    return tuple(_SLATES[key][1:])


def pretip_note(sports: list) -> str:
    """'I'll re-check each NHL game before it starts' (when asking switched pre-tip scans on)."""
    if not sports:
        return ""
    from .alerts import pretip_minutes
    names = ", ".join(SPORT_DISPLAY.get(s, s) for s in sports)
    return (f"⏰ I'll also re-check each {names} game about {pretip_minutes()} minutes before it "
            "starts today (once lineups are in) and post anything new that clears the gates.")


def drop_slate(sport: str, date: str, kind: str = "props"):
    """Forget a saved slate (news changed the projections, so the next ask re-prices)."""
    _SLATES.pop((sport, date, kind), None)
    try:
        os.remove(_cache_path(sport, date, kind))
    except OSError:
        pass


def track_game_lines(sport: str, date: str) -> int:
    """Price today's not-yet-started moneylines/spreads/totals with the game model just to
    log them for grading and weight tuning (nothing is posted). One /odds call, ~3 credits.
    Returns how many markets were logged."""
    from .busy import busy
    from .game_model import SUPPORTED
    from .live import run_games
    if sport not in SUPPORTED or os.environ.get("LINEMAKER_TRACK_GAME_LINES", "on").lower() in ("off", "0", "no", "false"):
        return 0
    cfg = ProConfig()
    cfg.max_picks = 0   # log only: nothing was posted, so nothing counts as a pick
    with busy():
        lr = run_games(OddsAPI(), [sport], date, _tz(), cfg, None)
    log.info("%s game lines logged for tracking: %d markets", sport, len(lr.result.evaluated))
    return len(lr.result.evaluated)


def scan_events(sport: str, date: str, event_ids: set, title: str, only_if_picks: bool = True):
    """Price props for just these games and build the card. None when nothing cleared the
    gates and only_if_picks is set. Returns (messages, result)."""
    from .busy import busy
    from .live import run_best
    from .scan_format import scan_messages
    snaps = os.environ.get("LINEMAKER_SNAPSHOTS", "snapshots")
    cfg = ProConfig()
    with busy():
        lr = run_best(OddsAPI(), [sport], date, _tz(), cfg, Freezer(snaps, date), only=set(event_ids))
    res = lr.result
    res.kind, res.odds_age = "props", 0.0
    extras = enrich(res, sport, date)
    log.info("%s scan of %d game(s): %d priced, %d picks", title, len(event_ids),
             len(res.evaluated), len(res.picks))
    if only_if_picks and not res.picks:
        return None, res
    return scan_messages([(sport, res, extras)], date, cfg, title), res


def slate_age_minutes(sport: str, date: str, kind: str = "props") -> Optional[float]:
    hit = _SLATES.get((sport, date, kind))
    return (time.time() - hit[0]) / 60 if hit else None


def _cache_path(sport: str, date: str, kind: str = "props") -> str:
    name = f"slate_{sport}_{date}" + ("" if kind == "props" else f"_{kind}")
    return os.path.join(os.environ.get("LINEMAKER_CACHE_DIR", "cache"), name + ".json")


def save_slate_cache(sport: str, date: str, entry: tuple, kind: str = "props"):
    """Keep the bought odds on disk so a restart inside the reuse window costs no credits."""
    import json as _json
    try:
        os.makedirs(os.path.dirname(_cache_path(sport, date, kind)), exist_ok=True)
        with open(_cache_path(sport, date, kind), "w") as f:
            _json.dump(list(entry), f)
    except OSError as e:
        log.warning("couldn't save slate cache: %s", e)


def load_slate_cache(sport: str, date: str, kind: str = "props"):
    import json as _json
    try:
        with open(_cache_path(sport, date, kind)) as f:
            entry = tuple(_json.load(f))
        return entry if len(entry) == 6 else None
    except (OSError, ValueError):
        return None


def friendly_error(sport: str, e: Exception) -> str:
    text = str(e)
    if "OUT_OF_USAGE_CREDITS" in text or "quota has been reached" in text.lower():
        return (f"🔋 **{sport}: every Odds API key is out of credits.** The free plan refills "
                "monthly on your signup date (see your the-odds-api.com dashboard), or a paid "
                "plan adds more. `/quota` shows each key. Tip: `LINEMAKER_ALT_LINES=off` in "
                ".env roughly halves the credits each scan uses.")
    if "401" in text:
        return (f"🔑 **{sport}: The Odds API rejected the key.** Check `ODDS_API_KEY` in .env "
                "for typos or extra spaces.")
    return f"⚠️ {sport}: couldn't fetch plays ({type(e).__name__}: {e})"


def enrich(res, sport: str, date: str, floor: Optional[float] = None) -> dict:
    """Extras for the write-ups: game-log hit rates (picks the season rate contradicts are
    dropped), Kalshi win probabilities and team context per game, and each pick's price
    history. Every source is optional; a failure just leaves that part out."""
    from .gamelogs import apply_hit_gate, profiles_for
    extras = {"profiles": {}, "context": {}, "history": {}, "sport": sport, "date": date}
    if os.environ.get("LINEMAKER_HIT_RATES", "on").lower() in ("off", "0", "false", "no"):
        return extras
    try:
        extras["profiles"] = profiles_for(res.picks, sport, date)
    except Exception as e:   # unofficial source: never block the answer on it
        log.warning("hit rates unavailable: %s", e)
    apply_hit_gate(res, extras["profiles"], floor)
    try:
        from .context import game_context
        extras["context"] = game_context(sport, date, sorted({ev.market.game for ev in res.picks}))
    except Exception as e:
        log.warning("game context unavailable: %s", e)
    from .tracking import price_history
    tdir = os.environ.get("LINEMAKER_TRACKING_DIR", "tracking")
    extras["history"] = {ev.market.key: price_history(tdir, date, ev.market.key) for ev in res.picks}
    return extras


def summarize(lr, header: str, limit: int = 10) -> str:
    """Short, model-friendly list of the ranked plays (the full card is attached separately)."""
    res = lr.result
    lines = [header]
    if not res.picks:
        lines.append("No play cleared the gap at FanDuel/DraftKings prices.")
    for i, ev in enumerate(res.picks[:limit], 1):
        s, m = ev.best, ev.market
        flags = [m.injury_status] if m.injury_status not in ("", "active", "probable") else []
        flags += [n for n in ev.notes if n.startswith("soft market")]
        lines.append(f"{i}. **{s.label}** at {fmt_odds(s.best_odds)} {s.best_book} | ours "
                     f"{s.blended:.1%} vs market {s.market_prob:.1%} | gap **{s.gap:+.1%}** | EV "
                     f"{fmt_money(s.ev)}/$100" + (f" | {'; '.join(flags)}" if flags else ""))
    for legs in res.correlated.values():
        lines.append("Same game, not independent: " + " + ".join(legs))
    return "\n".join(lines)


PLAYS_INTENT = re.compile(r"\b(best|top|good|any|today'?s|tonight'?s)\b.*\b(props?|plays?|picks?|bets?)\b"
                          r"|\b(props?|plays?|picks?)\b.*\b(today|tonight)\b", re.I)
SPORT_DISPLAY = {"NCAAF": "college football"}
TIPS_WORDS = re.compile(r"[!/]?(betting )?tips[?!.]*|how (should|do) i think about (these|this|betting)[?!.]*",
                        re.I)
GAME_LINE_WORDS = re.compile(r"\b(money ?lines?|ml|spreads?|totals?|over/unders?|o/u|"
                             r"winners?|who wins|game lines?)\b", re.I)
REFRESH_WORDS = re.compile(r"\b(fresh|refresh|updated?|latest|new odds|re-?pull)\b", re.I)
PP_WORDS = re.compile(r"\b(prize ?picks?|pp|pick ?em)\b", re.I)
BOOK_WORDS = {"FanDuel": re.compile(r"\b(fan ?duel|fd)\b", re.I),
              "DraftKings": re.compile(r"\b(draft ?kings?|dk)\b", re.I)}
LIVE_WORDS = re.compile(r"\b(live|in[- ]?game|in[- ]?play|right now|currently on)\b", re.I)
LIVE_INTENT = re.compile(LIVE_WORDS.pattern + r".*\b(bets?|edges?|plays?|picks?|lines?|games?|odds|"
                         r"money ?lines?|ml|spreads?|totals?|value|props?)\b|\b(bets?|edges?|plays?|"
                         r"picks?|value)\b.*" + LIVE_WORDS.pattern, re.I)
GAMES_INTENT = re.compile(
    r"\b(best|top|good|any|today'?s|tonight'?s|who)\b.*" + GAME_LINE_WORDS.pattern
    + r"|" + GAME_LINE_WORDS.pattern + r".*\b(today|tonight)\b", re.I)
# Checked in order; a more specific league claims its words first (WNBA before NBA, college
# football before NFL), so "wnba" or "college football" never also pulls the pro league.
SPORT_WORDS = {"WNBA": r"\b(wnba|women'?s basketball)\b",
               "NCAAF": r"\b(ncaaf|cfb|college football)\b",
               "NBA": r"\b(nba|basketball)\b", "NFL": r"\b(nfl|football)\b",
               "MLB": r"\b(mlb|baseball)\b", "NHL": r"\b(nhl|hockey)\b"}


# Leagues the bot can't price yet: say so instead of quietly answering with the default league.
UNSUPPORTED = {"UFC/MMA": r"\b(ufc|mma|fight(s|ing)?|bellator|pfl)\b", "boxing": r"\bboxing\b",
               "tennis": r"\b(tennis|atp|wta)\b", "golf": r"\b(golf|pga|liv)\b",
               "F1": r"\b(f1|formula ?1)\b", "NASCAR": r"\bnascar\b"}


def unsupported_league(text: str) -> Optional[str]:
    if any(re.search(p, text, re.I) for p in SPORT_WORDS.values()):
        return None
    return next((name for name, p in UNSUPPORTED.items() if re.search(p, text, re.I)), None)


STRONG_VOLUME = re.compile(r"\b(parlays?|volume)\b", re.I)
SOFT_VOLUME = re.compile(r"\b(safe|safest|locks?|chalk|high[- ]hit|likeliest)\b", re.I)
BET_WORDS = re.compile(r"\b(props?|plays?|picks?|legs?)\b", re.I)


def quick_mode(text: str) -> str:
    """volume for parlay/volume requests, or 'safe/locks/...' with a bet word; else smart."""
    if STRONG_VOLUME.search(text) or (SOFT_VOLUME.search(text) and BET_WORDS.search(text)):
        return "volume"
    return "smart"


def quick_kind(text: str) -> str:
    """games for moneyline/spread/total questions, props otherwise."""
    if PP_WORDS.search(text):
        return "pp"
    if LIVE_WORDS.search(text):
        return "live"
    return "games" if GAME_LINE_WORDS.search(text) else "props"


def quick_book(text: str) -> Optional[str]:
    """FanDuel or DraftKings when the ask names just one of them ("best fanduel picks")."""
    named = [b for b, pat in BOOK_WORDS.items() if pat.search(text)]
    return named[0] if len(named) == 1 else None


def quick_plays_request(text: str) -> Optional[list]:
    """Sports for a plain 'best props tonight?' question, answered without the AI model."""
    if not (PLAYS_INTENT.search(text) or GAMES_INTENT.search(text) or LIVE_INTENT.search(text)
            or PP_WORDS.search(text) or quick_mode(text) == "volume"):
        return None
    if re.search(r"\b(i have|i think|i got|projection|my number|at \d)", text, re.I):
        return None   # the user supplied their own number: let the model read it
    sports, rest = [], text
    for s, pat in SPORT_WORDS.items():
        if re.search(pat, rest, re.I):
            sports.append(s)
            rest = re.sub(pat, " ", rest, flags=re.I)
    if not sports:
        env = [x.strip().upper() for x in os.environ.get("LINEMAKER_SPORTS", "").split(",")]
        sports = [x for x in env if x in SPORT_WORDS] or ["NBA"]
    return sports


def split_for_discord(text: str, limit: int = 1990) -> list:
    """Split on paragraph, then line, boundaries to fit Discord's 2000-char messages."""
    chunks, cur = [], ""
    for para in text.split("\n"):
        piece = para + "\n"
        if len(cur) + len(piece) > limit and cur:
            chunks.append(cur.rstrip())
            cur = ""
        while len(piece) > limit:
            chunks.append(piece[:limit])
            piece = piece[limit:]
        cur += piece
    if cur.strip():
        chunks.append(cur.rstrip())
    return chunks
