import os
import tempfile
import unittest

try:
    import discord  # noqa: F401
except ImportError:  # discord.py is only needed for the bot
    discord = None

HERE = os.path.dirname(__file__)


@unittest.skipIf(discord is None, "discord.py not installed")
class DiscordFormatTest(unittest.TestCase):
    def check_limits(self, embeds):
        from linemaker.discord_format import chunk_embeds
        for e in embeds:
            self.assertLessEqual(len(e.title or ""), 256)
            self.assertLessEqual(len(e.description or ""), 4096)
            self.assertLessEqual(len(e.fields), 25)
            for f in e.fields:
                self.assertLessEqual(len(f.value), 1024)
        for group in chunk_embeds(embeds):
            self.assertLessEqual(sum(len(e) for e in group), 6000)
            self.assertLessEqual(len(group), 10)

    def test_pro_embeds(self):
        from linemaker.discord_format import pro_embeds
        from linemaker.freeze import Freezer
        from linemaker.pro import ProConfig, load_pro_markets, run_pro
        with tempfile.TemporaryDirectory() as d:
            cfg = ProConfig(n_sims=2000)
            res = run_pro(load_pro_markets(os.path.join(HERE, "..", "data", "sample_pro_slate.csv")),
                          "2026-10-21", cfg, Freezer(d, "2026-10-21"))
        embeds = pro_embeds(res, cfg)
        self.assertEqual(len(embeds), 1 + len(res.picks))
        self.check_limits(embeds)

    def test_lite_embeds(self):
        from linemaker.config import Config
        from linemaker.discord_format import lite_embeds
        from linemaker.market import load_markets
        from linemaker.pipeline import run
        cfg = Config()
        res = run(load_markets(os.path.join(HERE, "..", "data", "sample_slate.csv")), "2026-10-21", cfg)
        self.check_limits(lite_embeds(res, cfg))

    def test_bot_registers_commands(self):
        from linemaker.bot import bot
        names = {c.name for c in bot.tree.get_commands()}
        self.assertEqual(names, {"pro", "lite", "grade", "devig", "fair", "ev", "live", "odds", "quota", "picks", "calibrate", "parlay", "record", "clv", "tips", "settings", "set", "unset", "restart", "logs", "status", "envchanges", "ufc"})


class TipsTest(unittest.TestCase):
    def test_tips_command_and_chat(self):
        import asyncio
        from linemaker.scan_format import tips_embed
        e = tips_embed()
        self.assertEqual(e.title, "🧠 How to Think About These Long-Term")
        self.assertIn("A bet can lose and still have been a great decision", e.description)
        self.assertIn("hundreds of wagers", e.description)
        from linemaker import chat
        bot = chat.LinemakerChat.__new__(chat.LinemakerChat)
        from collections import defaultdict
        bot.history, bot.provider = defaultdict(list), "gemini"
        for text in ("tips", "/tips", "betting tips?"):
            turn = asyncio.run(bot.respond(1, "J", text))
            self.assertIn("thousand times", turn.text)
        self.assertIsNone(chat.TIPS_WORDS.fullmatch("best props tips for nba tonight"))


if __name__ == "__main__":
    unittest.main()


@unittest.skipIf(discord is None, "discord.py not installed")
class HomeChannelTest(unittest.TestCase):
    def test_in_home(self):
        from types import SimpleNamespace as NS
        from linemaker.bot import in_home
        self.assertTrue(in_home(NS(id=5), None))
        self.assertTrue(in_home(NS(id=5), 5))
        self.assertTrue(in_home(NS(id=9, parent_id=5), 5))     # thread inside the channel
        self.assertFalse(in_home(NS(id=9, parent_id=None), 5))
        self.assertFalse(in_home(None, 5))

    def test_slash_command_outside_home_is_refused(self):
        import asyncio
        import os
        from types import SimpleNamespace as NS
        from unittest import mock
        from linemaker.bot import bot
        sent = []

        async def send_message(text, ephemeral=False):
            sent.append((text, ephemeral))
        inter = NS(channel=NS(id=9, parent_id=None), response=NS(send_message=send_message))
        with mock.patch.dict(os.environ, {"LINEMAKER_HOME_CHANNEL": "5"}):
            ok = asyncio.run(bot.tree.interaction_check(inter))
        self.assertFalse(ok)
        self.assertEqual(sent, [("Linemaker only works in <#5>.", True)])


@unittest.skipIf(discord is None, "discord.py not installed")
class PicksCardTest(unittest.TestCase):
    def _result(self):
        import os as _os
        from linemaker.pro import ProConfig, rows_to_markets, run_pro
        rows = [{"date": "2026-10-21", "sport": "NBA", "game_id": "g1", "game": "Road @ Home",
                 "commence": "2026-10-21T23:30:00Z", "lineup_confirmed": "false",
                 "bet_type": "prop", "subject": "Star Guard", "market": "points", "line": "22.5",
                 "mean": "27", "injury_status": "questionable",
                 "books": "FanDuel:-110/-110|DraftKings:-118/-102|BetMGM:-115/-105"},
                {"date": "2026-10-21", "sport": "NBA", "game_id": "g1", "game": "Road @ Home",
                 "commence": "2026-10-21T23:30:00Z", "bet_type": "prop", "subject": "Big Man",
                 "market": "rebounds", "line": "9.5", "mean": "12.5",
                 "books": "FanDuel:-120/+100|DraftKings:-105/-115"},
                {"date": "2026-10-21", "sport": "NBA", "game_id": "g2", "game": "A @ B",
                 "bet_type": "prop", "subject": "Bench Guy", "market": "assists", "line": "3.5",
                 "mean": "3.4", "books": "FanDuel:-110/-110|DraftKings:-110/-110"}]
        return run_pro(rows_to_markets(rows), "2026-10-21",
                       ProConfig(n_sims=4000, bet_books=("FanDuel", "DraftKings"), track=False))

    def test_grouped_by_best_book_with_both_prices(self):
        from linemaker.discord_format import picks_embeds
        res = self._result()
        embeds = picks_embeds(res, "NBA Best Props")
        titles = [e.title for e in embeds]
        self.assertTrue(titles[0].startswith("🎯 NBA Best Props"))
        self.assertIn("🟦 Best at FanDuel", titles)      # Star Guard over: FD -110 beats DK -118
        self.assertIn("🟩 Best at DraftKings", titles)   # Big Man over: DK -105 beats FD -120
        fd = next(e for e in embeds if e.title == "🟦 Best at FanDuel")
        val = fd.fields[0].value
        self.assertIn("**FD -110** │ DK -118", val)
        self.assertIn("⚠️ Questionable", val)
        self.assertIn("🔗 Same game as #", val)
        self.assertIn("Tracked **3** markets across **2** games", embeds[0].description)
        # The projection build (Step / Value) is not on the card.
        self.assertFalse(any("Step" in f.name or "↳" in f.value for e in embeds for f in e.fields))

    def test_no_plays(self):
        from linemaker.discord_format import picks_embeds
        res = self._result()
        res.picks = []
        embeds = picks_embeds(res, "NBA Best Props")
        self.assertEqual(len(embeds), 1)
        self.assertIn("No plays", embeds[0].description)


@unittest.skipIf(discord is None, "discord.py not installed")
class ScanFormatTest(unittest.TestCase):
    def test_scan_layout_and_hit_gate(self):
        from linemaker.discord_format import chunk_embeds
        from linemaker.gamelogs import HitProfile, Window
        from linemaker.pro import ProConfig
        from linemaker.scan_format import scan_embeds
        res = PicksCardTest()._result()
        star = next(ev for ev in res.picks if ev.market.subject == "Star Guard")
        bench = next(ev for ev in res.picks if ev.market.subject == "Bench Guy")
        profiles = {
            star.market.key: HitProfile(season=Window(70, 80, 26.1),
                                        recent={5: Window(5, 5, 29.0), 10: Window(9, 10, 27.5)},
                                        venue=Window(36, 40, 26.8), venue_label="at home",
                                        versus=Window(2, 3, 24.0), opponent="Road Team"),
            # Season rate below the price: should be vetoed into the notes.
            bench.market.key: HitProfile(season=Window(20, 50, 3.1)),
        }
        embeds = scan_embeds([("NBA", res, profiles)], "2026-10-21", ProConfig(track=False))
        titles = [e.title for e in embeds]
        self.assertTrue(titles[0].startswith("🎯 NBA Value Scan · Today's Board (Oct 21, 2026)"))
        self.assertIn("📊 The Board, Ranked by Probability Gap", titles)
        self.assertNotIn("🧠 How to Think About These Long-Term", titles)   # lives in /tips now
        self.assertIn("📌 Staking Note", titles)
        self.assertIn("⚠️ Scoping & Data Notes", titles)
        self.assertFalse(any("Bench Guy" in (t or "") for t in titles))
        notes = embeds[-1].description
        self.assertIn("Bench Guy Under 3.5 assists", notes)
        self.assertIn("excluded on data grounds", notes)
        star_card = next(e for e in embeds if "Star Guard" in (e.title or ""))
        body = star_card.description
        for piece in ("Implied 52.4% → Estimated", "Over the last 80 games, Guard has cleared 22.5 "
                      "**87.5%**", "100% over the last 5 (29.0)", "At home, where tonight's game is",
                      "Against the Road Team, 2 of 3", "**FanDuel -110** · DraftKings -118",
                      "Why it's a good long-term bet at this price", "Bet this at FanDuel",
                      "listed **questionable**"):
            self.assertIn(piece, body)
        for group in chunk_embeds(embeds):
            self.assertLessEqual(sum(len(e) for e in group), 6000)


@unittest.skipIf(discord is None, "discord.py not installed")
class ButtonPagesTest(unittest.TestCase):
    def _detail(self):
        from linemaker.context import TeamContext
        from linemaker.gamelogs import HitProfile, Window
        res = PicksCardTest()._result()
        ev = next(e for e in res.picks if e.market.subject == "Star Guard")
        prof = HitProfile(season=Window(60, 80, 25.0), recent={5: Window(4, 5, 27.0)},
                          opponent="Road Team", line=22.5, over=True,
                          games=[("2026-10-19", "Road Team", True, 28.0),
                                 ("2026-10-17", "Other Club", False, 19.0)])
        ctx = {"kalshi": {"Home": 0.6, "Road": 0.4},
               "teams": {"Road Team": TeamContext("Road Team", 82, "last season", pace=100.2,
                                                  def_rating=115.0, allowed=115.2)}}
        hist = [("2026-10-21T15:00:00+00:00", {"A": {"FanDuel": -105, "DraftKings": -110}}),
                ("2026-10-21T20:00:00+00:00", {"A": {"FanDuel": -110, "DraftKings": -118}})]
        return {"rank": 1, "ev": ev, "profile": prof, "context": ctx, "history": hist}

    def test_pages(self):
        from linemaker.scan_format import chart_page, movement_page, odds_page, stats_page
        d = self._detail()
        odds = odds_page(d).description
        self.assertIn("★FanDuel", odds)
        self.assertIn("BetMGM", odds)
        chart = chart_page(d).description
        self.assertIn("✅", chart)
        self.assertIn("❌", chart)
        move = movement_page(d, "America/New_York").description
        self.assertIn("-105", move)
        self.assertIn("-118", move)
        stats = stats_page(d)
        names = [f.name for f in stats.fields]
        self.assertIn("Kalshi win prob", names)
        self.assertIn("Road Team (last season, est.)", names)
        self.assertFalse(any("Step" in n for n in names))   # projection build stays in the back end

    def test_write_up_uses_context_and_movement(self):
        from linemaker.pro import ProConfig
        from linemaker.scan_format import scan_messages
        d = self._detail()
        res = PicksCardTest()._result()
        ev = next(e for e in res.picks if e.market.subject == "Star Guard")
        extras = {"profiles": {ev.market.key: d["profile"]},
                  "context": {ev.market.game: d["context"]},
                  "history": {ev.market.key: d["history"]}}
        msgs = scan_messages([("NBA", res, extras)], "2026-10-21", ProConfig(track=False))
        detail_msgs = [m for m in msgs if m[1] is not None]
        self.assertTrue(detail_msgs)
        body = next(m[0][0].description for m in detail_msgs if m[1]["ev"].market.subject == "Star Guard")
        self.assertIn("Kalshi's prediction market has the Home at **60%**", body)
        self.assertIn("estimated defensive rating of 115.0", body)
        self.assertIn("FanDuel opened at -105 on the first scan and is -110 now", body)

    def test_view_has_four_buttons(self):
        import asyncio
        from linemaker.bot import PickView

        async def build():
            return PickView("abc")
        view = asyncio.run(build())
        self.assertEqual([c.label for c in view.children],
                         ["Odds Comparison", "Hit Rate Chart", "Line Movement", "Key Stats"])


@unittest.skipIf(discord is None, "discord.py not installed")
class SlateNotesTest(unittest.TestCase):
    def test_full_slate_missing_games_and_reasons(self):
        from linemaker.pro import ProConfig
        from linemaker.scan_format import scan_embeds
        res = PicksCardTest()._result()
        res.picks = []
        res.passes += [("X Over 1.5 points", "no gap: blended 50% vs best price -110 (52.4%)"),
                       ("Y Over 2.5 points", "market caught up: model 40% vs no-vig 45%"),
                       ("Z Over 9.5 points", "odds -120 outside window")]
        res.slate = [("Road @ Home", "2026-10-21T23:30:00Z"), ("A @ B", "2026-10-22T00:00:00Z"),
                     ("Early @ Game", "2020-01-01T00:00:00Z"), ("Late @ Game", "2099-01-01T00:00:00Z")]
        embeds = scan_embeds([("NBA", res, {})], "2026-10-21", ProConfig(track=False))
        self.assertIn("NBA (4 games on the slate, 2 with props I could price)", embeds[0].description)
        notes = embeds[-1].description
        self.assertIn("no priceable props yet (1):** Late @ Game", notes)
        self.assertIn("already started (1):** Early @ Game", notes)
        self.assertIn("1 the best price didn't beat our estimate", notes)
        self.assertIn("1 price outside your odds window", notes)


@unittest.skipIf(discord is None, "discord.py not installed")
class ClosestMissesTest(unittest.TestCase):
    def test_misses_listed_with_reason(self):
        from linemaker.pro import ProConfig, rows_to_markets, run_pro
        from linemaker.scan_format import scan_embeds
        rows = [{"date": "2026-10-21", "sport": "NBA", "game_id": "g", "game": "A @ H",
                 "bet_type": "prop", "subject": "Near Miss", "market": "points", "line": "20.5",
                 "mean": "23", "books": "FanDuel:-110/-110|DraftKings:-112/-108"}]
        cfg = ProConfig(n_sims=4000, track=False, min_odds=-150, max_odds=-300,
                        bet_books=("FanDuel", "DraftKings"))
        res = run_pro(rows_to_markets(rows), "2026-10-21", cfg)
        notes = scan_embeds([("NBA", res, {})], "2026-10-21", cfg)[-1].description
        self.assertIn("closest misses", notes)
        self.assertIn("Near Miss Over 20.5 points -110", notes)
        self.assertIn("outside window", notes)
