"""The check that only microcosm-runs reads Orrery graph files.

The collector serves Orrery graph files (``telemetry_collector.orrery_graph_api``)
only to requests that carry ``Authorization: Bearer <token>``, where the token
is a Google-signed OpenID Connect identity token of the reader service
account, ``COLLECTOR_ORRERY_READER_SERVICE_ACCOUNT``. microcosm-runs gets one
by acting as that account through its Vercel deployment's identity, without a
stored key, and calling the IAM Credentials API's ``generateIdToken`` for it
with the audience ``ORRERY_GRAPHS_AUDIENCE`` and ``includeEmail: true``.
Without ``includeEmail`` the token names no account, and is refused.

The check, in order:

1. A missing ``Authorization`` header, or one that is not ``Bearer <token>``,
   answers 401 with ``identity_token_required``.
2. A token whose header google-auth cannot decode, or that is not signed with
   RS256, answers 401 with ``identity_token_invalid``.
3. Google's published certificates (``GOOGLE_CERTIFICATES_URL``) are fetched
   once and kept for as long as Google's ``Cache-Control`` allows
   (``GoogleCertificates``), not fetched for each request. A failed fetch
   answers 503 with ``identity_certificates_unavailable``.
4. ``google.auth.jwt.decode`` checks the signature against those
   certificates, ``iat`` and ``exp`` (allowing ``CLOCK_SKEW_SECONDS``) and the
   audience; the issuer must then be Google's. Any failure answers 401 with
   ``identity_token_invalid``. This is what
   ``google.oauth2.id_token.verify_oauth2_token`` does after fetching the
   certificates itself on every call; fetching them here instead keeps them
   between requests and keeps a failed fetch (503) apart from a refused
   token (401).
5. ``email`` must be the reader account exactly, and ``email_verified`` the
   boolean ``true``; a valid Google token of any other account answers 403
   with ``identity_not_allowed``.

Every 401 carries ``WWW-Authenticate: Bearer``. No answer or log message holds
the token, and no message from google-auth, which can quote parts of the
token, is passed on.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Annotated, Any, Protocol

import google.auth.jwt
import google.auth.transport.requests
from fastapi import Header
from google.auth.exceptions import GoogleAuthError

from telemetry_collector.intake_api import IntakeProblemError

#: The audience microcosm-runs requests its identity tokens for. The token's
#: account tells the environments apart, so both use this one string.
ORRERY_GRAPHS_AUDIENCE = "microcosm-collector/orrery-graphs"
#: Google's certificates for the identity tokens it signs, as PEM certificates
#: keyed by key ID; ``google.oauth2.id_token`` reads the same address.
GOOGLE_CERTIFICATES_URL = "https://www.googleapis.com/oauth2/v1/certs"
GOOGLE_ISSUERS = frozenset({"accounts.google.com", "https://accounts.google.com"})
SIGNING_ALGORITHM = "RS256"
CLOCK_SKEW_SECONDS = 10
CERTIFICATE_FETCH_TIMEOUT_SECONDS = 10
CERTIFICATES_RETRY_AFTER_SECONDS = 30
BEARER_CHALLENGE = "Bearer"
# What google-auth raises for a token it cannot decode or verify: its own
# MalformedError and InvalidValue are ValueErrors, base64 and JSON errors are
# ValueErrors, and a claim of an unexpected type raises a TypeError.
TOKEN_ERRORS: tuple[type[Exception], ...] = (ValueError, TypeError, GoogleAuthError)


class IdentityTokenRefusedError(Exception):
    """The token is not a valid Google identity token for the audience."""


class GoogleCertificatesUnavailableError(Exception):
    """Google's certificates could not be fetched, so no token can be checked."""


@dataclass(frozen=True)
class FetchedCertificates:
    """Google's certificates, keyed by key ID, and how long they may be kept."""

    certificates: Mapping[str, str]
    lifetime_seconds: float


def certificate_lifetime_seconds(headers: Mapping[str, str]) -> float:
    """How long a response with ``headers`` may be kept: its ``Cache-Control``
    ``max-age`` less its ``Age``, and 0 without a usable ``max-age``, with
    ``no-store`` or with ``no-cache``."""

    by_name = {name.lower(): value for name, value in headers.items()}
    directives = [
        directive.strip().lower()
        for directive in by_name.get("cache-control", "").split(",")
    ]
    if "no-store" in directives or "no-cache" in directives:
        return 0
    max_age: int | None = None
    for directive in directives:
        name, _, value = directive.partition("=")
        if name.strip() == "max-age":
            try:
                max_age = int(value.strip().strip('"'))
            except ValueError:
                return 0
    if max_age is None:
        return 0
    try:
        age = int(by_name.get("age", "0").strip())
    except ValueError:
        age = 0
    return max(0, max_age - max(0, age))


def fetch_google_certificates(
    request: Callable[..., Any] | None = None,
) -> FetchedCertificates:
    """Fetch Google's certificates with ``request``, a
    ``google.auth.transport.requests.Request`` unless one is given.

    Raises ``GoogleCertificatesUnavailableError`` when the endpoint cannot be
    reached, answers anything but 200, or answers anything but a non-empty
    JSON object of text values.
    """

    transport = (
        request if request is not None else google.auth.transport.requests.Request()
    )
    try:
        response = transport(
            url=GOOGLE_CERTIFICATES_URL,
            method="GET",
            timeout=CERTIFICATE_FETCH_TIMEOUT_SECONDS,
        )
    except GoogleAuthError as error:
        raise GoogleCertificatesUnavailableError(
            "Google's certificates could not be fetched."
        ) from error
    if response.status != 200:
        raise GoogleCertificatesUnavailableError(
            f"Google's certificate endpoint answered HTTP {response.status}."
        )
    try:
        certificates = json.loads(response.data)
    except ValueError as error:
        raise GoogleCertificatesUnavailableError(
            "Google's certificate endpoint did not answer JSON."
        ) from error
    if (
        not isinstance(certificates, dict)
        or not certificates
        or not all(
            isinstance(key_id, str) and isinstance(certificate, str)
            for key_id, certificate in certificates.items()
        )
    ):
        raise GoogleCertificatesUnavailableError(
            "Google's certificate endpoint did not answer certificates by key ID."
        )
    return FetchedCertificates(
        certificates=certificates,
        lifetime_seconds=certificate_lifetime_seconds(response.headers),
    )


class GoogleCertificates:
    """Google's certificates, fetched again only once their lifetime is over.

    One instance serves every request of a collector process; the lock lets
    one request fetch while the others wait for its answer.
    """

    def __init__(
        self,
        *,
        fetch: Callable[[], FetchedCertificates] = fetch_google_certificates,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._fetch = fetch
        self._clock = clock
        self._lock = threading.Lock()
        self._certificates: Mapping[str, str] | None = None
        self._expires_at = 0.0

    def current(self) -> Mapping[str, str]:
        """The certificates; raises ``GoogleCertificatesUnavailableError``."""

        with self._lock:
            now = self._clock()
            if self._certificates is None or now >= self._expires_at:
                fetched = self._fetch()
                self._certificates = dict(fetched.certificates)
                self._expires_at = now + fetched.lifetime_seconds
            return self._certificates


class IdentityTokenVerifier(Protocol):
    """Checks a token and returns its claims."""

    def verify(self, token: str) -> Mapping[str, Any]:
        """Raises ``IdentityTokenRefusedError`` or
        ``GoogleCertificatesUnavailableError``."""


class GoogleIdentityTokenVerifier:
    """Verifies Google-signed identity tokens for one audience with google-auth."""

    def __init__(
        self,
        *,
        certificates: GoogleCertificates | None = None,
        audience: str = ORRERY_GRAPHS_AUDIENCE,
        clock_skew_seconds: int = CLOCK_SKEW_SECONDS,
    ) -> None:
        self._certificates = (
            certificates if certificates is not None else GoogleCertificates()
        )
        self._audience = audience
        self._clock_skew_seconds = clock_skew_seconds

    def verify(self, token: str) -> Mapping[str, Any]:
        # google-auth's messages can quote the token, so none is kept.
        try:
            header = google.auth.jwt.decode_header(token)
        except TOKEN_ERRORS:
            raise IdentityTokenRefusedError("The token cannot be decoded.") from None
        if header.get("alg") != SIGNING_ALGORITHM:
            raise IdentityTokenRefusedError("The token is not signed with RS256.")
        certificates = self._certificates.current()
        try:
            claims = google.auth.jwt.decode(
                token,
                certs=certificates,
                audience=self._audience,
                clock_skew_in_seconds=self._clock_skew_seconds,
            )
        except TOKEN_ERRORS:
            raise IdentityTokenRefusedError(
                "The token's signature, lifetime or audience is not valid."
            ) from None
        issuer = claims.get("iss")
        if not isinstance(issuer, str) or issuer not in GOOGLE_ISSUERS:
            raise IdentityTokenRefusedError("Google did not issue the token.")
        return claims


@dataclass(frozen=True)
class OrreryReader:
    """The checked caller: the reader service account."""

    service_account: str


def _token_required() -> IntakeProblemError:
    return IntakeProblemError(
        401,
        "identity_token_required",
        "A Google identity token is required as a Bearer credential.",
        headers={"WWW-Authenticate": BEARER_CHALLENGE},
    )


def _bearer_token(authorization: str | None) -> str:
    scheme, _, token = (authorization or "").partition(" ")
    token = token.strip()
    if (
        scheme.lower() != "bearer"
        or not token
        or any(character.isspace() for character in token)
    ):
        raise _token_required()
    return token


def orrery_reader_check(
    verifier: IdentityTokenVerifier, reader_service_account: str
) -> Callable[..., OrreryReader]:
    """The FastAPI dependency that admits only ``reader_service_account``."""

    def require_orrery_reader(
        authorization: Annotated[str | None, Header()] = None,
    ) -> OrreryReader:
        token = _bearer_token(authorization)
        try:
            claims = verifier.verify(token)
        except GoogleCertificatesUnavailableError:
            raise IntakeProblemError(
                503,
                "identity_certificates_unavailable",
                "Google's certificates for identity tokens are unavailable; "
                "retry later.",
                headers={"Retry-After": str(CERTIFICATES_RETRY_AFTER_SECONDS)},
            ) from None
        except IdentityTokenRefusedError:
            raise IntakeProblemError(
                401,
                "identity_token_invalid",
                "The Google identity token is not valid: its signature, "
                "lifetime, audience or issuer is wrong.",
                headers={"WWW-Authenticate": BEARER_CHALLENGE},
            ) from None
        if (
            claims.get("email") != reader_service_account
            or claims.get("email_verified") is not True
        ):
            raise IntakeProblemError(
                403,
                "identity_not_allowed",
                "Only microcosm-runs' service account may read Orrery graph files.",
            )
        return OrreryReader(service_account=reader_service_account)

    return require_orrery_reader
