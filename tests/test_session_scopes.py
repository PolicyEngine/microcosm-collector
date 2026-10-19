from __future__ import annotations

import jwt
import pytest
from fastapi.testclient import TestClient

from telemetry_collector.app import CollectorSettings, create_app
from telemetry_collector.auth import HuggingFacePrincipal
from telemetry_collector.intake import IntakeServices, IntakeSettings
from tests.fakes import (
    FakeIntakeObjectStore,
    FakeSubmissionStore,
    FakeTelemetryStore,
    FakeWorkerQueue,
)

JWT_SECRET = "test-secret-that-is-long-enough-32"


class MemberAuthenticator:
    def authenticate(self, token: str, required_org: str) -> HuggingFacePrincipal:
        return HuggingFacePrincipal(
            user_id="hf-user-1", username="builder", organizations=(required_org,)
        )


def intake_settings() -> IntakeSettings:
    return IntakeSettings(
        bucket="collector-intake-test",
        signing_service_account="collector@example-project.iam.gserviceaccount.com",
        worker_queue="projects/example-project/locations/us-central1/queues/intake",
        worker_job="projects/example-project/locations/us-central1/jobs/worker",
        worker_invoker_service_account=(
            "invoker@example-project.iam.gserviceaccount.com"
        ),
    )


def collector_client(*, intake: IntakeSettings | None) -> TestClient:
    settings = CollectorSettings(
        jwt_secret=JWT_SECRET,
        read_token="dashboard-read-token",
        collector_issuer="test-collector",
        intake=intake,
    )
    intake_services = (
        None
        if intake is None
        else IntakeServices(
            submissions=FakeSubmissionStore(),
            objects=FakeIntakeObjectStore(),
            worker_queue=FakeWorkerQueue(),
        )
    )
    return TestClient(
        create_app(
            settings=settings,
            store=FakeTelemetryStore(),
            huggingface_authenticator=MemberAuthenticator(),
            intake_services=intake_services,
        )
    )


def token_scope(access_token: str) -> str:
    claims = jwt.decode(
        access_token,
        JWT_SECRET,
        algorithms=["HS256"],
        audience="microcosm-telemetry-collector",
    )
    return claims["scope"]


def exchange(client: TestClient, **request: object):
    return client.post(
        "/v1/auth/huggingface/exchange",
        headers={"Authorization": "Bearer hf_example"},
        **request,
    )


def test_exchange_without_a_body_still_issues_a_telemetry_token() -> None:
    response = exchange(collector_client(intake=None))

    assert response.status_code == 200
    assert token_scope(response.json()["access_token"]) == "telemetry:write"


def test_exchange_with_an_empty_object_still_issues_a_telemetry_token() -> None:
    response = exchange(collector_client(intake=intake_settings()), json={})

    assert response.status_code == 200
    assert token_scope(response.json()["access_token"]) == "telemetry:write"


def test_exchange_for_artifact_submission_is_not_found_while_intake_is_off() -> None:
    response = exchange(
        collector_client(intake=None), json={"scope": "artifacts:submit"}
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "Artifact intake is not enabled."}


def test_exchange_for_artifact_submission_issues_a_submit_token() -> None:
    response = exchange(
        collector_client(intake=intake_settings()),
        json={"scope": "artifacts:submit"},
    )

    assert response.status_code == 200
    assert token_scope(response.json()["access_token"]) == "artifacts:submit"


@pytest.mark.parametrize(
    "request_body",
    [{"scope": "telemetry:read"}, {"scope": "artifacts:submit", "extra": 1}],
)
def test_exchange_refuses_unknown_scopes_and_fields(request_body: dict) -> None:
    response = exchange(collector_client(intake=intake_settings()), json=request_body)

    assert response.status_code == 422


def test_a_submit_token_cannot_register_telemetry_runs() -> None:
    client = collector_client(intake=intake_settings())
    submit_token = exchange(client, json={"scope": "artifacts:submit"}).json()[
        "access_token"
    ]

    response = client.post(
        "/v1/runs",
        headers={"Authorization": f"Bearer {submit_token}"},
        json={
            "run_id": "run-1",
            "producer_id": "producer-1",
            "country_code": "US",
            "pipeline": "us-fiscal-refresh",
        },
    )

    assert response.status_code == 403
    assert response.json() == {"detail": "Collector credential cannot write telemetry."}
