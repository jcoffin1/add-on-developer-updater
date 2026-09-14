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
- Push a disposable development branch whose commit is not on the repository's default branch; confirm source is pushed but no tag or release is created, then merge it and confirm the next publishing run permits the release.

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
