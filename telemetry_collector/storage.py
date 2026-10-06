"""SQLAlchemy persistence and deterministic run-document materialization."""

from __future__ import annotations

from collections.abc import Iterable
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy import and_, select, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from telemetry_collector.auth import HuggingFacePrincipal
from telemetry_collector.database import (
    TelemetryEvent as StoredTelemetryEvent,
)
from telemetry_collector.database import (
    TelemetryProducer,
    TelemetryRun,
    create_database_engine,
    create_session_factory,
)
from telemetry_collector.models import RunRegistration, TelemetryEvent


class RunRegistrationConflictError(ValueError):
    """Raised when a run identifier is reused with different metadata."""


class TelemetryStore(Protocol):
    """Storage operations used by the HTTP service."""

    def register_run(
        self,
        registration: RunRegistration,
        principal: HuggingFacePrincipal,
    ) -> None: ...

    def append_events(
        self,
        run_id: str,
        owner_hf_id: str,
        events: Iterable[TelemetryEvent],
    ) -> tuple[int, int]: ...

    def list_runs(
        self,
        *,
        country: str | None,
        limit: int,
        before: tuple[datetime, str] | None,
    ) -> list[dict[str, Any]]: ...

    def get_run(self, run_id: str) -> dict[str, Any] | None: ...

    def is_ready(self) -> bool: ...


def _now() -> datetime:
    return datetime.now(UTC)


def initial_run_state(
    registration: RunRegistration,
    principal: HuggingFacePrincipal,
    registered_at: datetime | None = None,
) -> dict[str, Any]:
    """Build the initial materialized state for a newly registered run."""

    timestamp = registered_at or _now()
    return {
        "run_id": registration.run_id,
        "country_code": registration.country_code,
        "pipeline": registration.pipeline,
        "candidate_id": registration.candidate_id,
        "release_id": registration.release_id,
        "run_kind": registration.run_kind,
        "owner_hf_id": principal.user_id,
        "owner_hf_username": principal.username,
        "registered_at": timestamp,
        "status": "running",
        "current_stage": "created",
        "started_at": timestamp,
        "updated_at": timestamp,
        "ended_at": None,
        "heartbeat_at": None,
        "resources": None,
        "work": None,
        "failure": None,
    }


def _event_order(event: dict[str, Any]) -> tuple[datetime, str, int, str]:
    return (
        event["producer_registered_at"],
        event["producer_id"],
        event["sequence"],
        event["event_id"],
    )


def canonical_events(events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Order producer streams independently from client clocks and delivery order."""

    return sorted(events, key=_event_order)


def _apply_event(run: dict[str, Any], event: dict[str, Any]) -> None:
    timestamp = event["timestamp"]
    run["started_at"] = min(run["started_at"], timestamp)
    run["updated_at"] = max(
        run["updated_at"], timestamp, event["producer_registered_at"]
    )
    if event.get("resources") is not None:
        run["resources"] = event["resources"]
    if event["event_type"] == "heartbeat":
        run["heartbeat_at"] = timestamp
    stage_id = event.get("stage_id")
    if stage_id and stage_id not in {"complete", "failed"}:
        run["current_stage"] = stage_id
    if event["event_type"] == "run" and event["status"] == "started":
        # A later producer represents a restarted attempt for the same stable
        # run identifier. Its creation event must reopen a previously finished
        # run before subsequent progress arrives.
        run["status"] = "running"
        run["ended_at"] = None
        run["failure"] = None
    details = event.get("details") or {}
    if event["event_type"] == "progress" and {
        "done",
        "total",
    }.issubset(details):
        run["work"] = {
            "stage": stage_id,
            "done": details["done"],
            "total": details["total"],
            "unit": details.get("unit"),
            "elapsed_seconds": details.get("elapsed_seconds", 0),
            "updated_at": timestamp.isoformat(),
            "details": {
                key: value
                for key, value in details.items()
                if key not in {"done", "total", "unit", "elapsed_seconds"}
            },
        }
    if (event["status"] == "failed" and event["event_type"] == "run") or (
        stage_id == "failed"
    ):
        run["status"] = "failed"
        run["current_stage"] = "failed"
        run["ended_at"] = timestamp
        run["failure"] = {
            "message": event.get("message"),
            "error_type": details.get("error_type"),
            "failure_class": details.get("failure_class"),
            "failed_during": details.get("failed_during") or stage_id,
        }
    elif event["status"] == "completed" and (
        event["event_type"] == "run" or stage_id == "complete"
    ):
        run["status"] = "completed"
        run["current_stage"] = "complete"
        run["ended_at"] = timestamp
        run["failure"] = None


def materialize_run(
    registered_run: dict[str, Any], events: Iterable[dict[str, Any]]
) -> dict[str, Any]:
    """Rebuild mutable run state from a stable canonical event order."""

    run = {
        **registered_run,
        "status": "running",
        "current_stage": "created",
        "started_at": registered_run["registered_at"],
        "updated_at": registered_run["registered_at"],
        "ended_at": None,
        "heartbeat_at": None,
        "resources": None,
        "work": None,
        "failure": None,
    }
    active_producer_id: str | None = None
    for event in canonical_events(events):
        producer_id = event["producer_id"]
        if active_producer_id is not None and producer_id != active_producer_id:
            # A producer id identifies one execution attempt. Starting a later
            # producer under the same stable run id must not retain the prior
            # attempt's completion, progress, heartbeat, or resource snapshot.
            run.update(
                {
                    "status": "running",
                    "current_stage": "created",
                    "started_at": event["timestamp"],
                    "updated_at": max(
                        run["updated_at"],
                        event["timestamp"],
                        event["producer_registered_at"],
                    ),
                    "ended_at": None,
                    "heartbeat_at": None,
                    "resources": None,
                    "work": None,
                    "failure": None,
                }
            )
        active_producer_id = producer_id
        _apply_event(run, event)
    return run


def run_summary(run: dict[str, Any]) -> dict[str, Any]:
    """Return the bounded run representation used by list responses."""

    return {
        "run_id": run["run_id"],
        "country_code": run["country_code"],
        "pipeline": run["pipeline"],
        "candidate_id": run.get("candidate_id"),
        "release_id": run.get("release_id"),
        "run_kind": run["run_kind"],
        "status": run["status"],
        "current_stage": run["current_stage"],
        "registered_at": run["registered_at"],
        "started_at": run["started_at"],
        "updated_at": run["updated_at"],
        "ended_at": run.get("ended_at"),
        "heartbeat_at": run.get("heartbeat_at"),
    }


def run_documents(
    registered_run: dict[str, Any], events: Iterable[dict[str, Any]]
) -> dict[str, Any]:
    """Return dashboard-compatible documents from the canonical event stream."""

    ordered = canonical_events(events)
    run = materialize_run(registered_run, ordered)
    lifecycle_events: list[dict[str, Any]] = []
    calibration_events: list[dict[str, Any]] = []
    identity: dict[str, Any] | None = None
    for sequence, event in enumerate(ordered, start=1):
        details = deepcopy(event.get("details") or {})
        if event["event_type"] == "run" and isinstance(details.get("identity"), dict):
            identity = details["identity"]
        event_type = "calibration" if event["event_type"] == "calibration" else "stage"
        lifecycle_events.append(
            {
                "schema_version": 2,
                "sequence": sequence,
                "timestamp": event["timestamp"],
                "time": event["timestamp"],
                "event_type": event_type,
                "run_id": event["run_id"],
                "stage_id": event.get("stage_id"),
                "stage": event.get("stage_id"),
                "status": event["status"],
                "message": event.get("message"),
                "details": details,
                "resources": event.get("resources"),
                "producer_id": event["producer_id"],
                "event_id": event["event_id"],
            }
        )
        if event["event_type"] == "calibration":
            calibration_events.append(
                {"time": event["timestamp"], "timestamp": event["timestamp"], **details}
            )
    common = {
        "schema_version": 2,
        "run_id": run["run_id"],
        "country_code": run["country_code"],
        "operation_id": run["run_id"],
        "pipeline": {"id": run["pipeline"], "version": "collector-v1"},
        "candidate_id": run.get("candidate_id"),
        "release_id": run.get("release_id"),
        "run_kind": run["run_kind"],
        "non_release": run.get("release_id") is None,
        "started_at": run["started_at"],
        "updated_at": run["updated_at"],
        "status": run["status"],
        "current_stage": run["current_stage"],
    }
    progress = {
        **common,
        "heartbeat_at": run.get("heartbeat_at"),
        "resources": run.get("resources"),
        "work": run.get("work"),
        "failure": run.get("failure"),
    }
    manifest = {
        **common,
        "identity": identity,
        "failure": run.get("failure"),
        "delivery": {
            "mode": "collector",
            "upload_attempts": None,
            "upload_successes": None,
        },
    }
    return {
        "run_id": run["run_id"],
        "country_code": run["country_code"],
        "progress": progress,
        "run_manifest": manifest,
        "calibration_progress": {
            "schema_version": 2,
            "run_id": run["run_id"],
            "candidate_id": run.get("candidate_id"),
            "updated_at": run["updated_at"],
            "events": calibration_events,
        }
        if calibration_events
        else None,
        "events": lifecycle_events,
    }


def _run_mapping(run: TelemetryRun) -> dict[str, Any]:
    return {
        column.name: getattr(run, column.name)
        for column in TelemetryRun.__table__.columns
    }


def _event_mapping(
    event: StoredTelemetryEvent, producer_registered_at: datetime
) -> dict[str, Any]:
    return {
        "event_id": event.event_id,
        "run_id": event.run_id,
        "producer_id": event.producer_id,
        "producer_registered_at": producer_registered_at,
        "sequence": event.sequence,
        "timestamp": event.emitted_at,
        "event_type": event.event_type,
        "stage_id": event.stage_id,
        "status": event.status,
        "message": event.message,
        "details": event.details,
        "resources": event.resources,
    }


def _registered_metadata(
    run: TelemetryRun,
) -> tuple[str, str, str | None, str | None, str]:
    return (
        run.country_code,
        run.pipeline,
        run.candidate_id,
        run.release_id,
        run.run_kind,
    )


def _requested_metadata(
    registration: RunRegistration,
) -> tuple[str, str, str | None, str | None, str]:
    return (
        registration.country_code,
        registration.pipeline,
        registration.candidate_id,
        registration.release_id,
        registration.run_kind,
    )


class PostgresTelemetryStore:
    """PostgreSQL store implemented with SQLAlchemy ORM sessions."""

    def __init__(self, database_url: str) -> None:
        self.engine: Engine = create_database_engine(database_url)
        self._sessions: sessionmaker[Session] = create_session_factory(self.engine)

    def close(self) -> None:
        """Dispose the SQLAlchemy engine and its connection pool."""

        self.engine.dispose()

    def is_ready(self) -> bool:
        """Return whether PostgreSQL is reachable and fully migrated."""

        from telemetry_collector.migrate import database_revision_is_current

        try:
            return database_revision_is_current(self.engine)
        except SQLAlchemyError:
            return False

    def register_run(
        self,
        registration: RunRegistration,
        principal: HuggingFacePrincipal,
    ) -> None:
        timestamp = _now()
        initial = initial_run_state(registration, principal, timestamp)
        with self._sessions.begin() as session:
            session.execute(
                insert(TelemetryRun)
                .values(**initial)
                .on_conflict_do_nothing(index_elements=[TelemetryRun.run_id])
            )
            run = session.execute(
                select(TelemetryRun)
                .where(TelemetryRun.run_id == registration.run_id)
                .with_for_update()
            ).scalar_one()
            if run.owner_hf_id != principal.user_id:
                raise PermissionError("Run belongs to another Hugging Face user.")
            if _registered_metadata(run) != _requested_metadata(registration):
                raise RunRegistrationConflictError(
                    "Run identifier is already registered with different metadata."
                )
            run.owner_hf_username = principal.username
            session.execute(
                insert(TelemetryProducer)
                .values(
                    run_id=registration.run_id,
                    producer_id=registration.producer_id,
                    registered_at=timestamp,
                )
                .on_conflict_do_nothing(
                    index_elements=[
                        TelemetryProducer.run_id,
                        TelemetryProducer.producer_id,
                    ]
                )
            )

    def _events_for_run(self, session: Session, run_id: str) -> list[dict[str, Any]]:
        rows = session.execute(
            select(
                StoredTelemetryEvent,
                TelemetryProducer.registered_at,
            )
            .join(
                TelemetryProducer,
                and_(
                    StoredTelemetryEvent.run_id == TelemetryProducer.run_id,
                    StoredTelemetryEvent.producer_id == TelemetryProducer.producer_id,
                ),
            )
            .where(StoredTelemetryEvent.run_id == run_id)
            .order_by(
                TelemetryProducer.registered_at,
                TelemetryProducer.producer_id,
                StoredTelemetryEvent.sequence,
                StoredTelemetryEvent.event_id,
            )
        ).all()
        return [_event_mapping(event, registered_at) for event, registered_at in rows]

    def append_events(
        self,
        run_id: str,
        owner_hf_id: str,
        events: Iterable[TelemetryEvent],
    ) -> tuple[int, int]:
        models = list(events)
        with self._sessions.begin() as session:
            run = session.execute(
                select(TelemetryRun)
                .where(TelemetryRun.run_id == run_id)
                .with_for_update()
            ).scalar_one_or_none()
            if run is None:
                raise KeyError(run_id)
            if run.owner_hf_id != owner_hf_id:
                raise PermissionError("Run belongs to another Hugging Face user.")
            if not models:
                return 0, 0
            producer_ids = {model.producer_id for model in models}
            registered_producer_ids = set(
                session.execute(
                    select(TelemetryProducer.producer_id).where(
                        TelemetryProducer.run_id == run_id,
                        TelemetryProducer.producer_id.in_(producer_ids),
                    )
                ).scalars()
            )
            if registered_producer_ids != producer_ids:
                raise PermissionError(
                    "Telemetry producer is not registered for this run."
                )
            values = []
            for model in models:
                event = model.model_dump(mode="python")
                values.append(
                    {
                        "event_id": event["event_id"],
                        "run_id": event["run_id"],
                        "producer_id": event["producer_id"],
                        "sequence": event["sequence"],
                        "emitted_at": event["timestamp"],
                        "event_type": event["event_type"],
                        "stage_id": event["stage_id"],
                        "status": event["status"],
                        "message": event["message"],
                        "details": event["details"],
                        "resources": event["resources"],
                    }
                )
            accepted_ids = session.execute(
                insert(StoredTelemetryEvent)
                .values(values)
                .on_conflict_do_nothing()
                .returning(StoredTelemetryEvent.event_id)
            ).scalars()
            accepted = len(accepted_ids.all())
            if accepted:
                session.flush()
                materialized = materialize_run(
                    _run_mapping(run), self._events_for_run(session, run_id)
                )
                for field in (
                    "status",
                    "current_stage",
                    "started_at",
                    "updated_at",
                    "ended_at",
                    "heartbeat_at",
                    "resources",
                    "work",
                    "failure",
                ):
                    setattr(run, field, materialized[field])
        return accepted, len(models) - accepted

    def list_runs(
        self,
        *,
        country: str | None,
        limit: int,
        before: tuple[datetime, str] | None,
    ) -> list[dict[str, Any]]:
        statement = select(TelemetryRun)
        if country is not None:
            statement = statement.where(TelemetryRun.country_code == country)
        if before is not None:
            statement = statement.where(
                tuple_(TelemetryRun.registered_at, TelemetryRun.run_id) < before
            )
        statement = statement.order_by(
            TelemetryRun.registered_at.desc(), TelemetryRun.run_id.desc()
        ).limit(limit)
        with self._sessions() as session:
            runs = session.execute(statement).scalars().all()
            return [run_summary(_run_mapping(run)) for run in runs]

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._sessions() as session:
            run = session.get(TelemetryRun, run_id)
            if run is None:
                return None
            events = self._events_for_run(session, run_id)
            return run_documents(_run_mapping(run), events)
