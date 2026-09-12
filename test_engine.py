import codecs, io, json, os, shutil, stat, subprocess, tempfile, unittest, urllib.error, zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from unittest import mock

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

    def test_manifest_reader_supports_single_and_multiline_triple_quoted_values(self):
        with tempfile.TemporaryDirectory() as folder:
            manifest = Path(folder) / "manifest.ini"
            manifest.write_text('name = demo\ndescription = """First line\nsecond line"""\nchangelog = """One line"""\nversion = "1.2"\n', encoding="utf-8")
            metadata = engine.values(manifest)
        self.assertEqual("First line\nsecond line", metadata["description"])
        self.assertEqual("One line", metadata["changelog"])
        self.assertEqual("1.2", metadata["version"])
    def test_addon_version_follows_current_stable_nvda_family(self):
        metadata = engine.values(Path(__file__).parent / "manifest.ini")
        version = metadata["version"]
        self.assertTrue(version == CURRENT_RELEASE_FAMILY or version.startswith(CURRENT_RELEASE_FAMILY + "."))
        if version != CURRENT_RELEASE_FAMILY:
            self.assertGreaterEqual(int(version.rsplit(".", 1)[1]), 0)
        self.assertEqual("2025.3.3", metadata["minimumnvdaversion"])
        self.assertEqual("2026.2", metadata["lasttestednvdaversion"])
    def test_release_selection_does_not_trust_api_order(self):
        items = [{"tag_name": "release-2026.3beta9", "prerelease": True}, {"tag_name": "release-2026.2.0"}, {"tag_name": "release-2026.3beta11", "prerelease": True}]
        self.assertEqual("2026.3beta11", engine._select_release(items, True).tag); self.assertEqual("2026.2.0", engine._select_release(items, False).tag)
        self.assertIsNone(engine.parse_release({"tag_name": "junk-2026.9"}))

    def test_release_lookup_selects_alpha_and_saves_one_shared_cache(self):
        api = io.BytesIO(json.dumps([{"tag_name": "release-2026.2.0", "prerelease": False, "html_url": "https://example.invalid/stable"}]).encode()); api.headers = {"ETag": "test-etag"}
        alpha_index = io.BytesIO(b'<a href="nvda_snapshot_alpha-57626,71bae80b.exe">alpha</a>'); alpha_index.headers = {}
        build_source = io.BytesIO(b"version_year = 2026\nversion_major = 3\nversion_minor = 0\n"); build_source.headers = {}
        with tempfile.TemporaryDirectory() as folder:
            cache = Path(folder) / "release.json"
            with mock.patch.object(engine.urllib.request, "urlopen", side_effect=[api, alpha_index, build_source]):
                release = engine.latest_release(True, cache)
            saved = engine.read_json(cache)
        self.assertEqual("alpha-57626,71bae80b", release.tag)
        self.assertEqual("2026.3", release.manifest_version)
        self.assertEqual("live", release.source)
        self.assertEqual("test-etag", saved["etag"])
        self.assertEqual("alpha-57626,71bae80b", saved["releases"]["prerelease"]["tag"])

    def test_release_lookup_uses_feed_then_last_known_cache(self):
        feed = b'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><entry><title>release-2026.2.1</title><link href="https://example.invalid/release"/></entry></feed>'
        with tempfile.TemporaryDirectory() as folder:
            cache = Path(folder) / "release.json"; feed_response = io.BytesIO(feed); feed_response.headers = {}
            with mock.patch.object(engine.urllib.request, "urlopen", side_effect=[OSError("api offline"), OSError("api offline"), feed_response]), mock.patch.object(engine.time, "sleep"):
                release = engine.latest_release(False, cache)
            self.assertEqual("release feed", release.source)
            with mock.patch.object(engine.urllib.request, "urlopen", side_effect=OSError("offline")), mock.patch.object(engine.time, "sleep"):
                cached = engine.latest_release(False, cache)
        self.assertEqual("2026.2.1", cached.tag)
        self.assertEqual("cached", cached.source)

    def test_not_modified_release_response_is_a_validated_cache_result(self):
        with tempfile.TemporaryDirectory() as folder:
            cache = Path(folder) / "release.json"; engine.atomic_json_write(cache, {"etag": "tag", "checkedAt": "2026-01-01T00:00:00+00:00", "releases": {"stable": {"tag": "2026.2.1", "manifest_version": "2026.2.1", "prerelease": False, "url": "https://example.invalid"}}})
            not_modified = urllib.error.HTTPError(engine.RELEASES_URL, 304, "Not Modified", None, None)
            with mock.patch.object(engine.urllib.request, "urlopen", side_effect=not_modified):
                release = engine.latest_release(False, cache)
        self.assertEqual("validated cache", release.source)
        self.assertEqual("2026.2.1", release.tag)

    def test_alpha_lookup_failure_never_regresses_below_cached_alpha(self):
        api = io.BytesIO(json.dumps([{"tag_name": "release-2026.2.0", "prerelease": False, "html_url": "https://example.invalid/stable"}]).encode()); api.headers = {"ETag": "new"}
        with tempfile.TemporaryDirectory() as folder:
            cache = Path(folder) / "release.json"; engine.atomic_json_write(cache, {"checkedAt": "2026-09-01T00:00:00+00:00", "releases": {"prerelease": {"tag": "alpha-60000,abcdef", "manifest_version": "2026.3", "prerelease": True, "url": "https://example.invalid/alpha", "source": "live", "checked_at": "2026-09-01T00:00:00+00:00"}}})
            with mock.patch.object(engine.urllib.request, "urlopen", side_effect=[api, OSError("alpha service offline")]):
                release = engine.latest_release(True, cache)
        self.assertEqual("alpha-60000,abcdef", release.tag)
        self.assertEqual("cached", release.source)
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

    def test_worker_confirmed_unavailable_manifest_replaces_stale_status(self):
        path = "C:/removed/manifest.ini"
        results = engine.unavailable_manifest_results([path], [{"project_id": "old-id", "name": "Removed", "path": path, "status": "update available: 2026.1 to 2026.2"}])
        self.assertEqual(1, len(results))
        self.assertEqual("old-id", results[0].project_id)
        self.assertEqual("Removed", results[0].name)
        self.assertIn("missing or offline", results[0].status)

    def test_worker_timeout_terminates_then_kills_a_stuck_process(self):
        process = mock.Mock()
        process.wait.side_effect = [subprocess.TimeoutExpired("worker", 90), subprocess.TimeoutExpired("worker", 5), 1]
        with self.assertRaisesRegex(RuntimeError, "90-second safety limit"):
            engine.wait_for_worker(process)
        process.terminate.assert_called_once_with()
        process.kill.assert_called_once_with()
        self.assertEqual(3, process.wait.call_count)

    def test_notification_state_announces_only_changes_and_recovery(self):
        records = [
            {"project_id": "update", "status": "update available: 2026.1 to 2026.2"},
            {"project_id": "waiting", "status": "awaiting manual approval"},
            {"project_id": "fixed", "status": "current"},
        ]
        current_updates, current_awaiting, current_failures, changed, resolved = engine.notification_state(
            records,
            {"update": "update available: 2026.1 to 2026.2"},
            {"waiting": "awaiting manual approval"},
            {"fixed": "validation failed: old problem"},
        )
        self.assertEqual({"update": "update available: 2026.1 to 2026.2"}, current_updates)
        self.assertEqual({"waiting": "awaiting manual approval"}, current_awaiting)
        self.assertEqual({}, current_failures)
        self.assertEqual(set(), changed)
        self.assertEqual({"fixed"}, resolved)

        records[0]["status"] = "update available: 2026.1 to 2026.3"
        *_, changed, _resolved = engine.notification_state(records, current_updates, current_awaiting, current_failures)
        self.assertEqual({"update"}, changed)

    def test_automatic_check_delay_waits_after_startup_and_backs_off(self):
        now = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(30, engine.automatic_check_delay({}, 30, now))
        state = {"lastCheckAt": (now - timedelta(minutes=5)).isoformat(), "automaticFailureCount": 0}
        self.assertEqual(25 * 60, engine.automatic_check_delay(state, 30, now))
        state["automaticFailureCount"] = 2
        self.assertEqual(115 * 60, engine.automatic_check_delay(state, 30, now))
        state["automaticFailureCount"] = 10
        self.assertEqual((24 * 60 - 5) * 60, engine.automatic_check_delay(state, 1440, now))
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

    def test_discovered_update_revalidates_and_can_be_safely_undone(self):
        with tempfile.TemporaryDirectory() as folder:
            manifest = self.make_project(folder); original = manifest.read_bytes(); backups = Path(folder) / "backups"
            changed = engine.update(manifest, engine.Release("2026.2", "2026.2", False, ""), backups, True)
            self.assertTrue(changed.status.startswith("updated to "))
            self.assertTrue(Path(changed.backup_path).is_file())
            restored = engine.undo_compatibility_target(manifest, Path(changed.backup_path), changed.target_last_tested, changed.target_manifest_hash, backups)
            self.assertTrue(restored.status.startswith("restored last tested NVDA from "))
            self.assertEqual(original, manifest.read_bytes())

    def test_failed_post_update_validation_restores_original_manifest(self):
        with tempfile.TemporaryDirectory() as folder:
            manifest = self.make_project(folder); original = manifest.read_bytes()
            with mock.patch.object(engine, "validate", side_effect=[[], ["simulated failure"]]):
                result = engine.update(manifest, engine.Release("2026.2", "2026.2", False, ""), Path(folder) / "backups", True)
            self.assertIn("original manifest was restored", result.status)
            self.assertEqual(original, manifest.read_bytes())
    def test_atomic_state_round_trip_and_corrupt_fallback(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"; engine.atomic_json_write(path, {"ok": True}); self.assertEqual({"ok": True}, engine.read_json(path)); path.write_text("{"); self.assertEqual({}, engine.read_json(path))
            path.write_text("[]"); self.assertEqual({}, engine.read_json(path))
    def test_invalid_version_fails_validation(self):
        with tempfile.TemporaryDirectory() as folder:
            manifest = self.make_project(folder, manifest=MANIFEST.replace("2025.4", "future")); self.assertTrue(any("Invalid NVDA version" in error for error in engine.validate(manifest)))
        for invalid in ("2026", "2026.2.3.4", "2026.-1", "junk2026.2"):
            with self.assertRaises(ValueError): engine.version_tuple(invalid)

    def test_compatibility_targets_are_official_unique_and_shared(self):
        choices = engine.compatibility_version_choices({
            "2026.2.0": {"experimental": False},
            "2026.2": {"experimental": False},
            "2026.1.1": {"experimental": False},
            "2026.1": {"experimental": False},
            "2025.3.3": {"experimental": False},
            "2027.1": {"experimental": True},
            "invalid": {},
        })
        self.assertEqual(1, sum(version == "2026.2" for version, _experimental in choices))
        self.assertEqual(("2027.1", True), choices[0])
        projects = [
            engine.CompatibilityTargetProject("one", "One", "one.ini", "2026.2", "2025.3.3", "", "main", False),
            engine.CompatibilityTargetProject("two", "Two", "two.ini", "2026.1.1", "2025.3.3", "", "release", False),
        ]
        self.assertEqual(
            ["2026.1", "2025.3.3"],
            [version for version, _experimental in engine.allowed_compatibility_targets(projects, choices)],
        )
        invalid = [engine.CompatibilityTargetProject("bad", "Bad", "bad.ini", "unknown", "2025.3.3", "", "main", False)]
        self.assertEqual([], engine.allowed_compatibility_targets(invalid, choices))

        stable = [engine.CompatibilityTargetProject("stable", "Stable", "stable.ini", "2028.1", "2025.3.3", "", "main", False)]
        beta = [engine.CompatibilityTargetProject("beta", "Beta", "beta.ini", "2028.1", "2025.3.3", "beta", "main", False)]
        self.assertNotIn("2027.1", [version for version, _experimental in engine.allowed_compatibility_targets(stable, choices)])
        self.assertIn("2027.1", [version for version, _experimental in engine.allowed_compatibility_targets(beta, choices)])

    def test_compatibility_target_requires_an_official_version_and_can_be_undone(self):
        with tempfile.TemporaryDirectory() as folder:
            manifest = self.make_project(folder)
            original = manifest.read_bytes()
            rejected = engine.downgrade(manifest, "2025.3", Path(folder) / "backups", {"2025.2": False})
            self.assertIn("not a recognized NVDA API version", rejected.status)
            self.assertEqual(original, manifest.read_bytes())
            experimental = engine.downgrade(manifest, "2025.3", Path(folder) / "backups", {"2025.3": True})
            self.assertIn("requires updateChannel beta or dev", experimental.status)
            self.assertEqual(original, manifest.read_bytes())
            changed = engine.downgrade(manifest, "2025.3", Path(folder) / "backups", {"2025.3": False})
            self.assertTrue(changed.status.startswith("set last tested NVDA from "))
            self.assertTrue(Path(changed.backup_path).is_file())
            self.assertEqual("2025.4", changed.previous_last_tested)
            self.assertEqual("2025.3", changed.target_last_tested)
            self.assertEqual("2025.1", engine.values(manifest)["minimumnvdaversion"])
            restored = engine.undo_compatibility_target(
                manifest, Path(changed.backup_path), changed.target_last_tested,
                changed.target_manifest_hash, Path(folder) / "backups",
            )
            self.assertTrue(restored.status.startswith("restored last tested NVDA from "))
            self.assertEqual(original, manifest.read_bytes())

    def test_compatibility_undo_never_overwrites_later_manifest_edits(self):
        with tempfile.TemporaryDirectory() as folder:
            manifest = self.make_project(folder)
            changed = engine.downgrade(manifest, "2025.3", Path(folder) / "backups", {"2025.3": False})
            manifest.write_text(manifest.read_text().replace("summary = Demo", "summary = Later edit"), encoding="utf-8")
            later = manifest.read_bytes()
            result = engine.undo_compatibility_target(
                manifest, Path(changed.backup_path), changed.target_last_tested,
                changed.target_manifest_hash, Path(folder) / "backups",
            )
            self.assertIn("changed after the compatibility operation", result.status)
            self.assertEqual(later, manifest.read_bytes())

    def test_failed_post_change_validation_restores_original_manifest(self):
        with tempfile.TemporaryDirectory() as folder:
            manifest = self.make_project(folder); original = manifest.read_bytes()
            with mock.patch.object(engine, "validate", side_effect=[[], ["simulated failure"]]):
                result = engine.downgrade(manifest, "2025.3", Path(folder) / "backups", {"2025.3": False})
            self.assertIn("original manifest was restored", result.status)
            self.assertEqual(original, manifest.read_bytes())

    def test_git_manifest_state_reports_branch_and_local_change(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); manifest = root / "manifest.ini"; manifest.write_text(MANIFEST, encoding="utf-8")
            commands = (
                ["git", "init", "-b", "main"],
                ["git", "config", "user.name", "Test User"],
                ["git", "config", "user.email", "test@example.invalid"],
                ["git", "add", "manifest.ini"],
                ["git", "commit", "-m", "Initial"],
            )
            for command in commands:
                subprocess.run(command, cwd=root, check=True, capture_output=True)
            self.assertEqual(("main", False), engine.git_manifest_state(manifest))
            manifest.write_text(MANIFEST.replace("summary = Demo", "summary = Changed"), encoding="utf-8")
            self.assertEqual(("main", True), engine.git_manifest_state(manifest))

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

    def test_official_nvda_api_versions_can_fall_back_to_saved_response(self):
        data = [{
            "description": "NVDA 2026.2",
            "apiVer": {"major": 2026, "minor": 2, "patch": 0},
            "backCompatTo": {"major": 2026, "minor": 1, "patch": 0},
        }]
        with tempfile.TemporaryDirectory() as folder:
            cache = Path(folder) / "apiVersions.json"; cache.write_text(json.dumps(data), encoding="utf-8")
            with mock.patch.object(publisher.urllib.request, "urlopen", side_effect=OSError("offline")):
                versions = publisher.nvda_api_versions(cache)
        self.assertIn("2026.2", versions)
        self.assertFalse(versions["2026.2"]["experimental"])

    def test_valid_official_versions_survive_a_cache_write_failure(self):
        data = [{"apiVer": {"major": 2026, "minor": 2, "patch": 0}, "experimental": False}]
        with tempfile.TemporaryDirectory() as folder:
            with mock.patch.object(publisher.urllib.request, "urlopen", return_value=io.BytesIO(json.dumps(data).encode())), mock.patch.object(publisher.tempfile, "mkstemp", side_effect=OSError("disk full")):
                versions = publisher.nvda_api_versions(Path(folder) / "apiVersions.json")
        self.assertIn("2026.2", versions)

    def test_release_package_ai_disclosure_audit_checks_shipped_text_only(self):
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w") as archive:
            archive.writestr("manifest.ini", "name = demo\nversion = 2026.2\n")
            archive.writestr("globalPlugins/demo.py", "# This code was generated by " + "ChatGPT\n")
            archive.writestr("doc/en/readme.html", "<p>Built with " + "A" + "I assistance.</p>\n")
            archive.writestr("sounds/tone.wav", ("A" + "I-generated binary marker is ignored").encode())
        with zipfile.ZipFile(io.BytesIO(payload.getvalue())) as archive:
            findings = publisher._ai_disclosure_issues(archive)
        self.assertEqual(2, len(findings))
        self.assertTrue(any("globalPlugins/demo.py, line 1" in finding for finding in findings))
        self.assertTrue(any("doc/en/readme.html, line 1" in finding for finding in findings))

    def test_product_names_are_not_mistaken_for_ai_disclosures(self):
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w") as archive:
            archive.writestr("doc/en/readme.html", "Improves ChatGPT and OpenAI product accessibility.")
        with zipfile.ZipFile(io.BytesIO(payload.getvalue())) as archive:
            self.assertEqual([], publisher._ai_disclosure_issues(archive))

    def test_source_disclosure_audit_checks_repository_only_text(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "workflow.yml").write_text("note: built with " + "A" + "I\n", encoding="utf-8")
            (root / "sound.wav").write_bytes(("A" + "I").encode())
            findings = publisher._source_disclosure_issues(root, False)
        self.assertEqual(1, len(findings))
        self.assertIn("workflow.yml, line 1", findings[0])

    def test_source_disclosure_audit_ignores_detector_tests_and_definition(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "tests").mkdir()
            (root / "tools").mkdir()
            (root / "tests" / "test_validator.py").write_text(
                'blocked = "generated by ' + 'ChatGPT"\n', encoding="utf-8",
            )
            (root / "tools" / "addon_store_metadata.py").write_text(
                'PATTERN = r"written by ' + 'OpenAI"\n', encoding="utf-8",
            )
            (root / "readme.md").write_text("ordinary project documentation\n", encoding="utf-8")
            self.assertEqual([], publisher._source_disclosure_issues(root, False))

    def test_store_readiness_reports_every_guideline_issue(self):
        report = publisher.ProjectPublishInfo(
            "id", "Demo", "manifest.ini", ".", "2026.2", "Demo", "Justin Coffin",
            "https://github.com/jcoffin1/demo", 0, True, 0, "2026.2",
            "https://github.com/jcoffin1/demo/releases/download/v2026.2/demo.nvda-addon",
            True, True, "", ("Automated-authorship wording in release package: readme.md, line 2",),
        )
        self.assertEqual(list(report.store_guideline_issues), publisher.store_readiness_reasons(report))

    def test_store_readiness_does_not_duplicate_an_outdated_release_mismatch(self):
        report = publisher.ProjectPublishInfo(
            "id", "Demo", "manifest.ini", ".", "2026.2.33", "Demo", "Justin Coffin",
            "https://github.com/jcoffin1/demo", 0, True, 0, "2026.2.24",
            "https://github.com/jcoffin1/demo/releases/download/v2026.2.24/demo.nvda-addon",
            True, False, "release package version 2026.2.24 does not match local version 2026.2.33",
        )
        reasons = publisher.store_readiness_reasons(report)
        self.assertEqual(["GitHub Release 2026.2.24 does not match local version 2026.2.33"], reasons)

    def test_vendored_yaml_imports_without_site_packages(self):
        module_folder = Path(__file__).parent / "globalPlugins" / "addonDeveloperUpdater"
        script = (
            "import sys; "
            f"sys.path.insert(0, {str(module_folder)!r}); "
            "import publisher; "
            "template = publisher.GitHubIssueTemplate('.github/ISSUE_TEMPLATE/bug.yml', 'bug.yml', content='name: Bug\\ndescription: Report\\nbody: []\\n'); "
            "assert publisher.parse_issue_template_document(template).data['name'] == 'Bug'"
        )
        completed = subprocess.run(
            [sys.executable, "-S", "-c", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)

    def test_external_scan_worker_is_present_and_packaged(self):
        root = Path(__file__).parent
        worker = root / "globalPlugins" / "addonDeveloperUpdater" / "worker.ps1"
        self.assertTrue(worker.is_file())
        plugin = (worker.parent / "__init__.py").read_text(encoding="utf-8")
        self.assertIn('if not workerPath.is_file()', plugin)
        item = {"name": "addonDeveloperUpdater", "version": "2026.2.24", "manifest": str(root / "manifest.ini")}
        with tempfile.TemporaryDirectory() as folder:
            package = publisher.build_package(item, Path(folder))
            with zipfile.ZipFile(package) as archive:
                self.assertIn("globalPlugins/addonDeveloperUpdater/worker.ps1", archive.namelist())
                self.assertIn("globalPlugins/addonDeveloperUpdater/_vendor/yaml/__init__.py", archive.namelist())
                self.assertIn("globalPlugins/addonDeveloperUpdater/_vendor/PyYAML-LICENSE.txt", archive.namelist())
                self.assertIn("COPYING.txt", archive.namelist())

    @unittest.skipUnless(shutil.which("powershell.exe"), "NVDA's scan worker requires Windows PowerShell")
    def test_external_scan_worker_directly_discovers_only_developer_manifests(self):
        worker = Path(__file__).parent / "globalPlugins" / "addonDeveloperUpdater" / "worker.ps1"
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); project = root / "project"; project.mkdir(); (project / ".git").mkdir(); manifest = project / "manifest.ini"; manifest.write_text(MANIFEST, encoding="utf-8")
            ordinary = root / "ordinary"; ordinary.mkdir(); (ordinary / "manifest.ini").write_text(MANIFEST, encoding="utf-8")
            request = root / "request.json"; output = root / "output.json"; request.write_text(json.dumps({"mode": "manual", "fullSystem": False, "roots": [str(root)], "manifestPaths": []}), encoding="utf-8")
            completed = subprocess.run(["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(worker), "-RequestPath", str(request), "-OutputPath", str(output)], capture_output=True, text=True, timeout=20)
            result = engine.read_json(output)
            bounded_output = root / "bounded.json"; request.write_text(json.dumps({"mode": "manual", "fullSystem": False, "roots": [str(root)], "manifestPaths": [], "maxDirectories": 1, "maxSeconds": 5}), encoding="utf-8")
            bounded = subprocess.run(["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(worker), "-RequestPath", str(request), "-OutputPath", str(bounded_output)], capture_output=True, text=True, timeout=20)
            bounded_result = engine.read_json(bounded_output)
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(result["ok"])
        self.assertEqual([str(manifest.resolve())], result["manifests"])
        self.assertFalse(result["truncated"])
        self.assertEqual(0, bounded.returncode, bounded.stderr)
        self.assertEqual([], bounded_result["manifests"])
        self.assertTrue(bounded_result["truncated"])
        self.assertNotIn("Invoke-RestMethod", worker.read_text(encoding="utf-8-sig"))

    @unittest.skipUnless(shutil.which("powershell.exe"), "NVDA's scan worker requires Windows PowerShell")
    def test_external_scan_worker_reports_missing_background_manifests(self):
        worker = Path(__file__).parent / "globalPlugins" / "addonDeveloperUpdater" / "worker.ps1"
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); existing = self.make_project(root, "existing"); missing = root / "gone" / "manifest.ini"
            request = root / "request.json"; output = root / "result.json"
            request.write_text(json.dumps({"mode": "background", "manifestPaths": [str(existing), str(missing)]}), encoding="utf-8")
            completed = subprocess.run(["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(worker), "-RequestPath", str(request), "-OutputPath", str(output)], capture_output=True, text=True, timeout=30)
            result = engine.read_json(output)
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual([str(existing)], result["manifests"])
        self.assertEqual([str(missing)], result["unavailableManifestPaths"])

    def test_runtime_bounds_external_worker_and_safely_handles_progress_close(self):
        plugin = (Path(__file__).parent / "globalPlugins" / "addonDeveloperUpdater" / "__init__.py").read_text(encoding="utf-8")
        self.assertIn("engine.wait_for_worker(self._workerProcess)", plugin)
        self.assertIn("self._progressDialog = None\n            dialog.Destroy()", plugin)
        self.assertIn("Progress window closed. The operation is still running in the background.", plugin)

    def test_documentation_does_not_advertise_nonexistent_automatic_changes(self):
        documentation = (Path(__file__).parent / "doc" / "en" / "readme.html").read_text(encoding="utf-8")
        self.assertNotIn("opt-in automatic changes", documentation)
        self.assertIn("automatic manifest changes are not supported", documentation)

    def test_branch_targeted_release_does_not_use_unreliable_no_commit_flag(self):
        target = "af05c02f6b8d790fe6f41d3be5fd9f3fd5c27124"
        arguments = publisher.release_create_arguments(
            "gh", "v2026.2.5", Path("package.nvda-addon"),
            "jcoffin1/chatgpt-desktop-access", target, "ChatGPT Desktop Access",
        )
        self.assertIn("--target", arguments)
        self.assertIn(target, arguments)
        self.assertNotIn("--generate-notes", arguments)
        self.assertIn("--notes", arguments)
        self.assertIn("Release package for ChatGPT Desktop Access 2026.2.5.", arguments)
        self.assertIn("ChatGPT Desktop Access 2026.2.5", arguments)
        self.assertNotIn("--fail-on-no-commits", arguments)
        self.assertNotIn("--prerelease", arguments)
        self.assertIn("--prerelease", publisher.release_create_arguments(
            "gh", "v2026.3-alpha", Path("package.nvda-addon"),
            "jcoffin1/chatgpt-desktop-access", target, "ChatGPT Desktop Access", True,
        ))

    def test_public_github_text_is_checked_before_store_submission(self):
        responses = iter((
            '{"description":"Accessible NVDA add-on"}',
            '{"name":"Demo 1.0","body":"generated by ' + 'ChatGPT"}',
            '[{"number":7,"title":"Release prep","body":"written by ' + 'OpenAI"}]',
        ))
        with mock.patch.object(publisher, "_run", side_effect=lambda *_args, **_kwargs: next(responses)) as run:
            findings = publisher._github_disclosure_issues(
                "gh", "https://github.com/jcoffin1/demo", "v1.0",
            )
        self.assertEqual(2, len(findings))
        self.assertTrue(any("GitHub Release v1.0 notes" in finding for finding in findings))
        self.assertTrue(any("GitHub pull request 7 description" in finding for finding in findings))
        commands = [call.args[0] for call in run.call_args_list]
        self.assertTrue(any(command[1:3] == ["pr", "list"] and "1000" in command for command in commands))

    def test_release_package_uses_project_audited_builder_when_available(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "project"
            root.mkdir()
            manifest = root / "manifest.ini"
            manifest.write_text(MANIFEST, encoding="utf-8")
            (root / "build.ps1").write_text("# project builder\n", encoding="utf-8")
            def run_builder(arguments, **kwargs):
                outputs = root / "outputs"
                outputs.mkdir()
                (outputs / "public-name-1.0.nvda-addon").write_bytes(b"audited")
                return ""
            with mock.patch.object(publisher.shutil, "which", return_value="pwsh"), mock.patch.object(publisher, "_run", side_effect=run_builder) as run:
                package = publisher.build_release_package(
                    {"name": "demo", "version": "1.0", "manifest": str(manifest)},
                    Path(folder) / "packages",
                )
            self.assertEqual("public-name-1.0.nvda-addon", package.name)
            self.assertEqual(b"audited", package.read_bytes())
            self.assertIn("-ExecutionPolicy", run.call_args.args[0])
            self.assertIn("Bypass", run.call_args.args[0])

    def test_project_builder_cannot_reuse_a_stale_package(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "project"
            outputs = root / "outputs"
            outputs.mkdir(parents=True)
            manifest = root / "manifest.ini"
            manifest.write_text(MANIFEST, encoding="utf-8")
            (root / "build.ps1").write_text("# broken project builder\n", encoding="utf-8")
            (outputs / "old-1.0.nvda-addon").write_bytes(b"stale")
            with mock.patch.object(publisher.shutil, "which", return_value="pwsh"), mock.patch.object(publisher, "_run", return_value=""):
                with self.assertRaisesRegex(RuntimeError, "produced no package"):
                    publisher.build_release_package(
                        {"name": "demo", "version": "1.0", "manifest": str(manifest)},
                        Path(folder) / "packages",
                    )

    def test_new_release_builds_are_validated_before_github_push(self):
        report = publisher.ProjectPublishInfo(
            "id", "Demo", "manifest.ini", ".", "2026.2.1", "Demo", "Justin Coffin",
            "https://github.com/jcoffin1/demo", 1, True, 0, "2026.2",
        )
        current = publisher.ProjectPublishInfo(
            "current", "Current", "manifest.ini", ".", "2026.2", "Current", "Justin Coffin",
            "https://github.com/jcoffin1/current", 1, True, 0, "2026.2",
        )
        with mock.patch.object(publisher, "build_release_package") as build:
            publisher.validate_publish_builds([report, current])
        build.assert_called_once()
        self.assertEqual("Demo", build.call_args.args[0]["name"])

    def test_runtime_validates_release_builds_before_push(self):
        plugin = (Path(__file__).parent / "globalPlugins" / "addonDeveloperUpdater" / "__init__.py").read_text(encoding="utf-8")
        validate_index = plugin.index("publisher.validate_publish_builds(reports")
        push_index = plugin.index("publisher.push(reports", validate_index)
        self.assertLess(validate_index, push_index)

    def test_cancel_command_tracks_a_scan_instead_of_the_shared_operation_lock(self):
        plugin = (Path(__file__).parent / "globalPlugins" / "addonDeveloperUpdater" / "__init__.py").read_text(encoding="utf-8")
        start = plugin.index("def script_cancelAddonProjectScan")
        end = plugin.index("def script_submitUpdatedAddons", start)
        command = plugin[start:end]
        self.assertIn("self._scanActive.is_set()", command)
        self.assertNotIn("self._scanLock.locked()", command)

    def test_compatibility_ui_uses_official_versions_confirmation_and_persistent_undo(self):
        plugin = (Path(__file__).parent / "globalPlugins" / "addonDeveloperUpdater" / "__init__.py").read_text(encoding="utf-8")
        self.assertIn("publisher.nvda_api_versions(self._apiVersionCachePath)", plugin)
        self.assertIn("Official older NVDA target shared by every selected add-on", plugin)
        self.assertIn("Confirm older compatibility target", plugin)
        self.assertIn("Undo the most recent compatibility target changes", plugin)
        self.assertIn('state.update({"releaseTag": release.tag', plugin)
        self.assertIn("if changed:\n                state[\"compatibilityUndo\"]", plugin)

    def test_update_check_ui_requires_approval_and_reports_status(self):
        plugin = (Path(__file__).parent / "globalPlugins" / "addonDeveloperUpdater" / "__init__.py").read_text(encoding="utf-8")
        self.assertIn("class ProjectApprovalDialog", plugin)
        self.assertIn("Confirm approved add-on projects", plugin)
        self.assertIn("Report add-on update-check status", plugin)
        self.assertIn("engine.latest_release", plugin)
        self.assertIn("engine.notification_state", plugin)
        self.assertIn("Confirm add-on compatibility updates", plugin)
        self.assertIn("Undo the most recent discovered compatibility updates", plugin)
        self.assertIn('state["updateUndo"]', plugin)
        self.assertIn('state.get("lastDiscoveryAt")', plugin)
        self.assertNotIn("if manual: approved.update", plugin)

    def test_store_submission_remains_a_manual_browser_action(self):
        plugin = (Path(__file__).parent / "globalPlugins" / "addonDeveloperUpdater" / "__init__.py").read_text(encoding="utf-8")
        self.assertIn("Review every field and submit each form manually", plugin)
        self.assertIn("the updater never submits a store issue or pull request", plugin)
        self.assertNotIn('gh", "issue", "create', plugin)
        self.assertNotIn('gh", "pr", "create', plugin)

    def test_owned_fork_remote_is_preferred_over_upstream_origin(self):
        remotes = {
            "origin": "https://github.com/samtupy/b32tts_wrapper.git",
            "fork": "https://github.com/jcoffin1/bestspeech-tts-for-nvda.git",
        }
        self.assertEqual(
            ("fork", remotes["fork"]),
            publisher._select_publish_remote(remotes, "jcoffin1"),
        )

    def test_upstream_origin_remains_fallback_without_an_owned_remote(self):
        remotes = {"origin": "https://github.com/samtupy/b32tts_wrapper.git"}
        self.assertEqual(("origin", remotes["origin"]), publisher._select_publish_remote(remotes, "jcoffin1"))

    def test_generated_build_and_python_cache_paths_are_not_published(self):
        self.assertTrue(publisher._is_generated_path("build-local/package/file.dll"))
        self.assertTrue(publisher._is_generated_path("synthDrivers/__pycache__/driver.cpython-313.pyc"))
        self.assertTrue(publisher._is_generated_path("outputs/demo.nvda-addon"))
        self.assertFalse(publisher._is_generated_path("synthDrivers/bestspeech.py"))
        self.assertFalse(publisher._is_generated_path("docs/building.md"))

    def test_github_addon_repository_lookup_filters_and_paginates(self):
        first_page = {
            "data": {"viewer": {"login": "jcoffin1", "repositories": {
                "nodes": [
                    {"name": "demo", "nameWithOwner": "jcoffin1/demo", "url": "https://github.com/jcoffin1/demo", "description": "Demo add-on", "isPrivate": False, "isArchived": False, "releases": {"nodes": [
                        {"tagName": "v2.0", "isDraft": True, "isPrerelease": False, "releaseAssets": {"nodes": [{"name": "demo-2.0.nvda-addon", "downloadUrl": "https://github.com/jcoffin1/demo/releases/download/v2.0/demo-2.0.nvda-addon", "size": 20}]}},
                        {"tagName": "v1.0", "isDraft": False, "isPrerelease": False, "releaseAssets": {"nodes": [{"name": "demo-1.0.nvda-addon", "downloadUrl": "https://github.com/jcoffin1/demo/releases/download/v1.0/demo-1.0.nvda-addon", "size": 10}]}},
                    ]}},
                    {"name": "ordinary", "nameWithOwner": "jcoffin1/ordinary", "url": "https://github.com/jcoffin1/ordinary", "description": "No add-on package", "isPrivate": False, "isArchived": False, "releases": {"nodes": [{"tagName": "v1", "isDraft": False, "isPrerelease": False, "releaseAssets": {"nodes": [{"name": "source.zip", "downloadUrl": "https://github.com/jcoffin1/ordinary/releases/download/v1/source.zip", "size": 30}]}}]}},
                ],
                "pageInfo": {"hasNextPage": True, "endCursor": "next-page"},
            }}},
        }
        second_page = {
            "data": {"viewer": {"login": "jcoffin1", "repositories": {
                "nodes": [
                    {"name": "template-addon", "nameWithOwner": "jcoffin1/template-addon", "url": "https://github.com/jcoffin1/template-addon", "description": "Template project", "isPrivate": True, "isArchived": False, "releases": {"nodes": [{"tagName": "v3beta1", "isDraft": False, "isPrerelease": True, "releaseAssets": {"nodes": [{"name": "template-3beta1.nvda-addon", "downloadUrl": "https://github.com/jcoffin1/template-addon/releases/download/v3beta1/template-3beta1.nvda-addon", "size": 40}]}}]}},
                ],
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            }}},
        }
        responses = iter(("authenticated", json.dumps(first_page), json.dumps(second_page)))
        with mock.patch.object(publisher, "gh_path", return_value="gh"), mock.patch.object(publisher, "_run", side_effect=lambda *_args, **_kwargs: next(responses)) as run:
            owner, repositories = publisher.github_addon_repositories()
        self.assertEqual("jcoffin1", owner)
        self.assertEqual(["jcoffin1/demo", "jcoffin1/template-addon"], [repository.full_name for repository in repositories])
        self.assertEqual("v1.0", repositories[0].release_tag)
        self.assertTrue(repositories[0].download_url.endswith("demo-1.0.nvda-addon"))
        self.assertTrue(repositories[1].private)
        self.assertTrue(repositories[1].prerelease)
        self.assertIn("endCursor=next-page", run.call_args_list[-1].args[0])

    def test_github_addon_repository_lookup_reports_missing_authentication(self):
        with mock.patch.object(publisher, "gh_path", return_value="gh"), mock.patch.object(publisher, "_run", side_effect=RuntimeError("gh failed: not logged into any GitHub hosts")):
            with self.assertRaises(publisher.AuthenticationRequired): publisher.github_addon_repositories()

    def test_download_url_gesture_and_clipboard_action_are_present(self):
        plugin = (Path(__file__).parent / "globalPlugins" / "addonDeveloperUpdater" / "__init__.py").read_text(encoding="utf-8")
        self.assertIn('gesture="kb:NVDA+alt+shift+f"', plugin)
        self.assertIn("api.copyToClip(repository.download_url)", plugin)

    def test_github_addon_project_catalog_includes_unreleased_projects(self):
        page = {"data": {"viewer": {"login": "owner", "repositories": {
            "nodes": [
                {"name": "addon", "nameWithOwner": "owner/addon", "url": "https://github.com/owner/addon", "description": "", "isPrivate": False, "isArchived": False, "hasIssuesEnabled": True, "defaultBranchRef": {"name": "main"}, "buildVariables": {"__typename": "Blob"}},
                {"name": "ordinary", "nameWithOwner": "owner/ordinary", "url": "https://github.com/owner/ordinary", "description": "", "isPrivate": False, "isArchived": False, "hasIssuesEnabled": True, "defaultBranchRef": {"name": "main"}},
            ],
            "pageInfo": {"hasNextPage": False, "endCursor": None},
        }}}}
        responses = iter(("authenticated", json.dumps(page)))
        with mock.patch.object(publisher, "gh_path", return_value="gh"), mock.patch.object(publisher, "_run", side_effect=lambda *_args, **_kwargs: next(responses)):
            owner, repositories = publisher.github_addon_projects()
        self.assertEqual("owner", owner)
        self.assertEqual(["owner/addon"], [repository.full_name for repository in repositories])
        self.assertEqual("main", repositories[0].default_branch)
        self.assertTrue(repositories[0].issues_enabled)

    def test_issue_template_listing_accepts_only_direct_markdown_and_yaml_files(self):
        repository = publisher.GitHubAddonRepository("addon", "owner/addon", "https://github.com/owner/addon", default_branch="main")
        tree = {"truncated": False, "tree": [
            {"type": "blob", "path": ".github/ISSUE_TEMPLATE/bug.yml"},
            {"type": "blob", "path": ".github/ISSUE_TEMPLATE/config.yaml"},
            {"type": "blob", "path": ".github/ISSUE_TEMPLATE/legacy.md"},
            {"type": "blob", "path": ".github/ISSUE_TEMPLATE/readme.txt"},
            {"type": "blob", "path": ".github/ISSUE_TEMPLATE/nested/hidden.yml"},
            {"type": "blob", "path": "docs/bug.yml"},
        ]}
        with mock.patch.object(publisher, "gh_path", return_value="gh"), mock.patch.object(publisher, "_run", return_value=json.dumps(tree)):
            templates = publisher.github_issue_templates(repository)
        self.assertEqual(["bug.yml", "config.yaml", "legacy.md"], [template.name for template in templates])

    def test_issue_template_load_preserves_revision_and_utf8_content(self):
        repository = publisher.GitHubAddonRepository("addon", "owner/addon", "https://github.com/owner/addon", default_branch="main")
        template = publisher.GitHubIssueTemplate(".github/ISSUE_TEMPLATE/bug.yml", "bug.yml")
        response = {"encoding": "base64", "sha": "abc123", "content": publisher.base64.b64encode("name: Bug\ndescription: Café\n".encode()).decode()}
        with mock.patch.object(publisher, "github_issue_templates", return_value=[template]), mock.patch.object(publisher, "gh_path", return_value="gh"), mock.patch.object(publisher, "_run", return_value=json.dumps(response)):
            loaded = publisher.load_github_issue_template(repository, template)
        self.assertEqual("abc123", loaded.sha)
        self.assertIn("Café", loaded.content)

    def test_issue_template_update_uses_revision_and_stdin_json(self):
        repository = publisher.GitHubAddonRepository("addon", "owner/addon", "https://github.com/owner/addon", default_branch="main")
        template = publisher.GitHubIssueTemplate(".github/ISSUE_TEMPLATE/bug.yml", "bug.yml", "abc123", "old")
        with mock.patch.object(publisher, "gh_path", return_value="gh"), mock.patch.object(publisher, "_run", return_value="") as run:
            publisher.update_github_issue_template(repository, template, "name: Updated\n")
        arguments = run.call_args.args[0]; request = json.loads(run.call_args.kwargs["input_text"])
        self.assertEqual(["--input", "-"], arguments[-2:])
        self.assertEqual("abc123", request["sha"])
        self.assertEqual("main", request["branch"])
        self.assertEqual("name: Updated\n", publisher.base64.b64decode(request["content"]).decode())

    def test_issue_template_delete_uses_revision_and_default_branch(self):
        repository = publisher.GitHubAddonRepository("addon", "owner/addon", "https://github.com/owner/addon", default_branch="main")
        template = publisher.GitHubIssueTemplate(".github/ISSUE_TEMPLATE/bug.yml", "bug.yml", "abc123", "name: Bug\n")
        with mock.patch.object(publisher, "gh_path", return_value="gh"), mock.patch.object(publisher, "_run", return_value="") as run:
            publisher.delete_github_issue_template(repository, template)
        arguments = run.call_args.args[0]; request = json.loads(run.call_args.kwargs["input_text"])
        self.assertIn("DELETE", arguments)
        self.assertEqual("abc123", request["sha"])
        self.assertEqual("main", request["branch"])
        self.assertNotIn("content", request)

    def test_yaml_issue_form_round_trip_preserves_unknown_fields(self):
        content = "name: Bug report\ndescription: Report a problem\ntitle: '[Bug] '\nlabels:\n- bug\nbody:\n- type: textarea\n  id: details\n  attributes:\n    label: What happened?\n    custom: keep me\n  validations:\n    required: true\nx-extra: preserved\n"
        template = publisher.GitHubIssueTemplate(".github/ISSUE_TEMPLATE/bug.yml", "bug.yml", "abc", content)
        document = publisher.parse_issue_template_document(template)
        document.data["description"] = "Updated description"
        rendered = publisher.render_issue_template_document(document)
        reparsed = publisher.parse_issue_template_document(template, rendered)
        self.assertEqual("Updated description", reparsed.data["description"])
        self.assertEqual("keep me", reparsed.data["body"][0]["attributes"]["custom"])
        self.assertEqual("preserved", reparsed.data["x-extra"])

    def test_yaml_issue_form_validation_rejects_duplicate_ids_and_empty_options(self):
        template = publisher.GitHubIssueTemplate(".github/ISSUE_TEMPLATE/bug.yml", "bug.yml")
        document = publisher.IssueTemplateDocument("yaml", {"name": "Bug", "description": "Report", "body": [
            {"type": "input", "id": "details", "attributes": {"label": "Details"}},
            {"type": "dropdown", "id": "details", "attributes": {"label": "Version", "options": []}},
        ]})
        with self.assertRaisesRegex(ValueError, "used more than once"): publisher.validate_issue_template_document(template, document)
        document.data["body"][1]["id"] = "version"
        with self.assertRaisesRegex(ValueError, "at least one option"): publisher.validate_issue_template_document(template, document)

    def test_yaml_issue_form_treats_yes_option_as_visible_text(self):
        content = "name: Compatibility\ndescription: Check compatibility\nbody:\n- type: dropdown\n  id: workedBefore\n  attributes:\n    label: Did this work before?\n    options:\n    - Yes\n    - No\n"
        template = publisher.GitHubIssueTemplate(".github/ISSUE_TEMPLATE/compatibility.yml", "compatibility.yml", content=content)
        document = publisher.parse_issue_template_document(template)
        self.assertEqual(["Yes", "No"], document.data["body"][0]["attributes"]["options"])
        publisher.validate_issue_template_document(template, document)
        rendered = publisher.render_issue_template_document(document)
        self.assertIn("- 'Yes'", rendered)

    def test_markdown_issue_template_form_preserves_body(self):
        content = "---\nname: Bug\nabout: Report a bug\ntitle: ''\nlabels: bug\nassignees: ''\n---\n## Steps\nTell us what happened.\n"
        template = publisher.GitHubIssueTemplate(".github/ISSUE_TEMPLATE/bug.md", "bug.md", "abc", content)
        document = publisher.parse_issue_template_document(template)
        document.data["about"] = "Updated"
        rendered = publisher.render_issue_template_document(document)
        self.assertIn("about: Updated", rendered)
        self.assertTrue(rendered.endswith("## Steps\nTell us what happened.\n"))

    def test_issue_and_template_gestures_are_registered(self):
        plugin = (Path(__file__).parent / "globalPlugins" / "addonDeveloperUpdater" / "__init__.py").read_text(encoding="utf-8")
        self.assertIn('gesture="kb:NVDA+alt+shift+i"', plugin)
        self.assertIn('gesture="kb:NVDA+alt+shift+t"', plugin)
        self.assertIn("Save issue template to GitHub", plugin)
        self.assertIn("Delete GitHub issue template", plugin)
        self.assertIn("dialog.getContent()", plugin)
        self.assertIn("Issue template display &name, shown in GitHub's New Issue menu", plugin)
        self.assertIn("Short answer, one line", plugin)
        self.assertIn("Require an answer before the issue can be submitted", plugin)
        self.assertIn("control.SetName(label.replace", plugin)
        self.assertIn("self.editor.SetName", plugin)
        self.assertIn("self.questionList.SetName", plugin)
        self.assertIn("Markdown means ordinary text with optional formatting symbols", plugin)
        self.assertIn("# for headings", plugin)
        self.assertIn("template.sha", (Path(__file__).parent / "globalPlugins" / "addonDeveloperUpdater" / "publisher.py").read_text(encoding="utf-8"))

if __name__ == "__main__": unittest.main()
