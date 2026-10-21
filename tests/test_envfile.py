import os
import tempfile
import unittest

from linemaker.envfile import load_env


class EnvFileTest(unittest.TestCase):
    def test_load(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, ".env")
            with open(path, "w") as f:
                f.write('# comment\nLM_T1=abc\nexport LM_T2="quoted value"\nLM_T3=\nLM_T4=keep\n')
            os.environ["LM_T4"] = "already set"
            try:
                loaded = load_env(path)
                self.assertEqual(os.environ["LM_T1"], "abc")
                self.assertEqual(os.environ["LM_T2"], "quoted value")
                self.assertNotIn("LM_T3", os.environ)          # blank values are skipped
                self.assertEqual(os.environ["LM_T4"], "already set")
                self.assertEqual(sorted(loaded), ["LM_T1", "LM_T2"])
            finally:
                for k in ("LM_T1", "LM_T2", "LM_T4"):
                    os.environ.pop(k, None)
