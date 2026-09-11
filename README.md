# Add-on Developer Updater

Add-on Developer Updater helps NVDA add-on developers discover development projects, track NVDA compatibility releases, update selected manifests safely, publish selected projects to GitHub, and verify release packages before opening the official NVDA Add-on Store submission form.

## Safety defaults

- Automatic checks are disabled by default.
- Background checks stay within previously approved development projects.
- Recursive discovery runs in a separate, below-normal-priority process.
- Manifest changes require explicit selection and receive backups.
- GitHub publishing and release creation require confirmation.
- Store submission checks only the selected projects and never creates or comments on datastore issues.
- Nothing is submitted until the user reviews and submits the official form.

## Commands

- `NVDA+Alt+Shift+U`: check for NVDA compatibility updates.
- `NVDA+Alt+Shift+D`: downgrade selected compatibility declarations.
- `NVDA+Alt+G` or `NVDA+Alt+Shift+G`: select projects to publish to GitHub.
- `NVDA+Alt+Shift+F`: select the newest released `.nvda-addon` file from the signed-in GitHub account and copy its direct download URL.
- `NVDA+Alt+Shift+I`: select an add-on repository and open its GitHub Issues page.
- `NVDA+Alt+Shift+T`: select an add-on repository, edit one of its GitHub issue templates as an accessible form or raw text, or delete it after confirmation.
- `NVDA+Alt+Shift+S`: select projects for Add-on Store readiness review.

Additional commands can be assigned in NVDA's Input Gestures dialog under **Add-on Developer Updater**.

## Requirements

- NVDA 2025.1 or later.
- Windows PowerShell for isolated compatibility discovery.
- Git and GitHub CLI for optional GitHub publishing features.

The download URL command lists the newest non-draft GitHub Release containing a `.nvda-addon` asset for each owned repository. Repositories without a released add-on package are excluded. GitHub queries run on a worker thread, and public, private, archived, and prerelease packages are clearly identified in the selection list.

The Issues command lists owned repositories with recognized NVDA add-on layouts and identifies repositories where Issues are disabled. The issue-template editor supports Markdown and YAML files directly inside `.github/ISSUE_TEMPLATE` on the repository's default branch. Its Form tab exposes template details and GitHub issue-form questions as labeled controls, including question order, type, ID, label, help text, placeholder, options, and required state. Every editable control also has an explicit accessibility name, so NVDA announces its purpose when it receives focus rather than only saying "edit." Labels explain where each value appears on GitHub, and technical YAML question types are presented as plain-language choices such as Short answer, Long answer, Drop-down choice list, Checkbox list, and Information text. The Raw Text tab remains available for advanced or unsupported YAML. Structured saves preserve unrecognized properties but may normalize YAML formatting and comments. The editor validates GitHub's required fields, question IDs, and choices before saving. Saving or deleting requires a separate confirmation and uses the exact revision loaded from GitHub, so a concurrent remote change is rejected rather than overwritten or removed. Archived repositories cannot be edited.

## License

Copyright (C) 2026 Justin Coffin.

Licensed under the GNU General Public License, version 2 or later. See `COPYING.txt`.
