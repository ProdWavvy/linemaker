import base64
import io
import json
import os
import stat
import tempfile
import unittest
from unittest import mock

from linemaker import polymarket_us as pm
from linemaker.oddsapi import FeedMarket

GAME = "Chicago Bulls @ Cleveland Cavaliers"
MARKETS = [
    {"slug": "aec-nba-chi-cle", "type": "basketball_team_full_game_winner", "title": "Bulls vs Cavaliers", "line": None,
     "tick": 0.01, "min_qty": 1.0, "sides": [{"long": True, "desc": "Bulls", "team": "Chicago Bulls", "quote": 0.25},
                                             {"long": False, "desc": "Cavaliers", "team": "Cleveland Cavaliers", "quote": 0.76}]},
    {"slug": "asc-nba-chi-cle-pos-8pt5", "type": "basketball_team_full_game_spread", "title": "Cavaliers by 8.5", "line": 8.5,
     "tick": 0.01, "min_qty": 1.0, "sides": [{"long": True, "desc": "+8.50", "team": "Chicago Bulls", "quote": 0.5},
                                             {"long": False, "desc": "-8.50", "team": "Cleveland Cavaliers", "quote": 0.52}]},
    {"slug": "tsc-nba-chi-cle-226pt5", "type": "basketball_team_full_game_total", "title": "Over 226.5", "line": 226.5,
     "tick": 0.01, "min_qty": 1.0, "sides": [{"long": True, "desc": "Over", "team": "", "quote": 0.49},
                                             {"long": False, "desc": "Under", "team": "", "quote": 0.53}]},
    {"slug": "astatc-nba-chi-cle-3pm-donmit-gte3", "type": "basketball_player_threes",
     "title": "Donovan Mitchell 3+ three-pointers made", "line": 3, "tick": 0.01, "min_qty": 1.0,
     "sides": [{"long": True, "desc": "Yes", "team": "", "quote": 0.58}, {"long": False, "desc": "No", "team": "", "quote": 0.44}]},
]


def fm(bet_type, subject, market, line):
    return FeedMarket("2026-11-16", "NBA", "g", GAME, "2026-11-17T00:00:00Z", bet_type, subject,
                      "Chicago Bulls" if bet_type != "prop" else "", market, line, [("FanDuel", [-110, -110])])


class MatchTest(unittest.TestCase):
    def test_each_kind_maps_to_its_market_and_side(self):
        ml = pm.match(fm("moneyline", "Cleveland Cavaliers", "moneyline", 0.0), MARKETS)
        self.assertEqual([(m["slug"], long) for m, long in ml], [("aec-nba-chi-cle", False), ("aec-nba-chi-cle", True)])
        sp = pm.match(fm("spread", "Cleveland Cavaliers", "spread", -8.5), MARKETS)
        self.assertEqual([long for _m, long in sp], [False, True])                 # Cavs -8.5 is the short side
        self.assertIsNone(pm.match(fm("spread", "Cleveland Cavaliers", "spread", 8.5), MARKETS))
        tot = pm.match(fm("total", "Cleveland Cavaliers", "game total", 226.5), MARKETS)
        self.assertEqual([long for _m, long in tot], [True, False])
        th = pm.match(fm("prop", "Donovan Mitchell", "threes", 2.5), MARKETS)       # over 2.5 = 3+ yes
        self.assertEqual([long for _m, long in th], [True, False])
        self.assertIsNone(pm.match(fm("prop", "Donovan Mitchell", "threes", 3.0), MARKETS))   # whole lines: no ladder

    def test_inject_prices_with_fee_and_links(self):
        f = fm("moneyline", "Cleveland Cavaliers", "moneyline", 0.0)
        f.books.append(("Polymarket", [-300, 250]))                                  # a second-hand price, replaced
        ev = {"slug": "nba-chi-cle-2026-11-16", "title": "CHI Bulls vs. CLE Cavaliers", "markets": []}
        with mock.patch.object(pm, "events", return_value=[ev]), mock.patch.object(pm, "event_markets", return_value=MARKETS), \
                mock.patch.dict(os.environ, {"LINEMAKER_PM_TAKER_FEE": "0.095", "LINEMAKER_OFFLINE": ""}):
            self.assertEqual(pm.inject([f], "NBA"), 1)
        books = dict(f.books)
        self.assertEqual(books["Polymarket"], [pm.american(0.76), pm.american(0.25)])
        self.assertLess(books["Polymarket"][0], -316)                               # 76c is -317 before the fee
        self.assertEqual(f.links["Polymarket"][0]["pm_slug"], "aec-nba-chi-cle")
        self.assertFalse(f.links["Polymarket"][0]["pm_long"])
        self.assertTrue(f.links["Polymarket"][0]["link"].endswith("nba-chi-cle-2026-11-16"))


class OrderTest(unittest.TestCase):
    def test_signature_verifies(self):
        from nacl.signing import SigningKey
        sk = SigningKey.generate()
        h = pm._sign("kid", base64.b64encode(bytes(sk)).decode(), "POST", "/v1/orders")
        msg = f"{h['X-PM-Timestamp']}POST/v1/orders".encode()
        sk.verify_key.verify(msg, base64.b64decode(h["X-PM-Signature"]))           # raises if wrong
        self.assertEqual(h["X-PM-Access-Key"], "kid")

    def test_plan_place_and_fills(self):
        with mock.patch.dict(os.environ, {"LINEMAKER_PM_TAKER_FEE": "0.095"}):
            plan = pm.order_plan(10.0, 0.64)
        self.assertEqual(plan["qty"], 15.0)                                         # 15 x 66.2c = $9.93
        self.assertLessEqual(plan["cost"], 10.0)
        self.assertIsNone(pm.order_plan(0.5, 0.64))
        sent = {}

        class Resp(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def opener(req, timeout=0):
            sent["body"] = json.loads(req.data)
            sent["headers"] = dict(req.headers)
            return Resp(json.dumps({"id": "o1", "executions": [
                {"type": "EXECUTION_TYPE_FILL", "lastShares": "15", "lastPx": {"value": "0.36"}}]}).encode())
        from nacl.signing import SigningKey
        cr = ("kid", base64.b64encode(bytes(SigningKey.generate())).decode())
        resp = pm.place(cr, "aec-nba-chi-cle", False, 0.64, 15.0, opener=opener)
        b = sent["body"]
        self.assertEqual((b["intent"], b["price"]["value"], b["tif"]),
                         ("ORDER_INTENT_BUY_SHORT", "0.36", "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL"))   # long side's price
        self.assertIn("Idempotency-key", sent["headers"])
        qty, avg = pm.fills(resp, False)
        self.assertEqual(qty, 15.0)
        self.assertAlmostEqual(avg, 0.64)


class AccountsTest(unittest.TestCase):
    def test_link_private_file_sync_and_settle(self):
        from linemaker import bets, pm_accounts
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"LINEMAKER_SECRETS_DIR": os.path.join(d, "s"),
                                                                              "LINEMAKER_TRACKING_DIR": d, "LINEMAKER_PM_MAX_STAKE": "25"}):
            pm_accounts.link("42", "wavvy", " kid ", "c2VjcmV0", check=lambda cr: {"ok": True})
            path = pm_accounts._path()
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            self.assertEqual(pm_accounts.creds("42"), ("kid", "c2VjcmV0"))
            self.assertTrue(pm_accounts.set_max("42", 10))
            self.assertEqual(pm_accounts.max_stake("42"), 10)
            hand = bets.add_bet([{"label": "Donovan Mitchell Over 2.5 threes", "sport": "NBA", "date": "2026-11-16",
                                  "key": "2026-11-16|g|Donovan Mitchell|threes|2.5", "side": "A", "odds": -140}],
                                -140, 7.0, "Polymarket", "wavvy")
            pos = [{"slug": "astatc-nba-chi-cle-3pm-donmit-gte3", "netPosition": "12", "cost": {"value": "7.20"},
                    "marketMetadata": {"title": "Donovan Mitchell 3+ three-pointers made"}},
                   {"slug": "aec-nba-orl-bos-2026-11-16", "netPosition": "-10", "cost": {"value": "7.60"},
                    "marketMetadata": {"title": "Orlando Magic vs Boston Celtics", "outcome": "Magic"}}]
            got = pm_accounts.sync_user("42", "wavvy", ("kid", "x"), positions=pos, settlement=lambda s: {})
            self.assertEqual((got["added"], got["matched"]), (1, 1))
            allb = bets.load_bets()
            self.assertEqual(next(b for b in allb if b["id"] == hand["id"])["pm_slug"], pos[0]["slug"])   # matched, not doubled
            ext = next(b for b in allb if b.get("pm_external"))
            self.assertEqual((ext["stake"], ext["pm_long"], ext["legs"][0]["sport"]), (7.6, False, "NBA"))
            self.assertIn("No Magic", ext["legs"][0]["label"])
            self.assertEqual(bets.settle(), 0)                                      # bot grading leaves it alone
            got = pm_accounts.sync_user("42", "wavvy", ("kid", "x"), positions=pos[:1],
                                        settlement=lambda s: {"settlement": 0.0})  # Celtics won: our NO side wins
            self.assertEqual(got["settled"], 1)
            ext = next(b for b in bets.load_bets() if b.get("pm_external"))
            self.assertEqual((ext["status"], ext["profit"]), ("won", 2.4))
            self.assertTrue(pm_accounts.unlink("42"))
            self.assertIsNone(pm_accounts.creds("42"))
