import codecs, json, os, stat, tempfile, unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent / "globalPlugins" / "addonDeveloperUpdater"))
import engine

MANIFEST = "name = demo\nsummary = Demo\nversion = 1.0\nminimumNVDAVersion = 2025.1\nlastTestedNVDAVersion = 2026.1\n"

class EngineTests(unittest.TestCase):
    def make_project(self, root, name="project", manifest=MANIFEST):
        project = Path(root) / name; project.mkdir(parents=True); (project / ".git").mkdir(); path = project / "manifest.ini"; path.write_text(manifest, encoding="utf-8"); return path
    def test_beta_version(self):
        release = engine.parse_release({"tag_name": "release-2026.2beta11", "prerelease": True}); self.assertEqual("2026.2", release.manifest_version)
    def test_release_selection_does_not_trust_api_order(self):
        items = [{"tag_name": "release-2026.2beta9", "prerelease": True}, {"tag_name": "release-2026.1.1"}, {"tag_name": "release-2026.2beta11", "prerelease": True}]
        self.assertEqual("2026.2beta11", engine._select_release(items, True).tag); self.assertEqual("2026.1.1", engine._select_release(items, False).tag)
    def test_developer_project_discovery_and_preview_default(self):
        with tempfile.TemporaryDirectory() as folder:
            manifest = self.make_project(folder); self.assertEqual([manifest.resolve()], engine.discover_manifests([Path(folder)]))
            result = engine.update(manifest, engine.Release("2026.2beta1", "2026.2", True, ""), Path(folder) / "backups")
            self.assertTrue(result.status.startswith("update available")); self.assertIn("2026.1", manifest.read_text())
    def test_non_project_and_runtime_test_are_ignored(self):
        with tempfile.TemporaryDirectory() as folder:
            plain = Path(folder) / "plain"; plain.mkdir(); (plain / "manifest.ini").write_text(MANIFEST)
            runtime = Path(folder) / "Runtime Tests"; runtime.mkdir(); self.make_project(runtime)
            self.assertEqual([], engine.discover_manifests([Path(folder)]))
    def test_cancellation_stops_discovery_and_update(self):
        with tempfile.TemporaryDirectory() as folder:
            manifest = self.make_project(folder)
            self.assertEqual([], engine.discover_manifests([Path(folder)], cancelled=lambda: True))
            result = engine.update(manifest, engine.Release("2026.2", "2026.2", False, ""), Path(folder), True, lambda: True)
            self.assertEqual("cancelled", result.status); self.assertIn("2026.1", manifest.read_text())
    def test_duplicate_names_have_distinct_ids_and_backups(self):
        with tempfile.TemporaryDirectory() as folder:
            first = self.make_project(folder, "one"); second = self.make_project(folder, "two"); release = engine.Release("2026.2", "2026.2", False, ""); backups = Path(folder) / "safe"
            one = engine.update(first, release, backups, True); two = engine.update(second, release, backups, True)
            self.assertNotEqual(one.project_id, two.project_id); self.assertEqual(2, len(list(backups.rglob("manifest.ini"))))
    def test_manifest_bom_newline_and_mode_are_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            manifest = self.make_project(folder); manifest.write_bytes(codecs.BOM_UTF8 + MANIFEST.replace("\n", "\r\n").encode()); os.chmod(manifest, stat.S_IREAD | stat.S_IWRITE); old_mode = stat.S_IMODE(manifest.stat().st_mode)
            engine.update(manifest, engine.Release("2026.2", "2026.2", False, ""), Path(folder) / "safe", True)
            data = manifest.read_bytes(); self.assertTrue(data.startswith(codecs.BOM_UTF8)); self.assertIn(b"\r\n", data); self.assertEqual(old_mode, stat.S_IMODE(manifest.stat().st_mode))
    def test_atomic_state_round_trip_and_corrupt_fallback(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"; engine.atomic_json_write(path, {"ok": True}); self.assertEqual({"ok": True}, engine.read_json(path)); path.write_text("{"); self.assertEqual({}, engine.read_json(path))
    def test_invalid_version_fails_validation(self):
        with tempfile.TemporaryDirectory() as folder:
            manifest = self.make_project(folder, manifest=MANIFEST.replace("2026.1", "future")); self.assertTrue(any("Invalid NVDA version" in error for error in engine.validate(manifest)))

if __name__ == "__main__": unittest.main()
