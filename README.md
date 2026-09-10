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
- `NVDA+Alt+Shift+F`: select an add-on repository from the signed-in GitHub account and copy its URL.
- `NVDA+Alt+Shift+S`: select projects for Add-on Store readiness review.

Additional commands can be assigned in NVDA's Input Gestures dialog under **Add-on Developer Updater**.

## Requirements

- NVDA 2025.1 or later.
- Windows PowerShell for isolated compatibility discovery.
- Git and GitHub CLI for optional GitHub publishing features.

The repository URL command recognizes common NVDA add-on layouts, including root, `addon`, `nvda`, and `src` manifests as well as template-based add-on projects. GitHub queries run on a worker thread, and public, private, and archived repositories are clearly identified in the selection list.

## License

Copyright (C) 2026 Justin Coffin.

Licensed under the GNU General Public License, version 2 or later. See `COPYING.txt`.
