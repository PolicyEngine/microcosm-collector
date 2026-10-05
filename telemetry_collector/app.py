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
    HuggingFaceAuthenticator,
    HuggingFacePrincipal,
)
from telemetry_collector.models import (
    CollectorToken,
    EventBatch,
    PageCursor,
    RunRegistration,
)
from telemetry_collector.repository import (
    PostgresTelemetryRepository,
    RunRegistrationConflictError,
    TelemetryRepository,
)


@dataclass(frozen=True)
class CollectorSettings:
    """Secrets and policy read once when the collector starts."""

    jwt_secret: str
    read_token: str
    required_huggingface_org: str = "policyengine"
    collector_issuer: str = "policyengine-telemetry"
    run_token_ttl_seconds: int = 15 * 60

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
            run_token_ttl_seconds=int(
                os.environ.get("TELEMETRY_RUN_TOKEN_TTL_SECONDS", "900")
            ),
        )


@dataclass(frozen=True)
class RunTokenClaims:
    """Authorization carried by a collector-issued run token."""

    subject: str
    run_id: str
    producer_id: str


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


def _issue_run_token(
    settings: CollectorSettings,
    registration: RunRegistration,
    principal: HuggingFacePrincipal,
) -> CollectorToken:
    now = datetime.now(UTC)
    expires = now + timedelta(seconds=settings.run_token_ttl_seconds)
    encoded = jwt.encode(
        {
            "iss": settings.collector_issuer,
            "sub": principal.user_id,
            "scope": "telemetry:write",
            "run_id": registration.run_id,
            "producer_id": registration.producer_id,
            "iat": now,
            "exp": expires,
        },
        settings.jwt_secret,
        algorithm="HS256",
    )
    return CollectorToken(
        access_token=encoded,
        expires_in=settings.run_token_ttl_seconds,
    )


def _decode_run_token(
    settings: CollectorSettings, authorization: str | None
) -> RunTokenClaims:
    token = _bearer_token(authorization)
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=["HS256"],
            issuer=settings.collector_issuer,
            options={"require": ["exp", "iat", "iss", "sub"]},
        )
    except InvalidTokenError as error:
        raise HTTPException(
            status_code=401, detail="Run credential is invalid."
        ) from error
    if payload.get("scope") != "telemetry:write":
        raise HTTPException(
            status_code=403, detail="Run credential cannot ingest telemetry."
        )
    run_id = payload.get("run_id")
    producer_id = payload.get("producer_id")
    subject = payload.get("sub")
    if not all(
        isinstance(value, str) and value for value in (run_id, producer_id, subject)
    ):
        raise HTTPException(status_code=401, detail="Run credential is incomplete.")
    return RunTokenClaims(
        subject=subject,
        run_id=run_id,
        producer_id=producer_id,
    )


def create_app(
    *,
    settings: CollectorSettings,
    repository: TelemetryRepository,
    huggingface_authenticator: HuggingFaceAuthenticator,
) -> FastAPI:
    """Create the collector with explicit dependencies for tests and deployment."""

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> Iterator[None]:
        try:
            yield
        finally:
            close = getattr(repository, "close", None)
            if callable(close):
                close()

    application = FastAPI(
        title="PolicyEngine Microcosm telemetry collector",
        version="1.0.0",
        lifespan=lifespan,
    )

    application.add_middleware(RequestBodyLimitMiddleware, max_bytes=1_048_576)

    def require_read_token(
        token: Annotated[str | None, Header(alias="X-Telemetry-Read-Token")] = None,
    ) -> None:
        if token is None or not hmac.compare_digest(token, settings.read_token):
            raise HTTPException(
                status_code=401,
                detail="A valid dashboard read credential is required.",
            )

    def run_claims(
        authorization: Annotated[str | None, Header()] = None,
    ) -> RunTokenClaims:
        return _decode_run_token(settings, authorization)

    @application.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/ready")
    def ready() -> dict[str, str]:
        if not repository.is_ready():
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
        registration: RunRegistration,
        authorization: Annotated[str | None, Header()] = None,
    ) -> CollectorToken:
        opaque_token = _bearer_token(authorization)
        try:
            principal = huggingface_authenticator.authenticate(
                opaque_token,
                settings.required_huggingface_org,
            )
        except HuggingFaceAuthenticationError as error:
            raise HTTPException(status_code=401, detail=str(error)) from error
        if settings.required_huggingface_org not in principal.organizations:
            raise HTTPException(
                status_code=403,
                detail="The Hugging Face user is not a PolicyEngine organization member.",
            )
        try:
            repository.register_run(registration, principal)
        except PermissionError as error:
            raise HTTPException(status_code=403, detail=str(error)) from error
        except RunRegistrationConflictError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return _issue_run_token(settings, registration, principal)

    @application.post(
        "/v1/runs/{run_id}/events",
        status_code=status.HTTP_202_ACCEPTED,
    )
    def ingest_events(
        run_id: str,
        batch: EventBatch,
        claims: RunTokenClaims = Depends(run_claims),
    ) -> dict[str, int]:
        if claims.run_id != run_id:
            raise HTTPException(
                status_code=403,
                detail="Run credential is bound to another run.",
            )
        for event in batch.events:
            if event.run_id != run_id or event.producer_id != claims.producer_id:
                raise HTTPException(
                    status_code=403,
                    detail="Event identity does not match the run credential.",
                )
        try:
            accepted, duplicates = repository.append_events(run_id, batch.events)
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
        runs = repository.list_runs(
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
        run = repository.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Run was not found.")
        return run

    return application


def create_app_from_environment() -> FastAPI:
    """Create the production application from Cloud Run environment values."""

    settings = CollectorSettings.from_environment()
    repository = PostgresTelemetryRepository(_required_environment("DATABASE_URL"))
    return create_app(
        settings=settings,
        repository=repository,
        huggingface_authenticator=HuggingFaceAuthenticator(),
    )
