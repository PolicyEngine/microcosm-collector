from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from telemetry_collector.app import CollectorSettings, create_app
from telemetry_collector.auth import HuggingFacePrincipal
from telemetry_collector.models import RunRegistration
from tests.fakes import FakeTelemetryRepository


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
        run_token_ttl_seconds=900,
    )


def exchange(client: TestClient) -> str:
    response = client.post(
        "/v1/auth/huggingface/exchange",
        headers={"Authorization": "Bearer hf_example"},
        json={
            "run_id": "route-a-1",
            "producer_id": "producer-1",
            "country_code": "US",
            "pipeline": "us-fiscal-refresh",
            "candidate_id": "candidate-1",
            "release_id": None,
            "run_kind": "build",
        },
    )
    assert response.status_code == 200
    return response.json()["access_token"]


def test_exchange_ingest_and_read_run() -> None:
    repository = FakeTelemetryRepository()
    authenticator = StubHuggingFaceAuthenticator()
    client = TestClient(
        create_app(
            settings=settings(),
            repository=repository,
            huggingface_authenticator=authenticator,
        )
    )
    token = exchange(client)
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
        headers={"Authorization": f"Bearer {token}"},
        json={"events": [event]},
    )
    duplicate = client.post(
        "/v1/runs/route-a-1/events",
        headers={"Authorization": f"Bearer {token}"},
        json={"events": [event]},
    )

    assert first.status_code == 202
    assert first.json() == {"accepted": 1, "duplicates": 0}
    assert duplicate.status_code == 202
    assert duplicate.json() == {"accepted": 0, "duplicates": 1}
    assert authenticator.tokens == ["hf_example"]

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
            repository=FakeTelemetryRepository(),
            huggingface_authenticator=StubHuggingFaceAuthenticator(member=False),
        )
    )
    response = client.post(
        "/v1/auth/huggingface/exchange",
        headers={"Authorization": "Bearer hf_outsider"},
        json={
            "run_id": "public-build",
            "producer_id": "producer-1",
            "country_code": "US",
            "pipeline": "us-fiscal-refresh",
            "candidate_id": "candidate-public",
            "run_kind": "build",
        },
    )
    assert response.status_code == 403


def test_run_identifier_cannot_be_reused_with_different_metadata() -> None:
    repository = FakeTelemetryRepository()
    client = TestClient(
        create_app(
            settings=settings(),
            repository=repository,
            huggingface_authenticator=StubHuggingFaceAuthenticator(),
        )
    )
    exchange(client)

    response = client.post(
        "/v1/auth/huggingface/exchange",
        headers={"Authorization": "Bearer hf_example"},
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


def test_run_token_is_bound_to_run_and_producer() -> None:
    client = TestClient(
        create_app(
            settings=settings(),
            repository=FakeTelemetryRepository(),
            huggingface_authenticator=StubHuggingFaceAuthenticator(),
        )
    )
    token = exchange(client)
    response = client.post(
        "/v1/runs/another-run/events",
        headers={"Authorization": f"Bearer {token}"},
        json={"events": []},
    )
    assert response.status_code == 403


def test_failed_validation_event_does_not_finish_the_run() -> None:
    repository = FakeTelemetryRepository()
    client = TestClient(
        create_app(
            settings=settings(),
            repository=repository,
            huggingface_authenticator=StubHuggingFaceAuthenticator(),
        )
    )
    token = exchange(client)
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


def test_health_does_not_require_credentials() -> None:
    client = TestClient(
        create_app(
            settings=settings(),
            repository=FakeTelemetryRepository(),
            huggingface_authenticator=StubHuggingFaceAuthenticator(),
        )
    )
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_streamed_request_body_is_bounded() -> None:
    client = TestClient(
        create_app(
            settings=settings(),
            repository=FakeTelemetryRepository(),
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
    monkeypatch.setattr("telemetry_collector.repository._now", lambda: fixed)
    repository = FakeTelemetryRepository()
    principal = HuggingFacePrincipal(
        user_id="hf-user-1",
        username="builder",
        organizations=("policyengine",),
    )
    for index in range(205):
        repository.register_run(
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
            repository=repository,
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
