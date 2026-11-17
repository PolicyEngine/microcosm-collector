"""Vercel OIDC tokens that consumer apps present to read API v2.

A Vercel deployment holds an OIDC token that Vercel signs for its team (team
issuer mode): issuer ``https://oidc.vercel.com/<team slug>``, audience
``https://vercel.com/<team slug>``, RS256, with the claims ``owner_id`` (the
team's ID), ``project_id`` and ``environment`` (``production``, ``preview`` or
``development``). Read API v2 accepts such a token in ``Authorization: Bearer``
from the projects and environments its settings list as readers.

The token's audience is the team, not this collector, so whoever receives it
can present it to any other party that trusts the same issuer and audience
until it expires. Nothing here logs, stores or returns a token, and no error
message includes one.

Three settings, all or none, switch Vercel OIDC reads on; with none set (unset
or blank) the collector refuses every Vercel OIDC token. ``scripts/deploy.sh``
checks the same patterns before a deployment.
"""

from __future__ import annotations

import os
import re
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import httpx
import jwt

TEAM_SLUG_SETTING = "TELEMETRY_VERCEL_OIDC_TEAM_SLUG"
TEAM_ID_SETTING = "TELEMETRY_VERCEL_OIDC_TEAM_ID"
READERS_SETTING = "TELEMETRY_VERCEL_OIDC_READERS"
SETTING_NAMES = (TEAM_SLUG_SETTING, TEAM_ID_SETTING, READERS_SETTING)

# The patterns use only syntax that Python's re and bash's [[ =~ ]] (POSIX
# extended regular expressions) read alike, since the deploy script holds the
# same text.
TEAM_SLUG_PATTERN = r"^[a-z0-9]([a-z0-9-]{0,98}[a-z0-9])?$"
TEAM_ID_PATTERN = r"^team_[A-Za-z0-9]{1,64}$"
READER_PATTERN = r"^prj_[A-Za-z0-9]{1,64}:(production|preview|development)$"

ISSUER_PREFIX = "https://oidc.vercel.com/"
AUDIENCE_PREFIX = "https://vercel.com/"
# The address Vercel's own verifier (@vercel/oidc's verifyVercelOidcToken)
# reads signing keys from, for team issuers as for the global one.
KEY_SET_URL = "https://oidc.vercel.com/.well-known/jwks"

ALGORITHM = "RS256"
MAXIMUM_TOKEN_LENGTH = 8192
CLOCK_LEEWAY_SECONDS = 30
REQUIRED_CLAIMS = ["exp", "iat", "iss", "aud", "sub"]

FETCH_TIMEOUT_SECONDS = 5
# How long a request whose key isn't cached waits for another thread's fetch
# before answering that the keys are unavailable, so a burst of tokens naming
# keys nobody lists cannot hold every worker thread through a slow fetch.
FETCH_WAIT_SECONDS = 10
# After a successful fetch, a key the set lists is used without fetching again
# for this long.
KEY_SET_FRESH_SECONDS = 10 * 60
# Fetches, successful or not, are at least this far apart.
MINIMUM_FETCH_INTERVAL_SECONDS = 60
# While fetches fail, keys from the last successful fetch stay usable for this
# long after it.
STALE_KEY_LIFETIME_SECONDS = 24 * 60 * 60

KeySetGet = Callable[..., httpx.Response]


class VercelOidcError(Exception):
    """A Vercel OIDC token was not accepted. No message includes the token."""


class InvalidVercelOidcTokenError(VercelOidcError):
    """The token is malformed, expired, or not signed by Vercel for the team."""


class VercelOidcReaderNotAllowedError(VercelOidcError):
    """The token is valid, but its team, project or environment may not read."""


class VercelOidcKeysUnavailableError(VercelOidcError):
    """Vercel's signing keys could not be read, and none usable is cached."""


@dataclass(frozen=True)
class VercelOidcReader:
    """A Vercel project and one of its environments."""

    project_id: str
    environment: str


@dataclass(frozen=True)
class VercelOidcSettings:
    """The Vercel team whose tokens read API v2 accepts, and its readers."""

    team_slug: str
    team_id: str
    readers: frozenset[VercelOidcReader]

    @property
    def issuer(self) -> str:
        return f"{ISSUER_PREFIX}{self.team_slug}"

    @property
    def audience(self) -> str:
        return f"{AUDIENCE_PREFIX}{self.team_slug}"

    @classmethod
    def from_environment(
        cls, environ: Mapping[str, str] = os.environ
    ) -> VercelOidcSettings | None:
        """The settings, or ``None`` while none of them is set.

        Raises ``RuntimeError`` naming each missing or malformed setting,
        without its value.
        """

        values = {name: environ.get(name, "").strip() for name in SETTING_NAMES}
        if not any(values.values()):
            return None
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise RuntimeError(
                f"Vercel OIDC reads need all of {', '.join(SETTING_NAMES)}; "
                f"missing: {' '.join(missing)}."
            )
        team_slug = values[TEAM_SLUG_SETTING]
        team_id = values[TEAM_ID_SETTING]
        entries = values[READERS_SETTING].split()
        if not re.fullmatch(TEAM_SLUG_PATTERN, team_slug):
            raise RuntimeError(f"{TEAM_SLUG_SETTING} is not a valid Vercel team slug.")
        if not re.fullmatch(TEAM_ID_PATTERN, team_id):
            raise RuntimeError(f"{TEAM_ID_SETTING} is not a valid Vercel team ID.")
        if not all(re.fullmatch(READER_PATTERN, entry) for entry in entries):
            raise RuntimeError(
                f"{READERS_SETTING} is not a whitespace-separated list of "
                "<project ID>:<environment> entries."
            )
        readers = frozenset(VercelOidcReader(*entry.split(":", 1)) for entry in entries)
        return cls(team_slug=team_slug, team_id=team_id, readers=readers)


def _usable_keys(answer: Any) -> dict[str, jwt.PyJWK] | None:
    """The RS256 signing keys of a key set, by ``kid``; ``None`` if ``answer``
    is not a key set. The result is empty for a set with no usable key."""

    if not isinstance(answer, dict) or not isinstance(answer.get("keys"), list):
        return None
    keys: dict[str, jwt.PyJWK] = {}
    for jwk in answer["keys"]:
        if (
            not isinstance(jwk, dict)
            or jwk.get("kty") != "RSA"
            or not isinstance(jwk.get("kid"), str)
            or not jwk["kid"]
            or jwk.get("use", "sig") != "sig"
            or jwk.get("alg", ALGORITHM) != ALGORITHM
            or jwk["kid"] in keys
        ):
            continue
        try:
            keys[jwk["kid"]] = jwt.PyJWK(jwk, algorithm=ALGORITHM)
        except (jwt.PyJWTError, KeyError, TypeError, ValueError):
            continue
    return keys


class VercelOidcVerifier:
    """Checks Vercel OIDC tokens against the settings and Vercel's signing keys.

    The keys are cached. A fetch is successful when Vercel answers 200 with a
    key set holding at least one usable key; any other answer, an empty set or
    a set whose keys are all skipped included, is a failed fetch and leaves the
    cached keys as they are. A key from a successful fetch is used without
    fetching again for 10 minutes. A stale set, or a ``kid`` the set doesn't
    list, fetches again, at most once a minute. While fetches fail, keys from
    the last successful fetch stay usable for up to 24 hours from it.

    FastAPI runs the routes' dependencies in a thread pool. One lock is held
    only to read or write the cache's state, never during a fetch; a second
    lets one thread fetch at a time. A request whose key is cached, fresh or
    stale within the 24 hours, never waits for another thread's fetch: a stale
    key is used while that fetch runs. A request whose key isn't cached waits
    for a fetch in progress, for at most ``FETCH_WAIT_SECONDS``, then looks
    again; past that wait it gets ``VercelOidcKeysUnavailableError``.
    """

    def __init__(
        self,
        settings: VercelOidcSettings,
        *,
        http_get: KeySetGet | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.settings = settings
        self._http_get = httpx.get if http_get is None else http_get
        self._clock = time.monotonic if clock is None else clock
        # Held only while reading or writing the four attributes below.
        self._state_lock = threading.Lock()
        # Held by the one thread that is fetching the key set.
        self._fetch_lock = threading.Lock()
        self._fetch_wait_seconds: float = FETCH_WAIT_SECONDS
        self._keys: dict[str, jwt.PyJWK] = {}
        self._fetched_at: float | None = None
        self._attempted_at: float | None = None
        self._last_fetch_failed = False

    def verify(self, token: str) -> VercelOidcReader:
        """The reader the token belongs to.

        Raises ``InvalidVercelOidcTokenError``,
        ``VercelOidcReaderNotAllowedError`` or ``VercelOidcKeysUnavailableError``.
        """

        if len(token) > MAXIMUM_TOKEN_LENGTH:
            raise InvalidVercelOidcTokenError("The token is too long.")
        try:
            header = jwt.get_unverified_header(token)
        except (jwt.PyJWTError, RecursionError, ValueError, TypeError):
            raise InvalidVercelOidcTokenError(
                "The token's header cannot be read."
            ) from None
        key_id = header.get("kid")
        if header.get("alg") != ALGORITHM or not isinstance(key_id, str) or not key_id:
            raise InvalidVercelOidcTokenError(
                "The token's algorithm or key ID is not accepted."
            )
        key = self._signing_key(key_id)
        try:
            claims = jwt.decode(
                token,
                key,
                algorithms=[ALGORITHM],
                audience=self.settings.audience,
                issuer=self.settings.issuer,
                leeway=CLOCK_LEEWAY_SECONDS,
                options={"require": REQUIRED_CLAIMS},
            )
        except (jwt.PyJWTError, RecursionError, ValueError, TypeError):
            # Raised without PyJWT's error, which could quote the token.
            raise InvalidVercelOidcTokenError(
                "The token is not valid for this team, or has expired."
            ) from None
        owner_id = claims.get("owner_id")
        project_id = claims.get("project_id")
        environment = claims.get("environment")
        if not all(
            isinstance(value, str) and value
            for value in (owner_id, project_id, environment)
        ):
            raise InvalidVercelOidcTokenError(
                "The token lacks its owner, project or environment."
            )
        reader = VercelOidcReader(project_id=project_id, environment=environment)
        if owner_id != self.settings.team_id or reader not in self.settings.readers:
            raise VercelOidcReaderNotAllowedError(
                "The token's team, project or environment may not read."
            )
        return reader

    def _signing_key(self, key_id: str) -> jwt.PyJWK:
        with self._state_lock:
            now = self._clock()
            key = self._usable_cached_key(key_id, now)
            if key is not None and self._is_fresh(now):
                return key
            fetch_due = self._fetch_is_due(now)
        if key is not None:
            # A stale key: fetch again if it's time and no other thread is
            # fetching, and otherwise use it without waiting.
            if not fetch_due or not self._fetch_lock.acquire(blocking=False):
                return key
            try:
                self._fetch_if_due()
            finally:
                self._fetch_lock.release()
        else:
            # No usable key: wait for any fetch in progress, for at most
            # FETCH_WAIT_SECONDS, then fetch unless one was attempted within
            # the minute.
            if not self._fetch_lock.acquire(timeout=self._fetch_wait_seconds):
                raise VercelOidcKeysUnavailableError(
                    "Vercel's signing keys could not be read in time."
                )
            try:
                self._fetch_if_due()
            finally:
                self._fetch_lock.release()
        with self._state_lock:
            now = self._clock()
            key = self._usable_cached_key(key_id, now)
            if key is not None:
                return key
            if self._last_fetch_failed:
                raise VercelOidcKeysUnavailableError(
                    "Vercel's signing keys could not be read."
                )
            raise InvalidVercelOidcTokenError(
                "Vercel's key set does not list the token's key."
            )

    # The next three methods read the cache's state: call them holding
    # ``_state_lock``.

    def _usable_cached_key(self, key_id: str, now: float) -> jwt.PyJWK | None:
        """The cached key with this ``kid``, unless the last successful fetch
        was more than 24 hours ago."""

        key = self._keys.get(key_id)
        if (
            key is None
            or self._fetched_at is None
            or now - self._fetched_at > STALE_KEY_LIFETIME_SECONDS
        ):
            return None
        return key

    def _is_fresh(self, now: float) -> bool:
        return (
            self._fetched_at is not None
            and now - self._fetched_at < KEY_SET_FRESH_SECONDS
        )

    def _fetch_is_due(self, now: float) -> bool:
        return (
            self._attempted_at is None
            or now - self._attempted_at >= MINIMUM_FETCH_INTERVAL_SECONDS
        )

    def _fetch_if_due(self) -> None:
        """Fetches the key set unless an attempt was made within the minute.
        Call it holding ``_fetch_lock``; it takes ``_state_lock`` only to read
        and write the cache's state, not during the fetch."""

        with self._state_lock:
            now = self._clock()
            if not self._fetch_is_due(now):
                return
            self._attempted_at = now
        keys = self._fetch()
        with self._state_lock:
            if keys:
                self._keys = keys
                self._fetched_at = now
                self._last_fetch_failed = False
            else:
                # Not a key set, or one with no usable key: the cached keys
                # stay as they are.
                self._last_fetch_failed = True

    def _fetch(self) -> dict[str, jwt.PyJWK] | None:
        """The usable keys of Vercel's key set, or ``None`` if Vercel's answer
        is not a key set."""

        try:
            response = self._http_get(
                KEY_SET_URL,
                timeout=FETCH_TIMEOUT_SECONDS,
                follow_redirects=False,
            )
            if response.status_code != 200:
                return None
            return _usable_keys(response.json())
        except (httpx.HTTPError, RecursionError, ValueError):
            return None
