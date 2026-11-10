"""The Orrery read service, which serves Orrery graph files to microcosm-runs.

The Orrery read service is a separate Cloud Run service, run from the
collector's image (``scripts/deploy.sh deploy-orrery-read-service``). It
mounts only the Orrery graph routes (``telemetry_collector.orrery_graph_api``),
``GET /v1/orrery-graphs`` and
``GET /v1/orrery-graphs/{submission_id}/{document_sha256}/{file_name}``, and
reads the private Orrery bucket and the collector's database as the
collector's runtime account. The collector's main service mounts neither
route.

**Cloud Run's caller check is the read service's only access control.** The
service allows no unauthenticated calls, and its IAM policy grants the Cloud
Run Invoker role to one member, microcosm-runs' service account, so Cloud Run
checks each request's Google-signed identity token, for the service's
address or the custom audience ``ORRERY_GRAPHS_AUDIENCE``, and refuses the
request with 401 or 403 before it reaches this code. This code verifies no
token and reads no credential. The service must therefore never allow
unauthenticated calls, and its invoker check must never be switched off:
either would serve every file, licensed ones included, to anyone.

Its settings are ``COLLECTOR_ORRERY_BUCKET`` and ``DATABASE_URL``
(``OrreryServingSettings``), and it refuses to start without either. Every
answer has ``Cache-Control: private, no-store``. It has no other route, no
OpenAPI document and no health route: Cloud Run's startup probe checks that
it listens on its port.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI

from telemetry_collector import google_cloud
from telemetry_collector.database import create_database_engine
from telemetry_collector.intake_api import IntakeProblemError, intake_problem_response
from telemetry_collector.orrery_graph_api import (
    PrivateNoStoreMiddleware,
    orrery_graph_router,
)
from telemetry_collector.orrery_serving import (
    OrreryServingServices,
    OrreryServingSettings,
    PostgresOrreryGraphCatalogue,
)

#: The custom audience the read service accepts identity tokens for, beside
#: its own address. microcosm-runs requests its tokens for it; the deploy
#: sets it as the service's only custom audience.
ORRERY_GRAPHS_AUDIENCE = "microcosm-collector/orrery-graphs"
#: The read service's request timeout, which the deploy sets (``--timeout``).
#: A fetch of a 64 MiB file, the largest a document or an upstream file may
#: be, reads it twice, in about 130 ranged reads of 1 MiB one after another,
#: and sends it to microcosm-runs during the second read. That takes some
#: seconds; 300 seconds leaves room for a slow bucket and a slow client, and
#: each storage call's own limits stay under a quarter of it
#: (``google_cloud.ORRERY_STORAGE_TIMEOUT_SECONDS`` and
#: ``ORRERY_STORAGE_RETRY_SECONDS``).
REQUEST_TIMEOUT_SECONDS = 300


def create_orrery_read_app(
    *,
    services: OrreryServingServices,
    close: Callable[[], None] | None = None,
) -> FastAPI:
    """Create the read service over explicit services, for tests and
    deployment. ``close`` runs when the server shuts down."""

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            if close is not None:
                close()

    application = FastAPI(
        title="PolicyEngine Microcosm Orrery read service",
        version="1.0.0",
        lifespan=lifespan,
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
    )
    application.add_middleware(PrivateNoStoreMiddleware)
    application.add_exception_handler(IntakeProblemError, intake_problem_response)
    application.include_router(orrery_graph_router(services=services))
    return application


def create_orrery_read_app_from_environment() -> FastAPI:
    """Create the deployed read service from its Cloud Run settings."""

    settings = OrreryServingSettings.from_environment()
    engine = create_database_engine(settings.database_url)
    services = OrreryServingServices(
        catalogue=PostgresOrreryGraphCatalogue(engine),
        objects=google_cloud.GoogleCloudStorageOrreryGraphReader(
            bucket_name=settings.bucket
        ),
    )
    return create_orrery_read_app(services=services, close=engine.dispose)
