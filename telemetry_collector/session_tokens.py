"""Keys that sign and verify the collector's session tokens.

Once an ES256 key is configured it signs every new token, and its public half
is published as a JWKS. While the HS256 secret is also configured, tokens
signed with it are still accepted, so tokens issued before the switch keep
working until they expire. Removing the secret retires HS256.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

import jwt
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

ES256 = "ES256"
HS256 = "HS256"
SIGNING_KEY_VARIABLE = "TELEMETRY_JWT_SIGNING_KEY"
P256_COORDINATE_BYTES = 32
REQUIRED_CLAIMS = ["aud", "exp", "iat", "iss", "scope", "sub"]


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _public_key_members(public_key: ec.EllipticCurvePublicKey) -> dict[str, str]:
    """The members RFC 7518 requires in an EC public JWK."""

    numbers = public_key.public_numbers()
    return {
        "kty": "EC",
        "crv": "P-256",
        "x": _base64url(numbers.x.to_bytes(P256_COORDINATE_BYTES, "big")),
        "y": _base64url(numbers.y.to_bytes(P256_COORDINATE_BYTES, "big")),
    }


def _jwk_thumbprint(required_members: dict[str, str]) -> str:
    """RFC 7638 SHA-256 thumbprint: the required members, sorted, without spaces."""

    canonical = json.dumps(required_members, separators=(",", ":"), sort_keys=True)
    return _base64url(hashlib.sha256(canonical.encode()).digest())


@dataclass(frozen=True)
class SessionSigningKey:
    """The ES256 private key that signs session tokens, with its key ID.

    The key ID (``kid``) is the RFC 7638 thumbprint of the public key, so it
    is derived rather than configured: it cannot disagree with the key, and a
    new key always has a new ID.
    """

    private_key: ec.EllipticCurvePrivateKey = field(repr=False)
    key_id: str

    @classmethod
    def from_pem(cls, pem: str) -> SessionSigningKey:
        try:
            private_key = serialization.load_pem_private_key(
                pem.encode(), password=None
            )
        except (TypeError, ValueError, UnsupportedAlgorithm):
            # Raised without the parser's error, so no detail of the key's
            # bytes can reach a log or traceback.
            raise ValueError(
                f"{SIGNING_KEY_VARIABLE} must be an unencrypted PEM private key."
            ) from None
        if not isinstance(private_key, ec.EllipticCurvePrivateKey) or not isinstance(
            private_key.curve, ec.SECP256R1
        ):
            raise ValueError(f"{SIGNING_KEY_VARIABLE} must be an EC P-256 private key.")
        key_id = _jwk_thumbprint(_public_key_members(private_key.public_key()))
        return cls(private_key=private_key, key_id=key_id)

    @property
    def public_key(self) -> ec.EllipticCurvePublicKey:
        return self.private_key.public_key()

    def public_jwk(self) -> dict[str, str]:
        """The public key as a JWK, as the collector publishes it."""

        return {
            **_public_key_members(self.public_key),
            "kid": self.key_id,
            "use": "sig",
            "alg": ES256,
        }


def verify_session_token(
    token: str,
    *,
    signing_key: SessionSigningKey | None,
    hs256_secret: str | None,
    issuer: str,
    audience: str,
) -> dict[str, Any]:
    """Return a session token's claims, or raise ``jwt.InvalidTokenError``.

    The header's algorithm chooses the key, and decoding then allows only that
    algorithm, so no key is ever checked as another kind (such as the
    published public key used as an HS256 secret). ES256 needs the configured
    key's ID in the header; HS256 needs the HS256 secret to be configured.
    """

    header = jwt.get_unverified_header(token)
    algorithm = header.get("alg")
    verification_key: ec.EllipticCurvePublicKey | str
    if (
        algorithm == ES256
        and signing_key is not None
        and header.get("kid") == signing_key.key_id
    ):
        verification_key = signing_key.public_key
    elif algorithm == HS256 and hs256_secret is not None:
        verification_key = hs256_secret
    else:
        raise jwt.InvalidTokenError(
            "The session token's algorithm or key ID is not accepted."
        )
    return jwt.decode(
        token,
        verification_key,
        algorithms=[algorithm],
        issuer=issuer,
        audience=audience,
        options={"require": REQUIRED_CLAIMS},
    )
