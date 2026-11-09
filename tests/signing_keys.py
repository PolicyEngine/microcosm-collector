"""Freshly generated keys for session token tests; no test uses a stored key."""

from __future__ import annotations

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa


def p256_private_key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


def private_key_pem(
    private_key: ec.EllipticCurvePrivateKey
    | rsa.RSAPrivateKey
    | ed25519.Ed25519PrivateKey,
    *,
    private_format: serialization.PrivateFormat = serialization.PrivateFormat.PKCS8,
    encryption: serialization.KeySerializationEncryption | None = None,
) -> str:
    return private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=private_format,
        encryption_algorithm=encryption or serialization.NoEncryption(),
    ).decode("ascii")


def p256_private_key_pem() -> str:
    return private_key_pem(p256_private_key())


def public_key_pem(private_key: ec.EllipticCurvePrivateKey) -> str:
    return (
        private_key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("ascii")
    )
