## [0.4.0] - 2026-10-27

### Added

- Document the artifact submission contract (`docs/artifact-submissions.md`): the `calibration-diagnostics` and `orrery-inputs` kinds, their file roles and how the emitter's roles map to them, provenance syntax, collector-derived identifiers, statuses, every collector and storage answer and what a client does with it, how a client recognises a collector without intake, job recovery and reprocessing, receipts, size limits, and the disclosure rules: intake storage is private, Orrery outputs of licensed builds stay private until cleared, calibration data may be public, and native evidence is never published.


## [0.3.3] - 2026-10-22

### Fixed

- - Lock certifi 2026.10.19, which removes the "emSign ECC Root CA - G3" root. The previously locked certifi 2026.7.22 trusted eMudhra intermediate CA certificates whose private keys can be recovered from their public certificates (https://emudhra.we-have-a-problem.com), so the deployed image could accept a forged certificate for `huggingface.co` when it verifies a Hugging Face token. A test now fails if the locked certifi is older than 2026.10.19, or if the trust store httpx uses by default contains "emSign ECC Root CA - G3" or "AAA Certificate Services".


## [0.3.2] - 2026-10-21

### Fixed

- Apply each stored event batch incrementally instead of re-materializing the whole run: `PostgresTelemetryStore.append_events` re-read every stored event of a run and rebuilt the run for every batch, which is O(n²) over a run and held the run's row lock for longer as a build went on. The store now folds each batch onto the run's stored columns, with the same run state, database contents and HTTP responses as before, and no schema change. Carried over from PolicyEngine/calibration-diagnostics#219 (head `3dfbe05`) with `tools/extract-from-calibration-diagnostics.sh`.


## [0.3.1] - 2026-10-20

### Fixed

- Refuse to carry over `telemetry-service/` changes when the recorded extraction is not in the history of `HEAD`: `tools/extract-from-calibration-diagnostics.sh` exited successfully after a squash-merged or rebase-merged carry-over pull request, although merging its output would add the carried-over commits a second time. Carry-over pull requests must be merged with a merge commit; the README describes the rule and how to recover.


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
