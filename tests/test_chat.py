"""Chat loop with a scripted stand-in for Claude and a fake odds feed (no network, no cost)."""
import asyncio
import os
import tempfile
import unittest
from types import SimpleNamespace as NS
from unittest import mock

try:
    import anthropic  # noqa: F401
except ImportError:
    anthropic = None

from tests.test_oddsapi import FakeAPI


def block(**kw):
    return NS(**kw)


class ScriptedClaude:
    """Returns the queued responses in order and records each request."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.beta = NS(messages=NS(create=self.create))

    async def create(self, **kw):
        self.requests.append(dict(kw, messages=list(kw["messages"])))
        return self.responses.pop(0)


@unittest.skipIf(anthropic is None, "anthropic not installed")
class ChatTest(unittest.TestCase):
    def setUp(self):
        os.environ.setdefault("ANTHROPIC_API_KEY", "test")
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["LINEMAKER_SNAPSHOTS"] = self.tmp.name
        from linemaker import chat
        self.chat_mod = chat
        self.patch = mock.patch.object(chat, "OddsAPI", lambda: FakeAPI())
        self.patch.start()
        self.today = mock.patch.object(chat, "_today", lambda: "2026-10-21")
        self.today.start()

    def tearDown(self):
        self.patch.stop()
        self.today.stop()
        self.tmp.cleanup()

    def test_sentence_to_priced_play(self):
        bot = self.chat_mod.LinemakerChat()
        bot.client = ScriptedClaude([
            NS(stop_reason="tool_use", content=[
                block(type="tool_use", id="t1", name="price_projections",
                      input={"sport": "NBA", "projections": [
                          {"subject": "Jose Example", "market": "points", "mean": 27.0,
                           "lineup_confirmed": True}]})]),
            NS(stop_reason="end_turn", content=[block(type="text", text="Over 22.5 has an edge.")]),
        ])
        turn = asyncio.run(bot.respond(1, "Justin", "I have Jose Example at 27 points tonight"))
        self.assertEqual(turn.text, "Over 22.5 has an edge.")
        self.assertEqual(len(turn.results), 1)          # shown as pick cards, no file
        tool_result = bot.client.requests[1]["messages"][-1]["content"][0]
        self.assertFalse(tool_result.get("is_error"))
        self.assertIn("Over 22.5 points", tool_result["content"])
        self.assertIn("gap **+", tool_result["content"])          # compact summary to the model
        self.assertEqual(turn.files, [])
        self.assertIn("Over 22.5 points", turn.results[0][1].picks[0].best.label)
        req = bot.client.requests[0]
        self.assertEqual(req["model"], "claude-opus-5-5")
        self.assertEqual(req["fallbacks"], "default")
        # History keeps plain text for the next message in this channel.
        self.assertEqual([m["role"] for m in bot.history[1]], ["user", "assistant"])

    def test_tool_errors_go_back_to_the_model(self):
        bot = self.chat_mod.LinemakerChat()
        bot.client = ScriptedClaude([
            NS(stop_reason="tool_use", content=[
                block(type="tool_use", id="t1", name="devig", input={"odds": ["abc", "-110"]})]),
            NS(stop_reason="end_turn", content=[block(type="text", text="Those odds look off.")]),
        ])
        turn = asyncio.run(bot.respond(1, "J", "devig abc and -110"))
        result = bot.client.requests[1]["messages"][-1]["content"][0]
        self.assertTrue(result["is_error"])
        self.assertEqual(turn.text, "Those odds look off.")

    def test_tools_compute_real_numbers(self):
        bot = self.chat_mod.LinemakerChat()
        ctx = {"files": [], "attached_rows": []}
        self.assertIn("EV +$14.00", bot.run_tool("expected_value",
                                                 {"probability": 38, "odds": "+200"}, ctx))
        self.assertIn("no-vig 50.00%", bot.run_tool("devig", {"odds": ["-110", "-110"]}, ctx))
        board = bot.run_tool("get_odds_board", {"sport": "NBA"}, ctx)
        self.assertIn("Road Bulls @ Home Hawks", board)

    def test_refusal(self):
        bot = self.chat_mod.LinemakerChat()
        bot.client = ScriptedClaude([NS(stop_reason="refusal", content=[])])
        self.assertIn("can't help", asyncio.run(bot.respond(1, "J", "x")).text)

    def test_split_for_discord(self):
        chunks = self.chat_mod.split_for_discord("a" * 3000 + "\nshort line")
        self.assertTrue(all(len(c) <= 1990 for c in chunks))
        self.assertEqual("".join(chunks).replace("\n", ""), "a" * 3000 + "short line")


if __name__ == "__main__":
    unittest.main()


@unittest.skipIf(anthropic is None, "anthropic not installed")
class ModelChoiceTest(unittest.TestCase):
    def test_haiku_gets_no_fallback_params(self):
        os.environ.setdefault("ANTHROPIC_API_KEY", "test")
        from linemaker import chat
        with mock.patch.object(chat, "MODEL", "claude-haiku-5-5"):
            bot = chat.LinemakerChat()
            bot.client = ScriptedClaude([NS(stop_reason="end_turn",
                                            content=[block(type="text", text="hi")])])
            asyncio.run(bot.respond(1, "J", "hello"))
        req = bot.client.requests[0]
        self.assertEqual(req["model"], "claude-haiku-5-5")
        self.assertNotIn("fallbacks", req)
        self.assertNotIn("betas", req)


class ScriptedGemini:
    """Stand-in for google.genai.Client: returns queued responses, records requests."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.aio = NS(models=NS(generate_content=self.generate_content))

    async def generate_content(self, model, contents, config):
        self.requests.append({"model": model, "contents": list(contents), "config": config})
        return self.responses.pop(0)


def gemini_call(name, args):
    from google.genai import types
    call = types.FunctionCall(name=name, args=args)
    content = types.Content(role="model", parts=[types.Part(function_call=call)])
    return NS(function_calls=[call], candidates=[NS(content=content)], text=None)


def gemini_text(text):
    return NS(function_calls=None, candidates=[], text=text)


try:
    import google.genai  # noqa: F401
    have_genai = True
except ImportError:
    have_genai = False


@unittest.skipIf(not have_genai, "google-genai not installed")
class GeminiChatTest(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"GEMINI_API_KEY": "test", "LINEMAKER_AI": "gemini"})
        self.env.start()
        from linemaker import chat
        self.chat_mod = chat
        self.p1 = mock.patch.object(chat, "OddsAPI", lambda: FakeAPI())
        self.p2 = mock.patch.object(chat, "_today", lambda: "2026-10-21")
        self.p1.start()
        self.p2.start()

    def tearDown(self):
        self.p1.stop()
        self.p2.stop()
        self.env.stop()

    def test_provider_selected(self):
        self.assertEqual(self.chat_mod.provider(), "gemini")
        self.assertEqual(self.chat_mod.LinemakerChat().provider, "gemini")

    def test_tool_loop(self):
        bot = self.chat_mod.LinemakerChat()
        bot.client = ScriptedGemini([
            gemini_call("expected_value", {"probability": 38, "odds": "+200"}),
            gemini_text("Yes: **+$14.00** per $100."),
        ])
        turn = asyncio.run(bot.respond(7, "J", "is 38% at +200 worth it?"))
        self.assertEqual(turn.text, "Yes: **+$14.00** per $100.")
        second = bot.client.requests[1]
        self.assertEqual(second["model"], "gemini-3.8-flash")
        fn_part = second["contents"][-1].parts[0]
        self.assertEqual(fn_part.function_response.name, "expected_value")
        self.assertIn("EV +$14.00", fn_part.function_response.response["result"])
        # Tools are declared from the same schemas Claude uses.
        names = {d.name for d in second["config"].tools[0].function_declarations}
        self.assertIn("find_plays", names)
        self.assertEqual([m["role"] for m in bot.history[7]], ["user", "assistant"])

    def test_tool_error_is_reported(self):
        bot = self.chat_mod.LinemakerChat()
        bot.client = ScriptedGemini([gemini_call("devig", {"odds": ["abc", "-110"]}),
                                     gemini_text("Those odds look off.")])
        asyncio.run(bot.respond(7, "J", "devig abc -110"))
        part = bot.client.requests[1]["contents"][-1].parts[0]
        self.assertIn("error", part.function_response.response)


@unittest.skipIf(not have_genai, "google-genai not installed")
class SpeedTest(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"GEMINI_API_KEY": "test", "LINEMAKER_AI": "gemini",
                                                "FANTASYPROS_API_KEY": "test"})
        self.env.start()
        from linemaker import chat, fantasypros
        from tests.test_fantasypros import FakeFP
        self.chat_mod = chat
        self.patches = [mock.patch.object(chat, "OddsAPI", lambda: FakeAPI()),
                        mock.patch.object(chat, "_today", lambda: "2026-10-21"),
                        mock.patch.object(fantasypros, "FantasyPros", lambda: FakeFP())]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.env.stop()

    def test_quick_path_skips_the_model(self):
        q = self.chat_mod.quick_plays_request
        self.assertEqual(q("what are the best nba props for today"), ["NBA"])
        self.assertEqual(q("any good plays tonight?"), ["NBA"])
        self.assertEqual(q("top MLB and NBA picks"), ["NBA", "MLB"])
        self.assertEqual(q("best props wnba"), ["WNBA"])
        self.assertEqual(q("best college football props"), ["NCAAF"])
        self.assertEqual(q("nhl and nfl picks tonight"), ["NFL", "NHL"])
        self.assertEqual(q("best wnba and nba props"), ["WNBA", "NBA"])
        self.assertIsNone(q("I have Brunson at 29.5 points, best play?"))
        self.assertIsNone(q("devig -115 -105"))
        self.assertEqual(q("best moneyline picks tonight"), ["NBA"])
        self.assertEqual(q("nhl moneylines tonight"), ["NHL"])
        self.assertEqual(self.chat_mod.quick_kind("best nba spreads and totals today"), "games")
        self.assertEqual(self.chat_mod.quick_kind("best nba props today"), "props")
        bot = self.chat_mod.LinemakerChat()
        bot.client = ScriptedGemini([])            # would raise if the model were called
        turn = asyncio.run(bot.respond(3, "J", "what are the best nba props for today"))
        self.assertEqual(turn.text, "")                    # the cards are the answer
        self.assertIn("Over 22.5 points", turn.results[0][1].picks[0].best.label)
        self.assertEqual(bot.client.requests, [])

    def test_overloaded_model_falls_back(self):
        from google.genai import errors
        busy = errors.ServerError(503, {"error": {"code": 503, "message": "high demand",
                                                  "status": "UNAVAILABLE"}})

        class Busy(ScriptedGemini):
            async def generate_content(self, model, contents, config):
                self.requests.append({"model": model})
                if model == self_mod.GEMINI_MODEL:
                    raise busy
                return gemini_text("Backup model here.")

        self_mod = self.chat_mod
        bot = self.chat_mod.LinemakerChat()
        bot.client = Busy([])
        with mock.patch.object(self.chat_mod.asyncio, "sleep", mock.AsyncMock()):
            turn = asyncio.run(bot.respond(4, "J", "what is a vig"))
        self.assertEqual(turn.text, "Backup model here.")
        self.assertEqual(len(bot.client.requests), 3)   # main twice, then the first fallback

        class AllBusy(ScriptedGemini):
            async def generate_content(self, model, contents, config):
                raise busy
        bot.client = AllBusy([])
        with mock.patch.object(self.chat_mod.asyncio, "sleep", mock.AsyncMock()):
            turn = asyncio.run(bot.respond(5, "J", "what is a vig"))
        self.assertIn("overloaded", turn.text)

    def test_quick_path_needs_no_fantasypros_key(self):
        with mock.patch.dict(os.environ, {"FANTASYPROS_API_KEY": ""}):
            self.assertEqual(self.chat_mod.quick_plays_request("best nba props today"), ["NBA"])

    def test_slow_model_falls_back_to_results(self):
        bot = self.chat_mod.LinemakerChat()

        class Slow(ScriptedGemini):
            async def generate_content(self, model, contents, config):
                if self.responses:
                    return await super().generate_content(model, contents, config)
                await asyncio.sleep(5)

        bot.client = Slow([gemini_call("find_plays", {"sport": "NBA"})])
        with mock.patch.object(self.chat_mod, "GEMINI_TIMEOUT", 0.2):
            turn = asyncio.run(bot.respond(4, "J", "what should I bet in basketball"))
        self.assertIn("Gemini was slow", turn.text)
        self.assertIn("Over 22.5 points", turn.results[0][1].picks[0].best.label)
        from google.genai import types
        self.assertEqual(bot.client.requests[0]["config"].thinking_config.thinking_level,
                         types.ThinkingLevel.LOW)


class VolumeModeTest(unittest.TestCase):
    def test_intent_detection(self):
        from linemaker.chat import quick_mode
        self.assertEqual(quick_mode("build me a 3 leg parlay"), "volume")
        self.assertEqual(quick_mode("volume mode nba"), "volume")
        self.assertEqual(quick_mode("give me some safe legs tonight"), "volume")
        self.assertEqual(quick_mode("is it safe to bet the knicks?"), "smart")
        self.assertEqual(quick_mode("best nba props today"), "smart")


class CacheAndErrorsTest(unittest.TestCase):
    def test_slate_cache_survives_restart(self):
        import tempfile
        import time as _t
        from linemaker import chat
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"LINEMAKER_CACHE_DIR": d}):
            entry = (_t.time(), [{"subject": "X"}], [["A @ H", "2026-10-21T23:00:00Z"]], 400, None, "ESPN")
            chat.save_slate_cache("NBA", "2026-10-21", entry)
            back = chat.load_slate_cache("NBA", "2026-10-21")
            self.assertEqual(back[1], [{"subject": "X"}])
            self.assertIsNone(chat.load_slate_cache("NBA", "2026-10-22"))

    def test_out_of_credits_message(self):
        from linemaker.chat import friendly_error
        msg = friendly_error("NBA", Exception('HTTP 401: {"error_code":"OUT_OF_USAGE_CREDITS"}'))
        self.assertIn("out of credits", msg)
        self.assertIn("LINEMAKER_ALT_LINES=off", msg)
