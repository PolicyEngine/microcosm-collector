"""Validated collector request and storage models."""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime
from types import MappingProxyType
from typing import Annotated, Any, Final, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    StrictBool,
    StrictFloat,
    StrictInt,
    ValidationError,
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


# The details keys microcosm-emitter writes itself (microcosm-emitter#90): a
# reading of the build and its machine and any sleep on heartbeats, the
# machine description on the run's start, stage spans, and outer progress.

_Count = Annotated[StrictInt, Field(ge=0)]
_Amount = Annotated[float, Field(ge=0, allow_inf_nan=False)]
_Share = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
_Text = Annotated[str, Field(max_length=200)]
_Identifier = Annotated[str, Field(pattern=SAFE_IDENTIFIER)]


class _EmitterDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OuterProgress(_EmitterDetail):
    """The counter one level above a stage's own, such as the pass of a batch."""

    done: StrictInt
    total: StrictInt
    unit: _Text | None = None


class ReadingProgress(_EmitterDetail):
    """The latest progress the build reported, as a reading repeats it.

    ``done`` and ``total`` are null unless the build sent numbers.
    """

    stage_id: _Identifier
    done: StrictInt | StrictFloat | None
    total: StrictInt | StrictFloat | None
    unit: _Text | None
    outer: OuterProgress | None
    reported_at: AwareDatetime


class ProcessTreeReading(_EmitterDetail):
    """The build and every descendant process at one reading.

    Memory is the physical footprint on macOS and RSS elsewhere; the CPU
    shares are cores in use and the disk bytes those moved during the
    reading's interval.
    """

    processes: _Count
    memory_bytes: _Count
    memory_peak_bytes: _Count
    interval_memory_peak_bytes: _Count
    cpu_user_cores: _Amount | None
    cpu_system_cores: _Amount | None
    disk_read_bytes: _Count | None
    disk_written_bytes: _Count | None


class MachineReading(_EmitterDetail):
    """The machine at one reading; each value the emitter can't read is null."""

    memory_available_bytes: _Count | None
    memory_compressed_bytes: _Count | None
    memory_pressure_level: Literal["normal", "warning", "critical"] | None
    memory_stall_some_share: _Share | None
    memory_stall_full_share: _Share | None
    swap_used_bytes: _Count | None
    swap_total_bytes: _Count | None
    swap_in_bytes: _Count | None
    swap_out_bytes: _Count | None
    cpu_busy_share: _Share | None
    load_average_1m: _Amount | None
    on_battery: StrictBool | None
    disk_free_bytes: _Count | None
    cgroup_memory_bytes: _Count | None
    cgroup_memory_limit_bytes: _Count | None


class Reading(_EmitterDetail):
    """A heartbeat's reading of the build and its machine (``details.reading``)."""

    interval_seconds: _Amount
    late_seconds: _Amount
    stages: list[_Identifier] = Field(max_length=32)
    progress: ReadingProgress | None
    process_tree: ProcessTreeReading
    machine: MachineReading


class SleepRecord(_EmitterDetail):
    """One system sleep, on the heartbeat sent on waking (``details.sleep``)."""

    started_at: AwareDatetime
    ended_at: AwareDatetime
    seconds: _Amount

    @model_validator(mode="after")
    def _ends_after_it_starts(self) -> SleepRecord:
        if self.ended_at < self.started_at:
            raise ValueError("a sleep must end after it starts")
        return self


class StageSpan(_EmitterDetail):
    """A stage start's parent and occurrence, and an end's durations and cause.

    ``closed_by`` is null when the build ended the stage itself.
    """

    parent_stage_id: _Identifier | None
    occurrence: Annotated[StrictInt, Field(ge=1)]
    awake_seconds: _Amount | None = None
    asleep_seconds: _Amount | None = None
    closed_by: (
        Literal[
            "enclosing_stage_ended",
            "next_stage_started",
            "other_stage_updated",
            "run_completed",
            "run_blocked",
            "run_failed",
        ]
        | None
    ) = None


class ModalContainer(_EmitterDetail):
    """The Modal container a build runs in."""

    task_id: _Text
    region: _Text | None
    cloud_provider: _Text | None


class MachineEnvironment(_EmitterDetail):
    """Where a build runs, on its run's start (``details.environment``)."""

    kind: Literal["local", "modal_container"]
    os: _Text | None
    os_version: _Text | None
    architecture: _Text | None
    cpu_model: _Text | None
    logical_cpus: Annotated[StrictInt, Field(ge=1)] | None
    memory_total_bytes: _Count | None
    cgroup_cpu_limit_cores: _Amount | None
    cgroup_memory_limit_bytes: _Count | None
    modal: ModalContainer | None
    emitter_version: _Text | None
    python_version: _Text | None

    @model_validator(mode="after")
    def _modal_matches_kind(self) -> MachineEnvironment:
        if (self.kind == "modal_container") != (self.modal is not None):
            raise ValueError("only a Modal container has a Modal description")
        return self


#: Each event type's emitter-written details keys and the model of each.
EMITTER_DETAILS: Final[Mapping[str, Mapping[str, type[BaseModel]]]] = MappingProxyType(
    {
        "heartbeat": {"reading": Reading, "sleep": SleepRecord},
        "run": {"environment": MachineEnvironment},
        "stage": {"stage_span": StageSpan},
        "progress": {"outer": OuterProgress},
    }
)
#: Names the emitter-written keys the collector removed because they didn't
#: match their model. Only the collector writes it.
DROPPED_EMITTER_DETAILS_KEY: Final = "dropped_emitter_details"


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

    @model_validator(mode="after")
    def check_emitter_details(self) -> TelemetryEvent:
        """Remove each emitter-written key that doesn't match its model.

        The removed keys are named in ``DROPPED_EMITTER_DETAILS_KEY``, and the
        rest of the event is kept: refusing the batch would make the emitter
        stop sending the run's events. Keys on other event types are kept as
        they are.
        """

        details = dict(self.details)
        details.pop(DROPPED_EMITTER_DETAILS_KEY, None)
        dropped = []
        for key, model in EMITTER_DETAILS.get(self.event_type, {}).items():
            if key not in details:
                continue
            try:
                model.model_validate(details[key])
            except ValidationError:
                del details[key]
                dropped.append(key)
        if dropped:
            details[DROPPED_EMITTER_DETAILS_KEY] = dropped
        self.details = details
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
