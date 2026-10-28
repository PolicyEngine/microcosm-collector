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


# The claims of every session token. A token issued with lifetime_seconds adds
# "delegated".
SESSION_TOKEN_CLAIMS = {
    "iss",
    "aud",
    "sub",
    "username",
    "organizations",
    "scope",
    "iat",
    "exp",
}


def token_claims(access_token: str) -> dict:
    return jwt.decode(
        access_token,
        JWT_SECRET,
        algorithms=["HS256"],
        audience="microcosm-telemetry-collector",
    )


def exchange(client: TestClient, **request: object):
    return client.post(
        "/v1/auth/huggingface/exchange",
        headers={"Authorization": "Bearer hf_example"},
        **request,
    )


@pytest.mark.parametrize(
    ("intake", "request_body", "scope", "granted_seconds", "delegated"),
    [
        pytest.param(None, None, "telemetry:write", 3600, False, id="no body"),
        pytest.param(
            intake_settings(), {}, "telemetry:write", 3600, False, id="empty object"
        ),
        pytest.param(
            intake_settings(),
            {"scope": "artifacts:submit"},
            "artifacts:submit",
            3600,
            False,
            id="artifact submission",
        ),
        pytest.param(
            None,
            {"lifetime_seconds": 600},
            "telemetry:write",
            600,
            True,
            id="lifetime below the default",
        ),
        pytest.param(
            None,
            {"scope": "telemetry:write", "lifetime_seconds": 64_800},
            "telemetry:write",
            64_800,
            True,
            id="lifetime of 18 hours",
        ),
        pytest.param(
            None,
            {"lifetime_seconds": 86_401},
            "telemetry:write",
            86_400,
            True,
            id="lifetime above the 24-hour cap",
        ),
    ],
)
def test_exchange_issues_a_token_of_the_scope_and_lifetime_granted(
    intake: IntakeSettings | None,
    request_body: dict | None,
    scope: str,
    granted_seconds: int,
    delegated: bool,
) -> None:
    response = exchange(collector_client(intake=intake), json=request_body)

    assert response.status_code == 200
    access_token = response.json()["access_token"]
    assert response.json() == {
        "access_token": access_token,
        "token_type": "bearer",
        "expires_in": granted_seconds,
        "scope": scope,
    }
    claims = token_claims(access_token)
    assert claims["scope"] == scope
    assert claims["exp"] - claims["iat"] == granted_seconds
    added_claims = {
        name: value
        for name, value in claims.items()
        if name not in SESSION_TOKEN_CLAIMS
    }
    assert added_claims == ({"delegated": True} if delegated else {})


def test_exchange_for_artifact_submission_is_not_found_while_intake_is_off() -> None:
    response = exchange(
        collector_client(intake=None), json={"scope": "artifacts:submit"}
    )

    assert response.status_code == 404
    assert response.json() == {
        "detail": "Artifact intake is not enabled on this collector.",
        "code": "intake_not_enabled",
    }


@pytest.mark.parametrize(
    ("request_body", "refusal"),
    [
        pytest.param(
            {"scope": "telemetry:read"},
            "Input should be 'telemetry:write' or 'artifacts:submit'",
            id="unknown scope",
        ),
        pytest.param(
            {"scope": "artifacts:submit", "extra": 1},
            "Extra inputs are not permitted",
            id="unknown field",
        ),
        pytest.param(
            {"lifetime_seconds": 0},
            "Input should be greater than 0",
            id="lifetime of zero",
        ),
        pytest.param(
            {"lifetime_seconds": -3600},
            "Input should be greater than 0",
            id="negative lifetime",
        ),
        pytest.param(
            {"lifetime_seconds": "3600"},
            "Input should be a valid integer",
            id="lifetime as a string",
        ),
        pytest.param(
            {"scope": "artifacts:submit", "lifetime_seconds": 3600},
            "Value error, lifetime_seconds is accepted only with scope "
            "telemetry:write.",
            id="lifetime for artifact submission",
        ),
    ],
)
def test_exchange_refuses_an_invalid_request_body(
    request_body: dict, refusal: str
) -> None:
    response = exchange(collector_client(intake=intake_settings()), json=request_body)

    assert response.status_code == 422
    assert [error["msg"] for error in response.json()["detail"]] == [refusal]


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
