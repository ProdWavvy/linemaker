import os
import unittest
from unittest import mock

from linemaker import nhl_api


def games(n, shots=3, toi="18:00", recent_toi=None):
    out = [{"gameDate": f"2026-10-{i + 10:02d}", "shots": shots, "points": 1, "goals": 0, "assists": 1,
            "toi": toi} for i in range(n)]
    if recent_toi:
        for g in out[-5:]:
            g["toi"] = recent_toi
    return out


class ToiTest(unittest.TestCase):
    def test_role_change_moves_the_projection(self):
        steady = nhl_api.toi_projection(games(12), "shots", 3.0)
        self.assertAlmostEqual(steady[0], 3.0, places=6)          # same ice time: no change
        promoted = nhl_api.toi_projection(games(12, recent_toi="22:00"), "shots", 3.0)
        self.assertGreater(promoted[0], 3.0)
        self.assertAlmostEqual(promoted[1], 22.0)
        self.assertIsNone(nhl_api.toi_projection(games(4), "shots", 3.0))

    def test_season_id(self):
        self.assertEqual(nhl_api.season_id("2026-11-14"), "20262027")
        self.assertEqual(nhl_api.season_id("2027-03-01"), "20262027")

    def test_apply_in_place(self):
        rows = [{"market": "shots on goal", "subject": "Leon Draisaitl", "_team": "Edmonton Oilers",
                 "mean": "3.000", "notes": "ESPN"},
                {"market": "points", "subject": "Somebody", "_team": "Edmonton Oilers", "mean": "20", "notes": ""}]
        with mock.patch.dict(os.environ, {"LINEMAKER_OFFLINE": "", "LINEMAKER_NHL_TOI": "on"}), \
                mock.patch.object(nhl_api, "team_abbrevs", return_value={"edmonton oilers": "EDM"}), \
                mock.patch.object(nhl_api, "roster_ids", return_value={"leon draisaitl": 8477934}), \
                mock.patch.object(nhl_api, "game_log", return_value=games(12, recent_toi="22:00")):
            self.assertEqual(nhl_api.apply_toi("2026-11-14", rows), 1)
        self.assertGreater(float(rows[0]["mean"]), 3.0)
        self.assertIn("NHL ice time 22:00 last 5", rows[0]["notes"])
        self.assertEqual(rows[1]["mean"], "20")                   # not an NHL skater market

    def test_offline_does_nothing(self):
        with mock.patch.dict(os.environ, {"LINEMAKER_OFFLINE": "1"}):
            self.assertEqual(nhl_api.apply_toi("2026-11-14", [{"market": "shots on goal", "_team": "x"}]), 0)
