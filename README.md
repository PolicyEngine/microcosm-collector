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

The dashboard query endpoints require `X-Telemetry-Read-Token`. This credential
is configured only in the dashboard server environment and is never returned
to the browser.

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

## Cloud Run deployment

The production service is isolated in this top-level directory. Its Docker
build context is `telemetry-service/`, so dashboard source files cannot enter
the image. Cloud Run assigns the production service address. Microcosm fixes
that origin in its delivery service; the address is intentionally not repeated
in documentation.

Infrastructure was provisioned once with disposable scripts kept outside the
repository. The repository intentionally contains no bootstrap script or
mutable infrastructure template.

`.github/workflows/telemetry-service.yml` runs only when this directory or the
workflow itself changes. Pull requests run tests, Ruff, and a container build.
After a change reaches `main`, the workflow authenticates to Google Cloud with
GitHub OIDC and pushes an image identified by the commit SHA. It first routes
traffic to a verified maintenance revision that returns HTTP 503 for data
operations, then waits for prior requests to finish. It runs that exact image
as a one-task Cloud Run job to apply Alembic migrations, deploys a candidate
without production traffic, checks the candidate's `/ready` endpoint, and only
then routes traffic to the candidate. Once migration begins, failures leave the
maintenance revision serving retryable responses; the workflow does not restore
schema-incompatible application code. Manual production deployment runs are
accepted only from `main`.

The workflow reads its Google Cloud identifiers from the GitHub `Production`
environment. Runtime secrets remain in Google Secret Manager. The dashboard
deployment receives `MICROCOSM_TELEMETRY_COLLECTOR_URL` and the independent
`MICROCOSM_TELEMETRY_COLLECTOR_READ_TOKEN` from the same GitHub environment.
Public ingress lets developer machines reach the service; ingestion still
requires a short-lived collector credential issued after Hugging Face
authentication.

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
requires a database connection and the current Alembic revision.
