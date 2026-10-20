"""Each session token carries one scope, and each endpoint requires its own."""

from __future__ import annotations

from typing import get_args

import jwt
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from telemetry_collector import app as collector_app
from telemetry_collector import models
from telemetry_collector.app import CollectorSettings, create_app
from telemetry_collector.auth import HuggingFacePrincipal
from telemetry_collector.session_tokens import SessionSigningKey
from tests.fakes import FakeTelemetryStore
from tests.signing_keys import p256_private_key_pem

HS256_SECRET = "test-secret-that-is-long-enough-32"
BUILDER = HuggingFacePrincipal(
    user_id="hf-user-1", username="builder", organizations=("policyengine",)
)


class MemberAuthenticator:
    def authenticate(self, token: str, required_org: str) -> HuggingFacePrincipal:
        return BUILDER


@pytest.fixture(params=["es256", "hs256"])
def settings(request: pytest.FixtureRequest) -> CollectorSettings:
    if request.param == "es256":
        return CollectorSettings(
            jwt_secret=None,
            read_token="dashboard-read-token",
            signing_key=SessionSigningKey.from_pem(p256_private_key_pem()),
        )
    return CollectorSettings(jwt_secret=HS256_SECRET, read_token="dashboard-read-token")


def collector_client(settings: CollectorSettings) -> TestClient:
    return TestClient(
        create_app(
            settings=settings,
            store=FakeTelemetryStore(),
            huggingface_authenticator=MemberAuthenticator(),
        )
    )


def session_token(settings: CollectorSettings, scope: str) -> str:
    return collector_app._issue_session_token(settings, BUILDER, scope).access_token


def run_registration() -> dict[str, str]:
    return {
        "run_id": "run-1",
        "producer_id": "producer-1",
        "country_code": "US",
        "pipeline": "us-fiscal-refresh",
    }


def test_session_scopes_are_telemetry_write_and_artifacts_submit() -> None:
    assert get_args(models.SessionScope) == ("telemetry:write", "artifacts:submit")


def test_every_scope_has_its_own_refusal() -> None:
    assert collector_app.SCOPE_REFUSALS == {
        "telemetry:write": "Collector credential cannot write telemetry.",
        "artifacts:submit": "Collector credential cannot submit artifacts.",
    }


def test_exchange_still_issues_telemetry_write_tokens(
    settings: CollectorSettings,
) -> None:
    response = collector_client(settings).post(
        "/v1/auth/huggingface/exchange",
        headers={"Authorization": "Bearer hf_example"},
    )

    access_token = response.json()["access_token"]
    claims = jwt.decode(access_token, options={"verify_signature": False})
    assert claims["scope"] == "telemetry:write"


def test_an_artifacts_submit_token_cannot_register_telemetry_runs(
    settings: CollectorSettings,
) -> None:
    response = collector_client(settings).post(
        "/v1/runs",
        headers={
            "Authorization": f"Bearer {session_token(settings, 'artifacts:submit')}"
        },
        json=run_registration(),
    )

    assert response.status_code == 403
    assert response.json() == {"detail": "Collector credential cannot write telemetry."}


def test_an_artifacts_submit_token_cannot_send_telemetry_events(
    settings: CollectorSettings,
) -> None:
    client = collector_client(settings)
    telemetry_token = session_token(settings, "telemetry:write")
    registration = client.post(
        "/v1/runs",
        headers={"Authorization": f"Bearer {telemetry_token}"},
        json=run_registration(),
    )

    response = client.post(
        "/v1/runs/run-1/events",
        headers={
            "Authorization": f"Bearer {session_token(settings, 'artifacts:submit')}"
        },
        json={"events": []},
    )

    assert registration.status_code == 201
    assert response.status_code == 403
    assert response.json() == {"detail": "Collector credential cannot write telemetry."}


def test_submission_verification_accepts_only_artifacts_submit_tokens(
    settings: CollectorSettings,
) -> None:
    submit_token = session_token(settings, "artifacts:submit")
    telemetry_token = session_token(settings, "telemetry:write")

    claims = collector_app._decode_session_token(
        settings, f"Bearer {submit_token}", "artifacts:submit"
    )
    with pytest.raises(HTTPException) as refusal:
        collector_app._decode_session_token(
            settings, f"Bearer {telemetry_token}", "artifacts:submit"
        )

    assert claims.subject == "hf-user-1"
    assert refusal.value.status_code == 403
    assert refusal.value.detail == "Collector credential cannot submit artifacts."
