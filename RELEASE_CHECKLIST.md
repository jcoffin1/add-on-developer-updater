# Release checklist

Automated checks catch packaging and logic regressions. Complete this manual checklist in both NVDA 2025.3.3 and the current stable NVDA release before publishing.

## Installation and startup

- Install the newly built package manually and restart NVDA.
- Confirm NVDA starts without an Add-on Developer Updater traceback.
- Confirm speech, braille, the NVDA menu, and the Input Gestures dialog remain responsive.
- Restart NVDA while no updater operation is running and while a cancellable scan is running.
- Close the progress window during a scan; confirm the scan continues in the background and later progress or completion handling does not raise an error.

## Scan and manifest workflow

- Run `NVDA+Alt+Shift+U`; verify progress feedback, completion details, and continued NVDA responsiveness.
- Confirm newly found projects remain unapproved, nothing is selected initially, and only confirmed maintained copies become available to later workflows.
- Test the assignable approval and update-check status commands, including a project left awaiting approval.
- Verify repeated unchanged updates and failures are announced once, backoff increases after failures, and recovery is announced.
- Test live release lookup, HTTP not-modified cache validation, official feed fallback, offline saved fallback, and same-family alpha feedback.
- Run the assigned review command; verify focus, Space selection, Control+A, Enter, and Escape.
- Confirm review and final confirmation report path, branch, minimum and target versions, and uncommitted-manifest state, and that a newly detected compatibility family defers an outdated selection.
- Update one disposable manifest and confirm the announcement, formatting preservation, and backup.
- Verify post-update validation rollback, successful update undo, missing backup handling, and refusal to overwrite later edits.
- Run the compatibility-target command against multiple disposable manifests and verify that only shared official versions above every minimum are offered, and that experimental versions require beta or development channels for every selection.
- Confirm branch, path, minimum, current target, and uncommitted-manifest status are announced accurately.
- Test confirmation, automatic rollback after simulated validation failure, successful undo, missing backup, and refusal to overwrite a later manifest edit.
- Start a scan and invoke the assigned cancel command. During a non-scan GitHub operation, confirm the command says no scan is running.
- Simulate a blocked external scan and confirm it is stopped at the 90-second safety limit; remove an approved disposable manifest and confirm the next background project check reports the unavailable path.

## GitHub workflow

- Test signed-in, signed-out, offline, inaccessible-repository, and disabled-Issues cases. Confirm every signed-out GitHub entry point, including store readiness, offers device sign-in and retries the original operation.
- Test without GitHub CLI installed; confirm every GitHub entry point offers the official installation page, makes no installation changes itself, and explains how to continue afterward.
- Verify each selection and confirmation dialog retains focus and every editable field has a meaningful spoken name.
- Verify Copy download link copies the direct `.nvda-addon` asset URL and Copy repository link copies the selected repository's GitHub page.
- Open Issues and edit, validate, save, and delete a disposable Markdown and YAML template.
- Confirm no push or release occurs before its explicit confirmation.
- Confirm matching local and GitHub versions produce one clear current-release message.
- Confirm a project whose public summary differs from its internal manifest name uses the public name in every GitHub selection, progress message, confirmation, error, and completion dialog.
- With braille connected, confirm the focused progress field changes at every GitHub stage instead of retaining the preceding stage's text.
- Confirm a historical matching version is recognized without attempting to recreate its release, and a matching release with a missing asset offers a repair rather than a duplicate tag.
- Make an uncommitted change without incrementing an already released manifest version; confirm publishing stops before staging and explains that the version must be incremented.
- Point an existing version tag at another commit and confirm publishing refuses to move, overwrite, or reuse it.
- Add a credential-like file after the publishing review and confirm the immediate pre-stage check stops the operation.
- Enable prereleases and start a manual scan. Confirm the build chooser names stable, beta, release candidate, and alpha independently when each is available; stable is initially selected, every row announces both exact build and manifest target, and Escape closes without scanning.
- Select an alpha or beta target, scan, and update a disposable manifest. Confirm the review and confirmation retain that exact build even when a newer build exists in another channel, and confirm another scan remembers the choice while it remains available.
- Close a manual beta review without updating, allow or trigger a background alpha check, then run the review command. Confirm the saved beta build and its pending add-ons remain unchanged. Update only one of two pending add-ons and confirm the other remains available for review.
- Disable prereleases and confirm a manual scan uses the stable target without offering experimental builds. Confirm automatic checks never open the target chooser or change a manifest.
- Approve a disposable project outside every configured discovery root, then run manual and periodic discovery. Confirm it is still checked directly; remove its manifest and confirm every scan mode reports it unavailable.
- With a populated release cache, simulate an unavailable GitHub releases API while leaving the official releases feed available. Confirm the chooser uses the feed result instead of stale cached choices.
- Publish a disposable development branch whose remote default branch is its direct ancestor; confirm the updater names both branches, offers a separate default-branch update, advances only the remote default branch after Yes, leaves the checked-out local branch unchanged, reverifies it, and then offers the release.
- Create a disposable diverged or protected default branch; confirm the updater does not merge or force-push it and instead offers the correct GitHub comparison page. Select No and confirm only the source branch remains pushed.
- Change branches after opening the publishing review and test a detached HEAD; confirm both operations stop before committing or pushing.

## Store readiness

- Verify clean, changed, unpushed, wrong-owner, missing-release, mismatched-version, malformed-package, and invalid-manifest cases.
- Confirm an outdated GitHub release produces one version mismatch rather than a duplicate package mismatch.
- Confirm only verified add-ons are offered and the official form is never submitted automatically.
- Review every prefilled field and public release note for accuracy before manual submission.

## Final package

- Run `python -m unittest -v`.
- Run `python -m ruff check .`.
- Run `powershell -NoProfile -File .\build.ps1`.
- Confirm the package version, checksum, archive contents, documentation, license files, and clean Git status.
