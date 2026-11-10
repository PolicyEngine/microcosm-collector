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
2. A token whose header google-auth cannot decode, that is not signed with
   RS256, or that names no key ID (``kid``), answers 401 with
   ``identity_token_invalid``.
3. Google's published certificates (``GOOGLE_CERTIFICATES_URL``) are fetched
   once and kept for as long as Google's ``Cache-Control`` allows
   (``GoogleCertificates``), not fetched for each request:

   - A token whose key ID the kept certificates don't hold fetches them
     again before it is refused, at most once a minute
     (``CERTIFICATE_REFETCH_INTERVAL_SECONDS``), so a key Google starts
     using before the kept certificates expire is accepted.
   - When a fetch fails, the certificates fetched before are still used, for
     up to an hour past their expiry (``CERTIFICATE_GRACE_SECONDS``), with a
     warning in the log, and the fetch is tried again at most once a minute.
     Only without such certificates does the request answer 503 with
     ``identity_certificates_unavailable``.
   - One request fetches at a time. A request the kept certificates can
     check, within their grace period too, never waits for it; only a
     request they can't check waits, and then uses that fetch's outcome.
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
import logging
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
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
#: How long past their expiry the certificates fetched before are still used
#: while they cannot be fetched again.
CERTIFICATE_GRACE_SECONDS = 3600
#: The shortest time between two fetches, from the start of the last one,
#: for a key the kept certificates don't hold, or after a failed fetch while
#: the kept certificates are within their grace period.
CERTIFICATE_REFETCH_INTERVAL_SECONDS = 60
BEARER_CHALLENGE = "Bearer"
# What google-auth raises for a token it cannot decode or verify: its own
# MalformedError and InvalidValue are ValueErrors, base64 and JSON errors are
# ValueErrors, and a claim of an unexpected type raises a TypeError.
TOKEN_ERRORS: tuple[type[Exception], ...] = (ValueError, TypeError, GoogleAuthError)

LOGGER = logging.getLogger(__name__)


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


@dataclass(frozen=True)
class _KeptCertificates:
    """What ``GoogleCertificates`` keeps, replaced whole after each fetch."""

    certificates: Mapping[str, str] | None = None
    #: When the certificates' lifetime ends, by the instance's clock.
    expires_at: float = 0.0
    #: When the last fetch started, whatever its outcome; None before any.
    fetch_started_at: float | None = None
    #: How many fetches have finished, so that a request that waited for
    #: one can tell that another request fetched meanwhile.
    finished_fetches: int = 0


class GoogleCertificates:
    """Google's certificates, kept between requests (see the module's
    docstring for when they are fetched again).

    One instance serves every request of a collector process. One request
    fetches at a time, holding ``_fetch_lock``; the kept certificates are
    replaced whole, never changed, so requests read them without the lock.
    """

    def __init__(
        self,
        *,
        fetch: Callable[[], FetchedCertificates] = fetch_google_certificates,
        clock: Callable[[], float] = time.monotonic,
        grace_seconds: float = CERTIFICATE_GRACE_SECONDS,
        refetch_interval_seconds: float = CERTIFICATE_REFETCH_INTERVAL_SECONDS,
    ) -> None:
        self._fetch = fetch
        self._clock = clock
        self._grace_seconds = grace_seconds
        self._refetch_interval_seconds = refetch_interval_seconds
        self._fetch_lock = threading.Lock()
        self._kept = _KeptCertificates()

    def for_key(self, key_id: str) -> Mapping[str, str]:
        """Certificates to check a token signed with key ``key_id``: they
        hold that key unless Google doesn't publish it.

        Raises ``GoogleCertificatesUnavailableError`` when no certificates
        can be used.
        """

        kept = self._kept
        now = self._clock()
        certificates = kept.certificates
        if certificates is not None and now < kept.expires_at:
            if key_id in certificates or not self._refetch_due(kept, now):
                return certificates
        elif (
            certificates is not None
            and key_id in certificates
            and self._within_grace(kept, now)
        ):
            # Expired, but within the grace period and holding the key: fetch
            # again if one is due and none is running, and never wait.
            if not self._refetch_due(kept, now) or not self._fetch_lock.acquire(
                blocking=False
            ):
                return certificates
            try:
                latest = self._kept
                if latest.finished_fetches != kept.finished_fetches:
                    # Another request fetched since this one looked: a failed
                    # fetch kept the same certificates.
                    return latest.certificates or certificates
                return self._fetch_again(kept, now)
            finally:
                self._fetch_lock.release()
        # The kept certificates can't check the token: there are none, they
        # are past their grace period, or they don't hold its key.
        with self._fetch_lock:
            return self._after_waiting(kept)

    def _within_grace(self, kept: _KeptCertificates, now: float) -> bool:
        return now < kept.expires_at + self._grace_seconds

    def _refetch_due(self, kept: _KeptCertificates, now: float) -> bool:
        return (
            kept.fetch_started_at is None
            or now - kept.fetch_started_at >= self._refetch_interval_seconds
        )

    def _after_waiting(self, seen: _KeptCertificates) -> Mapping[str, str]:
        """Under the lock: use the outcome of a fetch another request made
        while this one waited, or else fetch."""

        kept = self._kept
        now = self._clock()
        if kept.finished_fetches != seen.finished_fetches:
            if kept.certificates is not None and self._within_grace(kept, now):
                return kept.certificates
            raise GoogleCertificatesUnavailableError(
                "Google's certificates could not be fetched."
            )
        if (
            kept.certificates is not None
            and self._within_grace(kept, now)
            and not self._refetch_due(kept, now)
        ):
            # A key the certificates fetched less than a minute ago don't hold.
            return kept.certificates
        return self._fetch_again(kept, now)

    def _fetch_again(
        self, previous: _KeptCertificates, now: float
    ) -> Mapping[str, str]:
        """Under the lock: fetch, keeping ``previous`` within its grace
        period when the fetch fails."""

        try:
            fetched = self._fetch()
        except GoogleCertificatesUnavailableError:
            self._kept = replace(
                previous,
                fetch_started_at=now,
                finished_fetches=previous.finished_fetches + 1,
            )
            if previous.certificates is not None and self._within_grace(previous, now):
                LOGGER.warning(
                    "Google's certificates could not be fetched again, so the "
                    "ones fetched before are still used, for at most %d seconds "
                    "past their expiry.",
                    self._grace_seconds,
                )
                return previous.certificates
            LOGGER.warning(
                "Google's certificates could not be fetched, and none fetched "
                "before can be used, so identity tokens cannot be checked."
            )
            raise
        certificates = dict(fetched.certificates)
        self._kept = _KeptCertificates(
            certificates=certificates,
            expires_at=now + fetched.lifetime_seconds,
            fetch_started_at=now,
            finished_fetches=previous.finished_fetches + 1,
        )
        return certificates


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
        key_id = header.get("kid")
        if not isinstance(key_id, str) or not key_id:
            # Google's tokens always name their key.
            raise IdentityTokenRefusedError("The token names no signing key.")
        certificates = self._certificates.for_key(key_id)
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
