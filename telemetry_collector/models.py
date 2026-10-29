"""Validated collector request and storage models."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    field_validator,
    model_validator,
)

SAFE_IDENTIFIER = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,191}$"


def _require_block(details: dict[str, Any]) -> None:
    """A ``blocked`` event carries the refusal as staging contract v3 defines a
    block: the gate phase, at least one blocking failure, and the gate ids
    (possibly none). ``gate_statuses`` and other details are optional."""

    phase = details.get("phase")
    count = details.get("blocking_failure_count")
    gate_ids = details.get("blocking_gate_ids")
    if not isinstance(phase, str) or not re.fullmatch(SAFE_IDENTIFIER, phase):
        raise ValueError("a blocked event's phase must be a safe identifier")
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise ValueError(
            "a blocked event's blocking_failure_count must be an integer of at least 1"
        )
    if not isinstance(gate_ids, list) or any(
        not isinstance(gate_id, str) or not 1 <= len(gate_id) <= 200
        for gate_id in gate_ids
    ):
        raise ValueError(
            "a blocked event's blocking_gate_ids must be a list of gate ids"
        )


class PageCursor(RootModel[tuple[datetime, str]]):
    """Typed contents of an opaque run-list pagination cursor."""

    @model_validator(mode="after")
    def validate_contents(self) -> PageCursor:
        registered_at, run_id = self.root
        if registered_at.tzinfo is None or registered_at.utcoffset() is None:
            raise ValueError("cursor timestamp must include a timezone")
        if not re.fullmatch(SAFE_IDENTIFIER, run_id):
            raise ValueError("cursor run identifier is invalid")
        return self


class RunRegistration(BaseModel):
    """Run identity bound to an expiring collector credential."""

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(pattern=SAFE_IDENTIFIER)
    producer_id: str = Field(pattern=SAFE_IDENTIFIER)
    country_code: str = Field(pattern=r"^[A-Z]{2}$")
    pipeline: str = Field(pattern=SAFE_IDENTIFIER)
    candidate_id: str | None = Field(default=None, pattern=SAFE_IDENTIFIER)
    release_id: str | None = Field(default=None, pattern=SAFE_IDENTIFIER)
    run_kind: str = Field(default="build", pattern=SAFE_IDENTIFIER)


class ResourceSnapshot(BaseModel):
    """Cumulative CPU and current/peak resident memory for a process tree."""

    model_config = ConfigDict(extra="forbid")

    cpu_user_seconds: float | None = Field(default=None, ge=0)
    cpu_system_seconds: float | None = Field(default=None, ge=0)
    rss_bytes: int | None = Field(default=None, ge=0)
    peak_rss_bytes: int | None = Field(default=None, ge=0)


class TelemetryEvent(BaseModel):
    """One idempotent event emitted by a Microcosm build process."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    event_id: str = Field(pattern=SAFE_IDENTIFIER)
    run_id: str = Field(pattern=SAFE_IDENTIFIER)
    producer_id: str = Field(pattern=SAFE_IDENTIFIER)
    sequence: int = Field(ge=1)
    timestamp: datetime
    event_type: Literal["run", "stage", "progress", "calibration", "heartbeat"]
    stage_id: str | None = Field(default=None, pattern=SAFE_IDENTIFIER)
    status: Literal["started", "progress", "completed", "failed", "blocked"]
    message: str | None = Field(default=None, max_length=500)
    details: dict[str, Any] = Field(default_factory=dict)
    resources: ResourceSnapshot | None = None

    @field_validator("timestamp")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must include a timezone")
        return value

    @model_validator(mode="after")
    def require_block_details(self) -> TelemetryEvent:
        if self.status == "blocked":
            _require_block(self.details)
        return self


class EventBatch(BaseModel):
    """Bounded event batch accepted by the ingestion endpoint."""

    model_config = ConfigDict(extra="forbid")

    events: list[TelemetryEvent] = Field(max_length=200)


SessionScope = Literal["telemetry:write", "artifacts:submit"]


def require_telemetry_scope_for_lifetime(
    scope: SessionScope, lifetime_seconds: int | None
) -> None:
    """Refuse a requested token lifetime with any scope but telemetry:write.

    The exchange's request model and the function that issues tokens both call
    this, so the rule holds for every caller.
    """
    if lifetime_seconds is not None and scope != "telemetry:write":
        raise ValueError(
            "lifetime_seconds is accepted only with scope telemetry:write."
        )


class TokenExchangeRequest(BaseModel):
    """Optional body of the token exchange; omitting it asks for telemetry."""

    model_config = ConfigDict(extra="forbid")

    scope: SessionScope = "telemetry:write"
    # Asks for a token that a build on another machine, with no Hugging Face
    # credential of its own, uses for its whole run. The collector grants at
    # most 24 hours and marks the token "delegated". An explicit null is the
    # same as leaving the field out.
    lifetime_seconds: int | None = Field(default=None, gt=0, strict=True)

    @model_validator(mode="after")
    def _lifetime_only_for_telemetry(self) -> TokenExchangeRequest:
        require_telemetry_scope_for_lifetime(self.scope, self.lifetime_seconds)
        return self


class CollectorToken(BaseModel):
    """Expiring token returned after Hugging Face verification.

    ``scope`` repeats the token's scope, so a client can tell that a collector
    without intake answered an ``artifacts:submit`` request with a
    ``telemetry:write`` token.
    """

    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int
    scope: SessionScope
