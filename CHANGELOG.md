# Changelog

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
