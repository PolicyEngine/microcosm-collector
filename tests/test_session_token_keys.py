"""The ES256 key that signs session tokens, and the keys that verify them."""

from __future__ import annotations

import base64
import hashlib
import json

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa
from jwt.algorithms import ECAlgorithm

from telemetry_collector.session_tokens import SIGNING_KEY_VARIABLE, SessionSigningKey
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
