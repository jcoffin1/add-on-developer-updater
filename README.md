# Add-on Developer Updater

Add-on Developer Updater helps NVDA add-on developers discover development projects, track NVDA compatibility releases, update selected manifests safely, publish selected projects to GitHub, and verify release packages before opening the official NVDA Add-on Store submission form.

## Quick start

1. Press `NVDA+Alt+Shift+U` to scan your normal development folders.
2. Approve only the development projects you maintain when the discovery window opens.
3. Assign **Review previously discovered NVDA add-on compatibility updates** in NVDA's Input Gestures dialog, run it, and select the manifests you intend to change.
4. Use the GitHub publishing command only after reviewing the changed source and documentation.
5. Use `NVDA+Alt+Shift+S` for the final Store-readiness check. The official submission form still requires your review and manual submission.

## Safety defaults

- Automatic checks are disabled by default.
- Newly discovered projects require explicit approval before monitoring or publishing workflows can use them.
- Routine unchanged background checks are silent and do not launch recursive discovery.
- Recursive discovery runs in a separate, below-normal-priority process.
- Manifest changes require explicit selection and receive backups.
- GitHub publishing and release creation require confirmation.
- Store submission checks only the selected projects and never creates or comments on datastore issues.
- Nothing is submitted until the user reviews and submits the official form.

## Commands

- `NVDA+Alt+Shift+U`: check for NVDA compatibility updates.
- `NVDA+Alt+Shift+D`: set an older last-tested NVDA target for selected manifests.
- `NVDA+Alt+G` or `NVDA+Alt+Shift+G`: select projects to publish to GitHub.
- `NVDA+Alt+Shift+F`: select the newest released `.nvda-addon` file from the signed-in GitHub account and copy its direct download URL.
- `NVDA+Alt+Shift+I`: select an add-on repository and open its GitHub Issues page.
- `NVDA+Alt+Shift+T`: select an add-on repository, edit one of its GitHub issue templates as an accessible form or raw text, or delete it after confirmation.
- `NVDA+Alt+Shift+S`: select projects for Add-on Store readiness review.

Additional commands, including **Review newly discovered add-on projects awaiting approval**, **Report add-on update-check status**, **Undo the most recent discovered compatibility updates**, and **Undo the most recent compatibility target changes**, can be assigned in NVDA's Input Gestures dialog under **Add-on Developer Updater**.

Release lookup uses one shared implementation for stable releases, release candidates, betas, and direct NVDA alpha snapshots. It uses GitHub ETags, falls back to the official releases feed, and finally uses saved last-known-good information when live services are unavailable. The status command reports which source was used, the detected release and compatibility family, check and scan times, project counts, pending updates, validation failures, and the expected next automatic check. Automatic checks wait after NVDA starts, back off after repeated failures, and announce only a new or changed action, first fallback to cached data, or recovery. A different alpha build in the same compatibility family is identified without claiming that another manifest change is required.

Discovery and approval are separate. Manual and periodic scans can find projects, but each new project remains unavailable to update, GitHub, and Store workflows until the user selects it in the approval window and confirms that it is a maintained development copy. Compatibility-update review shows each path, minimum and proposed NVDA versions, Git branch, and manifest change state. Nothing is selected by default, and a final confirmation appears before writing. The official release is checked again immediately before the update; a newer compatibility family defers the operation for a fresh scan. Each change is revalidated and automatically rolled back on failure. The update undo command restores the most recent group only when no manifest has been edited afterward.

The compatibility-target command loads the version list recognized by the official NVDA Add-on Store away from NVDA's main thread and saves the last successful response for offline fallback. Select add-ons first; the version list then contains only older targets shared by every selection and never permits a target below an add-on's `minimumNVDAVersion`. Experimental targets are available only when every selected manifest uses the `beta` or `dev` update channel. Each row identifies the path, Git branch, current and minimum versions, and whether the manifest has uncommitted changes. A final confirmation explains every proposed change and warns about dirty manifests. The operation changes only `lastTestedNVDAVersion`; it does not lower the minimum version or alter NVDA, source code, or the add-on version. Every changed manifest is backed up and revalidated, and a failed validation restores the original automatically. The undo command restores the most recent set only when the manifest is byte-for-byte unchanged since the compatibility operation, so later edits are never overwritten. If source already uses newer NVDA APIs, check out the appropriate older source branch instead.

## Requirements

- NVDA 2025.3.3 or later. The add-on is currently tested through NVDA 2026.2.
- Windows PowerShell for isolated compatibility discovery.
- Git and GitHub CLI for optional GitHub publishing features.

The download URL command lists the newest non-draft GitHub Release containing a `.nvda-addon` asset for each owned repository. Repositories without a released add-on package are excluded. GitHub queries run on a worker thread, and public, private, archived, and prerelease packages are clearly identified in the selection list.

The Issues command lists owned repositories with recognized NVDA add-on layouts and identifies repositories where Issues are disabled. The issue-template editor supports Markdown and YAML files directly inside `.github/ISSUE_TEMPLATE` on the repository's default branch. Its Form tab exposes template details and GitHub issue-form questions as labeled controls, including question order, type, ID, label, help text, placeholder, options, and required state. Every editable control also has an explicit accessibility name, so NVDA announces its purpose when it receives focus rather than only saying "edit." Labels explain where each value appears on GitHub, and technical YAML question types are presented as plain-language choices such as Short answer, Long answer, Drop-down choice list, Checkbox list, and Information text. The editor explains that Markdown is ordinary text with optional symbols for headings, bullets, and links; no special formatting is required. The Raw Text tab remains available for advanced or unsupported YAML. Structured saves preserve unrecognized properties but may normalize YAML formatting and comments. The editor validates GitHub's required fields, question IDs, and choices before saving. Saving or deleting requires a separate confirmation and uses the exact revision loaded from GitHub, so a concurrent remote change is rejected rather than overwritten or removed. Archived repositories cannot be edited.

## Privacy and network activity

- Scans read folder names, manifests, and selected project files locally. Cloud folders are scanned only through copies synchronized and accessible on the computer.
- Background checks inspect only approved projects and configured development roots. Full-drive discovery is manual and opt-in.
- NVDA release checks contact NV Access and the official NVDA GitHub repository.
- GitHub commands use the account already authenticated in GitHub CLI. Repository metadata, issue templates, source commits, and release packages are sent to GitHub only for the command the user confirms.
- The Store-readiness check downloads the selected GitHub release package for local verification. It does not submit a form, create a datastore issue, or store a GitHub password or token.
- State, release-cache, and manifest-backup files remain in the active NVDA configuration folder.

## Development and release checks

Run `python -m unittest -v` for the unit suite, `python -m ruff check .` for critical static checks, and `powershell -NoProfile -File .\build.ps1` to create and audit the package. The manual checks that cannot be proven by unit tests are listed in `RELEASE_CHECKLIST.md`.

## License

Copyright (C) 2026 Justin Coffin.

Licensed under the GNU General Public License, version 2 or later. See `COPYING.txt`.

The bundled pure-Python YAML parser is PyYAML 6.0.3, distributed under the MIT license. Its license is included with the vendored files.
