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


def more_buttons(view):
    """The buttons behind a card's 📂 More."""
    from linemaker import bot
    btn = next(c for c in view.children if str(getattr(c, "custom_id", "")).startswith("lm:more:"))

    async def build():
        return bot.more_view(bot.PICK_DETAILS[btn.custom_id.split(":")[2]]["more"])[1]
    return asyncio.run(build()).children


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
        self.assertTrue(any(getattr(c, "label", "") == "Notes & passes" for c in more_buttons(view)))
        self.assertLessEqual(len({c.row for c in view.children}), 3)       # menu + parlay menu + one button row
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
        ids = [getattr(c, "custom_id", "") for c in more_buttons(sent[0]["view"])]
        self.assertIn(f"lm:mkt:{key}", ids)                         # behind 📂 More
        sent.clear()
        asyncio.run(bot._send_card(send, messages))                # no key: no button
        self.assertFalse(any(str(getattr(c, "custom_id", "")).startswith("lm:mkt:")
                             for c in more_buttons(sent[0]["view"])))

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
        boxes = [e for e in embeds if (e.title or "").startswith("#")]
        self.assertTrue(boxes and boxes[0].title.startswith("#1 · "))            # numbered, one box each
        menu = next(c for c in view.children if str(getattr(c, "custom_id", "")).startswith("lm:pbet:"))
        self.assertTrue(menu.options[0].label.startswith("#1 · "))               # same numbers in the menu
        self.assertEqual(menu.row, 1)
        rows = {getattr(c, "label", None): c.row for c in view.children if getattr(c, "label", None)}
        self.assertEqual(rows.get("Safest by payout"), 4)                         # the one button row, last
        self.assertEqual(sorted(l for l, r in rows.items() if r == 4),
                         ["Build a parlay", "More", "Safest by payout"])
        self.assertFalse(any(str(getattr(c, "custom_id", "")).startswith("lm:betgo:") for c in view.children))
        self.assertIn("Odds & stats", [c.label for c in more_buttons(view)])
        self.assertTrue(any(e.title == "✅ High-Hit-Rate Legs" for e in embeds))     # the board stays
        build = next(c for c in view.children if getattr(c, "label", "") == "Build a parlay")
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
        self.assertTrue(any(getattr(c, "label", "") == "Build a parlay" for c in cview.children))
        self.assertEqual(embed.title, "🎰 Your 3-Leg Parlay")
        self.assertEqual([f.name.split(" · ")[0] for f in embed.fields], ["🟦 FanDuel", "🟩 DraftKings"])
        self.assertRegex(embed.fields[0].name, r"· \d+% hit")              # odds and hit chance up top
        self.assertIn("💵", embed.fields[0].value)
        self.assertIn("Not offered here", embed.fields[1].value)
        labels = [getattr(c, "label", "") for c in view.children]
        self.assertTrue(any(l.startswith("Bet on FanDuel") for l in labels))
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
                    if cid.startswith(("lm:more:", "lm:mkparlay:")):
                        await bot.route_component(Inter(cid))
                more = next(kw["view"] for kw in sent if kw.get("view") and any(
                    str(getattr(c, "custom_id", "")).startswith("lm:notes:") for c in kw["view"].children))
                for c in more.children:
                    if c.custom_id.startswith("lm:notes:"):
                        await bot.route_component(Inter(c.custom_id))
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
        self.assertTrue(any(str(getattr(c, "custom_id", "")).startswith("lm:betgo:") for c in view.children))


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
        btn = next(c for c in more_buttons(view) if getattr(c, "label", "") == "Moneyline parlay")
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
        self.assertTrue(any(c.label.startswith("Bet on FanDuel") for c in v.children))
        e, v = tap(btn(v, ":t2").custom_id)
        self.assertEqual(e.title, "🎰 Your 3-Leg Parlay · live slip")
        e, v = tap(btn(v, ":t0").custom_id)                          # tap again removes it
        self.assertEqual(e.title, "🎰 Your 2-Leg Parlay · live slip")
        self.assertFalse(btn(v, ":t0").label.startswith("✅"))
        boards = [c for c in v.children if ":sboard:" in (c.custom_id or "")]
        self.assertEqual([b.label for b in boards], ["Leg stats"])          # one button, every view
        self.assertFalse(any((c.custom_id or "").endswith(":all") and ":slip:" in c.custom_id for c in v.children))
        self.assertLessEqual(len({c.row for c in v.children}), 4)

        class R2:
            async def send_message(self, **kw):
                self.sent = kw
        it = NS(data={"custom_id": boards[0].custom_id}, response=R2())
        self.assertTrue(asyncio.run(bot.route_component(it)))
        sent = it.response.sent["embeds"]
        self.assertEqual(len(sent), 4)                                      # odds, hit rates, moves, stats
        self.assertEqual(sent[0].title, "🏷️ Odds, Your 2-Leg Slip")
        self.assertEqual(sent[0].description.count("\n"), 2 + 2)
        e, v = tap(btn(v, ":clr").custom_id)
        self.assertFalse(any(":sboard:" in (c.custom_id or "") for c in v.children))
        self.assertIn("Tap legs to add them", e.description)
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
        boards = [c for c in more_buttons(view) if ":board:" in (getattr(c, "custom_id", "") or "")]
        self.assertEqual([b.label for b in boards], ["Odds & stats"])
        it = NS(data={"custom_id": boards[0].custom_id}, response=Resp())
        self.assertTrue(asyncio.run(bot.route_component(it)))
        self.assertEqual(len(it.response.sent["embeds"]), 4)
        for embed in it.response.sent["embeds"]:
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


class OneBookTest(unittest.TestCase):
    def test_fanduel_only_hides_draftkings(self):
        from linemaker.pro import rows_to_markets, run_pro
        from linemaker.scan_format import BOARDS, _price_sentence, movement_page
        from dataclasses import replace
        rows = [{"date": "2026-10-21", "sport": "NBA", "game_id": f"g{i}", "game": f"A{i} @ H{i}",
                 "bet_type": "prop", "subject": f"Player {i}", "market": "points", "line": "15.5",
                 "mean": "24", "stdev": "4", "books": "FanDuel:-150/+120|DraftKings:-140/+115"}
                for i in range(3)]
        res = run_pro(rows_to_markets(rows), "2026-10-21",
                      replace(ProConfig(track=False), bet_books=("FanDuel",), min_gap=-1.0, min_ev=None))
        self.assertTrue(res.picks)
        for ev in res.evaluated:
            ev.shown_books = ("FanDuel",)
        ev = res.picks[0]
        self.assertIn("FanDuel", _price_sentence(ev))
        self.assertNotIn("DraftKings", _price_sentence(ev))
        board = BOARDS["odds"][2]([{"rank": 1, "ev": ev}]).description
        self.assertIn("FD", board)
        self.assertNotIn("DK", board)
        hist = [("2026-10-21T20:00:00+00:00", {ev.best.side: {"FanDuel": -150, "DraftKings": -140}}),
                ("2026-10-21T21:00:00+00:00", {ev.best.side: {"FanDuel": -160, "DraftKings": -145}})]
        self.assertNotIn("DraftKings", movement_page({"ev": ev, "history": hist}).description)
        ev.shown_books = None                                   # both books by default
        self.assertIn("DraftKings", movement_page({"ev": ev, "history": hist}).description)


class BetDropdownTest(unittest.TestCase):
    def _card(self, with_links=True):
        import json
        from linemaker import bot
        from linemaker.pro import ml_parlay_config, rows_to_markets, run_pro
        from linemaker.scan_format import volume_messages
        rows = []
        for i in range(8):
            f = -260 - i * 20
            links = {"FanDuel": [{"link": f"https://sportsbook.fanduel.com/addToBetslip?marketId[0]=m{i}&selectionId[0]=s{i}",
                                  "sid": f"s{i}", "msid": f"m{i}"}, None],
                     "DraftKings": [{"link": "", "sid": f"o{i}", "msid": ""}, None]} if with_links else {}
            rows.append({"date": "2026-10-25", "sport": "NFL", "game_id": f"g{i}", "game": f"A{i} @ H{i}",
                         "bet_type": "moneyline", "subject": f"H{i}", "opponent": f"A{i}", "market": "moneyline",
                         "line": "0", "mean": str(6 + i), "stdev": "13.5",
                         "books": f"FanDuel:{f}/+{int(-f * 0.8)}|DraftKings:{f - 5}/+{int(-f * 0.8) - 5}",
                         "links": json.dumps(links)})
        cfg = ml_parlay_config(track=False)
        res = run_pro(rows_to_markets(rows), "2026-10-25", cfg)
        self.assertGreaterEqual(len(res.picks), 4)
        msgs = volume_messages([("NFL", res, {"profiles": {}, "context": {}, "history": {}})], "2026-10-25", cfg)

        async def build():
            return bot.compact_card(msgs)
        return asyncio.run(build())

    def test_every_parlay_reachable_with_bet_buttons(self):
        from linemaker import bot
        _embeds, view = self._card()
        select = next(c for c in view.children if str(getattr(c, "custom_id", "")).startswith("lm:pbet:"))
        keys = {o.value.rsplit(":", 1)[0] for o in select.options}
        n = sum(len(bot.PICK_DETAILS[k]["parlays"]) for k in keys)
        self.assertEqual(len(select.options), n)                          # every parlay on the card
        self.assertGreaterEqual(n, 6)
        self.assertLessEqual(len({c.row for c in view.children}), 3)      # betting is in the parlay menu

        async def open_one():
            return bot.parlay_card([ev for ev, _o in bot.PICK_DETAILS[select.options[0].value.rsplit(":", 1)[0]]
                                    ["parlays"][0].legs], ("FanDuel",))
        _e, pview = asyncio.run(open_one())
        self.assertTrue(any(str(getattr(c, "custom_id", "")).startswith("lm:betgo:") for c in pview.children))

    def test_no_slip_ids_gives_open_book_button(self):
        from linemaker import bot
        _embeds, view = self._card(with_links=False)
        select = next(c for c in view.children if str(getattr(c, "custom_id", "")).startswith("lm:pbet:"))
        key = select.options[0].value.rsplit(":", 1)[0]

        async def open_one():
            p = bot.PICK_DETAILS[key]["parlays"][0]
            return bot.parlay_card([ev for ev, _o in p.legs], (p.book,))
        embed, pview = asyncio.run(open_one())
        self.assertTrue(any(str(getattr(c, "custom_id", "")).startswith("lm:betgo:") for c in pview.children))
        self.assertIn("no one-tap slip", embed.fields[0].value)


class OneLegVolumeTest(unittest.TestCase):
    def test_single_leg_gets_bet_dropdown_and_note(self):
        import json
        from linemaker import bot
        from linemaker.pro import rows_to_markets, run_pro, volume_config
        from linemaker.scan_format import volume_messages
        links = {"DraftKings": [None, {"link": "https://sportsbook.draftkings.com/event/1?outcomes=o1", "sid": "o1"}]}
        rows = [{"date": "2026-10-25", "sport": "NFL", "game_id": "g1", "game": "A @ H", "bet_type": "prop",
                 "subject": "Geno Smith", "market": "passing tds", "line": "2.5", "mean": "1.2", "stdev": "0.9",
                 "books": "DraftKings:+230/-280", "links": json.dumps(links)}]
        cfg = volume_config()
        cfg.min_prob, cfg.min_odds, cfg.max_odds, cfg.min_gap, cfg.track = 0.5, -100, -10000, -1.0, False
        res = run_pro(rows_to_markets(rows), "2026-10-25", cfg)
        self.assertEqual(len(res.picks), 1)
        msgs = volume_messages([("NFL", res, {"profiles": {}, "context": {}, "history": {}})], "2026-10-25", cfg)

        async def build():
            return bot.compact_card(msgs)
        embeds, view = asyncio.run(build())
        self.assertIn("no parlay", embeds[0].description)
        sel = next(c for c in view.children if getattr(c, "placeholder", "") and "Pick a leg" in c.placeholder)
        detail = bot.PICK_DETAILS[sel.options[0].value]

        async def open_leg():
            return bot.PickView(sel.options[0].value, detail)
        pv = asyncio.run(open_leg())
        self.assertTrue(any(getattr(c, "url", None) for c in pv.children))            # Bet on DraftKings
        self.assertTrue(any(getattr(c, "label", "") == "I bet this" for c in pv.children))


class NeverSilentTest(unittest.TestCase):
    def test_card_refused_still_posts(self):
        import discord
        from linemaker import bot
        from linemaker.scan_format import scan_messages
        res = PicksCardTest._result(None)
        messages = scan_messages([("NBA", res, {})], "2026-10-21", ProConfig(track=False))
        sent = []

        class Refused(discord.HTTPException):
            def __init__(self):
                Exception.__init__(self, "400 Bad Request: invalid component")
                self.status, self.text = 400, "invalid component"

        async def send(**kw):
            if kw.get("view") is not None:
                raise Refused()
            sent.append(kw)
        asyncio.run(bot._send_card(send, messages))
        self.assertTrue(sent and "refused" in (sent[0]["content"] or ""))
        self.assertTrue(sent[0]["embeds"])


class CardSizeTest(unittest.TestCase):
    def _messages(self):
        from linemaker.pro import rows_to_markets, run_pro, volume_config
        from linemaker.scan_format import volume_messages
        rows = [{"date": "2026-10-21", "sport": "NBA", "game_id": f"g{i}", "game": f"A{i} @ H{i}",
                 "bet_type": "prop", "subject": f"A Fairly Long Player Name {i}", "market": "pts + reb + ast",
                 "line": "15.5", "mean": str(24 - i * 0.2), "stdev": "4",
                 "books": "FanDuel:-400/+300|DraftKings:-380/+290"} for i in range(25)]
        cfg = volume_config()
        cfg.min_prob, cfg.min_odds, cfg.max_odds, cfg.min_gap, cfg.track = 0.5, -100, -10000, -1.0, False
        res = run_pro(rows_to_markets(rows), "2026-10-21", cfg)
        return volume_messages([("NBA", res, {"profiles": {}, "context": {}, "history": {}})], "2026-10-21", cfg)

    def test_boxes_and_buttons_fit_together(self):
        from linemaker import bot

        async def build():
            return bot.compact_card(self._messages())
        embeds, view = asyncio.run(build())
        self.assertLessEqual(total(embeds) + bot._view_text(view), 5800)

    def test_too_large_retries_slimmer_with_buttons(self):
        import discord
        from linemaker import bot
        sent, calls = [], [0]

        class TooLarge(discord.HTTPException):
            def __init__(self):
                Exception.__init__(self, "413 Payload Too Large (error code: 40005)")
                self.status, self.text = 413, "too large"

        async def send(**kw):
            calls[0] += 1
            if calls[0] == 1:
                raise TooLarge()
            sent.append(kw)
        asyncio.run(bot._send_card(send, self._messages()))
        self.assertEqual(len(sent), 1)
        self.assertIsNotNone(sent[0].get("view"))                     # buttons kept on the retry
        self.assertLessEqual(sum(1 for e in sent[0]["embeds"] if (e.title or "").startswith("#")), 3)


class AutoLogTest(unittest.TestCase):
    def test_bet_logs_once_then_undo(self):
        import tempfile
        from types import SimpleNamespace
        from linemaker import bets, bot, parlay
        from tests.test_parlay_opt import leg
        a, b = leg("g1", "A", "points", p=0.7, odds=-200, key="k1"), leg("g2", "B", "points", p=0.7, odds=-200, key="k2")
        p = parlay.price("FanDuel", [(a, -200), (b, -200)])
        key = bot._remember({"parlays": [p]})
        replies = []

        class Resp:
            async def send_message(self, text, view=None, ephemeral=False):
                replies.append((text, view))

        inter = SimpleNamespace(user="me", response=Resp())
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"LINEMAKER_TRACKING_DIR": d}):
            asyncio.run(bot._bet_go(key, 0)(inter))
            asyncio.run(bot._bet_go(key, 0)(inter))                    # second tap: no duplicate
            logged = bets.load_bets()
            self.assertEqual(len(logged), 1)
            self.assertTrue(logged[0]["auto"])
            self.assertIn("Logged", replies[0][0])
            self.assertIn("Already logged", replies[1][0])
            ids = [c.custom_id for c in replies[0][1].children if getattr(c, "custom_id", None)]
            self.assertIn(f"lm:unlog:{logged[0]['id']}", ids)
            asyncio.run(bot._unlog(logged[0]["id"])(inter))
            self.assertEqual(bets.load_bets(), [])


class NoParlayTest(unittest.TestCase):
    def test_reported_market_stays_out_of_that_books_parlays(self):
        import tempfile
        from types import SimpleNamespace
        from linemaker import bets, bot, parlay
        from tests.test_parlay_opt import leg

        def dk_leg(game, subject, market, mk):
            ev = leg(game, subject, market, p=0.72, odds=-200, key=f"{game}|{subject}|{market}")
            ev.market.books = [("FanDuel", [-200, 160]), ("DraftKings", [-205, 165])]
            ev.market.links = {"DraftKings": [{"link": "", "sid": f"s-{subject}", "msid": "", "mk": mk}, None]}
            return ev
        steals = dk_leg("g1", "Mikal Bridges", "steals", "player_steals")
        points = dk_leg("g2", "Scoot Henderson", "points", "player_points_alternate")
        other = dk_leg("g3", "Other Guy", "rebounds", "player_rebounds")
        replies = []

        class Resp:
            async def send_message(self, text, view=None, ephemeral=False):
                replies.append((text, view))
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {
                "LINEMAKER_TRACKING_DIR": d, "LINEMAKER_CACHE_DIR": d, "LINEMAKER_NO_PARLAY": ""}):
            p = parlay.price("DraftKings", [(steals, -205), (points, -205)])
            key = bot._remember({"parlays": [p]})
            inter = SimpleNamespace(user="me", response=Resp(), data={})
            asyncio.run(bot._bet_go(key, 0)(inter))
            self.assertEqual(len(bets.load_bets()), 1)
            nopar = next(c for c in replies[0][1].children if str(getattr(c, "custom_id", "")).startswith("lm:nopar:"))
            asyncio.run(bot.route_component(SimpleNamespace(data={"custom_id": nopar.custom_id}, response=Resp())))
            menu = replies[-1][1].children[0]
            self.assertEqual([o.label for o in menu.options][-1], "Not sure which one")
            asyncio.run(bot.route_component(SimpleNamespace(
                data={"custom_id": menu.custom_id, "values": ["0"]}, response=Resp())))
            self.assertIn("won't parlay **NBA steals**", replies[-1][0])
            self.assertEqual(bets.load_bets(), [])                              # the logged bet is gone
            self.assertEqual(parlay.no_parlay_markets(), {"draftkings": {"NBA|player_steals"}})
            built = parlay.build_parlays([steals, points, other], sizes=(2,), books=("DraftKings",), value=False)
            self.assertTrue(built and all(steals not in [ev for ev, _o in b.legs] for b in built))
            self.assertTrue(parlay.sgp_check([steals, points], "DraftKings")["blocked"])
            self.assertFalse(parlay.sgp_check([steals, points], "FanDuel")["blocked"])   # FanDuel unaffected
            self.assertFalse(parlay.sgp_check([steals], "DraftKings")["blocked"])        # straight bet fine
