import codecs, io, json, os, shutil, stat, subprocess, tempfile, unittest, urllib.error, urllib.parse, zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent / "globalPlugins" / "addonDeveloperUpdater"))
import engine
import publisher

MANIFEST = "name = demo\nsummary = Demo\nversion = 1.0\nminimumNVDAVersion = 2025.1\nlastTestedNVDAVersion = 2025.4\n"
CURRENT_RELEASE_FAMILY = "2026.2"
PLUGIN_FOLDER = Path(__file__).parent / "globalPlugins" / "addonDeveloperUpdater"

def plugin_source():
    """Return the NVDA-dependent plugin code, including its separate dialog module."""
    return "\n".join((PLUGIN_FOLDER / name).read_text(encoding="utf-8") for name in ("__init__.py", "dialogs.py"))

WORKER = PLUGIN_FOLDER / "worker.ps1"

def run_scan_worker(request: dict) -> dict:
    """Run NVDA's real discovery worker on a request and return its result."""
    with tempfile.TemporaryDirectory() as folder:
        request_path = Path(folder) / "request.json"; output_path = Path(folder) / "result.json"
        request_path.write_text(json.dumps(request), encoding="utf-8")
        completed = subprocess.run(["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(WORKER), "-RequestPath", str(request_path), "-OutputPath", str(output_path)], capture_output=True, text=True, timeout=60)
        if completed.returncode: raise AssertionError(completed.stderr or completed.stdout)
        return engine.read_json(output_path)

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

    def test_selectable_releases_keep_stable_beta_rc_and_alpha_separate(self):
        items = [
            {"tag_name": "release-2026.2.0", "prerelease": False},
            {"tag_name": "release-2026.3beta9", "prerelease": True},
            {"tag_name": "release-2026.3beta11", "prerelease": True},
            {"tag_name": "release-2026.3rc1", "prerelease": True},
            {"tag_name": "release-2026.2beta8", "prerelease": True},
        ]
        alpha = engine.Release("alpha-60000,abcdef", "2026.4", True, "https://example.invalid/alpha", build_version="2026.4.0.60000")
        choices = engine._release_choices(items, True, alpha, checked_at="now")
        self.assertEqual(["stable", "alpha", "rc", "beta"], [engine.release_channel(item) for item in choices])
        self.assertEqual("2026.3beta11", choices[-1].tag)
        self.assertNotIn("2026.2beta8", [item.tag for item in choices])
        self.assertEqual(["2026.2.0"], [item.tag for item in engine._release_choices(items, False)])

    def test_available_release_choice_cache_preserves_exact_build_metadata(self):
        api = io.BytesIO(json.dumps([
            {"tag_name": "release-2026.2.0", "prerelease": False, "html_url": "https://example.invalid/stable"},
            {"tag_name": "release-2026.3beta2", "prerelease": True, "html_url": "https://example.invalid/beta"},
        ]).encode()); api.headers = {}
        alpha = engine.Release("alpha-57626,71bae80b", "2026.4", True, "https://example.invalid/alpha", build_version="2026.4.0.57626")
        with tempfile.TemporaryDirectory() as folder:
            cache = Path(folder) / "release.json"
            with mock.patch.object(engine.urllib.request, "urlopen", return_value=api), mock.patch.object(engine, "_latest_alpha_release", return_value=alpha):
                choices = engine.available_releases(True, cache)
            with mock.patch.object(engine.urllib.request, "urlopen", side_effect=OSError("offline")), mock.patch.object(engine.time, "sleep"):
                cached = engine.available_releases(True, cache)
        self.assertEqual([item.tag for item in choices], [item.tag for item in cached])
        self.assertEqual("2026.4.0.57626", cached[1].build_version)
        self.assertTrue(all(item.source == "cached" for item in cached))
        self.assertEqual(cached[1], engine.matching_release(choices[1], cached))

    def test_available_releases_try_official_feed_before_stale_cache(self):
        feed = io.BytesIO(b'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><entry><title>release-2026.2</title><link href="https://example.invalid/current"/></entry></feed>'); feed.headers = {}
        stale = engine.Release("2026.1", "2026.1", False, "https://example.invalid/stale")
        with tempfile.TemporaryDirectory() as folder:
            cache = Path(folder) / "release.json"; engine.atomic_json_write(cache, {"availableReleases": [engine.asdict(stale)]})
            with mock.patch.object(engine.urllib.request, "urlopen", side_effect=[OSError("API offline"), OSError("API offline"), feed]), mock.patch.object(engine.time, "sleep"):
                choices = engine.available_releases(False, cache)
        self.assertEqual(["2026.2"], [item.tag for item in choices])
        self.assertEqual("release feed", choices[0].source)

    def test_manual_review_survives_background_release_state_changes(self):
        release = engine.Release("2026.3beta2", "2026.3", True, "https://example.invalid/beta", "live", "now", "2026.3beta2")
        pending = [
            engine.ProjectResult("one", "One", "C:/one/manifest.ini", "update available: 2026.2 to 2026.3"),
            engine.ProjectResult("current", "Current", "C:/current/manifest.ini", "current"),
        ]
        state = {}
        engine.save_manual_review(state, release, pending)
        state.update(releaseTag="alpha-99999,abcdef", manifestVersion="2027.1", prerelease=True)
        saved_release, saved_results = engine.load_manual_review(state)
        self.assertEqual("2026.3beta2", saved_release.tag)
        self.assertEqual(["one"], [result.project_id for result in saved_results])
        engine.finish_manual_review_items(state, {"one"})
        self.assertNotIn("manualUpdateReview", state)

    def test_manual_review_merge_keeps_unselected_projects_and_rejects_corrupt_state(self):
        release = engine.Release("2026.3beta2", "2026.3", True, "")
        state = {}
        engine.save_manual_review(state, release, [engine.ProjectResult("one", "One", "one.ini", "update available: 2026.2 to 2026.3")])
        engine.save_manual_review(state, release, [engine.ProjectResult("two", "Two", "two.ini", "update available: 2026.1 to 2026.3")], merge=True)
        _saved_release, results = engine.load_manual_review(state)
        self.assertEqual({"one", "two"}, {result.project_id for result in results})
        state["manualUpdateReview"]["release"]["unexpected"] = True
        self.assertEqual((None, []), engine.load_manual_review(state))

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
        self.assertEqual("2026.3.0.57626", release.build_version)
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
            manifest = self.make_project(folder)
            result = engine.update(manifest, engine.Release("2026.2beta1", "2026.2", True, ""), Path(folder) / "backups")
            self.assertTrue(result.status.startswith("update available")); self.assertIn("2025.4", manifest.read_text())
    def test_development_roots_include_codex_cloud_storage_and_configured_folders(self):
        home = Path("C:/Users/Example")
        roots = engine.development_roots(" D:/Extra ; ;E:/More", home, {"OneDrive": "C:/Users/Example/OneDrive"})
        self.assertIn(os.path.normcase(str(home / "Documents" / "Codex")), roots)
        self.assertIn(os.path.normcase("C:/Users/Example/OneDrive"), roots)
        self.assertIn(os.path.normcase("D:/Extra"), roots); self.assertIn(os.path.normcase("E:/More"), roots)
        self.assertEqual(len(roots), len(set(roots)))
    @unittest.skipUnless(shutil.which("powershell.exe"), "NVDA's scan worker requires Windows PowerShell")
    def test_non_project_and_runtime_test_are_ignored(self):
        with tempfile.TemporaryDirectory() as folder:
            plain = Path(folder) / "plain"; plain.mkdir(); (plain / "manifest.ini").write_text(MANIFEST)
            runtime = Path(folder) / "Runtime Tests"; runtime.mkdir(); self.make_project(runtime)
            self.assertEqual([], run_scan_worker({"mode": "manual", "fullSystem": False, "roots": [folder], "manifestPaths": []})["manifests"])
    def test_cancellation_stops_discovery_and_update(self):
        with tempfile.TemporaryDirectory() as folder:
            manifest = self.make_project(folder)
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

    def test_saved_project_filtering_never_reads_manifests(self):
        state = {
            "approvedProjects": ["primary", "codex"],
            "projects": [
                {"project_id": "primary", "name": "Demo", "path": r"C:\Users\tester\Documents\NVDA Add-on Development\Add-ons\Demo\manifest.ini", "status": "current", "addon_version": "2026.2"},
                {"project_id": "codex", "name": "Demo", "path": r"C:\Users\tester\Documents\Codex\demo\manifest.ini", "status": "current", "addon_version": "2026.2"},
            ],
        }
        with mock.patch.object(engine, "values", side_effect=AssertionError("main-thread manifest read")):
            visible, ignored = engine.visible_project_records(state)
            newest, _older = engine.newest_local_project_records(visible)
        self.assertEqual({"codex"}, ignored)
        self.assertEqual(["primary"], [item["project_id"] for item in newest])
        self.assertEqual({"primary", "codex"}, engine.saved_approved_project_ids(state))
        legacy = {"projects": [{key: value for key, value in item.items() if key != "addon_version"} for item in state["projects"]]}
        legacy_visible, legacy_ignored = engine.visible_project_records(legacy)
        self.assertEqual(set(), legacy_ignored)
        self.assertEqual(2, len(engine.newest_local_project_records(legacy_visible)[0]))

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

    def test_post_write_exception_restores_original_update_and_downgrade_manifests(self):
        with tempfile.TemporaryDirectory() as folder:
            manifest = self.make_project(folder); original = manifest.read_bytes(); backups = Path(folder) / "backups"
            with mock.patch.object(engine, "validate", side_effect=[[], OSError("simulated read failure")]):
                with self.assertRaisesRegex(OSError, "simulated read failure"):
                    engine.update(manifest, engine.Release("2026.2", "2026.2", False, ""), backups, True)
            self.assertEqual(original, manifest.read_bytes())
            manifest.write_text(MANIFEST.replace("lastTestedNVDAVersion = 2025.4", "lastTestedNVDAVersion = 2026.2"), encoding="utf-8")
            downgrade_original = manifest.read_bytes()
            with mock.patch.object(engine, "validate", side_effect=[[], OSError("simulated hash failure")]):
                with self.assertRaisesRegex(OSError, "simulated hash failure"):
                    engine.downgrade(manifest, "2025.4", backups, {"2025.4": False})
            self.assertEqual(downgrade_original, manifest.read_bytes())

    def test_downgrade_cancellation_stops_before_manifest_write(self):
        with tempfile.TemporaryDirectory() as folder:
            manifest = self.make_project(folder); manifest.write_text(MANIFEST.replace("lastTestedNVDAVersion = 2025.4", "lastTestedNVDAVersion = 2026.2"), encoding="utf-8"); original = manifest.read_bytes()
            result = engine.downgrade(manifest, "2025.4", Path(folder) / "backups", {"2025.4": False}, cancelled=lambda: True)
            self.assertEqual("cancelled", result.status)
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

    def test_lowercase_translation_words_are_not_mistaken_for_ai_disclosures(self):
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w") as archive:
            archive.writestr("locale/it/LC_MESSAGES/nvda.po", 'msgstr "Aggiungi ai preferiti"\nmsgstr "Ai sensi della licenza"\n')
        with zipfile.ZipFile(io.BytesIO(payload.getvalue())) as archive:
            self.assertEqual([], publisher._ai_disclosure_issues(archive))

    def test_github_urls_with_trailing_slash_identify_the_same_repository(self):
        self.assertEqual("https://github.com/owner/demo", publisher._remote_web_url("https://github.com/owner/demo/"))
        self.assertEqual("origin", publisher._matching_remote_name({"origin": "https://github.com/owner/demo.git"}, "https://github.com/owner/demo/"))

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
        plugin = plugin_source()
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

    @unittest.skipUnless(shutil.which("powershell.exe"), "NVDA's scan worker requires Windows PowerShell")
    def test_manual_worker_always_includes_approved_projects_outside_scan_roots(self):
        worker = Path(__file__).parent / "globalPlugins" / "addonDeveloperUpdater" / "worker.ps1"
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); scan_root = root / "configured"; scan_root.mkdir(); approved = self.make_project(root, "outside"); missing = root / "missing" / "manifest.ini"
            request = root / "request.json"; output = root / "result.json"
            request.write_text(json.dumps({"mode": "manual", "fullSystem": False, "roots": [str(scan_root)], "manifestPaths": [str(approved), str(missing)]}), encoding="utf-8")
            completed = subprocess.run(["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(worker), "-RequestPath", str(request), "-OutputPath", str(output)], capture_output=True, text=True, timeout=30)
            result = engine.read_json(output)
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual([str(approved)], result["manifests"])
        self.assertEqual([str(missing)], result["unavailableManifestPaths"])

    def test_runtime_bounds_external_worker_and_safely_handles_progress_close(self):
        plugin = plugin_source()
        self.assertIn("engine.wait_for_worker(self._workerProcess)", plugin)
        self.assertIn("self._progressDialog = None\n            dialog.Destroy()", plugin)
        self.assertIn("Progress window closed. The operation is still running in the background.", plugin)

    def test_github_feedback_uses_public_names_and_refreshes_progress_accessibility(self):
        report = publisher.ProjectPublishInfo(
            "id", "internalModuleName", "manifest.ini", ".", "2026.2", "Public Add-on Name", "Justin Coffin",
            "https://github.com/example/public-addon", 0, True, 0,
        )
        self.assertEqual("Public Add-on Name", publisher.public_name(report))
        self.assertEqual("Public Add-on Name", publisher.public_name({"name": "internalModuleName", "summary": "Public Add-on Name"}))
        plugin = plugin_source()
        self.assertIn("_projectDisplayName(result)", plugin)
        self.assertIn("publisher.public_name(report)", plugin)
        self.assertIn("publisher.public_name(item)", plugin)
        self.assertIn("self.status.SetName(message)", plugin)
        self.assertIn("self._progressDialog.setStatus(message)", plugin)

    def test_failed_command_reports_the_cause_at_the_end_of_verbose_output(self):
        verbose_tests = "\n".join(f"test_{index} ... ok" for index in range(400))
        long_warning_output = "warning line\n" * 300
        process = mock.Mock()
        process.communicate.return_value = (
            "Translation template is out of date; run tools/extract_messages.py\n",
            verbose_tests + "\n" + long_warning_output + "Translation template validation failed\n",
        )
        process.returncode = 1
        with mock.patch.object(publisher.subprocess, "Popen", return_value=process):
            with self.assertRaises(RuntimeError) as caught:
                publisher._run(["powershell.exe", "-File", "build.ps1"])
        message = str(caught.exception)
        self.assertIn("earlier command output omitted", message)
        self.assertIn("Output:", message)
        self.assertIn("Error output:", message)
        self.assertIn("Translation template is out of date", message)
        self.assertIn("Translation template validation failed", message)
        self.assertNotIn("test_0 ... ok", message)

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

    def test_release_plan_finds_historical_versions_and_repairs_bad_assets(self):
        self.assertEqual("2026.2", publisher.matching_release_tag(["v2026.3", "2026.2"], "v2026.2"))
        self.assertEqual("release-2026.2", publisher.matching_release_tag(["release-2026.2"], "2026.2"))
        current = publisher.ProjectPublishInfo(
            "id", "Demo", "manifest.ini", ".", "2026.2", "Demo", "Justin Coffin",
            "https://github.com/example/demo", 0, True, 0, "2026.2",
            "https://github.com/example/demo/releases/download/v2026.2/demo.nvda-addon",
        )
        self.assertEqual("", publisher.release_publish_action(current))
        current.matching_release_tag = "v2026.2"
        current.matching_release_download_url = ""
        self.assertEqual("repair", publisher.release_publish_action(current))
        current.github_release_version = "2026.3"
        current.matching_release_download_url = "https://github.com/example/demo/releases/download/v2026.2/demo.nvda-addon"
        self.assertEqual("", publisher.release_publish_action(current))
        current.changed_files = 1
        self.assertEqual("version-conflict", publisher.release_publish_action(current))
        current.changed_files = 0
        current.matching_release_tag = ""
        current.matching_release_download_url = ""
        self.assertEqual("create", publisher.release_publish_action(current))
        current.matching_release_tag = "v2026.2"
        current.matching_release_targets_head = False
        self.assertEqual("version-conflict", publisher.release_publish_action(current))
        self.assertTrue(any("older commit" in reason for reason in publisher.store_readiness_reasons(current)))

    def test_store_submission_uses_exact_local_version_release_not_newest_release(self):
        report = publisher.ProjectPublishInfo(
            "id", "Demo", "manifest.ini", ".", "2026.2", "Demo", "Justin Coffin",
            "https://github.com/jcoffin1/demo", 0, True, 0, "2026.3",
            "https://github.com/jcoffin1/demo/releases/download/v2026.3/demo-2026.3.nvda-addon",
            matching_release_tag="v2026.2",
            matching_release_download_url="https://github.com/jcoffin1/demo/releases/download/v2026.2/demo-2026.2.nvda-addon",
        )
        item = publisher.store_release_item(report)
        self.assertTrue(item["downloadUrl"].endswith("/v2026.2/demo-2026.2.nvda-addon"))
        self.assertEqual([], publisher.store_readiness_reasons(report))

    def test_existing_matching_release_is_repaired_instead_of_recreated(self):
        with tempfile.TemporaryDirectory() as folder:
            package = Path(folder) / "Demo-2026.2.nvda-addon"
            with zipfile.ZipFile(package, "w") as archive:
                archive.writestr("manifest.ini", 'name = Demo\nversion = 2026.2\n')
            item = {
                "project_id": "demo", "name": "Demo", "summary": "Demo", "version": "2026.2",
                "manifest": str(Path(folder) / "manifest.ini"), "root": folder,
                "sourceUrl": "https://github.com/example/demo", "pushRemote": "origin",
                "targetCommitish": "a" * 40, "matching_release_tag": "2026.2",
                "matching_release_download_url": "", "releaseBlocker": "", "channel": "stable",
            }
            with mock.patch.object(publisher, "gh_path", return_value="gh"), mock.patch.object(publisher, "build_release_package", return_value=package), mock.patch.object(publisher, "_remote_tag_commit", return_value="a" * 40), mock.patch.object(publisher, "_uploaded_asset_url", return_value="https://github.com/example/demo/releases/download/2026.2/Demo-2026.2.nvda-addon"), mock.patch.object(publisher, "_run", return_value="") as run:
                released = publisher.release([item], Path(folder))
        command = run.call_args.args[0]
        self.assertEqual(["gh", "release", "upload"], command[:3])
        self.assertIn("--clobber", command)
        self.assertEqual("2026.2", released[0]["matching_release_tag"])

    def test_release_never_reuses_a_version_tag_from_another_commit(self):
        with tempfile.TemporaryDirectory() as folder:
            package = Path(folder) / "Demo-2026.2.nvda-addon"
            with zipfile.ZipFile(package, "w") as archive:
                archive.writestr("manifest.ini", 'name = Demo\nversion = 2026.2\n')
            item = {
                "name": "Demo", "summary": "Demo", "version": "2026.2", "root": folder,
                "manifest": str(Path(folder) / "manifest.ini"), "sourceUrl": "https://github.com/example/demo",
                "pushRemote": "origin", "targetCommitish": "a" * 40, "matching_release_tag": "v2026.2",
                "matching_release_download_url": "", "releaseBlocker": "", "channel": "stable",
            }
            with mock.patch.object(publisher, "gh_path", return_value="gh"), mock.patch.object(publisher, "build_release_package", return_value=package), mock.patch.object(publisher, "_remote_tag_commit", return_value="b" * 40):
                with self.assertRaisesRegex(RuntimeError, "different commit"):
                    publisher.release([item], Path(folder))

    def test_release_validation_checks_the_actual_package_before_upload(self):
        with tempfile.TemporaryDirectory() as folder:
            package = Path(folder) / "Demo-2026.2.nvda-addon"
            with zipfile.ZipFile(package, "w") as archive:
                archive.writestr("manifest.ini", 'name = OtherAddon\nversion = 2026.2\n')
            verified, error, _issues = publisher.verify_release_package_file(package, "Demo", "2026.2")
        self.assertFalse(verified)
        self.assertIn("OtherAddon", error)

    def test_release_validation_rejects_unsafe_ambiguous_and_symlink_paths(self):
        def package(extra_info, content=b"data"):
            payload = io.BytesIO()
            with zipfile.ZipFile(payload, "w") as archive:
                archive.writestr("manifest.ini", "name = Demo\nversion = 2026.2\n")
                archive.writestr(extra_info, content)
            return payload.getvalue()

        for name in ("../outside.py", "/absolute.py", "C:/absolute.py", "MANIFEST.INI"):
            verified, error, _issues = publisher._verify_release_payload(package(name), "Demo", "2026.2")
            self.assertFalse(verified, name)
            self.assertTrue("unsafe path" in error or "duplicate or case-colliding" in error, error)
        link = zipfile.ZipInfo("globalPlugins/link.py")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        verified, error, _issues = publisher._verify_release_payload(package(link, b"target.py"), "Demo", "2026.2")
        self.assertFalse(verified)
        self.assertIn("symbolic link", error)

    def test_release_validation_reads_every_member_and_rejects_corrupt_binary_data(self):
        payload = io.BytesIO()
        marker = b"unique-binary-member-content"
        with zipfile.ZipFile(payload, "w", zipfile.ZIP_STORED) as archive:
            archive.writestr("manifest.ini", "name = Demo\nversion = 2026.2\n")
            archive.writestr("synthDrivers/demo.dll", marker)
        damaged = bytearray(payload.getvalue()); offset = damaged.find(marker)
        self.assertGreaterEqual(offset, 0); damaged[offset] ^= 0xFF
        verified, error, _issues = publisher._verify_release_payload(bytes(damaged), "Demo", "2026.2")
        self.assertFalse(verified)
        self.assertIn("damaged or unsupported", error)

    @unittest.skipUnless(shutil.which("git"), "Git is required for the publish safety test")
    def test_push_rechecks_credentials_after_the_user_confirms(self):
        self.assertTrue(publisher._is_sensitive_path("config/.env.production"))
        self.assertTrue(publisher._is_sensitive_path("keys/signing.PFX"))
        self.assertFalse(publisher._is_sensitive_path("doc/environment.md"))
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True, capture_output=True)
            (root / ".env.production").write_text("TOKEN=secret\n", encoding="utf-8")
            report = publisher.ProjectPublishInfo(
                "id", "Demo", str(root / "manifest.ini"), str(root), "2026.2", "Demo", "Justin Coffin",
                "https://github.com/example/demo", 1, True, 0, "",
                current_branch="main",
            )
            with mock.patch.object(publisher, "gh_path", return_value="gh"):
                with self.assertRaisesRegex(RuntimeError, "gained files that may hold credentials"):
                    publisher.push([report])

    def test_release_target_must_be_on_repository_default_branch(self):
        missing = subprocess.CompletedProcess([], 1, "", "")
        fast_forward = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch.object(publisher, "_git", return_value=""), mock.patch.object(publisher.subprocess, "run", side_effect=[missing, fast_forward]) as run:
            blocker, can_fast_forward = publisher.release_target_status(Path("C:/project"), "origin", "main", "abc123")
        self.assertIn("not yet on", blocker)
        self.assertIn("main", blocker)
        self.assertTrue(can_fast_forward)
        self.assertIn("merge-base", run.call_args.args[0])
        with mock.patch.object(publisher, "_git", return_value=""), mock.patch.object(publisher.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")):
            self.assertEqual("", publisher.release_target_blocker(Path("C:/project"), "origin", "main", "abc123"))

    def test_diverged_default_branch_requires_a_pull_request(self):
        missing = subprocess.CompletedProcess([], 1, "", "")
        with mock.patch.object(publisher, "_git", return_value=""), mock.patch.object(publisher.subprocess, "run", side_effect=[missing, missing]):
            blocker, can_fast_forward = publisher.release_target_status(Path("C:/project"), "origin", "main", "abc123")
        self.assertIn("diverged", blocker)
        self.assertIn("pull request", blocker)
        self.assertFalse(can_fast_forward)

    @unittest.skipUnless(shutil.which("git"), "Git is required for the default-branch integration test")
    def test_confirmed_default_branch_promotion_is_a_remote_fast_forward(self):
        def git(*arguments, cwd=None):
            completed = subprocess.run(["git", *arguments], cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
            self.assertEqual(0, completed.returncode, completed.stderr)
            return completed.stdout.strip()
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder); remote = base / "remote.git"; root = base / "project"; root.mkdir()
            git("init", "--bare", str(remote)); git("init", "-b", "main", cwd=root)
            git("config", "user.name", "Test Publisher", cwd=root); git("config", "user.email", "publisher@example.invalid", cwd=root)
            (root / "file.txt").write_text("base\n", encoding="utf-8"); git("add", "file.txt", cwd=root); git("commit", "-m", "base", cwd=root)
            git("remote", "add", "origin", str(remote), cwd=root); git("push", "-u", "origin", "main", cwd=root)
            git("switch", "-c", "development/current", cwd=root); (root / "file.txt").write_text("release\n", encoding="utf-8"); git("commit", "-am", "release", cwd=root)
            git("push", "-u", "origin", "development/current", cwd=root); target = git("rev-parse", "HEAD", cwd=root)
            item = {"project_id": "demo", "name": "Demo", "root": str(root), "pushRemote": "origin", "defaultBranch": "main", "currentBranch": "development/current", "targetCommitish": target, "sourceUrl": "https://github.com/example/demo", "releaseBlocker": "pending", "defaultBranchCanFastForward": True}
            with mock.patch.object(publisher, "_require_reviewed_push_url", return_value=str(remote)):
                promoted = publisher.promote_default_branches([item])[0]
            remote_main = git("--git-dir", str(remote), "rev-parse", "refs/heads/main")
            current_branch = git("branch", "--show-current", cwd=root)
        self.assertEqual(target, remote_main)
        self.assertEqual("development/current", current_branch)
        self.assertEqual("", promoted["releaseBlocker"])
        self.assertFalse(promoted["defaultBranchCanFastForward"])

    def test_pull_request_url_names_the_default_and_working_branches(self):
        item = {"sourceUrl": "https://github.com/example/demo", "defaultBranch": "main", "currentBranch": "development/current"}
        self.assertEqual("https://github.com/example/demo/compare/main...development%2Fcurrent?expand=1", publisher.pull_request_url(item))

    def test_release_refuses_an_item_with_a_recorded_branch_blocker(self):
        item = {"name": "Demo", "releaseBlocker": "commit is not on main"}
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(publisher, "gh_path") as gh:
            with self.assertRaisesRegex(RuntimeError, "not on main"):
                publisher.release([item], Path(folder))
        gh.assert_called_once()

    def test_runtime_defers_ineligible_github_releases(self):
        plugin = plugin_source()
        self.assertIn('item.get("releaseBlocker")', plugin)
        self.assertIn("GitHub release deferred", plugin)
        self.assertIn("Update repository default branches", plugin)
        self.assertIn("publisher.promote_default_branches", plugin)
        self.assertIn("publisher.pull_request_url", plugin)

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

    def test_release_package_finds_repository_builder_above_addon_manifest(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "project"; addon = root / "addon"; addon.mkdir(parents=True)
            manifest = addon / "manifest.ini"; manifest.write_text(MANIFEST, encoding="utf-8")
            (root / "build.ps1").write_text("# repository builder\n", encoding="utf-8")
            def run_builder(_arguments, **kwargs):
                self.assertEqual(root, kwargs["cwd"])
                outputs = root / "outputs"; outputs.mkdir()
                (outputs / "demo-1.0.nvda-addon").write_bytes(b"audited")
                return ""
            with mock.patch.object(publisher.shutil, "which", return_value="pwsh"), mock.patch.object(publisher, "_run", side_effect=run_builder) as run:
                package = publisher.build_release_package(
                    {"name": "demo", "version": "1.0", "manifest": str(manifest), "root": str(root)},
                    Path(folder) / "packages",
                )
            self.assertEqual(b"audited", package.read_bytes())
            run.assert_called_once()

    def test_generic_package_filename_cannot_escape_output_folder(self):
        for name, version in (("../outside", "1.0"), ("C:\\outside", "1.0"), ("demo", "../1.0"), ("demo", "1.0/other")):
            with self.subTest(name=name, version=version):
                with self.assertRaisesRegex(RuntimeError, "safe release-package filename"):
                    publisher._safe_package_filename(name, version)

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

    def test_project_builder_cannot_change_source_during_release_validation(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "project"; root.mkdir()
            manifest = root / "manifest.ini"; manifest.write_text(MANIFEST, encoding="utf-8")
            (root / "build.ps1").write_text("# builder\n", encoding="utf-8")
            def mutating_builder(_arguments, **_kwargs):
                manifest.write_text(MANIFEST.replace("1.0", "1.1"), encoding="utf-8")
                outputs = root / "outputs"; outputs.mkdir()
                (outputs / "demo-1.0.nvda-addon").write_bytes(b"package")
                return ""
            with mock.patch.object(publisher.shutil, "which", return_value="pwsh"), mock.patch.object(publisher, "_run", side_effect=mutating_builder):
                with self.assertRaisesRegex(RuntimeError, "changed source files"):
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
            "https://github.com/jcoffin1/current/releases/download/v2026.2/current.nvda-addon",
        )
        with mock.patch.object(publisher, "build_release_package") as build, mock.patch.object(publisher, "verify_release_package_file", return_value=(True, "", ())):
            publisher.validate_publish_builds([report, current])
        build.assert_called_once()
        self.assertEqual("Demo", build.call_args.args[0]["name"])

    def test_runtime_validates_release_builds_before_push(self):
        plugin = plugin_source()
        validate_index = plugin.index("publisher.validate_publish_builds(reports")
        push_index = plugin.index("publisher.push(reports", validate_index)
        self.assertLess(validate_index, push_index)

    def test_cancel_command_tracks_a_scan_instead_of_the_shared_operation_lock(self):
        plugin = plugin_source()
        start = plugin.index("def script_cancelAddonProjectScan")
        end = plugin.index("def script_submitUpdatedAddons", start)
        command = plugin[start:end]
        self.assertIn("self._scanActive.is_set()", command)
        self.assertNotIn("self._scanLock.locked()", command)

    def test_compatibility_ui_uses_official_versions_confirmation_and_persistent_undo(self):
        plugin = plugin_source()
        self.assertIn("publisher.nvda_api_versions(self._apiVersionCachePath)", plugin)
        self.assertIn("Official older NVDA target shared by every selected add-on", plugin)
        self.assertIn("Confirm older compatibility target", plugin)
        self.assertIn("Undo the most recent compatibility target changes", plugin)
        self.assertIn('state.update({"releaseTag": release.tag', plugin)
        self.assertIn("if changed:\n                state[\"compatibilityUndo\"]", plugin)

    def test_update_check_ui_requires_approval_and_reports_status(self):
        plugin = plugin_source()
        self.assertIn("class ProjectApprovalDialog", plugin)
        self.assertIn("class ScanTargetDialog", plugin)
        self.assertIn("engine.available_releases", plugin)
        self.assertIn("engine.matching_release", plugin)
        self.assertIn("Selected NVDA build:", plugin)
        self.assertIn('state["manualReleaseTargetTag"]', plugin)
        self.assertIn("Choose add-ons to track", plugin)
        self.assertIn("Report add-on update-check status", plugin)
        self.assertIn("engine.latest_release", plugin)
        self.assertIn("engine.notification_state", plugin)
        self.assertIn("Confirm add-on compatibility updates", plugin)
        self.assertIn("Undo the most recent discovered compatibility updates", plugin)
        self.assertIn('state["updateUndo"]', plugin)
        self.assertIn('state.get("lastDiscoveryAt")', plugin)
        self.assertNotIn("if manual: approved.update", plugin)

    def test_store_submission_remains_a_manual_browser_action(self):
        plugin = plugin_source()
        self.assertIn("Nothing is submitted until you review the form and submit it yourself.", plugin)
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

    def test_pushurl_cannot_redirect_a_reviewed_remote_to_another_repository(self):
        with mock.patch.object(publisher, "_git", return_value="https://github.com/other-account/other-repository.git"):
            with self.assertRaisesRegex(RuntimeError, "pushes to a different repository"):
                publisher._require_reviewed_push_url(Path("C:/project"), "origin", "https://github.com/jcoffin1/demo.git", "Demo")

    def test_non_github_remotes_do_not_block_github_discovery_or_matching(self):
        local_only = {"origin": "file:///C:/repositories/demo.git"}
        self.assertEqual(("", ""), publisher._select_publish_remote(local_only, "jcoffin1"))
        mixed = {
            "origin": "file:///C:/repositories/demo.git",
            "fork": "https://github.com/jcoffin1/demo.git",
        }
        self.assertEqual("fork", publisher._matching_remote_name(mixed, "https://github.com/jcoffin1/demo"))
        self.assertEqual("publisher", publisher._available_remote_name(local_only))
        self.assertEqual("publisher3", publisher._available_remote_name({"origin": "local", "publisher": "one", "publisher2": "two"}))

    def test_staged_change_check_distinguishes_changes_from_git_errors(self):
        for returncode, expected in ((0, False), (1, True)):
            completed = subprocess.CompletedProcess([], returncode, "", "")
            with mock.patch.object(publisher.subprocess, "run", return_value=completed):
                self.assertEqual(expected, publisher._has_staged_changes(Path("C:/project"), "Demo"))
        failed = subprocess.CompletedProcess([], 128, "", "fatal: bad repository")
        with mock.patch.object(publisher.subprocess, "run", return_value=failed):
            with self.assertRaisesRegex(RuntimeError, "exit code 128"):
                publisher._has_staged_changes(Path("C:/project"), "Demo")

    def test_unpushed_status_never_turns_an_unknown_comparison_into_zero(self):
        with mock.patch.object(publisher, "_git", side_effect=RuntimeError("missing ref")):
            count, error = publisher._unpushed_status(Path("C:/project"), "origin", "main")
        self.assertEqual(0, count)
        self.assertIn("could not be verified", error)
        report = publisher.ProjectPublishInfo(
            "id", "Demo", "manifest.ini", "C:/project", "2026.2", "Demo", "Justin Coffin",
            "https://github.com/example/demo", 0, True, count, unpushed_check_error=error,
        )
        self.assertTrue(any("could not be verified" in reason for reason in publisher.store_readiness_reasons(report)))

    def test_unpushed_status_fetches_and_compares_the_selected_remote_branch(self):
        commands = []; timeouts = {}
        def git(_root, *arguments, timeout=publisher.COMMAND_TIMEOUT_SECONDS):
            commands.append(arguments); timeouts[arguments[0]] = timeout
            return "2" if arguments[:2] == ("rev-list", "--count") else ""
        with mock.patch.object(publisher, "_git", side_effect=git):
            count, error = publisher._unpushed_status(Path("C:/project"), "fork", "main")
        self.assertEqual(publisher.TRANSFER_TIMEOUT_SECONDS, timeouts["fetch"])
        self.assertEqual(publisher.COMMAND_TIMEOUT_SECONDS, timeouts["rev-list"])
        self.assertEqual((2, ""), (count, error))
        self.assertIn(("fetch", "--no-tags", "fork", "+refs/heads/main:refs/remotes/fork/main"), commands)
        self.assertIn(("rev-list", "--count", "refs/remotes/fork/main..HEAD"), commands)
        self.assertFalse(any("@{upstream}" in argument for command in commands for argument in command))

    def test_source_change_after_review_blocks_push(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); source = root / "manifest.ini"; source.write_text(MANIFEST, encoding="utf-8")
            fingerprint = publisher._source_fingerprint(root, False)
            source.write_text(MANIFEST.replace("1.0", "1.1"), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "changed after the publishing review"):
                publisher._require_source_fingerprint(root, fingerprint, False, "Demo")

    @unittest.skipUnless(shutil.which("git"), "Git is required for release source-integrity tests")
    def test_release_source_must_still_match_reviewed_files_and_pushed_commit(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=root, check=True)
            manifest = root / "manifest.ini"; manifest.write_text(MANIFEST, encoding="utf-8")
            subprocess.run(["git", "add", "manifest.ini"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-m", "base"], cwd=root, check=True, capture_output=True)
            target = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True).stdout.strip()
            item = {"root": str(root), "has_git": True, "targetCommitish": target, "source_fingerprint": publisher._source_fingerprint(root, True)}
            manifest.write_text(MANIFEST.replace("1.0", "1.1"), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "changed after the publishing review"):
                publisher._require_release_source(item, "Demo")
            manifest.write_text(MANIFEST, encoding="utf-8")
            subprocess.run(["git", "commit", "--allow-empty", "-m", "later"], cwd=root, check=True, capture_output=True)
            with self.assertRaisesRegex(RuntimeError, "no longer at the commit"):
                publisher._require_release_source(item, "Demo")

    @unittest.skipUnless(shutil.which("git"), "Git is required for release source-integrity tests")
    def test_committing_a_reviewed_deletion_keeps_the_source_fingerprint(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=root, check=True)
            (root / "manifest.ini").write_text(MANIFEST, encoding="utf-8"); obsolete = root / "obsolete.py"; obsolete.write_text("value = 1\n", encoding="utf-8")
            subprocess.run(["git", "add", "--all"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-m", "base"], cwd=root, check=True, capture_output=True)
            obsolete.unlink()
            reviewed = publisher._source_fingerprint(root, True)
            subprocess.run(["git", "add", "--all", "--", "obsolete.py"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-m", "Remove obsolete module"], cwd=root, check=True, capture_output=True)
            publisher._require_source_fingerprint(root, reviewed, True, "Demo")
            obsolete.write_text("value = 2\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "changed after the publishing review"):
                publisher._require_source_fingerprint(root, reviewed, True, "Demo")

    def test_generated_build_and_python_cache_paths_are_not_published(self):
        self.assertTrue(publisher._is_generated_path("build-local/package/file.dll"))
        self.assertTrue(publisher._is_generated_path("synthDrivers/__pycache__/driver.cpython-313.pyc"))
        self.assertTrue(publisher._is_generated_path("outputs/demo.nvda-addon"))
        self.assertFalse(publisher._is_generated_path("synthDrivers/bestspeech.py"))

    @unittest.skipUnless(shutil.which("git"), "Git is required for staged-path safety tests")
    def test_staged_only_source_is_counted_and_staged_generated_output_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=root, check=True)
            source = root / "manifest.ini"; source.write_text(MANIFEST, encoding="utf-8")
            subprocess.run(["git", "add", "manifest.ini"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-m", "base"], cwd=root, check=True, capture_output=True)
            source.write_text(MANIFEST.replace("1.0", "1.1"), encoding="utf-8")
            subprocess.run(["git", "add", "manifest.ini"], cwd=root, check=True)
            self.assertIn("manifest.ini", publisher._publishable_paths(root))
            output = root / "outputs" / "demo.nvda-addon"; output.parent.mkdir(); output.write_bytes(b"package")
            subprocess.run(["git", "add", "-f", "outputs/demo.nvda-addon"], cwd=root, check=True)
            self.assertNotIn("outputs/demo.nvda-addon", publisher._publishable_paths(root))
            excluded = [name for name in publisher._staged_paths(root) if publisher._is_generated_path(name)]
            self.assertEqual(["outputs/demo.nvda-addon"], excluded)
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

    def test_missing_github_cli_has_a_distinct_actionable_error(self):
        with mock.patch.object(publisher.shutil, "which", return_value=None), mock.patch.object(publisher.Path, "is_file", return_value=False):
            with self.assertRaisesRegex(publisher.GitHubCliRequired, "not installed"):
                publisher.gh_path()

    def test_every_github_entry_point_offers_missing_cli_guidance(self):
        plugin = plugin_source()
        self.assertGreaterEqual(plugin.count("except publisher.GitHubCliRequired"), 7)
        self.assertIn("self._offerStoreGitHubLogin", plugin)
        self.assertIn("self._retryStorePreflight", plugin)
        self.assertIn("publisher.GITHUB_CLI_URL", plugin)
        self.assertIn("Installation is not automatic", plugin)

    def test_download_url_gesture_and_clipboard_action_are_present(self):
        plugin = plugin_source()
        self.assertIn('gesture="kb:NVDA+alt+shift+f"', plugin)
        self.assertIn("api.copyToClip(repository.download_url)", plugin)
        self.assertIn('label=_("&Copy download link")', plugin)
        self.assertIn('label=_("Copy &repository link")', plugin)
        self.assertIn("api.copyToClip(repository.url)", plugin)
        self.assertLess(plugin.index('label=_("&Copy download link")'), plugin.index('label=_("Copy &repository link")'))

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

    def test_accessible_issue_form_rejects_unsupported_question_types(self):
        supported = publisher.GitHubIssueTemplate(".github/ISSUE_TEMPLATE/bug.yml", "bug.yml", "sha", "name: Bug\ndescription: Report\nbody:\n  - type: input\n    id: summary\n    attributes:\n      label: Summary\n")
        unsupported_text = "name: Bug\ndescription: Report\nbody:\n  - type: custom-widget\n    id: summary\n    attributes:\n      label: Summary\n"
        unsupported = publisher.GitHubIssueTemplate(supported.path, supported.name, "sha", unsupported_text)
        self.assertTrue(publisher.issue_template_form_supported(supported, publisher.parse_issue_template_document(supported)))
        self.assertFalse(publisher.issue_template_form_supported(unsupported, publisher.parse_issue_template_document(unsupported)))
        self.assertFalse(publisher.issue_template_form_supported(supported, publisher.parse_issue_template_document(supported), "x" * (256 * 1024 + 1)))

    def test_github_login_timeout_terminates_and_kills_the_cli(self):
        class Input:
            def write(self, _text): return None
            def flush(self): return None
        class Process:
            def __init__(self): self.stdin = Input(); self.returncode = None; self.terminated = False; self.killed = False; self.waits = 0
            def communicate(self, timeout=None): raise subprocess.TimeoutExpired("gh", timeout)
            def terminate(self): self.terminated = True
            def wait(self, timeout=None):
                self.waits += 1
                if not self.killed: raise subprocess.TimeoutExpired("gh", timeout)
                self.returncode = -9; return self.returncode
            def kill(self): self.killed = True
            def poll(self): return None
        process = Process()
        with mock.patch.object(publisher, "gh_path", return_value="gh.exe"), mock.patch.object(publisher.subprocess, "Popen", return_value=process), mock.patch.object(publisher.os, "startfile"), mock.patch.object(publisher.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "timed out"):
                publisher.login()
        self.assertTrue(process.terminated); self.assertTrue(process.killed); self.assertEqual(2, process.waits)

    def test_addon_termination_can_stop_tracked_external_commands(self):
        class Process:
            def __init__(self): self.returncode = None; self.terminated = False
            def poll(self): return self.returncode
            def terminate(self): self.terminated = True; self.returncode = -15
            def wait(self, timeout=None): return self.returncode
            def kill(self): self.returncode = -9
        process = Process(); publisher._track_process(process)
        try:
            publisher.cancel_running_commands()
        finally:
            publisher._untrack_process(process)
        self.assertTrue(process.terminated)

    def test_runtime_keeps_state_and_cancellation_work_off_the_main_thread(self):
        source = plugin_source()
        status_start = source.index("def script_reportAddonUpdateCheckStatus")
        status = source[status_start:source.index("@staticmethod", status_start)]
        self.assertIn("engine.saved_approved_project_ids", status)
        self.assertNotIn("engine.approved_project_ids", status)
        preflight = source[source.index("def _githubPreflight"):source.index("def _offerGitHubLogin")]
        self.assertLess(preflight.index("self._rememberExistingReleases(reports)"), preflight.index("self._scanLock.release()"))
        apply_start = source[source.index("def _startApply"):source.index("def _finishApply")]
        self.assertIn("self._scanActive.set()", apply_start)
        self.assertIn("self._scanActive.clear()", apply_start)
        self.assertIn("manifest results or undo history could not be saved", source)
        self.assertIn("Finish the current confirmation", source)
        self.assertIn("publisher.cancel_running_commands()", source)

    def test_keyboard_project_selection_is_async_and_dialogs_are_independent(self):
        source = plugin_source()
        display_helper = source[source.index("def _projectDisplayName"):source.index("class UpdaterSettingsPanel")]
        self.assertNotIn("engine.values", display_helper)
        self.assertIn('self._beginApprovedProjectSelection("github")', source)
        self.assertIn('self._beginApprovedProjectSelection("store")', source)
        self.assertIn("self._storeDialog", source)
        self.assertIn("self._githubSelectionAction", source)

    def test_markdown_issue_template_form_preserves_body(self):
        content = "---\nname: Bug\nabout: Report a bug\ntitle: ''\nlabels: bug\nassignees: ''\n---\n## Steps\nTell us what happened.\n"
        template = publisher.GitHubIssueTemplate(".github/ISSUE_TEMPLATE/bug.md", "bug.md", "abc", content)
        document = publisher.parse_issue_template_document(template)
        document.data["about"] = "Updated"
        rendered = publisher.render_issue_template_document(document)
        self.assertIn("about: Updated", rendered)
        self.assertTrue(rendered.endswith("## Steps\nTell us what happened.\n"))

    def test_issue_and_template_gestures_are_registered(self):
        plugin = plugin_source()
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

GPL2_HEADER = "                    GNU GENERAL PUBLIC LICENSE\n                       Version 2, June 1991\n"

def publish_report(**changes):
    report = publisher.ProjectPublishInfo(
        "id", "demo", "manifest.ini", ".", "2026.2.1", "Demo", "Jane Developer <jane@example.invalid>",
        "https://github.com/example/demo", 0, True, 0, "2026.2.1",
        "https://github.com/example/demo/releases/download/v2026.2.1/demo-2026.2.1.nvda-addon",
    )
    for key, value in changes.items(): setattr(report, key, value)
    return report

class StoreSubmissionDetailTests(unittest.TestCase):
    def test_publisher_comes_from_manifest_author_without_email(self):
        self.assertEqual("Jane Developer", publisher.store_publisher_name("Jane Developer <jane@example.invalid>"))
        self.assertEqual("Jane Developer, Sam Helper", publisher.store_publisher_name("Jane Developer <a@b.c>, Sam Helper <d@e.f>"))

    def test_standard_license_files_are_recognized(self):
        cases = (
            ("COPYING.txt", GPL2_HEADER, "GPL v2"),
            ("LICENSE", "GNU GENERAL PUBLIC LICENSE\n   Version 3, 29 June 2007\n", "GPL v3"),
            ("LICENSE.md", "MIT License\n\nCopyright (c) 2026\n", "MIT"),
            ("license.txt", "Apache License\n                           Version 2.0, January 2004\n", "Apache 2.0"),
        )
        for filename, text, expected in cases:
            with self.subTest(filename=filename), tempfile.TemporaryDirectory() as folder:
                root = Path(folder); (root / filename).write_text(text, encoding="utf-8")
                self.assertEqual(expected, publisher.detect_license(root, root)[0])

    def test_unrecognized_license_is_left_blank(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); (root / "LICENSE").write_text("All rights reserved.\n", encoding="utf-8")
            self.assertEqual(("", ""), publisher.detect_license(root, root))

    def test_store_form_uses_detected_details_settings_and_blank_fallbacks(self):
        detected = publisher.store_release_item(publish_report(license_name="GPL v2", license_url="https://www.gnu.org/licenses/gpl-2.0.html"))
        fields = urllib.parse.parse_qs(urllib.parse.urlsplit(publisher.store_url(detected)).query)
        self.assertEqual(["Jane Developer"], fields["publisher"]); self.assertEqual(["GPL v2"], fields["license-name"])
        unknown = publisher.store_release_item(publish_report())
        fields = urllib.parse.parse_qs(urllib.parse.urlsplit(publisher.store_url(unknown)).query, keep_blank_values=True)
        self.assertEqual([""], fields["license-name"]); self.assertEqual([""], fields["license-url"])
        fields = urllib.parse.parse_qs(urllib.parse.urlsplit(publisher.store_url(unknown, "Example Team", "MIT", "https://opensource.org/license/mit")).query)
        self.assertEqual(["Example Team"], fields["publisher"]); self.assertEqual(["MIT"], fields["license-name"])
        self.assertEqual(["https://opensource.org/license/mit"], fields["license-url"])

class VersionIncrementTests(unittest.TestCase):
    def test_next_patch_version(self):
        self.assertEqual("2026.2.48", publisher.next_patch_version("2026.2.47"))
        self.assertEqual("2026.2.1", publisher.next_patch_version("2026.2"))
        self.assertEqual("", publisher.next_patch_version("1.0-beta2"))

    def test_increment_changes_manifest_build_variables_and_readme_preserving_format(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); manifest = root / "manifest.ini"
            manifest.write_bytes(codecs.BOM_UTF8 + b'name = demo\r\nversion = "2026.2.47"\r\nminimumNVDAVersion = 2025.1\r\n')
            (root / "buildVars.py").write_text('addon_info = {\n    "addon_name": "demo",\n    "addon_version": "2026.2.47",\n}\n', encoding="utf-8")
            (root / "doc" / "en").mkdir(parents=True); readme = root / "doc" / "en" / "readme.html"
            readme.write_text("<p>Version 2026.2.47. Tested with 2026.2.47 builds.</p>\n", encoding="utf-8")
            changed = publisher.increment_project_version(manifest, root, "2026.2.47", "2026.2.48")
            self.assertEqual(3, len(changed))
            self.assertEqual(codecs.BOM_UTF8 + b'name = demo\r\nversion = "2026.2.48"\r\nminimumNVDAVersion = 2025.1\r\n', manifest.read_bytes())
            self.assertIn('"addon_version": "2026.2.48"', (root / "buildVars.py").read_text(encoding="utf-8"))
            self.assertEqual("<p>Version 2026.2.48. Tested with 2026.2.47 builds.</p>\n", readme.read_text(encoding="utf-8"))

    def test_increment_changes_nothing_when_build_variables_disagree(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); manifest = root / "manifest.ini"; manifest.write_text("name = demo\nversion = 1.0.0\n", encoding="utf-8")
            (root / "buildVars.py").write_text('addon_info = {"addon_version": VERSION}\n', encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "addon_version"):
                publisher.increment_project_version(manifest, root, "1.0.0", "1.0.1")
            self.assertEqual("name = demo\nversion = 1.0.0\n", manifest.read_text(encoding="utf-8"))

    def test_publish_plan_separates_version_conflicts(self):
        conflict = publish_report(matching_release_tag="v2026.2.1", changed_files=2)
        current = publish_report(project_id="current")
        new = publish_report(project_id="new", version="2026.2.2", changed_files=1)
        foreign = publish_report(project_id="foreign", repository_owned_by_user=False, changed_files=1)
        eligible, not_owned, conflicts = publisher.publish_plan([conflict, current, new, foreign])
        self.assertEqual(["new"], [report.project_id for report in eligible])
        self.assertEqual(["foreign"], [report.project_id for report in not_owned])
        self.assertEqual(["id"], [report.project_id for report in conflicts])
        self.assertEqual(["id", "current"], [report.project_id for report in publisher.reports_with_current_releases([conflict, current, new, foreign])])

class ReleaseNotesTests(unittest.TestCase):
    CHANGELOG = "# Changelog\n\n## 2026.2.48\n\n- Fixed the thing.\n\n### Details\n\nMore.\n\n## 2026.2.4\n\n- Older.\n"

    def test_changelog_section_for_exact_version_is_used(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); (root / "CHANGELOG.md").write_text(self.CHANGELOG, encoding="utf-8")
            self.assertEqual("- Fixed the thing.\n\n### Details\n\nMore.", publisher.release_notes(root, root, "2026.2.48", "manifest text"))
            self.assertEqual("- Older.", publisher.release_notes(root, root, "v2026.2.4"))
            self.assertEqual("", publisher.release_notes(root, root, "2026.2.49", "stale manifest text"))

    def test_manifest_changelog_is_used_only_without_a_changelog_file(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.assertEqual("Adds a feature.", publisher.release_notes(root, root, "1.0", "Adds a feature."))

    def test_notes_with_disclosure_wording_fall_back_to_fixed_text(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.assertEqual("", publisher.release_notes(root, root, "1.0", "Written by " + "ChatGPT."))

    def test_reviewed_notes_are_sent_through_standard_input(self):
        arguments = publisher.release_create_arguments("gh", "v1.0", Path("demo.nvda-addon"), "example/demo", "a" * 40, "Demo", notes="- Fixed.")
        self.assertIn("--notes-file", arguments); self.assertNotIn("--notes", arguments)
        with tempfile.TemporaryDirectory() as folder:
            package = Path(folder) / "demo-1.0.nvda-addon"
            with zipfile.ZipFile(package, "w") as archive: archive.writestr("manifest.ini", "name = demo\nversion = 1.0\n")
            item = {"name": "demo", "summary": "Demo", "version": "1.0", "root": folder, "manifest": str(Path(folder) / "manifest.ini"), "sourceUrl": "https://github.com/example/demo", "pushRemote": "origin", "targetCommitish": "a" * 40, "releaseBlocker": "", "channel": "stable", "release_notes": "- Fixed."}
            with mock.patch.object(publisher, "gh_path", return_value="gh"), mock.patch.object(publisher, "build_release_package", return_value=package), mock.patch.object(publisher, "_require_release_source"), mock.patch.object(publisher, "_remote_tag_commit", return_value=""), mock.patch.object(publisher, "_uploaded_asset_url", return_value="https://github.com/example/demo/releases/download/v1.0/demo-1.0.nvda-addon"), mock.patch.object(publisher, "_run", return_value="") as run:
                publisher.release([item], Path(folder))
        create = next(call for call in run.call_args_list if call.args[0][1:3] == ["release", "create"])
        self.assertEqual("- Fixed.", create.kwargs["input_text"])

class ProjectBuilderVersionTests(unittest.TestCase):
    def test_version_parameter_detection(self):
        cases = (
            ('param(\n\t[string]$Version = ""\n)\n& python build.py\n', True),
            ('<# Builds the add-on #>\n[CmdletBinding()]\nparam([Parameter(Mandatory = $true)][string]$Version)\n', True),
            ('param([switch]$Clean)\nWrite-Output $Version\n', False),
            ('function Build { param($Version) }\nBuild\n', False),
            ('# param($Version)\nscons\n', False),
        )
        for text, expected in cases:
            with self.subTest(text=text), tempfile.TemporaryDirectory() as folder:
                script = Path(folder) / "build.ps1"; script.write_text(text, encoding="utf-8")
                self.assertEqual(expected, publisher.builder_accepts_version(script))

    def test_builder_without_version_parameter_runs_without_it(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "project"; root.mkdir(); manifest = root / "manifest.ini"; manifest.write_text(MANIFEST, encoding="utf-8")
            (root / "build.ps1").write_text("scons\n", encoding="utf-8")
            def run_builder(_arguments, **_kwargs):
                (root / "outputs").mkdir(exist_ok=True); (root / "outputs" / "demo-1.0.nvda-addon").write_bytes(b"package"); return ""
            with mock.patch.object(publisher.shutil, "which", return_value="pwsh"), mock.patch.object(publisher, "_run", side_effect=run_builder) as run:
                publisher.build_release_package({"name": "demo", "version": "1.0", "manifest": str(manifest)}, Path(folder) / "packages")
            self.assertNotIn("-Version", run.call_args.args[0])

class MovedDecisionLogicTests(unittest.TestCase):
    def test_release_feed_tags_come_from_entry_ids(self):
        import xml.etree.ElementTree as ET
        feed = ET.fromstring(
            '<feed xmlns="http://www.w3.org/2005/Atom">'
            '<entry><id>tag:github.com,2008:Repository/45885732/release-2026.3beta4</id><title>NVDA 2026.3 Beta 4</title><link href="https://github.com/nvaccess/nvda/releases/tag/release-2026.3beta4"/></entry>'
            '<entry><id>urn:unrelated</id><title>2026.2</title></entry>'
            '</feed>'
        )
        items = engine.parse_release_feed(feed)
        self.assertEqual(["release-2026.3beta4", "2026.2"], [item["tag_name"] for item in items])
        self.assertEqual([True, False], [item["prerelease"] for item in items])

    def test_scan_scheduling(self):
        now = datetime(2026, 10, 14, 12, tzinfo=timezone.utc)
        recent = {"lastDiscoveryAt": (now - timedelta(hours=1)).isoformat()}
        old = {"lastDiscoveryAt": (now - timedelta(hours=30)).isoformat()}
        future = {"lastDiscoveryAt": (now + timedelta(hours=1)).isoformat()}
        self.assertEqual("manual", engine.worker_mode(True, recent, 24, now))
        self.assertEqual("background", engine.worker_mode(False, recent, 24, now))
        self.assertEqual("periodic", engine.worker_mode(False, old, 24, now))
        self.assertTrue(engine.rescan_due(future, 24, now)); self.assertTrue(engine.rescan_due({}, 24, now))

    def test_submission_channel_follows_selected_build(self):
        self.assertEqual("dev", engine.submission_channel(engine.Release("alpha-57626,71bae80b", "2026.3", True, "")))
        self.assertEqual("beta", engine.submission_channel(engine.Release("2026.3beta4", "2026.3", True, "")))
        self.assertEqual("stable", engine.submission_channel(engine.Release("2026.2", "2026.2", False, "")))

    def test_preflight_release_channel_is_not_replaced_by_saved_channel(self):
        items = [{"project_id": "beta", "channel": "beta"}, {"project_id": "old", "channel": ""}, {"project_id": "new"}]
        publisher.resolve_release_channels(items, {"beta": "stable", "old": "dev"})
        self.assertEqual(["beta", "dev", "stable"], [item["channel"] for item in items])

class PythonCompatibilityTests(unittest.TestCase):
    def test_nvda_releases_map_to_their_bundled_python(self):
        import python_compatibility
        self.assertEqual((3, 7), python_compatibility.python_version_for_nvda("2023.3.4"))
        self.assertEqual((3, 11), python_compatibility.python_version_for_nvda("2024.1"))
        self.assertEqual((3, 11), python_compatibility.python_version_for_nvda("2025.3.3"))
        self.assertEqual((3, 13), python_compatibility.python_version_for_nvda("2026.1"))
        with self.assertRaises(ValueError): python_compatibility.python_version_for_nvda("future")

    def test_newer_syntax_is_rejected_for_older_nvda(self):
        import python_compatibility
        cases = (
            ("if (count := 1): pass\n", (3, 7), True),
            ("if (count := 1): pass\n", (3, 11), False),
            ("type Alias = int\n", (3, 11), True),
            ("type Alias = int\n", (3, 13), False),
            ('label = f"{item["name"]}"\n', (3, 11), True),
            ('label = f"{item["name"]}"\n', (3, 13), False),
            (r"""label = f"{'\n'.join(lines)}"
""", (3, 11), True),
            ("""label = f"{item['name']}" + f'''{"nested"}''' + f"{value:{width}}" + f"{{literal}}"\n""", (3, 11), False),
            ("def broken(:\n", (3, 13), True),
        )
        for source, target, rejected in cases:
            with self.subTest(source=source, target=target):
                self.assertEqual(rejected, bool(python_compatibility.compatibility_errors(source, "plugin.py", target)))

    def test_release_package_rejects_python_too_new_for_minimum_nvda(self):
        def package(minimum, code):
            payload = io.BytesIO()
            with zipfile.ZipFile(payload, "w") as archive:
                archive.writestr("manifest.ini", f"name = demo\nversion = 1.0\nminimumNVDAVersion = {minimum}\nlastTestedNVDAVersion = 2026.2\n")
                archive.writestr("globalPlugins/demo.py", code)
            return payload.getvalue()
        code = 'label = f"{item["name"]}"\n'
        verified, error, _issues = publisher._verify_release_payload(package("2025.3.3", code), "demo", "1.0")
        self.assertFalse(verified); self.assertIn("NVDA 2025.3.3", error); self.assertIn("globalPlugins/demo.py", error)
        self.assertTrue(publisher._verify_release_payload(package("2026.1", code), "demo", "1.0")[0])
        self.assertFalse(publisher._verify_release_payload(package("2026.1", "def broken(:\n"), "demo", "1.0")[0])

    def test_manifest_validation_uses_minimum_nvda_python(self):
        with tempfile.TemporaryDirectory() as folder:
            manifest = Path(folder) / "manifest.ini"
            manifest.write_text(MANIFEST.replace("2025.1", "2023.3"), encoding="utf-8")
            (Path(folder) / "plugin.py").write_text("if (count := 1): pass\n", encoding="utf-8")
            self.assertTrue(any("Python 3.7 cannot load" in error for error in engine.validate(manifest)))
            manifest.write_text(MANIFEST, encoding="utf-8")
            self.assertEqual([], engine.validate(manifest))

    def test_this_addon_loads_in_its_oldest_supported_nvda(self):
        self.assertEqual([], engine.validate(Path(__file__).parent / "manifest.ini"))

    def test_new_releases_without_notes_are_reported(self):
        with_notes = publish_report(project_id="notes", version="2026.2.2", release_notes="- Fixed.")
        without_notes = publish_report(project_id="plain", version="2026.2.2")
        repair = publish_report(project_id="repair", matching_release_tag="v2026.2.1", matching_release_download_url="")
        self.assertEqual(["plain"], [report.project_id for report in publisher.reports_without_release_notes([with_notes, without_notes, repair])])

class ExistingFeatureImprovementTests(unittest.TestCase):
    def make_project(self, root, name):
        project = Path(root) / name; project.mkdir(parents=True); (project / ".git").mkdir()
        manifest = project / "manifest.ini"; manifest.write_text(MANIFEST, encoding="utf-8"); return manifest

    @unittest.skipUnless(shutil.which("powershell.exe"), "NVDA's scan worker requires Windows PowerShell")
    def test_worker_searches_shallow_folders_before_one_huge_subtree(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            # Depth-first order explored this huge tree first and exhausted the budget.
            for index in range(40): (root / "z-huge-tests" / f"case{index}" / "data").mkdir(parents=True)
            manifest = self.make_project(root, "a-addon")
            result = run_scan_worker({"mode": "manual", "fullSystem": False, "roots": [folder], "manifestPaths": [], "maxDirectories": 10, "maxSeconds": 30})
        self.assertEqual([str(manifest.resolve())], result["manifests"])
        self.assertTrue(result["truncated"])
        self.assertEqual(1, result["completeDepth"])

    @unittest.skipUnless(shutil.which("powershell.exe"), "NVDA's scan worker requires Windows PowerShell")
    def test_worker_skips_version_control_folders_and_reports_complete_scans(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); manifest = self.make_project(root, "addon")
            for index in range(30): (root / "addon" / ".git" / "objects" / f"{index:02x}").mkdir(parents=True)
            result = run_scan_worker({"mode": "manual", "fullSystem": False, "roots": [folder], "manifestPaths": []})
        self.assertEqual([str(manifest.resolve())], result["manifests"])
        self.assertEqual(2, result["directoriesVisited"])
        self.assertFalse(result["truncated"]); self.assertIsNone(result["completeDepth"])

    def test_worker_and_engine_skip_the_same_folders(self):
        import re
        worker_text = WORKER.read_text(encoding="utf-8-sig")
        listed = re.search(r"\n@\((.*?)\) \| ForEach-Object", worker_text).group(1)
        self.assertEqual(engine.PRUNE_NAMES, set(re.findall(r"'([^']*)'", listed)))

    def test_github_operations_stop_only_between_projects(self):
        reports = [publish_report(project_id="first", version="2026.2.2"), publish_report(project_id="second", version="2026.2.2")]
        calls = []
        with mock.patch.object(publisher, "gh_path", return_value="gh"):
            with self.assertRaises(publisher.OperationCancelled) as raised:
                publisher.push(reports, cancelled=lambda: calls.append("checked") or True)
        self.assertEqual([], raised.exception.completed); self.assertEqual(["checked"], calls)
        item = {"name": "demo", "summary": "Demo", "version": "1.0", "releaseBlocker": ""}
        with mock.patch.object(publisher, "gh_path", return_value="gh"), mock.patch.object(publisher, "_require_release_source") as source_check:
            with self.assertRaises(publisher.OperationCancelled):
                publisher.release([item], Path("."), cancelled=lambda: True)
        source_check.assert_not_called()

    def test_cancellation_reports_releases_already_completed(self):
        items = [{"project_id": "one", "name": "one", "summary": "One", "version": "1.0", "releaseBlocker": ""}, {"project_id": "two", "name": "two", "summary": "Two", "version": "1.0", "releaseBlocker": ""}]
        checks = iter([False, True])
        with mock.patch.object(publisher, "gh_path", return_value="gh"), mock.patch.object(publisher, "release_publish_action", return_value="create"), mock.patch.object(publisher, "_require_release_source"), mock.patch.object(publisher, "build_release_package", return_value=Path("one-1.0.nvda-addon")), mock.patch.object(publisher, "verify_release_package_file", return_value=(True, "", ())), mock.patch.object(publisher, "_remote_tag_commit", return_value=""), mock.patch.object(publisher, "_run", return_value=""), mock.patch.object(publisher, "_uploaded_asset_url", return_value="https://github.com/example/one/releases/download/v1.0/one-1.0.nvda-addon"):
            items[0].update(root=".", sourceUrl="https://github.com/example/one", targetCommitish="a" * 40); items[1].update(root=".", sourceUrl="https://github.com/example/two", targetCommitish="b" * 40)
            with self.assertRaises(publisher.OperationCancelled) as raised:
                publisher.release(items, Path("."), cancelled=lambda: next(checks))
        self.assertEqual(["one"], [item["project_id"] for item in raised.exception.completed])

    def test_builders_and_transfers_get_longer_time_limits(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "project"; root.mkdir(); manifest = root / "manifest.ini"; manifest.write_text(MANIFEST, encoding="utf-8")
            (root / "build.ps1").write_text("scons\n", encoding="utf-8")
            def run_builder(_arguments, **_kwargs):
                (root / "outputs").mkdir(exist_ok=True); (root / "outputs" / "demo-1.0.nvda-addon").write_bytes(b"package"); return ""
            with mock.patch.object(publisher.shutil, "which", return_value="pwsh"), mock.patch.object(publisher, "_run", side_effect=run_builder) as run:
                publisher.build_release_package({"name": "demo", "version": "1.0", "manifest": str(manifest)}, Path(folder) / "packages")
        self.assertEqual(publisher.BUILDER_TIMEOUT_SECONDS, run.call_args.kwargs["timeout"])
        self.assertEqual("30-minute", publisher._timeout_text(publisher.BUILDER_TIMEOUT_SECONDS))
        self.assertEqual("180-second", publisher._timeout_text(publisher.COMMAND_TIMEOUT_SECONDS))

    def test_information_window_keeps_an_earlier_result_below_the_newest(self):
        self.assertEqual("New result", engine.information_window_text("New result", "Old title", ""))
        self.assertEqual("Same", engine.information_window_text("Same", "Old title", "Same"))
        combined = engine.information_window_text("New result", "Old title", "Old result")
        self.assertEqual("New result\n\nEarlier result, Old title:\nOld result", combined)
        self.assertEqual(combined, engine.information_window_text("New result", "New title", combined))
        self.assertLessEqual(len(engine.information_window_text("New", "Old", "x\n" * 20000, limit=100)), 100)

class SpeechAndBrailleLayoutTests(unittest.TestCase):
    def test_sentences_and_lists_get_their_own_lines(self):
        text = "Compatibility update complete. 2 manifests updated: Demo to NVDA 2026.3; Other to NVDA 2026.3. Use the review command later."
        self.assertEqual(
            "Compatibility update complete.\n2 manifests updated:\nDemo to NVDA 2026.3\nOther to NVDA 2026.3.\nUse the review command later.",
            engine.readable_text(text),
        )

    def test_versions_and_formatted_text_are_not_split(self):
        self.assertEqual("Version 2026.2.47 is current.", engine.readable_text("Version 2026.2.47 is current."))
        formatted = "Publish 1 add-on?\n\nDemo: 1.0"
        self.assertEqual(formatted, engine.readable_text(formatted))

    def test_messages_put_the_outcome_first_with_single_blank_separators(self):
        message = engine.readable_message("Publish 2 add-ons?", ["Commit 3 files.", "", "", "Demo 1.0", ""], "Yes publishes.")
        self.assertEqual("Publish 2 add-ons?\n\nCommit 3 files.\n\nDemo 1.0\n\nYes publishes.", message)
        self.assertEqual("Done.", engine.readable_message("Done."))
        self.assertEqual("Publish 2 add-ons?", engine.first_line("\n  Publish 2 add-ons?\nDetails"))

    def test_message_windows_focus_reviewable_text_and_honor_escape(self):
        plugin = plugin_source()
        self.assertIn("wx.CallAfter(dialog.message.SetFocus)", plugin)
        self.assertNotIn("dialog.okButton.SetFocus", plugin)
        self.assertIn("if key == wx.WXK_ESCAPE: no(); return", plugin)
        self.assertNotIn("ui.message(message); self._showInformation(", plugin)
        self.assertNotIn("_pulseProgress", plugin)
        self.assertIn("self.gauge.Hide()", plugin)

class FoundCopyDescriptionTests(unittest.TestCase):
    def copy(self, version, **details):
        result = engine.ProjectResult("id", "codexStatusAnnouncer", "C:/Users/Example/Documents/Codex/demo/manifest.ini", "awaiting manual approval", addon_version=version, summary="ChatGPT Desktop Access")
        for key, value in details.items(): setattr(result, key, value)
        return result

    def test_public_name_version_and_newest_copy_come_first(self):
        home = Path("C:/Users/Example")
        newest = engine.describe_copy(self.copy("2026.2.2", remote="https://github.com/me/demo.git", uncommitted_changes=0, unpushed_commits=0, branch="main", last_change="2026-09-04"), ["2026.2.2", "2026.2.1"], [], home)
        self.assertEqual("ChatGPT Desktop Access 2026.2.2; newest copy; on GitHub as me/demo, everything pushed; last changed September 4, 2026; folder " + str(Path("Documents/Codex/demo")), newest)
        older = engine.describe_copy(self.copy("2026.2.1"), ["2026.2.2", "2026.2.1"], [], home)
        self.assertIn("older copy; the newest is 2026.2.2", older)
        self.assertNotIn("codexStatusAnnouncer", older)

    def test_tracked_copies_count_as_newer_and_same_versions_are_named(self):
        older = engine.describe_copy(self.copy("2026.2.9"), ["2026.2.9"], ["2026.2.13"])
        self.assertIn("older copy; the newest is 2026.2.13; you already track version 2026.2.13", older)
        same = engine.describe_copy(self.copy("2026.2.13"), ["2026.2.13"], ["2026.2.13"])
        self.assertIn("you already track another copy of this same version", same); self.assertNotIn("newest", same)
        self.assertIn("you already track versions 1.0 and 2.0", engine.describe_copy(self.copy("1.5"), ["1.5"], ["2.0", "1.0"]))

    def test_github_and_local_states_are_described_plainly(self):
        changed = engine.describe_copy(self.copy("1.0", remote="git@github.com:me/demo.git", uncommitted_changes=14, unpushed_commits=2, branch="improve/speed"), ["1.0"], [])
        self.assertIn("on GitHub as me/demo, 14 files changed but not committed, 2 commits not pushed; branch improve/speed", changed)
        self.assertIn("this branch is not linked to a GitHub branch", engine.describe_copy(self.copy("1.0", remote="https://github.com/me/demo", uncommitted_changes=0, branch="audit"), ["1.0"], []))
        self.assertIn("only on this computer; no GitHub repository", engine.describe_copy(self.copy("1.0", branch="main", uncommitted_changes=0), ["1.0"], []))
        self.assertIn("GitHub state not checked", engine.describe_copy(self.copy("1.0", branch="main"), ["1.0"], []))
        self.assertIn("only on this computer; not in Git", engine.describe_copy(self.copy("1.0", branch="not a Git repository"), ["1.0"], []))
        self.assertIn("not on GitHub; its Git remote is D:/backup.git", engine.describe_copy(self.copy("1.0", remote="D:/backup.git", branch="main"), ["1.0"], []))
        self.assertEqual("", engine.github_repository_label("https://gitlab.com/me/demo"))

    @unittest.skipUnless(shutil.which("git"), "Git is required for copy-detail tests")
    def test_copy_details_are_read_from_git_without_network_access(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True); subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=root, check=True)
            manifest = root / "manifest.ini"; manifest.write_text(MANIFEST.replace("summary = Demo", "summary = Demo Add-on"), encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=root, check=True); subprocess.run(["git", "commit", "-m", "base"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "remote", "add", "origin", "https://github.com/me/demo.git"], cwd=root, check=True)
            (root / "new.py").write_text("x = 1\n", encoding="utf-8")
            result = engine.add_copy_details(engine.ProjectResult("id", "demo", str(manifest), "awaiting manual approval"), manifest)
        self.assertEqual(("Demo Add-on", "1.0", "main", "https://github.com/me/demo.git", 1, -1), (result.summary, result.addon_version, result.branch, result.remote, result.uncommitted_changes, result.unpushed_commits))
        self.assertRegex(result.last_change, r"^\d{4}-\d{2}-\d{2}$")

    def test_tracking_window_describes_each_copy(self):
        dialogs = (PLUGIN_FOLDER / "dialogs.py").read_text(encoding="utf-8")
        self.assertIn("engine.describe_copy(", dialogs)
        self.assertNotIn("% (result.name, labels[", dialogs)

class TrackingChoiceTests(unittest.TestCase):
    def state(self):
        return {
            "approvedProjects": ["tracked", "old"], "hiddenProjects": ["hidden"],
            "projects": [{"project_id": identifier, "name": "demo", "path": f"C:/{identifier}/manifest.ini", "status": status} for identifier, status in (("tracked", "current"), ("old", "update available: 2026.2 to 2026.3"), ("hidden", engine.HIDDEN_STATUS), ("new", "awaiting manual approval"))],
            "manualUpdateReview": {"release": {"tag": "2026.3rc1", "manifest_version": "2026.3", "prerelease": True, "url": ""}, "results": [{"project_id": "old", "name": "demo", "path": "C:/old/manifest.ini", "status": "update available: 2026.2 to 2026.3"}]},
        }

    def test_each_copy_has_one_tracking_state(self):
        state = self.state()
        self.assertEqual(["tracked", "tracked", "hidden", "untracked"], [engine.tracking_state(state, identifier) for identifier in ("tracked", "old", "hidden", "new")])

    def test_stop_tracking_and_hide_update_lists_statuses_and_pending_review(self):
        state = self.state()
        engine.set_tracking(state, {"old": "untracked", "new": "hidden", "hidden": "tracked"})
        self.assertEqual(["tracked"], state["approvedProjects"]); self.assertEqual(["new"], state["hiddenProjects"])
        statuses = {item["project_id"]: item["status"] for item in state["projects"]}
        self.assertEqual("awaiting manual approval", statuses["old"]); self.assertEqual(engine.HIDDEN_STATUS, statuses["new"])
        # The stopped copy's pending NVDA update is withdrawn, so it is not updated by mistake.
        self.assertNotIn("manualUpdateReview", state)
        # Tracking itself is completed by the validating step, so "hidden" -> "tracked" only unhides here.
        self.assertNotIn("hidden", state["approvedProjects"])

    def test_hidden_copies_are_not_counted_as_waiting_or_failed(self):
        records = [{"project_id": "hidden", "status": engine.HIDDEN_STATUS}]
        updates, awaiting, failures, changed, resolved = engine.notification_state(records, {}, {}, {})
        self.assertEqual(({}, {}, {}, set()), (updates, awaiting, failures, changed))

    def test_records_from_other_versions_load_safely(self):
        result = engine.project_result_from_record({"project_id": "id", "name": "demo", "path": "C:/demo/manifest.ini", "status": "current", "public_name": "unexpected", "futureField": 1})
        self.assertEqual("demo", result.name)
        self.assertIsNone(engine.project_result_from_record({"name": "missing required fields"}))

    def test_copy_rank_names_the_newest_copy(self):
        self.assertEqual("newest copy", engine.copy_rank("2026.2.54", ["2026.2.54", "2026.2.22"]))
        self.assertEqual("older copy; the newest is 2026.2.54", engine.copy_rank("2026.2.22", ["2026.2.54", "2026.2.22"]))
        self.assertEqual("", engine.copy_rank("1.0", ["1.0"]))

class StoreChannelNoteTests(unittest.TestCase):
    def test_note_states_the_store_channel_for_the_target(self):
        versions = {"2026.3": {"experimental": False}, "2027.1": {"experimental": True}}
        self.assertIn("listed NVDA 2026.3 as stable", publisher.store_channel_note("2026.3", versions))
        self.assertTrue(publisher.store_channel_note("2026.3", versions, as_of="October 23, 2026").startswith("As of October 23, 2026: "))
        self.assertIn("only to the beta or dev channel", publisher.store_channel_note("2027.1", versions))
        self.assertIn("did not list NVDA 2027.2 yet", publisher.store_channel_note("2027.2", versions))
        self.assertEqual("", publisher.store_channel_note("2026.3", {}))

    def test_cached_versions_are_read_without_network_access(self):
        with tempfile.TemporaryDirectory() as folder:
            cache = Path(folder) / "apiVersions.json"
            cache.write_text(json.dumps([{"apiVer": {"major": 2026, "minor": 3, "patch": 0}, "experimental": False}]), encoding="utf-8")
            with mock.patch.object(publisher.urllib.request, "urlopen", side_effect=AssertionError("network used")):
                self.assertIn("2026.3", publisher.cached_nvda_api_versions(cache))
            self.assertEqual({}, publisher.cached_nvda_api_versions(Path(folder) / "missing.json"))
            self.assertEqual({}, publisher.cached_nvda_api_versions(None))

if __name__ == "__main__": unittest.main()
