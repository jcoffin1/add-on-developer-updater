# Release checklist

Automated checks catch packaging and logic regressions. Complete this manual checklist in both NVDA 2025.3.3 and the current stable NVDA release before publishing.

## Versioning

- Check the NVDA releases page. When a newer NVDA family has become stable, update `CURRENT_RELEASE_FAMILY` in `test_engine.py`, start this add-on's version at `year.release.0`, and test and raise `lastTestedNVDAVersion`.

## Installation and startup

- Install the newly built package manually and restart NVDA.
- Confirm NVDA starts without an Add-on Developer Updater traceback.
- Confirm speech, braille, the NVDA menu, and the Input Gestures dialog remain responsive.
- Restart NVDA while no updater operation is running, while a cancellable scan is running, and during a disposable multi-manifest compatibility operation.
- Close the progress window during a scan; confirm the scan continues in the background and later progress or completion handling does not raise an error.

## Scan and manifest workflow

- Run `NVDA+Alt+Shift+U`; verify progress feedback, completion details, and continued NVDA responsiveness.
- Confirm the Choose add-ons to track window explains in plain words what tracking means and which add-ons to leave unchecked, and that its confirmation and results use the same wording.
- With several copies of one add-on, confirm the tracking window groups them newest first and each line states the public name, version, newest or older, already-tracked copies, GitHub or local-only state, branch, last change, and folder.
- Open Manage tracked add-ons with NVDA+Alt+Shift+A; confirm each line starts with Tracked, Not tracked, or Hidden, that T, N, and H change the selected copy and are announced, that Git details are accurate for tracked copies, and that saving tracks, stops tracking, and hides as confirmed. Rescan and confirm hidden copies are not offered and a stopped copy's pending update is gone.
- In the tracking window after a scan, check Hide the unchecked add-ons and confirm those copies are not offered on the next scan.
- Open the update review window; confirm add-on names, versions, older-copy marks, the dated Add-on Store channel note, and the test-build reminder.
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
- Start a scan and invoke the assigned cancel command. Repeat during disposable compatibility update, downgrade, and undo batches. During a multi-project GitHub publish and release, invoke cancel; confirm the current push or upload finishes, remaining projects are skipped, and the result lists exactly what was completed.
- Scan a workspace larger than 20,000 folders; confirm the completion message states how many levels were searched completely and that shallow projects are found.
- Apply compatibility updates and confirm the results window begins with "Compatibility update complete". Trigger two results in a row and confirm the earlier result remains below the newest.
- Simulate a blocked external scan and confirm it is stopped at the 90-second safety limit; remove an approved disposable manifest and confirm the next background project check reports the unavailable path.

## GitHub workflow

- Force a local builder check to fail after verbose passing tests; confirm the dialog is titled as a failure and includes the final cause from both output streams.
- Test a repository with only a local `file://` remote and one with a local origin plus an owned GitHub fork; confirm the correct GitHub repository is found or created.
- Stage a source-only change and confirm it is counted. Force-stage an `outputs` package and confirm publishing stops before any commit or remote change.
- Change a source file after the confirmation review and simulate a builder that changes source; confirm both operations stop before push.
- Publish a released version with new local changes; confirm the increment offer names old and new versions, updates the manifest, buildVars.py, and readme, rechecks before pushing, and that No skips only those add-ons.
- Publish a project whose minimumNVDAVersion is 2025.3 with a Python 3.12-only f-string; confirm publishing stops before pushing and names the file and line. Confirm the notes warning appears when CHANGELOG.md has no section for the new version.
- Confirm a new release uses the matching CHANGELOG.md section as its notes, and a builder without a Version parameter still produces a verified package.
- Open a Store form for GPL, MIT, and unlicensed projects; confirm publisher and license fields, the blank-license warning, and the settings overrides.
- Verify an unknown upstream/default-branch comparison blocks Store readiness, and verify unsafe or ambiguous package paths are rejected.

- Test signed-in, signed-out, offline, inaccessible-repository, and disabled-Issues cases. Confirm every signed-out GitHub entry point, including store readiness, offers device sign-in and retries the original operation.
- Test without GitHub CLI installed; confirm every GitHub entry point offers the official installation page, makes no installation changes itself, and explains how to continue afterward.
- Verify each selection and confirmation dialog retains focus and every editable field has a meaningful spoken name.
- In every question and result window, confirm NVDA speaks the title and the first line only once, the first line states the question or outcome, Up and Down Arrow and braille move one fact per line, Tab reaches the buttons, Escape answers No or closes, and Enter in the message chooses the highlighted button.
- With progress-bar beeps and spoken percentages enabled in NVDA, confirm a GitHub operation produces no percentage until real step counts appear.
- Enable automatic checks and confirm the optional tone plays before a new background announcement and stays silent when turned off.
- Verify Copy download link copies the direct `.nvda-addon` asset URL and Copy repository link copies the selected repository's GitHub page.
- Open Issues and edit, validate, save, and delete a disposable Markdown and YAML template.
- Confirm no push or release occurs before its explicit confirmation.
- Confirm matching local and GitHub versions produce one clear current-release message.
- Confirm a project whose public summary differs from its internal manifest name uses the public name in every GitHub selection, progress message, confirmation, error, and completion dialog.
- Open GitHub and Store selection in either order; confirm each shortcut opens or returns to its own correctly titled window and that project metadata loads without blocking speech or braille.
- From raw YAML, add an unsupported issue-form question type and confirm the editor refuses to reopen the Form tab without changing the raw text.
- Simulate a GitHub device-login timeout and confirm the hidden GitHub CLI process is terminated before the failure is reported.
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
