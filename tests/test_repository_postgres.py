from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from telemetry_collector.auth import HuggingFacePrincipal
from telemetry_collector.migrate import upgrade_database
from telemetry_collector.models import RunRegistration, TelemetryEvent
from telemetry_collector.repository import (
    PostgresTelemetryRepository,
    RunRegistrationConflictError,
)


def _principal(user_id: str = "owner-1") -> HuggingFacePrincipal:
    return HuggingFacePrincipal(
        user_id=user_id,
        username="builder",
        organizations=("policyengine",),
    )


def _registration(**changes: str) -> RunRegistration:
    values = {
        "run_id": "run-1",
        "producer_id": "producer-1",
        "country_code": "US",
        "pipeline": "us-fiscal-refresh",
    }
    values.update(changes)
    return RunRegistration(**values)


def _event(
    *, event_id: str, sequence: int, timestamp: datetime, status: str, stage_id: str
) -> TelemetryEvent:
    return TelemetryEvent(
        schema_version=1,
        event_id=event_id,
        run_id="run-1",
        producer_id="producer-1",
        sequence=sequence,
        timestamp=timestamp,
        event_type="run",
        stage_id=stage_id,
        status=status,
        details={},
        resources={
            "cpu_user_seconds": sequence,
            "rss_bytes": sequence * 1024,
        },
    )


def test_repository_is_idempotent_and_reduces_out_of_order_events(
    postgres_url: str,
) -> None:
    upgrade_database(postgres_url)
    repository = PostgresTelemetryRepository(postgres_url)
    registered_at = datetime.now(UTC)
    repository.register_run(_registration(), _principal())
    completed = _event(
        event_id="completed",
        sequence=2,
        timestamp=registered_at + timedelta(minutes=1),
        status="completed",
        stage_id="complete",
    )
    delayed_failure = _event(
        event_id="delayed-failure",
        sequence=1,
        timestamp=registered_at + timedelta(minutes=2),
        status="failed",
        stage_id="failed",
    )

    assert repository.append_events("run-1", "owner-1", [completed]) == (1, 0)
    assert repository.append_events("run-1", "owner-1", [completed]) == (0, 1)
    assert repository.append_events("run-1", "owner-1", [delayed_failure]) == (
        1,
        0,
    )

    listed = repository.list_runs(country="US", limit=10, before=None)
    detail = repository.get_run("run-1")
    repository.close()

    assert listed[0]["status"] == "completed"
    assert detail is not None
    assert detail["progress"]["status"] == "completed"
    assert detail["progress"]["failure"] is None
    assert detail["progress"]["resources"]["rss_bytes"] == 2048
    assert [event["event_id"] for event in detail["events"]] == [
        "delayed-failure",
        "completed",
    ]


def test_registration_rejects_owner_and_metadata_conflicts(
    postgres_url: str,
) -> None:
    upgrade_database(postgres_url)
    repository = PostgresTelemetryRepository(postgres_url)
    repository.register_run(_registration(), _principal())

    with pytest.raises(PermissionError):
        repository.register_run(_registration(), _principal("owner-2"))
    with pytest.raises(RunRegistrationConflictError):
        repository.register_run(
            _registration(pipeline="another-pipeline"), _principal()
        )

    repository.close()


def test_event_ingestion_rejects_another_owner_and_unregistered_producer(
    postgres_url: str,
) -> None:
    upgrade_database(postgres_url)
    repository = PostgresTelemetryRepository(postgres_url)
    repository.register_run(_registration(), _principal())
    event = _event(
        event_id="owned-event",
        sequence=1,
        timestamp=datetime.now(UTC),
        status="progress",
        stage_id="compile",
    )

    with pytest.raises(PermissionError, match="another Hugging Face user"):
        repository.append_events("run-1", "owner-2", [event])

    unregistered = event.model_copy(
        update={"event_id": "unknown-producer", "producer_id": "producer-2"}
    )
    with pytest.raises(PermissionError, match="not registered"):
        repository.append_events("run-1", "owner-1", [unregistered])

    repository.close()


def test_repository_readiness_requires_alembic_head(postgres_url: str) -> None:
    repository = PostgresTelemetryRepository(postgres_url)
    assert repository.is_ready() is False

    upgrade_database(postgres_url)

    assert repository.is_ready() is True
    repository.close()
