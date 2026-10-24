import asyncio
import os
import tempfile
import unittest
from types import SimpleNamespace as NS
from unittest import mock


class SettingsLayersTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.root, "cache"))
        with open(os.path.join(self.root, ".env"), "w") as f:
            f.write("ODDS_API_KEY=abcdef123456\nLINEMAKER_MIN_GAP=0.03\nLINEMAKER_MAX_PICKS=25\n")
        with open(os.path.join(self.root, "settings.env"), "w") as f:
            f.write("LINEMAKER_MIN_GAP=0.02\nODDS_API_KEY=fromrepo\n")
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        for k in ("ODDS_API_KEY", "LINEMAKER_MIN_GAP", "LINEMAKER_MAX_PICKS", "LINEMAKER_OWNER_IDS"):
            os.environ.pop(k, None)
        self.root_patch = mock.patch("linemaker.envfile.root_dir", return_value=self.root)
        self.root_patch.start()

    def tearDown(self):
        self.root_patch.stop()
        self.env.stop()

    def test_priority_and_secrets(self):
        from linemaker.envfile import effective, load_all, set_override
        load_all(self.root)
        self.assertEqual(os.environ["LINEMAKER_MIN_GAP"], "0.02")        # GitHub beats .env
        self.assertEqual(os.environ["ODDS_API_KEY"], "abcdef123456")     # secrets never from GitHub
        set_override("LINEMAKER_MIN_GAP", "0.05", self.root)
        self.assertEqual(effective(self.root)["LINEMAKER_MIN_GAP"], ("0.05", "set from Discord"))

    def test_set_unset_and_masking(self):
        from linemaker import admin
        from linemaker.envfile import load_all
        load_all(self.root)
        msg = admin.apply_set("linemaker_over_min_gap", "0.05")
        self.assertIn("LINEMAKER_OVER_MIN_GAP", msg)
        self.assertIn("no restart needed", msg)
        self.assertEqual(os.environ["LINEMAKER_OVER_MIN_GAP"], "0.05")
        msg = admin.apply_set("LINEMAKER_MIN_GAP", "0.04")
        self.assertIn("`0.02` → `0.04`", msg)
        self.assertIn("back to `0.02` from settings.env", admin.apply_unset("LINEMAKER_MIN_GAP"))
        self.assertEqual(os.environ["LINEMAKER_MIN_GAP"], "0.02")
        msg = admin.apply_set("ODDS_API_KEY", "newsecretvalue99")
        self.assertNotIn("newsecretvalue99", msg)
        text = "\n".join(admin.settings_lines())
        self.assertNotIn("newsecretvalue99", text)
        self.assertNotIn("abcdef123456", text)
        self.assertIn("…ue99", text)
        self.assertIn("doesn't look like", admin.apply_set("bad key!", "1"))

    def test_logs_scrub_secrets(self):
        import logging
        from linemaker import admin
        os.environ["ODDS_API_KEY"] = "supersecretkey123"
        admin.install_log_buffer()
        logging.getLogger("x").warning("calling https://api?apiKey=supersecretkey123 now")
        out = admin.recent_logs(5, "calling")
        self.assertNotIn("supersecretkey123", out)
        self.assertIn("<ODDS_API_KEY>", out)

    def test_owner_check(self):
        from linemaker import admin
        admin._OWNERS.clear()

        class Client:
            async def application_info(self):
                return NS(owner=NS(id=42), team=None)
        self.assertTrue(asyncio.run(admin.is_owner(Client(), NS(id=42))))
        self.assertFalse(asyncio.run(admin.is_owner(Client(), NS(id=7))))
        os.environ["LINEMAKER_OWNER_IDS"] = "7, 8"
        self.assertTrue(asyncio.run(admin.is_owner(Client(), NS(id=7))))


if __name__ == "__main__":
    unittest.main()


class EnvChangesTest(SettingsLayersTest):
    def test_lists_lines_to_copy(self):
        from linemaker import admin
        with open(os.path.join(self.root, "settings.env"), "a") as f:
            f.write("# ---- Change log (newest first)\n# 10-24 · LINEMAKER_MIN_GAP · 0.03 → 0.02 · test\n"
                    "# ---- Active\nLINEMAKER_MAX_PICKS=25\n")
        admin.apply_set("LINEMAKER_OVER_MIN_GAP", "0.05")
        out = "\n".join(admin.env_changes())
        self.assertIn("`LINEMAKER_MIN_GAP=0.02` · settings.env (GitHub) · .env has `0.03`", out)
        self.assertIn("`LINEMAKER_OVER_MIN_GAP=0.05` · set from Discord · .env has nothing", out)
        self.assertNotIn("MAX_PICKS", out)               # same as .env: nothing to copy
        self.assertEqual(admin.changelog(), ["10-24 · LINEMAKER_MIN_GAP · 0.03 → 0.02 · test"])
