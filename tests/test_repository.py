from __future__ import annotations

from datetime import UTC, datetime, timedelta

from telemetry_collector.auth import HuggingFacePrincipal
from telemetry_collector.models import RunRegistration, TelemetryEvent
from telemetry_collector.repository import (
    initial_run_state,
    materialize_run,
    run_documents,
)


def _registered(at: datetime) -> dict[str, object]:
    return initial_run_state(
        RunRegistration(
            run_id="run-1",
            producer_id="producer-1",
            country_code="US",
            pipeline="us-fiscal-refresh",
        ),
        HuggingFacePrincipal(
            user_id="owner-1",
            username="builder",
            organizations=("policyengine",),
        ),
        at,
    )


def _event(
    *,
    event_id: str,
    producer_id: str = "producer-1",
    producer_registered_at: datetime,
    sequence: int,
    timestamp: datetime,
    status: str,
    stage_id: str,
) -> dict[str, object]:
    return {
        "event_id": event_id,
        "run_id": "run-1",
        "producer_id": producer_id,
        "producer_registered_at": producer_registered_at,
        "sequence": sequence,
        "timestamp": timestamp,
        "event_type": "run",
        "stage_id": stage_id,
        "status": status,
        "message": event_id,
        "details": {},
        "resources": None,
    }


def test_materialization_is_independent_of_delivery_order() -> None:
    registered_at = datetime(2026, 10, 1, tzinfo=UTC)
    failed = _event(
        event_id="failed-first",
        producer_registered_at=registered_at,
        sequence=1,
        timestamp=registered_at + timedelta(minutes=2),
        status="failed",
        stage_id="failed",
    )
    completed = _event(
        event_id="completed-second",
        producer_registered_at=registered_at,
        sequence=2,
        timestamp=registered_at + timedelta(minutes=1),
        status="completed",
        stage_id="complete",
    )

    normal = materialize_run(_registered(registered_at), [failed, completed])
    delayed = materialize_run(_registered(registered_at), [completed, failed])

    assert delayed == normal
    assert delayed["status"] == "completed"
    assert delayed["failure"] is None
    assert delayed["ended_at"] == completed["timestamp"]


def test_sequence_order_wins_when_a_producer_clock_moves_backward() -> None:
    registered_at = datetime(2026, 10, 1, tzinfo=UTC)
    events = [
        _event(
            event_id="started",
            producer_registered_at=registered_at,
            sequence=1,
            timestamp=registered_at + timedelta(minutes=2),
            status="started",
            stage_id="compile",
        ),
        _event(
            event_id="completed",
            producer_registered_at=registered_at,
            sequence=2,
            timestamp=registered_at + timedelta(minutes=1),
            status="completed",
            stage_id="complete",
        ),
    ]

    materialized = materialize_run(_registered(registered_at), events)

    assert materialized["status"] == "completed"
    assert materialized["started_at"] == registered_at
    assert materialized["updated_at"] == registered_at + timedelta(minutes=2)


def test_later_registered_producer_stream_takes_precedence() -> None:
    registered_at = datetime(2026, 10, 1, tzinfo=UTC)
    events = [
        _event(
            event_id="old-completion",
            producer_registered_at=registered_at,
            sequence=1,
            timestamp=registered_at + timedelta(minutes=5),
            status="completed",
            stage_id="complete",
        ),
        _event(
            event_id="restart-failure",
            producer_id="producer-2",
            producer_registered_at=registered_at + timedelta(minutes=10),
            sequence=1,
            timestamp=registered_at + timedelta(minutes=3),
            status="failed",
            stage_id="failed",
        ),
    ]

    materialized = materialize_run(_registered(registered_at), events)
    documents = run_documents(_registered(registered_at), events)

    assert materialized["status"] == "failed"
    assert materialized["failure"]["message"] == "restart-failure"
    assert [event["event_id"] for event in documents["events"]] == [
        "old-completion",
        "restart-failure",
    ]


def test_later_registered_producer_reopens_a_finished_run() -> None:
    registered_at = datetime(2026, 10, 1, tzinfo=UTC)
    old_progress = _event(
        event_id="old-progress",
        producer_registered_at=registered_at,
        sequence=1,
        timestamp=registered_at + timedelta(minutes=4),
        status="progress",
        stage_id="compile",
    )
    old_progress.update(
        {
            "event_type": "progress",
            "details": {"done": 5, "total": 10},
            "resources": {"cpu_user_seconds": 100},
        }
    )
    events = [
        old_progress,
        _event(
            event_id="old-completion",
            producer_registered_at=registered_at,
            sequence=2,
            timestamp=registered_at + timedelta(minutes=5),
            status="completed",
            stage_id="complete",
        ),
        _event(
            event_id="restart-started",
            producer_id="producer-2",
            producer_registered_at=registered_at + timedelta(minutes=10),
            sequence=1,
            timestamp=registered_at + timedelta(minutes=6),
            status="started",
            stage_id="created",
        ),
    ]

    materialized = materialize_run(_registered(registered_at), events)

    assert materialized["status"] == "running"
    assert materialized["current_stage"] == "created"
    assert materialized["ended_at"] is None
    assert materialized["failure"] is None
    assert materialized["started_at"] == registered_at + timedelta(minutes=6)
    assert materialized["updated_at"] == registered_at + timedelta(minutes=10)
    assert materialized["heartbeat_at"] is None
    assert materialized["resources"] is None
    assert materialized["work"] is None


def test_progress_work_uses_json_compatible_timestamp() -> None:
    registered_at = datetime(2026, 10, 1, tzinfo=UTC)
    event = TelemetryEvent(
        schema_version=1,
        event_id="progress",
        run_id="run-1",
        producer_id="producer-1",
        sequence=1,
        timestamp=registered_at,
        event_type="progress",
        stage_id="compile",
        status="progress",
        details={"done": 1, "total": 2},
    ).model_dump(mode="python")
    event["producer_registered_at"] = registered_at

    materialized = materialize_run(_registered(registered_at), [event])

    assert materialized["work"]["updated_at"] == registered_at.isoformat()
