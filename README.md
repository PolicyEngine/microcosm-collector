# Microcosm telemetry collector

This service accepts live build telemetry from Microcosm and exposes a
read-only API to the calibration dashboard. It never stores Hugging Face
credentials, and it stores build artifacts only when artifact intake is
switched on (see "Artifact intake").

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

## Artifact intake

Builds hand calibration diagnostics and Orrery inputs to the collector as
submissions. [`docs/artifact-submissions.md`](docs/artifact-submissions.md) is
the contract: kinds, file roles, provenance, identifiers, statuses, receipts and
limits. File bytes go straight to a private bucket through signed upload URLs;
`POST /v1/submissions/{id}/complete` checks the stored objects' sizes and
recorded digests and enqueues a worker job through Cloud Tasks; the worker, a
Cloud Run Job running `uv run --no-sync python -m telemetry_collector.worker`,
recomputes every file's SHA-256 and runs the handler registered for the kind.
Until the calibration-tree and Orrery workers register theirs, both kinds are
only checked and kept.

Intake is off unless `COLLECTOR_INTAKE_ENABLED` is exactly `1`. While it is
off, the collector reads no other intake setting, the submission endpoints do
not exist, and the token exchange answers 404 to `{"scope": "artifacts:submit"}`.
`scripts/deploy.sh` sets none of these variables, so deploying this code
changes nothing.

| Setting | Read by | Value |
|---|---|---|
| `COLLECTOR_INTAKE_ENABLED` | service | `1` switches intake on |
| `COLLECTOR_INTAKE_BUCKET` | service, worker | the private intake bucket's name |
| `COLLECTOR_INTAKE_SIGNING_SERVICE_ACCOUNT` | service | the runtime service account, which signs upload URLs through IAM `signBlob` |
| `COLLECTOR_WORKER_QUEUE` | service | `projects/<project>/locations/<region>/queues/<queue>` |
| `COLLECTOR_WORKER_JOB` | service | `projects/<project>/locations/<region>/jobs/<job>` |
| `COLLECTOR_WORKER_INVOKER_SERVICE_ACCOUNT` | service | the account whose OAuth token each Cloud Task uses to run the worker job |
| `COLLECTOR_WORKER_JOB_ID` | worker | set on each execution by its Cloud Task |
| `DATABASE_URL` | worker | the runtime database secret, as for the service |

None of these values is secret. Switching intake on for an environment needs,
in that environment's project and region:

- **A bucket** with uniform bucket-level access, public access prevention
  enforced, and no lifecycle deletion, so intake files stay available for
  back-building.
- **A Cloud Tasks queue** for worker jobs.
- **A worker Cloud Run Job** running the released image with the command above,
  one task, two task retries (three attempts, matching the worker's limit) and
  a one-hour task timeout, with the runtime database secret as `DATABASE_URL`,
  the Cloud SQL connection and `COLLECTOR_INTAKE_BUCKET`.
- **Permissions:** the runtime service account may create and read objects in
  the bucket, sign as itself (`roles/iam.serviceAccountTokenCreator` on
  itself), enqueue on the queue, and act as the invoker account; the invoker
  account may run the worker job with overrides; the worker's service account
  may read the bucket, connect to Cloud SQL and read the runtime database
  secret.
- **Deployment wiring:** `scripts/deploy.sh` passing the settings to the
  service and updating the worker job's image on every release, from new
  `staging` and `production` environment variables. That change is the one
  that switches intake on.

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
uv run ruff check .
uv run ruff format --check .
```

The PostgreSQL integration tests and the migration checks need a disposable
database whose name contains `test`, given as `TEST_DATABASE_URL`. Without it
they are skipped. CI runs them against PostgreSQL 16 and also runs
`python -m telemetry_collector.migrate` and `alembic check` against that
database.

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
and then runs the gated deployment described below.

## Cloud Run deployment

This service was developed in `telemetry-service/` inside
PolicyEngine/calibration-diagnostics, and its history was extracted here.
calibration-diagnostics deploys production until this repo takes over (see
"Deployment gate" below). The Docker build context is the root of this repo.
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
credentials. Both deployment jobs therefore also require
`github.event_name == 'push' && github.ref == 'refs/heads/main'`, so that such a
run skips them instead of failing at authentication. No other job requests an
OIDC token.

### Deployment gate

Both deployment jobs run only when the repo variable `COLLECTOR_DEPLOY_ENABLED`
is `true`. Leave it unset until the cutover from calibration-diagnostics is
complete:

1. Google Cloud trusts this repo's GitHub OIDC identity, as described above.
2. The `staging` and `production` environments hold the same secrets and
   variables, by name, as the calibration-diagnostics `staging` and
   `Production` environments: the secrets `TELEMETRY_GCP_WIF_PROVIDER` (this
   repo's provider) and `TELEMETRY_GCP_DEPLOY_SERVICE_ACCOUNT`, and every
   `TELEMETRY_*` variable read by `.github/workflows/push.yaml`.
3. calibration-diagnostics no longer deploys this service.

### Carrying over changes from calibration-diagnostics

Until cutover, changes to `telemetry-service/` can still merge in
calibration-diagnostics. `tools/extraction-source.env` records the
calibration-diagnostics commit that this history was extracted from. To carry
later changes over, from the root of a clean checkout of `main`:

```bash
tools/extract-from-calibration-diagnostics.sh ../collector-extraction \
  tools/extraction-source.env
git switch -c carry-over-telemetry-service
git fetch ../collector-extraction main
git merge FETCH_HEAD
```

The script needs git and uv. git-filter-repo rewrites unchanged source
commits to the same commits, so the merge adds only the newer ones. The
script refuses to continue if the previously extracted commit is not part of
the new history, because merging that history would duplicate this one rather
than extend it. Resolve any conflicts with changes made here, such as paths in
tests, then add a changelog fragment and open a pull request that includes the
updated record.

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
