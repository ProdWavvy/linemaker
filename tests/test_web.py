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
