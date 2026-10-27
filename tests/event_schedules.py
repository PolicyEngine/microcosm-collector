"""Telemetry event kinds and delivery schedules for the batch-application tests."""

from __future__ import annotations

import random
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from telemetry_collector.models import TelemetryEvent

RUN_REGISTERED_AT = datetime(2026, 10, 1, 12, tzinfo=UTC)

Event = dict[str, Any]
EventKind = Callable[[str, int, float], Event]


def telemetry_event(
    producer_id: str,
    sequence: int,
    minutes_after_registration: float,
    *,
    event_type: str,
    status: str,
    stage_id: str | None = None,
    details: dict[str, Any] | None = None,
    resources: dict[str, Any] | None = None,
    message: str | None = None,
    event_id: str | None = None,
) -> Event:
    """Return an event as a producer sends it, validated by the collector model."""

    event = TelemetryEvent(
        schema_version=1,
        event_id=event_id or f"{producer_id}-{sequence}",
        run_id="run-1",
        producer_id=producer_id,
        sequence=sequence,
        timestamp=RUN_REGISTERED_AT + timedelta(minutes=minutes_after_registration),
        event_type=event_type,
        stage_id=stage_id,
        status=status,
        message=message,
        details=details or {},
        resources=resources,
    ).model_dump(mode="python")
    del event["schema_version"]
    return event


def resource_snapshot(sequence: int) -> dict[str, Any]:
    return {
        "cpu_user_seconds": sequence * 1.5,
        "rss_bytes": sequence * 1024,
        "peak_rss_bytes": sequence * 2048,
    }


def run_started(producer_id: str, sequence: int, minutes: float) -> Event:
    return telemetry_event(
        producer_id,
        sequence,
        minutes,
        event_type="run",
        status="started",
        stage_id="created",
        message="Run started",
    )


def stage_started(producer_id: str, sequence: int, minutes: float) -> Event:
    return telemetry_event(
        producer_id,
        sequence,
        minutes,
        event_type="stage",
        status="started",
        stage_id="calibrate",
    )


def stage_completed(producer_id: str, sequence: int, minutes: float) -> Event:
    return telemetry_event(
        producer_id,
        sequence,
        minutes,
        event_type="stage",
        status="completed",
        stage_id="export",
    )


def stage_failed_without_failing_the_run(
    producer_id: str, sequence: int, minutes: float
) -> Event:
    return telemetry_event(
        producer_id,
        sequence,
        minutes,
        event_type="stage",
        status="failed",
        stage_id="validate",
    )


def progress_with_totals(producer_id: str, sequence: int, minutes: float) -> Event:
    return telemetry_event(
        producer_id,
        sequence,
        minutes,
        event_type="progress",
        status="progress",
        stage_id="calibrate",
        details={
            "done": sequence,
            "total": 500,
            "unit": "epochs",
            "elapsed_seconds": sequence * 2.5,
            "loss": 0.125,
        },
    )


def progress_without_totals(producer_id: str, sequence: int, minutes: float) -> Event:
    return telemetry_event(
        producer_id,
        sequence,
        minutes,
        event_type="progress",
        status="progress",
        stage_id="calibrate",
        details={"done": sequence},
    )


def calibration_epoch(producer_id: str, sequence: int, minutes: float) -> Event:
    return telemetry_event(
        producer_id,
        sequence,
        minutes,
        event_type="calibration",
        status="progress",
        stage_id="calibrate",
        details={"epoch": sequence, "loss": 0.5 / sequence},
    )


def heartbeat(producer_id: str, sequence: int, minutes: float) -> Event:
    return telemetry_event(
        producer_id,
        sequence,
        minutes,
        event_type="heartbeat",
        status="progress",
        resources=resource_snapshot(sequence),
    )


def stage_resource_snapshot(producer_id: str, sequence: int, minutes: float) -> Event:
    return telemetry_event(
        producer_id,
        sequence,
        minutes,
        event_type="stage",
        status="progress",
        stage_id="export",
        resources=resource_snapshot(sequence),
    )


def run_failed(producer_id: str, sequence: int, minutes: float) -> Event:
    return telemetry_event(
        producer_id,
        sequence,
        minutes,
        event_type="run",
        status="failed",
        stage_id="calibrate",
        message="Calibration diverged",
        details={
            "error_type": "FloatingPointError",
            "error_code": "DIVERGED",
            "failure_class": "build",
            "failed_during": "calibrating",
        },
    )


def run_failed_without_details(
    producer_id: str, sequence: int, minutes: float
) -> Event:
    return telemetry_event(
        producer_id, sequence, minutes, event_type="run", status="failed"
    )


def failed_stage(producer_id: str, sequence: int, minutes: float) -> Event:
    return telemetry_event(
        producer_id,
        sequence,
        minutes,
        event_type="stage",
        status="failed",
        stage_id="failed",
        message="Interrupted",
        details={"error_type": "KeyboardInterrupt", "error_code": "INTERRUPTED"},
    )


def run_blocked(producer_id: str, sequence: int, minutes: float) -> Event:
    return telemetry_event(
        producer_id,
        sequence,
        minutes,
        event_type="run",
        status="blocked",
        details={
            "phase": "terminal",
            "blocking_failure_count": 1,
            "blocking_gate_ids": ["uk_target_fit"],
            "gate_statuses": {"uk_target_fit": "failed"},
        },
    )


def blocked_stage(producer_id: str, sequence: int, minutes: float) -> Event:
    return telemetry_event(
        producer_id,
        sequence,
        minutes,
        event_type="stage",
        status="blocked",
        stage_id="blocked",
        details={
            "phase": "preflight",
            "blocking_failure_count": 2,
            "blocking_gate_ids": [],
        },
    )


def blocked_status_on_another_stage(
    producer_id: str, sequence: int, minutes: float
) -> Event:
    return telemetry_event(
        producer_id,
        sequence,
        minutes,
        event_type="stage",
        status="blocked",
        stage_id="gate_battery",
        details={
            "phase": "terminal",
            "blocking_failure_count": 3,
            "blocking_gate_ids": ["us_target_fit"],
        },
    )


def run_completed(producer_id: str, sequence: int, minutes: float) -> Event:
    return telemetry_event(
        producer_id, sequence, minutes, event_type="run", status="completed"
    )


def complete_stage(producer_id: str, sequence: int, minutes: float) -> Event:
    return telemetry_event(
        producer_id,
        sequence,
        minutes,
        event_type="stage",
        status="completed",
        stage_id="complete",
    )


def complete_stage_id_without_completion(
    producer_id: str, sequence: int, minutes: float
) -> Event:
    return telemetry_event(
        producer_id,
        sequence,
        minutes,
        event_type="stage",
        status="started",
        stage_id="complete",
    )


# Every branch of the materializer: reopening, stage tracking, work, heartbeats,
# resources, failure, block and completion, and terminal-looking events that
# don't end the run.
EVENT_KINDS: dict[str, EventKind] = {
    kind.__name__: kind
    for kind in (
        run_started,
        stage_started,
        stage_completed,
        stage_failed_without_failing_the_run,
        progress_with_totals,
        progress_without_totals,
        calibration_epoch,
        heartbeat,
        stage_resource_snapshot,
        run_failed,
        run_failed_without_details,
        failed_stage,
        run_blocked,
        blocked_stage,
        blocked_status_on_another_stage,
        run_completed,
        complete_stage,
        complete_stage_id_without_completion,
    )
}


def every_kind_stream(
    producer_id: str,
    kinds: Sequence[EventKind] = tuple(EVENT_KINDS.values()),
    first_minute: float = 1.0,
) -> list[Event]:
    return [
        kind(producer_id, sequence, first_minute + sequence)
        for sequence, kind in enumerate(kinds, start=1)
    ]


def long_stream(
    producer_id: str, event_count: int, first_minute: float = 1.0
) -> list[Event]:
    kinds = list(EVENT_KINDS.values())
    return [
        kinds[(sequence - 1) % len(kinds)](
            producer_id, sequence, first_minute + sequence / 10
        )
        for sequence in range(1, event_count + 1)
    ]


def in_batches(events: Sequence[Event], batch_size: int) -> list[list[Event]]:
    return [
        list(events[start : start + batch_size])
        for start in range(0, len(events), batch_size)
    ]


ONE_PRODUCER = {"producer-a": RUN_REGISTERED_AT}
RESTARTED_PRODUCERS = {
    "producer-a": RUN_REGISTERED_AT,
    "producer-b": RUN_REGISTERED_AT + timedelta(minutes=60),
    "producer-c": RUN_REGISTERED_AT + timedelta(minutes=60),
}


def out_of_order_schedules(shuffle_count: int = 20) -> dict[str, list[list[Event]]]:
    """One producer's events of every kind, delivered out of order."""

    stream = every_kind_stream("producer-a") + every_kind_stream(
        "producer-a", first_minute=100.0
    )
    stream = [
        {**event, "sequence": sequence, "event_id": f"producer-a-{sequence}"}
        for sequence, event in enumerate(stream, start=1)
    ]
    batches = in_batches(stream, 4)
    schedules = {
        "batches in reverse": list(reversed(batches)),
        "events in reverse": [[event] for event in reversed(stream)],
        "first event last": [stream[1:], [stream[0]]],
        "middle batch last": [batches[0], *batches[2:], batches[1]],
        "events reversed inside each batch": [
            list(reversed(batch)) for batch in batches
        ],
    }
    for shuffle_seed in range(shuffle_count):
        shuffled = list(stream)
        random.Random(shuffle_seed).shuffle(shuffled)
        schedules[f"shuffled single events {shuffle_seed}"] = [
            [event] for event in shuffled
        ]
        schedules[f"shuffled batches of five {shuffle_seed}"] = in_batches(shuffled, 5)

    skew = random.Random(99)
    skewed_stream = [
        {
            **event,
            "timestamp": RUN_REGISTERED_AT + timedelta(minutes=skew.uniform(-30, 300)),
        }
        for event in stream
    ]
    schedules["skewed clocks, in order"] = in_batches(skewed_stream, 3)
    schedules["skewed clocks, batches in reverse"] = list(
        reversed(in_batches(skewed_stream, 3))
    )
    return schedules


def restarted_producer_schedules() -> dict[str, list[list[Event]]]:
    """Attempts registered as ``RESTARTED_PRODUCERS``, delivered in several orders."""

    first_attempt = every_kind_stream("producer-a")
    second_attempt = every_kind_stream("producer-b", first_minute=61.0)
    third_attempt = every_kind_stream("producer-c", first_minute=62.0)
    first_without_fifth = first_attempt[:4] + first_attempt[5:]
    mixed_batch = first_attempt[:6] + second_attempt[:6]
    random.Random(7).shuffle(mixed_batch)
    early_clock_attempt = [
        {**event, "timestamp": RUN_REGISTERED_AT - timedelta(minutes=sequence)}
        for sequence, event in enumerate(second_attempt, start=1)
    ]

    schedules = {
        "restart after the first attempt ends": [
            *in_batches(first_attempt, 5),
            *in_batches(second_attempt, 5),
        ],
        "later attempt reports before the earlier one": [
            *in_batches(second_attempt, 5),
            *in_batches(first_attempt, 5),
        ],
        "earlier attempt reports late": [
            *in_batches(first_attempt[:8], 4),
            *in_batches(second_attempt, 4),
            *in_batches(first_attempt[8:], 4),
        ],
        "one batch spans both attempts": [
            mixed_batch,
            first_attempt[6:],
            second_attempt[6:],
        ],
        "late event of the earlier attempt beside the later attempt": [
            first_without_fifth,
            [first_attempt[4], *second_attempt[:3]],
            second_attempt[3:],
        ],
        "earlier attempt's late event after the later attempt reports": [
            first_without_fifth,
            second_attempt[:3],
            [first_attempt[4]],
        ],
        "three attempts with tied registrations": [
            *in_batches(first_attempt, 6),
            *[
                batch
                for pair in zip(
                    in_batches(third_attempt, 3),
                    in_batches(second_attempt, 3),
                    strict=True,
                )
                for batch in pair
            ],
        ],
        "later attempt clocks before the registration": [
            *in_batches(first_attempt, 5),
            *in_batches(early_clock_attempt, 5),
        ],
        "earlier attempt's last event carries the latest clock": [
            *in_batches(first_attempt, 5),
            *in_batches(second_attempt, 5),
            [heartbeat("producer-a", len(first_attempt) + 1, 1000.0)],
        ],
    }
    return schedules


def random_delivery_schedule(
    rng: random.Random,
) -> tuple[dict[str, datetime], list[list[Event]], set[str]]:
    """Generate events of random kinds and deliver them out of order, with re-sends."""

    producer_ids = ["producer-a", "producer-b", "producer-c"][: rng.randint(1, 3)]
    registrations = {"producer-a": RUN_REGISTERED_AT}
    for producer_id in producer_ids[1:]:
        registrations[producer_id] = RUN_REGISTERED_AT + timedelta(
            minutes=rng.choice([0, 30, 30, 90])
        )
    kinds_used: set[str] = set()
    batches_by_producer: list[list[list[Event]]] = []
    for producer_id in producer_ids:
        stream = []
        for sequence in range(1, rng.randint(1, 30) + 1):
            kind_name = rng.choice(list(EVENT_KINDS))
            kinds_used.add(kind_name)
            stream.append(
                EVENT_KINDS[kind_name](producer_id, sequence, rng.uniform(-30, 300))
            )
        batches = []
        while stream:
            batch_size = rng.randint(1, 8)
            batches.append(stream[:batch_size])
            stream = stream[batch_size:]
        batches_by_producer.append(batches)

    schedule: list[list[Event]] = []
    while any(batches_by_producer):
        producer_batches = rng.choice(
            [batches for batches in batches_by_producer if batches]
        )
        schedule.append(producer_batches.pop(0))

    for conflict_number in range(rng.randint(0, 6)):
        perturbation = rng.choice(
            ["swap", "merge", "shuffle", "resend", "withhold", "conflict"]
        )
        position = rng.randrange(len(schedule))
        if perturbation == "swap":
            other_position = rng.randrange(len(schedule))
            schedule[position], schedule[other_position] = (
                schedule[other_position],
                schedule[position],
            )
        elif perturbation == "merge" and position + 1 < len(schedule):
            schedule[position] = schedule[position] + schedule.pop(position + 1)
        elif perturbation == "shuffle":
            shuffled_batch = list(schedule[position])
            rng.shuffle(shuffled_batch)
            schedule[position] = shuffled_batch
        elif perturbation == "resend":
            schedule.insert(rng.randrange(len(schedule) + 1), list(schedule[position]))
        elif perturbation == "withhold" and len(schedule[position]) > 1:
            remaining_batch = list(schedule[position])
            withheld_event = remaining_batch.pop(rng.randrange(len(remaining_batch)))
            schedule[position] = remaining_batch
            schedule.insert(
                rng.randrange(position + 1, len(schedule) + 1), [withheld_event]
            )
        elif perturbation == "conflict":
            original = rng.choice(schedule[position])
            kind_name = rng.choice(list(EVENT_KINDS))
            kinds_used.add(kind_name)
            conflicting_event = EVENT_KINDS[kind_name](
                original["producer_id"], original["sequence"], rng.uniform(-30, 300)
            ) | {
                "event_id": f"{original['event_id']}-conflict-{conflict_number}",
            }
            schedule.insert(rng.randrange(len(schedule) + 1), [conflicting_event])
    return registrations, schedule, kinds_used
