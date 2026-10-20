"""The ES256 key that signs session tokens, and the keys that verify them."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa
from jwt import (
    ExpiredSignatureError,
    ImmatureSignatureError,
    InvalidAudienceError,
    InvalidIssuerError,
    InvalidSignatureError,
    InvalidTokenError,
    MissingRequiredClaimError,
)
from jwt.algorithms import ECAlgorithm

from telemetry_collector.session_tokens import (
    SIGNING_KEY_VARIABLE,
    SessionSigningKey,
    json_web_key_set,
    sign_session_token,
    verify_session_token,
)
from tests.signing_keys import (
    p256_private_key,
    p256_private_key_pem,
    private_key_pem,
    public_key_pem,
)


def independent_thumbprint(private_key: ec.EllipticCurvePrivateKey) -> str:
    """RFC 7638 thumbprint computed from PyJWT's JWK export, not the collector's."""

    exported = ECAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    required_members = {name: exported[name] for name in ("crv", "kty", "x", "y")}
    canonical = json.dumps(required_members, separators=(",", ":"), sort_keys=True)
    digest = hashlib.sha256(canonical.encode()).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def test_key_id_is_the_rfc_7638_thumbprint_of_the_public_key() -> None:
    private_key = p256_private_key()

    signing_key = SessionSigningKey.from_pem(private_key_pem(private_key))

    assert signing_key.key_id == independent_thumbprint(private_key)


def test_key_id_does_not_depend_on_how_the_key_is_encoded() -> None:
    private_key = p256_private_key()
    pkcs8_pem = private_key_pem(private_key)
    sec1_pem = private_key_pem(
        private_key, private_format=serialization.PrivateFormat.TraditionalOpenSSL
    )

    assert "BEGIN PRIVATE KEY" in pkcs8_pem
    assert "BEGIN EC PRIVATE KEY" in sec1_pem
    assert (
        SessionSigningKey.from_pem(pkcs8_pem).key_id
        == SessionSigningKey.from_pem(sec1_pem).key_id
    )


def test_each_key_has_its_own_key_id() -> None:
    first_key = SessionSigningKey.from_pem(p256_private_key_pem())
    second_key = SessionSigningKey.from_pem(p256_private_key_pem())

    assert first_key.key_id != second_key.key_id


def test_public_jwk_describes_the_es256_verification_key_only() -> None:
    private_key = p256_private_key()
    signing_key = SessionSigningKey.from_pem(private_key_pem(private_key))
    exported = ECAlgorithm.to_jwk(private_key.public_key(), as_dict=True)

    assert signing_key.public_jwk() == {
        "kty": "EC",
        "crv": "P-256",
        "x": exported["x"],
        "y": exported["y"],
        "kid": signing_key.key_id,
        "use": "sig",
        "alg": "ES256",
    }


def test_public_key_matches_the_private_key() -> None:
    private_key = p256_private_key()

    signing_key = SessionSigningKey.from_pem(private_key_pem(private_key))

    assert signing_key.public_key.public_numbers() == (
        private_key.public_key().public_numbers()
    )


def test_signing_key_is_left_out_of_its_representation() -> None:
    signing_key = SessionSigningKey.from_pem(p256_private_key_pem())

    assert "private_key" not in repr(signing_key)
    assert signing_key.key_id in repr(signing_key)


@pytest.mark.parametrize(
    ("unsuitable_key", "expected_message"),
    [
        (
            lambda: private_key_pem(ec.generate_private_key(ec.SECP384R1())),
            "must be an EC P-256 private key",
        ),
        (
            lambda: private_key_pem(
                rsa.generate_private_key(public_exponent=65537, key_size=2048)
            ),
            "must be an EC P-256 private key",
        ),
        (
            lambda: private_key_pem(ed25519.Ed25519PrivateKey.generate()),
            "must be an EC P-256 private key",
        ),
        (
            lambda: public_key_pem(p256_private_key()),
            "must be an unencrypted PEM private key",
        ),
        (
            lambda: private_key_pem(
                p256_private_key(),
                encryption=serialization.BestAvailableEncryption(b"passphrase"),
            ),
            "must be an unencrypted PEM private key",
        ),
        (
            lambda: "not a key",
            "must be an unencrypted PEM private key",
        ),
    ],
    ids=["p384", "rsa", "ed25519", "public-key", "encrypted", "not-pem"],
)
def test_signing_key_refuses_keys_that_cannot_sign_es256_without_echoing_them(
    unsuitable_key, expected_message: str
) -> None:
    unsuitable_pem = unsuitable_key()

    with pytest.raises(ValueError) as refusal:
        SessionSigningKey.from_pem(unsuitable_pem)

    assert str(refusal.value) == f"{SIGNING_KEY_VARIABLE} {expected_message}."
    # The parser's own error is not attached, so a traceback shows nothing of it.
    assert refusal.value.__cause__ is None
    assert refusal.value.__context__ is None or refusal.value.__suppress_context__
    assert unsuitable_pem not in str(refusal.value)


ISSUER = "test-collector"
AUDIENCE = "microcosm-telemetry-collector"
HS256_SECRET = "test-secret-that-is-long-enough-32"


def session_claims(**overrides: object) -> dict[str, object]:
    now = datetime.now(UTC)
    claims: dict[str, object] = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": "hf-user-1",
        "username": "builder",
        "organizations": ["policyengine"],
        "scope": "telemetry:write",
        "iat": now,
        "exp": now + timedelta(minutes=15),
    }
    claims.update(overrides)
    return claims


def es256_token(
    private_key: ec.EllipticCurvePrivateKey,
    key_id: str | None,
    claims: dict[str, object] | None = None,
) -> str:
    headers = {} if key_id is None else {"kid": key_id}
    return jwt.encode(
        claims or session_claims(), private_key, algorithm="ES256", headers=headers
    )


def hand_signed_token(header: dict[str, str], signature_key: bytes | None) -> str:
    """A token signed outside PyJWT, which refuses to build these on purpose."""

    claims = session_claims()
    claims["iat"] = int(claims["iat"].timestamp())  # type: ignore[union-attr]
    claims["exp"] = int(claims["exp"].timestamp())  # type: ignore[union-attr]

    def segment(value: dict[str, object]) -> str:
        encoded = json.dumps(value, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(encoded).decode().rstrip("=")

    signing_input = f"{segment(header)}.{segment(claims)}"
    if signature_key is None:
        return f"{signing_input}."
    signature = hmac.new(signature_key, signing_input.encode(), hashlib.sha256)
    encoded_signature = base64.urlsafe_b64encode(signature.digest()).decode()
    return f"{signing_input}.{encoded_signature.rstrip('=')}"


def verify(
    token: str,
    *,
    signing_key: SessionSigningKey | None,
    hs256_secret: str | None,
) -> dict[str, object]:
    return verify_session_token(
        token,
        signing_key=signing_key,
        hs256_secret=hs256_secret,
        issuer=ISSUER,
        audience=AUDIENCE,
    )


@pytest.mark.parametrize(
    "hs256_secret", [HS256_SECRET, None], ids=["switch-window", "hs256-retired"]
)
def test_es256_token_with_the_configured_key_id_verifies(
    hs256_secret: str | None,
) -> None:
    private_key = p256_private_key()
    signing_key = SessionSigningKey.from_pem(private_key_pem(private_key))

    claims = verify(
        es256_token(private_key, signing_key.key_id),
        signing_key=signing_key,
        hs256_secret=hs256_secret,
    )

    assert claims["sub"] == "hf-user-1"
    assert claims["scope"] == "telemetry:write"


@pytest.mark.parametrize(
    "signing_key_configured", [True, False], ids=["switch-window", "before-es256"]
)
def test_hs256_token_verifies_while_the_hs256_secret_is_configured(
    signing_key_configured: bool,
) -> None:
    signing_key = (
        SessionSigningKey.from_pem(p256_private_key_pem())
        if signing_key_configured
        else None
    )
    token = jwt.encode(session_claims(), HS256_SECRET, algorithm="HS256")

    claims = verify(token, signing_key=signing_key, hs256_secret=HS256_SECRET)

    assert claims["sub"] == "hf-user-1"


def test_hs256_token_is_refused_once_the_hs256_secret_is_removed() -> None:
    signing_key = SessionSigningKey.from_pem(p256_private_key_pem())
    token = jwt.encode(session_claims(), HS256_SECRET, algorithm="HS256")

    with pytest.raises(InvalidTokenError):
        verify(token, signing_key=signing_key, hs256_secret=None)


def test_es256_token_is_refused_while_no_signing_key_is_configured() -> None:
    private_key = p256_private_key()
    key_id = SessionSigningKey.from_pem(private_key_pem(private_key)).key_id

    with pytest.raises(InvalidTokenError):
        verify(
            es256_token(private_key, key_id),
            signing_key=None,
            hs256_secret=HS256_SECRET,
        )


@pytest.mark.parametrize(
    "key_id", ["another-key", None], ids=["another-key-id", "no-key-id"]
)
def test_es256_token_without_the_configured_key_id_is_refused(
    key_id: str | None,
) -> None:
    private_key = p256_private_key()
    signing_key = SessionSigningKey.from_pem(private_key_pem(private_key))

    with pytest.raises(InvalidTokenError):
        verify(
            es256_token(private_key, key_id),
            signing_key=signing_key,
            hs256_secret=HS256_SECRET,
        )


def test_es256_token_signed_by_another_key_under_the_configured_key_id_is_refused() -> (
    None
):
    signing_key = SessionSigningKey.from_pem(p256_private_key_pem())

    with pytest.raises(InvalidSignatureError):
        verify(
            es256_token(p256_private_key(), signing_key.key_id),
            signing_key=signing_key,
            hs256_secret=HS256_SECRET,
        )


@pytest.mark.parametrize(
    "hs256_secret", [HS256_SECRET, None], ids=["switch-window", "hs256-retired"]
)
def test_hs256_token_keyed_with_the_published_public_key_is_refused(
    hs256_secret: str | None,
) -> None:
    # Anyone can read the public key from the JWKS, so it must never be
    # accepted as an HS256 secret.
    private_key = p256_private_key()
    signing_key = SessionSigningKey.from_pem(private_key_pem(private_key))
    forged_token = hand_signed_token(
        {"alg": "HS256", "typ": "JWT"}, public_key_pem(private_key).encode()
    )

    with pytest.raises(InvalidTokenError):
        verify(forged_token, signing_key=signing_key, hs256_secret=hs256_secret)


@pytest.mark.parametrize("algorithm", ["none", "None", "RS256", "ES384"])
def test_token_with_any_other_algorithm_is_refused(algorithm: str) -> None:
    signing_key = SessionSigningKey.from_pem(p256_private_key_pem())
    token = hand_signed_token(
        {"alg": algorithm, "typ": "JWT", "kid": signing_key.key_id}, None
    )

    with pytest.raises(InvalidTokenError):
        verify(token, signing_key=signing_key, hs256_secret=HS256_SECRET)


def switch_window_token(
    algorithm: str,
    private_key: ec.EllipticCurvePrivateKey,
    signing_key: SessionSigningKey,
    claims: dict[str, object],
) -> str:
    """A token as either kind of signer would issue it during the switch."""

    if algorithm == "ES256":
        return es256_token(private_key, signing_key.key_id, claims)
    return jwt.encode(claims, HS256_SECRET, algorithm="HS256")


@pytest.mark.parametrize("algorithm", ["ES256", "HS256"])
@pytest.mark.parametrize(
    ("claim_overrides", "expected_error"),
    [
        ({"aud": "another-service"}, InvalidAudienceError),
        ({"iss": "another-issuer"}, InvalidIssuerError),
        (
            {"exp": datetime.now(UTC) - timedelta(minutes=1)},
            ExpiredSignatureError,
        ),
        (
            {"nbf": datetime.now(UTC) + timedelta(minutes=5)},
            ImmatureSignatureError,
        ),
        (
            {"iat": datetime.now(UTC) + timedelta(minutes=5)},
            ImmatureSignatureError,
        ),
    ],
    ids=["audience", "issuer", "expired", "not-before-later", "issued-later"],
)
def test_token_must_carry_the_collectors_registered_claims(
    algorithm: str,
    claim_overrides: dict[str, object],
    expected_error: type[InvalidTokenError],
) -> None:
    # HS256 tokens issued before the switch go through the same checks as the
    # ES256 tokens issued after it.
    private_key = p256_private_key()
    signing_key = SessionSigningKey.from_pem(private_key_pem(private_key))
    token = switch_window_token(
        algorithm, private_key, signing_key, session_claims(**claim_overrides)
    )

    with pytest.raises(expected_error):
        verify(token, signing_key=signing_key, hs256_secret=HS256_SECRET)


@pytest.mark.parametrize("algorithm", ["ES256", "HS256"])
@pytest.mark.parametrize("missing_claim", ["aud", "exp", "iat", "iss", "scope", "sub"])
def test_token_without_a_required_claim_is_refused(
    algorithm: str, missing_claim: str
) -> None:
    private_key = p256_private_key()
    signing_key = SessionSigningKey.from_pem(private_key_pem(private_key))
    claims = session_claims()
    del claims[missing_claim]
    token = switch_window_token(algorithm, private_key, signing_key, claims)

    with pytest.raises(MissingRequiredClaimError, match=missing_claim):
        verify(token, signing_key=signing_key, hs256_secret=HS256_SECRET)


def test_malformed_token_is_refused() -> None:
    signing_key = SessionSigningKey.from_pem(p256_private_key_pem())

    with pytest.raises(InvalidTokenError):
        verify("not-a-token", signing_key=signing_key, hs256_secret=HS256_SECRET)


@pytest.mark.parametrize(
    "hs256_secret", [HS256_SECRET, None], ids=["switch-window", "hs256-retired"]
)
def test_new_tokens_are_es256_naming_the_key_once_a_signing_key_is_configured(
    hs256_secret: str | None,
) -> None:
    signing_key = SessionSigningKey.from_pem(p256_private_key_pem())

    token = sign_session_token(
        session_claims(), signing_key=signing_key, hs256_secret=hs256_secret
    )

    assert jwt.get_unverified_header(token) == {
        "alg": "ES256",
        "kid": signing_key.key_id,
        "typ": "JWT",
    }
    assert verify(token, signing_key=signing_key, hs256_secret=None)["sub"] == (
        "hf-user-1"
    )


def test_new_tokens_are_hs256_until_a_signing_key_is_configured() -> None:
    token = sign_session_token(
        session_claims(), signing_key=None, hs256_secret=HS256_SECRET
    )

    assert jwt.get_unverified_header(token) == {"alg": "HS256", "typ": "JWT"}
    assert verify(token, signing_key=None, hs256_secret=HS256_SECRET)["sub"] == (
        "hf-user-1"
    )


def test_signing_needs_a_signing_key_or_an_hs256_secret() -> None:
    with pytest.raises(ValueError, match="signing key or an HS256 secret"):
        sign_session_token(session_claims(), signing_key=None, hs256_secret=None)


def test_key_set_lists_the_signing_public_key() -> None:
    signing_key = SessionSigningKey.from_pem(p256_private_key_pem())

    assert json_web_key_set(signing_key) == {"keys": [signing_key.public_jwk()]}


def test_key_set_is_empty_until_a_signing_key_is_configured() -> None:
    assert json_web_key_set(None) == {"keys": []}
