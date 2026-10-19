"""Incremental event-batch application must equal full re-materialization.

The store folds each accepted batch onto a run's materialized columns instead
of replaying the run. These tests deliver event schedules to an in-memory run
that de-duplicates events as the store does, and after every batch compare its
columns with ``materialize_run`` over every stored event.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from copy import deepcopy
from datetime import datetime, timedelta
from typing import Any

from telemetry_collector.auth import HuggingFacePrincipal
from telemetry_collector.models import RunRegistration
from telemetry_collector.storage import (
    MATERIALIZED_RUN_COLUMNS,
    StoredProducer,
    apply_event_batch,
    initial_run_state,
    materialize_run,
)
from tests.event_schedules import (
    EVENT_KINDS,
    ONE_PRODUCER,
    RESTARTED_PRODUCERS,
    RUN_REGISTERED_AT,
    Event,
    complete_stage,
    every_kind_stream,
    heartbeat,
    in_batches,
    long_stream,
    out_of_order_schedules,
    progress_with_totals,
    random_delivery_schedule,
    restarted_producer_schedules,
    run_completed,
    run_failed,
)


class CountingStoredEvents:
    """In-memory stored-event reader that counts what the reducer reads."""

    def __init__(self, stored_events: dict[str, Event]) -> None:
        self._stored_events = stored_events
        self.events_read = 0
        self.earliest_timestamp_reads = 0

    def events_from(self, producer_id: str, sequence: int) -> list[Event]:
        selected = sorted(
            (
                event
                for event in self._stored_events.values()
                if event["producer_id"] == producer_id and event["sequence"] >= sequence
            ),
            key=lambda event: (event["sequence"], event["event_id"]),
        )
        self.events_read += len(selected)
        return deepcopy(selected)

    def earliest_timestamp(self, producer_id: str) -> datetime:
        self.earliest_timestamp_reads += 1
        return min(
            event["timestamp"]
            for event in self._stored_events.values()
            if event["producer_id"] == producer_id
        )


class IncrementalRun:
    """One run stored as the collector stores it, folding each accepted batch."""

    def __init__(self, producer_registrations: dict[str, datetime]) -> None:
        self.state = initial_run_state(
            RunRegistration(
                run_id="run-1",
                producer_id="producer-a",
                country_code="UK",
                pipeline="uk-calibration",
            ),
            HuggingFacePrincipal(
                user_id="owner-1",
                username="builder",
                organizations=("policyengine",),
            ),
            RUN_REGISTERED_AT,
        )
        self.producer_registrations = producer_registrations
        self.stored_events: dict[str, Event] = {}
        self.reader = CountingStoredEvents(self.stored_events)

    def deliver(self, batch: Sequence[Event]) -> tuple[int, int]:
        """Store the batch's new events and fold them; return (accepted, duplicates)."""

        producers = [
            StoredProducer(
                producer_id=producer_id,
                registered_at=registered_at,
                last_sequence=max(
                    (
                        event["sequence"]
                        for event in self.stored_events.values()
                        if event["producer_id"] == producer_id
                    ),
                    default=None,
                ),
            )
            for producer_id, registered_at in self.producer_registrations.items()
        ]
        stored_sequences = {
            (event["producer_id"], event["sequence"])
            for event in self.stored_events.values()
        }
        accepted_events = []
        for event in batch:
            sequence_key = (event["producer_id"], event["sequence"])
            if event["event_id"] in self.stored_events or (
                sequence_key in stored_sequences
            ):
                continue
            stored_event = {
                **deepcopy(event),
                "producer_registered_at": self.producer_registrations[
                    event["producer_id"]
                ],
            }
            self.stored_events[event["event_id"]] = stored_event
            stored_sequences.add(sequence_key)
            accepted_events.append(deepcopy(stored_event))
        if accepted_events:
            self.state.update(
                apply_event_batch(self.state, producers, accepted_events, self.reader)
            )
        return len(accepted_events), len(batch) - len(accepted_events)

    def stored_columns(self) -> dict[str, Any]:
        return {column: self.state[column] for column in MATERIALIZED_RUN_COLUMNS}

    def fully_materialized_columns(self) -> dict[str, Any]:
        materialized = materialize_run(self.state, self.stored_events.values())
        return {column: materialized[column] for column in MATERIALIZED_RUN_COLUMNS}


def deliver_and_compare(
    producer_registrations: dict[str, datetime],
    schedule: Sequence[Sequence[Event]],
    description: str,
) -> IncrementalRun:
    """Deliver every batch, checking the stored columns after each one."""

    run = IncrementalRun(producer_registrations)
    for batch_number, batch in enumerate(schedule, start=1):
        run.deliver(batch)
        assert run.stored_columns() == run.fully_materialized_columns(), (
            f"{description}: batch {batch_number} of {len(schedule)}"
        )
    return run


def test_incremental_batch_apply_matches_full_materialization_for_every_event_kind() -> (
    None
):
    kind_orders = [list(EVENT_KINDS.values())]
    for order_seed in range(8):
        shuffled_kinds = list(EVENT_KINDS.values())
        random.Random(order_seed).shuffle(shuffled_kinds)
        kind_orders.append(shuffled_kinds)

    for order_number, kinds in enumerate(kind_orders):
        stream = every_kind_stream("producer-a", kinds)
        for batch_size in (1, 2, 3, 5, len(stream)):
            deliver_and_compare(
                ONE_PRODUCER,
                in_batches(stream, batch_size),
                f"kind order {order_number}, batches of {batch_size}",
            )


def test_incremental_batch_apply_matches_full_materialization_for_out_of_order_batches() -> (
    None
):
    for description, schedule in out_of_order_schedules().items():
        deliver_and_compare(ONE_PRODUCER, schedule, description)


def test_incremental_batch_apply_matches_full_materialization_for_duplicate_resends() -> (
    None
):
    stream = every_kind_stream("producer-a")
    run = IncrementalRun(ONE_PRODUCER)

    for batch in in_batches(stream, 4):
        assert run.deliver(batch) == (len(batch), 0)
        assert run.stored_columns() == run.fully_materialized_columns()
        before_resend = run.stored_columns()
        assert run.deliver(batch) == (0, len(batch))
        assert run.stored_columns() == before_resend

    assert run.deliver(stream) == (0, len(stream))
    conflicting_sequence = run_failed("producer-a", 3, 500.0) | {
        "event_id": "producer-a-3-resent-with-another-id"
    }
    assert run.deliver([conflicting_sequence]) == (0, 1)
    assert run.stored_columns() == run.fully_materialized_columns()

    next_sequence = len(stream) + 1
    new_events = [
        progress_with_totals("producer-a", next_sequence, 600.0),
        heartbeat("producer-a", next_sequence + 1, 601.0),
    ]
    overlapping_batch = [stream[-1], new_events[0], new_events[0], new_events[1]]
    assert run.deliver(overlapping_batch) == (2, 2)
    assert run.stored_columns() == run.fully_materialized_columns()

    late_resend_with_late_event = [
        *stream[:3],
        complete_stage("producer-a", next_sequence + 3, 602.0),
        run_completed("producer-a", next_sequence + 2, 603.0),
    ]
    assert run.deliver(late_resend_with_late_event) == (2, 3)
    assert run.stored_columns() == run.fully_materialized_columns()


def test_incremental_batch_apply_matches_full_materialization_across_restarted_producers() -> (
    None
):
    for description, schedule in restarted_producer_schedules().items():
        deliver_and_compare(RESTARTED_PRODUCERS, schedule, description)


def test_incremental_batch_apply_matches_full_materialization_for_random_delivery_schedules() -> (
    None
):
    kinds_covered: set[str] = set()
    for schedule_seed in range(300):
        registrations, schedule, kinds_used = random_delivery_schedule(
            random.Random(schedule_seed)
        )
        kinds_covered |= kinds_used
        deliver_and_compare(registrations, schedule, f"schedule seed {schedule_seed}")

    assert kinds_covered == set(EVENT_KINDS)


def test_incremental_batch_apply_reads_no_stored_events_for_in_order_batches() -> None:
    first_attempt = long_stream("producer-a", 600)
    second_attempt = long_stream("producer-b", 600, first_minute=61.0)

    run = deliver_and_compare(
        RESTARTED_PRODUCERS,
        [*in_batches(first_attempt, 20), *in_batches(second_attempt, 20)],
        "two in-order attempts",
    )

    assert run.reader.events_read == 0
    assert run.reader.earliest_timestamp_reads == 0


def test_incremental_batch_apply_reads_only_the_events_after_a_late_event() -> None:
    events_read_by_run_length = {}
    for run_length in (60, 600):
        stream = long_stream("producer-a", run_length)
        late_event = stream[-6]
        run = deliver_and_compare(
            ONE_PRODUCER,
            in_batches([event for event in stream if event is not late_event], 20),
            f"{run_length} events with one withheld",
        )
        assert run.reader.events_read == 0

        assert run.deliver([late_event]) == (1, 0)

        assert run.stored_columns() == run.fully_materialized_columns()
        events_read_by_run_length[run_length] = run.reader.events_read

    # The late event and the five stored after it, however long the run is.
    assert events_read_by_run_length == {60: 6, 600: 6}


def test_incremental_batch_apply_reads_the_earliest_timestamp_once_when_an_earlier_producer_first_reports() -> (
    None
):
    registrations = {
        "producer-a": RUN_REGISTERED_AT,
        "producer-b": RUN_REGISTERED_AT + timedelta(minutes=30),
    }
    later_attempt = long_stream("producer-b", 300, first_minute=40.0)
    earlier_attempt = long_stream("producer-a", 30)
    run = deliver_and_compare(
        registrations, in_batches(later_attempt, 20), "later attempt first"
    )
    assert run.state["started_at"] == RUN_REGISTERED_AT

    for batch in in_batches(earlier_attempt, 5):
        run.deliver(batch)
        assert run.stored_columns() == run.fully_materialized_columns()

    # Once an earlier attempt has events, the later one's segment starts at its
    # own earliest event rather than at the run's registration.
    assert run.state["started_at"] == min(event["timestamp"] for event in later_attempt)
    assert run.reader.earliest_timestamp_reads == 1
    assert run.reader.events_read == 0
