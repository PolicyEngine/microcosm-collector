from __future__ import annotations

import base64
import json
from dataclasses import replace
from datetime import UTC, datetime

import jwt
import pytest
from fastapi.testclient import TestClient

from telemetry_collector.app import CollectorSettings, create_app
from telemetry_collector.auth import (
    HuggingFaceAuthenticationUnavailableError,
    HuggingFacePrincipal,
)
from telemetry_collector.models import RunRegistration, TelemetryEvent
from tests.fakes import FakeTelemetryStore


class StubHuggingFaceAuthenticator:
    def __init__(self, *, member: bool = True) -> None:
        self.member = member
        self.tokens: list[str] = []

    def authenticate(self, token: str, required_org: str) -> HuggingFacePrincipal:
        self.tokens.append(token)
        return HuggingFacePrincipal(
            user_id="hf-user-1",
            username="builder",
            organizations=(required_org,) if self.member else ("somewhere-else",),
        )


def settings() -> CollectorSettings:
    return CollectorSettings(
        jwt_secret="test-secret-that-is-long-enough-32",
        read_token="dashboard-read-token",
        required_huggingface_org="policyengine",
        collector_issuer="test-collector",
        session_token_ttl_seconds=900,
    )


def registration() -> dict[str, object]:
    return {
        "run_id": "route-a-1",
        "producer_id": "producer-1",
        "country_code": "US",
        "pipeline": "us-fiscal-refresh",
        "candidate_id": "candidate-1",
        "release_id": None,
        "run_kind": "build",
    }


def exchange(client: TestClient) -> str:
    response = client.post(
        "/v1/auth/huggingface/exchange",
        headers={"Authorization": "Bearer hf_example"},
    )
    assert response.status_code == 200
    return response.json()["access_token"]


def register(client: TestClient, session_token: str) -> None:
    response = client.post(
        "/v1/runs",
        headers={"Authorization": f"Bearer {session_token}"},
        json=registration(),
    )
    assert response.status_code == 201
    assert response.json() == {"registered": True}


def test_exchange_ingest_and_read_run() -> None:
    store = FakeTelemetryStore()
    authenticator = StubHuggingFaceAuthenticator()
    client = TestClient(
        create_app(
            settings=settings(),
            store=store,
            huggingface_authenticator=authenticator,
        )
    )
    session_token = exchange(client)
    register(client, session_token)
    timestamp = datetime.now(UTC).isoformat()
    event = {
        "schema_version": 1,
        "event_id": "evt-1",
        "run_id": "route-a-1",
        "producer_id": "producer-1",
        "sequence": 1,
        "timestamp": timestamp,
        "event_type": "stage",
        "stage_id": "target_compilation",
        "status": "started",
        "message": "Compiling targets.",
        "details": {},
        "resources": {
            "cpu_user_seconds": 2.0,
            "cpu_system_seconds": 0.5,
            "rss_bytes": 1024,
            "peak_rss_bytes": 2048,
        },
    }

    first = client.post(
        "/v1/runs/route-a-1/events",
        headers={"Authorization": f"Bearer {session_token}"},
        json={"events": [event]},
    )
    duplicate = client.post(
        "/v1/runs/route-a-1/events",
        headers={"Authorization": f"Bearer {session_token}"},
        json={"events": [event]},
    )

    assert first.status_code == 202
    assert first.json() == {"accepted": 1, "duplicates": 0}
    assert duplicate.status_code == 202
    assert duplicate.json() == {"accepted": 0, "duplicates": 1}
    assert authenticator.tokens == ["hf_example"]

    session_claims = jwt.decode(
        session_token,
        settings().jwt_secret,
        algorithms=["HS256"],
        issuer=settings().collector_issuer,
        audience="microcosm-telemetry-collector",
    )
    assert session_claims["scope"] == "telemetry:write"

    denied = client.get("/v1/runs?country=US")
    assert denied.status_code == 401

    headers = {"X-Telemetry-Read-Token": "dashboard-read-token"}
    listed = client.get("/v1/runs?country=US", headers=headers)
    assert listed.status_code == 200
    assert listed.json()["runs"][0]["run_id"] == "route-a-1"

    detail = client.get("/v1/runs/route-a-1", headers=headers)
    assert detail.status_code == 200
    payload = detail.json()
    assert payload["progress"]["current_stage"] == "target_compilation"
    assert payload["events"][0]["stage_id"] == "target_compilation"
    assert payload["events"][0]["resources"]["rss_bytes"] == 1024


def test_non_member_cannot_exchange_token() -> None:
    client = TestClient(
        create_app(
            settings=settings(),
            store=FakeTelemetryStore(),
            huggingface_authenticator=StubHuggingFaceAuthenticator(member=False),
        )
    )
    response = client.post(
        "/v1/auth/huggingface/exchange",
        headers={"Authorization": "Bearer hf_outsider"},
    )
    assert response.status_code == 403


def test_identity_provider_outage_is_retryable() -> None:
    authenticator = StubHuggingFaceAuthenticator()

    def unavailable(token: str, required_org: str) -> HuggingFacePrincipal:
        raise HuggingFaceAuthenticationUnavailableError(
            "Hugging Face identity verification is unavailable."
        )

    authenticator.authenticate = unavailable  # type: ignore[method-assign]
    client = TestClient(
        create_app(
            settings=settings(),
            store=FakeTelemetryStore(),
            huggingface_authenticator=authenticator,
        )
    )

    response = client.post(
        "/v1/auth/huggingface/exchange",
        headers={"Authorization": "Bearer hf_example"},
    )

    assert response.status_code == 503


def test_run_registration_requires_collector_membership_token() -> None:
    store = FakeTelemetryStore()
    client = TestClient(
        create_app(
            settings=settings(),
            store=store,
            huggingface_authenticator=StubHuggingFaceAuthenticator(),
        )
    )

    response = client.post(
        "/v1/runs",
        headers={"Authorization": "Bearer hf_raw_credential"},
        json=registration(),
    )

    assert response.status_code == 401
    assert store.list_runs(country=None, limit=10, before=None) == []


def test_run_identifier_cannot_be_reused_with_different_metadata() -> None:
    store = FakeTelemetryStore()
    client = TestClient(
        create_app(
            settings=settings(),
            store=store,
            huggingface_authenticator=StubHuggingFaceAuthenticator(),
        )
    )
    session_token = exchange(client)
    register(client, session_token)

    response = client.post(
        "/v1/runs",
        headers={"Authorization": f"Bearer {session_token}"},
        json={
            "run_id": "route-a-1",
            "producer_id": "producer-2",
            "country_code": "US",
            "pipeline": "different-pipeline",
            "candidate_id": "candidate-1",
            "release_id": None,
            "run_kind": "build",
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"] == (
        "Run identifier is already registered with different metadata."
    )


def test_session_token_cannot_ingest_another_users_run() -> None:
    store = FakeTelemetryStore()
    store.register_run(
        RunRegistration(
            run_id="another-run",
            producer_id="another-producer",
            country_code="US",
            pipeline="us-fiscal-refresh",
        ),
        HuggingFacePrincipal(
            user_id="another-owner",
            username="another-builder",
            organizations=("policyengine",),
        ),
    )
    client = TestClient(
        create_app(
            settings=settings(),
            store=store,
            huggingface_authenticator=StubHuggingFaceAuthenticator(),
        )
    )
    token = exchange(client)
    response = client.post(
        "/v1/runs/another-run/events",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "events": [
                {
                    "schema_version": 1,
                    "event_id": "other-event",
                    "run_id": "another-run",
                    "producer_id": "another-producer",
                    "sequence": 1,
                    "timestamp": datetime.now(UTC).isoformat(),
                    "event_type": "heartbeat",
                    "status": "progress",
                    "details": {},
                }
            ]
        },
    )
    assert response.status_code == 403


def test_failed_validation_event_does_not_finish_the_run() -> None:
    store = FakeTelemetryStore()
    client = TestClient(
        create_app(
            settings=settings(),
            store=store,
            huggingface_authenticator=StubHuggingFaceAuthenticator(),
        )
    )
    token = exchange(client)
    register(client, token)
    response = client.post(
        "/v1/runs/route-a-1/events",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "events": [
                {
                    "schema_version": 1,
                    "event_id": "validation-failure",
                    "run_id": "route-a-1",
                    "producer_id": "producer-1",
                    "sequence": 1,
                    "timestamp": datetime.now(UTC).isoformat(),
                    "event_type": "stage",
                    "stage_id": "weight_validation",
                    "status": "failed",
                    "message": "Validation failed.",
                    "details": {},
                    "resources": None,
                }
            ]
        },
    )
    detail = client.get(
        "/v1/runs/route-a-1",
        headers={"X-Telemetry-Read-Token": "dashboard-read-token"},
    )

    assert response.status_code == 202
    assert detail.json()["progress"]["status"] == "running"


def test_blocked_run_event_is_accepted_and_ends_the_run() -> None:
    store = FakeTelemetryStore()
    client = TestClient(
        create_app(
            settings=settings(),
            store=store,
            huggingface_authenticator=StubHuggingFaceAuthenticator(),
        )
    )
    token = exchange(client)
    register(client, token)
    response = client.post(
        "/v1/runs/route-a-1/events",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "events": [
                {
                    "schema_version": 1,
                    "event_id": "gates-refused",
                    "run_id": "route-a-1",
                    "producer_id": "producer-1",
                    "sequence": 1,
                    "timestamp": datetime.now(UTC).isoformat(),
                    "event_type": "run",
                    "stage_id": "blocked",
                    "status": "blocked",
                    "message": "The gates refused the candidate at terminal.",
                    "details": {
                        "phase": "terminal",
                        "blocking_failure_count": 1,
                        "blocking_gate_ids": ["uk_target_fit"],
                        "gate_statuses": {"uk_target_fit": "failed"},
                    },
                    "resources": None,
                }
            ]
        },
    )
    headers = {"X-Telemetry-Read-Token": "dashboard-read-token"}
    detail = client.get("/v1/runs/route-a-1", headers=headers)
    listed = client.get("/v1/runs?country=US", headers=headers)

    assert response.status_code == 202
    assert response.json() == {"accepted": 1, "duplicates": 0}
    progress = detail.json()["progress"]
    assert progress["status"] == "blocked"
    assert progress["block"] == {
        "phase": "terminal",
        "blocking_failure_count": 1,
        "blocking_gate_ids": ["uk_target_fit"],
    }
    assert listed.json()["runs"][0]["status"] == "blocked"
    assert listed.json()["runs"][0]["ended_at"] is not None


def test_health_does_not_require_credentials() -> None:
    client = TestClient(
        create_app(
            settings=settings(),
            store=FakeTelemetryStore(),
            huggingface_authenticator=StubHuggingFaceAuthenticator(),
        )
    )
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_maintenance_mode_keeps_health_available_and_rejects_data_requests() -> None:
    client = TestClient(
        create_app(
            settings=replace(settings(), maintenance_mode=True),
            store=FakeTelemetryStore(),
            huggingface_authenticator=StubHuggingFaceAuthenticator(),
        )
    )

    assert client.get("/health").status_code == 200
    unavailable = client.post(
        "/v1/auth/huggingface/exchange",
        headers={"Authorization": "Bearer hf_example"},
    )
    assert unavailable.status_code == 503
    assert unavailable.headers["Retry-After"] == "60"
    assert client.get("/ready").status_code == 503


def test_readiness_checks_store_without_credentials() -> None:
    store = FakeTelemetryStore()
    client = TestClient(
        create_app(
            settings=settings(),
            store=store,
            huggingface_authenticator=StubHuggingFaceAuthenticator(),
        )
    )

    assert client.get("/ready").json() == {"status": "ok"}

    store.is_ready = lambda: False  # type: ignore[method-assign]
    unavailable = client.get("/ready")
    assert unavailable.status_code == 503
    assert unavailable.json()["detail"] == (
        "Telemetry database is unavailable or not fully migrated."
    )


def test_streamed_request_body_is_bounded() -> None:
    client = TestClient(
        create_app(
            settings=settings(),
            store=FakeTelemetryStore(),
            huggingface_authenticator=StubHuggingFaceAuthenticator(),
        )
    )

    def chunks():
        yield b'{"events":[{"details":"'
        yield b"x" * 1_048_576
        yield b'"}]}'

    response = client.post(
        "/v1/runs/unregistered/events",
        headers={"Authorization": "Bearer invalid"},
        content=chunks(),
    )

    assert response.status_code == 413


def test_run_pagination_does_not_skip_equal_timestamps(monkeypatch) -> None:
    fixed = datetime(2026, 10, 2, tzinfo=UTC)
    monkeypatch.setattr("telemetry_collector.storage._now", lambda: fixed)
    store = FakeTelemetryStore()
    principal = HuggingFacePrincipal(
        user_id="hf-user-1",
        username="builder",
        organizations=("policyengine",),
    )
    for index in range(205):
        store.register_run(
            RunRegistration(
                run_id=f"run-{index:03d}",
                producer_id="producer-1",
                country_code="US",
                pipeline="us-fiscal-refresh",
            ),
            principal,
        )
    client = TestClient(
        create_app(
            settings=settings(),
            store=store,
            huggingface_authenticator=StubHuggingFaceAuthenticator(),
        )
    )
    headers = {"X-Telemetry-Read-Token": "dashboard-read-token"}

    first = client.get("/v1/runs?country=US&limit=200", headers=headers)
    cursor = first.json()["next_before"]
    second = client.get(
        f"/v1/runs?country=US&limit=200&before={cursor}", headers=headers
    )

    run_ids = [run["run_id"] for run in first.json()["runs"]]
    run_ids.extend(run["run_id"] for run in second.json()["runs"])
    assert len(run_ids) == 205
    assert len(set(run_ids)) == 205
    assert second.json()["next_before"] is None


def test_run_pagination_is_stable_when_an_older_run_updates(monkeypatch) -> None:
    timestamps = iter(datetime(2026, 10, day, tzinfo=UTC) for day in (1, 2, 3))
    monkeypatch.setattr("telemetry_collector.storage._now", lambda: next(timestamps))
    store = FakeTelemetryStore()
    principal = HuggingFacePrincipal(
        user_id="hf-user-1",
        username="builder",
        organizations=("policyengine",),
    )
    for run_id in ("old", "middle", "new"):
        store.register_run(
            RunRegistration(
                run_id=run_id,
                producer_id="producer-1",
                country_code="US",
                pipeline="us-fiscal-refresh",
            ),
            principal,
        )
    client = TestClient(
        create_app(
            settings=settings(),
            store=store,
            huggingface_authenticator=StubHuggingFaceAuthenticator(),
        )
    )
    headers = {"X-Telemetry-Read-Token": "dashboard-read-token"}

    first = client.get("/v1/runs?country=US&limit=2", headers=headers)
    store.append_events(
        "old",
        "hf-user-1",
        [
            TelemetryEvent(
                schema_version=1,
                event_id="old-updated",
                run_id="old",
                producer_id="producer-1",
                sequence=1,
                timestamp=datetime(2026, 10, 5, tzinfo=UTC),
                event_type="heartbeat",
                stage_id=None,
                status="progress",
                details={},
            )
        ],
    )
    second = client.get(
        f"/v1/runs?country=US&limit=2&before={first.json()['next_before']}",
        headers=headers,
    )

    assert [run["run_id"] for run in first.json()["runs"]] == ["new", "middle"]
    assert [run["run_id"] for run in second.json()["runs"]] == ["old"]


def _encoded_cursor(value: object) -> str:
    return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")


@pytest.mark.parametrize(
    "cursor",
    [
        "not-base64!",
        _encoded_cursor({}),
        _encoded_cursor("cursor"),
        _encoded_cursor([]),
        _encoded_cursor(["2026-10-01T00:00:00+00:00"]),
        _encoded_cursor(["2026-10-01T00:00:00+00:00", "run", "extra"]),
        _encoded_cursor(["not-a-timestamp", "run"]),
        _encoded_cursor(["2026-10-01T00:00:00", "run"]),
        _encoded_cursor(["2026-10-01T00:00:00+00:00", "bad run"]),
    ],
)
def test_invalid_pagination_cursor_returns_422(cursor: str) -> None:
    client = TestClient(
        create_app(
            settings=settings(),
            store=FakeTelemetryStore(),
            huggingface_authenticator=StubHuggingFaceAuthenticator(),
        )
    )

    response = client.get(
        f"/v1/runs?before={cursor}",
        headers={"X-Telemetry-Read-Token": "dashboard-read-token"},
    )

    assert response.status_code == 422
    assert response.json() == {"detail": "Pagination cursor is invalid."}
