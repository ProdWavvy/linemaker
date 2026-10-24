import gzip
import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock

from linemaker import backup


class BackupTest(unittest.TestCase):
    def test_pushes_changed_files_gzipped_and_hides_the_token(self):
        root = tempfile.mkdtemp()
        remote = os.path.join(tempfile.mkdtemp(), "data.git")
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", remote], check=True)
        os.makedirs(os.path.join(root, "tracking", "live"))
        with open(os.path.join(root, "tracking", "2026-10-24.json"), "w") as f:
            json.dump({"k": 1}, f)
        with open(os.path.join(root, "tracking", "live", "2026-10-24.json"), "w") as f:
            json.dump({"k": 2}, f)
        env = {"LINEMAKER_BACKUP_REPO": "me/data", "LINEMAKER_BACKUP_TOKEN": "secrettoken123",
               "LINEMAKER_BACKUP_URL": remote, "LINEMAKER_TRACKING_DIR": "tracking",
               "LINEMAKER_SNAPSHOTS": "snapshots", "LINEMAKER_CACHE_DIR": "cache"}
        with mock.patch.dict(os.environ, env):
            first = backup.run_backup(root)
            again = backup.run_backup(root)
        self.assertIn("2 file(s)", first)
        self.assertIn("nothing changed", again)
        check = tempfile.mkdtemp()
        subprocess.run(["git", "clone", "-q", remote, check], check=True)
        with gzip.open(os.path.join(check, "tracking", "live", "2026-10-24.json.gz")) as g:
            self.assertEqual(json.load(g), {"k": 2})
        cfg = open(os.path.join(root, "cache", "backup_repo", ".git", "config")).read()
        self.assertNotIn("secrettoken123", cfg)

    def test_not_configured(self):
        with mock.patch.dict(os.environ, {"LINEMAKER_BACKUP_REPO": "", "LINEMAKER_BACKUP_TOKEN": ""}):
            self.assertIn("isn't set up", backup.run_backup(tempfile.mkdtemp()))


if __name__ == "__main__":
    unittest.main()
