"""Keys that sign and verify the collector's session tokens."""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass, field

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

ES256 = "ES256"
SIGNING_KEY_VARIABLE = "TELEMETRY_JWT_SIGNING_KEY"
P256_COORDINATE_BYTES = 32


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
