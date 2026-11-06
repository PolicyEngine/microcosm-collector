"""The collector checks the details keys the emitter writes, and serves them.

``fixtures/emitter_events/build-machine-readings.json`` holds the events an
emitter with heartbeat readings stored for one short run: its start with the
machine description, two stages with their spans, progress with an outer
counter, a heartbeat with a reading, and one sent on waking with a sleep.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from telemetry_collector.app import create_app
from telemetry_collector.auth import HuggingFacePrincipal
from telemetry_collector.models import RunRegistration, TelemetryEvent
from telemetry_collector.storage import initial_run_state, run_documents
from tests.fakes import FakeTelemetryStore
from tests.test_app import StubHuggingFaceAuthenticator, exchange, settings

FIXTURE = Path(__file__).parent / "fixtures" / "emitter_events"
REGISTERED_AT = datetime(2026, 11, 6, 11, 59, tzinfo=UTC)
DROPPED = "dropped_emitter_details"


def emitter_events() -> list[dict[str, Any]]:
    return json.loads((FIXTURE / "build-machine-readings.json").read_text())


def first_with(event_type: str, key: str) -> dict[str, Any]:
    return deepcopy(
        next(
            event
            for event in emitter_events()
            if event["event_type"] == event_type and key in event["details"]
        )
    )


def stored(
    raw: dict[str, Any], producer_registered_at: datetime = REGISTERED_AT
) -> dict[str, Any]:
    """Return an event as the store keeps it: checked, then dumped."""
    event = TelemetryEvent.model_validate(raw).model_dump(mode="python")
    event["producer_registered_at"] = producer_registered_at
    return event


def registered_run() -> dict[str, Any]:
    return initial_run_state(
        RunRegistration(
            run_id="fixture-run",
            producer_id="fixture-producer",
            country_code="US",
            pipeline="fixture-pipeline",
        ),
        HuggingFacePrincipal(
            user_id="owner-1", username="builder", organizations=("policyengine",)
        ),
        REGISTERED_AT,
    )


def test_the_emitters_events_are_accepted_unchanged() -> None:
    for raw in emitter_events():
        event = TelemetryEvent.model_validate(raw)
        assert event.details == raw["details"], raw["event_type"]


def test_the_fixture_holds_every_emitter_key() -> None:
    keys = {
        (event["event_type"], key)
        for event in emitter_events()
        for key in event["details"]
        if key in {"reading", "sleep", "environment", "stage_span", "outer"}
    }
    assert keys == {
        ("heartbeat", "reading"),
        ("heartbeat", "sleep"),
        ("run", "environment"),
        ("stage", "stage_span"),
        ("progress", "outer"),
    }


INVALID_CHANGES: list[tuple[str, str, Callable[[dict[str, Any]], None]]] = [
    (
        "heartbeat",
        "reading",
        lambda value: value["process_tree"].update(memory_bytes=-1),
    ),
    ("heartbeat", "reading", lambda value: value.update(unexpected=1)),
    (
        "heartbeat",
        "reading",
        lambda value: value["machine"].update(memory_pressure_level="severe"),
    ),
    ("heartbeat", "reading", lambda value: value.update(stages=["not an identifier"])),
    ("heartbeat", "reading", lambda value: value["machine"].update(cpu_busy_share=1.5)),
    ("heartbeat", "reading", lambda value: value.pop("process_tree")),
    (
        "heartbeat",
        "sleep",
        lambda value: value.update(ended_at="2026-11-06T11:00:00+00:00"),
    ),
    ("heartbeat", "sleep", lambda value: value.update(seconds=-1)),
    (
        "heartbeat",
        "sleep",
        lambda value: value.update(started_at="2026-11-06T12:00:00"),
    ),
    ("run", "environment", lambda value: value.update(kind="server")),
    ("run", "environment", lambda value: value.update(kind="modal_container")),
    ("run", "environment", lambda value: value.update(logical_cpus=0)),
    ("stage", "stage_span", lambda value: value.update(occurrence=0)),
    ("stage", "stage_span", lambda value: value.update(closed_by="because")),
    ("stage", "stage_span", lambda value: value.update(parent_stage_id="no spaces")),
    ("progress", "outer", lambda value: value.update(done="57")),
    ("progress", "outer", lambda value: value.update(done=True)),
    ("progress", "outer", lambda value: value.update(unit="x" * 201)),
]


@pytest.mark.parametrize(
    ("event_type", "key", "change"),
    INVALID_CHANGES,
    ids=[
        f"{event_type}-{key}-{index}"
        for index, (event_type, key, _) in enumerate(INVALID_CHANGES)
    ],
)
def test_an_invalid_emitter_key_is_dropped_and_named(
    event_type: str, key: str, change: Callable[[dict[str, Any]], None]
) -> None:
    raw = first_with(event_type, key)
    change(raw["details"][key])
    event = TelemetryEvent.model_validate(raw)
    assert key not in event.details
    assert event.details.get(DROPPED) == [key]
    kept = {name: value for name, value in event.details.items() if name != DROPPED}
    assert kept == {
        name: value for name, value in raw["details"].items() if name != key
    }


def test_a_reading_and_a_sleep_are_checked_separately() -> None:
    raw = first_with("heartbeat", "sleep")
    raw["details"]["reading"]["unexpected"] = 1
    raw["details"]["sleep"]["seconds"] = -1
    event = TelemetryEvent.model_validate(raw)
    assert event.details.get(DROPPED) == ["reading", "sleep"]


def test_a_modal_container_needs_its_description_and_a_local_machine_has_none() -> None:
    raw = first_with("run", "environment")
    raw["details"]["environment"].update(
        kind="modal_container",
        modal={"task_id": "ta-fixture", "region": "us-east", "cloud_provider": None},
    )
    assert DROPPED not in TelemetryEvent.model_validate(raw).details
    raw["details"]["environment"]["kind"] = "local"
    assert TelemetryEvent.model_validate(raw).details.get(DROPPED) == ["environment"]


def test_a_key_on_another_event_type_is_left_alone() -> None:
    raw = first_with("stage", "stage_span")
    raw["details"]["reading"] = {"anything": True}
    event = TelemetryEvent.model_validate(raw)
    assert event.details["reading"] == {"anything": True}
    assert DROPPED not in event.details


def test_the_emitter_cannot_send_the_dropped_key_itself() -> None:
    raw = first_with("heartbeat", "reading")
    raw["details"][DROPPED] = ["reading"]
    event = TelemetryEvent.model_validate(raw)
    assert DROPPED not in event.details


def test_a_batch_with_an_invalid_reading_is_accepted_without_it() -> None:
    store = FakeTelemetryStore()
    client = TestClient(
        create_app(
            settings=settings(),
            store=store,
            huggingface_authenticator=StubHuggingFaceAuthenticator(),
        )
    )
    token = exchange(client)
    registration = {
        "run_id": "fixture-run",
        "producer_id": "fixture-producer",
        "country_code": "US",
        "pipeline": "fixture-pipeline",
    }
    headers = {"Authorization": f"Bearer {token}"}
    assert (
        client.post("/v1/runs", headers=headers, json=registration).status_code == 201
    )
    events = emitter_events()
    heartbeat = next(event for event in events if "reading" in event["details"])
    heartbeat["details"]["reading"]["process_tree"]["memory_bytes"] = "lots"
    response = client.post(
        "/v1/runs/fixture-run/events", headers=headers, json={"events": events}
    )
    assert response.status_code == 202, response.text
    assert response.json() == {"accepted": len(events), "duplicates": 0}
    document = client.get(
        "/v1/runs/fixture-run",
        headers={"X-Telemetry-Read-Token": "dashboard-read-token"},
    ).json()
    served = next(
        event
        for event in document["events"]
        if event["event_id"] == heartbeat["event_id"]
    )
    assert "reading" not in served["details"]
    assert served["details"][DROPPED] == ["reading"]


def test_the_run_document_serves_the_environment_latest_reading_and_sleeps() -> None:
    raws = emitter_events()
    document = run_documents(registered_run(), [stored(raw) for raw in raws])
    started = raws[0]
    heartbeats = [raw for raw in raws if raw["event_type"] == "heartbeat"]
    latest = heartbeats[-1]
    assert (
        document["run_manifest"].get("environment") == started["details"]["environment"]
    )
    reading = document["progress"].get("reading") or {}
    assert reading.get("timestamp") == TelemetryEvent.model_validate(latest).timestamp
    assert {name: value for name, value in reading.items() if name != "timestamp"} == (
        latest["details"]["reading"]
    )
    assert document.get("sleeps") == [
        {"producer_id": "fixture-producer", **latest["details"]["sleep"]}
    ]


def test_a_restarted_attempt_has_no_reading_until_it_sends_one() -> None:
    raws = emitter_events()
    restart = deepcopy(raws[0])
    restart.update(
        event_id="restart-started",
        producer_id="restart-producer",
        sequence=1,
        timestamp=(datetime(2026, 11, 6, 13, tzinfo=UTC)).isoformat(),
    )
    restart["details"]["environment"]["os_version"] = "restart-version"
    events = [stored(raw) for raw in raws] + [
        stored(restart, REGISTERED_AT + timedelta(hours=2))
    ]
    document = run_documents(registered_run(), events)
    assert "reading" in document["progress"]
    assert document["progress"]["reading"] is None
    assert (document["run_manifest"].get("environment") or {}).get("os_version") == (
        "restart-version"
    )
    # A sleep is a fact about the machine, so every attempt's sleeps are kept.
    assert [sleep["producer_id"] for sleep in document.get("sleeps", [])] == [
        "fixture-producer"
    ]


def test_a_run_without_readings_serves_none() -> None:
    raws = [raw for raw in emitter_events() if raw["event_type"] not in {"heartbeat"}]
    for raw in raws:
        raw["details"] = {
            key: value
            for key, value in raw["details"].items()
            if key not in {"environment", "stage_span", "outer"}
        }
    document = run_documents(registered_run(), [stored(raw) for raw in raws])
    assert "environment" in document["run_manifest"]
    assert document["run_manifest"]["environment"] is None
    assert "reading" in document["progress"]
    assert document["progress"]["reading"] is None
    assert document.get("sleeps") == []
