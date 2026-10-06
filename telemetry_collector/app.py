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

import jwt
from fastapi import Depends, FastAPI, Header, HTTPException, Query, status
from fastapi.responses import JSONResponse
from jwt import InvalidTokenError
from pydantic import ValidationError
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from telemetry_collector.auth import (
    HuggingFaceAuthenticationError,
    HuggingFaceAuthenticationUnavailableError,
    HuggingFaceAuthenticator,
    HuggingFacePrincipal,
)
from telemetry_collector.models import (
    CollectorToken,
    EventBatch,
    PageCursor,
    RunRegistration,
)
from telemetry_collector.storage import (
    PostgresTelemetryStore,
    RunRegistrationConflictError,
    TelemetryStore,
)


@dataclass(frozen=True)
class CollectorSettings:
    """Secrets and policy read once when the collector starts."""

    jwt_secret: str
    read_token: str
    required_huggingface_org: str = "policyengine"
    collector_issuer: str = "policyengine-telemetry"
    collector_audience: str = "microcosm-telemetry-collector"
    session_token_ttl_seconds: int = 60 * 60
    maintenance_mode: bool = False

    @classmethod
    def from_environment(cls) -> CollectorSettings:
        jwt_secret = _required_environment("TELEMETRY_JWT_SECRET")
        read_token = _required_environment("TELEMETRY_READ_TOKEN")
        if len(jwt_secret) < 32:
            raise RuntimeError(
                "TELEMETRY_JWT_SECRET must contain at least 32 characters."
            )
        if len(read_token) < 24:
            raise RuntimeError(
                "TELEMETRY_READ_TOKEN must contain at least 24 characters."
            )
        return cls(
            jwt_secret=jwt_secret,
            read_token=read_token,
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
        )


@dataclass(frozen=True)
class SessionTokenClaims:
    """PolicyEngine identity carried by a short-lived collector token."""

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
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required.")
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


def _parse_page_cursor(value: str | None) -> tuple[datetime, str] | None:
    if value is None:
        return None
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
        raise HTTPException(
            status_code=422, detail="Pagination cursor is invalid."
        ) from error
    return timestamp, run_id


def _issue_session_token(
    settings: CollectorSettings,
    principal: HuggingFacePrincipal,
) -> CollectorToken:
    now = datetime.now(UTC)
    expires = now + timedelta(seconds=settings.session_token_ttl_seconds)
    encoded = jwt.encode(
        {
            "iss": settings.collector_issuer,
            "aud": settings.collector_audience,
            "sub": principal.user_id,
            "username": principal.username,
            "organizations": list(principal.organizations),
            "scope": "telemetry:write",
            "iat": now,
            "exp": expires,
        },
        settings.jwt_secret,
        algorithm="HS256",
    )
    return CollectorToken(
        access_token=encoded,
        expires_in=settings.session_token_ttl_seconds,
    )


def _decode_token(
    settings: CollectorSettings, authorization: str | None
) -> dict[str, Any]:
    token = _bearer_token(authorization)
    try:
        return jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=["HS256"],
            issuer=settings.collector_issuer,
            audience=settings.collector_audience,
            options={"require": ["aud", "exp", "iat", "iss", "scope", "sub"]},
        )
    except InvalidTokenError as error:
        raise HTTPException(
            status_code=401, detail="Collector credential is invalid."
        ) from error


def _decode_session_token(
    settings: CollectorSettings, authorization: str | None
) -> SessionTokenClaims:
    payload = _decode_token(settings, authorization)
    if payload.get("scope") != "telemetry:write":
        raise HTTPException(
            status_code=403,
            detail="Collector credential cannot write telemetry.",
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
) -> FastAPI:
    """Create the collector with explicit dependencies for tests and deployment."""

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

    def require_read_token(
        token: Annotated[str | None, Header(alias="X-Telemetry-Read-Token")] = None,
    ) -> None:
        if token is None or not hmac.compare_digest(token, settings.read_token):
            raise HTTPException(
                status_code=401,
                detail="A valid dashboard read credential is required.",
            )

    def session_claims(
        authorization: Annotated[str | None, Header()] = None,
    ) -> SessionTokenClaims:
        return _decode_session_token(settings, authorization)

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

    @application.post(
        "/v1/auth/huggingface/exchange",
        response_model=CollectorToken,
    )
    def exchange_huggingface_token(
        authorization: Annotated[str | None, Header()] = None,
    ) -> CollectorToken:
        opaque_token = _bearer_token(authorization)
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
        return _issue_session_token(settings, principal)

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

    return application


def create_app_from_environment() -> FastAPI:
    """Create the production application from Cloud Run environment values."""

    settings = CollectorSettings.from_environment()
    store = PostgresTelemetryStore(_required_environment("DATABASE_URL"))
    return create_app(
        settings=settings,
        store=store,
        huggingface_authenticator=HuggingFaceAuthenticator(),
    )
