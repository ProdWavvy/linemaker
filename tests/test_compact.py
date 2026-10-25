import asyncio
from types import SimpleNamespace as NS
import os
from zoneinfo import ZoneInfo
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
        self.assertTrue(detail["embed"]["title"].startswith("1. "))

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

    def test_market_edges_button(self):
        from linemaker import bot
        from linemaker.scan_format import scan_messages
        messages = scan_messages([("NBA", self.res, {})], "2026-10-21", ProConfig(track=False))
        sent = []

        async def send(**kw):
            sent.append(kw)
        key = bot._remember({"market_scans": [{"sport": "NBA", "kind": "props", "teams": ["Knicks"],
                                                "book": None, "mode": "smart", "date": "2026-10-21"}]})
        asyncio.run(bot._send_card(send, messages, market_key=key))
        ids = [getattr(c, "custom_id", "") for c in sent[0]["view"].children]
        self.assertIn(f"lm:mkt:{key}", ids)
        sent.clear()
        asyncio.run(bot._send_card(send, messages))                # no key: no button
        self.assertFalse(any(str(getattr(c, "custom_id", "")).startswith("lm:mkt:")
                             for c in sent[0]["view"].children))

    def test_volume_card_keeps_parlays_and_buttons(self):
        from linemaker import bot
        from linemaker.pro import volume_config
        from linemaker.scan_format import volume_messages
        from linemaker.pro import rows_to_markets, run_pro
        rows = [{"date": "2026-10-21", "sport": "NBA", "game_id": f"g{i}", "game": f"A{i} @ H{i}",
                 "bet_type": "prop", "subject": f"Player {i}", "market": "points", "line": "15.5",
                 "mean": "24", "stdev": "4", "books": "FanDuel:-400/+300|DraftKings:-380/+290"}
                for i in range(6)]
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
        self.assertTrue(any(e.title == "✅ High-Hit-Rate Legs" for e in embeds))     # the board stays
        build = next(c for c in view.children if getattr(c, "label", "") == "Build my own parlay")
        from linemaker import bot as _bot
        pool = _bot.PICK_DETAILS[build.custom_id.split(":")[2]]["picks"]
        self.assertEqual(len(pool), len(res.picks))                  # every leg, not just 2-4

        async def picker():
            return _bot.slip_message(build.custom_id.split(":")[2], (1 << len(pool)) - 1)
        embed, sview = asyncio.run(picker())
        self.assertIn(f"{len(pool)}-Leg Parlay", embed.title)        # as many legs as you like

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


class ParlayButtonTest(unittest.TestCase):
    def _legs(self, games=("g0", "g1", "g2")):
        from linemaker.pro import ProConfig, rows_to_markets, run_pro
        rows = [{"date": "2026-10-21", "sport": "NBA", "game_id": g, "game": f"A @ H{i}",
                 "bet_type": "prop", "subject": f"Player {i}", "market": "points", "line": "15.5",
                 "mean": "24", "stdev": "4",
                 "books": "FanDuel:-400/+300|DraftKings:-350/+280" if i else "FanDuel:-300/+240"}
                for i, g in enumerate(games)]
        cfg = ProConfig(track=False, min_gap=-1.0)
        return run_pro(rows_to_markets(rows), "2026-10-21", cfg).evaluated

    def test_custom_parlay_math(self):
        from linemaker.odds import american_to_decimal
        from linemaker.parlay import custom_parlays, same_game
        legs = self._legs()
        built = dict((p.book, (p, miss)) for p, miss in custom_parlays(legs))
        fd, missing = built["FanDuel"]
        self.assertEqual(len(fd.legs), 3)
        self.assertAlmostEqual(fd.decimal, american_to_decimal(-300) * american_to_decimal(-400) ** 2)
        dk, dk_missing = built["DraftKings"]
        self.assertEqual(len(dk.legs), 2)                       # Player 0 isn't on DraftKings
        self.assertEqual(dk_missing, ["Player 0 Over 15.5 points"])
        self.assertEqual(same_game(legs), [])
        self.assertEqual(len(same_game(self._legs(("g0", "g0", "g1")))), 1)

    def test_card_and_buttons(self):
        from linemaker import bot

        async def build():
            legs = self._legs()
            card = bot.compact_card([([discord_embed("🎯 h")], None)] +
                                    [([discord_embed(f"{i}. x")], {"rank": i, "ev": ev, "profile": None})
                                     for i, ev in enumerate(legs, 1)])
            embed, view = bot.parlay_card(legs)
            picker = bot.slip_message(bot._remember({"picks": [{"rank": i, "ev": ev}
                                                              for i, ev in enumerate(legs, 1)]}))[1]
            return card, embed, view, picker
        (embeds, cview), embed, view, picker = asyncio.run(build())
        self.assertTrue(any(getattr(c, "label", "") == "Turn into parlay" for c in cview.children))
        self.assertEqual(embed.title, "🎰 Your 3-Leg Parlay")
        self.assertEqual([f.name.split(" · ")[0] for f in embed.fields], ["🟦 FanDuel", "🟩 DraftKings"])
        self.assertRegex(embed.fields[0].name, r"· \d+% hit")              # odds and hit chance up top
        self.assertIn("💵", embed.fields[0].value)
        self.assertIn("Not offered here", embed.fields[1].value)
        labels = [getattr(c, "label", "") for c in view.children]
        self.assertIn("Log FanDuel parlay", labels)
        self.assertEqual(sum(1 for c in picker.children if ":t" in (c.custom_id or "")), 3)


def discord_embed(title):
    import discord
    return discord.Embed(title=title, description="d")


class RestartTest(unittest.TestCase):
    """Buttons on a card keep working after the bot restarts."""

    def test_buttons_survive_restart(self):
        import tempfile
        from linemaker import bot
        from linemaker.scan_format import scan_messages
        res = PicksCardTest._result(None)
        messages = scan_messages([("NBA", res, {})], "2026-10-21", ProConfig(track=False))
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"LINEMAKER_CACHE_DIR": d}):
            async def build():
                return bot.compact_card(messages)
            embeds, view = asyncio.run(build())
            select = next(c for c in view.children if hasattr(c, "options"))
            ids = [c.custom_id for c in view.children if getattr(c, "custom_id", None)]
            bot.save_details()
            bot.PICK_DETAILS.clear()                 # the restart
            bot.load_details()
            sent = []

            class Resp:
                async def send_message(self, *a, **kw):
                    sent.append(kw)

                async def send_modal(self, modal):
                    sent.append({"modal": modal})

            class Inter:
                def __init__(self, cid, values=None):
                    self.data = {"custom_id": cid, "values": values or []}
                    self.response = Resp()
                    self.type = None

            async def press():
                ok = await bot.route_component(Inter(select.custom_id, [select.options[0].value]))
                for cid in ids:
                    if cid.startswith(("lm:notes:", "lm:mkparlay:")):
                        await bot.route_component(Inter(cid))
                return ok
            self.assertTrue(asyncio.run(press()))
        self.assertTrue(sent[0]["embed"].title.startswith("1. "))      # the pick write-up
        self.assertTrue(sent[0]["ephemeral"])
        self.assertTrue(any("view" in kw and kw.get("ephemeral") for kw in sent[1:]))   # parlay picker
        self.assertTrue(any("embeds" in kw for kw in sent))            # notes

    def test_not_ours(self):
        from linemaker import bot

        class Inter:
            data = {"custom_id": "someone-else"}
        self.assertFalse(asyncio.run(bot.route_component(Inter())))


class BigParlayTest(unittest.TestCase):
    def test_twelve_legs_get_a_text_link(self):
        import json
        from linemaker import bot
        from linemaker.oddsapi import feed_row, parse_event_props
        from linemaker.pro import ProConfig, rows_to_markets, run_pro
        rows = []
        for i in range(12):
            event = {"id": f"e{i}", "home_team": "H", "away_team": "A", "commence_time": "2026-10-21T23:30:00Z",
                     "bookmakers": [{"key": "fanduel", "title": "FanDuel", "markets": [{"key": "player_points", "sid": f"42.61770{i:04d}", "outcomes": [
                         {"name": "Over", "description": f"P{i}", "price": -300, "point": 10.5,
                          "link": f"https://sportsbook.fanduel.com/addToBetslip?marketId=42.61770{i:04d}&selectionId=4318{i:04d}"},
                         {"name": "Under", "description": f"P{i}", "price": 240, "point": 10.5}]}]}]}
            fm = parse_event_props(event, "NBA", ZoneInfo("America/New_York"))[0]
            r = feed_row(fm)
            r.update(mean="16", stdev="3")
            rows.append(r)
        legs = run_pro(rows_to_markets(rows), "2026-10-21", ProConfig(track=False, min_gap=-1.0)).evaluated

        async def build():
            return bot.parlay_card(legs)
        embed, view = asyncio.run(build())
        fd = next(f for f in embed.fields if "FanDuel" in f.name)
        self.assertIn("Open this 12-leg slip on FanDuel", fd.value)
        self.assertRegex(fd.name, r"% hit")
        self.assertTrue(any(getattr(c, "label", "") == "Log FanDuel parlay" for c in view.children))


class MoneylineParlayTest(unittest.TestCase):
    def test_game_card_gets_parlay_moneylines_button(self):
        import json
        from linemaker import bot
        from linemaker.oddsapi import feed_row, parse_game_odds
        from linemaker.pro import ProConfig, rows_to_markets, run_pro
        from linemaker.scan_format import scan_messages
        path = os.path.join(os.path.dirname(__file__), "fixtures", "oddsapi_nba_odds.json")
        with open(path) as f:
            events = json.load(f)[:1]
        twin = json.loads(json.dumps(events[0]).replace(events[0]["home_team"], "Twin Home")
                          .replace(events[0]["away_team"], "Twin Away"))
        twin["id"] = "twin"
        events.append(twin)
        rows = []
        for fm in parse_game_odds(events, "NBA", ZoneInfo("America/New_York")):
            if fm.bet_type == "moneyline":
                r = feed_row(fm)
                r.update(mean="3", stdev="12")
                rows.append(r)
        date = rows[0]["date"]
        res = run_pro(rows_to_markets(rows), date, ProConfig(track=False))
        res.kind = "games"
        games = {ev.market.game_id for ev in res.evaluated}
        self.assertGreaterEqual(len(games), 2)
        msgs = scan_messages([("NBA", res, {"profiles": {}})], date, ProConfig(track=False))

        async def build():
            return bot.compact_card(msgs)
        _embeds, view = asyncio.run(build())
        btn = next(c for c in view.children if getattr(c, "label", "") == "Parlay moneylines")
        pool = bot.PICK_DETAILS[btn.custom_id.split(":")[2]]["picks"]
        self.assertEqual(len(pool), len(games))          # one per game
        for d in pool:                                   # the side the model thinks wins
            self.assertEqual(d["ev"].best.blended, max(s.blended for s in d["ev"].sides))

        async def card():
            return bot.parlay_card([d["ev"] for d in pool])
        embed, _v = asyncio.run(card())
        self.assertTrue(any("% hit" in f.name for f in embed.fields))


class OneBookTest(unittest.TestCase):
    def test_quick_book(self):
        from linemaker.chat import quick_book, quick_kind, quick_plays_request
        self.assertEqual(quick_book("best fanduel picks"), "FanDuel")
        self.assertEqual(quick_book("best fd props tonight"), "FanDuel")
        self.assertEqual(quick_book("best draftkings picks nba"), "DraftKings")
        self.assertEqual(quick_book("top dk plays"), "DraftKings")
        self.assertIsNone(quick_book("best fanduel and draftkings picks"))
        self.assertIsNone(quick_book("best nba props"))
        self.assertIsNotNone(quick_plays_request("best fanduel picks"))
        self.assertEqual(quick_kind("best prizepick"), "pp")
        self.assertEqual(quick_kind("best pp nba"), "pp")

    def test_parlay_card_one_book(self):
        from linemaker import bot
        from linemaker.oddsapi import feed_row, parse_event_props
        from linemaker.pro import ProConfig, rows_to_markets, run_pro
        rows = []
        for i in range(3):
            books = [{"key": k, "title": t, "markets": [{"key": "player_points", "outcomes": [
                {"name": "Over", "description": f"P{i}", "price": -150, "point": 10.5},
                {"name": "Under", "description": f"P{i}", "price": 120, "point": 10.5}]}]}
                for k, t in (("fanduel", "FanDuel"), ("draftkings", "DraftKings"))]
            event = {"id": f"e{i}", "home_team": "H", "away_team": "A",
                     "commence_time": "2026-10-21T23:30:00Z", "bookmakers": books}
            r = feed_row(parse_event_props(event, "NBA", ZoneInfo("America/New_York"))[0])
            r.update(mean="14", stdev="3")
            rows.append(r)
        cfg = ProConfig(track=False, min_gap=-1.0, bet_books=("DraftKings",))
        legs = run_pro(rows_to_markets(rows), "2026-10-21", cfg).evaluated
        self.assertTrue(all(ev.best.best_book == "DraftKings" for ev in legs))

        async def build():
            return bot.parlay_card(legs, ("DraftKings",))
        embed, view = asyncio.run(build())
        self.assertEqual([f.name for f in embed.fields if "FanDuel" in f.name], [])
        self.assertTrue(any("DraftKings" in f.name for f in embed.fields))
        self.assertNotIn("FD ", embed.description)
        self.assertFalse(any("FanDuel" in getattr(c, "label", "") for c in view.children))


class LiveSlipTest(unittest.TestCase):
    def test_every_tap_reprices(self):
        from linemaker import bot
        legs = ParlayButtonTest._legs(ParlayButtonTest())
        key = bot._remember({"picks": [{"rank": i, "ev": ev} for i, ev in enumerate(legs, 1)]})

        class Resp:
            def __init__(self):
                self.edited = None

            async def edit_message(self, **kw):
                self.edited = kw

        class Inter:
            def __init__(self, cid):
                self.data = {"custom_id": cid}
                self.response = Resp()

        def tap(cid):
            it = Inter(cid)
            self.assertTrue(asyncio.run(bot.route_component(it)))
            return it.response.edited["embed"], it.response.edited["view"]

        def btn(view, suffix):
            return next(c for c in view.children if (c.custom_id or "").endswith(suffix))

        async def first():
            return bot.slip_message(key)
        _e, v = asyncio.run(first())
        e, v = tap(btn(v, ":t0").custom_id)
        self.assertIn("Tap one more leg", e.description)
        self.assertTrue(btn(v, ":t0").label.startswith("✅"))
        e, v = tap(btn(v, ":t1").custom_id)
        self.assertEqual(e.title, "🎰 Your 2-Leg Parlay · live slip")
        self.assertRegex(e.fields[0].name, r"% hit")
        self.assertIn("EV", e.fields[0].value)
        self.assertIn("Log FanDuel parlay", [c.label for c in v.children])
        e, v = tap(btn(v, ":t2").custom_id)
        self.assertEqual(e.title, "🎰 Your 3-Leg Parlay · live slip")
        e, v = tap(btn(v, ":t0").custom_id)                          # tap again removes it
        self.assertEqual(e.title, "🎰 Your 2-Leg Parlay · live slip")
        self.assertFalse(btn(v, ":t0").label.startswith("✅"))
        boards = [c for c in v.children if ":sboard:" in (c.custom_id or "")]
        self.assertEqual([b.label for b in boards], ["Odds", "Hit Rates", "Line Moves", "Key Stats"])

        class R2:
            async def send_message(self, **kw):
                self.sent = kw
        it = NS(data={"custom_id": boards[0].custom_id}, response=R2())
        self.assertTrue(asyncio.run(bot.route_component(it)))
        self.assertEqual(it.response.sent["embed"].title, "🏷️ Odds, Your 2-Leg Slip")
        self.assertEqual(it.response.sent["embed"].description.count("\n"), 2 + 2)
        e, v = tap(btn(v, ":clr").custom_id)
        self.assertFalse(any(":sboard:" in (c.custom_id or "") for c in v.children))
        self.assertIn("Tap legs below", e.description)
        self.assertTrue(all(len(c.custom_id or "") <= 100 and len(c.label or "") <= 80 for c in v.children))

    def test_pages_for_25_legs(self):
        from linemaker import bot
        legs = ParlayButtonTest._legs(ParlayButtonTest())
        key = bot._remember({"picks": [{"rank": i, "ev": legs[i % 3]} for i in range(1, 26)]})

        async def build(page):
            return bot.slip_message(key, (1 << 25) - 1, page)
        _e, v0 = asyncio.run(build(0))
        _e, v1 = asyncio.run(build(1))
        _e, v2 = asyncio.run(build(2))
        self.assertEqual(sum(":t" in c.custom_id for c in v0.children if c.custom_id), 10)
        self.assertEqual(sum(":t" in c.custom_id for c in v2.children if c.custom_id), 5)
        self.assertLessEqual(len(v0.children), 25)


class BoardButtonsTest(unittest.TestCase):
    def test_main_card_has_whole_card_views(self):
        from linemaker import bot
        legs = ParlayButtonTest._legs(ParlayButtonTest())

        class Resp:
            async def send_message(self, **kw):
                self.sent = kw

        async def build():
            return bot.compact_card([([discord_embed("🎯 h")], None)] +
                                    [([discord_embed(f"{i}. x")], {"rank": i, "ev": ev, "profile": None,
                                                                    "history": None})
                                     for i, ev in enumerate(legs, 1)])
        _embeds, view = asyncio.run(build())
        boards = [c for c in view.children if ":board:" in (getattr(c, "custom_id", "") or "")]
        self.assertEqual([b.label for b in boards], ["Odds", "Hit Rates", "Line Moves", "Key Stats"])
        for b in boards:
            it = NS(data={"custom_id": b.custom_id}, response=Resp())
            self.assertTrue(asyncio.run(bot.route_component(it)))
            embed = it.response.sent["embed"]
            self.assertIn("Every Pick", embed.title)
            self.assertEqual(embed.description.count("\n"), len(legs) + 2)   # header + one row per pick


class SlipBoostSuggestTest(unittest.TestCase):
    def test_boost_and_best_next_leg(self):
        from linemaker import bot
        legs = ParlayButtonTest._legs(ParlayButtonTest())
        key = bot._remember({"picks": [{"rank": i, "ev": ev} for i, ev in enumerate(legs, 1)]})

        class Resp:
            async def edit_message(self, **kw):
                self.edited = kw

        def tap(cid):
            it = NS(data={"custom_id": cid}, response=Resp())
            self.assertTrue(asyncio.run(bot.route_component(it)))
            return it.response.edited["embed"], it.response.edited["view"]

        def btn(view, suffix):
            return next(c for c in view.children if (c.custom_id or "").endswith(suffix))

        async def first():
            return bot.slip_message(key, 0b11)
        e0, v = asyncio.run(first())
        fd0 = next(f.value for f in e0.fields if "FanDuel" in f.name)
        e, v = tap(btn(v, ":b").custom_id)                    # +10% boost
        fd1 = next(f.value for f in e.fields if "FanDuel" in f.name)
        self.assertIn("🚀 +10%", fd1)
        self.assertNotEqual(fd0, fd1)
        self.assertIn("Boost +10%", btn(v, ":b").label)
        e, v = tap(btn(v, ":s").custom_id)                    # adds the third leg
        self.assertEqual(e.title, "🎰 Your 3-Leg Parlay · live slip")
        self.assertIn("🚀 +10%", next(f.value for f in e.fields if "FanDuel" in f.name))

    def test_old_slip_buttons_still_work(self):
        from linemaker import bot
        legs = ParlayButtonTest._legs(ParlayButtonTest())
        key = bot._remember({"picks": [{"rank": i, "ev": ev} for i, ev in enumerate(legs, 1)]})

        class Resp:
            async def edit_message(self, **kw):
                self.edited = kw
        it = NS(data={"custom_id": f"lm:slip:{key}:1:0:t1"}, response=Resp())   # pre-boost format
        self.assertTrue(asyncio.run(bot.route_component(it)))
        self.assertEqual(it.response.edited["embed"].title, "🎰 Your 2-Leg Parlay · live slip")
