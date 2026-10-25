import datetime as dt
import json
import os
import tempfile
import unittest
from unittest import mock

from linemaker import oddsapi


class CreditDayTest(unittest.TestCase):
    def test_local_day_and_breakdown(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.dict(os.environ, {"LINEMAKER_CACHE_DIR": d, "LINEMAKER_TZ": "America/New_York"}):
            day = oddsapi.local_today()
            oddsapi.record_spend(30)                          # default source
            tok = oddsapi.SPEND_SOURCE.set("chat asks")
            oddsapi.record_spend(12)
            oddsapi.SPEND_SOURCE.reset(tok)
            self.assertEqual(oddsapi.credits_today(), 42)
            self.assertEqual(oddsapi.spend_breakdown(), {"buttons & commands": 30, "chat asks": 12})
            self.assertTrue(os.path.exists(os.path.join(d, f"credits_{day}.json")))
            # files from before the breakdown (a bare number) still read
            with open(os.path.join(d, "credits_2026-01-01.json"), "w") as f:
                json.dump(500, f)
            self.assertEqual(oddsapi.credits_today("2026-01-01"), 500)

    def test_day_follows_your_timezone_not_utc(self):
        late = dt.datetime(2026, 10, 26, 2, 30, tzinfo=dt.timezone.utc)    # 10:30 PM Oct 25 in New York
        with mock.patch.dict(os.environ, {"LINEMAKER_TZ": "America/New_York"}), \
                mock.patch.object(oddsapi.dt, "datetime", wraps=dt.datetime) as fake:
            fake.now.side_effect = lambda tz=None: late.astimezone(tz) if tz else late
            self.assertEqual(oddsapi.local_today(), "2026-10-25")


class AltLinesUntilTest(unittest.TestCase):
    def test_override_until_time(self):
        with mock.patch.object(oddsapi, "credits_ok", return_value=False):
            with mock.patch.dict(os.environ, {"LINEMAKER_ALT_LINES_UNTIL": ""}):
                self.assertFalse(oddsapi.alt_lines_ok())
            with mock.patch.dict(os.environ, {"LINEMAKER_ALT_LINES_UNTIL": "2999-01-01 23:59"}):
                self.assertTrue(oddsapi.alt_lines_ok())
            with mock.patch.dict(os.environ, {"LINEMAKER_ALT_LINES_UNTIL": "2001-01-01 23:59"}):
                self.assertFalse(oddsapi.alt_lines_ok())
            with mock.patch.dict(os.environ, {"LINEMAKER_ALT_LINES_UNTIL": "tonight"}):
                self.assertFalse(oddsapi.alt_lines_ok())
