import asyncio
import unittest
from unittest import mock

from linemaker import parlay
from linemaker.chat import parse_boost, quick_mode, quick_plays_request
from linemaker.parlay import BoostSpec
from tests.test_parlay_opt import leg


class ParseBoostTest(unittest.TestCase):
    def test_rules(self):
        s = parse_boost("cross sport 30% boost +300 min odds 3+ legs max $25 wager")
        self.assertEqual((s.boost, s.min_total, s.min_legs, s.max_wager), (0.3, 300, 3, 25.0))
        s = parse_boost("50% nba boost each leg -200 or longer, $10 wager")
        self.assertEqual((s.boost, s.min_leg, s.min_total, s.min_wager, s.max_wager), (0.5, -200, None, 10.0, 10.0))
        s = parse_boost("30% boost legs at -200 or longer, total +150")
        self.assertEqual((s.min_leg, s.min_total), (-200, 150))
        self.assertIsNone(parse_boost("best nfl props"))

    def test_routes_to_quick_path(self):
        self.assertEqual(quick_plays_request("30% nfl boost"), ["NFL"])
        self.assertEqual(quick_plays_request("i have a 50% nba boost"), ["NBA"])
        self.assertEqual(quick_mode("nba boost 50%"), "volume")


class BestBoostedTest(unittest.TestCase):
    def setUp(self):
        self.legs = ([leg(f"g{i}", f"P{i}", "points", p=0.70, odds=-200) for i in range(3)]
                     + [leg(f"h{i}", f"Q{i}", "points", p=0.56, odds=+100) for i in range(3)])
        patch = mock.patch.object(parlay, "parlay_leg_price", side_effect=lambda ev, b: ev.best.best_odds)
        patch.start()
        self.addCleanup(patch.stop)
        cal = mock.patch("linemaker.autotune.parlay_calibrated", side_effect=lambda p, *a: p)
        cal.start()
        self.addCleanup(cal.stop)

    def test_respects_rules(self):
        spec = BoostSpec(boost=0.5, min_total=300, min_legs=3)
        p = parlay.best_boosted(self.legs, "FanDuel", spec)
        self.assertGreaterEqual(len(p.legs), 3)
        self.assertGreaterEqual(p.listed_decimal, 4.0)
        self.assertAlmostEqual(p.decimal, 1 + (p.listed_decimal - 1) * 1.5)
        spec = BoostSpec(boost=0.5, min_leg=-150)
        p = parlay.best_boosted(self.legs, "FanDuel", spec)
        self.assertTrue(all(o >= -150 for _e, o in p.legs))

    def test_safest_is_likelier(self):
        spec = BoostSpec(boost=0.5)
        top = parlay.best_boosted(self.legs, "FanDuel", spec)
        safe = parlay.best_boosted(self.legs, "FanDuel", spec, safest=True)
        self.assertGreaterEqual(safe.prob, top.prob)
        self.assertGreaterEqual(safe.prob * safe.decimal, 1.0)
        self.assertGreaterEqual(top.prob * top.decimal, safe.prob * safe.decimal - 1e-9)

    def test_stake_inside_wager_limits(self):
        from linemaker.staking import parlay_stake
        spec = BoostSpec(boost=0.5, min_wager=10, max_wager=25)
        p = parlay.best_boosted(self.legs, "FanDuel", spec)
        with mock.patch("linemaker.staking.suggested_dollars", return_value=3.0):
            self.assertEqual(parlay_stake(p), 10)
        with mock.patch("linemaker.staking.suggested_dollars", return_value=80.0):
            self.assertEqual(parlay_stake(p), 25)


class BoostCardTest(unittest.TestCase):
    def test_card_builds(self):
        from linemaker import bot
        from linemaker.pro import rows_to_markets, run_pro, volume_config
        from linemaker.scan_format import boost_messages
        rows = [{"date": "2026-10-21", "sport": "NBA", "game_id": f"g{i}", "game": f"A{i} @ H{i}",
                 "bet_type": "prop", "subject": f"Player {i}", "market": "points", "line": "15.5",
                 "mean": "20", "stdev": "4", "books": "FanDuel:-200/+160|DraftKings:-190/+150"}
                for i in range(6)]
        cfg = volume_config()
        cfg.min_prob, cfg.min_odds, cfg.max_odds, cfg.min_gap, cfg.track = 0.5, -100, -10000, -1.0, False
        res = run_pro(rows_to_markets(rows), "2026-10-21", cfg)
        spec = BoostSpec(boost=0.5, min_total=200, min_legs=2)
        with mock.patch("linemaker.bets.open_exposure", return_value={}):
            msgs = boost_messages([("NBA", res, {"profiles": {}, "context": {}, "history": {}})],
                                  "2026-10-21", cfg, spec)
        boxes = [d for _e, d in msgs if d]
        self.assertTrue(boxes)
        p = boxes[0]["parlays"][0]
        self.assertEqual(p.style, "boost")
        self.assertGreaterEqual(p.listed_decimal, 3.0)

        async def build():
            return bot.compact_card(msgs)
        embeds, view = asyncio.run(build())
        self.assertTrue(any("Boost" in (e.title or "") for e in embeds))


class SgpBoostTest(unittest.TestCase):
    def setUp(self):
        for target, kw in (("linemaker.parlay.parlay_leg_price", {"side_effect": lambda ev, b: ev.best.best_odds}),
                           ("linemaker.autotune.parlay_calibrated", {"side_effect": lambda p, *a: p}),
                           ("linemaker.parlay_math.team_of", {"return_value": None})):
            pt = mock.patch(target, **kw)
            pt.start()
            self.addCleanup(pt.stop)
        self.legs = ([leg("g1", f"A{i}", "points", p=0.62, odds=-140) for i in range(4)]
                     + [leg("g2", f"B{i}", "rebounds", p=0.6, odds=-130) for i in range(3)]
                     + [leg(f"h{i}", f"C{i}", "assists", p=0.64, odds=-150) for i in range(3)])

    def test_sgp_is_one_game(self):
        p = parlay.best_boosted(self.legs, "DraftKings", BoostSpec(boost=0.3, kind="sgp", min_legs=3))
        self.assertEqual(len({ev.market.game_id for ev, _o in p.legs}), 1)
        self.assertGreaterEqual(len(p.legs), 3)
        self.assertEqual(p.kind, "SGP")

    def test_sgpx_has_a_group_and_two_games(self):
        p = parlay.best_boosted(self.legs, "DraftKings", BoostSpec(boost=0.3, kind="sgpx", min_legs=3))
        games = [ev.market.game_id for ev, _o in p.legs]
        self.assertGreaterEqual(len(set(games)), 2)
        self.assertTrue(any(games.count(g) >= 2 for g in games))
        self.assertEqual(p.kind, "SGPx")
