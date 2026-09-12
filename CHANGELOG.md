# Changelog

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
