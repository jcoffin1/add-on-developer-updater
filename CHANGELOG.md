# Changelog

## 2026.2.46

- Show the decisive end of failed build and command output instead of the beginning of verbose passing-test output.
- Preserve useful text from both standard output and standard error so release-build failures identify the check that actually needs attention.
- Label failed publishing dialogs as failures instead of saying publishing completed.
- Ignore unsupported local-only Git remotes while finding the correct GitHub repository, including repositories with both local and GitHub remotes.
- Count staged-only source changes, reject staged generated packages, and distinguish real Git inspection errors from ordinary staged differences.
- Stop publication when commit status cannot be verified instead of treating an unknown result as fully pushed.
- Fingerprint reviewed source and stop if files change before push or if a project builder modifies source during release validation.
- Reject release archives containing traversal, absolute, duplicate, case-colliding, or symbolic-link paths.
- Keep saved project filtering free of manifest reads on NVDA's main thread, serialize saved GitHub release metadata with the publishing preflight, and report state-save failures after compatibility changes.
- Make selected manifest application cancellable and stop tracked GitHub or project-builder processes when the add-on terminates.

## 2026.2.45

- Load, inspect, and de-duplicate approved project manifests on a worker thread before opening GitHub or Store selection windows, preventing synchronized or unavailable folders from blocking NVDA's main thread.
- Keep GitHub publishing and Add-on Store selection dialogs independent, and distinguish Issues, repository-template, and template-file selection actions so another shortcut never raises the wrong window.
- Reject unsupported GitHub YAML question types when moving from raw text to the accessible form instead of silently converting or discarding their properties.
- Fully terminate a timed-out or interrupted GitHub CLI device sign-in process.
- Refresh an existing information window with the newest result instead of silently retaining an unrelated older message.
- Include an existing `Documents\Codex` workspace in bounded, isolated development-project discovery.

## 2026.2.44

- Use each manifest's public summary throughout GitHub selection, inspection, branch, push, release, repair, error, and completion feedback instead of exposing its internal add-on ID.
- Keep the internal manifest name exclusively for package identity and validation.
- Derive a missing repository's proposed name from the public add-on name rather than its internal module identifier.
- Update both the focused progress field's value and accessibility name at every stage so current status remains synchronized in speech and braille.

## 2026.2.43

- Inspect up to 1,000 non-draft GitHub Releases so an existing historical tag is not mistaken for a new version merely because it is not the newest release.
- Distinguish release creation from repair, and upload a missing or invalid `.nvda-addon` asset to the matching release instead of attempting to recreate its tag.
- Refuse to reuse a released version when its tag does not identify the current commit or when current local changes are not part of that release; require a manifest version increment instead.
- Resolve lightweight and annotated remote tags and stop before publishing when an existing version tag points to a different commit.
- Validate the exact locally built package before any push and again before upload, including its archive, root manifest, add-on ID, version, and disclosure scan.
- Confirm that GitHub exposes the newly uploaded asset before saving store-submission metadata.
- Recheck credential-like files after confirmation and immediately before staging, including `.env.*` files in projects that do not yet have a Git repository.
- Compare unpushed commits with the selected owned remote and its default branch instead of assuming the remote is named `origin`.

## 2026.2.42

- Preserve a manual scan's exact build and pending add-on results in a separate review snapshot so a later background check cannot replace them before review.
- Remove only successfully completed add-ons from that snapshot, retaining unselected or failed items for another review.
- Try the official NVDA releases feed before falling back to a stale selectable-build cache when the GitHub releases API is unavailable.
- During manual and periodic discovery, always merge every existing approved manifest path into the worker result, including maintained projects outside configured scan roots.
- Report approved manifests that disappeared or became cloud-only consistently in background, manual, and periodic scans.

## 2026.2.41

- Treat the prerelease setting as an availability filter instead of automatically forcing manual scans to the newest alpha build.
- Before a manual scan, offer an accessible list containing the latest meaningful stable, beta, release candidate, and alpha choices, with stable selected initially and the last successful manual choice remembered.
- Announce the exact NVDA build separately from the manifest compatibility target, including full alpha build versions such as `2026.3.0.57626`.
- Revalidate the exact selected build before approval or manifest changes rather than silently replacing it with a newer channel.
- Keep automatic background scans non-interactive and continue requiring review before any manifest change.

## 2026.2.40

- Announce every selected project's working branch and GitHub default branch before source publishing begins.
- When a release commit is only on a development branch, detect whether the remote default branch can be advanced without discarding or conflicting with any commits.
- Offer a separate confirmation to push an eligible exact commit to the named default branch, reverify it, and then continue to release confirmation without changing the developer's checked-out branch.
- Never merge diverged histories or bypass branch protection. When a safe direct update is unavailable or rejected, offer to open GitHub's comparison page so the developer can review and merge a pull request.
- In multi-add-on publishing, preserve the pull-request offer for blocked projects after eligible projects are released or declined.
- Stop publishing if the repository is in detached-HEAD state or its working branch changes after the publishing review.

## 2026.2.39

- Offer GitHub's secure browser device sign-in directly from the store-readiness command, then automatically retry the selected add-ons after authorization.
- Distinguish a missing GitHub CLI installation from authentication and other GitHub errors in every GitHub entry point.
- When GitHub CLI is missing, offer to open its official installation page and explain that installation is manual and NVDA should be restarted afterward.

## 2026.2.38

- Add a separate Copy repository link button directly below Copy download link in the released add-on selection dialog.
- Preserve Enter and double-click as shortcuts for copying the selected release asset's direct download link.
- Announce whether a download link or repository link was copied, including the selected add-on or repository name.

## 2026.2.37

- Verify that the exact pushed commit is contained in the repository's current default branch before offering to create a GitHub Release.
- Defer ineligible releases with a focused explanation telling the user to merge the commit into the default branch and rerun publishing.
- Continue offering eligible releases when a multi-add-on publishing operation contains other projects that are not yet on their default branches.

## 2026.2.36

- Stop an isolated discovery worker after 90 seconds even if Windows blocks inside a cloud or removable-drive filesystem call.
- Allow the progress window to be closed without retaining a destroyed control that later progress updates could access.
- Report approved manifest paths that are missing or offline during background checks instead of leaving stale update results in saved status.
- Read valid single-line and multiline triple-quoted NVDA manifest fields completely instead of treating them as empty.

## 2026.2.35

- Centralize stable, beta, release-candidate, and alpha detection in one tested implementation while keeping recursive discovery in the isolated worker.
- Add ETag caching, official release-feed fallback, last-known-good offline fallback, and clear live, validated-cache, feed, or cached status.
- Delay the first automatic check, exponentially back off repeated failures, suppress duplicate failures and unchanged update notices, and announce recovery.
- Keep routine unchanged background release polls silent and avoid launching the discovery worker until an actual project scan is required.
- Require explicit confirmation before newly discovered projects enter monitoring, GitHub publishing, or Store-readiness workflows.
- Add an assignable project-approval command and automatically offer the approval window after a manual discovery scan.
- Show minimum NVDA version, Git branch, manifest change state, and exact paths before compatibility updates, followed by a second confirmation.
- Recheck the official release target immediately before writing selected manifests and defer changes when a newer compatibility family has appeared.
- Revalidate every changed update manifest, roll back failed writes automatically, and add a hash-protected undo command for the most recent update operation.
- Add meaningful scan stages, determinate per-manifest progress, clearer same-family alpha feedback, and an assignable update-check status command.
- Add direct external-worker tests plus release-cache, feed fallback, alpha selection, notification deduplication, and recovery tests.

## 2026.2.34

- Rename the downgrade interface to Set an older NVDA compatibility target and explain that it changes metadata rather than code or NVDA.
- Replace free-text versions with official NVDA Add-on Store versions valid for every selected manifest.
- Offer experimental NVDA targets only to manifests using the beta or development update channel.
- Cache the last successful official version list so the chooser can still open while offline.
- Display paths, Git branches, compatibility bounds, and uncommitted-manifest warnings before changes.
- Add a detailed final confirmation while keeping every selection off by default.
- Revalidate changed manifests and restore the original automatically when validation fails.
- Add an assignable undo command that refuses to overwrite any manifest changed after the compatibility operation.
- Preserve the previous undo operation when a later compatibility attempt makes no changes.
- Keep `minimumNVDAVersion`, source code, add-on versions, and NVDA itself unchanged.

## 2026.2.33

- Set the minimum supported NVDA version to the final 2025 stable release, NVDA 2025.3.3, and the last tested version to the current stable release, NVDA 2026.2.
- Bundle the pure-Python PyYAML parser so issue-template editing does not depend on libraries supplied by a particular NVDA release.
- Ensure the cancel-scan command applies only to an active compatibility scan.
- Avoid reporting the same outdated GitHub release as two separate package-version blockers.
- Add repeatable package building, critical lint checks, continuous integration, release documentation, and a manual accessibility smoke-test checklist.
- Correct the settings documentation and remove its reference to a nonexistent automatic-changes option.

## 2026.2.32

- Explain Markdown in plain language throughout the GitHub issue-template editor.

Earlier changes are recorded in the Git history and release notes.
