import asyncio
import os
import tempfile
import unittest
from types import SimpleNamespace as NS
from unittest import mock


class DashboardTest(unittest.TestCase):
    def test_help_fits_discord(self):
        from linemaker.bot import _embed_len
        from linemaker.dashboard import help_embed
        e = help_embed()
        self.assertLessEqual(_embed_len(e), 6000)
        self.assertTrue(all(len(f.value) <= 1024 for f in e.fields))
        self.assertIn("Pick a parlay", "\n".join(f.value for f in e.fields))

    def test_today_with_nothing_set(self):
        from linemaker.dashboard import today_embed
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {
                "LINEMAKER_TRACKING_DIR": d, "LINEMAKER_CACHE_DIR": d, "LINEMAKER_BANKROLL": ""}):
            e = today_embed("2026-10-27")
        self.assertIn("No bankroll set", e.description)

    def test_today_with_bets_and_news(self):
        from linemaker import bets, chat
        from linemaker.dashboard import today_embed
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {
                "LINEMAKER_TRACKING_DIR": d, "LINEMAKER_CACHE_DIR": d, "LINEMAKER_BANKROLL": "30"}):
            bets.add_bet([{"key": "d|g|P|points|20.5", "side": "A", "label": "P o20.5"}], -120, 1.0, "FanDuel")
            chat.record_news("NBA", "2026-10-27", "A @ H", "Somebody ruled OUT")
            e = today_embed("2026-10-27")
        self.assertIn("Bankroll $30", e.description)
        self.assertIn("1 open", e.description)
        self.assertIn("Somebody ruled OUT", e.description)

    def test_help_typed_in_chat(self):
        from linemaker.chat import LinemakerChat
        c = LinemakerChat.__new__(LinemakerChat)
        import collections
        c.history = collections.defaultdict(list)
        turn = asyncio.run(c.respond(1, "me", "help"))
        self.assertEqual(turn.text, "HELP")


class FreshButtonTest(unittest.TestCase):
    def test_card_gets_fresh_odds_button_and_it_rescans(self):
        from linemaker import bot
        from linemaker.pro import ProConfig
        from linemaker.scan_format import scan_messages
        from tests.test_discord import PicksCardTest
        res = PicksCardTest._result(None)
        messages = scan_messages([("NBA", res, {})], "2026-10-21", ProConfig(track=False))
        sent = []

        async def send(**kw):
            sent.append(kw)
        key = bot._remember({"fresh_scans": [{"sport": "NBA", "kind": "props", "mode": "smart", "date": "2026-10-21"}]})
        asyncio.run(bot._send_card(send, messages, fresh_key=key))
        ids = [getattr(c, "custom_id", "") for c in sent[0]["view"].children]
        self.assertIn(f"lm:fresh:{key}", ids)
        calls = []

        def fake_tool(helper, name, args, ctx):
            calls.append(args)
            ctx.setdefault("results", []).append(("NBA", res, {"mode": "smart", "profiles": {}}))
        followups = []

        class Inter:
            response = NS(defer=lambda **k: asyncio.sleep(0))
            followup = NS(send=lambda **kw: followups.append(kw) or asyncio.sleep(0))
        with mock.patch("linemaker.chat.LinemakerChat.run_tool", fake_tool):
            asyncio.run(bot._rescan(key, "fresh_scans", refresh=True)(Inter()))
        self.assertTrue(calls and calls[0]["refresh"])                   # new odds, same ask
        self.assertTrue(followups and followups[0].get("view") is not None)


class MarketEdgesCrossSportTest(unittest.TestCase):
    def test_scans_every_main_league(self):
        from linemaker import bot
        scans = [{"sport": "NHL", "kind": "props", "teams": ["Rangers"], "book": "FanDuel", "mode": "smart",
                  "date": "2026-10-20", "game_parlay_types": []}]
        with mock.patch.dict(os.environ, {"LINEMAKER_SPORTS": "NBA,NFL,MLB"}):
            out = bot.cross_sport_scans(scans)
        self.assertEqual([a["sport"] for a in out], ["NHL", "NBA", "NFL", "MLB"])   # the card's league first
        self.assertTrue(all(a["mode"] == "market" and a["kind"] == "props" and a["book"] == "FanDuel"
                            and a["teams"] == [] and "date" not in a for a in out))   # whole slates, today

    def test_button_runs_all_leagues_and_drops_idle_ones(self):
        import copy
        from linemaker import bot
        from tests.test_discord import PicksCardTest
        res = PicksCardTest._result(None)
        res.slate = [("Road @ Home", "2026-10-21T23:30:00Z")]
        idle = copy.copy(res)
        idle.slate, idle.evaluated, idle.picks = [], [], []
        key = bot._remember({"market_scans": [{"sport": "NBA", "kind": "props", "teams": ["Knicks"], "book": None,
                                               "mode": "smart", "date": "2026-10-21"}]})
        calls = []

        def fake_tool(helper, name, args, ctx):
            calls.append(args)
            ctx.setdefault("results", []).append((args["sport"], res if args["sport"] == "NBA" else idle,
                                                  {"mode": "market", "profiles": {}}))
        followups = []

        class Inter:
            response = NS(defer=lambda **k: asyncio.sleep(0))
            followup = NS(send=lambda **kw: followups.append(kw) or asyncio.sleep(0))
        with mock.patch.dict(os.environ, {"LINEMAKER_SPORTS": "NBA,NFL,MLB"}), \
                mock.patch("linemaker.chat.LinemakerChat.run_tool", fake_tool), \
                mock.patch("linemaker.bot.cards_for", lambda results: calls.append(
                    [s for s, _r, _e in results]) or [([bot.discord.Embed(title="x")], None)]):
            asyncio.run(bot._rescan(key, "market_scans", mode="market")(Inter()))
        self.assertEqual(sorted(a["sport"] for a in calls[:-1]), ["MLB", "NBA", "NFL"])
        self.assertTrue(all(a["mode"] == "market" and not a["teams"] for a in calls[:-1]))
        self.assertEqual(calls[-1], ["NBA"])                                # no games today: left off
        self.assertTrue(followups)


class PrivateThreadTest(unittest.TestCase):
    def test_ask_goes_to_one_private_thread_a_day(self):
        import tempfile
        import discord
        from linemaker import bot as B
        made, deleted, pointers = [], [], []

        class Thread:
            id, mention, archived = 555, "<#555>", False
            sent = []

            async def send(self, *a, **kw):
                self.sent.append((a, kw))

            async def add_user(self, u):
                pass

        class Channel:
            name = "bets"

            async def create_thread(self, **kw):
                made.append(kw)
                return Thread()

            async def send(self, text, **kw):
                pointers.append((text, kw))

        class Msg:
            guild = object()
            channel = Channel()
            author = NS(id=7, display_name="Wavvy", mention="@Wavvy")

            async def delete(self):
                deleted.append(True)

        fake = NS(get_channel=lambda i: Thread(), fetch_channel=None)
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"LINEMAKER_CACHE_DIR": d,
                                                                             "LINEMAKER_PRIVATE_THREADS": "on"}):
            t1 = asyncio.run(B.LinemakerBot._private_thread(fake, Msg(), "best nba props parlay"))
            t2 = asyncio.run(B.LinemakerBot._private_thread(fake, Msg(), "fresh nba props"))
        self.assertEqual(t1.id, 555)
        self.assertEqual(t2.id, 555)
        self.assertEqual(len(made), 1)                                           # one thread a day
        self.assertEqual(made[0]["type"], discord.ChannelType.private_thread)
        self.assertEqual(deleted, [True, True])                                  # the asks tidied away
        self.assertEqual(pointers[0][1]["delete_after"], 30)
