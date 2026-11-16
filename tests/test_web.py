import os
import tempfile
import unittest
from unittest import mock

from aiohttp.test_utils import TestClient, TestServer

from linemaker import web


def _ev():
    from linemaker.pro import ProConfig, evaluate, rows_to_markets
    row = {"date": "2026-11-16", "sport": "NBA", "game_id": "g", "game": "Utah Jazz @ Memphis Grizzlies",
           "bet_type": "prop", "subject": "Ja Guard", "market": "points", "line": "22.5", "mean": "26",
           "notes": "ESPN game-log model (20 games); x; DvP ×1.09 vs Utah Jazz (G)",
           "books": "FanDuel:-110/-110|DraftKings:-115/-105|Polymarket:-120/+100"}
    return evaluate(rows_to_markets([row])[0], ProConfig(n_sims=2000, track=False))


class WebTest(unittest.IsolatedAsyncioTestCase):
    async def test_login_cards_and_private_api(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"LINEMAKER_CACHE_DIR": d,
                                                                              "LINEMAKER_TRACKING_DIR": d,
                                                                              "LINEMAKER_WEB_URL": "https://dash.test"}):
            web._CARDS.clear()
            ev = _ev()
            web.record_card("🎯 NBA Value Scan", [(ev, None)], [], [])
            web.record_card("🎯 NBA Value Scan", [(ev, None)], [], [])          # a slimmer retry: not doubled
            self.assertEqual(len(web.cards()), 1)
            link = web.login_link("42", "wavvy")
            self.assertTrue(link.startswith("https://dash.test/login?t="))
            token = link.split("t=", 1)[1]
            async with TestClient(TestServer(web.make_app())) as c:
                r = await c.get("/")
                self.assertIn("/web", await r.text())                           # signed out
                r = await c.get("/api/cards")
                self.assertEqual(r.status, 401)
                r = await c.get("/login?t=nope", allow_redirects=False)
                self.assertEqual(r.status, 403)
                r = await c.get(f"/login?t={token}", allow_redirects=False)
                self.assertEqual(r.status, 302)
                sid = r.cookies["lm_session"].value
                self.assertTrue(r.cookies["lm_session"]["httponly"])
                r = await c.get(f"/login?t={token}", allow_redirects=False)
                self.assertEqual(r.status, 403)                                 # one use only
                h = {"Cookie": f"lm_session={sid}"}
                self.assertEqual((await (await c.get("/api/me", headers=h)).json())["name"], "wavvy")
                card = (await (await c.get("/api/cards", headers=h)).json())[0]
                pick = card["picks"][0]
                self.assertEqual(pick["label"], "Ja Guard o22.5 points")
                self.assertIn("Jazz soft vs guards", pick["tags"])
                self.assertEqual({b["book"] for b in pick["books"]}, {"FanDuel", "DraftKings", "Polymarket"})
                self.assertIn("Line shop", await (await c.get("/", headers=h)).text())
                self.assertEqual((await c.get("/api/bets", headers=h)).status, 200)
                self.assertEqual((await c.get("/api/group", headers=h)).status, 200)
                await c.get("/logout", headers=h, allow_redirects=False)
                self.assertEqual((await c.get("/api/me", headers=h)).status, 401)

    async def test_ufc_card_and_weekly(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"LINEMAKER_CACHE_DIR": d,
                                                                              "LINEMAKER_TRACKING_DIR": d,
                                                                              "LINEMAKER_WEB_URL": "https://dash.test"}):
            web._CARDS.clear()
            web.record_card("🥊 UFC Props", [], [], [], ufc={"fight": [{"n": 1, "label": "Gane by KO/TKO", "quotes": []}],
                                                            "strike": []})
            self.assertEqual(web.cards()[0]["ufc"]["fight"][0]["label"], "Gane by KO/TKO")
            sid = web.redeem(web.login_link("42", "wavvy").split("t=", 1)[1])
            async with TestClient(TestServer(web.make_app())) as c:
                wk = await (await c.get("/api/weekly", headers={"Cookie": f"lm_session={sid}"})).json()
            self.assertTrue(any(l.startswith("🎯 **Picks:**") for l in wk))

    async def test_owner_sees_everyone_members_dont(self):
        from linemaker import bets
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"LINEMAKER_CACHE_DIR": d, "LINEMAKER_TRACKING_DIR": d,
                                                                              "LINEMAKER_WEB_URL": "https://dash.test",
                                                                              "LINEMAKER_SECRETS_DIR": os.path.join(d, "s"),
                                                                              "LINEMAKER_PER_USER_BETS": "on"}):
            bets.add_bet([{"label": "Lakers ML", "sport": "NBA", "date": "2026-11-16"}], 120, 8, "FanDuel", "friend1")
            boss = web.redeem(web.login_link("1", "wavvy", True).split("t=", 1)[1])
            pal = web.redeem(web.login_link("2", "friend2", False).split("t=", 1)[1])
            async with TestClient(TestServer(web.make_app())) as c:
                hb, hp = {"Cookie": f"lm_session={boss}"}, {"Cookie": f"lm_session={pal}"}
                self.assertTrue((await (await c.get("/api/me", headers=hb)).json())["owner"])
                self.assertEqual((await c.get("/api/members", headers=hp)).status, 403)
                self.assertEqual((await c.get("/api/activity", headers=hp)).status, 403)
                names = [m["name"] for m in await (await c.get("/api/members", headers=hb)).json()]
                self.assertIn("friend1", names)
                theirs = await (await c.get("/api/bets?user=friend1", headers=hb)).json()
                self.assertEqual(theirs["open"][0]["what"], "Lakers ML")                 # owner: anyone's dashboard
                mine = await (await c.get("/api/bets?user=friend1", headers=hp)).json()
                self.assertEqual(mine["open"], [])                                      # members: only their own

    def test_stars(self):
        from types import SimpleNamespace as NS
        from linemaker.gamelogs import HitProfile, Window
        from linemaker.scan_format import pick_stars
        ev = NS(best=NS(best_implied=0.52, gap=0.09, market_prob=0.55, side="A", label="X Over 1.5 points"),
                market=NS(notes="DvP ×1.08 vs Utah Jazz (G)", bet_type="prop", sport="NBA"), model_weight=0.5, vig_assumed=False)
        strong = HitProfile(season=Window(14, 20, 2.0), recent={10: Window(8, 10, 2.1)})
        self.assertEqual(pick_stars(ev, strong), 5)
        ev.best.gap = 0.055
        self.assertEqual(pick_stars(ev, HitProfile(season=Window(5, 20, 1.0))), 4)   # sharp + DvP
        ev.market.notes, ev.best.market_prob = "", 0.5
        self.assertEqual(pick_stars(ev, None), 3)
