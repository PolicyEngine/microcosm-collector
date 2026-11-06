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
the code `intake_not_enabled`. `scripts/deploy.sh` switches intake on in both
environments: it sets `COLLECTOR_INTAKE_ENABLED=1` and the other intake
settings on both service revisions, and updates both intake jobs (see "Worker
jobs and recovery"). Switching an environment's intake off past its next
release therefore takes a change to `scripts/deploy.sh`.

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
exception to the rule below that downgrades are for disposable test databases.
Both tables stay empty only while an environment's intake has never been
switched on. In an environment whose intake is or was on, the downgrade
deletes the record of every submission and job; the files stay in the
bucket.

| Setting | Read by | Value | The deploy sets it from |
|---|---|---|---|
| `COLLECTOR_INTAKE_ENABLED` | service | `1` switches intake on | always `1` |
| `COLLECTOR_INTAKE_BUCKET` | service, worker | the private intake bucket's name | `TELEMETRY_INTAKE_BUCKET` |
| `COLLECTOR_INTAKE_SIGNING_SERVICE_ACCOUNT` | service | the runtime service account, which signs upload URLs through IAM `signBlob` | `TELEMETRY_RUNTIME_SERVICE_ACCOUNT` |
| `COLLECTOR_WORKER_QUEUE` | service, operations | `projects/<project>/locations/<region>/queues/<queue>` | `TELEMETRY_WORKER_QUEUE`, the queue's name, in the deployment's project and region |
| `COLLECTOR_WORKER_JOB` | service, operations | `projects/<project>/locations/<region>/jobs/<job>` | `TELEMETRY_WORKER_JOB`, the worker job's name, likewise |
| `COLLECTOR_WORKER_INVOKER_SERVICE_ACCOUNT` | service, operations | the account whose OAuth token each Cloud Task uses to run the worker job | `TELEMETRY_WORKER_INVOKER_SERVICE_ACCOUNT` |
| `COLLECTOR_WORKER_JOB_ID` | worker | set on each execution by its Cloud Task | not set by the deploy |
| `DATABASE_URL` | worker, operations | the runtime database secret, as for the service | `TELEMETRY_DATABASE_SECRET_NAME` |

The `TELEMETRY_*` names are GitHub `staging` and `production` environment
variables, which `.github/workflows/push.yaml` passes to `scripts/deploy.sh`.
The deploy also reads these, to update the two jobs:

| Variable | Value |
|---|---|
| `TELEMETRY_RECOVERY_JOB` | the recovery job's name |
| `TELEMETRY_WORKER_SERVICE_ACCOUNT` | the worker job's service account |
| `TELEMETRY_RECOVERY_SERVICE_ACCOUNT` | the recovery job's service account |
| `TELEMETRY_HF_READ_SECRET_NAME` | the environment's own Hugging Face read token's secret, mounted on the worker job (see "Hugging Face read access") |

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

None of these values is secret. Intake needs, in each environment's project
and region, these resources, created by hand (both environments have them):

- **A bucket** with uniform bucket-level access, public access prevention
  enforced, and no lifecycle deletion, so intake files stay available for
  back-building.
- **A Cloud Tasks queue** for worker jobs.
- **The worker Cloud Run Job** described above and **the recovery Cloud Run
  Job**, and a Cloud Scheduler job that runs the recovery job every 10
  minutes. The deploy updates both jobs but never creates one, so each must
  exist, with the permissions below, before the release that first deploys
  to its environment.
- **Database access:** after the migration, the runtime database user has
  `SELECT`, `INSERT`, `UPDATE` and `DELETE` on `artifact_submissions` and
  `artifact_jobs`, through the migration user's default privileges, which
  both environments already have (see "Database access").
- **Permissions:** the runtime service account may create and read objects in
  the bucket, sign as itself (`roles/iam.serviceAccountTokenCreator` on
  itself), enqueue on the queue, and act as the invoker account; the invoker
  account may run the worker job with overrides; the worker's service account
  may read the bucket, connect to Cloud SQL and read the runtime database
  secret and its environment's Hugging Face read token's secret; the recovery
  job's service account may connect to Cloud SQL, read the runtime database
  secret, enqueue on the queue and act as the invoker account; Cloud
  Scheduler's account may run the recovery job; and the deployment service
  account may update both jobs and act as the worker and recovery accounts.

Each release, in each environment, `scripts/deploy.sh`:

1. checks, before it changes anything:
   - that every intake name has the shape the collector accepts
     (`require-configuration`, before it authenticates), so a malformed name
     stops the deploy before a revision or job that reads it fails to start;
   - that each job exists, already runs as the service account the deploy
     gives it, and connects to the environment's own Cloud SQL instance
     (`check-intake-jobs`), so a variable naming the other environment's job
     stops the deploy. To move a job to another account, update the job by
     hand first;
   - that the environment's Hugging Face read token's secret exists
     (`check-secrets-exist`);
2. passes the intake settings in the first table to the maintenance and
   candidate revisions;
3. after the migration and before the candidate revision, updates the worker
   job (`configure-worker-job`) and then the recovery job
   (`configure-recovery-job`) to the released image, with their whole
   configuration: service account, Cloud SQL connection, secrets, settings,
   command, task count, retries, task timeout, CPU and memory. Each update
   sets the job's secrets with `--set-secrets`, which replaces every mount,
   so the worker job's lists `DATABASE_URL` and `COLLECTOR_HF_READ_TOKEN`
   together. The deploy changes neither the
   schedule nor any job's permissions.

Maintenance mode holds only the service. An execution of either job keeps the
image it started with, for all its attempts: a worker execution that is
running during a migration (up to three attempts of an hour each), or one that
a scheduled recovery run or a Cloud Tasks retry starts before the job is
updated, runs the previous release's code against the migrated schema. A
migration that changes the intake tables must therefore keep working with the
previous release's worker and recovery code.

Each dispatch is a `jobs.run` call that returns once the execution is created,
so the queue's limit on concurrent dispatches does not limit how many worker
executions run at once.

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

## Calibration tree package

`packages/calibration-tree` is the calibration tree package: the TypeScript
tree builder, its schema-6 types and readers, the release and staging readers
it builds from, and the GitHub Actions publisher that calibration-diagnostics
runs today. It was copied from calibration-diagnostics'
`frontend/packages/calibration-tree` and is published to npm as
`@policyengine/calibration-tree`, so the dashboard can import it from npm
instead of keeping its own copy. Until the dashboard does, calibration-diagnostics
keeps its copy, and its GitHub Actions workflow still publishes the production
trees with it.

The package ships TypeScript source; Bun runs it directly and Next.js
compiles it through `transpilePackages`. Its own
[README](packages/calibration-tree/README.md) describes how to import it.

### The tree worker

`src/calibration-tree-worker.ts` builds one release's tree from local files
and writes its gzip parts to a directory, as its own Bun process:

```bash
cd packages/calibration-tree
bun run src/calibration-tree-worker.ts REQUEST.json OUTPUT_DIRECTORY
```

It builds exactly what the publisher builds from the same files, commit and
publication time. Nothing in the collector runs it yet; the calibration-tree
worker (K4) will run it for each `calibration-diagnostics` submission, so the
worker image will need Bun then.

### Tests

The "Calibration tree package" job in `.github/workflows/pr.yaml` runs the
package's Bun tests and its typecheck on every pull request, with Bun 1.4.2.
The golden tests rebuild recorded US, UK and Belgian releases, staging runs
and comparisons, and require every part's raw JSON and deflate stream to match
`src/fixtures/calibration-tree-golden.json`, which calibration-diagnostics'
builder recorded. The worker's tests run it as a separate process on the
recorded releases and require the same bytes on disk.

```bash
cd packages/calibration-tree
bun install --frozen-lockfile
bun test src/calibration-tree-worker.test.ts
bun run typecheck
```

### Carrying over changes from calibration-diagnostics

`packages/calibration-tree/provenance.json` records the calibration-diagnostics
commit the package was copied from, each copied file's git blob ID, the copied
files changed here and why (only `package.json`), and the files that exist only
here. `src/provenance.test.ts` fails when a copied file no longer has its
recorded blob ID, when a file is neither copied nor listed as the collector's
own, or when a file listed as changed is identical to the dashboard's.

While calibration-diagnostics keeps its copy, a change merged there is carried
over by copying the changed files from a calibration-diagnostics commit,
keeping this repo's changes to `package.json`, and recording that commit and
the new blob IDs, which git lists with:

```bash
git -C ../calibration-diagnostics ls-tree -r \
  --format='%(objectname) %(path)' COMMIT -- frontend/packages/calibration-tree
```

Once the dashboard imports the published package and removes its copy, this
copy is the only one, and the package is changed here.

### Publishing to npm

`.github/workflows/publish-calibration-tree.yaml` publishes the package when a
push to `main` changes `packages/calibration-tree` and npm does not have its
`package.json` version yet. To release a change, raise `version` in the same
pull request; a change without a new version publishes nothing. The
workflow's first job tests and typechecks the package. Its second job, the
only one that can request a GitHub OIDC token, installs nothing: it checks
that Node and npm are the pinned versions (`tools/check-npm-toolchain.sh`),
then runs `tools/publish-calibration-tree.sh`, which asks npm for the version
and publishes only if npm does not have it. It publishes under npm's default
`latest` tag, which npm refuses for a prerelease version or one lower than the
latest, so a release raises the version to a new highest one. One run goes at
a time, so versions merged one after another publish in that order. The
workflow can be run again by hand from `main` (`workflow_dispatch`) to retry a
failed publication.

The workflow uses npm's trusted publishing: npm accepts the publish job's
GitHub OIDC token in place of a stored npm token, and adds provenance because
this repo is public. npm does not check the branch, so the `npm` environment's
rule is what keeps other branches out. Nothing is published until the repo
variable `CALIBRATION_TREE_NPM_PUBLISH_ENABLED` is `true`, which needs this
one-time setup first, in this order:

1. **The first version, by hand.** npm can only trust a workflow for a package
   that exists, so a member of the `policyengine` npm organization publishes
   the first version from a clean checkout of the `main` commit that added the
   package: `bun install --frozen-lockfile`, then `npm pack --dry-run` to check
   the file list, then `npm publish --access public` in
   `packages/calibration-tree`.
2. **The environment.** In this repo's settings, create the `npm` environment
   and allow deployments from `main` only, so a run from any other branch
   cannot obtain npm's trust. Create it before the trusted publisher: GitHub
   creates a missing environment, without that rule, when a job first names
   it.
3. **The trusted publisher.** In the package's settings on npmjs.com, add a
   GitHub Actions trusted publisher: organization `PolicyEngine`, repo
   `microcosm-collector`, workflow `publish-calibration-tree.yaml`,
   environment `npm`. A new trusted publisher expires unless a publication
   through it succeeds within two days, so add it when a pull request that
   raises the version is ready to merge, and merge that pull request after
   step 4. An expired one is deleted and added again.
4. **The switch.** Set the repo variable
   `CALIBRATION_TREE_NPM_PUBLISH_ENABLED` to `true`.

## Hugging Face read access

Only the collector's worker jobs read Hugging Face, for backfills, old
releases and back-building. The service itself holds no Hugging Face
credential: a build's own token reaches it only at the token exchange, and is
never stored. The worker jobs read with a purpose-specific, fine-grained
read token, never a generic `HF_TOKEN`. Staging and production each have
their own token, with the same scope, so either can be replaced or revoked
without the other:

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

Setting up each environment's token is a manual step, repeated whenever a
dataset is added here:

1. **Token:** a fine-grained Hugging Face token whose only permission is
   "Read access to contents of selected repos", for every dataset in the
   token's scope above. It has no write, organization, inference or webhook
   permission.
2. **Secret:** the token is the latest version of the environment's own
   Secret Manager secret in the collector's Google Cloud project. It is
   never copied into GitHub.
3. **Access:** only the environment's worker service account may read the
   secret's value, through `roles/secretmanager.secretAccessor` on that
   secret alone; the other environment's worker cannot read it.
4. **Mount:** each environment's worker job mounts its secret as
   `COLLECTOR_HF_READ_TOKEN`. Each release's deploy sets it in both
   environments (`scripts/deploy.sh configure-worker-job`), naming the secret
   from the environment variable `TELEMETRY_HF_READ_SECRET_NAME`, and first
   checks that the secret exists. Any other job that reads Hugging Face
   mounts it too, for example with
   `gcloud run jobs update JOB --update-secrets COLLECTOR_HF_READ_TOKEN=SECRET:latest`.
   A command that sets a job's secrets with `--set-secrets` replaces all of
   its mounts, so any such command for these jobs must list this one too, as
   the deploy's does.

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

Both environments run in the collector's own Google Cloud project, separate
from the project that hosts the Public API. Each environment is reached through
its own hostname: a global external Application Load Balancer with a static IP
address, a Google-managed certificate from Certificate Manager, and a
serverless network endpoint group for that environment's Cloud Run service.
The emitter's default collector origin is the production hostname. The
hostnames are intentionally not repeated in documentation; the dashboard's
`MICROCOSM_TELEMETRY_COLLECTOR_URL` settings hold them.
[`docs/project-and-domain-decision.md`](docs/project-and-domain-decision.md)
records why, and [`docs/project-move-runbook.md`](docs/project-move-runbook.md)
how the services, databases and secrets moved there. Cloud Run's default
addresses stay on: the deployment checks each maintenance and candidate
revision through its tagged default address. The services left in the earlier
project stay in maintenance mode, answering 503, until the runbook's section 13
retires them.

The load balancers and the collector's project were created by hand, following
the runbook. The source tree intentionally contains no bootstrap script or
mutable infrastructure template.

`.github/workflows/pr.yaml` runs actionlint and `.github/workflows/ci.yml`
(tests, migration checks, Ruff and a container build) on every pull request,
and reports one "Required checks" status that the main ruleset requires.
`.github/workflows/push.yaml` runs the same checks on `main`. When deployment
is enabled, it then authenticates to Google Cloud with GitHub OIDC and pushes
one image identified by the commit SHA. It first applies the migration to the
persistent staging PostgreSQL database, verifies both the Alembic revision and
the complete SQLAlchemy metadata, and deploys the staging collector. Production receives that exact image only when the staging sequence
succeeds. Before either environment changes, deployment checks only that the
environment's five secrets exist (the runtime database URL, the migration
database URL, the JWT signing secret, the dashboard's read token and the
Hugging Face read token), and that its worker and recovery jobs exist and
belong to that environment (see "Worker jobs and recovery"). It does not
access the secrets' payloads or test their values. The
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
Identity provider in the collector's project, dedicated to this repo, then act
as a deployment service account in that project that only this repo uses. The
provider maps
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
  for the service and its migration that `.github/workflows/push.yaml`
  reads. They were first copied from the calibration-diagnostics `staging`
  and `Production` environments; the project move pointed each environment's
  project, Cloud SQL connections, service accounts, provider and deployment
  account at the collector's project (the runbook's section 14 lists them).
- For intake, each environment also holds the seven `TELEMETRY_*` variables
  for its bucket, queue, jobs and the service accounts that run them, and
  `TELEMETRY_HF_READ_SECRET_NAME` for its own Hugging Face read token's
  secret (see "Artifact intake").
  `scripts/deploy.sh require-configuration` names any that is missing, and
  the deployment stops before it authenticates.

The move from calibration-diagnostics is complete:

- Its `telemetry-service/` changes up to commit `3dfbe05`
  (PolicyEngine/calibration-diagnostics#219) were carried over, as
  `tools/extraction-source.env` records (see below).
- PolicyEngine/calibration-diagnostics#233 stopped its `telemetry-service.yml`
  workflow from deploying on push. Since the project move, that workflow's
  manual redeploy must never be run: its settings name the earlier project,
  and its code predates migration `0002_artifact_intake`
  (PolicyEngine/calibration-diagnostics#234 removes it).
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
