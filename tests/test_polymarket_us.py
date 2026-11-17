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


class UsOnlyTest(unittest.TestCase):
    def test_second_hand_price_dropped_where_polymarket_us_has_no_market(self):
        listed = fm("moneyline", "Cleveland Cavaliers", "moneyline", 0.0)
        unlisted = fm("prop", "Donovan Mitchell", "rebounds", 4.5)               # no rebounds ladder on PM US
        other = FeedMarket("2026-11-16", "NBA", "g2", "Boston Celtics @ Miami Heat", "2026-11-17T00:30:00Z",
                           "moneyline", "Miami Heat", "Miami Heat", "moneyline", 0.0, [("FanDuel", [-110, -110])])
        for f in (listed, unlisted, other):
            f.books.append(("Polymarket", [-120, 100]))
        ev = {"slug": "nba-chi-cle-2026-11-16", "title": "CHI Bulls vs. CLE Cavaliers", "markets": []}
        env = {"LINEMAKER_PM_TAKER_FEE": "0.095", "LINEMAKER_OFFLINE": ""}
        with mock.patch.object(pm, "events", return_value=[ev]), mock.patch.object(pm, "event_markets", return_value=MARKETS), \
                mock.patch.dict(os.environ, env):
            self.assertEqual(pm.inject([listed, unlisted, other], "NBA"), 1)
        self.assertIn("Polymarket", dict(listed.books))
        self.assertNotIn("Polymarket", dict(unlisted.books))                     # can't be bet in the States
        self.assertNotIn("Polymarket", dict(other.books))                        # game not on PM US
        kept = fm("prop", "Donovan Mitchell", "rebounds", 4.5)
        kept.books.append(("Polymarket", [-120, 100]))
        with mock.patch.object(pm, "events", return_value=[]), mock.patch.dict(os.environ, env):
            pm.inject([kept], "NBA")                                             # PM US said nothing: leave it
        self.assertIn("Polymarket", dict(kept.books))
        with mock.patch.object(pm, "events", return_value=[ev]), mock.patch.object(pm, "event_markets", return_value=MARKETS), \
                mock.patch.dict(os.environ, dict(env, LINEMAKER_PM_US_ONLY="off")):
            pm.inject([kept], "NBA")
        self.assertIn("Polymarket", dict(kept.books))


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


class ComboSyncTest(unittest.TestCase):
    LEGS = [{"symbol": "aec-nba-chi-cle", "side": "SIDE_SELL", "sport": "NBA", "label": "Cleveland Cavaliers ML"},
            {"symbol": "astatc-nba-chi-cle-3pm-donmit-gte3", "side": "SIDE_BUY", "sport": "NBA",
             "label": "Donovan Mitchell 3+ three-pointers made"}]

    def _env(self, d):
        return mock.patch.dict(os.environ, {"LINEMAKER_TRACKING_DIR": d, "LINEMAKER_SECRETS_DIR": os.path.join(d, "s")})

    def test_old_raw_entry_gets_its_legs_and_settles_from_them(self):
        from linemaker import bets, pm_accounts
        pos = [{"slug": "crossd-3b18bef8e0e8d26e", "netPosition": "26", "cost": {"value": "2.50"}}]
        with tempfile.TemporaryDirectory() as d, self._env(d):
            pm_accounts.sync_user("42", "wavvy", ("k", "s"), positions=pos, combos=lambda sym: None)
            old = bets.load_bets()[0]                                           # Polymarket wouldn't say: placeholder
            self.assertEqual(old["legs"][0]["label"], "Polymarket parlay")
            got = pm_accounts.sync_user("42", "wavvy", ("k", "s"), positions=pos, combos=lambda sym: self.LEGS)
            self.assertEqual(got["fixed"], 1)
            b = bets.load_bets()[0]
            self.assertEqual(b["kind"], "parlay")
            self.assertEqual([l["label"] for l in b["legs"]], ["Cleveland Cavaliers ML", "Donovan Mitchell 3+ three-pointers made"])
            self.assertEqual(len(bets.load_bets()), 1)
            settled = {"aec-nba-chi-cle": {"settlement": 0.0}, "astatc-nba-chi-cle-3pm-donmit-gte3": {"settlement": 1.0}}
            got = pm_accounts.sync_user("42", "wavvy", ("k", "s"), positions=[], settlement=lambda s: settled.get(s, {}),
                                        combos=lambda sym: self.LEGS)
            b = bets.load_bets()[0]
            self.assertEqual((got["settled"], b["status"], b["profit"]), (1, "won", 23.5))   # Cavs won (our NO), Mitchell hit

    def test_one_leg_lost_settles_before_the_rest(self):
        from linemaker import pm_accounts
        value = pm_accounts.combo_value(self.LEGS, lambda s: {"settlement": 1.0} if s == "aec-nba-chi-cle" else {})
        self.assertEqual(value, 0.0)                                            # our SELL side of the ML lost
        self.assertIsNone(pm_accounts.combo_value(self.LEGS, lambda s: {}))
        half = pm_accounts.combo_value(self.LEGS, lambda s: {"settlement": 0.0} if s == "aec-nba-chi-cle" else {"settlement": 0.6})
        self.assertAlmostEqual(half, 0.6)                                       # a leg that couldn't resolve pays its price

    def test_parlay_you_logged_is_matched_not_doubled(self):
        from linemaker import bets, pm_accounts
        pos = [{"slug": "crossd-49e2938a73e7ec84", "netPosition": "8", "cost": {"value": "4.76"}}]
        with tempfile.TemporaryDirectory() as d, self._env(d):
            mine = bets.add_bet([{"label": "Cleveland Cavaliers ML", "sport": "NBA", "date": "2026-11-16"},
                                 {"label": "Donovan Mitchell Over 2.5 threes", "sport": "NBA", "date": "2026-11-16"}],
                                -147, 5.0, "Polymarket", "wavvy")
            got = pm_accounts.sync_user("42", "wavvy", ("k", "s"), positions=pos, combos=lambda sym: self.LEGS)
            allb = bets.load_bets()
        self.assertEqual((got["added"], got["matched"]), (0, 1))
        self.assertEqual(len(allb), 1)
        self.assertEqual((allb[0]["id"], allb[0]["pm_slug"], allb[0]["stake"]), (mine["id"], "crossd-49e2938a73e7ec84", 5.0))

    def test_leg_words(self):
        m = {"title": "Bulls vs Cavaliers", "sportsMarketType": "basketball_team_full_game_winner",
             "marketSides": [{"long": True, "description": "Bulls", "team": {"name": "Chicago Bulls"}},
                             {"long": False, "description": "Cavaliers", "team": {"name": "Cleveland Cavaliers"}}]}
        self.assertEqual(pm.leg_text(m, False), "Cleveland Cavaliers ML")
        tot = {"title": "Over 226.5", "sportsMarketType": "basketball_team_full_game_total", "line": 226.5,
               "marketSides": [{"long": True, "description": "Over"}, {"long": False, "description": "Under"}]}
        self.assertEqual(pm.leg_text(tot, False, "tsc-nba-chi-cle-226pt5"), "Under 226.5 (CHI/CLE)")
        prop = {"title": "Donovan Mitchell 3+ three-pointers made", "sportsMarketType": "basketball_player_threes",
                "marketSides": [{"long": True, "description": "Yes"}, {"long": False, "description": "No"}]}
        self.assertEqual(pm.leg_text(prop, False), "No: Donovan Mitchell 3+ three-pointers made")


class FakePM:
    """A pretend Polymarket US API for the combo + RFQ flow."""

    def __init__(self, quotes_after=1, maker_pulls=False, fill=12.5, price="0.21", later_price=None):
        self.calls, self.quotes_after, self.maker_pulls = [], quotes_after, maker_pulls
        self.fill, self.price, self.later_price = fill, price, later_price
        self.polls, self.accepted = 0, False

    def __call__(self, req, timeout=0):
        url = req.full_url
        path = url.split("api.polymarket.us", 1)[1].split("?")[0]
        body = json.loads(req.data) if req.data else None
        self.calls.append((req.get_method(), path, body, url))

        class Resp(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *a): return False
        if path == "/v1/combos":
            out = {"combo": {"id": "crossd-1", "legs": body["legs"], "state": "INSTRUMENT_STATE_OPEN", "tickSize": 0.001}}
        elif path == "/v1/rfqs" and req.get_method() == "POST":
            out = {"rfqId": "rfq-1"}
        elif path == "/v1/rfqs/quotes":
            self.polls += 1
            price = self.later_price if (self.later_price and self.polls > 2) else self.price
            if self.accepted:
                status = "QUOTE_STATUS_DELETED" if self.maker_pulls else "QUOTE_STATUS_EXECUTED"
                out = {"quotes": [{"id": "q-1", "status": status, "buyPrice": price, "buyQtyDecimal": "40",
                                   "rfqCreatorOrderId": "" if self.maker_pulls else "ord-9"}]}
            elif self.polls >= self.quotes_after:
                out = {"quotes": [{"id": "q-2", "status": "QUOTE_STATUS_ACTIVE", "buyPrice": "0.25", "buyQtyDecimal": "36",
                                   "createdTime": "2026-11-17T00:00:00Z"},
                                  {"id": "q-1", "status": "QUOTE_STATUS_ACTIVE", "buyPrice": price, "buyQtyDecimal": "40",
                                   "createdTime": "2026-11-17T00:00:01Z"}]}
            else:
                out = {"quotes": []}
        elif path.endswith("/accept"):
            self.accepted = True
            out = {}
        elif path.startswith("/v1/order/"):
            out = {"order": {"id": "ord-9", "state": "ORDER_STATE_FILLED" if self.fill else "ORDER_STATE_CANCELED",
                             "cumQuantity": self.fill, "leavesQuantity": 0, "avgPx": {"value": self.price},
                             "commissionNotionalTotalCollected": {"value": "0.31"}}}
        elif req.get_method() == "DELETE":
            out = {}
        else:
            raise AssertionError(path)
        return Resp(json.dumps(out).encode())


class ComboTest(unittest.TestCase):
    SIDES = [{"pm_slug": "aec-nba-chi-cle", "pm_long": False}, {"pm_slug": "tsc-nba-chi-cle-226pt5", "pm_long": True}]

    def _cr(self):
        from nacl.signing import SigningKey
        return ("kid", base64.b64encode(bytes(SigningKey.generate())).decode())

    def test_fee_and_odds(self):
        with mock.patch.dict(os.environ, {"LINEMAKER_PM_TAKER_FEE": "0.0695"}):   # the docs' table (tier 4, 100 lots)
            self.assertEqual(round(pm.combo_fee(0.10, 100), 2), 1.02)
            self.assertEqual(round(pm.combo_fee(0.25, 100), 2), 1.78)
        with mock.patch.dict(os.environ, {"LINEMAKER_PM_TAKER_FEE": "0.095"}):
            self.assertLess(pm.combo_american(0.25), 300)                     # 25c is +300 before the fee
            self.assertGreater(pm.combo_american(0.25), 200)

    def test_offer_takes_lowest_buy_price_and_cash_amount(self):
        fake = FakePM(quotes_after=2)
        offer = pm.combo_offer(self._cr(), self.SIDES, 10.0, sleep=lambda s: None, opener=fake)
        self.assertTrue(offer["ok"])
        self.assertEqual((offer["quote"], offer["price"], offer["qty"]), ("q-1", 0.21, 40.0))   # 21c beats 25c
        legs = fake.calls[0][2]["legs"]
        self.assertEqual(legs, [{"symbol": "aec-nba-chi-cle", "side": "SIDE_SELL"},
                                {"symbol": "tsc-nba-chi-cle-226pt5", "side": "SIDE_BUY"}])
        rfq = next(c[2] for c in fake.calls if c[1] == "/v1/rfqs")
        self.assertEqual(rfq, {"symbol": "crossd-1", "cashOrderQty": "10.00", "restRemainder": False})
        self.assertEqual(offer["cash"], 10.0)
        self.assertAlmostEqual(offer["cost"], round(40 * 0.21 + round(pm.combo_fee(0.21, 40), 2), 2))

    def test_no_quotes_closes_the_request(self):
        fake = FakePM(quotes_after=999)
        offer = pm.combo_offer(self._cr(), self.SIDES, 10.0, wait=2, sleep=lambda s: None, opener=fake)
        self.assertFalse(offer["ok"])
        self.assertIn("No market maker", offer["msg"])
        self.assertEqual(fake.calls[-1][0], "DELETE")

    def test_buy_accepts_and_reads_the_fill(self):
        fake = FakePM()
        cr = self._cr()
        offer = pm.combo_offer(cr, self.SIDES, 10.0, sleep=lambda s: None, opener=fake)
        res = pm.combo_buy(cr, offer, self.SIDES, sleep=lambda s: None, opener=fake)
        self.assertTrue(res["ok"], res)
        accept = next(c for c in fake.calls if c[1].endswith("/accept"))
        self.assertEqual((accept[0], accept[1], accept[2]), ("PUT", "/v1/rfqs/rfq-1/quotes/q-1/accept",
                                                             {"acceptedSide": "SIDE_BUY"}))
        self.assertEqual(res["qty"], 12.5)
        self.assertAlmostEqual(res["cost"], round(12.5 * 0.21 + 0.31, 2))
        self.assertEqual(res["symbol"], "crossd-1")
        self.assertTrue(all("rfqId=rfq-1" in c[3] for c in fake.calls if c[1] == "/v1/rfqs/quotes"))

    def test_buy_stops_when_the_price_got_worse(self):
        fake = FakePM(later_price="0.24")
        cr = self._cr()
        offer = pm.combo_offer(cr, self.SIDES, 10.0, sleep=lambda s: None, opener=fake)
        res = pm.combo_buy(cr, offer, self.SIDES, sleep=lambda s: None, opener=fake)
        self.assertFalse(res["ok"])
        self.assertEqual(res["moved"]["price"], 0.24)
        self.assertFalse(any(c[1].endswith("/accept") for c in fake.calls))      # nothing sent

    def test_maker_pulls_at_last_look(self):
        fake = FakePM(maker_pulls=True)
        cr = self._cr()
        offer = pm.combo_offer(cr, self.SIDES, 10.0, sleep=lambda s: None, opener=fake)
        res = pm.combo_buy(cr, offer, self.SIDES, sleep=lambda s: None, opener=fake)
        self.assertFalse(res["ok"])
        self.assertIn("pulled the price", res["msg"])

    def test_unfilled_order(self):
        fake = FakePM(fill=0)
        cr = self._cr()
        offer = pm.combo_offer(cr, self.SIDES, 10.0, sleep=lambda s: None, opener=fake)
        res = pm.combo_buy(cr, offer, self.SIDES, sleep=lambda s: None, opener=fake)
        self.assertFalse(res["ok"])
        self.assertIn("No fill", res["msg"])
