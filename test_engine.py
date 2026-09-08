import codecs, io, json, os, stat, tempfile, unittest, zipfile
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent / "globalPlugins" / "addonDeveloperUpdater"))
import engine
import publisher

MANIFEST = "name = demo\nsummary = Demo\nversion = 1.0\nminimumNVDAVersion = 2025.1\nlastTestedNVDAVersion = 2025.4\n"
CURRENT_RELEASE_FAMILY = "2026.2"

class EngineTests(unittest.TestCase):
    def make_project(self, root, name="project", manifest=MANIFEST):
        project = Path(root) / name; project.mkdir(parents=True); (project / ".git").mkdir(); path = project / "manifest.ini"; path.write_text(manifest, encoding="utf-8"); return path
    def test_beta_version(self):
        release = engine.parse_release({"tag_name": "release-2026.2beta11", "prerelease": True}); self.assertEqual("2026.2", release.manifest_version)
        self.assertTrue(engine.parse_release({"tag_name": "RELEASE-2026.2RC1", "prerelease": False}).prerelease)
    def test_addon_version_follows_current_stable_nvda_family(self):
        version = engine.values(Path(__file__).parent / "manifest.ini")["version"]
        self.assertTrue(version == CURRENT_RELEASE_FAMILY or version.startswith(CURRENT_RELEASE_FAMILY + "."))
        if version != CURRENT_RELEASE_FAMILY:
            self.assertGreaterEqual(int(version.rsplit(".", 1)[1]), 0)
    def test_release_selection_does_not_trust_api_order(self):
        items = [{"tag_name": "release-2026.3beta9", "prerelease": True}, {"tag_name": "release-2026.2.0"}, {"tag_name": "release-2026.3beta11", "prerelease": True}]
        self.assertEqual("2026.3beta11", engine._select_release(items, True).tag); self.assertEqual("2026.2.0", engine._select_release(items, False).tag)
        self.assertIsNone(engine.parse_release({"tag_name": "junk-2026.9"}))
    def test_developer_project_discovery_and_preview_default(self):
        with tempfile.TemporaryDirectory() as folder:
            manifest = self.make_project(folder); self.assertEqual([manifest.resolve()], engine.discover_manifests([Path(folder)]))
            result = engine.update(manifest, engine.Release("2026.2beta1", "2026.2", True, ""), Path(folder) / "backups")
            self.assertTrue(result.status.startswith("update available")); self.assertIn("2025.4", manifest.read_text())
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
            self.assertEqual("cancelled", result.status); self.assertIn("2025.4", manifest.read_text())
    def test_background_paths_use_only_existing_approved_projects(self):
        with tempfile.TemporaryDirectory() as folder:
            approved = self.make_project(folder, "approved"); unapproved = self.make_project(folder, "unapproved")
            approved_id = engine.project_id(approved)
            state = {"approvedProjects": [approved_id], "projects": [{"project_id": approved_id, "path": str(approved)}, {"project_id": engine.project_id(unapproved), "path": str(unapproved)}, {"project_id": "missing", "path": str(Path(folder) / "missing.ini")}]}
            self.assertEqual([approved.resolve()], engine.approved_manifest_paths(state))
    def test_bounded_scan_does_not_forget_previous_projects(self):
        current = engine.ProjectResult("new", "New", "C:/new/manifest.ini", "current")
        merged = engine.merge_project_records([{"project_id": "old", "name": "Old", "path": "D:/old/manifest.ini", "status": "current"}], [current])
        self.assertEqual({"old", "new"}, {item["project_id"] for item in merged})
    def test_duplicate_names_have_distinct_ids_and_backups(self):
        with tempfile.TemporaryDirectory() as folder:
            first = self.make_project(folder, "one"); second = self.make_project(folder, "two"); release = engine.Release("2026.2", "2026.2", False, ""); backups = Path(folder) / "safe"
            one = engine.update(first, release, backups, True); two = engine.update(second, release, backups, True)
            self.assertNotEqual(one.project_id, two.project_id); self.assertEqual(2, len(list(backups.rglob("manifest.ini"))))
    def test_unsafe_names_cannot_escape_backup_root(self):
        with tempfile.TemporaryDirectory() as folder:
            manifest = self.make_project(folder, manifest=MANIFEST.replace("name = demo", "name = ../../bad:name")); backups = Path(folder) / "safe"
            result = engine.update(manifest, engine.Release("../2026.2", "2026.2", False, ""), backups, True)
            self.assertTrue(result.status.startswith("updated")); self.assertEqual(1, len(list(backups.rglob("manifest.ini"))))
    def test_manifest_bom_newline_and_mode_are_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            manifest = self.make_project(folder); manifest.write_bytes(codecs.BOM_UTF8 + MANIFEST.replace("\n", "\r\n").encode()); os.chmod(manifest, stat.S_IREAD | stat.S_IWRITE); old_mode = stat.S_IMODE(manifest.stat().st_mode)
            engine.update(manifest, engine.Release("2026.2", "2026.2", False, ""), Path(folder) / "safe", True)
            data = manifest.read_bytes(); self.assertTrue(data.startswith(codecs.BOM_UTF8)); self.assertIn(b"\r\n", data); self.assertEqual(old_mode, stat.S_IMODE(manifest.stat().st_mode))
    def test_quoted_manifest_version_and_comment_format_are_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            text = MANIFEST.replace("lastTestedNVDAVersion = 2025.4", "lastTestedNVDAVersion = \"2025.4\"  # tested")
            manifest = self.make_project(folder, manifest=text)
            result = engine.update(manifest, engine.Release("2026.2", "2026.2", False, ""), Path(folder) / "safe", True)
            self.assertTrue(result.status.startswith("updated")); self.assertIn('lastTestedNVDAVersion = "2026.2"  # tested', manifest.read_text())
    def test_atomic_state_round_trip_and_corrupt_fallback(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"; engine.atomic_json_write(path, {"ok": True}); self.assertEqual({"ok": True}, engine.read_json(path)); path.write_text("{"); self.assertEqual({}, engine.read_json(path))
            path.write_text("[]"); self.assertEqual({}, engine.read_json(path))
    def test_invalid_version_fails_validation(self):
        with tempfile.TemporaryDirectory() as folder:
            manifest = self.make_project(folder, manifest=MANIFEST.replace("2025.4", "future")); self.assertTrue(any("Invalid NVDA version" in error for error in engine.validate(manifest)))
        for invalid in ("2026", "2026.2.3.4", "2026.-1", "junk2026.2"):
            with self.assertRaises(ValueError): engine.version_tuple(invalid)

    def test_store_manifest_guidelines_validate_names_urls_api_versions_and_channels(self):
        versions = {"2026.2": {"experimental": False}, "2026.3": {"experimental": True}}
        valid = {"name": "demo_addon", "version": "2026.2.1", "url": "https://example.com", "minimumnvdaversion": "2026.2", "lasttestednvdaversion": "2026.2"}
        self.assertEqual([], publisher._manifest_guideline_issues(valid, versions))
        invalid = {**valid, "name": "bad name", "version": "v1", "url": "http://example.com", "lasttestednvdaversion": "2026.3", "updatechannel": "stable"}
        issues = publisher._manifest_guideline_issues(invalid, versions)
        self.assertTrue(any("manifest name" in issue for issue in issues))
        self.assertTrue(any("manifest version" in issue for issue in issues))
        self.assertTrue(any("HTTPS" in issue for issue in issues))
        self.assertTrue(any("experimental" in issue for issue in issues))

    def test_release_package_ai_disclosure_audit_checks_shipped_text_only(self):
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w") as archive:
            archive.writestr("manifest.ini", "name = demo\nversion = 2026.2\n")
            archive.writestr("globalPlugins/demo.py", "# This code was generated by ChatGPT\n")
            archive.writestr("sounds/tone.wav", b"AI-generated binary marker is ignored")
        with zipfile.ZipFile(io.BytesIO(payload.getvalue())) as archive:
            findings = publisher._ai_disclosure_issues(archive)
        self.assertEqual(1, len(findings))
        self.assertIn("globalPlugins/demo.py, line 1", findings[0])

    def test_store_readiness_reports_every_guideline_issue(self):
        report = publisher.ProjectPublishInfo(
            "id", "Demo", "manifest.ini", ".", "2026.2", "Demo", "Justin Coffin",
            "https://github.com/jcoffin1/demo", 0, True, 0, "2026.2",
            "https://github.com/jcoffin1/demo/releases/download/v2026.2/demo.nvda-addon",
            True, True, "", ("AI-related authorship wording in release package: readme.md, line 2",),
        )
        self.assertEqual(list(report.store_guideline_issues), publisher.store_readiness_reasons(report))

if __name__ == "__main__": unittest.main()
