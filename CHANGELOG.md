## [0.14.0] - 2026-11-09

### Added

- Build calibration trees from `calibration-diagnostics` submissions where the worker job has its calibration tree Blob token (`COLLECTOR_CALIBRATION_TREE_BLOB_TOKEN`): a build folder gives a release tree of that build and a staged bundle a staging tree of its run, built by the tree package's Bun worker without the token and published under a Postgres lock by a new publication command that writes the manifest with a compare-and-swap. A release build becomes its country's latest when its release is the country's default release (the UK's for now). The image now holds Bun 1.4.2 and the package's production install, and CI checks that the built image builds a tree.


## [0.13.1] - 2026-11-09

### Changed

- The calibration tree package's version is raised to 0.1.1, so that the publish workflow has a version npm doesn't have yet. Its first publication through npm's trusted publishing, a manual run from `main` after the one-time setup, can then follow that setup within the 48 hours npm allows; the README's "Publishing to npm" now describes that order (microcosm-collector#48).


## [0.13.0] - 2026-11-09

### Added

- Validate Orrery graph documents with one pinned `@axiom-foundation/orrery` (0.6.2) under Node: `orrery-validator` accepts exactly the graph file contents microcosm-runs' publication accepts (strict UTF-8 JSON, `parseGraphDocument` and `checkSources`), and its refusals never repeat a link's value, always encode as UTF-8 and never carry a NUL, and `telemetry_collector.orrery.validation` runs it from Python with only `PATH` and a UTF-8 locale in Node's environment, telling a refused document apart from a validator that could not run. The image now holds Node 24.21.0 and the installed validator, which adds about 130 MB, and CI checks the validator inside the built image. Nothing in the service or the worker validates documents yet.


## [0.12.0] - 2026-11-09

### Added

- Add the calibration tree package, `packages/calibration-tree`, copied from calibration-diagnostics' `frontend/packages/calibration-tree` with a record of the commit and files it came from. A Bun worker, `src/calibration-tree-worker.ts`, builds one release's tree from local files and writes the same bytes as the publisher; pull requests run the package's tests, including the recorded golden trees, and its typecheck. `.github/workflows/publish-calibration-tree.yaml` publishes it to npm as `@policyengine/calibration-tree` through npm's trusted publishing, and stays switched off until `CALIBRATION_TREE_NPM_PUBLISH_ENABLED` is `true`; the README lists the one-time setup. Nothing in the service runs the worker yet, and the deployed image is unchanged.


## [0.11.0] - 2026-11-09

### Added

- Sign session tokens with ES256 once `TELEMETRY_JWT_SIGNING_KEY` holds a P-256 private key, delegated tokens for builds on other machines included, and publish its public key at `GET /.well-known/jwks.json`. Each ES256 token names its key in the `kid` header, the RFC 7638 thumbprint of the public key. While `TELEMETRY_JWT_SECRET` is still configured, HS256 tokens signed with it are accepted too, so tokens issued before the switch keep working until they expire; once the key is set the secret is optional, and removing it from the deployment later retires HS256, no sooner than 24 hours after the last revision that issued HS256 tokens stopped serving, the longest a delegated token lasts. Either variable set but blank stops startup. Deployment mounts the key from the Secret Manager secret named by the new `TELEMETRY_JWT_SIGNING_KEY_SECRET_NAME` variable in the `staging` and `production` environments, and checks that the secret exists before changing traffic.


## [0.10.0] - 2026-11-09

### Added

- Add `@policyengine/microcosm-collector-client` in `packages/collector-client`, a typed TypeScript client for read API v2 whose types are generated from the OpenAPI document and whose tests replay the recorded interactions. Each release gives it the collector's version. Once production runs a release, the Push workflow's new `pack-client` job packs it without an OIDC token, and `publish-client` publishes that tarball to npm with trusted publishing, installing nothing and running no package script. Both run only when the repo variable `COLLECTOR_CLIENT_PUBLISH_ENABLED` is `true`, which it is not yet. The README lists the manual steps that switch publishing on, starting with making the Workload Identity provider accept only tokens from the `staging` and `production` environments.
- Serve read API v2: `GET /v2/runs` lists runs as v1 does, with typed answers, and `GET /v2/runs/{run_id}` returns a run's state together with every stored event with the fields its producer sent, the state folded from exactly those events, with all timestamps in UTC. Both take the dashboard read credential. `contracts/read-api-v2` holds the OpenAPI document, which the collector also serves at `GET /v2/openapi.json`, and recorded interactions; tests check the collector against both, from the in-memory store and from PostgreSQL, and record version 1's answers so that the dashboard's reads cannot change unnoticed.


## [0.9.0] - 2026-11-06

### Added

- Switch artifact intake on in staging and production from the deployment. `scripts/deploy.sh` passes `COLLECTOR_INTAKE_ENABLED=1` and the other intake settings to both service revisions in both environments, and every release updates the worker and recovery Cloud Run jobs to the released image with their whole configuration, after the migration and before the candidate revision, without ever creating either job. Each environment's worker job mounts that environment's own Hugging Face read token as `COLLECTOR_HF_READ_TOKEN`, listed with `DATABASE_URL` in one `--set-secrets`; staging and production have separate tokens and secrets, each readable only by its own environment's worker service account. Before anything changes, the deploy refuses an intake name the collector's own patterns would refuse, and a job that does not exist, runs as another service account than the deploy gives it, or connects to another environment's Cloud SQL instance; it also checks that the environment's token secret exists. The deploy reads seven new `TELEMETRY_*` variables and `TELEMETRY_HF_READ_SECRET_NAME` in each environment; staging needs all eight created, production six, and a deploy without one stops before it authenticates.


## [0.8.0] - 2026-11-06

### Added

- Read Hugging Face from worker jobs with the collector's own fine-grained read token, `COLLECTOR_HF_READ_TOKEN`, mounted from Secret Manager. `telemetry_collector.huggingface_read` refuses a missing or malformed token and any generic `HF_TOKEN`, lists the datasets the collector reads and the token's scope, and reads dataset information, file lists and files, sending the token only to Hugging Face. Nothing in the service or the existing worker reads Hugging Face yet; the README lists the manual steps that create the token, its secret, its access and its mount.


## [0.7.3] - 2026-11-05

### Changed

- The README describes the collector's own Google Cloud project: each environment behind its own hostname on a global external Application Load Balancer, deployments authenticating through a provider and deployment account in that project, and calibration-diagnostics' old `telemetry-service.yml` redeploy never to be run.


## [0.7.2] - 2026-11-04

### Fixed

- The project-move runbook keeps database passwords out of gcloud's log files: gcloud writes every flag's value to `~/.config/gcloud/logs`, so the commands that pass a password in `--password` now run with `CLOUDSDK_CORE_DISABLE_FILE_LOGGING=1`.


## [0.7.1] - 2026-11-04

### Changed

- Staging's Cloud Run service now scales to zero between releases: `scripts/deploy.sh` deploys it with `--min 0`, chosen from `DEPLOYMENT_ENVIRONMENT`. Production still keeps one instance running (`--min 1`), and a service deployment for any other environment name fails before calling `gcloud`.


## [0.7.0] - 2026-10-29

### Added

- Issue telemetry tokens for builds on other machines. The token exchange accepts an optional `lifetime_seconds`, a positive integer, only with scope `telemetry:write` (with `artifacts:submit` it answers 422); `null` is the same as leaving it out. It grants the requested lifetime up to 86,400 seconds, a constant in the code rather than a deployment setting, reports the granted lifetime in `expires_in`, and marks the token with the claim `"delegated": true`. Every endpoint that accepts `telemetry:write` accepts such a token. An operator's workstation can then exchange its Hugging Face token at launch for a token that a build without a Hugging Face credential, such as a Microcosm stage on Modal, uses for up to 24 hours. Exchanges without `lifetime_seconds` are unchanged.


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
