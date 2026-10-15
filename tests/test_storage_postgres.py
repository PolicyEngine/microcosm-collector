from __future__ import annotations

import random
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import event as sqlalchemy_event

from telemetry_collector import storage
from telemetry_collector.auth import HuggingFacePrincipal
from telemetry_collector.database import TelemetryRun, create_session_factory
from telemetry_collector.migrate import upgrade_database
from telemetry_collector.models import RunRegistration, TelemetryEvent
from telemetry_collector.storage import (
    MATERIALIZED_RUN_COLUMNS,
    PostgresTelemetryStore,
    RunRegistrationConflictError,
    materialize_run,
    run_summary,
)
from tests.event_schedules import (
    ONE_PRODUCER,
    RESTARTED_PRODUCERS,
    Event,
    every_kind_stream,
    in_batches,
    long_stream,
    out_of_order_schedules,
    random_delivery_schedule,
    restarted_producer_schedules,
    run_failed,
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


def test_store_is_idempotent_and_reduces_out_of_order_events(
    postgres_url: str,
) -> None:
    upgrade_database(postgres_url)
    store = PostgresTelemetryStore(postgres_url)
    registered_at = datetime.now(UTC)
    store.register_run(_registration(), _principal())
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

    assert store.append_events("run-1", "owner-1", [completed]) == (1, 0)
    assert store.append_events("run-1", "owner-1", [completed]) == (0, 1)
    assert store.append_events("run-1", "owner-1", [delayed_failure]) == (
        1,
        0,
    )

    listed = store.list_runs(country="US", limit=10, before=None)
    detail = store.get_run("run-1")
    store.close()

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
    store = PostgresTelemetryStore(postgres_url)
    store.register_run(_registration(), _principal())

    with pytest.raises(PermissionError):
        store.register_run(_registration(), _principal("owner-2"))
    with pytest.raises(RunRegistrationConflictError):
        store.register_run(_registration(pipeline="another-pipeline"), _principal())

    store.close()


def test_event_ingestion_rejects_another_owner_and_unregistered_producer(
    postgres_url: str,
) -> None:
    upgrade_database(postgres_url)
    store = PostgresTelemetryStore(postgres_url)
    store.register_run(_registration(), _principal())
    event = _event(
        event_id="owned-event",
        sequence=1,
        timestamp=datetime.now(UTC),
        status="progress",
        stage_id="compile",
    )

    with pytest.raises(PermissionError, match="another Hugging Face user"):
        store.append_events("run-1", "owner-2", [event])

    unregistered = event.model_copy(
        update={"event_id": "unknown-producer", "producer_id": "producer-2"}
    )
    with pytest.raises(PermissionError, match="not registered"):
        store.append_events("run-1", "owner-1", [unregistered])

    store.close()


def test_store_readiness_requires_alembic_head(postgres_url: str) -> None:
    store = PostgresTelemetryStore(postgres_url)
    assert store.is_ready() is False

    upgrade_database(postgres_url)

    assert store.is_ready() is True
    store.close()


def _register_producers(
    store: PostgresTelemetryStore,
    run_id: str,
    registrations: dict[str, datetime],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Register each producer at its stated time; the first registers the run."""

    for producer_id, registered_at in registrations.items():
        monkeypatch.setattr(
            storage, "_now", lambda registered_at=registered_at: registered_at
        )
        store.register_run(
            _registration(run_id=run_id, producer_id=producer_id), _principal()
        )


def _for_run(batch: Sequence[Event], run_id: str) -> list[TelemetryEvent]:
    """Address scheduled events to one run, with event ids unique across runs."""

    return [
        TelemetryEvent(
            schema_version=1,
            **{
                **event,
                "run_id": run_id,
                "event_id": f"{run_id}.{event['event_id']}",
            },
        )
        for event in batch
    ]


def _assert_stored_state_is_fully_materialized(
    store: PostgresTelemetryStore, run_id: str, context: str
) -> dict[str, Any]:
    """Compare the stored run with a re-materialization from every stored event."""

    with create_session_factory(store.engine)() as session:
        run = session.get(TelemetryRun, run_id)
        stored_columns = {
            column: getattr(run, column) for column in MATERIALIZED_RUN_COLUMNS
        }
        # What ingestion stored before batches were applied incrementally.
        expected = materialize_run(
            storage._run_mapping(run), store._events_for_run(session, run_id)
        )
    assert stored_columns == {
        column: expected[column] for column in MATERIALIZED_RUN_COLUMNS
    }, context
    return expected


def _assert_listed_as_fully_materialized(
    store: PostgresTelemetryStore, run_id: str, expected: dict[str, Any]
) -> None:
    listed = store.list_runs(country="US", limit=200, before=None)
    assert [summary for summary in listed if summary["run_id"] == run_id] == [
        run_summary(expected)
    ], run_id


@contextmanager
def _event_rows_read(store: PostgresTelemetryStore) -> Iterator[list[int]]:
    """Record how many rows each query reading telemetry_events returns."""

    row_counts: list[int] = []

    def record_rows(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT") and (
            "telemetry_events" in statement
        ):
            row_counts.append(cursor.rowcount)

    sqlalchemy_event.listen(store.engine, "after_cursor_execute", record_rows)
    try:
        yield row_counts
    finally:
        sqlalchemy_event.remove(store.engine, "after_cursor_execute", record_rows)


def test_incremental_batch_apply_reads_a_constant_number_of_event_rows_per_in_order_batch(
    postgres_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    upgrade_database(postgres_url)
    store = PostgresTelemetryStore(postgres_url)
    _register_producers(store, "run-1", ONE_PRODUCER, monkeypatch)
    batches = in_batches(long_stream("producer-a", 400), 20)

    rows_read_per_batch = []
    for batch in batches:
        with _event_rows_read(store) as row_counts:
            assert store.append_events(
                "run-1", "owner-1", _for_run(batch, "run-1")
            ) == (len(batch), 0)
        rows_read_per_batch.append(sum(row_counts))
    expected = _assert_stored_state_is_fully_materialized(store, "run-1", "400 events")
    _assert_listed_as_fully_materialized(store, "run-1", expected)
    store.close()

    # Re-reading the run would read 20 rows for the first batch and 400 for the last.
    assert len(set(rows_read_per_batch)) == 1, rows_read_per_batch
    assert rows_read_per_batch[0] <= len(batches[0])


def test_incremental_batch_apply_reads_only_the_event_rows_after_a_late_event(
    postgres_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    upgrade_database(postgres_url)
    store = PostgresTelemetryStore(postgres_url)

    rows_read_by_run_length = {}
    for run_length in (60, 600):
        run_id = f"run-{run_length}"
        _register_producers(store, run_id, ONE_PRODUCER, monkeypatch)
        stream = long_stream("producer-a", run_length)
        late_event = stream[-6]
        for batch in in_batches(
            [event for event in stream if event is not late_event], 20
        ):
            store.append_events(run_id, "owner-1", _for_run(batch, run_id))

        with _event_rows_read(store) as row_counts:
            assert store.append_events(
                run_id, "owner-1", _for_run([late_event], run_id)
            ) == (1, 0)

        rows_read_by_run_length[run_length] = sum(row_counts)
        expected = _assert_stored_state_is_fully_materialized(
            store, run_id, f"{run_length} events with a late one"
        )
        _assert_listed_as_fully_materialized(store, run_id, expected)
    store.close()

    # One producer row, then the late event and the five stored after it.
    assert rows_read_by_run_length == {60: 7, 600: 7}


def test_incremental_batch_apply_stores_the_fully_materialized_state(
    postgres_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    upgrade_database(postgres_url)
    store = PostgresTelemetryStore(postgres_url)
    every_kind = every_kind_stream("producer-a")
    deliveries = [
        ("every kind in batches of three", ONE_PRODUCER, in_batches(every_kind, 3)),
        (
            "each batch sent twice, a conflicting sequence, then everything again",
            ONE_PRODUCER,
            [
                *[batch for batch in in_batches(every_kind, 4) for _ in range(2)],
                [run_failed("producer-a", 3, 500.0) | {"event_id": "resent-3"}],
                every_kind,
            ],
        ),
        *[
            (description, ONE_PRODUCER, schedule)
            for description, schedule in out_of_order_schedules(shuffle_count=2).items()
        ],
        *[
            (description, RESTARTED_PRODUCERS, schedule)
            for description, schedule in restarted_producer_schedules().items()
        ],
    ]
    for schedule_seed in range(20):
        registrations, schedule, _ = random_delivery_schedule(
            random.Random(schedule_seed)
        )
        deliveries.append((f"schedule seed {schedule_seed}", registrations, schedule))

    for run_number, (description, registrations, schedule) in enumerate(deliveries):
        run_id = f"run-{run_number}"
        _register_producers(store, run_id, registrations, monkeypatch)
        for batch_number, batch in enumerate(schedule, start=1):
            store.append_events(run_id, "owner-1", _for_run(batch, run_id))
            expected = _assert_stored_state_is_fully_materialized(
                store, run_id, f"{description}: batch {batch_number}"
            )
        _assert_listed_as_fully_materialized(store, run_id, expected)
    store.close()
