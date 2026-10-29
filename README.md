# Microcosm telemetry collector

This service accepts live build telemetry from Microcosm and exposes a
read-only API to the calibration dashboard. It never stores Hugging Face
credentials, and it stores build artifacts only when artifact intake is
switched on (see "Artifact intake").

## Authentication

Microcosm sends its ambient Hugging Face token only to
`POST /v1/auth/huggingface/exchange`. The collector validates that opaque token
against Hugging Face's `whoami-v2` endpoint and confirms current membership in
the `policyengine` organization. It then returns a collector JWT that expires
after one hour by default, or after at most 24 hours when issued for a build on
another machine (see "Tokens for builds on other machines").
That credential registers runs and uploads events; the collector verifies that
the authenticated Hugging Face user owns the referenced run and that each
producer was registered first. The raw Hugging Face token is neither logged nor
written to Postgres.

### Tokens for builds on other machines

A collector JWT lasts `TELEMETRY_SESSION_TOKEN_TTL_SECONDS` (3600 seconds unless
set) and can only be replaced by exchanging a Hugging Face token again. A build
on a machine without a Hugging Face credential, such as a Microcosm stage on
Modal, cannot do that. Instead, the operator's workstation exchanges its own
Hugging Face token at launch with a body such as
`{"scope": "telemetry:write", "lifetime_seconds": 64800}`, and passes the
returned JWT to the build.

- `lifetime_seconds` must be a positive JSON integer; `null` is the same as
  leaving the field out. A lifetime is accepted only with scope
  `telemetry:write`, the default. Otherwise the exchange answers 422. With
  `artifacts:submit`, whether intake is on or off, the refusal's
  `detail[0].msg` is exactly
  `Value error, lifetime_seconds is accepted only with scope telemetry:write.`
  (pydantic adds the prefix "Value error, ").
- The collector grants the requested lifetime, at most 86,400 seconds (24
  hours). The maximum is a constant in the code, not a deployment setting.
  `expires_in` reports the lifetime granted, so a client that needs longer can
  refuse to start the build. A request below the default lifetime is granted as
  requested.
- The token carries the claim `"delegated": true`, which tokens exchanged
  without `lifetime_seconds` never carry. It marks a token issued for use on
  another machine. Every endpoint that accepts `telemetry:write` accepts it; a
  future endpoint that must not accept such tokens refuses tokens with this
  claim.
- Until it expires, whoever holds the token can register runs and upload events
  as the operator, and nothing else. Changing the signing secret is the only way
  to revoke it early.

Without `lifetime_seconds`, the exchange is unchanged: the same lifetime, claims
and response.

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
the contract: kinds, file roles, provenance, identifiers, statuses, answers,
receipts and limits. File bytes go straight to a private bucket through signed
upload URLs; `POST /v1/submissions/{id}/complete` checks the stored objects'
sizes and recorded digests and dispatches a worker job through Cloud Tasks; the
worker, a Cloud Run Job running
`uv run --no-sync python -m telemetry_collector.worker`, recomputes every
file's SHA-256 and runs the handler registered for the kind. Until the
calibration-tree and Orrery workers register theirs, both kinds are only
checked and kept.

Intake is off unless `COLLECTOR_INTAKE_ENABLED` is exactly `1`. While it is
off, the collector reads no other intake setting, and the token exchange for
`{"scope": "artifacts:submit"}` and every `/v1/submissions` path answer 404 with
the code `intake_not_enabled`. `scripts/deploy.sh` sets none of these
variables, so a deployment leaves intake off. It still changes three things:
the migration job applies `0002_artifact_intake`, which adds the two intake
tables; token responses gain `scope`; and the token exchange, which used to
ignore its request body, now answers 422 for unknown fields and 404
`intake_not_enabled` for `{"scope": "artifacts:submit"}`.

Once `0002_artifact_intake` is applied, an image without it cannot run against
the database. That includes every earlier collector release and the old copy in
calibration-diagnostics' `telemetry-service/`. Its migration job fails with
"Can't locate revision identified by '0002_artifact_intake'", and its `/ready`
check fails. To roll back to such an image, first run
`alembic downgrade 0001_initial_schema`, which drops both intake tables and
every submission and job in them. Run it with this release's code, for
example as its migration job with overridden arguments, and as the migration
database user, before deploying the older image: the older code can't find
the revision either, and the runtime user doesn't own the tables. That is an
exception to the rule below that downgrades are for disposable test databases;
while intake is off, both tables stay empty.

| Setting | Read by | Value |
|---|---|---|
| `COLLECTOR_INTAKE_ENABLED` | service | `1` switches intake on |
| `COLLECTOR_INTAKE_BUCKET` | service, worker | the private intake bucket's name |
| `COLLECTOR_INTAKE_SIGNING_SERVICE_ACCOUNT` | service | the runtime service account, which signs upload URLs through IAM `signBlob` |
| `COLLECTOR_WORKER_QUEUE` | service, operations | `projects/<project>/locations/<region>/queues/<queue>` |
| `COLLECTOR_WORKER_JOB` | service, operations | `projects/<project>/locations/<region>/jobs/<job>` |
| `COLLECTOR_WORKER_INVOKER_SERVICE_ACCOUNT` | service, operations | the account whose OAuth token each Cloud Task uses to run the worker job |
| `COLLECTOR_WORKER_JOB_ID` | worker | set on each execution by its Cloud Task |
| `DATABASE_URL` | worker, operations | the runtime database secret, as for the service |

The two service-account settings accept only user-managed accounts,
`<name>@<project>.iam.gserviceaccount.com`. The collector refuses to start
with the default Compute Engine account
(`<number>-compute@developer.gserviceaccount.com`), so the runtime and invoker
accounts must be user-managed ones.

### Database access

The migration job's database user owns the tables it creates; the service,
the worker and the operator commands connect as the runtime database user.
The migrations grant nothing. The runtime user's access to the telemetry
tables was granted by hand when each database was set up, outside this
repo. Its access to every table created since then comes from the
migration user's default privileges, set once by hand in each environment:

```sql
ALTER DEFAULT PRIVILEGES FOR ROLE <migration_role> IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO <runtime_role>;
```

`<migration_role>` is the user in the migration database secret, the secret
named by `TELEMETRY_MIGRATION_DATABASE_SECRET_NAME`, and `<runtime_role>` is
the user in the runtime database secret, named by
`TELEMETRY_DATABASE_SECRET_NAME`. Default privileges apply only to tables that
role creates in `public` after they are set.

- **Where it is set:** staging and production both have these default
  privileges already (checked read-only through each environment's
  migration job on 2026-10-27). Before merging a change that adds tables,
  check with `\ddp` that the entry is still there; there is nothing to run.
- **When to run it again:** when the database users are recreated, as the
  project move in PolicyEngine/microcosm-collector#21 does. An entry belongs
  to the role itself, not its name, so a recreated migration user has one
  only if the import or the statement creates it: check `\ddp` in the new
  database before a migration job runs there, and run the statement if the
  entry is missing.
- **Who can run it:** the migration user itself, or a role that is a member
  of it with the `INHERIT` option. PostgreSQL 16 refuses the statement for a
  member with only `ADMIN` or `SET`. A role that Cloud SQL's `postgres`
  created in SQL is held by `postgres` with only `ADMIN`, so either connect as
  the migration user, or have `postgres` first grant itself the role
  `WITH INHERIT TRUE`.
- **How to check it:** `\ddp` lists an entry owned by `<migration_role>` for
  schema `public` and type `table`, with `<runtime_role>=arwd/<migration_role>`.
  After a migration that creates tables,
  `SELECT has_table_privilege('<runtime_role>', '<table>', '<privilege>')`
  is true for each of them and each of the four privileges.

The intake code needs `SELECT`, `INSERT` and `UPDATE`; row locks
(`SELECT ... FOR UPDATE`) need `UPDATE`. No table uses a sequence. A `serial`
column would need `USAGE` on its sequence, which these default privileges
don't give, so a migration that adds one needs
`GRANT USAGE, SELECT ON SEQUENCES` added to the default privileges first; an
identity column needs no sequence privilege to insert rows.

### Worker jobs and recovery

- **The worker Cloud Run Job** runs one task, with two task retries
  (`--max-retries 2`, so three attempts, matching `MAX_JOB_ATTEMPTS`) and a
  one-hour task timeout (`--task-timeout 1h`, matching
  `WORKER_TASK_TIMEOUT_SECONDS`). Each claim leases the job for 65 minutes
  (`JOB_LEASE_SECONDS`), so a lease expires only after its attempt has ended.
  A task retry is a new attempt under the same lease holder
  (`CLOUD_RUN_EXECUTION`).
- **Recovery** (`uv run --no-sync python -m telemetry_collector.operations
  recover`) runs every 10 minutes, as an execution of a second Cloud Run Job
  on the same image that Cloud Scheduler starts. It fails a job whose lease
  expired after its third attempt (`attempts_exhausted`) or that would need a
  fourth dispatch (`worker_unavailable`), and dispatches again a job whose
  attempt ended early or that stayed queued for 30 minutes without being
  claimed. Each dispatch is a Cloud Task named `<job_id>-<dispatch number>`.
  Without recovery, a job whose last attempt was killed stays `processing`.
- **Reprocessing** (`uv run --no-sync python -m telemetry_collector.operations
  reprocess SUBMISSION_ID...`, or `reprocess --kind <kind> --intake-only`, with
  `--dry-run` to list first) processes final submissions again in a new
  processing round, for example after a handler is registered. An operator
  runs it as an execution of the recovery job with its arguments overridden,
  or locally through the Cloud SQL Auth Proxy with Application Default
  Credentials that may enqueue on the queue and act as the invoker account.

None of these values is secret. Switching intake on for an environment needs,
in that environment's project and region:

- **A bucket** with uniform bucket-level access, public access prevention
  enforced, and no lifecycle deletion, so intake files stay available for
  back-building.
- **A Cloud Tasks queue** for worker jobs.
- **The worker Cloud Run Job** described above, running the released image
  with the worker command, with the runtime database secret as `DATABASE_URL`,
  the Cloud SQL connection and `COLLECTOR_INTAKE_BUCKET`.
- **The recovery Cloud Run Job** on the same image with the recovery command,
  the runtime database secret as `DATABASE_URL`, the Cloud SQL connection and
  the three `COLLECTOR_WORKER_*` queue settings, and a Cloud Scheduler job that
  runs it every 10 minutes.
- **Database access:** after the migration, the runtime database user has
  `SELECT`, `INSERT`, `UPDATE` and `DELETE` on `artifact_submissions` and
  `artifact_jobs`, through the migration user's default privileges, which
  both environments already have (see "Database access").
- **Permissions:** the runtime service account may create and read objects in
  the bucket, sign as itself (`roles/iam.serviceAccountTokenCreator` on
  itself), enqueue on the queue, and act as the invoker account; the invoker
  account may run the worker job with overrides; the worker's service account
  may read the bucket, connect to Cloud SQL and read the runtime database
  secret; the recovery job's service account may connect to Cloud SQL, read the
  runtime database secret, enqueue on the queue and act as the invoker
  account; Cloud Scheduler's account may run the recovery job.
- **Deployment wiring:** `scripts/deploy.sh` passing the settings to the
  service and updating both jobs' images on every release, from new `staging`
  and `production` environment variables. That change is the one that
  switches intake on.

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

`python -m telemetry_collector.huggingface_check` runs in production as a
Cloud Run job, started by Cloud Scheduler at 06:00 and 18:00 UTC
(`0 6,18 * * *`). It reads Hugging Face with the collector's read token and
runs two checks:

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
of the job fails, and one when no execution has succeeded for 23 hours, so a
stopped schedule is noticed too. Cloud Monitoring allows a metric-absence
condition to wait at most 23.5 hours, which is why the check runs twice a day:
with one run a day, the gap between successful runs would exceed 23 hours and
the second alert would fire before every run. With runs 12 hours apart, it
fires 23 hours after the last successful run, so one scheduled run that did
not succeed is enough: after a failed 18:00 run, the failure alert fires
straight away and the absence alert at about 05:00 the next day. No log entry
includes the token.

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

Create the notification channel and both alert policies before the job's
first execution, then create the schedule and run the job once by hand. A
metric-absence condition is met only after the job writes at least one data
point after the policy is created or changed. Policies created after the last
successful run therefore never report a schedule that cannot start the job,
and the same holds after either policy is edited until the next successful
run.

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
This repo has deployed staging and production since release 0.3.2 on
2026-10-21; calibration-diagnostics no longer deploys the service (see
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
ingestion still requires an expiring collector credential issued after
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
is `true`. It has been `true` since 2026-10-20, so every release from `main`
deploys to staging and, if that succeeds, to production. With any other value,
or unset, releases from `main` are tagged but not deployed.

The deployment's access and configuration are provisioned:

- The `microcosm-collector` Workload Identity provider and the deployment
  service account's binding for this repo exist, as described above.
- The `staging` and `production` environments each hold the secrets
  `TELEMETRY_GCP_WIF_PROVIDER` (this repo's provider) and
  `TELEMETRY_GCP_DEPLOY_SERVICE_ACCOUNT`, and the 15 `TELEMETRY_*` variables
  that `.github/workflows/push.yaml` reads, copied by name and value from the
  calibration-diagnostics `staging` and `Production` environments.

The move from calibration-diagnostics is complete:

- Its `telemetry-service/` changes up to commit `3dfbe05`
  (PolicyEngine/calibration-diagnostics#219) were carried over, as
  `tools/extraction-source.env` records (see below).
- PolicyEngine/calibration-diagnostics#233 stopped its `telemetry-service.yml`
  workflow from deploying on push. That workflow keeps a manual redeploy of
  calibration-diagnostics' `main`, for moving deployment back there; whoever
  dispatches it must first unset `COLLECTOR_DEPLOY_ENABLED` here, and confirms
  that in the dispatch form.
- `COLLECTOR_DEPLOY_ENABLED` was set to `true` on 2026-10-20. Release 0.3.2,
  on 2026-10-21, was the first that this repo deployed to staging and
  production.

### Carrying over changes from calibration-diagnostics

While calibration-diagnostics keeps `telemetry-service/`, changes to it can
still merge there, but they deploy nothing until they are carried over here.
`tools/extraction-source.env` records the
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
tables during application startup. Apart from setting the migration user's
default privileges once per environment (see "Database access"), production
deployment never requires a person to run database commands. Add a reviewed
Alembic revision for every
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
