from __future__ import annotations

from datetime import UTC, datetime, timedelta

from telemetry_collector.auth import HuggingFacePrincipal
from telemetry_collector.models import RunRegistration, TelemetryEvent
from telemetry_collector.storage import (
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


def test_blocked_run_event_ends_the_run_with_its_block() -> None:
    registered_at = datetime(2026, 10, 1, tzinfo=UTC)
    blocked = _event(
        event_id="blocked",
        producer_registered_at=registered_at,
        sequence=2,
        timestamp=registered_at + timedelta(minutes=9),
        status="blocked",
        stage_id="blocked",
    )
    blocked["details"] = {
        "phase": "terminal",
        "blocking_failure_count": 1,
        "blocking_gate_ids": ["uk_target_fit"],
        "gate_statuses": {"uk_target_fit": "failed"},
    }
    events = [
        _event(
            event_id="gates",
            producer_registered_at=registered_at,
            sequence=1,
            timestamp=registered_at + timedelta(minutes=8),
            status="completed",
            stage_id="gate_battery",
        ),
        blocked,
    ]
    events[0]["event_type"] = "stage"

    materialized = materialize_run(_registered(registered_at), events)
    documents = run_documents(_registered(registered_at), events)

    block = {
        "phase": "terminal",
        "blocking_failure_count": 1,
        "blocking_gate_ids": ["uk_target_fit"],
    }
    assert materialized["status"] == "blocked"
    assert materialized["current_stage"] == "blocked"
    assert materialized["ended_at"] == blocked["timestamp"]
    assert materialized["failure"] is None
    assert materialized["block"] == block
    assert documents["progress"]["status"] == "blocked"
    assert documents["progress"]["block"] == block
    assert documents["run_manifest"]["block"] == block
    assert documents["events"][-1]["details"]["gate_statuses"] == {
        "uk_target_fit": "failed"
    }


def test_blocked_run_without_gate_ids_or_statuses_keeps_its_count() -> None:
    registered_at = datetime(2026, 10, 1, tzinfo=UTC)
    blocked = _event(
        event_id="blocked",
        producer_registered_at=registered_at,
        sequence=1,
        timestamp=registered_at + timedelta(minutes=1),
        status="blocked",
        stage_id="blocked",
    )
    blocked["details"] = {
        "phase": "preflight",
        "blocking_failure_count": 2,
        "blocking_gate_ids": [],
    }

    materialized = materialize_run(_registered(registered_at), [blocked])

    assert materialized["status"] == "blocked"
    assert materialized["block"] == {
        "phase": "preflight",
        "blocking_failure_count": 2,
        "blocking_gate_ids": [],
    }


def test_failed_run_keeps_its_error_code() -> None:
    registered_at = datetime(2026, 10, 1, tzinfo=UTC)
    failed = _event(
        event_id="failed",
        producer_registered_at=registered_at,
        sequence=1,
        timestamp=registered_at + timedelta(minutes=1),
        status="failed",
        stage_id="failed",
    )
    failed["details"] = {
        "error_type": "KeyboardInterrupt",
        "error_code": "INTERRUPTED",
        "failure_class": "interrupted",
        "failed_during": "calibrating",
    }

    assert materialize_run(_registered(registered_at), [failed])["failure"] == {
        "message": "failed",
        "error_type": "KeyboardInterrupt",
        "error_code": "INTERRUPTED",
        "failure_class": "interrupted",
        "failed_during": "calibrating",
    }


def test_restarted_producer_clears_a_previous_block() -> None:
    registered_at = datetime(2026, 10, 1, tzinfo=UTC)
    blocked = _event(
        event_id="blocked",
        producer_registered_at=registered_at,
        sequence=1,
        timestamp=registered_at + timedelta(minutes=1),
        status="blocked",
        stage_id="blocked",
    )
    blocked["details"] = {
        "phase": "terminal",
        "blocking_failure_count": 1,
        "blocking_gate_ids": ["uk_target_fit"],
    }
    restarted = _event(
        event_id="restart-started",
        producer_id="producer-2",
        producer_registered_at=registered_at + timedelta(minutes=10),
        sequence=1,
        timestamp=registered_at + timedelta(minutes=11),
        status="started",
        stage_id="created",
    )

    materialized = materialize_run(_registered(registered_at), [blocked, restarted])

    assert materialized["status"] == "running"
    assert materialized["block"] is None
