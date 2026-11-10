"""FastAPI application for authenticated Microcosm telemetry ingestion."""

from __future__ import annotations

import base64
import binascii
import hmac
import json
import os
from collections.abc import Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Path,
    Query,
    Response,
    Security,
    status,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader
from jwt import InvalidTokenError
from pydantic import ValidationError
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from telemetry_collector.auth import (
    HuggingFaceAuthenticationError,
    HuggingFaceAuthenticationUnavailableError,
    HuggingFaceAuthenticator,
    HuggingFacePrincipal,
)
from telemetry_collector.intake import IntakeServices, IntakeSettings
from telemetry_collector.intake_api import (
    IntakeProblemError,
    intake_not_enabled,
    intake_not_enabled_router,
    intake_problem_response,
    submission_router,
)
from telemetry_collector.models import (
    SAFE_IDENTIFIER,
    CollectorToken,
    EventBatch,
    PageCursor,
    RunRegistration,
    SessionScope,
    TokenExchangeRequest,
    require_telemetry_scope_for_lifetime,
)
from telemetry_collector.orrery_access import GoogleIdentityTokenVerifier
from telemetry_collector.orrery_graph_api import (
    PrivateNoStoreMiddleware,
    orrery_graph_router,
    orrery_graphs_not_enabled_router,
)
from telemetry_collector.orrery_serving import (
    OrreryServingServices,
    OrreryServingSettings,
    PostgresOrreryGraphCatalogue,
)
from telemetry_collector.read_api_v2 import (
    ErrorMessage,
    RunDetail,
    RunPage,
    openapi_document,
    run_detail,
)
from telemetry_collector.session_tokens import (
    SIGNING_KEY_VARIABLE,
    SessionSigningKey,
    json_web_key_set,
    sign_session_token,
    verify_session_token,
)
from telemetry_collector.storage import (
    PostgresTelemetryStore,
    RunRegistrationConflictError,
    TelemetryStore,
)


@dataclass(frozen=True)
class CollectorSettings:
    """Secrets and policy read once when the collector starts."""

    # The HS256 secret. It signs new session tokens only while no signing key
    # is configured, and verifies HS256 tokens for as long as it is configured.
    jwt_secret: str | None
    read_token: str
    # The ES256 key that signs new session tokens once it is configured.
    signing_key: SessionSigningKey | None = None
    required_huggingface_org: str = "policyengine"
    collector_issuer: str = "policyengine-telemetry"
    collector_audience: str = "microcosm-telemetry-collector"
    session_token_ttl_seconds: int = 60 * 60
    maintenance_mode: bool = False
    # None while artifact intake is switched off (the default).
    intake: IntakeSettings | None = None
    # None while serving Orrery graph files is switched off (the default).
    orrery_serving: OrreryServingSettings | None = None

    @classmethod
    def from_environment(cls) -> CollectorSettings:
        jwt_secret = _optional_secret_environment("TELEMETRY_JWT_SECRET")
        signing_key_pem = _optional_secret_environment(SIGNING_KEY_VARIABLE)
        read_token = _required_environment("TELEMETRY_READ_TOKEN")
        if jwt_secret is None and signing_key_pem is None:
            raise RuntimeError(
                f"{SIGNING_KEY_VARIABLE} or TELEMETRY_JWT_SECRET is required."
            )
        if jwt_secret is not None and len(jwt_secret) < 32:
            raise RuntimeError(
                "TELEMETRY_JWT_SECRET must contain at least 32 characters."
            )
        if len(read_token) < 24:
            raise RuntimeError(
                "TELEMETRY_READ_TOKEN must contain at least 24 characters."
            )
        try:
            signing_key = (
                None
                if signing_key_pem is None
                else SessionSigningKey.from_pem(signing_key_pem)
            )
        except ValueError as error:
            raise RuntimeError(str(error)) from None
        return cls(
            jwt_secret=jwt_secret,
            read_token=read_token,
            signing_key=signing_key,
            required_huggingface_org=os.environ.get("TELEMETRY_HF_ORG", "policyengine"),
            collector_issuer=os.environ.get(
                "TELEMETRY_JWT_ISSUER", "policyengine-telemetry"
            ),
            collector_audience=os.environ.get(
                "TELEMETRY_JWT_AUDIENCE", "microcosm-telemetry-collector"
            ),
            session_token_ttl_seconds=int(
                os.environ.get("TELEMETRY_SESSION_TOKEN_TTL_SECONDS", "3600")
            ),
            maintenance_mode=os.environ.get("TELEMETRY_MAINTENANCE_MODE") == "1",
            intake=IntakeSettings.from_environment(),
            orrery_serving=OrreryServingSettings.from_environment(),
        )

    def __post_init__(self) -> None:
        if self.signing_key is None and self.jwt_secret is None:
            raise ValueError("Session tokens need a signing key or an HS256 secret.")


@dataclass(frozen=True)
class SessionTokenClaims:
    """PolicyEngine identity carried by an expiring collector token."""

    subject: str
    username: str
    organizations: tuple[str, ...]


class RequestBodyLimitMiddleware:
    """Reject oversized fixed-length and streamed request bodies."""

    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        declared = headers.get(b"content-length")
        if declared is not None:
            try:
                too_large = int(declared) > self.max_bytes
            except ValueError:
                too_large = True
            if too_large:
                await self._reject(scope, receive, send)
                return

        body = bytearray()
        more_body = True
        while more_body:
            message = await receive()
            if message["type"] != "http.request":
                await self.app(scope, receive, send)
                return
            body.extend(message.get("body", b""))
            if len(body) > self.max_bytes:
                await self._reject(scope, receive, send)
                return
            more_body = bool(message.get("more_body", False))

        replayed = False

        async def replay_receive() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {
                    "type": "http.request",
                    "body": bytes(body),
                    "more_body": False,
                }
            return await receive()

        await self.app(scope, replay_receive, send)

    @staticmethod
    async def _reject(scope: Scope, receive: Receive, send: Send) -> None:
        response = JSONResponse(
            status_code=413,
            content={"detail": "Request body exceeds 1 MiB."},
        )
        await response(scope, receive, send)


class MaintenanceModeMiddleware:
    """Keep health checks available while rejecting all data operations."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if scope["type"] != "http" or scope.get("path") == "/health":
            await self.app(scope, receive, send)
            return
        response = JSONResponse(
            status_code=503,
            content={"detail": "Telemetry service is temporarily unavailable."},
            headers={"Retry-After": "60"},
        )
        await response(scope, receive, send)


def _required_environment(name: str) -> str:
    value = _optional_environment(name)
    if value is None:
        raise RuntimeError(f"{name} is required.")
    return value


def _optional_environment(name: str) -> str | None:
    return os.environ.get(name, "").strip() or None


def _optional_secret_environment(name: str) -> str | None:
    """A secret that may be absent, but that stops startup when set and blank.

    Deployment mounts each one from Secret Manager, so a blank value means a
    mistaken secret version rather than a secret that isn't configured.
    """

    if name not in os.environ:
        return None
    value = _optional_environment(name)
    if value is None:
        raise RuntimeError(f"{name} is set but empty.")
    return value


def _bearer_token(value: str | None) -> str:
    if not value:
        raise HTTPException(
            status_code=401, detail="Bearer authentication is required."
        )
    scheme, separator, token = value.partition(" ")
    if separator != " " or scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(
            status_code=401, detail="Bearer authentication is required."
        )
    return token.strip()


def _page_cursor(registered_at: datetime, run_id: str) -> str:
    payload = json.dumps(
        [registered_at.isoformat(), run_id], separators=(",", ":")
    ).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


class InvalidPageCursorError(ValueError):
    """Raised for a pagination cursor that the collector did not issue."""


INVALID_PAGE_CURSOR = "Pagination cursor is invalid."


def _decode_page_cursor(value: str) -> tuple[datetime, str]:
    try:
        padded = value + "=" * (-len(value) % 4)
        raw = base64.b64decode(padded, altchars=b"-_", validate=True)
        decoded = json.loads(raw.decode())
        timestamp, run_id = PageCursor.model_validate(decoded).root
    except (
        ValueError,
        TypeError,
        UnicodeDecodeError,
        binascii.Error,
        ValidationError,
    ) as error:
        raise InvalidPageCursorError(INVALID_PAGE_CURSOR) from error
    return timestamp, run_id


def _parse_page_cursor(value: str | None) -> tuple[datetime, str] | None:
    if value is None:
        return None
    try:
        return _decode_page_cursor(value)
    except InvalidPageCursorError as error:
        raise HTTPException(status_code=422, detail=INVALID_PAGE_CURSOR) from error


def _parse_page_cursor_v2(value: str | None) -> tuple[datetime, str] | None:
    """Parse a cursor, refusing a bad one as FastAPI refuses other parameters."""

    if value is None:
        return None
    try:
        return _decode_page_cursor(value)
    except InvalidPageCursorError as error:
        raise RequestValidationError(
            [
                {
                    "type": "value_error",
                    "loc": ("query", "before"),
                    "msg": INVALID_PAGE_CURSOR,
                    "input": value,
                }
            ]
        ) from error


READ_TOKEN_HEADER = APIKeyHeader(
    name="X-Telemetry-Read-Token",
    scheme_name="ReadToken",
    description="The dashboard read credential.",
    auto_error=False,
)

V2_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    401: {
        "model": ErrorMessage,
        "description": "The read credential is missing or wrong.",
    },
    503: {
        "model": ErrorMessage,
        "description": (
            "A deployment is in progress; retry after the `Retry-After` header's "
            "seconds."
        ),
    },
}


SCOPE_REFUSALS: dict[str, str] = {
    "telemetry:write": "Collector credential cannot write telemetry.",
    "artifacts:submit": "Collector credential cannot submit artifacts.",
}


# The longest lifetime granted to a token issued with lifetime_seconds. It is a
# code constant, so no deployment setting can lengthen these tokens.
MAXIMUM_DELEGATED_LIFETIME_SECONDS = 24 * 60 * 60


def _issue_session_token(
    settings: CollectorSettings,
    principal: HuggingFacePrincipal,
    scope: SessionScope = "telemetry:write",
    lifetime_seconds: int | None = None,
) -> CollectorToken:
    # The request model checks this at the exchange; checking it here as well
    # covers every other caller.
    require_telemetry_scope_for_lifetime(scope, lifetime_seconds)
    # A requested lifetime means the token is for a build on another machine,
    # which cannot exchange again. Such a token carries "delegated", so a log
    # of its claims can tell it apart and an endpoint can refuse it.
    granted_seconds = (
        settings.session_token_ttl_seconds
        if lifetime_seconds is None
        else min(lifetime_seconds, MAXIMUM_DELEGATED_LIFETIME_SECONDS)
    )
    delegation = {} if lifetime_seconds is None else {"delegated": True}
    now = datetime.now(UTC)
    expires = now + timedelta(seconds=granted_seconds)
    encoded = sign_session_token(
        {
            "iss": settings.collector_issuer,
            "aud": settings.collector_audience,
            "sub": principal.user_id,
            "username": principal.username,
            "organizations": list(principal.organizations),
            "scope": scope,
            "iat": now,
            "exp": expires,
            **delegation,
        },
        signing_key=settings.signing_key,
        hs256_secret=settings.jwt_secret,
    )
    return CollectorToken(
        access_token=encoded,
        expires_in=granted_seconds,
        scope=scope,
    )


def _decode_token(
    settings: CollectorSettings, authorization: str | None
) -> dict[str, Any]:
    token = _bearer_token(authorization)
    try:
        return verify_session_token(
            token,
            signing_key=settings.signing_key,
            hs256_secret=settings.jwt_secret,
            issuer=settings.collector_issuer,
            audience=settings.collector_audience,
        )
    except InvalidTokenError as error:
        raise HTTPException(
            status_code=401, detail="Collector credential is invalid."
        ) from error


def _decode_session_token(
    settings: CollectorSettings,
    authorization: str | None,
    required_scope: SessionScope = "telemetry:write",
) -> SessionTokenClaims:
    payload = _decode_token(settings, authorization)
    # Each token carries exactly one scope, and each endpoint accepts one.
    if payload.get("scope") != required_scope:
        raise HTTPException(
            status_code=403,
            detail=SCOPE_REFUSALS[required_scope],
        )
    subject = payload.get("sub")
    username = payload.get("username")
    organizations = payload.get("organizations")
    if (
        not isinstance(subject, str)
        or not subject
        or not isinstance(username, str)
        or not username
        or not isinstance(organizations, list)
        or not all(isinstance(item, str) and item for item in organizations)
    ):
        raise HTTPException(
            status_code=401, detail="Collector credential is incomplete."
        )
    return SessionTokenClaims(
        subject=subject,
        username=username,
        organizations=tuple(organizations),
    )


def create_app(
    *,
    settings: CollectorSettings,
    store: TelemetryStore,
    huggingface_authenticator: HuggingFaceAuthenticator,
    intake_services: IntakeServices | None = None,
    orrery_services: OrreryServingServices | None = None,
) -> FastAPI:
    """Create the collector with explicit dependencies for tests and deployment.

    The submission endpoints exist only when ``settings.intake`` is set, and
    the Orrery graph endpoints only when ``settings.orrery_serving`` is set.
    """

    if settings.intake is not None and intake_services is None:
        raise ValueError("Intake is switched on but its services were not provided.")
    if settings.orrery_serving is not None and orrery_services is None:
        raise ValueError(
            "Serving Orrery graph files is switched on but its services were "
            "not provided."
        )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> Iterator[None]:
        try:
            yield
        finally:
            close = getattr(store, "close", None)
            if callable(close):
                close()

    application = FastAPI(
        title="PolicyEngine Microcosm telemetry collector",
        version="1.0.0",
        lifespan=lifespan,
    )

    application.add_middleware(RequestBodyLimitMiddleware, max_bytes=1_048_576)
    if settings.maintenance_mode:
        application.add_middleware(MaintenanceModeMiddleware)
    # Outermost, so that maintenance mode's answers are marked too.
    application.add_middleware(PrivateNoStoreMiddleware)

    def require_read_token(
        token: Annotated[str | None, Header(alias="X-Telemetry-Read-Token")] = None,
    ) -> None:
        # Compared as UTF-8 bytes: compare_digest refuses strings with
        # characters above ASCII, which a Latin-1 decoded header can hold.
        if token is None or not hmac.compare_digest(
            token.encode("utf-8"), settings.read_token.encode("utf-8")
        ):
            raise HTTPException(
                status_code=401,
                detail="A valid dashboard read credential is required.",
            )

    # The same credential for read API v2, declared as a security scheme so
    # that its OpenAPI document and typed client describe it.
    def require_read_token_v2(
        token: Annotated[str | None, Security(READ_TOKEN_HEADER)] = None,
    ) -> None:
        require_read_token(token)

    def session_claims(
        authorization: Annotated[str | None, Header()] = None,
    ) -> SessionTokenClaims:
        return _decode_session_token(settings, authorization)

    def submitter_claims(
        authorization: Annotated[str | None, Header()] = None,
    ) -> SessionTokenClaims:
        return _decode_session_token(settings, authorization, "artifacts:submit")

    application.add_exception_handler(IntakeProblemError, intake_problem_response)
    if settings.intake is not None and intake_services is not None:
        application.include_router(
            submission_router(
                services=intake_services, submitter_claims=submitter_claims
            )
        )
    else:
        application.include_router(intake_not_enabled_router())
    if settings.orrery_serving is not None and orrery_services is not None:
        application.include_router(
            orrery_graph_router(
                services=orrery_services,
                reader_service_account=settings.orrery_serving.reader_service_account,
            )
        )
    else:
        application.include_router(orrery_graphs_not_enabled_router())

    @application.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/ready")
    def ready() -> dict[str, str]:
        if not store.is_ready():
            raise HTTPException(
                status_code=503,
                detail="Telemetry database is unavailable or not fully migrated.",
            )
        return {"status": "ok"}

    @application.get("/.well-known/jwks.json")
    def session_token_key_set(response: Response) -> dict[str, Any]:
        """Public keys that verify session tokens, by their ``kid``."""

        response.headers["Cache-Control"] = "public, max-age=300"
        return json_web_key_set(settings.signing_key)

    @application.post(
        "/v1/auth/huggingface/exchange",
        response_model=CollectorToken,
    )
    def exchange_huggingface_token(
        exchange_request: TokenExchangeRequest | None = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> CollectorToken:
        opaque_token = _bearer_token(authorization)
        token_request = exchange_request or TokenExchangeRequest()
        scope = token_request.scope
        if scope == "artifacts:submit" and settings.intake is None:
            raise intake_not_enabled()
        try:
            principal = huggingface_authenticator.authenticate(
                opaque_token,
                settings.required_huggingface_org,
            )
        except HuggingFaceAuthenticationUnavailableError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        except HuggingFaceAuthenticationError as error:
            raise HTTPException(status_code=401, detail=str(error)) from error
        if settings.required_huggingface_org not in principal.organizations:
            raise HTTPException(
                status_code=403,
                detail="The Hugging Face user is not a PolicyEngine organization member.",
            )
        return _issue_session_token(
            settings, principal, scope, token_request.lifetime_seconds
        )

    @application.post("/v1/runs", status_code=status.HTTP_201_CREATED)
    def register_run(
        registration: RunRegistration,
        claims: SessionTokenClaims = Depends(session_claims),
    ) -> dict[str, bool]:
        principal = HuggingFacePrincipal(
            user_id=claims.subject,
            username=claims.username,
            organizations=claims.organizations,
        )
        try:
            store.register_run(registration, principal)
        except PermissionError as error:
            raise HTTPException(status_code=403, detail=str(error)) from error
        except RunRegistrationConflictError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return {"registered": True}

    @application.post(
        "/v1/runs/{run_id}/events",
        status_code=status.HTTP_202_ACCEPTED,
    )
    def ingest_events(
        run_id: str,
        batch: EventBatch,
        claims: SessionTokenClaims = Depends(session_claims),
    ) -> dict[str, int]:
        for event in batch.events:
            if event.run_id != run_id:
                raise HTTPException(
                    status_code=403,
                    detail="Event identity does not match the request path.",
                )
        try:
            accepted, duplicates = store.append_events(
                run_id, claims.subject, batch.events
            )
        except PermissionError as error:
            raise HTTPException(status_code=403, detail=str(error)) from error
        except KeyError as error:
            raise HTTPException(
                status_code=404, detail="Run is not registered."
            ) from error
        return {"accepted": accepted, "duplicates": duplicates}

    @application.get("/v1/runs", dependencies=[Depends(require_read_token)])
    def list_runs(
        country: Annotated[str | None, Query(pattern=r"^[A-Z]{2}$")] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 60,
        before: str | None = None,
    ) -> dict[str, Any]:
        runs = store.list_runs(
            country=country,
            limit=limit,
            before=_parse_page_cursor(before),
        )
        next_before = (
            _page_cursor(runs[-1]["registered_at"], runs[-1]["run_id"])
            if len(runs) == limit
            else None
        )
        return {"runs": runs, "next_before": next_before}

    @application.get("/v1/runs/{run_id}", dependencies=[Depends(require_read_token)])
    def get_run(run_id: str) -> dict[str, Any]:
        run = store.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Run was not found.")
        return run

    @application.get(
        "/v2/runs",
        response_model=RunPage,
        dependencies=[Depends(require_read_token_v2)],
        responses=V2_ERROR_RESPONSES,
        operation_id="listRuns",
        summary="List runs, newest registration first",
        tags=["read API v2"],
    )
    def list_runs_v2(
        country: Annotated[
            str | None,
            Query(
                pattern=r"^[A-Z]{2}$",
                description="Only runs of this country, such as `US` or `GB`.",
            ),
        ] = None,
        limit: Annotated[
            int, Query(ge=1, le=200, description="The most runs to return.")
        ] = 60,
        before: Annotated[
            str | None,
            Query(description="The previous page's `next_before`."),
        ] = None,
    ) -> RunPage:
        runs = store.list_runs(
            country=country,
            limit=limit,
            before=_parse_page_cursor_v2(before),
        )
        next_before = (
            _page_cursor(runs[-1]["registered_at"], runs[-1]["run_id"])
            if len(runs) == limit
            else None
        )
        return RunPage.model_validate({"runs": runs, "next_before": next_before})

    @application.get(
        "/v2/runs/{run_id}",
        response_model=RunDetail,
        dependencies=[Depends(require_read_token_v2)],
        responses={
            **V2_ERROR_RESPONSES,
            404: {"model": ErrorMessage, "description": "The run was not found."},
        },
        operation_id="getRun",
        summary="A run's state and the events it was rebuilt from",
        tags=["read API v2"],
    )
    def get_run_v2(
        run_id: Annotated[str, Path(pattern=SAFE_IDENTIFIER)],
    ) -> RunDetail:
        record = store.get_run_record(run_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Run was not found.")
        return run_detail(record.registered_run, record.events)

    @application.get("/v2/openapi.json", include_in_schema=False)
    def read_api_v2_openapi() -> dict[str, Any]:
        return openapi_document(application)

    return application


def create_app_from_environment() -> FastAPI:
    """Create the production application from Cloud Run environment values."""

    settings = CollectorSettings.from_environment()
    store = PostgresTelemetryStore(_required_environment("DATABASE_URL"))
    intake_services = None
    if settings.intake is not None:
        # Imported only when intake is on, so a collector with intake off
        # never loads the Google Cloud clients or looks for credentials.
        from telemetry_collector.google_cloud import (
            CloudTasksWorkerQueue,
            GoogleCloudStorageIntake,
        )
        from telemetry_collector.submission_store import PostgresSubmissionStore

        intake_services = IntakeServices(
            submissions=PostgresSubmissionStore(store.engine),
            objects=GoogleCloudStorageIntake(
                bucket_name=settings.intake.bucket,
                signing_service_account=settings.intake.signing_service_account,
            ),
            worker_queue=CloudTasksWorkerQueue(
                queue=settings.intake.worker_queue,
                worker_job=settings.intake.worker_job,
                invoker_service_account=(
                    settings.intake.worker_invoker_service_account
                ),
            ),
        )
    orrery_services = None
    if settings.orrery_serving is not None:
        # Imported only when serving is on, as for intake above.
        from telemetry_collector.google_cloud import (
            GoogleCloudStorageOrreryGraphReader,
        )

        orrery_services = OrreryServingServices(
            catalogue=PostgresOrreryGraphCatalogue(store.engine),
            objects=GoogleCloudStorageOrreryGraphReader(
                bucket_name=settings.orrery_serving.bucket
            ),
            verifier=GoogleIdentityTokenVerifier(),
        )
    return create_app(
        settings=settings,
        store=store,
        huggingface_authenticator=HuggingFaceAuthenticator(),
        intake_services=intake_services,
        orrery_services=orrery_services,
    )
