# Versioning policy

Add-on Developer Updater versions follow the current stable NVDA release family.

- The first add-on release for an NVDA family uses `year.release.patch`, such as `2026.2.0`.
- Add-on fixes within that family increment the patch number: `2026.2.1`, `2026.2.2`, and so on.
- When a newer NVDA family becomes stable, the add-on version resets to that family, such as `2026.2`.
- NVDA beta, release-candidate, and alpha build numbers do not change the add-on release family. The family changes when that NVDA release becomes stable.
- `lastTestedNVDAVersion` remains independent: it records the highest NVDA API version actually tested.

Before packaging a release, update `CURRENT_RELEASE_FAMILY` in `test_engine.py` only when a newer NVDA family becomes stable. All release packages must pass the version-policy test.
