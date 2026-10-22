import asyncio
import os
import unittest
from unittest import mock

from linemaker.pro import ProConfig
from tests.test_discord import PicksCardTest


def total(embeds):
    from linemaker.bot import _embed_len
    return sum(_embed_len(e) for e in embeds)


class CompactCardTest(unittest.TestCase):
    def setUp(self):
        self.res = PicksCardTest._result(None)

    def test_one_message_with_dropdown(self):
        from linemaker import bot
        from linemaker.scan_format import scan_messages
        messages = scan_messages([("NBA", self.res, {})], "2026-10-21", ProConfig(track=False))
        self.assertGreater(len(messages), 3)                       # the full style: many messages

        async def build():
            return bot.compact_card(messages)
        embeds, view = asyncio.run(build())
        self.assertLessEqual(len(embeds), 10)
        self.assertLessEqual(total(embeds), 6000)
        titles = [e.title for e in embeds]
        self.assertTrue(titles[0].startswith("🎯"))
        self.assertTrue(any(t.startswith("📊") for t in titles))  # the board stays visible
        select = next(c for c in view.children if hasattr(c, "options"))
        self.assertEqual(len(select.options), len(self.res.picks))
        self.assertIn("hit", select.options[0].description)
        self.assertTrue(any(getattr(c, "label", "") == "Notes & passes" for c in view.children))
        # the dropdown opens the full write-up
        detail = bot.PICK_DETAILS[select.options[0].value]
        self.assertTrue(detail["embed"].title.startswith("1. "))

    def test_send_card_posts_once(self):
        from linemaker import bot
        from linemaker.scan_format import scan_messages
        messages = scan_messages([("NBA", self.res, {})], "2026-10-21", ProConfig(track=False))
        sent = []

        async def send(**kw):
            sent.append(kw)

        async def go():
            await bot._send_card(send, messages, text="⏰ I'll re-check before tip.")
        asyncio.run(go())
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["content"], "⏰ I'll re-check before tip.")
        with mock.patch.dict(os.environ, {"LINEMAKER_CARD_STYLE": "full"}):
            sent.clear()
            asyncio.run(go())
            self.assertGreater(len(sent), 3)

    def test_volume_card_keeps_parlays_and_buttons(self):
        from linemaker import bot
        from linemaker.pro import volume_config
        from linemaker.scan_format import volume_messages
        from linemaker.pro import rows_to_markets, run_pro
        rows = [{"date": "2026-10-21", "sport": "NBA", "game_id": f"g{i}", "game": f"A{i} @ H{i}",
                 "bet_type": "prop", "subject": f"Player {i}", "market": "points", "line": "15.5",
                 "mean": "24", "stdev": "4", "books": "FanDuel:-400/+300|DraftKings:-380/+290"}
                for i in range(4)]
        cfg = volume_config()
        cfg.min_prob, cfg.min_odds, cfg.max_odds, cfg.min_gap, cfg.track = 0.5, -100, -10000, -1.0, False
        res = run_pro(rows_to_markets(rows), "2026-10-21", cfg)
        self.assertTrue(res.picks)
        messages = volume_messages([("NBA", res, {"profiles": {}, "context": {}, "history": {}})],
                                   "2026-10-21", cfg)

        async def build():
            return bot.compact_card(messages)
        embeds, view = asyncio.run(build())
        self.assertLessEqual(total(embeds), 6000)
        self.assertTrue(any("Parlays" in (e.title or "") for e in embeds))
        self.assertTrue(any((getattr(c, "label", "") or "").startswith("Log ") for c in view.children))

    def test_nothing_cleared_shows_why_inline(self):
        from linemaker import bot
        from linemaker.scan_format import scan_messages
        self.res.picks = []
        messages = scan_messages([("NBA", self.res, {})], "2026-10-21", ProConfig(track=False))

        async def build():
            return bot.compact_card(messages)
        embeds, view = asyncio.run(build())
        self.assertTrue(any("Scoping" in (e.title or "") for e in embeds))


if __name__ == "__main__":
    unittest.main()
