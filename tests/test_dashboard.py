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
