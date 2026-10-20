"""The collector signs session tokens with ES256 and accepts HS256 during the switch."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from telemetry_collector.app import CollectorSettings, create_app
from telemetry_collector.auth import HuggingFacePrincipal
from telemetry_collector.session_tokens import SessionSigningKey
from tests.fakes import FakeTelemetryStore
from tests.signing_keys import p256_private_key, private_key_pem

HS256_SECRET = "test-secret-that-is-long-enough-32"
ISSUER = "test-collector"
AUDIENCE = "microcosm-telemetry-collector"
READ_TOKEN = "dashboard-read-token"


class MemberAuthenticator:
    def authenticate(self, token: str, required_org: str) -> HuggingFacePrincipal:
        return HuggingFacePrincipal(
            user_id="hf-user-1", username="builder", organizations=(required_org,)
        )


def generated_signing_key() -> tuple[ec.EllipticCurvePrivateKey, SessionSigningKey]:
    private_key = p256_private_key()
    return private_key, SessionSigningKey.from_pem(private_key_pem(private_key))


def collector_client(
    *, signing_key: SessionSigningKey | None, hs256_secret: str | None
) -> TestClient:
    settings = CollectorSettings(
        jwt_secret=hs256_secret,
        read_token=READ_TOKEN,
        signing_key=signing_key,
        collector_issuer=ISSUER,
        collector_audience=AUDIENCE,
    )
    return TestClient(
        create_app(
            settings=settings,
            store=FakeTelemetryStore(),
            huggingface_authenticator=MemberAuthenticator(),
        )
    )


def session_claims() -> dict[str, object]:
    now = datetime.now(UTC)
    return {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": "hf-user-1",
        "username": "builder",
        "organizations": ["policyengine"],
        "scope": "telemetry:write",
        "iat": now,
        "exp": now + timedelta(minutes=15),
    }


def register_run(client: TestClient, session_token: str):
    return client.post(
        "/v1/runs",
        headers={"Authorization": f"Bearer {session_token}"},
        json={
            "run_id": "run-1",
            "producer_id": "producer-1",
            "country_code": "US",
            "pipeline": "us-fiscal-refresh",
        },
    )


@pytest.mark.parametrize(
    "hs256_secret", [HS256_SECRET, None], ids=["switch-window", "hs256-retired"]
)
def test_registration_accepts_an_es256_token_signed_by_the_configured_key(
    hs256_secret: str | None,
) -> None:
    private_key, signing_key = generated_signing_key()
    client = collector_client(signing_key=signing_key, hs256_secret=hs256_secret)
    es256_token = jwt.encode(
        session_claims(),
        private_key,
        algorithm="ES256",
        headers={"kid": signing_key.key_id},
    )

    response = register_run(client, es256_token)

    assert response.status_code == 201


def test_registration_accepts_an_hs256_token_issued_before_the_switch() -> None:
    _, signing_key = generated_signing_key()
    client = collector_client(signing_key=signing_key, hs256_secret=HS256_SECRET)
    hs256_token = jwt.encode(session_claims(), HS256_SECRET, algorithm="HS256")

    response = register_run(client, hs256_token)

    assert response.status_code == 201


def test_registration_refuses_hs256_tokens_once_the_hs256_secret_is_removed() -> None:
    _, signing_key = generated_signing_key()
    client = collector_client(signing_key=signing_key, hs256_secret=None)
    hs256_token = jwt.encode(session_claims(), HS256_SECRET, algorithm="HS256")

    response = register_run(client, hs256_token)

    assert response.status_code == 401
    assert response.json() == {"detail": "Collector credential is invalid."}


@pytest.mark.parametrize(
    "configured_key", ["another-key", "no-key"], ids=["key-rotated", "before-es256"]
)
def test_token_from_a_key_the_collector_does_not_hold_gets_401(
    configured_key: str,
) -> None:
    # The emitter exchanges its Hugging Face token again after a 401, so a
    # rotated key or a rollback costs one retry; a 403 would end the run's
    # delivery.
    private_key, unknown_key = generated_signing_key()
    _, other_signing_key = generated_signing_key()
    client = collector_client(
        signing_key=other_signing_key if configured_key == "another-key" else None,
        hs256_secret=HS256_SECRET,
    )
    es256_token = jwt.encode(
        session_claims(),
        private_key,
        algorithm="ES256",
        headers={"kid": unknown_key.key_id},
    )

    response = register_run(client, es256_token)

    assert response.status_code == 401
    assert response.json() == {"detail": "Collector credential is invalid."}


def test_collector_needs_a_signing_key_or_an_hs256_secret() -> None:
    with pytest.raises(ValueError, match="signing key or an HS256 secret"):
        CollectorSettings(jwt_secret=None, read_token=READ_TOKEN, signing_key=None)


@pytest.fixture
def collector_environment(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for variable in (
        "TELEMETRY_JWT_SECRET",
        "TELEMETRY_JWT_SIGNING_KEY",
        "TELEMETRY_HF_ORG",
        "TELEMETRY_JWT_ISSUER",
        "TELEMETRY_JWT_AUDIENCE",
        "TELEMETRY_SESSION_TOKEN_TTL_SECONDS",
        "TELEMETRY_MAINTENANCE_MODE",
    ):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("TELEMETRY_READ_TOKEN", "dashboard-read-token-of-24-chars")
    return monkeypatch


def test_settings_read_the_signing_key_and_the_hs256_secret_during_the_switch(
    collector_environment: pytest.MonkeyPatch,
) -> None:
    private_key, signing_key = generated_signing_key()
    collector_environment.setenv(
        "TELEMETRY_JWT_SIGNING_KEY", private_key_pem(private_key)
    )
    collector_environment.setenv("TELEMETRY_JWT_SECRET", HS256_SECRET)

    settings = CollectorSettings.from_environment()

    assert settings.signing_key is not None
    assert settings.signing_key.key_id == signing_key.key_id
    assert settings.jwt_secret == HS256_SECRET


def test_settings_read_the_signing_key_alone_once_hs256_is_retired(
    collector_environment: pytest.MonkeyPatch,
) -> None:
    private_key, signing_key = generated_signing_key()
    collector_environment.setenv(
        "TELEMETRY_JWT_SIGNING_KEY", private_key_pem(private_key)
    )

    settings = CollectorSettings.from_environment()

    assert settings.signing_key is not None
    assert settings.signing_key.key_id == signing_key.key_id
    assert settings.jwt_secret is None


def test_settings_read_the_hs256_secret_alone_until_a_signing_key_is_configured(
    collector_environment: pytest.MonkeyPatch,
) -> None:
    collector_environment.setenv("TELEMETRY_JWT_SECRET", HS256_SECRET)

    settings = CollectorSettings.from_environment()

    assert settings.signing_key is None
    assert settings.jwt_secret == HS256_SECRET


def test_settings_require_a_signing_key_or_an_hs256_secret(
    collector_environment: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(RuntimeError) as refusal:
        CollectorSettings.from_environment()

    assert str(refusal.value) == (
        "TELEMETRY_JWT_SIGNING_KEY or TELEMETRY_JWT_SECRET is required."
    )


def test_settings_refuse_an_unusable_signing_key_without_echoing_it(
    collector_environment: pytest.MonkeyPatch,
) -> None:
    unusable_key = "-----BEGIN PRIVATE KEY-----\nnot-a-key\n-----END PRIVATE KEY-----"
    collector_environment.setenv("TELEMETRY_JWT_SIGNING_KEY", unusable_key)
    collector_environment.setenv("TELEMETRY_JWT_SECRET", HS256_SECRET)

    with pytest.raises(RuntimeError) as refusal:
        CollectorSettings.from_environment()

    assert str(refusal.value) == (
        "TELEMETRY_JWT_SIGNING_KEY must be an unencrypted PEM private key."
    )
    assert "not-a-key" not in str(refusal.value)
    assert refusal.value.__cause__ is None
    assert refusal.value.__suppress_context__


def test_settings_still_refuse_a_short_hs256_secret(
    collector_environment: pytest.MonkeyPatch,
) -> None:
    private_key, _ = generated_signing_key()
    collector_environment.setenv(
        "TELEMETRY_JWT_SIGNING_KEY", private_key_pem(private_key)
    )
    collector_environment.setenv("TELEMETRY_JWT_SECRET", "too-short")

    with pytest.raises(RuntimeError, match="at least 32 characters"):
        CollectorSettings.from_environment()


def exchange(client: TestClient) -> str:
    response = client.post(
        "/v1/auth/huggingface/exchange",
        headers={"Authorization": "Bearer hf_example"},
    )
    assert response.status_code == 200
    return response.json()["access_token"]


def telemetry_event() -> dict[str, object]:
    return {
        "schema_version": 1,
        "event_id": "evt-1",
        "run_id": "run-1",
        "producer_id": "producer-1",
        "sequence": 1,
        "timestamp": datetime.now(UTC).isoformat(),
        "event_type": "stage",
        "stage_id": "target_compilation",
        "status": "started",
        "message": "Compiling targets.",
        "details": {},
    }


@pytest.mark.parametrize(
    "hs256_secret", [HS256_SECRET, None], ids=["switch-window", "hs256-retired"]
)
def test_exchange_issues_an_es256_token_naming_the_signing_key(
    hs256_secret: str | None,
) -> None:
    private_key, signing_key = generated_signing_key()
    client = collector_client(signing_key=signing_key, hs256_secret=hs256_secret)

    session_token = exchange(client)

    assert jwt.get_unverified_header(session_token) == {
        "alg": "ES256",
        "kid": signing_key.key_id,
        "typ": "JWT",
    }
    claims = jwt.decode(
        session_token,
        private_key.public_key(),
        algorithms=["ES256"],
        issuer=ISSUER,
        audience=AUDIENCE,
    )
    assert claims["sub"] == "hf-user-1"
    assert claims["scope"] == "telemetry:write"


def test_exchange_issues_hs256_tokens_until_a_signing_key_is_configured() -> None:
    client = collector_client(signing_key=None, hs256_secret=HS256_SECRET)

    session_token = exchange(client)

    assert jwt.get_unverified_header(session_token)["alg"] == "HS256"
    claims = jwt.decode(
        session_token,
        HS256_SECRET,
        algorithms=["HS256"],
        issuer=ISSUER,
        audience=AUDIENCE,
    )
    assert claims["scope"] == "telemetry:write"


def test_es256_session_token_registers_runs_and_ingests_events() -> None:
    _, signing_key = generated_signing_key()
    client = collector_client(signing_key=signing_key, hs256_secret=None)
    session_token = exchange(client)

    registration = register_run(client, session_token)
    ingestion = client.post(
        "/v1/runs/run-1/events",
        headers={"Authorization": f"Bearer {session_token}"},
        json={"events": [telemetry_event()]},
    )

    assert registration.status_code == 201
    assert ingestion.status_code == 202
    assert ingestion.json() == {"accepted": 1, "duplicates": 0}


def test_jwks_publishes_the_signing_public_key_without_credentials() -> None:
    _, signing_key = generated_signing_key()
    client = collector_client(signing_key=signing_key, hs256_secret=HS256_SECRET)

    response = client.get("/.well-known/jwks.json")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, max-age=300"
    assert response.json() == {"keys": [signing_key.public_jwk()]}
    assert "d" not in response.json()["keys"][0]


def test_jwks_is_empty_until_a_signing_key_is_configured() -> None:
    client = collector_client(signing_key=None, hs256_secret=HS256_SECRET)

    response = client.get("/.well-known/jwks.json")

    assert response.status_code == 200
    assert response.json() == {"keys": []}


def test_a_verifier_holding_only_the_jwks_accepts_issued_tokens() -> None:
    _, signing_key = generated_signing_key()
    client = collector_client(signing_key=signing_key, hs256_secret=HS256_SECRET)
    session_token = exchange(client)
    published_keys = jwt.PyJWKSet.from_dict(client.get("/.well-known/jwks.json").json())

    header = jwt.get_unverified_header(session_token)
    published_key = next(
        key for key in published_keys.keys if key.key_id == header["kid"]
    )
    claims = jwt.decode(
        session_token,
        published_key,
        algorithms=["ES256"],
        issuer=ISSUER,
        audience=AUDIENCE,
    )

    assert claims["sub"] == "hf-user-1"
