# Microcosm telemetry collector

This service accepts live build telemetry from Microcosm and exposes a
read-only API to the calibration dashboard. It does not store dataset artifacts
or Hugging Face credentials.

## Authentication

Microcosm sends its ambient Hugging Face token only to
`POST /v1/auth/huggingface/exchange`. The collector validates that opaque token
against Hugging Face's `whoami-v2` endpoint and confirms current membership in
the `policyengine` organization. It then returns a short-lived collector JWT.
That credential registers runs and uploads events; the collector verifies that
the authenticated Hugging Face user owns the referenced run and that each
producer was registered first. The raw Hugging Face token is neither logged nor
written to Postgres.

The dashboard query endpoints require `X-Telemetry-Read-Token`. The dashboard
server retrieves this credential from Google Secret Manager at runtime. Vercel
authenticates using its OIDC identity; the credential is never returned to the
browser or passed through GitHub Actions.

## Responses a producer acts on

Microcosm's delivery service (from PolicyEngine/microcosm#1147) treats any 4xx
other than 401, 408 and 429 as the collector's final answer for a run: it stops
delivering that run, keeps its telemetry on the build machine and warns once.
A 401 on registration or events makes it exchange its token again (a 401 or
403 from the token exchange itself is final); 408, 429, 5xx and the
maintenance revision's 503 are retried. So the collector answers 4xx only when
sending the same request again cannot succeed:

- `403`: the Hugging Face user is not a member of the organization (token
  exchange), the run belongs to another user, the producer was not registered
  for it, or an event names a different run than its path.
- `404` on `POST /v1/runs/{run_id}/events` ("Run is not registered"): the
  delivery service sends events only after its registration returned 201, so
  this means the run disappeared after it registered, for example a database
  reset while a build was running. That build's remaining telemetry stays local.
- `409` on `POST /v1/runs`: the run id is already registered with different
  metadata.
- `413`: the request body is larger than 1 MiB.
- `422`: the request does not match the event schema, including a `blocked`
  event without a valid block (a safe-identifier `phase`, a
  `blocking_failure_count` of at least 1, and `blocking_gate_ids`).

## Local development

```bash
uv sync --dev
uv run python -m telemetry_collector.migrate
uv run uvicorn telemetry_collector.app:create_app_from_environment \
  --factory --host 127.0.0.1 --port 8080 --no-access-log
```

Before running the migration or service, provide the database connection, JWT
signing secret, and dashboard read credential through the documented runtime
environment variables. Use at least 32 random characters for the JWT signing
secret and at least 24 for the dashboard read credential. Use Microcosm's
loopback-only development option when testing ingestion. Production Microcosm
builds use the fixed collector origin shipped by the package and do not accept
a destination environment variable.

## Tests

```bash
uv sync --locked --dev
uv run pytest -q
bash tools/check-quality.sh
```

`tools/check-quality.sh` runs Ruff's lint and format checks and syntax-checks
every shell script in `tools/` and `scripts/` with `bash -n`, as CI does.

The PostgreSQL integration tests and the migration checks need a disposable
database whose name contains `test`, given as `TEST_DATABASE_URL`. Without it
they are skipped. CI runs them against PostgreSQL 16 and also runs
`python -m telemetry_collector.migrate` and `alembic check` against that
database.

## Orrery projection

`telemetry_collector/orrery/projection` is a copy of Microcosm's Orrery
projection. `orrery_json_from_schema(schema, execution=overlay)` turns a
build's saved compiler schema (`graph.schema.json`) and execution overlay
(`graph.execution.json`) into the Orrery JSON that Microcosm's projection
writes from them, byte for byte. It needs only the standard library. Nothing
in the service calls it yet; the collector's Orrery worker will.

`provenance.py` records the Microcosm commit the modules were copied from,
each source file's git blob ID, and what the one changed module,
`orrery.py`, leaves out. The adapter identifier written at
`metadata.adapter`, `microcosm.graph.orrery.v1`, is Microcosm's and is kept
unchanged. `tests/test_orrery_projection.py` compares the copy's output with
Orrery JSON saved from Microcosm's projection at that commit.

Until Microcosm removes its own copy, its projection is the source: the
modules named in `provenance.py`, in
`packages/microcosm-graph/src/microcosm/graph/` on PolicyEngine/microcosm's
`main`. To take a later version:

1. Copy those modules from a Microcosm commit, remove from `orrery.py` what
   `provenance.py` lists, and record the commit and the new blob IDs.
2. From the root of that Microcosm checkout, with no uncommitted changes and
   using its Python environment, regenerate the saved inputs and outputs:

   ```bash
   .venv/bin/python ../microcosm-collector/tools/generate-orrery-projection-fixtures.py \
     ../microcosm-collector/tests/fixtures/orrery_projection
   ```

3. Run `uv run pytest tests/test_orrery_projection.py
   tests/test_orrery_projection_provenance.py`.

Once Microcosm hands its Orrery inputs to the collector and removes its copy,
this copy is the only one, and changes to the projection are made here.

## Hugging Face read access

Only the collector's worker jobs read Hugging Face, for backfills, old
releases and back-building. The service itself holds no Hugging Face
credential: a build's own token reaches it only at the token exchange, and is
never stored. The worker jobs read with one purpose-specific, fine-grained
read token, never a generic `HF_TOKEN`:

| Setting | Read by | Provider |
|---|---|---|
| `COLLECTOR_HF_READ_TOKEN` | worker jobs that read Hugging Face | a Google Secret Manager secret, mounted by Cloud Run into each such job |

`HuggingFaceReader.from_environment()` in
`telemetry_collector/huggingface_read.py` reads it when a job starts. A
missing or malformed token stops the job with a message naming the variable.
`HF_TOKEN`, `HUGGINGFACE_TOKEN` and `HUGGING_FACE_HUB_TOKEN`, which Hugging
Face's own tools read without being asked, are refused if set, so a job
cannot fall back to a token with another scope. No message includes a token,
and the reader sends it only to `huggingface.co`.

The datasets the worker jobs read are listed once, in `HUGGINGFACE_DATASETS`:

| Dataset | Holds | In the token's scope |
|---|---|---|
| `policyengine/populace-us` | US releases | no: it is public |
| `policyengine/populace-uk-private` | UK releases | yes |
| `policyengine/populace-be-private` | Belgian releases | yes |
| `policyengine/populace-us-staging` | US staging builds and run files | yes |
| `policyengine/populace-uk-staging` | UK staging builds and run files | yes |

Setting up the token is a manual step, repeated whenever a dataset is added
here:

1. **Token:** a fine-grained Hugging Face token whose only permission is
   "Read access to contents of selected repos", for every dataset in the
   token's scope above. It has no write, organization, inference or webhook
   permission.
2. **Secret:** the token is the latest version of one Secret Manager secret in
   the collector's Google Cloud project. It is never copied into GitHub.
3. **Access:** only the collector's worker service account may read the
   secret's value, through `roles/secretmanager.secretAccessor` on that secret
   alone.
4. **Mount:** each worker job that reads Hugging Face mounts the secret as
   `COLLECTOR_HF_READ_TOKEN`, for example with
   `gcloud run jobs update JOB --update-secrets COLLECTOR_HF_READ_TOKEN=SECRET:latest`.
   A command that sets a job's secrets with `--set-secrets` replaces all of
   its mounts, so any such command for these jobs must list this one too.

## Daily Hugging Face check

`python -m telemetry_collector.huggingface_check` runs once a day in
production as a Cloud Run job, started by Cloud Scheduler. It reads Hugging
Face with the collector's read token and runs two checks:

1. **Token check.** It requests every dataset in `HUGGINGFACE_DATASETS` once.
   Any answer but HTTP 200 is a problem, including a redirect or no answer.
2. **`releases.json` check.** For each dataset whose entry sets
   `checks_releases_record`, at that dataset's head commit:
   - there is exactly one `releases.json`, at the root, and no `latest*.json`
     file anywhere;
   - the record passes the rules in `microcosm_emitter_interface.releases`,
     the code the emitter's release writer runs before every write: `current`
     names a listed build that is not withdrawn, `default` names a listed
     release, and every listed build has its folder and a
     `release_manifest.json` naming the same release and build;
   - every file in a listed build's folder is in that build's manifest and
     has the SHA-256 the manifest records. Files stored with LFS are compared
     by the SHA-256 Hugging Face records for their content, without a
     download; other files are downloaded and hashed.

   The rules that compare one write with the state before it (existing build
   entries change only `withdrawn`; nothing inside an existing build folder
   is added, changed or removed) need that earlier state, so only the release
   writer applies them. The file check above finds any later change to a
   build's files except a change to its manifest.

**Problems and alerts.** Each problem is one JSON log entry with severity
`ERROR`, naming the check, the dataset and a code such as `HTTP_404`,
`UNREACHABLE`, `POINTER_FILE_PRESENT` or `SHA256_MISMATCH`, followed by a
summary entry. The job then exits 1, or 2 when the token setting itself is
missing or unusable, so the execution fails. Two Cloud Monitoring alert
policies email a person through a notification channel: one when an execution
of the job fails, and one when no execution has succeeded for more than a
day, so a stopped schedule is noticed too. No log entry includes the token.

**Deployment.** The production deploy runs
`scripts/deploy.sh configure-huggingface-check-job` before it changes the
service. It confirms the read token's secret exists, reading only its
metadata, then deploys the job with the released image, one task, no retries
and the token as its only secret. A missing secret or account therefore stops
the deploy before production changes. The job's names come from three
production environment variables:

| Variable | Value |
|---|---|
| `TELEMETRY_HF_CHECK_JOB` | the check job's name |
| `TELEMETRY_WORKER_SERVICE_ACCOUNT` | the worker service account, the only identity that may read the token |
| `TELEMETRY_HF_READ_SECRET_NAME` | the read token's Secret Manager secret |

Staging has no check job: there is one token and one schedule. The deployment
service account needs metadata read access to the secret and permission to
act as the worker service account. The schedule, the account it starts the
job with, and the alert policies are created once by hand; the deploy does not
change them.

**Switching on the `releases.json` check.** It is off for every dataset. For
a dataset, it can be switched on once:

1. the microcosm-emitter-interface package, with
   `microcosm_emitter_interface.releases` (PolicyEngine/microcosm-emitter#24),
   is published and added to this collector's dependencies;
2. Microcosm's new release manifest is defined, and `manifest_file_digests`
   reads its file list as defined there. Until then it reads a `files` list
   of entries with `name` and `sha256`, the fields of the interface package's
   `ReleaseFile`;
3. the dataset publishes builds side by side with `releases.json`, and its
   `latest*.json` files are deleted. Before that, the check would report them
   every day.

Then one change sets `checks_releases_record=True` on that dataset's entry.
`tests/test_releases_record_check.py` tests the check against the real rules,
and is skipped until the package is installed. To run it now against a
checkout of PolicyEngine/microcosm-emitter:

```bash
uv run --with ../microcosm-emitter/packages/microcosm-emitter-interface \
  python -m pytest tests/test_releases_record_check.py
```

## Releases

Every pull request needs a changelog fragment, `changelog.d/<name>.<type>.md`,
where `<type>` is `breaking`, `added`, `changed`, `fixed` or `removed`. A
breaking fragment bumps the major version, an added or removed fragment the
minor version, and any other fragment the patch version.

Each merge to `main` runs `.github/workflows/push.yaml` twice. The first run
bumps `pyproject.toml`, collects the fragments into `CHANGELOG.md`, updates
`uv.lock`, and commits the result as "Update microcosm-collector" with the
PolicyEngine GitHub App, which may bypass the main ruleset. The second run, on
that commit, verifies it, tags it with the bare version (for example `0.2.0`),
and then runs the deployment described below, if it is enabled.

## Cloud Run deployment

This service was developed in `telemetry-service/` inside
PolicyEngine/calibration-diagnostics, and its history was extracted here.
calibration-diagnostics deploys production until this repo takes over (see
"Enabling deployment" below). The Docker build context is the root of this repo.
Cloud Run assigns the production service address. Microcosm fixes that origin
in its delivery service; the address is intentionally not repeated in
documentation.

Infrastructure was provisioned once with disposable scripts kept outside the
source tree. The source tree intentionally contains no bootstrap script or
mutable infrastructure template.

`.github/workflows/pr.yaml` runs actionlint and `.github/workflows/ci.yml`
(tests, migration checks, Ruff and a container build) on every pull request,
and reports one "Required checks" status that the main ruleset requires.
`.github/workflows/push.yaml` runs the same checks on `main`. When deployment
is enabled, it then authenticates to Google Cloud with GitHub OIDC and pushes
one image identified by the commit SHA. It first applies the migration to the
persistent staging PostgreSQL database, verifies both the Alembic revision and
the complete SQLAlchemy metadata, and deploys the staging collector. Production receives that exact image only when the staging sequence
succeeds. Before either environment changes, deployment checks only that its
runtime database, migration database, JWT signing, and read-token secret
resources exist. It does not access their payloads or test their values. The
previous HF authentication and write/read deployment qualification is removed.

Each deployment first routes traffic to a verified maintenance revision that
returns HTTP 503 for data operations, then waits for prior requests to finish.
Production also creates a completed Cloud SQL backup before its migration. The
workflow runs the image as a one-task Cloud Run job to apply and verify Alembic
migrations, deploys a candidate without stable traffic, checks the candidate's
`/ready` endpoint, and only then directs traffic to the candidate. Once a
production migration begins, failures leave the maintenance revision serving
retryable responses; the workflow does not restore schema-incompatible
application code. To repeat a deployment, re-run the failed jobs of that push.

The workflow reads resource identifiers from the GitHub `staging` and
`production` environments, which accept only `main`. Runtime secrets remain in
Google Secret Manager. The migration job uses a separate service account and
database credential with schema-modification access; the collector runtime
credential has only the data access required by the application. The dashboard receives its collector
address and Secret Manager resource references through non-secret configuration.
Public ingress lets developer machines reach the service;
ingestion still requires a short-lived collector credential issued after
Hugging Face authentication.

### Google Cloud access

The deployment jobs exchange their GitHub OIDC token through a Workload
Identity provider dedicated to this repo, then act as the same deployment
service account that calibration-diagnostics uses. The provider maps
`google.subject=assertion.sub`, `attribute.actor=assertion.actor`,
`attribute.ref=assertion.ref` and `attribute.repository=assertion.repository`,
and accepts a token only when:

```
assertion.repository=='PolicyEngine/microcosm-collector' && assertion.ref=='refs/heads/main'
```

The service account grants `roles/iam.workloadIdentityUser` to this repo's
principal set (`attribute.repository/PolicyEngine/microcosm-collector`). A job
running for a tag, a pull request or any other branch cannot obtain Google
credentials. No other job requests an OIDC token.

Both deployment jobs also require
`github.event_name == 'push' && github.ref == 'refs/heads/main'`. The Push
workflow runs only for pushes to `main`, so this condition is always true
today. It guards only against a later change to the workflow's triggers, such
as adding tags or manual dispatch: the deployment jobs would skip those runs
instead of reaching authentication.

### Enabling deployment

Both deployment jobs run only when the repo variable `COLLECTOR_DEPLOY_ENABLED`
is `true`. It is unset, so releases from `main` are tagged but not deployed.

The deployment's access and configuration are already provisioned:

- The `microcosm-collector` Workload Identity provider and the deployment
  service account's binding for this repo exist, as described above.
- The `staging` and `production` environments each hold the secrets
  `TELEMETRY_GCP_WIF_PROVIDER` (this repo's provider) and
  `TELEMETRY_GCP_DEPLOY_SERVICE_ACCOUNT`, and the 15 `TELEMETRY_*` variables
  that `.github/workflows/push.yaml` reads, copied by name and value from the
  calibration-diagnostics `staging` and `Production` environments.

What remains is the cutover from calibration-diagnostics, in this order:

1. Carry over every `telemetry-service/` change merged in calibration-diagnostics
   (see below).
2. Stop calibration-diagnostics from deploying this service through its
   `telemetry-service.yml` workflow.
3. Set `COLLECTOR_DEPLOY_ENABLED` to `true`. The next release from `main` then
   deploys to staging and, if that succeeds, to production.

### Carrying over changes from calibration-diagnostics

Until cutover, changes to `telemetry-service/` can still merge in
calibration-diagnostics. `tools/extraction-source.env` records the
calibration-diagnostics commit that this history was extracted from, and the
commit it was rewritten to here. To carry later changes over, from the root of
a clean, up-to-date checkout of `main`:

```bash
git switch -c carry-over-telemetry-service
tools/extract-from-calibration-diagnostics.sh ../collector-extraction \
  tools/extraction-source.env
git fetch ../collector-extraction main
git merge FETCH_HEAD
```

The script needs git and uv. git-filter-repo rewrites unchanged source
commits to the same commits, so the merge adds only the newer ones. Resolve
any conflicts with changes made here, such as paths in tests, then add a
changelog fragment and open a pull request that includes the updated record.

Merge a carry-over pull request with a merge commit ("Create a merge commit"),
never with "Squash and merge" or "Rebase and merge". Squash and rebase merges
put the carried-over changes on `main` under new commits, so `main` no longer
contains the commits the record names, and the next carry-over would add them
a second time, conflicting with the copies of their changes already there.

The script refuses to continue, without writing the record, in either case
where merging its output would duplicate this history rather than extend it:

- The recorded extracted commit is not in the history of `HEAD` in the
  checkout that runs the script. Either an earlier carry-over pull request was
  squash-merged or rebase-merged, or the script ran from a checkout other than
  the collector's `main` or a branch created from it.
- The recorded extracted commit is not part of the new extraction, because
  the source history or the filter changed.

To recover from a squash-merged or rebase-merged carry-over pull request
(number `<number>` below), merge its original head into the next carry-over
branch with the `ours` strategy, which records those commits as merged without
changing any file, then run the remaining steps above on that branch:

```bash
git switch -c carry-over-telemetry-service
git fetch origin "pull/<number>/head"
git merge -s ours FETCH_HEAD
```

## Dashboard runtime access

The Vercel project configures these server-only, non-secret settings separately
for production and preview:

- `MICROCOSM_TELEMETRY_COLLECTOR_URL`: the matching environment's collector address.
- `MICROCOSM_TELEMETRY_READ_SECRET_VERSION`: the full Secret Manager version
  resource for that collector's read credential.
- `MICROCOSM_TELEMETRY_GOOGLE_IDENTITY_PROVIDER`: the full Google workload
  identity provider resource that trusts this Vercel project.

The provider must trust the project's Vercel OIDC issuer and audience. Bind the
production identity to only the production read secret, and the preview identity
to only the staging read secret. Grant secret-access permission on those
individual resources. These identity bindings and Vercel settings require
one-time provisioning before this change is deployed; their provisioning scripts
stay outside the source tree. The GitHub deployment identity needs metadata read
permission on the required secrets for the existence checks.

Local dashboard development uses Google Application Default Credentials and
the same collector address and secret-version settings. No copied collector
token or service-account key is required. The runtime caches a retrieved value
for up to one minute and retries failed retrievals on subsequent requests.
Configuration checks during deployment only confirm secret existence; they do
not establish that credentials work or that runtime permissions are correct.

Restrict the Cloud SQL instance to the Cloud Run connection and disable request
access logs for the container so authorization headers cannot enter application
logs. Cloud Run and the HTTPS load balancer may retain request metadata, but the
service never includes credentials in paths, query strings, bodies, or errors.

Application persistence uses SQLAlchemy ORM sessions and SQLAlchemy-managed
connection pooling. Psycopg is installed only as SQLAlchemy's PostgreSQL DBAPI
driver; application modules do not call it directly.

Alembic owns the complete database schema. The service never creates or alters
tables during application startup, and production deployment never requires a
person to run database commands. Add a reviewed Alembic revision for every
schema change. The deployment job recognizes an empty database, an
Alembic-managed database, or the exact schema installed before Alembic was
introduced; it refuses to modify any other schema. Production deployment
upgrades an Alembic-managed database. For the one-time cutover in this change,
it recognizes and removes the unversioned prototype tables through SQLAlchemy
metadata before Alembic creates the initial ORM schema. This intentionally
discards prototype telemetry data. Downgrades are reserved for disposable test
databases.

`/health` reports that the HTTP process is running. `/ready` additionally
requires a database connection and the current Alembic revision. The migration
job performs the stronger release-time comparison between the live schema and
the complete SQLAlchemy metadata.
