import unittest
from unittest import mock

from linemaker import ufc_props


def mk(series, code, sub, bid, ask, ticker_tail=""):
    return {"event_ticker": f"{series}-{code}", "ticker": f"{series}-{code}-{ticker_tail or sub[:3].upper()}",
            "yes_sub_title": sub, "yes_bid_dollars": str(bid), "yes_ask_dollars": str(ask)}


FEED = {
    "KXUFCFIGHT": [mk("KXUFCFIGHT", "26NOV14GANHOK", "Ciryl Gane", 0.66, 0.67),
                   mk("KXUFCFIGHT", "26NOV14GANHOK", "Josh Hokit", 0.33, 0.34)],
    "KXUFCDISTANCE": [mk("KXUFCDISTANCE", "26NOV14GANHOK", "Fight goes the distance", 0.28, 0.29, "DIST")],
    "KXUFCMOV": [mk("KXUFCMOV", "26NOV14GANHOK", "Josh Hokit by KO/TKO/DQ", 0.18, 0.19),
                 mk("KXUFCMOV", "26NOV14GANHOK", "Josh Hokit by Submission", 0.07, 0.08),
                 mk("KXUFCMOV", "26NOV14GANHOK", "Josh Hokit by Decision", 0.09, 0.10),
                 mk("KXUFCMOV", "26NOV14GANHOK", "Ciryl Gane by KO/TKO/DQ", 0.42, 0.43),
                 mk("KXUFCMOV", "26NOV14GANHOK", "Ciryl Gane by Submission", 0.06, 0.07),
                 mk("KXUFCMOV", "26NOV14GANHOK", "Ciryl Gane by Decision", 0.17, 0.18),
                 mk("KXUFCMOV", "26NOV14GANHOK", "Draw/No Contest", 0.01, 0.02, "DRAW")],
    "KXUFCROUNDS": [mk("KXUFCROUNDS", "26NOV14GANHOK", "Fight ends before round 2", 0.27, 0.28, "2"),
                    mk("KXUFCROUNDS", "26NOV14GANHOK", "Fight ends before round 6", 0.9, 0.95, "6")],
}
FIGHTER = {"wins": 12, "losses": 2, "finishes": 8, "kos": 6, "subs": 2, "td": 1.0, "sub": 0.5,
           "reach": 76, "age": 30, "form": [1, 1, -1]}
CARD = [("Josh Hokit", "1", "Ciryl Gane", "2", {"format": {"regulation": {"periods": 5}}})]


class KalshiParseTest(unittest.TestCase):
    def test_groups_series_by_fight(self):
        k = ufc_props.kalshi_fights(fetch=lambda s: FEED.get(s, []))
        f = k["26NOV14GANHOK"]
        self.assertEqual(sorted(f["names"]), ["Ciryl Gane", "Josh Hokit"])
        self.assertAlmostEqual(f["distance"]["mid"], 0.285)
        self.assertAlmostEqual(f["mov"][("Josh Hokit", "KO")]["ask"], 0.19)
        self.assertIn("draw", f)
        self.assertEqual(sorted(f["rounds"]), [2, 6])

    def test_name_matching(self):
        self.assertTrue(ufc_props.same_fighter("Alatengheili", "Alateng Heili"))
        self.assertTrue(ufc_props.same_fighter("Bia Mesquita", "Beatriz Mesquita"))
        self.assertTrue(ufc_props.same_fighter("Heili Alateng", "Alatengheili"))
        self.assertTrue(ufc_props.same_fighter("Heili Alateng", "Alateng Heili"))
        self.assertFalse(ufc_props.same_fighter("Josh Hokit", "Ciryl Gane"))


class ModelTest(unittest.TestCase):
    def test_outcomes_add_up_to_one(self):
        m = ufc_props.model_outcomes(FIGHTER, dict(FIGHTER, finishes=1, kos=0, subs=1), 0.6)
        self.assertAlmostEqual(sum(m.values()), 1.0)
        self.assertGreater(m[("a", "KO")], m[("b", "KO")])

    def test_recentering_moves_the_card_to_the_market_level(self):
        shift = ufc_props.recenter_finish([0.2, 0.3], [0.4, 0.5])
        self.assertGreater(shift, 0)
        m = ufc_props.model_outcomes(FIGHTER, FIGHTER, 0.5)
        new = ufc_props._apply_shift(m, shift)
        self.assertAlmostEqual(sum(new.values()), 1.0)
        self.assertAlmostEqual(new[("a", "KO")] + new[("a", "SUB")] + new[("a", "DEC")], 0.5)
        self.assertGreater(new[("a", "DEC")], m[("a", "DEC")])

    def test_fight_props_blend_and_sides(self):
        k = ufc_props.kalshi_fights(fetch=lambda s: FEED.get(s, []))
        priced = ufc_props.fight_props(CARD, {"1": FIGHTER, "2": FIGHTER}, k)
        self.assertEqual(len(priced), 1)
        labels = [p["label"] for p in priced[0]["props"]]
        self.assertTrue(any("distance" in l for l in labels))
        self.assertFalse(any("round 6" in l for l in labels))           # beyond the scheduled 5
        for p in priced[0]["props"]:
            self.assertGreaterEqual(p["chance"], p["market"] - 1e-9)    # always the side we like
            self.assertTrue(0 < p["kalshi_price"] < 1)

    def test_prices_text(self):
        self.assertEqual(ufc_props.american(0.75), "-300")
        self.assertEqual(ufc_props.american(0.25), "+300")
        self.assertEqual(ufc_props.worth_at(0.5), "+104")


class ReplyTest(unittest.TestCase):
    def test_typed_ask_routes_to_fight_props(self):
        from linemaker.chat import UFC_PROPS_WORDS
        for t in ("ufc props", "fight props tonight", "method of victory ufc", "mma ko props", "goes the distance ufc"):
            self.assertTrue(UFC_PROPS_WORDS.search(t), t)
        for t in ("ufc picks", "ufc ml parlay", "best nba props", "knicks props", "ufc total rounds"):
            self.assertFalse(UFC_PROPS_WORDS.search(t), t)

    def test_reply_when_nothing_priced(self):
        with mock.patch("linemaker.ufc_model.next_card_date", return_value="2026-11-14"), \
                mock.patch.object(ufc_props, "props_lines", return_value=([], [])):
            self.assertIn("No UFC fight props", ufc_props.props_reply("2026-11-14"))
        with mock.patch("linemaker.ufc_model.next_card_date", return_value="2026-11-14"), \
                mock.patch.object(ufc_props, "props_lines", return_value=(["**X** · A vs B"], [{}])):
            text = ufc_props.props_reply("2026-11-14")
        self.assertIn("vs Kalshi", text)
        self.assertIn("**X**", text)


class StatPropTest(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(ufc_props.stat_prop_ask("hokit over 45.5 sig strikes"),
                         {"over": True, "line": 45.5, "stat": "significant strikes", "odds": None})
        self.assertEqual(ufc_props.stat_prop_ask("hokit over 45.5 sig strikes -130")["odds"], -130)
        self.assertAlmostEqual(ufc_props.line_chance(-130), 130 / 230 - 0.0225)
        self.assertEqual(ufc_props.line_chance(None), 0.5)
        self.assertEqual(ufc_props.stat_prop_ask("gane u 1.5 takedowns")["stat"], "takedowns")
        self.assertEqual(ufc_props.stat_prop_ask("nunes under 9.5 fight time")["stat"], "fight time")
        for t in ("mahomes over 1.5 tds", "brunson over 34.5 minutes", "lebron over 25.5 points"):
            self.assertIsNone(ufc_props.stat_prop_ask(t), t)

    def test_quick_finishes_pull_overs_down(self):
        row = {"fighter": "A", "opponent": "B", "rounds": 3, "early": 0.7, "minutes": 9.0,
               "sig_strikes": 40.0, "takedowns": 1.0}
        over = ufc_props.price_stat_prop(row, "significant strikes", 39.5, True)
        under = ufc_props.price_stat_prop(row, "significant strikes", 39.5, False)
        self.assertAlmostEqual(over + under, 1.0, places=6)
        self.assertLess(over, 0.5)              # finishes cut the count short: median below the mean
        calm = dict(row, early=0.1, fighter="C")
        self.assertGreater(ufc_props.price_stat_prop(calm, "significant strikes", 39.5, True), over)

    def test_reply_names_the_fighter(self):
        rows = [{"fighter": "Josh Hokit", "opponent": "Ciryl Gane", "rounds": 5, "early": 0.6, "minutes": 14.0,
                 "sig_strikes": 60.0, "takedowns": 2.0},
                {"fighter": "Ciryl Gane", "opponent": "Josh Hokit", "rounds": 5, "early": 0.6, "minutes": 14.0,
                 "sig_strikes": 50.0, "takedowns": 0.5}]
        with mock.patch("linemaker.ufc_model.next_card_date", return_value="2026-11-14"), \
                mock.patch("linemaker.ufc_model.fighter_props", return_value=rows):
            text = ufc_props.stat_prop_reply("2026-11-14", "hokit over 20.5 sig strikes")
            self.assertIn("Josh Hokit Over 20.5 significant strikes", text)
            self.assertIn("worth it at", text)
            self.assertIn("the line counts 70%", text)
            self.assertIn("few fights on record", text)
            self.assertIn("far from the book's line", text)      # projects 60 on a 20.5 line
            priced = ufc_props.stat_prop_reply("2026-11-14", "hokit over 20.5 sig strikes -400")
            self.assertIn("-400", priced)
            self.assertIn("Name one fighter", ufc_props.stat_prop_reply("2026-11-14", "over 45.5 sig strikes"))
        self.assertIsNone(ufc_props.stat_prop_reply("2026-11-14", "ufc props"))


class LiveAndPlanTest(unittest.TestCase):
    def test_started_fights_and_dead_prices_are_skipped(self):
        from linemaker.ufc_model import not_started
        self.assertTrue(not_started({"status": {"type": {"state": "pre"}}}))
        self.assertFalse(not_started({"status": {"type": {"state": "in"}}}))
        self.assertFalse(not_started({"status": {"type": {"completed": True}}}))
        self.assertIsNone(ufc_props._prop("Ends before round 2", 0.3, 0.005, {"ask": 0.01, "bid": 0.0}))
        self.assertIsNone(ufc_props._prop("Goes the distance", 0.9, 0.99, {"ask": 1.0, "bid": 0.98}))
        self.assertIsNotNone(ufc_props._prop("Goes the distance", 0.5, 0.4, {"ask": 0.41, "bid": 0.39}))


ROWS = [{"fighter": "Juan Diaz", "opponent": "Alatengheili", "rounds": 3, "early": 0.5, "minutes": 10.7,
         "sig_strikes": 40.0, "takedowns": 3.3, "stat_fights": 18},
        {"fighter": "Alatengheili", "opponent": "Juan Diaz", "rounds": 3, "early": 0.5, "minutes": 10.7,
         "sig_strikes": 26.9, "takedowns": 1.3, "stat_fights": 30}]


class ScreenshotTest(unittest.TestCase):
    def _reply(self, data):
        with mock.patch("linemaker.ufc_model.next_card_date", return_value="2026-11-14"), \
                mock.patch("linemaker.ufc_model.fighter_props", return_value=ROWS):
            return ufc_props.screenshot_reply("2026-11-14", data)

    def test_prices_ladders_overs_and_rounds(self):
        text = self._reply({"sport": "UFC", "lines": [
            {"fighter": "Juan Diaz", "stat": "significant strikes", "side": "at_least", "line": 65, "odds": 215},
            {"fighter": "Juan Diaz", "stat": "takedowns", "side": "at_least", "line": 4, "odds": 250},
            {"fighter": "Heili Alateng", "stat": "significant strikes", "side": "over", "line": 24.5, "odds": -115},
            {"fighter": "Juan Diaz", "stat": "total rounds", "side": "over", "line": 2.5, "odds": -125},
            {"fighter": "Juan Diaz", "stat": "other", "side": "over", "line": 0, "odds": -300}]})
        self.assertIn("Read 5 lines", text)
        self.assertIn("Juan Diaz 65+ sig strikes** +215", text)
        self.assertIn("Alatengheili Over 24.5 sig strikes", text)     # "Heili Alateng" matched
        self.assertIn("Over 2.5 total rounds** -125", text)
        self.assertIn("Skipped: Juan Diaz (not a strike", text)
        self.assertIn("check the numbers", text)

    def test_other_sports_and_empty(self):
        self.assertIn("UFC", self._reply({"sport": "other", "lines": []}))
        self.assertIn("couldn't read any lines", self._reply({"sport": "UFC", "lines": []}))

    def test_ladder_margin_is_removed(self):
        # a "50+ +215" ladder price implies 31.7%; with the 15% margin removed the market reads ~27.6%
        self.assertAlmostEqual(ufc_props._implied(215) / 1.15, 100 / 315 / 1.15)

    def test_chat_reads_with_claude_structured_output(self):
        import asyncio
        import json
        from types import SimpleNamespace as NS
        from linemaker import chat
        bot = chat.LinemakerChat.__new__(chat.LinemakerChat)
        bot.provider = "claude"
        sent = {}

        async def create(**kw):
            sent.update(kw)
            return NS(stop_reason="end_turn", content=[NS(type="text", text=json.dumps(
                {"sport": "UFC", "lines": [{"fighter": "Juan Diaz", "stat": "takedowns", "side": "at_least",
                                             "line": 4, "odds": 250}]}))])
        bot.client = NS(beta=NS(messages=NS(create=create)))
        with mock.patch("linemaker.ufc_model.next_card_date", return_value="2026-11-14"), \
                mock.patch("linemaker.ufc_model.fighter_props", return_value=ROWS):
            text = asyncio.run(bot.read_screenshot([(b"\x89PNG", "image/png")], ""))
        self.assertIn("Juan Diaz 4+ takedowns", text)
        self.assertEqual(sent["messages"][0]["content"][0]["type"], "image")
        self.assertEqual(sent["output_config"]["format"]["type"], "json_schema")

    def test_chat_read_failure_is_friendly(self):
        import asyncio
        from types import SimpleNamespace as NS
        from linemaker import chat
        bot = chat.LinemakerChat.__new__(chat.LinemakerChat)
        bot.provider = "claude"

        async def create(**kw):
            raise RuntimeError("boom")
        bot.client = NS(beta=NS(messages=NS(create=create)))
        self.assertIn("couldn't read that screenshot", asyncio.run(bot.read_screenshot([(b"x", "image/png")])))
