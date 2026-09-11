# Release checklist

Automated checks catch packaging and logic regressions. Complete this manual checklist in both NVDA 2025.3.3 and the current stable NVDA release before publishing.

## Installation and startup

- Install the newly built package manually and restart NVDA.
- Confirm NVDA starts without an Add-on Developer Updater traceback.
- Confirm speech, braille, the NVDA menu, and the Input Gestures dialog remain responsive.
- Restart NVDA while no updater operation is running and while a cancellable scan is running.

## Scan and manifest workflow

- Run `NVDA+Alt+Shift+U`; verify progress feedback, completion details, and continued NVDA responsiveness.
- Run the assigned review command; verify focus, Space selection, Control+A, Enter, and Escape.
- Update one disposable manifest and confirm the announcement, formatting preservation, and backup.
- Run the downgrade command against disposable manifests at, above, and below their minimum versions.
- Start a scan and invoke the assigned cancel command. During a non-scan GitHub operation, confirm the command says no scan is running.

## GitHub workflow

- Test signed-in, signed-out, offline, inaccessible-repository, and disabled-Issues cases.
- Verify each selection and confirmation dialog retains focus and every editable field has a meaningful spoken name.
- Verify the download command copies the direct `.nvda-addon` asset URL.
- Open Issues and edit, validate, save, and delete a disposable Markdown and YAML template.
- Confirm no push or release occurs before its explicit confirmation.
- Confirm matching local and GitHub versions produce one clear current-release message.

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
