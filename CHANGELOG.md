## [0.6.1] - 2026-10-27

### Fixed

- Remove "emSign ECC Root CA - G3" and "AAA Certificate Services", the roots that the compromised eMudhra intermediate CA certificates chain to, from the image's own certificate stores. The image now builds from `ghcr.io/astral-sh/uv:python3.13-trixie-slim`, a floating tag that Astral rebuilds with each uv release, as PolicyEngine's other images use floating tags. Its current build has Debian's `ca-certificates` 20250419+deb13u1, which drops both roots, and pip 26.2.2, whose vendored certifi 2026.10.19 lacks the first; the previous base, `python3.13-bookworm-slim`, has not been rebuilt since February and held both. This also moves the image from Debian 12 to Debian 13, Python 3.13.11 to 3.13.18 and uv 0.9.30 to 0.13.6. The collector's Hugging Face check already verifies against the locked certifi and is unchanged.


## [0.6.0] - 2026-10-27

### Added

- Record the decisions on the collector's Google Cloud project and hostname (plan decisions D2 and D3: a dedicated project, behind a load balancer with one hostname per environment) in `docs/project-and-domain-decision.md`, and the steps for the hostname and then the move, including the Postgres dump and restore and rollback, in `docs/project-move-runbook.md`.


## [0.5.0] - 2026-10-27

### Added

- Accept artifact submissions, switched off unless `COLLECTOR_INTAKE_ENABLED` is `1`: `POST /v1/submissions` returns signed upload instructions for a private bucket, `POST /v1/submissions/{id}/complete` checks the stored objects and dispatches a worker job through Cloud Tasks, and status and receipt endpoints report the result. The token exchange issues `artifacts:submit` credentials on request and states each token's scope; while intake is off, the exchange and every submission path answer 404 with the code `intake_not_enabled`. A worker framework, run as a Cloud Run Job, re-hashes each file, calls the handler registered for its kind, and gives handlers the disclosure rule (only Orrery outputs of licensed builds stay private). `python -m telemetry_collector.operations` recovers jobs whose attempt ended without a result and reprocesses final submissions in a new round. Alembic revision `0002_artifact_intake` adds the submission and job tables and grants nothing: the runtime database user's access to them comes from the migration user's default privileges, which each environment sets once by hand before this release is deployed (see the README's "Database access").


## [0.4.1] - 2026-10-27

### Changed

- Test the trusted roots of the connection `HuggingFaceAuthenticator` opens to `huggingface.co`, compared by SHA-256 certificate fingerprint, instead of the roots in httpx's default SSL context compared by subject name. The test now also fails if the authenticator builds its own client whose SSL context trusts "emSign ECC Root CA - G3" or "AAA Certificate Services". Its comments now name certifi 2026.10.19 as the first fixed version in advisory GHSA-6q48-6c7w-9gw2, instead of calling it the first release without "emSign ECC Root CA - G3" (2026.10.18 already lacked it). The service's code and locked dependencies are unchanged.


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
