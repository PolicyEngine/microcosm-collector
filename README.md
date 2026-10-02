# Microcosm telemetry collector

This service accepts live build telemetry from Microcosm and exposes a
read-only API to the calibration dashboard. It does not store dataset artifacts
or Hugging Face credentials.

## Authentication

Microcosm sends its ambient Hugging Face token only to
`POST /v1/auth/huggingface/exchange`. The collector validates that opaque token
against Hugging Face's `whoami-v2` endpoint and confirms current membership in
the `policyengine` organization. It then returns a 15-minute JWT restricted to
one run and one producer process. The raw Hugging Face token is neither logged
nor written to Postgres.

The dashboard query endpoints require `X-Telemetry-Read-Token`. This credential
is configured only in the dashboard server environment and is never returned
to the browser.

## Local development

```bash
uv sync --dev
export DATABASE_URL=postgresql://...
export TELEMETRY_JWT_SECRET=...
export TELEMETRY_READ_TOKEN=...
uv run uvicorn telemetry_collector.app:create_app_from_environment \
  --factory --host 127.0.0.1 --port 8080 --no-access-log
```

Use at least 32 random characters for `TELEMETRY_JWT_SECRET` and at least 24
for `TELEMETRY_READ_TOKEN`. Point a Microcosm checkout at a local instance with
`MICROCOSM_TELEMETRY_COLLECTOR_URL=http://localhost:8080`.

## Cloud Run deployment

The production service is isolated in this top-level directory. Its Docker
build context is `telemetry-service/`, so dashboard source files cannot enter
the image. The service is available at the native Cloud Run address:

`https://microcosm-telemetry-389282473430.us-central1.run.app`

Infrastructure was provisioned once with disposable scripts kept outside the
repository. The repository intentionally contains no bootstrap script or
mutable infrastructure template.

`.github/workflows/telemetry-service.yml` runs only when this directory or the
workflow itself changes. Pull requests run tests, Ruff, and a container build.
After a change reaches `main`, the workflow authenticates to Google Cloud with
GitHub OIDC, pushes an image identified by the commit SHA, deploys it without
production traffic, checks its revision-specific `/health` endpoint, and then
routes production traffic to that exact revision. If the stable-address check
fails, it restores the previous revision.

The workflow reads its Google Cloud identifiers from the GitHub `Production`
environment. Runtime secrets remain in Google Secret Manager. The dashboard
deployment receives `MICROCOSM_TELEMETRY_COLLECTOR_URL` and the independent
`MICROCOSM_TELEMETRY_COLLECTOR_READ_TOKEN` from the same GitHub environment.
Public ingress lets developer machines reach the service; ingestion still
requires a short-lived run credential issued after Hugging Face authentication.

Restrict the Cloud SQL instance to the Cloud Run connection and disable request
access logs for the container so authorization headers cannot enter application
logs. Cloud Run and the HTTPS load balancer may retain request metadata, but the
service never includes credentials in paths, query strings, bodies, or errors.

The service creates its two tables on startup using
`telemetry_collector/migrations/001_initial.sql`. For production schema changes,
add a new idempotent migration instead of editing the initial migration.
