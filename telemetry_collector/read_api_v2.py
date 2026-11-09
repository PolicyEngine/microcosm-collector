"""Response models and documents for version 2 of the read API.

Version 1 (``GET /v1/runs`` and ``GET /v1/runs/{run_id}``) returns the
documents the dashboard rebuilt from Hugging Face run files, and stays
unchanged for it. Version 2 returns each run's state together with the events
its producers sent, as the collector stored them: every field of the event
model, with an optional field the producer left out as null (``details`` as an
empty object), and the timestamp in UTC. ``contracts/read-api-v2/openapi.json``
is generated from these models.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import FastAPI
from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field

from telemetry_collector.storage import canonical_events, materialize_run

OPENAPI_TITLE = "Microcosm collector read API"
OPENAPI_VERSION = "2"
PATH_PREFIX = "/v2/"


def _in_utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


# Every timestamp in a response is given in UTC, whatever offset its producer
# sent and whatever time zone the database session uses.
UtcDatetime = Annotated[AwareDatetime, AfterValidator(_in_utc)]

RunStatus = Literal["running", "completed", "failed", "blocked"]


class _Response(BaseModel):
    # Every field is always present in a response, including those with a
    # default, so the schema marks them all required.
    model_config = ConfigDict(
        extra="forbid", json_schema_serialization_defaults_required=True
    )


class RunSummary(_Response):
    """A run's registration and the stored summary of its state."""

    run_id: str
    country_code: str
    pipeline: str
    candidate_id: str | None
    release_id: str | None
    run_kind: str
    status: RunStatus
    current_stage: str
    registered_at: UtcDatetime
    started_at: UtcDatetime
    updated_at: UtcDatetime
    ended_at: UtcDatetime | None
    heartbeat_at: UtcDatetime | None


class RunPage(_Response):
    """One page of runs, newest registration first."""

    runs: list[RunSummary]
    next_before: str | None = Field(
        description=(
            "Pass as `before` for the next page. Null when this page holds fewer "
            "runs than `limit`; a full last page is followed by an empty one."
        )
    )


class Resources(_Response):
    """Cumulative CPU and current and peak resident memory of a process tree."""

    cpu_user_seconds: float | None = None
    cpu_system_seconds: float | None = None
    rss_bytes: int | None = None
    peak_rss_bytes: int | None = None


class RunWork(_Response):
    """The latest progress event that reported `done` and `total`.

    Except for `stage` and `updated_at`, the values are the event's details as
    the producer sent them.
    """

    stage: str | None
    done: Any
    total: Any
    unit: Any
    elapsed_seconds: Any
    updated_at: UtcDatetime = Field(description="The event's timestamp.")
    details: dict[str, Any]


class RunFailure(_Response):
    """Why a failed run failed, from the event that failed it."""

    message: str | None
    error_type: Any
    error_code: Any
    failure_class: Any
    failed_during: Any


class RunBlock(_Response):
    """The acceptance checks' refusal, from the event that blocked the run."""

    phase: str | None
    blocking_failure_count: int | None
    blocking_gate_ids: list[str]


class RunState(RunSummary):
    """A run's state, rebuilt from the events returned with it."""

    resources: Resources | None
    work: RunWork | None
    failure: RunFailure | None
    block: RunBlock | None


class RawEvent(_Response):
    """An event as the collector stored it.

    Every field of the event model is present: an optional field the producer
    left out is null (`details` an empty object), and the timestamp is in UTC.
    """

    schema_version: Literal[1]
    event_id: str
    run_id: str
    producer_id: str
    sequence: int
    timestamp: UtcDatetime
    event_type: Literal["run", "stage", "progress", "calibration", "heartbeat"]
    stage_id: str | None
    status: Literal["started", "progress", "completed", "failed", "blocked"]
    message: str | None
    details: dict[str, Any]
    resources: Resources | None


class RunDetail(_Response):
    """A run's state and every event it was rebuilt from."""

    run: RunState
    events: list[RawEvent] = Field(
        description=(
            "Every stored event of the run, in the order the collector applies "
            "them: producers by registration time, then producer id, and each "
            "producer's events by sequence."
        )
    )


class ErrorMessage(_Response):
    """An error explained in one sentence."""

    detail: str


RUN_STATE_FIELDS = tuple(RunState.model_fields)
RAW_EVENT_FIELDS = tuple(RawEvent.model_fields)


def run_detail(
    registered_run: dict[str, Any], events: Iterable[dict[str, Any]]
) -> RunDetail:
    """Return the run's state rebuilt from ``events``, and the events themselves.

    The state is folded from exactly the events returned, so the two agree even
    while the run is still receiving events.
    """

    ordered = canonical_events(events)
    state = materialize_run(registered_run, ordered)
    return RunDetail(
        run=RunState.model_validate(
            {field: state.get(field) for field in RUN_STATE_FIELDS}
        ),
        events=[
            RawEvent.model_validate(
                {
                    "schema_version": 1,
                    **{
                        field: event.get(field)
                        for field in RAW_EVENT_FIELDS
                        if field != "schema_version"
                    },
                }
            )
            for event in ordered
        ],
    )


def _referenced_schemas(value: Any) -> set[str]:
    if isinstance(value, dict):
        names = set()
        reference = value.get("$ref")
        if isinstance(reference, str) and reference.startswith("#/components/schemas/"):
            names.add(reference.removeprefix("#/components/schemas/"))
        for item in value.values():
            names |= _referenced_schemas(item)
        return names
    if isinstance(value, list):
        return set().union(*(_referenced_schemas(item) for item in value))
    return set()


def _security_scheme_names(paths: dict[str, Any]) -> set[str]:
    return {
        name
        for operations in paths.values()
        for operation in operations.values()
        for requirement in operation.get("security", [])
        for name in requirement
    }


def openapi_document(application: FastAPI) -> dict[str, Any]:
    """Return the OpenAPI document of the application's version 2 routes only."""

    document = application.openapi()
    paths = {
        path: operations
        for path, operations in document["paths"].items()
        if path.startswith(PATH_PREFIX)
    }
    all_schemas = document.get("components", {}).get("schemas", {})
    needed: set[str] = set()
    pending = _referenced_schemas(paths)
    while pending:
        name = pending.pop()
        if name in needed:
            continue
        needed.add(name)
        pending |= _referenced_schemas(all_schemas[name]) - needed
    components: dict[str, Any] = {
        "schemas": {name: all_schemas[name] for name in sorted(needed)}
    }
    # Only the schemes version 2's operations require, not those of other routes.
    all_security_schemes = document.get("components", {}).get("securitySchemes", {})
    scheme_names = _security_scheme_names(paths)
    if scheme_names:
        components["securitySchemes"] = {
            name: all_security_schemes[name] for name in sorted(scheme_names)
        }
    return {
        "openapi": document["openapi"],
        "info": {
            "title": OPENAPI_TITLE,
            "version": OPENAPI_VERSION,
            "description": (
                "Version 2 of the Microcosm collector's read API. Each request "
                "carries the dashboard read credential in "
                "`X-Telemetry-Read-Token`. While a deployment is in progress, "
                "every request may answer 503 with `Retry-After`."
            ),
        },
        "paths": paths,
        "components": components,
    }
