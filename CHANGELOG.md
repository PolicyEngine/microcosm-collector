## [0.3.0] - 2026-10-19

### Added

- Project a Microcosm build's saved compiler schema and execution overlay into Orrery JSON with `telemetry_collector.orrery.projection`, a copy of Microcosm's Orrery projection taken from PolicyEngine/microcosm#1295 (commit 8d159a8). It needs only the standard library, not Microcosm's content store, numpy or pandas. Tests compare its output with Orrery JSON saved from Microcosm's own projection for six inputs, including the complete UK dense graph, and require identical bytes, and identical errors one below the node, edge and output-size limits. `provenance.py` records the Microcosm commit and each copied file's git blob ID, and the README describes how to take a later version. Nothing calls the projection yet; the collector's Orrery worker will.


## [0.2.0] - 2026-10-15

### Added

- Carry later `telemetry-service/` changes over from calibration-diagnostics before cutover: `tools/extract-from-calibration-diagnostics.sh` repeats the history-preserving extraction, records the source and extracted commits in `tools/extraction-source.env`, and refuses a history that no longer contains the previous extraction.
- License the collector under the MIT license, with the same text and holder as microcosm-emitter, and declare it in the package metadata.
- Release automatically on every merge to main: bump the version from the changelog fragment types, commit the changelog, run all checks, tag the commit with the bare version, then run the deployment, which stays off until cutover. Pull requests now require a typed changelog fragment, pass actionlint, and report one "Required checks" status for the main ruleset.

### Changed

- Rename the Python distribution from `policyengine-telemetry-collector` to `microcosm-collector`, matching the repo. The import package stays `telemetry_collector`, so carried-over calibration-diagnostics commits still merge cleanly.
- Run the deployment jobs only for a push to main, matching this repo's Workload Identity provider, which accepts tokens only for `refs/heads/main`. A tag, pull request or other branch now skips deployment instead of failing at authentication.
- Develop, verify and deploy the collector from its own repo, PolicyEngine/microcosm-collector, extracted with its history from `telemetry-service/` in calibration-diagnostics. Tests, the deploy script and the image build now run from the repo root. The staging-then-production deployment is ported step for step but stays off until the repo variable `COLLECTOR_DEPLOY_ENABLED` is `true`; until that cutover, calibration-diagnostics keeps deploying production.


## [0.1.0] - 2026-10-09

### Added

- Run the Microcosm collector on Cloud Run: Hugging Face token exchange for short-lived collector credentials, run registration and event ingestion, the dashboard's read API, and Alembic-managed PostgreSQL storage. This version was developed and deployed from `telemetry-service/` in PolicyEngine/calibration-diagnostics and was never tagged; its history up to calibration-diagnostics commit c630c91 is the start of this repo.
