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


class GradeRobustnessTest(unittest.TestCase):
    def test_big_grade_file_is_zipped(self):
        from linemaker import bot
        small, name = bot.grade_file_bytes("a,b\n1,2\n", "2026-11-01")
        self.assertEqual(name, "graded_2026-11-01.csv")
        big = "col1,col2\n" + "some player o15.5 points,0.61\n" * 400000
        data, name = bot.grade_file_bytes(big, "2026-11-01")
        self.assertEqual(name, "graded_2026-11-01.zip")
        self.assertLess(len(data), bot.GRADE_FILE_LIMIT)

    def test_catch_up_day(self):
        import datetime as dt
        from linemaker import bot
        now = dt.datetime(2026, 11, 2, 10, 15)
        with mock.patch.object(bot, "grade_state", return_value={}):
            self.assertEqual(bot.grade_catch_up_day(now), "2026-11-01")
            self.assertIsNone(bot.grade_catch_up_day(dt.datetime(2026, 11, 2, 8, 0)))
        with mock.patch.object(bot, "grade_state", return_value={"day": "2026-11-01", "posted": True}):
            self.assertIsNone(bot.grade_catch_up_day(now))
        with mock.patch.object(bot, "grade_state", return_value={"day": "2026-11-01", "attempts": 2}):
            self.assertIsNone(bot.grade_catch_up_day(now))

    def test_lean_load(self):
        import json, os, tempfile
        from linemaker.tracking import load_day, load_records
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "NBA-props"))
            recs = {"k1": {"history": [{"t": str(i)} for i in range(10)], "result": {"winner": "A"},
                           "pick_time": "x", "books": {}},
                    "k2": {"history": []}}
            with open(os.path.join(d, "NBA-props", "2026-11-01.json"), "w") as f:
                json.dump(recs, f)
            day = load_day(d, "2026-11-01", lean=True)
            self.assertEqual([h["t"] for h in day["k1"]["history"]], ["0", "8", "9"])
            got = load_records(d, 5, "2026-11-02", graded_only=True, lean=True)
            self.assertEqual([r["key"] for r in got], ["k1"])
            self.assertEqual(len(got[0]["history"]), 3)
            self.assertNotIn("books", got[0])


class ParseDayTest(unittest.TestCase):
    def test_formats(self):
        import datetime as dt
        from linemaker import bot
        with mock.patch.object(bot.dt, "datetime", wraps=dt.datetime) as fake:
            fake.now.return_value = dt.datetime(2026, 11, 2, 15, 0)
            for raw in ("2026-10-31", "2026-10/31", "10/31", "10-31", "10/31/2026", "10/31/26"):
                self.assertEqual(bot.parse_day(raw), "2026-10-31", raw)
        with self.assertRaises(ValueError):
            bot.parse_day("yesterday")


class PickShrinkPerSportTest(unittest.TestCase):
    def test_nfl_gets_its_own_k(self):
        from linemaker import autotune
        recs = []
        for i in range(400):   # NBA: said 70, market 54, hit 60 -> real edge
            recs.append({"picked": True, "sport": "NBA", "pick_side": "A", "pick_prob": 0.70,
                         "pick_market_prob": 0.54, "pick_time": f"a{i:04d}",
                         "result": {"winner": "A" if i % 10 < 6 else "B"}})
        for i in range(160):   # NFL: said 82, market 53, hit 53 -> no edge
            recs.append({"picked": True, "sport": "NFL", "pick_side": "A", "pick_prob": 0.82,
                         "pick_market_prob": 0.53, "pick_time": f"b{i:04d}",
                         "result": {"winner": "A" if i < 85 else "B"}})
        fit = autotune.tune_pick_shrink(recs)
        self.assertIn("NFL", fit["sports"])
        self.assertLess(fit["sports"]["NFL"]["k"], fit["k"])
        self.assertGreater(fit["sports"]["NBA"]["k"], fit["sports"]["NFL"]["k"])
        with mock.patch.object(autotune, "segment", return_value={"pick_shrink": fit}), \
                mock.patch.dict("os.environ", {"LINEMAKER_PICK_SHRINK": ""}):
            self.assertEqual(autotune.pick_shrink("pre", "prop", "NFL"), fit["sports"]["NFL"]["k"])
            self.assertEqual(autotune.pick_shrink("pre", "prop", "WNBA"), fit["k"])

    def test_longest_lines_never_picked(self):
        from linemaker import autotune
        with mock.patch.dict("os.environ", {}, clear=False):
            self.assertTrue(autotune.pruned("NFL", "longest reception"))
            self.assertTrue(autotune.pruned("NFL", "Longest Rush"))
        with mock.patch.dict("os.environ", {"LINEMAKER_NO_PICK_MARKETS": ""}):
            with mock.patch.object(autotune, "segment", return_value={}):
                self.assertFalse(autotune.pruned("NFL", "longest reception"))


class PerUserBetsTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict("os.environ", {"LINEMAKER_PER_USER_BETS": "on", "LINEMAKER_BETS_DIR": self.tmp.name,
                                             "LINEMAKER_CACHE_DIR": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)

    def _leg(self, player):
        return {"key": f"NBA|g1|{player}|points|10.5", "side": "A", "label": f"{player} o10.5",
                "bet_type": "prop", "odds": -110}

    def test_each_person_has_their_own_open_bets(self):
        from linemaker import bets
        with mock.patch.object(bets, "bets_path", return_value=self.tmp.name + "/bets.json"):
            bets.add_bet([self._leg("A"), self._leg("B")], 250, 1, "FanDuel", "me")
            bets.add_bet([self._leg("C"), self._leg("D")], 250, 1, "FanDuel", "friend")
            self.assertEqual({k[1] for k in bets.open_exposure("me")}, {"A", "B"})
            self.assertEqual({k[1] for k in bets.open_exposure("friend")}, {"C", "D"})
            tok = bets.CURRENT_USER.set("friend")
            try:
                self.assertEqual({k[1] for k in bets.open_exposure()}, {"C", "D"})   # whoever tapped
                self.assertIsNone(bets.find_open("FanDuel", [self._leg("A"), self._leg("B")]))   # not hers
            finally:
                bets.CURRENT_USER.reset(tok)
            self.assertEqual(bets.open_exposure(), {})   # no one asking (the 11 AM post): nothing held
            self.assertEqual(len(bets.summary(None, user="me")["open"]), 1)
            self.assertEqual(len(bets.summary(None)["open"]), 2)

    def test_board_copy_for_someone_else(self):
        from linemaker import bot

        async def go():
            key = bot._remember({"parlays": [], "numbers": [], "pool": [], "owner": "me"})
            same = await bot._board_for(key, "me")
            other = await bot._board_for(key, "friend")
            again = await bot._board_for(key, "friend")
            return key, same, other, again
        key, same, other, again = asyncio.run(go())
        self.assertEqual(same, key)
        self.assertNotEqual(other, key)
        self.assertEqual(other, again)
        self.assertNotIn("card_msg", bot.PICK_DETAILS[other])


class PerUserGuardsTest(unittest.TestCase):
    def test_only_the_bettor_changes_a_bet(self):
        from types import SimpleNamespace as NS
        from linemaker import bot
        sent = []

        class Resp:
            async def send_message(self, msg, **kw):
                sent.append(msg)

        async def go(user):
            inter = NS(user=user, response=Resp())
            with mock.patch("linemaker.admin.is_owner", return_value=False):
                return await bot._own_bet(inter, {"user": "me"})
        with mock.patch.dict("os.environ", {"LINEMAKER_PER_USER_BETS": "on"}):
            async def owner_false(*a):
                return False
            with mock.patch("linemaker.admin.is_owner", side_effect=owner_false):
                self.assertTrue(asyncio.run(bot._own_bet(NS(user="me", response=Resp()), {"user": "me"})))
                self.assertFalse(asyncio.run(bot._own_bet(NS(user="friend", response=Resp()), {"user": "me"})))
        self.assertIn("me's bet", sent[-1])

    def test_bankroll_counts_only_the_owners_bets(self):
        from linemaker import bets
        with mock.patch.dict("os.environ", {"LINEMAKER_PER_USER_BETS": "on"}), \
                mock.patch.object(bets, "OWNER_NAMES", {"me"}):
            self.assertTrue(bets.owners_bet({"user": "me"}))
            self.assertFalse(bets.owners_bet({"user": "friend"}))


class UnderdogTest(unittest.TestCase):
    def test_asks(self):
        from linemaker.chat import quick_kind, quick_plays_request
        self.assertEqual(quick_kind("underdog nba"), "ud")
        self.assertEqual(quick_kind("best underdog fantasy picks nfl"), "ud")
        self.assertEqual(quick_kind("prizepicks nba"), "pp")
        self.assertNotEqual(quick_kind("underdog ml parlay nba"), "ud")   # plus-money moneylines
        self.assertEqual(quick_plays_request("underdog nba"), ["NBA"])

    def test_tables_and_names(self):
        from linemaker import pp
        with pp.use_app("ud"):
            self.assertEqual(pp.book(), "Underdog")
            self.assertEqual(pp.power_table()[3], 6.0)
            self.assertEqual(pp.entry_name("Power"), "Standard")
        self.assertEqual(pp.book(), "PrizePicks")
        self.assertEqual(pp.power_table()[3], 5.0)

    def test_settles_on_underdog_table(self):
        from linemaker import pp
        bet = {"stake": 10, "book": "Underdog", "pp_kind": "Power", "legs": [{}, {}, {}]}
        self.assertEqual(pp.settle_entry(bet, ["won", "won", "won"]), ("won", 50.0))   # 6x
        bet["book"] = "PrizePicks"
        self.assertEqual(pp.settle_entry(bet, ["won", "won", "won"]), ("won", 40.0))   # 5x

    def test_only_underdog_lines_kept(self):
        from types import SimpleNamespace as NS
        from linemaker import pp
        fm = NS(books=[("Underdog Fantasy", [-120, None]), ("PrizePicks", [-110, None])],
                multipliers={"Underdog Fantasy": [1.0]})
        with pp.use_app("ud"):
            out = pp._pp_only([fm])
        self.assertEqual([b for b, _p in out[0].books], ["Underdog"])


class BooksLoadTest(unittest.TestCase):
    def test_last_prices_only(self):
        import json, os, tempfile
        from linemaker.tracking import load_records
        from linemaker.book_report import book_scores
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "NBA-props"))
            hist = [{"t": str(i), "A": {"FanDuel": -110, "DraftKings": -120}, "B": {"FanDuel": -110, "DraftKings": 100}}
                    for i in range(30)]
            recs = {"k1": {"history": hist, "result": {"winner": "A"}, "inputs": {"bet_type": "prop"},
                           "sides": [{"side": "A"}], "books": {"x": 1}},
                    "k2": {"history": hist, "inputs": {"bet_type": "prop"}}}
            with open(os.path.join(d, "NBA-props", "2026-11-05.json"), "w") as f:
                json.dump(recs, f)
            got = load_records(d, 14, "2026-11-06", graded_only=True, last_prices=True)
            self.assertEqual(len(got), 1)
            self.assertEqual(len(got[0]["history"]), 1)
            self.assertNotIn("sides", got[0])
            self.assertIn("FanDuel", book_scores(got)["props"])


class AuditRulesTest(unittest.TestCase):
    def test_tune_band(self):
        from linemaker import autotune
        with mock.patch.dict("os.environ", {"LINEMAKER_TUNE_BAND": "0.2-0.8"}):
            self.assertTrue(autotune.in_band({"market_prob_a": 0.55}))
            self.assertFalse(autotune.in_band({"market_prob_a": 0.05}))   # a deep alt rung
        with mock.patch.dict("os.environ", {"LINEMAKER_TUNE_BAND": "off"}):
            self.assertTrue(autotune.in_band({"market_prob_a": 0.05}))

    def test_side_pause_and_its_shadow_record(self):
        from linemaker import autotune
        with mock.patch.dict("os.environ", {"LINEMAKER_PAUSED_MARKETS": "NFL overs"}), \
                mock.patch.object(autotune, "load_weights", return_value={}):
            self.assertTrue(autotune.side_paused("NFL", "A"))
            self.assertIsNone(autotune.side_paused("NFL", "B"))   # unders still picked
            self.assertIsNone(autotune.side_paused("NBA", "A"))
            self.assertIsNone(autotune.side_paused("NFL", "A", "total"))
        over = {"sport": "NFL", "inputs": {"bet_type": "prop"}, "shadow_pick": {"side": "A"}}
        under = dict(over, shadow_pick={"side": "B"})
        self.assertTrue(autotune._pause_matches(over, "nfl overs"))
        self.assertFalse(autotune._pause_matches(under, "nfl overs"))

    def test_overs_capped_per_game(self):
        from types import SimpleNamespace as NS
        from linemaker import pro

        def ev(game, player, side):
            return NS(market=NS(key=f"{game}|{player}", game_id=game, bet_type="prop"),
                      best=NS(side=side, label=f"{player} {side}"))
        picks = [ev("g1", "a", "A"), ev("g1", "b", "A"), ev("g1", "c", "A"), ev("g1", "d", "B"), ev("g2", "e", "A")]
        res = NS(picks=picks, passes=[])
        with mock.patch.dict("os.environ", {"LINEMAKER_MAX_OVERS_PER_GAME": "2"}):
            kept = pro._cap_overs(res)
            self.assertEqual([e.best.label for e in kept], ["a A", "b A", "d B", "e A"])
            tracker = NS(records={"g2|old": {"picked": True, "pick_side": "A", "game_id": "g2",
                                              "inputs": {"bet_type": "prop"}},
                                  "g2|old2": {"picked": True, "pick_side": "A", "game_id": "g2",
                                              "inputs": {"bet_type": "prop"}}})
            kept = pro._cap_overs(NS(picks=picks, passes=[]), tracker)
            self.assertNotIn("e A", [e.best.label for e in kept])   # g2 already has 2 overs today


class SharpCapTest(unittest.TestCase):
    def test_pick_too_far_against_the_sharps_is_dropped(self):
        from linemaker.pro import ProConfig, rows_to_markets, run_pro
        rows = [{"date": "2026-11-07", "sport": "NBA", "game_id": f"g{i}", "game": f"A{i} @ H{i}",
                 "bet_type": "prop", "subject": f"Player {i}", "market": "points", "line": "15.5",
                 "mean": "19", "stdev": "4", "books": books}
                for i, books in enumerate(["FanDuel:-110/-110|DraftKings:-150/+125",      # sharps lean over: fine
                                           "FanDuel:-110/-110|DraftKings:-110/-110"])]    # 50/50 at -110: 2.4 pts of juice
        with mock.patch.dict("os.environ", {"LINEMAKER_MAX_SHARP_DISAGREE": "0.02"}):
            res = run_pro(rows_to_markets(rows), "2026-11-07", ProConfig())
        reasons = [why for _l, why in res.passes if "sharp market rates it" in why]
        picked = {ev.market.subject for ev in res.picks}
        self.assertIn("Player 0", picked)
        self.assertNotIn("Player 1", picked)
        self.assertTrue(reasons)
        with mock.patch.dict("os.environ", {"LINEMAKER_MAX_SHARP_DISAGREE": "off"}):
            res = run_pro(rows_to_markets(rows), "2026-11-07", ProConfig())
        self.assertIn("Player 1", {ev.market.subject for ev in res.picks})


class StuckScanTest(unittest.TestCase):
    def test_oldest_busy_and_restart_threshold(self):
        import time
        from linemaker import bot, busy
        with busy.busy():
            self.assertLess(busy.oldest_seconds(), 5)
            later = time.time() + 1500
            with mock.patch.dict("os.environ", {"LINEMAKER_STUCK_SCAN_SECONDS": "1200"}):
                self.assertGreater(bot.scan_stuck_for(later), 1200)
                self.assertEqual(bot.scan_stuck_for(), 0.0)
            with mock.patch.dict("os.environ", {"LINEMAKER_STUCK_SCAN_SECONDS": "0"}):
                self.assertEqual(bot.scan_stuck_for(later), 0.0)
        self.assertEqual(busy.oldest_seconds(), 0.0)

    def test_nothing_tracked_alert(self):
        import datetime as dt, tempfile
        from zoneinfo import ZoneInfo
        from linemaker import admin
        tz = ZoneInfo("America/New_York")
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.dict("os.environ", {"LINEMAKER_TRACKING": "on", "LINEMAKER_TRACKING_DIR": d,
                                               "LINEMAKER_SPORTS": "NBA"}), \
                mock.patch("linemaker.live_props.espn_games", return_value=[{}, {}]):
            self.assertEqual(admin.nothing_tracked_today(dt.datetime(2026, 11, 3, 11, 0, tzinfo=tz)), "")   # too early
            self.assertIn("Nothing has been tracked", admin.nothing_tracked_today(dt.datetime(2026, 11, 3, 16, 0, tzinfo=tz)))
            import os
            os.makedirs(os.path.join(d, "NBA-props"))
            open(os.path.join(d, "NBA-props", "2026-11-03.json"), "w").write("{}")
            self.assertEqual(admin.nothing_tracked_today(dt.datetime(2026, 11, 3, 16, 0, tzinfo=tz)), "")


class CommandSyncTest(unittest.TestCase):
    def test_failure_doesnt_crash_and_unchanged_is_skipped(self):
        import tempfile
        from types import SimpleNamespace as NS
        from linemaker import bot
        calls = []

        class Tree:
            fail = False

            def get_commands(self, guild=None):
                return [NS(name="grade", description="Score a day", parameters=[])]

            def copy_global_to(self, guild):
                pass

            def clear_commands(self, guild=None):
                pass

            async def sync(self, guild=None):
                calls.append(guild)
                if self.fail:
                    raise RuntimeError("429 rate limited")

        client = NS(tree=Tree())

        async def go():
            with mock.patch("asyncio.sleep", side_effect=asyncio.CancelledError):
                client.tree.fail = True
                ok_fail = await bot.sync_commands(client)        # must not raise
                client.tree.fail = False
                ok = await bot.sync_commands(client)
                n = len(calls)
                again = await bot.sync_commands(client)          # unchanged: no API call
                return ok_fail, ok, again, n, len(calls)
        with tempfile.TemporaryDirectory() as d, mock.patch.dict("os.environ", {"LINEMAKER_CACHE_DIR": d,
                                                                               "LINEMAKER_GUILD_ID": ""}):
            ok_fail, ok, again, before, after = asyncio.run(go())
        self.assertFalse(ok_fail)
        self.assertTrue(ok)
        self.assertTrue(again)
        self.assertEqual(before, after)


class LoopGuardTest(unittest.TestCase):
    def test_error_in_a_loop_body_is_logged_not_raised(self):
        from linemaker import bot

        @bot._keep_running
        async def flaky():
            raise ValueError("Discord said no")
        self.assertIsNone(asyncio.run(flaky()))   # the loop's next round still runs

    def test_every_background_loop_is_guarded(self):
        from linemaker import bot
        for loop in (bot.pretip_loop, bot.news_loop, bot.health_loop, bot.budget_loop,
                     bot.daily_grade, bot.daily_post, bot.weekly_post):
            self.assertTrue(hasattr(loop.coro, "__wrapped__"), loop.coro.__name__)


class MultiKeyCreditsTest(unittest.TestCase):
    def test_total_is_every_keys_balance(self):
        import tempfile
        from linemaker import oddsapi
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.dict("os.environ", {"LINEMAKER_CACHE_DIR": d, "ODDS_API_KEYS": "aaaa1111,bbbb2222"}), \
                mock.patch.object(oddsapi, "_spend_path", side_effect=lambda day: f"{d}/spend_{day}.json"), \
                mock.patch.object(oddsapi, "local_today", return_value="2026-11-07"):
            oddsapi.record_spend(0, 5000, "aaaa1111")       # /quota reads key 1
            oddsapi.record_spend(0, 20000, "bbbb2222")      # and key 2
            self.assertEqual(oddsapi.total_remaining(), 25000)
            oddsapi.record_spend(30, 4970, "aaaa1111")      # a paid call on key 1
            self.assertEqual(oddsapi.total_remaining(), 24970)   # not just key 1's 4,970
            self.assertEqual(oddsapi.credits_today(), 30)


class BetNamesTest(unittest.TestCase):
    def test_underscore_names_dont_turn_into_italics(self):
        from linemaker import grade_report
        bets = [{"user": "certifiedseller_", "status": "won", "kind": "parlay", "odds": 113, "stake": 0.8, "profit": 0.9,
                 "legs": [{"date": "2026-11-07", "outcome": "won", "label": "a"}, {"date": "2026-11-07", "outcome": "won", "label": "b"}]},
                {"user": "yupii.", "status": "lost", "kind": "parlay", "odds": 186, "stake": 0.8, "profit": -0.8,
                 "legs": [{"date": "2026-11-07", "outcome": "lost", "label": "c"}, {"date": "2026-11-07", "outcome": "won", "label": "d"}]}]
        with mock.patch("linemaker.bets.load_bets", return_value=bets):
            e = grade_report.bets_embed("2026-11-07", 0)
        self.assertIn("certifiedseller\\_", e.description)
