"""Vercel OIDC tokens, key sets and a stand-in for Vercel's key set address.

Every key is generated when the tests run; no test uses a stored key or reaches
the network. The team, project and environment values are placeholders.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import threading
import time
from collections.abc import Callable
from typing import Any

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from jwt.algorithms import ECAlgorithm, RSAAlgorithm

TEAM_SLUG = "example-team"
TEAM_ID = "team_example"
DASHBOARD_PROJECT_ID = "prj_dashboard"
OTHER_PROJECT_ID = "prj_other"
ISSUER = f"https://oidc.vercel.com/{TEAM_SLUG}"
AUDIENCE = f"https://vercel.com/{TEAM_SLUG}"
# Where Vercel's own verifier (@vercel/oidc's verifyVercelOidcToken) reads the
# signing keys of every issuer, team issuers included.
KEY_SET_URL = "https://oidc.vercel.com/.well-known/jwks"
# The readers the tests configure: the dashboard's preview and production
# deployments.
READERS = "prj_dashboard:preview prj_dashboard:production"
SETTINGS_ENVIRONMENT = {
    "TELEMETRY_VERCEL_OIDC_TEAM_SLUG": TEAM_SLUG,
    "TELEMETRY_VERCEL_OIDC_TEAM_ID": TEAM_ID,
    "TELEMETRY_VERCEL_OIDC_READERS": READERS,
}


def rsa_private_key() -> rsa.RSAPrivateKey:
    # 2048 bits, the shortest key PyJWT accepts for RS256 without a warning.
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def rsa_public_jwk(
    private_key: rsa.RSAPrivateKey, key_id: str | None, **members: Any
) -> dict[str, Any]:
    """The public key as Vercel lists it: ``kty``, ``n``, ``e``, ``kid``,
    ``alg`` and ``use``, with ``members`` changed or added."""

    jwk: dict[str, Any] = RSAAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    jwk.pop("key_ops", None)
    if key_id is not None:
        jwk["kid"] = key_id
    jwk.update({"alg": "RS256", "use": "sig", **members})
    return {name: value for name, value in jwk.items() if value is not None}


def ec_public_jwk(key_id: str) -> dict[str, Any]:
    private_key = ec.generate_private_key(ec.SECP256R1())
    jwk: dict[str, Any] = ECAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    jwk.pop("key_ops", None)
    return {**jwk, "kid": key_id, "alg": "ES256", "use": "sig"}


def token_claims(
    *,
    project_id: str = DASHBOARD_PROJECT_ID,
    environment: str = "preview",
    without: tuple[str, ...] = (),
    **changed: Any,
) -> dict[str, Any]:
    """The claims of a token Vercel issues to the dashboard's deployments, in
    team issuer mode, valid for an hour from now."""

    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": (f"owner:{TEAM_SLUG}:project:dashboard:environment:{environment}"),
        "owner": TEAM_SLUG,
        "owner_id": TEAM_ID,
        "project": "dashboard",
        "project_id": project_id,
        "environment": environment,
        "iat": now,
        "nbf": now,
        "exp": now + 60 * 60,
        **changed,
    }
    return {name: value for name, value in claims.items() if name not in without}


def signed_token(
    private_key: rsa.RSAPrivateKey,
    key_id: str,
    claims: dict[str, Any] | None = None,
) -> str:
    return jwt.encode(
        token_claims() if claims is None else claims,
        private_key,
        algorithm="RS256",
        headers={"kid": key_id},
    )


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def unsigned_token(header_json: str, claims: dict[str, Any] | None = None) -> str:
    """A token with this exact header text and no signature."""

    payload = json.dumps(token_claims() if claims is None else claims)
    return f"{_base64url(header_json.encode())}.{_base64url(payload.encode())}."


def hmac_signed_token(
    secret: bytes, key_id: str, claims: dict[str, Any] | None = None
) -> str:
    """An HS256 token signed with ``secret``, built without PyJWT, which
    refuses to use a PEM public key as an HMAC secret."""

    header = json.dumps({"alg": "HS256", "typ": "JWT", "kid": key_id})
    payload = json.dumps(token_claims() if claims is None else claims)
    signing_input = f"{_base64url(header.encode())}.{_base64url(payload.encode())}"
    signature = hmac.new(secret, signing_input.encode("ascii"), hashlib.sha256)
    return f"{signing_input}.{_base64url(signature.digest())}"


def key_set_answer(*jwks: Any) -> httpx.Response:
    return httpx.Response(200, json={"keys": list(jwks)})


class FakeClock:
    """A monotonic clock that moves only when a test moves it."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeKeySetAddress:
    """Answers the collector's key set requests as ``httpx.get`` does, and
    records each one: its URL and keyword arguments.

    ``answer`` is the response, or an exception to raise as httpx raises a
    connection failure; a test changes it to change what Vercel serves.
    """

    def __init__(self, answer: httpx.Response | Exception) -> None:
        self.answer = answer
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.before_answering: Callable[[], None] | None = None
        self._lock = threading.Lock()

    def get(self, url: str, **options: Any) -> httpx.Response:
        with self._lock:
            self.requests.append((url, options))
        if self.before_answering is not None:
            self.before_answering()
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer
