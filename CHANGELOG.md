## [0.31.0] - 2026-11-27

### Added

- - The collector reads the result files of a version 2 staging run under `runs/<id>/artifacts/`, where Microcosm's version 2 writer puts them and where US builds will write them (microcosm#1135). It copies `artifacts/reform_validation.json` for the dashboard beside the top-level `reform_validation.json` of version 1 runs, named by that path in the index. A structured run whose diagnostics are in its run folder has its tree built with the `build_manifest` and `release_manifest` its run manifest declares, each checked against its declared SHA-256, and one the folder lacks makes the run `unbuildable`; the tree stores copies of the `reform_validation` and `parent_build_manifest` the run declares, leaving out one the folder lacks or with another digest, also for a run whose diagnostics come from its staged dataset. A structured run (one whose run manifest, or progress when it has no run manifest, is schema 2 or later) never takes these files from its folder's top level, which only schema-1 runs use. `@policyengine/calibration-tree` 0.1.6 adds `artifacts/reform_validation.json` to `STAGING_RUN_FILE_NAMES` and `stagingReformValidationFileName`; `loadCopiedStagingRun` returns a structured run's declared reform validation only when its digest is the declared one, and never a top-level copy, and the Hugging Face staging reader reads the same declared paths. Readers of 0.1.5 read the new index without the new copy.


## [0.30.0] - 2026-11-26

### Added

- An `orrery-inputs` submission may now leave out the execution overlay when its schema is one that Microcosm's reconstruction command (`python -m microcosm.build.uk_runtime.orrery_reconstruction`, microcosm#1297) rebuilt for a UK build made before microcosm#1144. Intake accepts at most one `execution_overlay` instead of exactly one. The Orrery graph handler checks the schema's `microcosm.reconstruction` marker with an unchanged copy of Microcosm's `reconstruction.py` (`telemetry_collector/orrery/projection/reconstruction.py`, copied at Microcosm `8c51b6e`), projects the schema with no overlay and no title, and records the document with `reconstructed: true`. A schema without the marker still needs an overlay, and an overlay file holding JSON `null`, which the projection used to skip, no longer counts as one; a marked schema may not come with an overlay; and a marker that fails Microcosm's checks rejects the submission. Each is rejected with `orrery_inputs_unusable`. `tools/generate-orrery-reconstruction-fixture.py` saves the reconstructed schema the tests submit.


## [0.29.0] - 2026-11-24

### Added

- `backfill-calibration-trees` now fills the calibration tree store in one execution: it stores each country's `releases.json`, covers every older staging run with the reconcile command's code, and stores the original files missing for every build in the store's own manifest, checked against each tree's record of the files it was built from. `--production-manifest-url` is now optional.


## [0.28.0] - 2026-11-19

### Added

- The collector keeps each country's `releases.json` in the calibration tree store: the reconcile command's release runs and the `releases-record` handler store the release repo's head, and an older execution cannot replace a newer copy. `@policyengine/calibration-tree` 0.1.5 adds `readStoredReleasesRecord`.


## [0.27.0] - 2026-11-19

### Added

- The `reconcile-calibration-trees` job now covers staging runs changed from 2026-11-19 on, whatever their status, and `--include-older-runs` lets the backfill cover the older ones: it keeps private gzip copies of the run files the dashboard reads, reads a run again when its folder or stored tree changes, treats a folder with no run documents as not a run, builds a tree for any run with calibration diagnostics, and replaces that tree when the diagnostics or manifests it was built from change. `@policyengine/calibration-tree` 0.1.4 has readers for the copies.


## [0.26.0] - 2026-11-18

### Added

- When the collector builds a calibration tree, it also stores private gzip copies of the build's original files, and `calibration-diagnostics` submissions may carry `parent_build_manifest.json` and `reform_validation.json`. `@policyengine/calibration-tree` 0.1.3 has a reader for the copies.


## [0.25.0] - 2026-11-18

### Added

- Add `POST /v1/calibration-tree-builds`, with which the dashboard's Hugging Face webhook asks the collector to build one country's release or staging calibration trees that no submission builds. A caller presents its Vercel OIDC token, which gets every check read API v2 gives one except the readers check: its `<project ID>:<environment>` must be in the new optional setting `TELEMETRY_VERCEL_OIDC_TREE_BUILD_CALLERS`, a whitespace-separated list apart from the readers, which needs the three `TELEMETRY_VERCEL_OIDC_*` settings (`VercelOidcVerifier.caller`, and `VercelOidcTeamNotAllowedError` for another team's token). The token is checked before the body is read. The body is `{country, kind, hf_repo, hf_ref?, hf_commit?}` with no other field; `hf_repo` must be the collector's own repo for the country and kind (otherwise 422 `repo_not_registered`), and `hf_ref` and `hf_commit` are only recorded. The first request of a country and kind in a 10-minute window of Unix time creates one Cloud Task, `calibration-trees-<country>-<kind>-<window>`, on the worker queue with the existing invoker account, scheduled for the window's end, which runs the worker job with its arguments overridden to the new command below (`CloudTasksWorkerQueue.schedule_run`); later requests in the window get Cloud Tasks' 409 and are answered `already_queued`. Both answers are 202 with `{status, task_id, country, kind, not_before}`. Without the callers setting, or with intake off, the path answers 404 `calibration_tree_builds_not_enabled`. A missing, invalid or not allowed token gets 401 or 403, unreadable Vercel keys 503, and an unavailable request table or queue 503 `worker_queue_unavailable` with `Retry-After: 30`. Each request writes one JSON log document, and each task one row of the new table `calibration_tree_build_requests` (migration `0004_tree_build_requests`), which counts its requests, records when Cloud Tasks accepted it and the execution's attempts, finish and summary; no answer, log document or row holds the token. Both deploy jobs pass the GitHub environment variable `TELEMETRY_VERCEL_OIDC_TREE_BUILD_CALLERS`, which `scripts/deploy.sh` checks with the readers' pattern and sets on the service revisions only.
- Add the `reconcile-calibration-trees` operations command, `--country <cc> [--kind release|staging] [--request-id <task ID>] [--dry-run]`, which the queued task runs in the worker job. It reads the country's repos at their head with the GitHub Actions publisher's listing rules ported to Python: every build the release repo's `releases.json` lists, withdrawn ones included, pinned to the newest last commit of its folder's files, which must hold `calibration_diagnostics.json` and a `release_manifest.json` naming the same release and build, with the record's `published_at` as its time; and every `passed`, `published` or `completed` run of the staging repo, its status from its run manifest, or for a schema-1 run from `runs.json` or its `progress.json` (a folder without a run manifest is a schema-1 run when `runs.json` is schema 1, which is read whenever a run the store lacks has a schema-1 manifest or none, never reading a stored run's manifest; a `runs.json` that isn't a JSON object counts as none, and one that can't be read makes only those runs an error), labelled with its candidate release and timed by its progress's `updated_at` as the publisher does. A build the store holds is `already_stored` with no further read, one a `queued` or `processing` submission of its folder builds is `submission_in_progress`, and the rest are built with the tree backfill's steps, the current default build first, never as the latest. A release run then ends by making the country's latest follow the current build of `releases.json` at the release repo's head, building nothing (`calibration_trees.follow_releases_record`): after the release builds, inside the release lock and outside the manifest lock, which that step takes itself. Its document, `{"latest": {...}}`, and the summary's `latest` say what moved or why the latest stayed; a repo without `releases.json`, a dry run and a lock wait that expires skip it, and a failure it may get past on another attempt leaves the run incomplete. A repo without `releases.json` is `no_releases_record` and builds nothing, a final staging run without calibration diagnostics is `ineligible` as the publisher reports it, and an incompatible staging run is reported on its own while the others continue. Each kind runs inside its own Postgres session advisory lock for the country, waiting up to 15 minutes, and reads the store's manifest once inside it. The command prints one document per build and then a summary, records its attempts on the request's row with `--request-id`, and exits 1 when anything ended in an error, was deferred past the 40-minute limit or waited for the lock in vain, or the latest step failed in a way another attempt may get past, and 2 for an argument error. A new module, `telemetry_collector/releases_record.py`, is the collector's one parser of `releases.json`: it returns every build the record lists, withdrawn ones included, and each release's current build, and `calibration_trees.releases_record_currents` and `current_default_build` now read the record through it, with the same results and refusals.

### Changed

- A `calibration-diagnostics` submission whose publication is refused because another writer already stored a tree for its build now succeeds instead of being rejected as `calibration_tree_refused`. The store's manifest is read after the refusal. A release build counts as stored when the manifest holds a tree of the same release build with the same build ID (a hash of the tree schema version, country, kind, source ID and each source file's SHA-256), and a staged run when it holds any tree of the same run: the manifest keeps one tree per staged run, and the reconcile command may have built it from other files than the submission's, in which case the stored tree is kept. The submission's result then says `already_stored: true` and `made_latest: false` and names the stored tree's `build_artifact_id` and its own `submitted_build_artifact_id`, and it moves no latest. The reconcile command reports such a build `already_stored` instead of `refused`. Any other refused build is still refused, and a manifest that can't be read then fails the attempt so it runs again.


## [0.24.2] - 2026-11-17

### Fixed

- The calibration tree package finds its pinned historical builds when a build is identified by its build folder, `<release>/<build>`, as the publisher identifies a build listed in `releases.json`, as well as by its bare build name. The two audited US local-area builds whose diagnostics have no `schema_version` are read as legacy files again, with their reconstructed weight summary and target-loss attribution, instead of failing every US release reconciliation. Every SHA-256 and target-surface check is unchanged. The package's version is raised to 0.1.2 (microcosm-collector#67).


## [0.24.1] - 2026-11-17

### Fixed

- The calibration tree publication and latest commands can now replace an existing manifest in Vercel Blob. They read the manifest uncompressed, so the ETag their conditional write sends is the strong one the store compares, not the weak one (`W/"…"`) that a compressed response carries; before, every replacement of a manifest the store served compressed failed with "Precondition failed: ETag mismatch". A manifest read with a weak ETag is refused before any write.


## [0.24.0] - 2026-11-17

### Added

- The Orrery read service lists the recorded Orrery graph documents of every run, newest first and a page at a time, when `GET /v1/orrery-graphs` has neither a run ID nor a build ID, so microcosm-runs can show which runs have graphs. That request answered 422 before. `limit` or `before` beside a run or build ID answers 422.


## [0.23.0] - 2026-11-17

### Added

- Add the `backfill-calibration-trees` operator command, which fills the collector's calibration tree store with every build production's public tree manifest lists, before the dashboard reads trees from that store. `--production-manifest-url` (required, `https`) is the dashboard's `https://<dashboard>/api/microcosm/tree-manifest`, which the command requests for each country (`--country`, repeatable; every country by default) with no credential and no redirect followed, through its own client. An answer that isn't HTTP 200, is over 16 MiB, takes over 2 minutes to arrive, isn't JSON, has another schema version or country, or names a latest build that isn't one of its release builds is an `error` for that country only. Each entry is checked before any of its values is used in a Hugging Face path (kind, the country's repo, a 40-hex commit, a `<release>` or `<release>/<build>` release ID and directory, a staging run that is one safe name and not `latest`, a publication time with an offset or none, a label), and an entry that fails is `invalid` while the rest continue. A release build is read from `releases/<releaseDirectory or releaseId>/` at its commit, with its build manifest, release manifest and demographics null when absent; a staging build follows only the staging readers' rules that locate files: a run's declared diagnostics path with its SHA-256 (or, for a schema-1 run, `runs/<id>/calibration_diagnostics.json`) with its build and release manifests, a staged bundle named by a run's `staged_dataset` receipt, or a staged bundle in the release repo. Each tree records production's release ID or run, label, repo, commit and publication time. Only production's latest entry asks to become its country's latest, and each country's latest is built first; production's latest already stored but not the store's latest is published again exactly as stored, from its files at the stored commit with the stored repo and time, so only the latest moves, and one that already is the store's latest needs no work. The store's manifest is read once, with the tree package's new read-only `src/calibration-tree-manifest-read.ts`; an entry the store holds with the same commit and time (to the millisecond) is `already_stored`, with another `mismatch`, and neither downloads anything. Builds run one at a time, each in a removed temporary directory, through the tree handler's shared `build_and_publish`: a file over 256 MiB is `too_large` from its listing, each file is streamed to disk with the new `HuggingFaceReader.download_file` and checked against its listing, the worker runs without the Blob token, and the manifest lock is held only around the publication. Worker and publication exit 2 are `unbuildable` and `refused`; any other failure is `error`, and the next build still runs; no build starts after 40 minutes (`deferred`). Each entry's document says whether its build ID matches production's; staging build IDs are expected to differ, since the worker hashes 4 of a staging build's source files where the GitHub Actions publisher hashes 7. The summary gives the counts, whether the run is incomplete, how many release builds differ from production, and each country's latest. The command exits 1 when anything is `error` or `deferred`, a country failed, or a country's latest wasn't made the store's latest, and 2 for an argument error. `--kind` limits it to release or staging builds, and `--dry-run` reads production's manifest, the Hugging Face listings and the store's manifest, and downloads no diagnostics, runs no tree worker and takes no lock. It needs `DATABASE_URL`, `COLLECTOR_HF_READ_TOKEN` and `COLLECTOR_CALIBRATION_TREE_BLOB_TOKEN`, so it runs as an execution of the worker job with its arguments overridden. `HuggingFaceReader` also gains `list_folder`.

### Changed

- The calibration tree handler's build and publication are now `calibration_trees.build_and_publish`, which the `backfill-calibration-trees` command runs too: the tree worker without the Blob token, then, under the manifest lock, the handler's `releases.json` read and the publication command. A submission's trees, results and rejections are unchanged. `TreeBuildPlan` gains each file's full Hugging Face path (`paths`) and the release folder (`release_directory`, the release ID by default), its `created_at` and `run_id` may be `None`, and `worker_request` takes each role's local copy alone.


## [0.22.0] - 2026-11-16

### Added

- Read API v2 (`GET /v2/runs`, `GET /v2/runs/{run_id}`) accepts, beside `X-Telemetry-Read-Token`, a consumer app's Vercel OIDC token in `Authorization: Bearer`, from the Vercel projects and environments listed in `TELEMETRY_VERCEL_OIDC_READERS` of the team named by `TELEMETRY_VERCEL_OIDC_TEAM_SLUG` and `TELEMETRY_VERCEL_OIDC_TEAM_ID`. The three settings are optional, all or none; without them every bearer token gets 401, and part of them or a malformed value stops startup naming the setting. A token must be RS256, signed by a key from `https://oidc.vercel.com/.well-known/jwks` (the address Vercel's own `@vercel/oidc` verifier uses), with the team's issuer and audience; an invalid or expired token gets 401, a valid token from another team, project or environment 403, and a token checked while Vercel's keys cannot be read and none usable is cached 503. The keys are cached for 10 minutes, fetched again at most once a minute, by one request at a time, and kept usable for 24 hours while fetches fail; an answer with no usable key is a failed fetch, a request whose key is cached never waits for another request's fetch, and one whose key isn't cached waits for it at most 10 seconds before answering 503. The OpenAPI document lists the `VercelOidcToken` scheme as an alternative to `ReadToken` and adds the 403 response. The typed client takes `vercelOidcToken` (a string or a function called for each request) instead of `readToken`, and throws a `TypeError` for both or neither. `CollectorClientOptions` is now a union type alias of `ReadTokenClientOptions` and `VercelOidcClientOptions` rather than an interface, so `interface … extends CollectorClientOptions` no longer compiles; extend one of the two instead. Both deploy jobs pass the GitHub environment variables `TELEMETRY_VERCEL_OIDC_TEAM_SLUG`, `TELEMETRY_VERCEL_OIDC_TEAM_ID` and `TELEMETRY_VERCEL_OIDC_READERS`, which `scripts/deploy.sh` checks and sets on the service revisions only when all three are set. Version 1 is unchanged.


## [0.21.1] - 2026-11-16

### Changed

- Drop the microdata stack's former product name from the calibration tree package's comments and three test names. The published Hugging Face repositories and deployment variables keep their names.


## [0.21.0] - 2026-11-13

### Added

- A `releases-record` submission kind moves a country's latest calibration tree when its release repo's `releases.json` changes without a new build, as after `microcosm-publish-release-build --promote`. Its one file is the `releases.json` the change wrote; its provenance names the release repo, the commit that wrote it, and the release and build it made current. Under the manifest lock, the worker reads `releases.json` at the repo's head and points the latest at the tree recorded for the default release's current build there, older or newer, with the tree package's new latest command (`src/calibration-tree-latest.ts`), which writes only the manifest. That step is also `calibration_trees.follow_releases_record(country, ...)`, which another collector job can call to set a country's latest to its `releases.json` without building anything. This applies to every country the collector lists.

### Changed

- A calibration tree built from a release build's submission now becomes its country's latest only when the release repo's `releases.json`, read at the repo's head commit under the manifest lock just before the publication, names it as its default release's current build, for every country the collector lists, as the GitHub Actions publisher decides. A repo without `releases.json` (a 404 without `RepoNotFound` or `RevisionNotFound`) keeps the `DEFAULT_RELEASES` rule. Any other failed read of `releases.json` returns the job for another attempt before anything is published; a `releases.json` nested more deeply than Python's JSON decoder allows is refused like any unreadable record; a `releases.json` that breaks the record's rules publishes the tree without moving the latest, and the result records `latest_skipped: "releases_record_refused"` with the reason. The worker job's tree handler therefore needs `COLLECTOR_HF_READ_TOKEN` beside `COLLECTOR_CALIBRATION_TREE_BLOB_TOKEN`, and the worker stops with a message naming it when it is missing; the deploy already mounts both on the worker job.


## [0.20.0] - 2026-11-12

### Added

- Check Hugging Face once a day from a production Cloud Run job, `python -m telemetry_collector.huggingface_check`, which the new workflow `.github/workflows/huggingface-check.yaml` starts. The check requests every dataset the collector reads with its read token and fails on any answer but HTTP 200. For datasets switched on, it also checks `releases.json` with the rules in the microcosm-emitter-interface package: one record and no `latest*.json` files, the record's rules, and every published file against the SHA-256 in its build's release manifest, read from the manifest's `artifacts` map. That part is off for every dataset until the package is a dependency. The workflow reads each execution's log entries and keeps one GitHub issue per problem, labelled `huggingface-check`: it opens, updates, reopens and closes them, and keeps one issue for a run that reports no result, including one that fails before the check reports. A completed run closes an issue only for a check it finished. A run by hand is a dry run unless told otherwise. The production deploy updates the job before it changes the service, from three production environment variables.


## [0.19.0] - 2026-11-11

### Added

- Add the `reproject-past-builds` operator command, which publishes Orrery graph files for past builds made after microcosm#1151 whose staged folder holds a graph schema and an execution overlay. For each such build in the inventory of past builds it makes the `orrery-inputs` submission the build would have sent, owned by `microcosm-collector`. The submission holds the folder's schema, overlay and upstream files, read at the inventory's revision. Its provenance gives the build's run ID as run and build ID, its country as Microcosm registers it (`GB` for `uk`), and the Hugging Face folder at the newest last commit among the submitted files (`HuggingFaceReader.last_commits`). `licensed_data` is true unless the build was staged in a public dataset. The submission ID is the request's hash, so a second run changes nothing unless a submitted file changed. Each build is first checked from Hugging Face's listings (the submission rules with the listed sizes, the Orrery handler's 32 MiB input limit, and a last commit for each submitted file) before anything is downloaded, and each download must match its listing's size and SHA-256 or git blob ID. The command creates the files create-only in the intake bucket (`GoogleCloudStorageIntake.create_object`), accepts the submission as completion does, and runs its job at once through the worker's `run_job`, so the document is validated, published and recorded as a new build's is, and recorded `reconstructed`, which the Orrery handler now sets for every submission `microcosm-collector` owns; a task retry continues a job its killed attempt left `processing`. Each build's outcome is printed as it is produced, then a summary of the run listing the incomplete builds and the projected builds whose document isn't published; an error in one build is its `error` outcome and the next build is still projected. The command exits non-zero when any build is incomplete: an `error`, or a build that would be projected but GitHub stopped answering before it was classed. The inventory of past builds now marks such builds with `class_retry_later`. Every other build is listed as `left_out` with the reason, and `--dry-run` makes the same checks and lists what would be projected, downloading no graph inputs and writing nothing. The command needs `DATABASE_URL`, `COLLECTOR_HF_READ_TOKEN`, `COLLECTOR_INTAKE_BUCKET` and `COLLECTOR_ORRERY_BUCKET`, and permission for the worker's account to create objects in the intake bucket for the run.


## [0.18.0] - 2026-11-11

### Added

- Add the `past-builds` operator command, which prints an inventory of past Microcosm builds as JSON and stores nothing: one entry for each `staged/<run_id>/` folder in each country's Hugging Face release dataset, read at the commit its `main` points at, and for each run in `telemetry_runs` (UK runs, which Microcosm registers as `GB`, listed as `uk`), joined by run ID. It prints a document with when the inventory was made, each dataset's revision and the totals, and then one document for each build. Each build gives its Microcosm commit, from its folder's `staged_manifest.json` (`summary.git_commit`) or else from its run's `run` events (`details.identity.git_commit`), with `commit_source` saying which; its class (`after_1151`, `between_1144_and_1151`, `before_1144` or `unknown`, with the reason), worked out with GitHub's compare API without a token, one or two requests for each distinct commit, with only that build `unknown` when GitHub has no such commit and every later commit `unknown` after a rate-limit answer or any other failure, and with staged builds that can be rebuilt checked first, then other staged builds, then recorded runs without a staged folder, each group newest first (descending run ID); the graph files the folder holds; and what can be rebuilt from Hugging Face (`full` with the schema and execution overlay, `declaration_only` with the schema alone, or `none`). Builds made before microcosm#1144, and builds made before microcosm#1151 with nothing to rebuild from, are listed as skipped, with the reason. `telemetry_collector.past_builds.list_past_builds` returns the same inventory to code. The command needs `DATABASE_URL` and `COLLECTOR_HF_READ_TOKEN`, so it runs as an execution of the worker job with its arguments overridden.


## [0.17.0] - 2026-11-10

### Added

- Serve Orrery graph files to microcosm-runs alone, from the Orrery read service: a separate Cloud Run service, `<service>-orrery` in each environment, that every release deploys from the collector's image once the collector's service is live. It allows no unauthenticated calls, keeps Cloud Run's invoker check on, has no custom audience, so that a caller's identity token must be for the service's own URL, and its IAM policy grants the Cloud Run Invoker role to `TELEMETRY_ORRERY_READER_SERVICE_ACCOUNT` alone, which the deploy sets and reads back on every release; so Cloud Run checks each caller's Google identity token, and the service's code verifies none. Anyone granted, on the project, its folder or the organization, any role that includes `run.routes.invoke` can call it too, and so can anyone who may make identity tokens as the reader account (`iam.serviceAccounts.getOpenIdToken` on it); the README's "Who else can call it" lists the predefined roles that include each permission, from IAM's permissions reference. Each release then calls the service without a token and fails unless Cloud Run refuses the call with 401 or 403. `GET /v1/orrery-graphs?run_id=…&build_id=…` lists a run's or a build's recorded documents, each with its fields, its current flag (the newest `created_at` among the documents with its `country_code` and `build_id`, ties ordered by `document_sha256` and then by `submission_id`, the larger first) and its effective visibility, and `GET /v1/orrery-graphs/<submission_id>/<document_sha256>/<file_name>` returns `graph.orrery.json` or an `upstream-<sha256>.json` file from the private Orrery bucket. A file is read twice at the generation it was found at, holding at most one 1 MiB part: first to check that its SHA-256 is the document's recorded `document_sha256` or the one in the upstream file's name, then, only on a match, to send it (500 `orrery_graph_digest_mismatch` or `orrery_graph_changed` otherwise, never served). Each Cloud Storage request gives up after 10 seconds without connecting or without data, and each call retries for at most 30 seconds. A bucket or database that doesn't answer gives 503 (`orrery_storage_unavailable`, logged with the object or folder and the error's type, or `orrery_catalogue_unavailable`), and every answer of the read service's routes and middleware has `Cache-Control: private, no-store` (Cloud Run's own 401 and 403, and the plain-text 500 for an unexpected exception, don't). A document is private when its row is, or when a recorded submission with licensed data has the run ID or the build ID (whatever its country code) of the document or of a submission that produced one of its upstream files, matched by the file's SHA-256. A client must compare a served file's SHA-256 with its `ETag` before caching or showing it, since a read that fails after the answer has started ends the body early under a 200. The read service needs `COLLECTOR_ORRERY_BUCKET` and `DATABASE_URL` and refuses to start without either; the collector's main service mounts none of its routes and gets neither Orrery setting.

### Changed

- The Orrery graph handler rejects an `orrery-inputs` submission with `orrery_inputs_unusable` when an `upstream` file is not named `upstream-<its SHA-256>.json`, with 64 lowercase hex characters, as Microcosm names the files it copies. This replaces the narrower rejection of an upstream file named `graph.orrery.json`.

### Fixed

- Running the database migrations no longer turns off the collector's own loggers in the same process: the Alembic environment's logging setup keeps every logger its configuration doesn't name.


## [0.16.0] - 2026-11-10

### Added

- Publish Orrery graph files from `orrery-inputs` submissions where the worker job has its environment's private Orrery bucket (`COLLECTOR_ORRERY_BUCKET`, which the deploy sets on the worker job only from `TELEMETRY_ORRERY_BUCKET`): the worker projects the schema and execution overlay, removes every link the published document could not resolve (evidence references keep their label and SHA-256 but lose their link, whatever its scheme; elsewhere `https://` links stay; links to submitted upstream files stay everywhere) and gives each node or edge that lost a link the projection's revision of the record as published, validates the document with the pinned Orrery validator, writes it and the upstream files it links to, the only upstream files it downloads, create-only under `graphs/<submission_id>/<document sha256>/`, and records it in the new `orrery_graph_files` table (migration `0003_orrery_graph_files`) with its run, build, versions and visibility, `private` for licensed builds. The schema and overlay are read as Microcosm's command reads its input (at most 32 MiB, strict JSON in UTF-8, nested no deeper than Python's JSON decoder can parse); an input that fails these rules, or a `TypeError`, `ValueError` or `RecursionError` from the projection, rejects the submission with `orrery_inputs_unusable`, and any other projection error fails the attempt. The worker's handlers now come from one registry, `telemetry_collector.handlers`.

### Fixed

- The worker now records a handler's rejection, an error's message and a handler's result with U+FFFD in place of each NUL and unpaired surrogate, which PostgreSQL's `jsonb` cannot store. Before, such text made the attempt crash while recording it, so a submission that should have been rejected ended `failed` once its attempts ran out.


## [0.15.0] - 2026-11-09

### Added

- The collector checks the five `details` keys microcosm-emitter 0.14.0 and later write themselves (a heartbeat's `reading` and `sleep`, a run start's `environment`, a stage's `stage_span` and progress's `outer`). A key that doesn't match its model is removed from the stored event, named in `details.dropped_emitter_details` and logged, and the rest of the event is accepted. Both read APIs serve the run's machine description, the current attempt's latest reading with its time, and every attempt's sleeps: version 1 as `run_manifest.environment`, `progress.reading` and a top-level `sleeps`, version 2 as the run's `environment`, `reading` and `sleeps`, typed in its OpenAPI document and the TypeScript client. Values stored before these checks are checked again when served (microcosm-collector#46).


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
