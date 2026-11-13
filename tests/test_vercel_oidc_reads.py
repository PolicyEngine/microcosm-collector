"""Read API v2 accepts a consumer app's Vercel OIDC token beside the read credential.

A collector configured for Vercel OIDC tokens answers a valid token from one of
its readers as it answers the read credential. Version 1 keeps taking the read
credential alone, and a collector without the settings refuses every token.
"""

from __future__ import annotations

import importlib
import importlib.util
import time
from dataclasses import dataclass, replace
from types import ModuleType

import httpx
import pytest
from fastapi.testclient import TestClient

from telemetry_collector.app import create_app
from tests.fakes import FakeTelemetryStore
from tests.read_api_contract import (
    READ_HEADERS,
    ContractAuthenticator,
    contract_client,
    contract_settings,
    store_contract_telemetry,
)
from tests.vercel_oidc_fakes import (
    OTHER_PROJECT_ID,
    SETTINGS_ENVIRONMENT,
    FakeClock,
    FakeKeySetAddress,
    key_set_answer,
    rsa_private_key,
    rsa_public_jwk,
    signed_token,
    token_claims,
)

VERCEL_OIDC_MODULE = "telemetry_collector.vercel_oidc"
SIGNING_KEY = rsa_private_key()
SIGNING_KEY_ID = "signing-key-1"
V2_PATHS = ["/v2/runs", "/v2/runs/us-run-completed"]
V1_PATHS = ["/v1/runs", "/v1/runs/us-run-completed"]

READ_CREDENTIAL_REQUIRED = {"detail": "A valid dashboard read credential is required."}
OIDC_OFF = {"detail": "This collector accepts no Vercel OIDC tokens."}
INVALID_TOKEN = {"detail": "The Vercel OIDC token is invalid or expired."}
READER_NOT_ALLOWED = {
    "detail": "This Vercel project and environment may not read this collector."
}
KEYS_UNAVAILABLE = {"detail": "Vercel's token signing keys could not be read."}


def vercel_oidc() -> ModuleType:
    assert importlib.util.find_spec(VERCEL_OIDC_MODULE) is not None
    return importlib.import_module(VERCEL_OIDC_MODULE)


@dataclass
class OidcCollector:
    """A collector holding the contract telemetry, configured for the
    dashboard's preview and production Vercel OIDC tokens."""

    client: TestClient
    key_set_address: FakeKeySetAddress


def oidc_collector(
    key_set: httpx.Response | Exception | None = None,
) -> OidcCollector:
    module = vercel_oidc()
    oidc_settings = module.VercelOidcSettings.from_environment(SETTINGS_ENVIRONMENT)
    key_set_address = FakeKeySetAddress(
        key_set_answer(rsa_public_jwk(SIGNING_KEY, SIGNING_KEY_ID))
        if key_set is None
        else key_set
    )
    client = TestClient(
        create_app(
            settings=replace(contract_settings(), vercel_oidc=oidc_settings),
            store=FakeTelemetryStore(),
            huggingface_authenticator=ContractAuthenticator(),
            vercel_oidc_verifier=module.VercelOidcVerifier(
                oidc_settings, http_get=key_set_address.get, clock=FakeClock()
            ),
        )
    )
    store_contract_telemetry(client)
    return OidcCollector(client=client, key_set_address=key_set_address)


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def dashboard_token(**claim_changes: object) -> str:
    return signed_token(SIGNING_KEY, SIGNING_KEY_ID, token_claims(**claim_changes))


@pytest.mark.parametrize("environment", ["preview", "production"])
@pytest.mark.parametrize("request_path", V2_PATHS)
def test_v2_answers_a_reader_s_token_as_it_answers_the_read_credential(
    request_path: str, environment: str
) -> None:
    collector = oidc_collector()

    response = collector.client.get(
        request_path, headers=bearer(dashboard_token(environment=environment))
    )

    assert response.status_code == 200, response.text
    assert response.json() == (
        collector.client.get(request_path, headers=READ_HEADERS).json()
    )


@pytest.mark.parametrize("request_path", V2_PATHS)
def test_v2_refuses_an_invalid_or_expired_token(request_path: str) -> None:
    collector = oidc_collector()
    expired_token = dashboard_token(exp=int(time.time()) - 120)

    for token in (expired_token, "not-a-jwt"):
        response = collector.client.get(request_path, headers=bearer(token))

        assert response.status_code == 401
        assert response.json() == INVALID_TOKEN
        assert token not in response.text


@pytest.mark.parametrize(
    "claim_changes",
    [
        {"owner_id": "team_other"},
        {"project_id": OTHER_PROJECT_ID},
        {"environment": "development"},
    ],
    ids=["another team", "another project", "another environment"],
)
@pytest.mark.parametrize("request_path", V2_PATHS)
def test_v2_refuses_a_token_of_a_project_or_environment_that_may_not_read(
    request_path: str, claim_changes: dict[str, str]
) -> None:
    collector = oidc_collector()

    response = collector.client.get(
        request_path, headers=bearer(dashboard_token(**claim_changes))
    )

    assert response.status_code == 403
    assert response.json() == READER_NOT_ALLOWED


@pytest.mark.parametrize("request_path", V2_PATHS)
def test_v2_answers_503_while_vercel_s_signing_keys_cannot_be_read(
    request_path: str,
) -> None:
    collector = oidc_collector(httpx.ConnectError("Vercel cannot be reached."))

    response = collector.client.get(request_path, headers=bearer(dashboard_token()))

    assert response.status_code == 503
    assert response.json() == KEYS_UNAVAILABLE
    # Only a deployment's 503 gives Retry-After.
    assert "Retry-After" not in response.headers


@pytest.mark.parametrize("request_path", V2_PATHS)
def test_v2_still_accepts_the_read_credential_without_reading_vercel_s_keys(
    request_path: str,
) -> None:
    collector = oidc_collector()

    response = collector.client.get(request_path, headers=READ_HEADERS)

    assert response.status_code == 200, response.text
    assert collector.key_set_address.requests == []


def test_a_matching_read_credential_is_enough_beside_an_invalid_token() -> None:
    collector = oidc_collector()

    response = collector.client.get(
        "/v2/runs", headers={**READ_HEADERS, **bearer("not-a-jwt")}
    )

    assert response.status_code == 200, response.text
    assert collector.key_set_address.requests == []


def test_a_reader_s_token_is_enough_beside_a_wrong_read_credential() -> None:
    collector = oidc_collector()

    response = collector.client.get(
        "/v2/runs",
        headers={
            "X-Telemetry-Read-Token": "not-the-read-token",
            **bearer(dashboard_token()),
        },
    )

    assert response.status_code == 200, response.text


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"X-Telemetry-Read-Token": "not-the-read-token"},
        {"Authorization": "Basic ZGFzaGJvYXJkOnNlY3JldA=="},
        {"Authorization": "Bearer"},
    ],
    ids=["no credential", "a wrong read credential", "basic", "an empty bearer"],
)
@pytest.mark.parametrize("request_path", V2_PATHS)
def test_v2_without_a_credential_it_accepts_answers_as_before(
    request_path: str, headers: dict[str, str]
) -> None:
    collector = oidc_collector()

    response = collector.client.get(request_path, headers=headers)

    assert response.status_code == 401
    assert response.json() == READ_CREDENTIAL_REQUIRED


@pytest.mark.parametrize("request_path", V1_PATHS)
def test_v1_refuses_a_reader_s_token_without_the_read_credential(
    request_path: str,
) -> None:
    collector = oidc_collector()

    refused = collector.client.get(request_path, headers=bearer(dashboard_token()))
    accepted = collector.client.get(request_path, headers=READ_HEADERS)

    assert refused.status_code == 401
    assert refused.json() == READ_CREDENTIAL_REQUIRED
    assert accepted.status_code == 200, accepted.text
    assert collector.key_set_address.requests == []


@pytest.mark.parametrize("request_path", V2_PATHS)
def test_a_collector_without_vercel_oidc_settings_refuses_every_token(
    request_path: str,
) -> None:
    client = contract_client(FakeTelemetryStore())

    response = client.get(request_path, headers=bearer(dashboard_token()))

    assert response.status_code == 401
    assert response.json() == OIDC_OFF


@pytest.mark.parametrize("request_path", V1_PATHS)
def test_v1_of_a_collector_without_vercel_oidc_settings_answers_as_before(
    request_path: str,
) -> None:
    client = contract_client(FakeTelemetryStore())

    response = client.get(request_path, headers=bearer(dashboard_token()))

    assert response.status_code == 401
    assert response.json() == READ_CREDENTIAL_REQUIRED


def test_the_collector_refuses_a_verifier_that_its_settings_do_not_describe() -> None:
    module = vercel_oidc()
    oidc_settings = module.VercelOidcSettings.from_environment(SETTINGS_ENVIRONMENT)
    other_settings = module.VercelOidcSettings.from_environment(
        {**SETTINGS_ENVIRONMENT, "TELEMETRY_VERCEL_OIDC_READERS": "prj_other:preview"}
    )

    for settings, verifier_settings in [
        (contract_settings(), oidc_settings),
        (replace(contract_settings(), vercel_oidc=oidc_settings), other_settings),
    ]:
        with pytest.raises(ValueError):
            create_app(
                settings=settings,
                store=FakeTelemetryStore(),
                huggingface_authenticator=ContractAuthenticator(),
                vercel_oidc_verifier=module.VercelOidcVerifier(verifier_settings),
            )
