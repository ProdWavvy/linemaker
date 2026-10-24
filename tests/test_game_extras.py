import os
import unittest
from unittest import mock
from zoneinfo import ZoneInfo

from linemaker.oddsapi import parse_game_odds, results_from_scores

TZ = ZoneInfo("America/New_York")
EVENT = {"id": "e1", "home_team": "Celtics", "away_team": "Knicks", "commence_time": "2026-10-24T23:30:00Z",
         "bookmakers": [{"key": "fanduel", "title": "FanDuel", "markets": [
             {"key": "alternate_spreads", "outcomes": [
                 {"name": "Celtics", "price": 150, "point": -9.5}, {"name": "Knicks", "price": -180, "point": 9.5},
                 {"name": "Celtics", "price": -250, "point": -2.5}, {"name": "Knicks", "price": 200, "point": 2.5}]},
             {"key": "alternate_totals", "outcomes": [
                 {"name": "Over", "price": 140, "point": 230.5}, {"name": "Under", "price": -170, "point": 230.5}]},
             {"key": "team_totals", "outcomes": [
                 {"name": "Over", "description": "Celtics", "price": -110, "point": 114.5},
                 {"name": "Under", "description": "Celtics", "price": -110, "point": 114.5},
                 {"name": "Over", "description": "Knicks", "price": -115, "point": 108.5},
                 {"name": "Under", "description": "Knicks", "price": -105, "point": 108.5}]}]}]}


class GameExtrasTest(unittest.TestCase):
    def test_parse(self):
        feed = parse_game_odds([EVENT], "NBA", TZ)
        spreads = sorted((fm.line, fm.books[0][1]) for fm in feed if fm.bet_type == "spread")
        self.assertEqual(spreads, [(-9.5, [150, -180]), (-2.5, [-250, 200])])   # both sides on one market
        tt = {fm.subject: (fm.line, fm.books[0][1], fm.opponent) for fm in feed if fm.bet_type == "team_total"}
        self.assertEqual(tt["Celtics"], (114.5, [-110, -110], "Knicks"))
        self.assertEqual(tt["Knicks"], (108.5, [-115, -105], "Celtics"))
        self.assertTrue(any(fm.bet_type == "total" and fm.line == 230.5 for fm in feed))

    def test_grade_team_total(self):
        recs = {"k": {"game_id": "e1", "subject": "Knicks", "market": "team total",
                      "inputs": {"bet_type": "team_total", "line": 108.5}}}
        scores = [{"id": "e1", "completed": True, "scores": [{"name": "Celtics", "score": "112"},
                                                              {"name": "Knicks", "score": "104"}]}]
        self.assertEqual(results_from_scores(scores, recs)[0]["actual"], "104")

    def test_team_total_projection_and_label(self):
        from linemaker.oddsapi import feed_row
        from linemaker.pro import ProConfig, rows_to_markets, run_pro
        fm = next(f for f in parse_game_odds([EVENT], "NBA", TZ) if f.bet_type == "team_total" and f.subject == "Knicks")
        row = feed_row(fm)
        row.update(mean="104", stdev="12.6")
        res = run_pro(rows_to_markets([row]), row["date"], ProConfig(track=False, n_sims=4000))
        sides = {s.label: s for s in res.evaluated[0].sides}
        self.assertIn("Knicks team total Under 108.5", sides)
        self.assertGreater(sides["Knicks team total Under 108.5"].model_prob, 0.6)

    def test_two_team_totals_can_share_a_ticket(self):
        from linemaker.parlay import sgp_check
        from types import SimpleNamespace as NS
        legs = [NS(market=NS(game_id="e1", game="g", bet_type="team_total", subject=t, market="team total",
                             key=t), best=NS(label=t)) for t in ("Celtics", "Knicks")]
        self.assertEqual(sgp_check(legs, "FanDuel")["blocked"], [])


if __name__ == "__main__":
    unittest.main()
