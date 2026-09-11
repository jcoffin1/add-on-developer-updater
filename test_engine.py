import codecs, io, json, os, stat, subprocess, tempfile, unittest, zipfile
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
