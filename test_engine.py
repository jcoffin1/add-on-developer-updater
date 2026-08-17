import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent / "globalPlugins" / "addonDeveloperUpdater"))
import engine


class EngineTests(unittest.TestCase):
    def test_beta_version(self):
        release = engine.parse_release({"tag_name": "release-2026.2beta9", "prerelease": True})
        self.assertEqual("2026.2", release.manifest_version)

    def test_developer_project_is_discovered_and_updated(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "project"
            root.mkdir()
            (root / ".git").mkdir()
            manifest = root / "manifest.ini"
            manifest.write_text("name = demo\nsummary = Demo\nversion = 1.0\nminimumNVDAVersion = 2025.1\nlastTestedNVDAVersion = 2026.1\n", encoding="utf-8")
            found = engine.discover_manifests([Path(folder)])
            self.assertEqual([manifest.resolve()], found)
            result = engine.update(manifest, engine.Release("2026.2beta1", "2026.2", True, ""), Path(folder) / "backups")
            self.assertTrue(result.status.startswith("updated"))
            self.assertIn("2026.2", manifest.read_text())

    def test_non_project_manifest_is_ignored(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "manifest.ini"
            path.write_text("name = demo\nlastTestedNVDAVersion = 2026.1\n", encoding="utf-8")
            self.assertEqual([], engine.discover_manifests([Path(folder)]))


if __name__ == "__main__":
    unittest.main()
