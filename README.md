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
  as the operator, and nothing else. Replacing the key that signed it is the
  only way to revoke it early: the signing key for an ES256 token ("Switching
  from HS256 to ES256"), or `TELEMETRY_JWT_SECRET` for an HS256 token.

Without `lifetime_seconds`, the exchange is unchanged: the same lifetime, claims
and response.

The dashboard query endpoints require `X-Telemetry-Read-Token`. The dashboard
server retrieves this credential from Google Secret Manager at runtime. Vercel
authenticates using its OIDC identity; the credential is never returned to the
browser or passed through GitHub Actions.

### Session tokens

The collector signs its session tokens and is the only service that verifies
them. The emitter treats a token as an opaque bearer credential: it keeps the
token for `expires_in` seconds and exchanges again after a 401.

- **Signing.** Once `TELEMETRY_JWT_SIGNING_KEY` holds an EC P-256 private key
  (unencrypted PEM, PKCS #8 or SEC 1), new tokens are signed with ES256. Until
  then they are signed with HS256 and `TELEMETRY_JWT_SECRET`. Either variable
  set but blank stops startup.
- **Key ID.** An ES256 token's header names its key in `kid`, the RFC 7638
  SHA-256 thumbprint of the public key. The ID is derived from the key rather
  than configured, so it cannot disagree with the key, and a new key always
  has a new ID.
- **Public key.** `GET /.well-known/jwks.json` publishes the public key as a
  JSON Web Key Set, without credentials and with
  `Cache-Control: public, max-age=300`:
  `{"keys": [{"kty": "EC", "crv": "P-256", "x": "...", "y": "...", "kid": "...", "use": "sig", "alg": "ES256"}]}`.
  Before a signing key is configured, `keys` is empty. Like every route but
  `/health`, it answers 503 while the maintenance revision serves.
- **Verification.** The header's `alg` chooses the key, and decoding then
  allows only that algorithm. ES256 needs the configured key's `kid`. HS256
  needs `TELEMETRY_JWT_SECRET` to be configured. Every other token, including
  `none`, an unknown `kid` and an HS256 token keyed with the public key, gets
  401.
- **Claims.** `iss`, `aud`, `sub` (the Hugging Face user ID), `username`,
  `organizations`, `scope`, `iat` and `exp`, and `"delegated": true` on a token
  issued with `lifetime_seconds`. A token lasts
  `TELEMETRY_SESSION_TOKEN_TTL_SECONDS`, one hour by default, or the lifetime
  granted, at most 24 hours, when it is delegated ("Tokens for builds on other
  machines").
- **Scopes.** `scope` is exactly one of `telemetry:write`, which registers runs
  and sends their events, and `artifacts:submit`, which only the artifact
  submission endpoints accept. Each endpoint requires its own scope and answers
  403 to the other, so neither token works for the other's endpoints. An
  exchange without a body issues a `telemetry:write` token.

### Switching from HS256 to ES256

Every deployed revision mounts the signing key, as `TELEMETRY_JWT_SIGNING_KEY`,
from the Secret Manager secret named by the `TELEMETRY_JWT_SIGNING_KEY_SECRET_NAME`
variable of the `staging` and `production` environments. Without that
variable, deployment stops at its first step, its configuration check. It also
checks that the secret exists before changing traffic. Each environment has its
own key in its own secret. As for the other secrets, grant two roles on the
secret itself: `roles/secretmanager.secretAccessor` to the runtime service
account, which reads the key, and `roles/secretmanager.viewer` to the
deployment identity, whose existence check reads the secret's metadata.

The switch window starts with the first deployment that mounts the signing key
and ends with the change that stops mounting `TELEMETRY_JWT_SECRET`. During
the window the collector issues ES256 tokens and accepts both kinds, so HS256
tokens issued before the deployment keep working until they expire. The window
must last at least 24 hours after the last revision that issued HS256 tokens
stopped serving: a delegated token that revision issued to a build on another
machine, such as a Microcosm stage on Modal, stays valid that long
(`MAXIMUM_DELEGATED_LIFETIME_SECONDS`), and that build cannot exchange again
for an ES256 token. Keep the window open longer, until rolling back to such a
revision is no longer wanted.

A revision from before the signing key answers 401 to ES256 tokens. After a
rollback to one, an emitter that holds a Hugging Face token exchanges again,
receives an HS256 token and carries on after one retry. A build holding a
delegated ES256 token cannot exchange again, so the collector refuses its
telemetry until that build ends; builds launched after the rollback get HS256
tokens.

To retire HS256:

1. Stop mounting `TELEMETRY_JWT_SECRET`: remove it from both `--set-secrets`
   lists in `scripts/deploy.sh`, and remove `JWT_SECRET_NAME` from every list
   of variables the script requires, from its secret check and from
   `.github/workflows/push.yaml`. Without the secret, the collector refuses to
   start unless the signing key is set, and it refuses HS256 tokens.
2. Once that release has deployed to both environments, delete the
   `TELEMETRY_JWT_SECRET_NAME` variables and the HS256 secrets. Every revision
   from the switch window mounts `TELEMETRY_JWT_SECRET`, so after this none of
   them can start: delete the secrets only once rolling back to one is no
   longer wanted.

To rotate the signing key, put the new key in a new secret. Don't add a
version to the secret the service already mounts. Cloud Run reads a secret
mounted as an environment variable when each instance starts, so a new
version would reach every instance the serving revision starts from then on,
while its running instances keep the old key. Until the next deployment, a
token signed by one instance would get 401 from another, and
`/.well-known/jwks.json` would answer with whichever key the instance holds.

1. Generate the new key straight into a new secret, and grant it the same two
   roles as the first key.
2. Set `TELEMETRY_JWT_SIGNING_KEY_SECRET_NAME` in that environment to the new
   secret's name. The serving revision keeps reading the old secret.
3. Deploy. Deployments run only from the release commit of a merge to
   `main`, so the rotation takes effect with the next merged change; every
   merge releases and deploys. The new revision signs with, and publishes,
   only the new key.
   Tokens signed with the previous key get 401 and are exchanged again. A
   build holding a delegated token signed with the previous key cannot
   exchange again, so the collector refuses its telemetry for the rest of its
   run. Unless the previous key must stop working at once, rotate when no such
   build has been launched in the previous 24 hours.
4. Delete the old secret once rolling back to a revision that mounts it is no
   longer wanted, since such a revision can't start without it.

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

### Details the emitter writes

microcosm-emitter 0.14.0 and later write five `details` keys themselves, and
the collector checks each against a model in `telemetry_collector.models`:

| Event | Key | Model |
| --- | --- | --- |
| `heartbeat` | `reading`: the build's process tree and the machine, on every heartbeat (every 30 s by default) | `Reading` |
| `heartbeat` | `sleep`: a system sleep, on the heartbeat sent on waking | `SleepRecord` |
| `run` | `environment`: a local machine or a Modal container | `MachineEnvironment` |
| `stage` | `stage_span`: the parent stage, occurrence, awake and asleep seconds, and why the emitter closed it (`closed_by`) | `StageSpan` |
| `progress` | `outer`: the counter one level above the stage's own | `OuterProgress` |

A key that doesn't match its model is removed from the stored event and named
in `details.dropped_emitter_details`, with a warning in the collector's log
naming the event and run. The rest of the event is accepted, since a `422`
would stop the emitter delivering the whole run. The models' numbers are
strict (an integer or a float, never a boolean or numeric text) and their times
are ISO 8601 text. The same keys on other
event types are stored as sent, and only the collector writes
`dropped_emitter_details`.

Both read APIs serve them beside the events, in version 1 as
`run_manifest.environment`, `progress.reading` and a top-level `sleeps`, and
in version 2 as the run's `environment`, `reading` and `sleeps`:

- the environment: the latest run start's machine description;
- the reading: the latest reading of the run's current attempt, with its
  heartbeat's `timestamp`, or `null` until that attempt sends one;
- the sleeps: every attempt's sleeps, in event order, each with its
  `producer_id`.

Each is checked against its model again when it is served, and left out if it
doesn't match, since a collector without these checks stored the keys as sent.
Only the latest reading is checked, then the one before it, so a long run
costs one check.

## Read API

Both versions take the dashboard read credential in `X-Telemetry-Read-Token`.

- **Version 1**, `GET /v1/runs` and `GET /v1/runs/{run_id}`, is what the
  dashboard reads today. A run comes back as the documents the dashboard used
  to read from Hugging Face run files (`progress`, `run_manifest`,
  `calibration_progress` and lifecycle `events`). It changes only by gaining
  keys the dashboard doesn't yet read, such as the emitter's readings below:
  `tests/fixtures/read_api_v1` records its answers, and a test fails if they
  change.
- **Version 2**, `GET /v2/runs` and `GET /v2/runs/{run_id}`, returns typed
  answers. The list takes the same `country`, `limit` and `before` as version 1
  and returns each run's registration and stored state. A run comes back as
  `{"run": ..., "events": [...]}`: every stored event with the fields its
  producer sent (an optional field it left out is null, and `details` an empty
  object), in the order the collector applies them (producers by registration
  time, then producer id, then sequence), and the run's state folded from
  exactly those events, so the two agree while the run is still receiving
  events. All timestamps are in UTC. Bad parameters, including an invalid cursor or run id,
  answer 422 with FastAPI's validation errors; an unknown run answers 404.

`contracts/read-api-v2` holds version 2's contract:

- `openapi.json`, its OpenAPI document, which the collector also serves at
  `GET /v2/openapi.json`;
- `telemetry.json`, telemetry for a few runs (completed, failed, blocked,
  restarted, only registered, and one whose emitter sent readings, a sleep,
  its machine description and stage spans);
- `interactions/`, one file per request, with the collector's answer once that
  telemetry is stored.

`tests/test_read_api_v2.py` checks that the collector serves exactly that
document and those answers, from the in-memory store and from PostgreSQL, and
that each answer matches the document's schema. After an intended change to
either version, run `uv run python tools/update-read-api-contract.py`, review
the changed files, and regenerate the client's types (below).

### The TypeScript client

`packages/collector-client` is `@policyengine/microcosm-collector-client`, a
typed client for version 2. Its types are generated from `openapi.json`
(`npm run generate`), and its tests answer it with the recorded interactions.
CI fails if the generated types are out of date. Each release gives the client
the collector's version (`tools/set-client-version.py`, run by
`tools/update-version.sh`).

Once production runs a release, and if the repo variable
`COLLECTOR_CLIENT_PUBLISH_ENABLED` is `true`, the Push workflow publishes that
version to npm in two jobs:

- `pack-client` installs the client's dependencies and runs `npm pack`, which
  builds it. It has no OIDC token.
- `publish-client` publishes that tarball with `tools/publish-client.sh`, which
  checks that it is this package at the release's version, skips a version
  already on npm, and runs `npm publish --ignore-scripts`. It installs nothing
  and runs no package script, so the only code that can request its OIDC token
  is the npm CLI that ships with Node and GitHub's own actions. It runs in the
  GitHub environment `npm` and uses npm's trusted publishing: npm accepts the
  job's GitHub OIDC token for this workflow file and environment, so no npm
  token is stored anywhere, and npm attaches a provenance attestation.

Both jobs follow `deploy-production`, so they run only while
`COLLECTOR_DEPLOY_ENABLED` is `true`; while it is not, releases are not
published either, without any job failing.

Publishing is not switched on. Switching it on is a manual step, after the
first release that contains the client has reached production:

1. **Google Cloud.** Make the Workload Identity provider accept only the
   deployment environments ("Google Cloud access"), so that `publish-client`'s
   token cannot obtain Google credentials. `TELEMETRY_GCP_WIF_PROVIDER`, a
   secret of the `staging` and `production` environments, holds the provider's
   resource name, which names its project, pool and provider:

   ```bash
   gcloud iam workload-identity-pools providers update-oidc <provider> \
     --project <collector project> --location global --workload-identity-pool <pool> \
     --attribute-condition "assertion.repository=='PolicyEngine/microcosm-collector' && assertion.ref=='refs/heads/main' && assertion.environment in ['staging', 'production']"
   ```

   The next release's deployment jobs then check that deployments still
   authenticate. To undo it, run the same command with the condition without
   its last clause.
2. **First version.** npm can trust a workflow only for a package that already
   exists, so a maintainer of the `@policyengine` npm scope, logged in with
   two-factor authentication, publishes that release's version once from its
   tag: `git switch --detach <version>`, then in `packages/collector-client`,
   `npm ci` and `npm publish --access public`. This version has no provenance
   attestation.
3. **Environment.** Create the GitHub environment `npm` with a deployment
   branch policy that accepts only `main`, as `staging` and `production` do.
4. **Trusted publisher**, on a day when a pull request is ready to merge: a new
   trusted publisher configuration expires if its first publication has not
   succeeded within 2 days. With npm 11.15.0 or later:
   `npm trust github @policyengine/microcosm-collector-client --file push.yaml --repo PolicyEngine/microcosm-collector --env npm --allow-publish`
   (or, on npmjs.com, the package's settings, "Trusted publishing", GitHub
   Actions, with the same organization, repo, workflow file and environment).
5. **Switch.** Set the repo variable `COLLECTOR_CLIENT_PUBLISH_ENABLED` to
   `true`, then merge the pull request. Its release's `publish-client` job
   publishes its version, and `npm view @policyengine/microcosm-collector-client`
   then lists it. If no publication succeeded within the 2 days, remove the
   configuration and repeat step 4:
   `npm trust list @policyengine/microcosm-collector-client` shows its id, and
   `npm trust revoke --id <id> @policyengine/microcosm-collector-client`
   removes it.

To stop publishing, set the variable to anything else; to stop npm trusting the
workflow, revoke the configuration in the same way.

## Artifact intake

Builds hand calibration diagnostics and Orrery inputs to the collector as
submissions. [`docs/artifact-submissions.md`](docs/artifact-submissions.md) is
the contract: kinds, file roles, provenance, identifiers, statuses, answers,
receipts and limits. File bytes go straight to a private bucket through signed
upload URLs; `POST /v1/submissions/{id}/complete` checks the stored objects'
sizes and recorded digests and dispatches a worker job through Cloud Tasks; the
worker, a Cloud Run Job running
`uv run --no-sync python -m telemetry_collector.worker`, recomputes every
file's SHA-256 and runs the handler registered for the kind. Where the
worker job has its calibration tree Blob token, `calibration-diagnostics`
submissions build calibration trees ("Building trees from submissions"), and
where it has its private Orrery bucket, `orrery-inputs` submissions publish
Orrery graph files ("Publishing Orrery graph files"). Otherwise a kind's
submissions are only checked and kept in intake storage.

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
| `DATABASE_URL` | worker, operations, Orrery read service | the runtime database secret, as for the service | `TELEMETRY_DATABASE_SECRET_NAME` |
| `COLLECTOR_CALIBRATION_TREE_BLOB_TOKEN` | worker | the read-write token of the environment's own calibration tree Vercel Blob store, a Secret Manager secret; without it, `calibration-diagnostics` submissions stay in intake storage (see "Building trees from submissions") | `TELEMETRY_CALIBRATION_TREE_BLOB_SECRET_NAME`, the secret's name |
| `COLLECTOR_ORRERY_BUCKET` | worker, Orrery read service | the name of the environment's private Orrery bucket. For the worker, the bucket it publishes Orrery graph files to; without it, `orrery-inputs` submissions stay in intake storage (see "Publishing Orrery graph files"). For the Orrery read service, the bucket it serves them from; it refuses to start without it (see "Serving Orrery graph files"). The service doesn't get it | `TELEMETRY_ORRERY_BUCKET` |

The `TELEMETRY_*` names are GitHub `staging` and `production` environment
variables, which `.github/workflows/push.yaml` passes to `scripts/deploy.sh`.
The deploy also reads these, to update the two jobs and to deploy the Orrery
read service:

| Variable | Value |
|---|---|
| `TELEMETRY_RECOVERY_JOB` | the recovery job's name |
| `TELEMETRY_WORKER_SERVICE_ACCOUNT` | the worker job's service account |
| `TELEMETRY_RECOVERY_SERVICE_ACCOUNT` | the recovery job's service account |
| `TELEMETRY_HF_READ_SECRET_NAME` | the environment's own Hugging Face read token's secret, mounted on the worker job (see "Hugging Face read access") |
| `TELEMETRY_CALIBRATION_TREE_BLOB_SECRET_NAME` | the secret holding the read-write token of the environment's own calibration tree Blob store, mounted on the worker job only as `COLLECTOR_CALIBRATION_TREE_BLOB_TOKEN` (see "Building trees from submissions") |
| `TELEMETRY_ORRERY_BUCKET` | the name of the environment's private Orrery bucket, set on the worker job and on the Orrery read service as `COLLECTOR_ORRERY_BUCKET`; the service revisions and the recovery job don't get it (see "Publishing Orrery graph files" and "Serving Orrery graph files") |
| `TELEMETRY_ORRERY_READER_SERVICE_ACCOUNT` | the address of the Google service account the environment's microcosm-runs deployments act as: the one member the deploy grants the Cloud Run Invoker role on the Orrery read service. No revision, job or service gets it as a setting (see "Serving Orrery graph files") |

The two service-account settings accept only user-managed accounts,
`<name>@<project>.iam.gserviceaccount.com`. The collector refuses to start
with the default Compute Engine account
(`<number>-compute@developer.gserviceaccount.com`), so the runtime and invoker
accounts must be user-managed ones. The deploy checks the Orrery reader
account's address against the same pattern.

### Database access

The migration job's database user owns the tables it creates; the service,
the worker, the Orrery read service and the operator commands connect as the
runtime database user.
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
(`SELECT ... FOR UPDATE`) need `UPDATE`. The Orrery graph handler needs
`SELECT` and `INSERT` on `orrery_graph_files`, which migration
`0003_orrery_graph_files` creates: it records each published graph file with
`INSERT ... ON CONFLICT DO NOTHING RETURNING`, and `RETURNING` needs
`SELECT`. The Orrery read service, which connects as the runtime user too,
needs `SELECT` on `orrery_graph_files` and `artifact_submissions`. No table
uses a sequence. A `serial`
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
- **The inventory of past builds** (`uv run --no-sync python -m
  telemetry_collector.operations past-builds`) prints, and changes nothing.
  It reads Hugging Face, so an operator runs it as an execution of the worker
  job, the only job with the Hugging Face read token, with its arguments
  overridden (see "Past builds").
- **Projecting past builds again** (`uv run --no-sync python -m
  telemetry_collector.operations reproject-past-builds`, with `--dry-run` to
  list first) makes an `orrery-inputs` submission for each past build that
  can be projected and processes it at once. An operator runs it the same
  way, and only while the worker's account may create objects in the intake
  bucket (see "Projecting past builds again").

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
- **Database access:** after the migrations, the runtime database user has
  `SELECT`, `INSERT`, `UPDATE` and `DELETE` on `artifact_submissions`,
  `artifact_jobs` and `orrery_graph_files`, through the migration user's
  default privileges, which both environments already have (see "Database
  access").
- **Permissions:** the runtime service account may create and read objects in
  the bucket, sign as itself (`roles/iam.serviceAccountTokenCreator` on
  itself), enqueue on the queue, act as the invoker account, and list and
  read objects in its environment's private Orrery bucket
  (`roles/storage.objectViewer`), for the Orrery read service, which runs as
  it (see "Serving Orrery graph files"); the invoker
  account may run the worker job with overrides; the worker's service account
  may read the bucket, create and read objects in its environment's private
  Orrery bucket (see "Publishing Orrery graph files"), connect to Cloud SQL
  and read the runtime database secret, its environment's Hugging Face read
  token's secret and its environment's calibration tree Blob token's secret;
  the recovery
  job's service account may connect to Cloud SQL, read the runtime database
  secret, enqueue on the queue and act as the invoker account; Cloud
  Scheduler's account may run the recovery job; and the deployment service
  account may update both jobs and act as the worker and recovery accounts,
  and may deploy the Orrery read service as the runtime account and set its
  IAM policy (Cloud Run Admin, `roles/run.admin`, on the project).

Each release, in each environment, `scripts/deploy.sh`:

1. checks, before it changes anything:
   - that every intake name, the Orrery bucket's name and the Orrery reader
     account's address have the shape the collector accepts, and that the
     Orrery read service's name fits Cloud Run's 49 characters
     (`require-configuration`, before it authenticates), so
     a malformed name stops the deploy before a revision or job that reads it
     fails to start;
   - that each job exists, already runs as the service account the deploy
     gives it, and connects to the environment's own Cloud SQL instance
     (`check-intake-jobs`), so a variable naming the other environment's job
     stops the deploy. To move a job to another account, update the job by
     hand first;
   - that the environment's seven secrets exist, its Hugging Face read
     token's and its calibration tree Blob token's among them
     (`check-secrets-exist`);
2. passes the intake settings in the first table to the maintenance and
   candidate revisions;
3. after the migration and before the candidate revision, updates the worker
   job (`configure-worker-job`) and then the recovery job
   (`configure-recovery-job`) to the released image, with their whole
   configuration: service account, Cloud SQL connection, secrets, settings,
   command, task count, retries, task timeout, CPU and memory. Each update
   sets the job's secrets with `--set-secrets`, which replaces every mount,
   so the worker job's lists `DATABASE_URL`, `COLLECTOR_HF_READ_TOKEN` and
   `COLLECTOR_CALIBRATION_TREE_BLOB_TOKEN` together, and sets the worker
   job's settings, `COLLECTOR_INTAKE_BUCKET` and `COLLECTOR_ORRERY_BUCKET`,
   with `--set-env-vars`. The deploy changes neither the schedule nor any
   job's permissions;
4. last, once the service is live, deploys the Orrery read service, sets
   who may call it and checks that it refuses a call without a token
   (`deploy-orrery-read-service`; see "Serving Orrery graph files").

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

Before running the migration or service, provide the database connection, a
session signing key, and the dashboard read credential through the documented
runtime environment variables. Generate a P-256 key for local use with
`openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256` and pass the
PEM text as `TELEMETRY_JWT_SIGNING_KEY`. `TELEMETRY_JWT_SECRET`, at least 32
random characters, is needed only to issue or accept HS256 tokens. Use at
least 24 random characters for the dashboard read credential. Use Microcosm's
loopback-only development option when testing ingestion. Production Microcosm
builds use the fixed collector origin shipped by the package and do not accept
a destination environment variable.

## Tests

```bash
uv sync --locked --dev
npm ci --prefix orrery-validator --omit=dev --omit=peer --ignore-scripts
uv run pytest -q
bash tools/check-quality.sh
```

The Orrery validator's tests (`tests/test_orrery_validation.py`) run the
real validator, so they need Node 22.14 or later and the validator's
packages installed as above; CI uses Node 24.21.0, the release the image holds.

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
writes from them, byte for byte. It needs only the standard library. The
worker's Orrery graph handler calls it ("Publishing Orrery graph files").

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

### Orrery validation

`orrery-validator/validate-orrery-document.mjs` validates an Orrery graph
document under Node with one version of `@axiom-foundation/orrery`, pinned in
`orrery-validator/package.json` and its lockfile (0.6.2; its validation code
is identical to 0.6.1's, which microcosm-runs uses). It accepts exactly the
graph file contents microcosm-runs' publication accepts (`lib/service.ts`):

- the bytes decode as strict UTF-8 and parse as JSON, or the answer is
  `invalid_json`;
- Orrery's `parseGraphDocument` accepts the document, or it is
  `invalid_graph`, with Orrery's issues;
- `checkSources`: every `url` is an `https://` URL without a user name or
  password, or names, before any `#`, a file published with the document, or
  it is `invalid_source`. An `https://` value that is not a URL is refused
  here; microcosm-runs answers 503 for it instead.

A refusal says where a link is (its JSON path), never what it holds, since a
link may carry a credential, and keeps at most 100 issues of at most 500
characters (code points) each. Issue text always encodes as UTF-8 and can be
stored in PostgreSQL's `jsonb`: shortening keeps whole characters, and an
unpaired surrogate or a NUL, which a document's JSON can escape and Orrery
repeats in its issues, becomes U+FFFD. microcosm-runs also
refuses a graph file over 64 MiB and file names outside its role patterns;
those are checks on a publication to microcosm-runs, not on the document. The
collector publishes graph files to its own private bucket instead
("Publishing Orrery graph files"), and its projection already refuses a
document over 64 MiB.

`telemetry_collector.orrery.validation.validate_orrery_document(document,
published_file_names)` runs it and returns the Orrery version, raises
`OrreryDocumentRefusedError` with the code, issues and Orrery version for a
refused document, and raises `OrreryValidatorError` when the validator could
not run. Node gets only `PATH` and `LANG=C.UTF-8`, so none of the worker's
settings or credentials, such as `DATABASE_URL`, reach it, and its answer is
read as UTF-8 whatever the worker's locale; output that is not UTF-8 is a
validator error. By hand:

```bash
uv run python -m telemetry_collector.orrery.validation graph.orrery.json NAME...
```

The image holds Node 24.21.0, copied from the official image by digest, and the
validator installed with `npm ci`. npm is not in the image. CI checks the built
image with `tools/check-worker-image.sh`, which runs the validator inside it on
a projected document and on one Orrery refuses. The worker's Orrery graph
handler validates every document it publishes.

To move to another Orrery version, change the version in
`orrery-validator/package.json`, run `npm install --prefix orrery-validator
--package-lock-only --ignore-scripts`, and update `ORRERY_VERSION` in
`tests/test_orrery_validation.py` and `tests/test_orrery_graphs.py`.

### Publishing Orrery graph files

The worker job publishes an Orrery graph file from each `orrery-inputs`
submission (`telemetry_collector/orrery_graphs.py`) where it has
`COLLECTOR_ORRERY_BUCKET`, the name of its environment's private Orrery
bucket, which the deploy sets on the worker job and on the Orrery read
service, which serves the files ("Serving Orrery graph files"), from the
GitHub environment variable `TELEMETRY_ORRERY_BUCKET`. Without it,
submissions stay in intake storage and can be reprocessed later (`reprocess
--kind orrery-inputs --intake-only`). The handler is registered in
`telemetry_collector/handlers.py`, the worker's one registry, beside the
calibration tree handler.

- **Inputs.** Each attempt downloads the schema and the execution overlay
  into a new temporary directory. Upstream files are downloaded only after
  links are removed, and only those the document still links to: removing
  links needs only their names, from the submission's file list, and the
  projection doesn't read them. Evidence files are never downloaded. The
  handler reads the schema and overlay as Microcosm's own command reads its
  input (`_read_json` at the projection's source commit): at most 32 MiB,
  strict JSON in UTF-8, no key repeated within an object, no `NaN` or
  infinity, and nested no deeper than Python's JSON decoder can parse. The
  projection, `orrery_json_from_schema(schema, execution=overlay)`, builds
  the document. These reject the submission with `orrery_inputs_unusable`:
  - a schema or overlay that fails those reading rules. A schema over
    32 MiB is rejected here, although intake accepts schemas up to 64 MiB;
  - a `TypeError`, `ValueError` or `RecursionError` from the projection, with
    its message shortened to 500 characters. Microcosm's command treats these
    as input errors, and also `OSError` and `GraphRuntimeError`, which the
    copied projection never raises;
  - an upstream file not named `upstream-<its SHA-256>.json`: `upstream-`,
    64 lowercase hex characters that are the SHA-256 the worker verified for
    the file, and `.json`. Microcosm names each producing run's file it
    copies that way (`save_graph_schema`), and the Orrery read service takes
    a served upstream file's SHA-256 from its name ("Serving Orrery graph
    files"). The rule also keeps an upstream file from taking the document's
    own name, `graph.orrery.json`.

  The worker records a NUL or an unpaired surrogate in a rejection's message,
  such as a schema column name the projection repeats, as U+FFFD, as it does
  for every handler, because PostgreSQL's `jsonb` cannot store them. Any
  exception from the projection other than a `TypeError`, `ValueError` or
  `RecursionError`, for example a `KeyError` or an `AttributeError`, fails the
  attempt and is retried, as Microcosm's own command would crash on it.
- **Links.** Evidence files stay in private intake storage, so a published
  document doesn't link to them: each evidence reference keeps its label and
  SHA-256 but loses its link, whatever the link's scheme, `https://`
  included. The evidence references are the overlay's phase references
  (`metadata.execution.phases[].references`) and the document's `artifacts`,
  which the projection builds only from the overlay: it copies a phase
  reference's `url` into its artifact's `uri`. There every `url` and `uri` is
  removed unless it names a submitted upstream file before any `#`.
  Everywhere else, a `url` or `uri` stays only when it starts with
  `https://` or names a submitted upstream file before any `#`, as Microcosm
  writes those links (`upstream-<sha256>.json`), and every other one is
  removed. The rest of each object stays.
- **What removal changes.** Each node or edge that lost a link gets a new
  `revision`: the projection's digest of the record as published, computed as
  the projection computes it. `metadata.execution` is the submitted overlay
  with its links removed as above, and `metadata.microcosm.extensions` the
  schema's extensions likewise. The digests that describe the submitted files
  are not changed: `metadata.microcosm.schema_sha256` and the document's own
  `revision` still describe the schema and overlay as submitted. The document
  is serialised exactly as the projection serialises it, so a document with
  nothing removed has the projection's bytes.
- **Checks.** The pinned validator ("Orrery validation") validates the
  document, with the upstream files it still links to as its published
  files. A refusal rejects the submission with `orrery_document_refused`,
  naming the validator's code and its first issue's path; a validator that
  could not run fails the attempt, so it runs again.
- **Storage.** The document and each upstream file it links to are written
  to `graphs/<submission_id>/<document sha256>/` in the bucket, as
  `graph.orrery.json` and under their own names, upstream files first, with
  the content type `application/json` and their SHA-256 in the `sha256`
  metadata, as intake objects carry it. A write only ever creates an object
  (`if_generation_match=0`). An existing object with the same SHA-256 is
  kept, so a retry, or a reprocessing with the same code, writes nothing
  new, and a reprocessing after the projection changes writes a new folder.
  An existing object with another SHA-256 fails the attempt. Upstream files
  the document doesn't link to, and evidence files, are not written.
- **Record.** Each document is recorded in `orrery_graph_files`: its
  submission and processing round, run, build and country, its folder,
  SHA-256 and size, how many upstream files it links to, the Orrery version
  that validated it, the Microcosm commit the projection was copied from
  (`projection_source`), the collector's version, whether it was
  reconstructed (true only for a document the collector rebuilt from a past
  build's staged inputs, whose submission `microcosm-collector` owns; see
  "Projecting past builds again"), and its visibility. A document already
  recorded for the submission keeps its first row.
- **Visibility.** A licensed build's graph file is recorded `private`: it
  stays in the private bucket, and no clearance step makes it public. Any
  other build's is recorded `public`. The Orrery read service serves both
  only to microcosm-runs, which shows licensed runs' files only to people
  signed in with policyengine.org Google accounts (tracker 2b · 7 and
  4b · 3), and treats a recorded `public` document as private when a run or
  a build it comes from had licensed data (see "Serving Orrery graph
  files").
- **Result.** The receipt's result is `{"processed_by": "orrery-graph",
  "object_prefix", "document_sha256", "document_bytes", "upstream_files",
  "orrery_version", "visibility", "removed_links"}`, where `removed_links`
  counts the `url` and `uri` keys removed.

Each environment's bucket, and the worker account's access to it, are set up
before the first deploy, as the permissions list in "Worker jobs and
recovery" describes. The deploy neither creates nor checks them. Each
bucket needs:

- **Private:** uniform bucket-level access, with public access prevention
  enforced.
- **Access:** only that environment's worker service account and runtime
  service account may use its objects. The worker's account has
  `roles/storage.objectCreator` and `roles/storage.objectViewer` on the
  bucket: it creates objects and reads an existing object's SHA-256 back,
  and can neither replace nor delete one. The runtime account, which the
  Orrery read service runs as, has `roles/storage.objectViewer` on the
  bucket: it lists and reads objects to serve them ("Serving Orrery graph
  files"), and can neither create, replace nor delete one.

Once `0003_orrery_graph_files` is applied, an image without it cannot run
against the database, as with `0002_artifact_intake` above: its migration job
cannot find the revision and its `/ready` check fails. To roll back to such
an image, first run `alembic downgrade 0002_artifact_intake` with this
release's code, as the migration database user. That drops
`orrery_graph_files` and every record in it; the graph files stay in the
bucket, and reprocessing the submissions records them again.

### Serving Orrery graph files

The Orrery read service serves the Orrery graph files the worker published
to microcosm-runs alone, for every document, public or private.
microcosm-runs decides what to show to whom: it shows licensed runs' files
only to people signed in with policyengine.org Google accounts (tracker
4b · 3). The read service is a separate Cloud Run service in each
environment, named `<service>-orrery` after the environment's collector
service (`TELEMETRY_SERVICE`). It runs the collector's image with an
application that mounts only the two routes below
(`telemetry_collector/orrery_read_app.py`; the routes are in
`telemetry_collector/orrery_graph_api.py`). The collector's own service
mounts neither route, and gets no Orrery setting.

**Cloud Run's caller check is the read service's only access control.** Its
code verifies no token and reads no credential, so the service must never
allow unauthenticated calls, and its Cloud Run invoker check must never be
switched off: either would serve every file, licensed ones included, to
anyone. Every deploy sets all of it again, and then checks it:

- `--no-allow-unauthenticated` and `--invoker-iam-check`;
- no custom audience (`--clear-custom-audiences`), so a token must be for
  the service's own URL, which Cloud Run always accepts;
- the service's whole IAM policy: the Cloud Run Invoker role
  (`roles/run.invoker`) for the environment's microcosm-runs account,
  `TELEMETRY_ORRERY_READER_SERVICE_ACCOUNT`, and nothing else, so `allUsers`,
  `allAuthenticatedUsers` or another member added by hand is removed. The
  deploy then reads the policy back and fails unless it is exactly that;
- a call without a token: the deploy reads the service's URL (`gcloud run
  services describe <service>-orrery --format 'value(status.url)'`), sends
  `GET <URL>/v1/orrery-graphs?run_id=deploy-access-check` with no
  `Authorization` header, and fails unless the answer is 401 or 403. It
  prints only the status code. IAM can take a few minutes to apply a policy
  change, so the first deploy after someone grants `allUsers` by hand can
  fail this check even though it has removed the grant; re-running the job
  then shows whether the service refuses the call.

**Who else can call it.** The service's own IAM policy covers only roles
granted on the service itself. Anyone granted, on the project, its folder or
the organization, any role that includes the permission to invoke services,
`run.routes.invoke`, can call the read service too, beside the reader
account its policy names: IAM passes roles granted on a folder or the
organization down to the project. That covers custom roles with the
permission as well as predefined ones. IAM's permissions reference
(<https://docs.cloud.google.com/iam/docs/roles-permissions/run>, the row for
`run.routes.invoke`) lists these predefined roles as including it:

| Role, as the reference titles it | Role ID |
|---|---|
| Admin | `roles/admin` |
| Writer | `roles/writer` |
| Owner (Legacy) | `roles/owner` |
| Editor (Legacy) | `roles/editor` |
| Cloud Run Admin | `roles/run.admin`, which the deployment service account holds |
| Cloud Run Developer | `roles/run.developer` |
| Cloud Run Editor | `roles/run.editor` |
| Cloud Run Invoker | `roles/run.invoker` |
| Cloud Run Service Invoker | `roles/run.servicesInvoker` |
| Cloud Run Source Developer | `roles/run.sourceDeveloper` |
| Cloud Functions Admin | `roles/cloudfunctions.admin` |
| Cloud Functions Editor | `roles/cloudfunctions.editor` |
| Cloud Functions Developer | `roles/cloudfunctions.developer` |
| Firebase Admin | `roles/firebase.admin` |
| Firebase Develop Admin | `roles/firebase.developAdmin` |
| Data Scientist | `roles/iam.dataScientist` |
| Dev Ops | `roles/iam.devOps` |
| ML Engineer | `roles/iam.mlEngineer` |

The same row also lists twelve service agent roles, which the reference says
to grant only to service agents, the Google-managed accounts that act for a
Google service: `roles/aiplatform.serviceAgent`, `roles/ces.serviceAgent`,
`roles/cloudfunctions.serviceAgent`,
`roles/cloudfunctions.standardServiceAgent`, `roles/dialogflow.serviceAgent`,
`roles/firebaseapphosting.serviceAgent`,
`roles/firebasedataconnect.serviceAgent`, `roles/integrations.serviceAgent`,
`roles/monitoring.notificationServiceAgent`, `roles/run.serviceAgent`,
`roles/spanner.serviceAgent` and `roles/visionai.serviceAgent`. Cloud Run's
access control page names only some of these roles, so check the permissions
reference's row, which changes as Google adds roles.

Anyone who may make identity tokens as the reader account can also call the
read service, as that account. Making one needs
`iam.serviceAccounts.getOpenIdToken` on the account. The Workload Identity
principals of the matching Vercel deployments have it, and so does anyone
granted, on the account, on the project that holds it, or on that project's
folder or organization, a role that includes it. IAM's permissions reference
(<https://docs.cloud.google.com/iam/docs/roles-permissions/iam>, the row for
`iam.serviceAccounts.getOpenIdToken`) lists these predefined roles, beside
service agent roles:

| Role, as the reference titles it | Role ID |
|---|---|
| Service Account OpenID Connect Identity Token Creator | `roles/iam.serviceAccountOpenIdTokenCreator` |
| Service Account Token Creator | `roles/iam.serviceAccountTokenCreator` |
| Workload Identity User | `roles/iam.workloadIdentityUser` |

No basic role includes it, not even Admin or Owner (Legacy), but anyone who
may change the account's or the project's IAM policy can grant themselves
one of these roles.

Grant the roles that include `run.routes.invoke` only to administrators, and
the roles that include `iam.serviceAccounts.getOpenIdToken` on the reader
account only to the matching Vercel deployments' principals. In particular, grant an account that must run a job, such as the worker's
invoker account or Cloud Scheduler's account, its role on that job, not on
the project. Whether any account other than an administrator holds one of
these roles in either environment is not checked.

| Setting | Value | The deploy sets it from |
|---|---|---|
| `COLLECTOR_ORRERY_BUCKET` | the environment's private Orrery bucket's name, checked as the worker checks it | `TELEMETRY_ORRERY_BUCKET` |
| `DATABASE_URL` | the collector's database, as for the service | the runtime database secret, `TELEMETRY_DATABASE_SECRET_NAME` |

The read service refuses to start without either setting.

**The deployed service** (`scripts/deploy.sh deploy-orrery-read-service`).
Each deploy job runs it last, once the collector's service is live, so that
a failure leaves the service released and out of maintenance mode. The
read service reads the migrated database, so it is deployed after the
migration; it has no maintenance mode, and while a migration runs, its
previous revision keeps answering.

| What | Value | Why |
|---|---|---|
| Name | `<service>-orrery` | needs no variable of its own; `require-configuration` checks it fits Cloud Run's 49 characters before the deploy authenticates |
| Identity | the runtime service account | it may read the bucket, connect to Cloud SQL and read the runtime database secret, as the service does |
| Start | `uv run --no-sync uvicorn telemetry_collector.orrery_read_app:create_orrery_read_app_from_environment --factory --host 0.0.0.0 --port 8080 --no-access-log`, with container port 8080 | Cloud Run sends requests to and probes port 8080. The TCP startup probe checks only that the service listens: it has no health route |
| Request timeout | 300 seconds | a fetch of a 64 MiB file reads it twice, in about 130 ranged reads of 1 MiB one after another, and sends it during the second read. Each Cloud Storage request gives up after 10 seconds without connecting or without data, and each call retries for at most 30 seconds: under a quarter of the timeout together |
| Memory, CPU, concurrency | 512 MiB, 1 CPU, 10 requests per instance | a request holds about one 1 MiB part of a file at a time, so ten fit beside the Python process with room to spare; hashing and sending are light work for one CPU |
| Instances | 0 to 2, in both environments | each instance's database pool holds at most five connections, so the read service holds at most ten. microcosm-runs calls it only to show a run's files, and may cache public ones |

The deployment service account deploys it as the runtime account and sets
its IAM policy with Cloud Run Admin (`roles/run.admin`) on the project; it
already may act as the runtime account, as it does for the service. The
runtime service account has `roles/storage.objectViewer` on its
environment's bucket, so it can list and read objects and can neither
create, replace nor delete one, and the runtime database user reads
`orrery_graph_files` and `artifact_submissions` (see "Database access").
Both were set up before the first release that serves the files; the deploy
neither creates nor checks them.

**How microcosm-runs calls it.** microcosm-runs gets a Google-signed
identity token for its environment's reader account from its Vercel
deployment's identity, without a stored key: it calls the IAM Credentials
API's `generateIdToken` for that account with the read service's URL as the
audience, `"audience": "<read service URL>"`, and `"includeEmail": true`. It
sends the token as `Authorization: Bearer <token>` to that same URL. The URL
is the deployed service's `run.app` URL, a scheme and a host with no path,
exactly as `gcloud run services describe <service>-orrery --region <region>
--format 'value(status.url)'` shows it; the `Service URL:` that `gcloud run
deploy` prints in the deploy job's log is the same. Cloud Run
always accepts a token whose audience is the service's URL, and the service
has no custom audience. The read service has no load balancer or hostname of
its own. Each environment's read service has its own URL, so the audience
differs between environments. microcosm-runs' production deployments use
the collector's production environment, and its preview deployments use
staging; each environment has its own reader account, which only the
matching Vercel deployments can act as.

Cloud Run's documentation doesn't say whether it needs the token's `email`
claim. Its authentication pages name only the audience (`aud`) as the claim
to set, and its own examples get tokens without `email`: the metadata
server's identity endpoint without `format=full`, and `gcloud auth
print-identity-token` without `--include-email`. Asking for the claim costs
nothing, and the read service reads no claim.

**Answers.**

- **Cloud Run's own refusals,** before a request reaches the read service:
  401 for an `Authorization` header without a Bearer ID token, a token that
  isn't a validly signed Google ID token, or one for another audience; 403
  for a valid token of an account without the Invoker role. Cloud Run's
  documentation says a request without a token can get 403 too. These are
  Cloud Run's answers, not the collector's: they have neither its
  `{"detail", "code"}` shape nor its `Cache-Control` header.
- **`GET /v1/orrery-graphs?run_id=<run>&build_id=<build>`**, with either or
  both, lists the recorded documents of the run, the build, or both, newest
  first, as `{"documents": [...]}`. Each has `submission_id`,
  `document_sha256`, `document_bytes`, `run_id`, `build_id`, `country_code`,
  `orrery_version`, `projection_source`, `collector_version`,
  `reconstructed`, `created_at`, `visibility` (its effective visibility,
  below), `current`, and `document_path` and `upstream_paths`, the paths of
  the document and of each upstream file in its folder. Neither filter, or a
  malformed one, answers 422 with `orrery_graph_query_invalid`.
- **`GET /v1/orrery-graphs/<submission_id>/<document_sha256>/<file_name>`**
  returns `graph.orrery.json` or one of its `upstream-<sha256>.json` files
  from the bucket. Each part is checked before it names an object: the
  submission ID as the collector derives it (`sub-` and 32 lowercase hex
  characters), the digest as 64 lowercase hex characters, and the file name
  as exactly one of those two forms. Any other path, a document without a
  row, and an object that doesn't exist answer 404 with
  `orrery_graph_not_found`; a path holding a parent folder or an encoded
  slash never names an object.
  - **The check.** Objects are never replaced, so the service reads the
    object at the generation it found, twice, in ranged reads of 1 MiB,
    holding at most one part at once. The first read hashes the bytes,
    keeping none, and their SHA-256 must equal the document's
    `document_sha256` in its row, or, for an upstream file, the SHA-256 in
    its name, which the worker checked is the file's own. The objects'
    `sha256` metadata is not read. Only on a match does the second read send
    the bytes, so nothing is sent before the whole file has matched.
  - 500 (`orrery_graph_digest_mismatch`), logged, nothing served: the bytes
    don't match, or the storage client reports a checksum failure.
  - 500 (`orrery_graph_changed`), logged, nothing served: the generation
    found is no longer stored when the first read asks for it.
  - **A client must check the body against the `ETag`.** If the second read
    fails (the generation is no longer stored, the bucket stops answering,
    or the storage client reports a checksum failure), the status 200 and
    the headers have already been sent. The read service logs the failure
    and stops sending, so the body ends early. Whether an HTTP client then
    reports an error depends on the client and on what Cloud Run's front
    end passes on, which is not checked. So before it caches or shows a
    file, microcosm-runs must compare the SHA-256 of the body it received
    with the `ETag`, and with the listed `document_sha256` for a document
    or the SHA-256 in an upstream file's name, and discard the file when
    they differ (tracker 4b · 3).
- **A bucket that doesn't answer** gives 503 with
  `orrery_storage_unavailable` and `Retry-After`, on both endpoints, and a
  warning in the log with the object or folder and the storage error's type,
  never its message, which can quote the request. Each Cloud Storage
  request gives up after 10 seconds without connecting or without data,
  instead of the client's 60, and each call retries for at most 30 seconds,
  instead of the client's 120.
- **A database that doesn't answer** gives 503 with
  `orrery_catalogue_unavailable` and `Retry-After`, on both endpoints.
- **Headers.** A served file has `Content-Type: application/json`, its
  SHA-256 as its `ETag` (`"<sha256>"`), and the document's effective
  visibility as `X-Orrery-Visibility: public` or `private`. It is sent in
  1 MiB parts without `Content-Length`, so that Cloud Run streams a file
  larger than its 32 MiB limit on a whole response. Every answer of the
  read service, the errors above and the 404 for any other path included,
  has `Cache-Control: private, no-store`. The errors with the codes above
  have the collector's `{"detail", "code"}` shape. Two answers have only
  `{"detail"}`, without a `code`: a 405 for a method a route doesn't serve,
  such as `POST`, and the 404 for a path that matches neither route, such
  as one with an encoded slash or with too few or too many parts. Only an
  unexpected exception, which the server answers with a plain-text 500 from
  outside the read service's own middleware, lacks both the header and a
  JSON body.
- **Current documents.** A build is a country code and a build ID: the
  contract doesn't require build IDs to differ between countries. Among
  all documents with the same `country_code` and `build_id`, whichever run
  recorded them, the one with the newest `created_at` is `current: true`.
  Documents recorded at the same moment are ordered by `document_sha256`,
  then by `submission_id`, the larger first. The others are listed with
  their dates. A listing by `build_id` that holds documents of two
  countries has a current document for each.
- **Effective visibility,** computed for every answer, so that a
  submission recorded later still counts. A document is `private` when its
  row is (its submission had licensed data), or when any recorded
  submission with licensed data has the run ID or the build ID of the
  document or of a producing submission of one of its upstream files.
  Otherwise it is `public`. Both IDs count because the contract defines
  `licensed_data` per build, and one build can be submitted from more than
  one run; matching on either can only make a document more private. The
  build ID is matched alone, whatever the submission's `country_code`,
  unlike `current`: matching more widely can only make more documents
  private. Upstream references carry no run ID. Microcosm copies a
  producing run's own file, byte for byte, as `upstream-<sha256>.json` (its
  graph declaration, run manifest, compiler schema or execution evidence
  index), so the file's SHA-256 is the link, taken from the file's name:
  the producing submissions of an upstream file are the recorded
  submissions, of any kind and status, that hold a file with that SHA-256
  in any role but `upstream`. A submission that lists the file only as its
  own upstream file copied it, so it is no producer. **An upstream file
  whose SHA-256 no recorded submission holds in another role does not
  change the visibility.**
- **Outside the read API.** The routes are not part of read API v2: the
  collector's service, which serves read API v2, doesn't mount them, and
  the read service publishes no OpenAPI document.

**Running it locally.** `uv run uvicorn
telemetry_collector.orrery_read_app:create_orrery_read_app_from_environment
--factory --host 127.0.0.1 --port 8081`, with `COLLECTOR_ORRERY_BUCKET`,
`DATABASE_URL` and Google credentials that may read the bucket. Nothing
checks the caller there, so keep it on `127.0.0.1`.

### Past builds

`python -m telemetry_collector.operations past-builds` prints an inventory of
past Microcosm builds as JSON, so that their Orrery graph documents can be
rebuilt. It writes nothing, and nothing stores the inventory:
`list_past_builds` in `telemetry_collector/past_builds.py` makes it afresh each
time, and later code that rebuilds documents calls the same function.

**What it lists.** One entry per build, from two places, joined by run ID:

- **Staged folders:** every `staged/<run_id>/` folder in each country's
  release dataset (`RELEASE_REPOS`), read with the collector's Hugging Face
  read token. Each dataset is read at the commit its `main` points at when the
  inventory starts, and the inventory records that revision.
- **Recorded runs:** every run in `telemetry_runs`. A run with no staged
  folder is listed with nothing to rebuild from Hugging Face. Microcosm
  registers UK runs as `GB`; the inventory calls the country `uk`, as
  `RELEASE_REPOS` does.

Each entry gives the run ID, country, dataset and revision, staged folder,
whether the collector recorded the run, the Microcosm commit and where it
came from (`commit_source`), its class, the graph files the folder holds (`graph.schema.json`, `graph.execution.json`,
`graph.orrery.json`, `execution.evidence.json`, `orrery.publication.json`)
and what can be rebuilt from Hugging Face:

| `rebuild` | The staged folder holds |
|---|---|
| `full` | the schema (`graph.schema.json`) and the execution overlay (`graph.execution.json`) |
| `declaration_only` | the schema without the overlay |
| `none` | no schema, or there is no staged folder |

The inventory also records when it was made, each dataset's revision and the
number of builds in each class, in each `rebuild` value and skipped. The
command prints that first, as one JSON document, and then one JSON document
for each build, so that no log entry grows with the number of builds.

**The Microcosm commit and its class.** The commit is `git rev-parse HEAD`
where the build ran, read from one of two places:

| `commit_source` | Where |
|---|---|
| `staged_manifest` | `summary.git_commit` in the folder's `staged_manifest.json`, which Microcosm copies from the build manifest's `git_commit` |
| `telemetry` | when no staged manifest records one: `details.identity.git_commit` in the run's `run` events, which the build's emitter (the microcosm-emitter package) sends from Microcosm's `runtime_identity()`; the first non-null one, in the order the events were emitted |

GitHub's compare API then says, for each distinct commit, whether it has the
merge commit of microcosm#1151 as an ancestor, and if not, whether it has
that of microcosm#1144. It compares `<commit>...<merge commit>`, so that when
the commit contains the merge commit, GitHub's answer lists no commits or
files:

| `commit_class` | The commit |
|---|---|
| `after_1151` | contains microcosm#1151 (one request) |
| `between_1144_and_1151` | contains microcosm#1144 but not #1151 (two requests) |
| `before_1144` | contains neither |
| `unknown` | couldn't be classed; `class_reason` says why |

A build is `unknown` when neither its staged manifest nor its telemetry
records a commit (for example, its folder has no staged manifest, or the
manifest isn't JSON), the commit isn't a 40-character commit hash, GitHub has
no such commit in PolicyEngine/microcosm, or GitHub gave no usable answer.

**Skipped builds.** Builds made before microcosm#1144 are skipped, since they
recorded no execution evidence, and so are builds made between #1144 and #1151
whose `rebuild` is `none`. Both stay in the inventory with `skipped` true and
the reason in `skip_reason`. `unknown` builds are never skipped.

**GitHub's rate limit.** The inventory calls GitHub without a token, and
GitHub allows 60 such requests an hour from one IP address. Each distinct
commit takes one or two requests. After a rate-limit answer, or any failure
other than GitHub not having the commit, that commit and every later one are
`unknown` with the reason, and the command sends no more requests; nothing is
guessed. Those builds also have `class_retry_later: true`, since a later run
may class them; builds `unknown` for any other reason have `false`. Cloud Run
jobs reach GitHub from addresses that other Google Cloud users share, so part
of the hour's allowance may already be used.

So that the builds that can be rebuilt are the ones classed when the
allowance runs out, commits are checked in this order, while the inventory
stays in run-ID order:

1. staged builds whose `rebuild` is `full` or `declaration_only`;
2. other staged builds;
3. recorded runs without a staged folder.

Within each group they are checked in descending run-ID order. That puts
newer builds first among run IDs that share a prefix, such as
`uk-frs-calibration-attempt-<UTC time>-<suffix>`.

**Running it.** It needs `DATABASE_URL` and `COLLECTOR_HF_READ_TOKEN`, which
only the worker job mounts together (production's daily Hugging Face check job
mounts the token but not the database), and stops with a message naming either variable
when it is missing. Run it as an execution of the worker job with its
arguments overridden:

```bash
gcloud run jobs execute <worker job> --project <project> --region <region> \
  --args=run,--no-sync,python,-m,telemetry_collector.operations,past-builds \
  --wait
```

The three values are the environment's GitHub variables
`TELEMETRY_WORKER_JOB`, `TELEMETRY_GCP_PROJECT_ID` and `TELEMETRY_GCP_REGION`.
Running a job with overridden arguments needs `run.jobs.runWithOverrides` on
it. Each printed document is one entry in the execution's log. A failed
execution is retried up to twice, like any worker execution; a retry only
repeats the reads.

### Projecting past builds again

`python -m telemetry_collector.operations reproject-past-builds` publishes
Orrery graph files for past builds, as if each had sent its graph inputs as a
new build does. It uses the inventory of past builds (see "Past builds") and
`telemetry_collector/past_build_projections.py`.

**Which builds.** A build is projected when the inventory classes it
`after_1151`, its `rebuild` is `full`, and it isn't skipped. Every other build
is listed as `left_out`, with the reason, checked in this order:
1. skipped builds;
2. builds whose staged folder holds the schema without the execution
   overlay, or no schema, or that have no staged folder;
3. builds classed `before_1144`, `between_1144_and_1151` or `unknown`;
4. builds one of whose submitted files Hugging Face lists without a last
   commit, after the checks that make a build `refused` (see "Checks before
   downloading").

A build left out only because GitHub stopped answering before it was classed
(`class_retry_later` in the inventory), with nothing else keeping it from
being projected, is also marked `incomplete`. A build that could never be
projected, such as a recorded run without a staged folder, never is.

**The submission.** For each build the command makes the `orrery-inputs`
submission the build would have sent:

| Field | Value |
|---|---|
| files | `graph.schema.json` (`schema`), `graph.execution.json` (`execution_overlay`) and every `upstream-<sha256>.json` (`upstream`) in the staged folder, read at the inventory's revision of the dataset. Evidence files are left out, since the collector never publishes them |
| `licensed_data` | true, unless the build was staged in a public dataset (`HUGGINGFACE_DATASETS`): the UK and Belgian release datasets are private, so their documents are `private` |
| `provenance` | the build's run ID as `run_id` and `build_id`, its country as Microcosm registers it (`GB` for `uk`), no release, and `huggingface`: the dataset, the staged folder, and the newest last commit among the submitted files, from one listing of the folder with `expand=true` |
| owner | `microcosm-collector`, an ID no Hugging Face user has |

The submission ID is the hash of the request, as for any submission. So a
second run makes the same submission, finds it already processed, and changes
nothing. Only a change to a submitted file makes a new one; a later change to
another file in the folder, such as its staged manifest, doesn't.

**Checks before downloading.** Each build is first checked from Hugging
Face's listings alone, in a dry run too, and is `refused` or `left_out` when:
- the files, with their listed sizes, break the submission rules (each role's
  size limit, at most 256 files, at most 8 GiB in all);
- the schema or the overlay is over the Orrery handler's 32 MiB input limit;
- Hugging Face lists a submitted file without a last commit (`left_out`).

**Processing.**
1. The command downloads each file and checks it against the listing: its
   size, and its SHA-256 for a file stored with LFS or its git blob ID
   otherwise. It then creates the file in the intake bucket under the
   submission's ID, create-only and with its SHA-256 in its metadata, as an
   upload does.
2. It accepts the submission as completion accepts one: each stored object
   must have the size and SHA-256 the submission's file list gives, and a
   stored object that differs rejects the submission with
   `stored_object_mismatch`.
3. It runs the submission's job at once, through the worker's `run_job` and
   the registered handlers. The worker hashes every stored file, and the
   Orrery handler projects, validates, publishes and records the document (see
   "Publishing Orrery graph files").
4. A job that fails before its last attempt is released, stays `queued`, and
   the scheduled recovery dispatches it to the worker job.
5. A submission an earlier run left `awaiting_upload` is completed. A task
   retry in the same Cloud Run execution continues a job its killed attempt
   left `processing`, since it has the same lease holder. A later execution
   has another holder, so the job is skipped (`processing`) until its
   65-minute lease expires and recovery queues it again.

The command prints one JSON document for each build, as soon as the build is
done: its run ID, its `outcome`, the reason, the submission ID, whether this
run created the submission, the handler's result, and whether it is
`incomplete`, so a later run should try it again. The outcomes are:
- `left_out` and, in a dry run, `would_reproject`;
- `refused`, when the staged files break the submission rules;
- `error`, when anything else stops that build, such as a failed Hugging
  Face or Cloud Storage request; the next build is still projected;
- the submission's status: `succeeded`, `rejected`, `failed`, `queued` or
  `processing`.

It then prints a summary:

```json
{"summary": {"outcomes": {}, "incomplete_run_ids": [], "unpublished_run_ids": []}}
```

- `incomplete_run_ids`: builds a later run should try again, because they
  ended in `error` or would be projected but GitHub stopped answering before
  they were classed. The command exits non-zero while there are any.
- `unpublished_run_ids`: projected builds whose document isn't published,
  because their job is `queued` or `processing` for the scheduled recovery,
  or ended `failed` or `rejected`. They don't change the exit code.

Every document a run publishes is recorded with `reconstructed: true` in
`orrery_graph_files`, so microcosm-runs can mark it as rebuilt from a past
build.

**When it is done.** Every attempt's output stays in the execution's log:
each printed document is one structured entry, which the Cloud Run console
shows for the execution, or this selects:

```bash
gcloud logging read 'labels."run.googleapis.com/execution_name"="<execution>" AND jsonPayload.summary:*' \
  --project <project> --format=json
```

The rebuild is complete when a summary lists no incomplete and no
unpublished builds. A build an earlier attempt reported `succeeded` stays
done even if a later attempt, short of GitHub requests, lists it as
incomplete. Judge the rebuild by these summaries, not by whether the
execution succeeded. An attempt whose inventory itself fails prints no
summary, and the log shows the error. For unpublished builds:
- `queued` or `processing`: wait for recovery (it runs every 10 minutes, and
  a job another holder leased waits up to 65 minutes), then run the command
  again. A rerun reports each build's current status and changes nothing
  already finished.
- `failed`: after fixing the cause, process it again with `reprocess
  <submission ID>`, run as the recovery job (see "Worker jobs and recovery").
- `rejected`: the handler refused its inputs, and rerunning won't change
  that; the outcome's reason says why.

**Running it.** It needs `DATABASE_URL`, `COLLECTOR_HF_READ_TOKEN`,
`COLLECTOR_INTAKE_BUCKET` and `COLLECTOR_ORRERY_BUCKET`, which the worker job
has, and stops with a message naming a missing one. The worker's service
account may only read the intake bucket. A run therefore also needs, for its
duration only, permission for that account to create objects in its own
environment's intake bucket (`roles/storage.objectCreator` on the bucket).
That permission is a manual step before the run, and it is removed after it.
It is needed only until each build's submission is accepted: an accepted
submission's job, its retries and recovery read intake storage and never
create in it. Creating can't overwrite or delete an object. While the permission exists, a
compromised worker could create a file where a pending upload is about to
go. The worker's hash check would then reject that submission with
`content_mismatch`; the file could not replace the upload's content.

A run must finish within the worker job's one-hour task timeout, and every
run downloads every eligible build's files again, even those already
projected, to work out each submission's ID. When the command exits non-zero,
or is stopped by the timeout, Cloud Run runs the task again, up to twice, like
any worker execution. A rerun builds the inventory again, makes the same
submissions, completes any left awaiting upload, and runs each queued job
again. Each run of a job is one more of its three attempts, so a job that
fails three times ends `failed`. Every run, a dry run included, uses GitHub
requests from the same hourly allowance. When the allowance runs out, the
task's retries in the same hour can't class more builds, so run the command
again an hour later. A dry run and the real run each use up to two requests
for every distinct commit, so leave an hour between them, and between the
two environments' runs, unless there are only a few builds to class.

First list what a run would do. A dry run makes the checks above, downloads
no build's graph inputs and writes nothing. It still reads what the inventory
reads (Hugging Face's listings and staged manifests, the database and GitHub)
and each eligible build's folder listing.

```bash
gcloud run jobs execute <worker job> --project <project> --region <region> \
  --args=run,--no-sync,python,-m,telemetry_collector.operations,reproject-past-builds,--dry-run \
  --wait
```

Then, an hour later, run it, in staging first:

```bash
gcloud run jobs execute <worker job> --project <project> --region <region> \
  --args=run,--no-sync,python,-m,telemetry_collector.operations,reproject-past-builds \
  --wait
```

The three values are the environment's GitHub variables, as for "Past
builds". Once the rebuild is complete, remove the worker's permission and
check that it is gone:

```bash
gcloud storage buckets remove-iam-policy-binding gs://<intake bucket> --project <project> \
  --member=serviceAccount:<worker service account> --role=roles/storage.objectCreator
gcloud storage buckets get-iam-policy gs://<intake bucket> --project <project> --format=json
```

The bucket and the account are the environment's `TELEMETRY_INTAKE_BUCKET`
and `TELEMETRY_WORKER_SERVICE_ACCOUNT`; the policy must list no
`roles/storage.objectCreator` for the worker's account.

**Undoing it.** No command undoes a run, and the worker can't delete
anything. To remove a rebuilt document by hand, delete its submission's
`artifact_submissions` row, which also deletes its job and its
`orrery_graph_files` rows, so the Orrery read service no longer lists or
serves it. Its objects stay in the intake and Orrery buckets until they are
deleted too.

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

`src/calibration-tree-worker.ts` builds one release's or one staged run's
tree from local files and writes its gzip parts to a directory, as its own Bun
process, and `src/calibration-tree-publication.ts` uploads those parts and adds
the build to the manifest. `src/calibration-tree-latest.ts` points a country's
latest at a build the manifest already records (see "Moving the latest when
releases.json changes"):

```bash
cd packages/calibration-tree
bun run src/calibration-tree-worker.ts REQUEST.json OUTPUT_DIRECTORY
CALIBRATION_TREE_BLOB_READ_WRITE_TOKEN=... \
  bun run src/calibration-tree-publication.ts PUBLICATION.json
CALIBRATION_TREE_BLOB_READ_WRITE_TOKEN=... \
  bun run src/calibration-tree-latest.ts LATEST.json
```

The worker builds a release exactly as the publisher builds it from the same
files, commit and publication time, and a staged run as the publisher builds a
staging tree, from the files it is given. It prints the manifest entry the
publisher would record. The publication command uploads each part to its
immutable path (keeping a part already stored with the same bytes), then
writes the manifest with a compare-and-swap: it creates the manifest only if
it is still absent and replaces it only if its ETag is still the one read, so
a manifest another writer changed is never overwritten, and a manifest read
without an ETag is never replaced. A build asked to become its country's
latest does so only if it is at least as new (`createdAt`) as the current
latest. Exit 2 means the build is refused: another build is already recorded
for the same release build or staged run, or the same build was already
recorded from another submission of the same files, whose index records
another time or label.

### Building trees from submissions

The worker job builds a calibration tree from each `calibration-diagnostics`
submission (`telemetry_collector/calibration_trees.py`) where it has
`COLLECTOR_CALIBRATION_TREE_BLOB_TOKEN`, the read-write token of its
environment's own Vercel Blob store, which the deploy mounts on the worker job
only, from the secret `TELEMETRY_CALIBRATION_TREE_BLOB_SECRET_NAME` names.
Each environment has its own store, which nothing reads until the dashboard
switches to it, and its own secret, which only its worker service account may
read. Without the token, submissions stay in intake storage and can be
reprocessed later. With it, the worker job also needs
`COLLECTOR_HF_READ_TOKEN`, which the deploy mounts beside it, and stops with
a message naming that variable without it. A tree's files and provenance come
from the submission; only the latest build is decided from Hugging Face.

- **Which tree.** A build folder, `releases/<release>/<build>`, gives a release
  tree whose release ID is `<release>/<build>`; its `release` and `build_id`
  must name the same release and build. A staged bundle, `staged/<run_id>`,
  gives a staging tree of that run, labelled with the submission's `release`
  (the candidate release) or, when that is null, the run ID; its `run_id`
  must be the folder's. Files must come from the country's release repo, or,
  for a staged bundle, its staging repo. Contradicting provenance and other
  repos reject the submission. Other folders, and countries the collector
  does not list, stay in intake storage with the reason in the result.
- **Time.** The tree records the submission's `accepted_at` as its
  publication time, so reprocessing a submission builds the same tree.
- **Latest.** For every country the collector lists, a release build becomes
  its country's latest when the release repo's `releases.json` names it as
  the current build of its default release, as the GitHub Actions publisher
  decides (the names are compared). The handler reads `releases.json` at the
  repo's head commit after the build, under the manifest lock and just before
  the publication, so a build made current while its tree is being built is
  never missed (see "Moving the latest when releases.json changes"). Other
  builds are recorded without moving the latest.
  - A repo without `releases.json` (Hugging Face's 404 without the
    `RepoNotFound` or `RevisionNotFound` error code) keeps the
    `DEFAULT_RELEASES` rule: a build of the country's default release there
    becomes its latest. Only the UK's is listed,
    `microcosm-uk-2024-25-national`, so builds of another country whose repo
    has no `releases.json` don't become its latest.
  - Any other failure to read `releases.json` fails the attempt before
    anything is published, so it runs again.
  - A `releases.json` the record's rules refuse (as the tree package's
    `release-record.ts` reads it: at most 4 MiB, exactly its keys,
    `schema_version` 1, safe names, each `current` a listed build that isn't
    withdrawn, `default` a listed release or null) isn't retried: the tree is
    published without moving the latest, and the result records
    `latest_skipped: "releases_record_refused"` and the reason.
  - A build takes the latest only from an older build, by publication time,
    so a retried or reprocessed older build never takes the latest back.
  - A build made current later, with no new build, moves the latest through
    a `releases-record` submission instead (below).
  - Staging trees never become the latest.
- **Size.** The worker job has 8 GiB of memory and 2 CPUs: building the
  tree for the largest current diagnostics file (a 128.7 MB US release) peaked
  at 3.5 GB, and Cloud Run keeps the downloaded and written files in memory.
  Each execution is billed only while it runs.
- **How.** Each attempt downloads the files into a new temporary directory
  and runs the tree worker with only `PATH`, `LANG` and the package's size
  limits (`CALIBRATION_TREE_MAX_RAW_BYTES`, `CALIBRATION_TREE_MAX_GZIP_BYTES`)
  in its environment, never the Blob token. That keeps the token out of the
  builder's environment, arguments and output, but it is not isolation: the
  builder runs as the same user as the worker, which holds the token in its
  own environment. It then runs the publication command, with the token,
  inside a Postgres transaction lock (`pg_advisory_xact_lock`), so the
  collector's writers change the manifest one at a time.
- **Results.** A published tree's result names its build ID, kind, how many
  parts were uploaded or already stored, and whether it became the latest.
  The worker refusing the files (`calibration_tree_unbuildable`), or the
  build being refused (`calibration_tree_refused`: another build already
  recorded for the same release build or staged run, or the same files
  already recorded from another submission), rejects the submission; any
  other failure, including a manifest another writer changed meanwhile, fails
  the attempt so it runs again.

The image holds Bun 1.4.2, copied from the official image by digest, and the
package's production install, without its tests, fixtures and test support.
CI checks the built image with `tools/check-calibration-tree-image.sh`, which
builds a small staged run's tree inside it and runs the publication command
without a token, which must refuse to run.

### Moving the latest when releases.json changes

When `microcosm-publish-release-build --promote` makes an already published
build current, no tree is built. Microcosm then submits the repo's new
`releases.json` as a `releases-record` submission (docs/artifact-submissions.md):
its one file is the `releases.json` the change wrote, and its provenance names
the release repo, the commit that wrote it, and the release and build it made
current, whose folder is `releases/<release>/<build>`. Where the worker job has
its Blob token, `calibration_trees.calibration_tree_latest_handler` handles it,
for every country the collector lists:

1. **Checks.** A repo that isn't the country's release repo rejects the
   submission (`repo_not_registered`), and a country the collector does not
   list stays in intake storage with the reason. A submitted `releases.json`
   the record's rules refuse rejects it (`releases_record_refused`), and so
   does one that doesn't name the build as its release's current build
   (`provenance_mismatch`). The release need not be the default one.
2. **Under the manifest lock,** it reads `releases.json` at the repo's head
   commit, not the submitted copy, so a later change is never undone by an
   earlier change's submission. It then runs the package's latest command,
   `src/calibration-tree-latest.ts`, for the default release's current build
   there. The command points the country's latest at the tree the manifest
   records under that release ID, whether that build is newer or older than
   the current latest (a rollback in `releases.json` moves the latest back).
   It writes only the manifest, with the same compare-and-swap as the
   publication command, and runs with only `PATH`, `LANG` and the Blob token.
3. **Results.** `current_build`, `build_artifact_id`, `made_latest` (the
   country's latest is that build's tree) and `latest_moved` (this
   submission changed it). The latest is left as it is, and `latest_unchanged`
   says why, when the head has no `releases.json` (`no_releases_record`), one
   the rules refuse (`releases_record_refused`, with
   `latest_unchanged_reason`), no current build of its default release
   (`no_current_build`), or a current build the manifest records no tree for
   (`no_recorded_tree`). A failed read or any other failure of the latest
   command fails the attempt, so it runs again.

Step 2 is `calibration_trees.follow_releases_record(country, ...)`, which
builds nothing and takes the lock itself, so another collector job can call
it too, for example to set each country's latest to its `releases.json` once
it has built or backfilled trees. It returns the same fields as the result,
refuses a country the collector does not list, and raises a failed read or a
failed latest command for its caller to try again. It must not be called
while the caller holds the manifest lock.

Both handlers read `releases.json` and write the manifest under the one lock,
so a build made current while its tree is still being built becomes the
latest either way: its publication reads the change, or the change's handler
finds its tree. A tree whose publication comes after a rollback to it isn't
made the latest by that publication, which takes the latest only from an older
build; reprocessing the change's `releases-record` submission moves it.

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
workflow can be run by hand from `main` (`workflow_dispatch`) to retry a
failed publication, or to publish a version that was merged while publishing
was switched off.

The workflow uses npm's trusted publishing: npm accepts the publish job's
GitHub OIDC token in place of a stored npm token, and adds provenance because
this repo is public. npm does not check the branch, so the `npm` environment's
rule is what keeps other branches out. Nothing is published until the repo
variable `CALIBRATION_TREE_NPM_PUBLISH_ENABLED` is `true`, which needs this
one-time setup first, in this order:

1. **Google Cloud.** Unless it was already done for the read API client ("The
   TypeScript client", step 1), make the Workload Identity provider accept
   only the deployment environments ("Google Cloud access"), so that the
   publish job's token cannot obtain Google credentials. Its condition must
   then be:

   ```
   assertion.repository=='PolicyEngine/microcosm-collector' && assertion.ref=='refs/heads/main' && assertion.environment in ['staging', 'production']
   ```

2. **The first version, by hand.** npm can only trust a workflow for a package
   that exists, so a member of the `policyengine` npm organization publishes
   the first version from a clean checkout of the `main` commit that added the
   package: `bun install --frozen-lockfile`, then `npm pack --dry-run` to check
   the file list, then `npm publish --access public` in
   `packages/calibration-tree`.
3. **The environment.** In this repo's settings, create the `npm` environment
   and allow deployments from `main` only, so a run from any other branch
   cannot obtain npm's trust. Create it before the trusted publisher: GitHub
   creates a missing environment, without that rule, when a job first names
   it.
4. **The trusted publisher.** In the package's settings on npmjs.com, add a
   GitHub Actions trusted publisher: organization `PolicyEngine`, repo
   `microcosm-collector`, workflow `publish-calibration-tree.yaml`,
   environment `npm`. A new trusted publisher expires unless a publication
   through it succeeds within two days, so add it only when step 5 and a
   publication can follow within that time: merge a pull request that raises
   the version after step 5, or, if `main` already has a version npm doesn't
   have, run the workflow by hand from `main` after step 5. An expired one is
   deleted and added again.
5. **The switch.** Set the repo variable
   `CALIBRATION_TREE_NPM_PUBLISH_ENABLED` to `true`.

## Hugging Face read access

Only the collector's worker jobs read Hugging Face, for backfills, old
releases, back-building and the calibration tree handler's latest rule. The
service itself holds no Hugging Face
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

What reads Hugging Face:
- the inventory of past builds, and projecting them again;
- the calibration tree handler, which reads each release build's repo
  `releases.json` at its head commit to decide the latest (see "Building
  trees from submissions"), and the `releases-record` handler, which reads it
  to move the latest (see "Moving the latest when releases.json changes"),
  so both need the token wherever they have the Blob token.

A read answered with anything but HTTP 200 reports the status and, for a
404, Hugging Face's `X-Error-Code`, so a missing file (`EntryNotFound`) can be
told from a missing repo or revision (`RepoNotFound`, `RevisionNotFound`).

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

## Daily Hugging Face check

`python -m telemetry_collector.huggingface_check` runs in production as a
Cloud Run job. Once a day at 06:17 UTC (`17 6 * * *`), the workflow
`.github/workflows/huggingface-check.yaml` starts it and keeps a GitHub issue
for each problem it finds, with `python -m telemetry_collector.huggingface_issues`.
It reads Hugging Face with production's read token, the one production's
worker job mounts, and runs two checks:

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
   - every file in a listed build's folder is in the `artifacts` map of that
     build's `release_manifest.json` and has the SHA-256 the map records.
     `manifest_file_digests` reads each entry's `path`, the file's bare name
     in the build folder, and its `sha256`. Files stored with LFS are
     compared by the SHA-256 Hugging Face records for their content, without
     a download; other files are downloaded and hashed. Three files of a
     build folder need no entry: the release manifest itself, and the
     `build_manifest.json` and `sha256sums.txt` that Microcosm's publisher
     writes beside it.

   The rules that compare one write with the state before it (existing build
   entries change only `withdrawn`; nothing inside an existing build folder
   is added, changed or removed) need that earlier state, so only the release
   writer applies them. The file check above finds any later change to a
   build's files except a change to those three.

**Problems and issues.** Each problem is one JSON log entry with severity
`ERROR`, naming the check, the dataset and a code such as `HTTP_404`,
`UNREACHABLE`, `POINTER_FILE_PRESENT` or `SHA256_MISMATCH`, followed by a
summary entry. The job then exits 1, or 2 when the token setting itself is
missing or unusable, so the execution fails. No log entry includes the token.

The workflow starts an execution, waits for it to finish, and reads that
execution's log entries. It keeps one GitHub issue per problem, keyed by its
check, dataset and code, each with the `huggingface-check` label:

- a run that finds a new problem opens its issue (at most 10 a run; the next
  run opens the rest);
- a run that finds it again updates the open issue with what it logged;
- a run that finds a problem whose issue was closed reopens it, with a
  comment, unless the issue was closed as not planned;
- a completed run that no longer finds a problem closes its issue, with a
  comment, but only if that run finished the check that found it: a
  dataset's `releases.json` check that stopped (at a read error, a missing or
  unreadable record, a failed token check, or without the rules package)
  leaves its issues as they are, and so does a check that could not compare
  a listed build's files with its manifest. The check's summary entry lists
  the datasets whose `releases.json` check ran to its end. An issue for a
  check that no longer runs (it was switched off for its dataset, or the
  dataset is no longer checked), or for a dataset with a build whose files
  can never be compared again (its manifest is gone, and build folders never
  change), stays open until someone closes it.

A run that reports no result opens or updates one issue titled "The check did
not report a result" instead, and leaves the problem issues as they are,
since what they report is then unknown. That covers a job that could not
start, failed before writing its summary entry, did not finish within 45
minutes or could not read its token setting, and entries that could not be
read, or fewer problem entries than the summary counts. A run that fails
before the command finishes (it could not authenticate, a setting is missing,
or the command failed or ran out of time) is reported the same way by the
workflow's last step, which needs only GitHub's own token; a failed checkout
or dependency install leaves only GitHub's own failure email. This issue reopens
even if someone closed it as not planned. The next run that reports a result
closes it. GitHub notifies the repo's watchers of each new issue, and the
workflow run's summary lists what each run did.

A run by hand ("Run workflow" on the Actions tab) is a dry run unless its
`dry_run` box is cleared: it runs the check and reports what it would change,
without changing any issue. The repo is public, so no issue names the
project, region, job, execution or account, and the workflow passes those as
secrets, which GitHub hides in its log. (The production deploy's log already
shows the project, region and job name, which are variables there.) If GitHub
stops starting the schedule, nothing reports it: GitHub disables scheduled
workflows in a public repo after 60 days without activity in it, and emails a
failed scheduled run only to whoever last changed its schedule.

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

Staging has no check job, so staging's own read token, which only staging's
worker job mounts, is not checked; production's job checks production's
token, once a day. The deployment service account needs metadata read access
to the secret and permission to act as the worker service account.

**The workflow's access.** The workflow runs in the
`huggingface-check` environment, which only `main` may use, and reads five
secrets from it. As
secrets, GitHub hides their values in the workflow's public log:

| Secret | Value |
|---|---|
| `TELEMETRY_GCP_WIF_PROVIDER` | the provider of the check's own Workload Identity pool, which accepts only this repo's `main` in the `huggingface-check` environment |
| `TELEMETRY_HF_CHECK_SERVICE_ACCOUNT` | the account the workflow acts as, which may only start the check job, follow its executions and read the project's logs |
| `TELEMETRY_GCP_PROJECT_ID` | the collector's project |
| `TELEMETRY_GCP_REGION` | the check job's region |
| `TELEMETRY_HF_CHECK_JOB` | the check job's name, as in production's `TELEMETRY_HF_CHECK_JOB` |

The workflow's account trusts only that pool, and the deployment account
does not trust it, so the deploy's access is unchanged ("Google Cloud access").
The deploy's own provider must check the environment before the workflow
first runs, with the condition
`assertion.repository=='PolicyEngine/microcosm-collector' && assertion.ref=='refs/heads/main' && assertion.environment in ['staging', 'production']`.
Without that clause, the check job's token would also pass the deploy's
provider, and the deployment account trusts every token of this repo that it
accepts.
The environment, its secrets, the pool, the account and its access are created
once by hand; the deploy does not change them. The workflow's `GITHUB_TOKEN` opens and
changes the issues, so it needs no other GitHub credential.

**Switching on the `releases.json` check.** It is off for every dataset. For
a dataset, it can be switched on once:

1. the microcosm-emitter-interface package, with
   `microcosm_emitter_interface.releases` (PolicyEngine/microcosm-emitter#24),
   is published and added to this collector's dependencies;
2. Microcosm publishes the dataset's builds into their folders with the
   release manifest decided on 2026-10-29 (H2c): today's `artifacts` map,
   each entry with `kind`, `role`, `repo_id`, `sha256` and a bare `path`,
   and no `revision`;
3. the dataset publishes builds side by side with `releases.json`, and its
   `latest*.json` files are deleted. Before that, the check would report them
   every day;
4. every file in each listed build's folder is in that build's `artifacts`
   map, apart from the manifests and the checksum list. Otherwise the check
   reports each other file as `FILE_NOT_IN_MANIFEST` every day, in an issue
   that can't close, because build folders never change.

`policyengine/populace-uk-private` and `policyengine/populace-us` meet the
second and third since their switches on 2026-11-09 and 2026-11-11.
`policyengine/populace-us` doesn't meet the fourth yet. Each of its three
`microcosm-us-2024-local` builds holds six files that its checksum list
records but its manifest doesn't declare, which the switch copied as they
were. So either the check must also accept files a build's checksum list
records, or those builds must stay unchecked.

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
environment's seven secrets exist (the runtime database URL, the migration
database URL, the HS256 JWT secret, the ES256 signing key, the dashboard's
read token, the Hugging Face read token and the calibration tree Blob token),
and that its worker and recovery jobs exist and belong to that environment
(see "Worker jobs and recovery"). It does not access the secrets' payloads or
test their values. The
previous HF authentication and write/read deployment qualification is removed.

Each deployment first routes traffic to a verified maintenance revision that
returns HTTP 503 for data operations, then waits for prior requests to finish.
Production also creates a completed Cloud SQL backup before its migration. The
workflow runs the image as a one-task Cloud Run job to apply and verify Alembic
migrations, deploys a candidate without stable traffic, checks the candidate's
`/ready` endpoint, and only then directs traffic to the candidate. Last, it
deploys the Orrery read service, a separate Cloud Run service from the same
image (see "Serving Orrery graph files"). Once a production migration begins, failures leave the maintenance revision serving
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
Hugging Face authentication. The Orrery read service, a separate service,
allows no unauthenticated calls: its IAM policy grants the Cloud Run Invoker
role only to microcosm-runs' own service account. Anyone granted a role that
includes `run.routes.invoke` on the project, its folder or the organization,
or who may make identity tokens as that account, can call it too (see "Who
else can call it" under "Serving Orrery graph files").

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
credentials.

Two other jobs request an OIDC token, both to publish to npm in the `npm`
environment: `publish-client`, which publishes the read API client ("The
TypeScript client"), and the `publish` job of
`publish-calibration-tree.yaml`, which publishes the calibration tree package
("Publishing to npm"). The condition above does not check the environment, so
a token from either job would pass it too. Before either is switched on, the
condition must become:

```
assertion.repository=='PolicyEngine/microcosm-collector' && assertion.ref=='refs/heads/main' && assertion.environment in ['staging', 'production']
```

The deployment jobs run in those two environments, so they keep their access,
and a token from a job in any other environment, or in none, is refused. From
then on, every job that authenticates through this provider must name one of
the two.

The daily Hugging Face check's job also requests an OIDC token, in the
`huggingface-check` environment. It authenticates through a Workload Identity
pool and provider of its own, whose condition accepts only this repo's `main`
in that environment, and acts as an account that trusts only that pool. Once
the deploy's provider checks the environment, as above, it refuses that token, and
the deployment account does not trust the check's pool, so the check cannot
act as the deployment account. That clause must be in place before the
check's workflow first runs ("Daily Hugging Face check").

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
  secret (see "Artifact intake"), and
  `TELEMETRY_CALIBRATION_TREE_BLOB_SECRET_NAME` for its own calibration tree
  Blob store's token's secret (see "Building trees from submissions"), and
  `TELEMETRY_ORRERY_BUCKET` for its own private Orrery bucket (see
  "Publishing Orrery graph files"), and
  `TELEMETRY_ORRERY_READER_SERVICE_ACCOUNT` for the service account of its
  own microcosm-runs deployments, the only account its Orrery read
  service's IAM policy grants the Cloud Run Invoker role (see "Serving
  Orrery graph files").
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
